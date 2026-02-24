from __future__ import annotations

import asyncio
import csv
import fnmatch
import html
import io
import json
import logging
import os
import pickle
import re
import socket
import threading
import time
import zipfile
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from google.transit import gtfs_realtime_pb2
from providers_db_timetables import (
    DBIrisEventChange as ProviderDBIrisEventChange,
    fetch_db_timetables_departures,
    parse_db_iris_fchg_changes as provider_parse_db_iris_fchg_changes,
    parse_db_iris_plan_departures as provider_parse_db_iris_plan_departures,
    parse_db_iris_timestamp as provider_parse_db_iris_timestamp,
)
from providers_gtfs_rt import (
    fetch_feed_bytes as provider_fetch_feed_bytes,
    load_static_gtfs_archive_bytes as provider_load_static_gtfs_archive_bytes,
)
from service_polling import (
    PollingDeps,
    ensure_data_fresh as service_ensure_data_fresh,
    poll_once as service_poll_once,
    run_startup_warmup as service_run_startup_warmup,
)
from web_views import (
    render_logs_html as views_render_logs_html,
    render_service_index_html as views_render_service_index_html,
    render_switch_debug_mode_html as views_render_switch_debug_mode_html,
    render_widget_html as views_render_widget_html,
    render_widget_index_html as views_render_widget_index_html,
)


CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/config.yaml")
FALLBACK_CONFIG_PATH = os.getenv("FALLBACK_CONFIG_PATH", "/app/config/config.yaml.example")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
)


def load_app_version() -> str:
    configured = os.getenv("APP_VERSION", "").strip()
    if configured:
        return configured

    candidates = [
        Path("/app/VERSION"),
        Path(__file__).resolve().with_name("VERSION"),
        Path.cwd() / "VERSION",
    ]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                value = candidate.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except Exception:
            continue
    return "dev"


APP_VERSION = load_app_version()
GTFS_STATIC_URL = os.getenv("GTFS_STATIC_URL", "https://download.gtfs.de/germany/nv_free/latest.zip")
GTFS_STATIC_CACHE_PATH = os.getenv("GTFS_STATIC_CACHE_PATH", "/data/nv_free_latest.zip")
STATIC_FALLBACK_INDEX_CACHE_PATH = os.getenv("STATIC_FALLBACK_INDEX_CACHE_PATH", "/data/static_fallback_index_cache.pkl")
STATIC_FALLBACK_INDEX_CACHE_VERSION = 1
try:
    GTFS_STATIC_CACHE_MAX_AGE_SECONDS = int(os.getenv("GTFS_STATIC_CACHE_MAX_AGE_SECONDS", "43200"))
except ValueError:
    GTFS_STATIC_CACHE_MAX_AGE_SECONDS = 43200
DB_IRIS_BASE_URL = os.getenv("DB_IRIS_BASE_URL", "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1")
DB_TIMETABLES_BASE_URL = os.getenv("DB_TIMETABLES_BASE_URL", DB_IRIS_BASE_URL).rstrip("/")
DB_APIKEY_FILE = os.getenv("DB_APIKEY_FILE", "/config/.dbapikey")


def _read_env_key_value_file(path: str) -> dict[str, str]:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        return {}

    entries: dict[str, str] = {}
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        entries[key] = value
    return entries


def resolve_db_credentials() -> tuple[str, str]:
    env_client_id = os.getenv("DB_CLIENT_ID", "").strip()
    env_api_key = os.getenv("DB_API_KEY", "").strip()
    if env_client_id and env_api_key:
        return env_client_id, env_api_key

    apikey_file = str(os.getenv("DB_APIKEY_FILE", DB_APIKEY_FILE)).strip() or "/config/.dbapikey"
    file_values = _read_env_key_value_file(apikey_file)
    file_client_id = str(file_values.get("DB_CLIENT_ID", "")).strip()
    file_api_key = str(file_values.get("DB_API_KEY", "")).strip()
    return env_client_id or file_client_id, env_api_key or file_api_key


DB_CLIENT_ID, DB_API_KEY = resolve_db_credentials()
DEFAULT_DEBUG_LOG_PATH = "/logs/logfile.txt"
DEFAULT_FALLBACK_LOG_PATH = "/tmp/timetable-widget/logfile.txt"
LOG_TAIL_LINES = 300
DEBUG_LOG_PATH = DEFAULT_DEBUG_LOG_PATH
ACTIVE_LOG_PATH = DEFAULT_DEBUG_LOG_PATH
WARMUP_ON_START = str(os.getenv("WARMUP_ON_START", "0")).strip().lower() in {"1", "true", "yes", "on"}
WARMUP_STATIC_CACHE_ON_START = str(os.getenv("WARMUP_STATIC_CACHE_ON_START", "1")).strip().lower() in {"1", "true", "yes", "on"}
LOCAL_TIMEZONE_NAME = os.getenv("LOCAL_TIMEZONE", "Europe/Berlin")
try:
    LOCAL_TIMEZONE = ZoneInfo(LOCAL_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    LOCAL_TIMEZONE = ZoneInfo("UTC")

DIRECTION_MAPPING_PATH = os.getenv("DIRECTION_MAPPING_PATH", "/config/direction_overrides.txt")
DIRECTION_MAPPING_SEPARATOR = os.getenv("DIRECTION_MAPPING_SEPARATOR", "|")
try:
    DIRECTION_MAPPING_RELOAD_SECONDS = int(os.getenv("DIRECTION_MAPPING_RELOAD_SECONDS", "15"))
except ValueError:
    DIRECTION_MAPPING_RELOAD_SECONDS = 15
DIRECTION_MAPPING_FILE_LOCK = threading.Lock()


def to_local_datetime(epoch_seconds: int) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=LOCAL_TIMEZONE)


def detect_instance_ip() -> str:
    configured_ip = os.getenv("LOG_INSTANCE_IP", "").strip()
    if configured_ip:
        return configured_ip

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            detected = str(probe.getsockname()[0]).strip()
            if detected:
                return detected
    except Exception:
        pass

    try:
        detected = str(socket.gethostbyname(socket.gethostname())).strip()
        if detected:
            return detected
    except Exception:
        pass

    return "unknown"


INSTANCE_HOSTNAME = socket.gethostname()
INSTANCE_IP = detect_instance_ip()


DEBUG_LOGGER_LOCK = threading.Lock()
DEBUG_ENABLED = False
DEBUG_LOGGER: Optional[logging.Logger] = None


def _close_logger_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def _prepare_writable_log_path(path: str) -> tuple[Optional[Path], Optional[str]]:
    preferred = Path(path)
    candidates = [preferred]
    fallback = Path(DEFAULT_FALLBACK_LOG_PATH)
    if fallback != preferred:
        candidates.append(fallback)

    errors: list[str] = []
    for target in candidates:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.touch()
            if target != preferred:
                return target, f"Primaerer Logpfad nicht schreibbar ({preferred}). Nutze Fallback {target}."
            return target, None
        except Exception as exc:
            errors.append(f"{target}: {exc}")
    return None, f"Logdatei konnte nicht vorbereitet werden: {' | '.join(errors)}"


def configure_debug_logger(enabled: bool, log_path: Optional[str] = None) -> tuple[bool, str]:
    global DEBUG_ENABLED
    global DEBUG_LOGGER
    global DEBUG_LOG_PATH
    global ACTIVE_LOG_PATH

    with DEBUG_LOGGER_LOCK:
        if log_path is not None:
            candidate = str(log_path).strip()
            if candidate:
                DEBUG_LOG_PATH = candidate
        if not DEBUG_LOG_PATH:
            DEBUG_LOG_PATH = DEFAULT_DEBUG_LOG_PATH

        logger = logging.getLogger("timetable_widget_debug")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        resolved_path, path_warning = _prepare_writable_log_path(DEBUG_LOG_PATH)
        if resolved_path is None:
            _close_logger_handlers(logger)
            DEBUG_LOGGER = None
            DEBUG_ENABLED = False
            ACTIVE_LOG_PATH = DEBUG_LOG_PATH
            return False, path_warning or "Debug-Modus konnte nicht aktiviert werden."

        try:
            _close_logger_handlers(logger)
            handler = logging.FileHandler(resolved_path, encoding="utf-8")
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s [host=%(hostname)s ip=%(instance_ip)s] %(message)s"
                )
            )
            logger.addHandler(handler)
            adapter = logging.LoggerAdapter(
                logger,
                {
                    "hostname": INSTANCE_HOSTNAME,
                    "instance_ip": INSTANCE_IP,
                },
            )
            ACTIVE_LOG_PATH = str(resolved_path)
            DEBUG_LOGGER = adapter
            DEBUG_ENABLED = enabled
            if path_warning:
                adapter.warning(path_warning)
            if enabled:
                adapter.info("Debug-Modus aktiviert. Log-Datei: %s", resolved_path)
                return True, f"Debug-Modus aktiviert. Log-Datei: {resolved_path}"
            adapter.info("Debug-Modus deaktiviert. Basis-Logging bleibt aktiv. Log-Datei: %s", resolved_path)
            return True, f"Debug-Modus deaktiviert. Basis-Logging aktiv. Log-Datei: {resolved_path}"
        except Exception as exc:
            _close_logger_handlers(logger)
            DEBUG_LOGGER = None
            DEBUG_ENABLED = False
            ACTIVE_LOG_PATH = DEBUG_LOG_PATH
            return False, f"Logging konnte nicht konfiguriert werden: {exc}"


def get_debug_status() -> dict:
    with DEBUG_LOGGER_LOCK:
        return {
            "enabled": DEBUG_ENABLED,
            "log_path": DEBUG_LOG_PATH,
            "active_log_path": ACTIVE_LOG_PATH,
            "active_logger": DEBUG_LOGGER is not None,
            "instance_host": INSTANCE_HOSTNAME,
            "instance_ip": INSTANCE_IP,
        }


configure_debug_logger(False, DEBUG_LOG_PATH)


def app_log(message: str) -> None:
    logger = DEBUG_LOGGER
    if logger:
        logger.info(message)


def debug_log(message: str) -> None:
    if not DEBUG_ENABLED:
        return
    logger = DEBUG_LOGGER
    if logger:
        logger.info("[DEBUG] %s", message)


def _normalize_direction_mapping_value(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    # tolerate replacement characters from bad encodings (e.g. Universit?t)
    text = text.replace("?", "?")
    return text


def _normalize_direction_mapping_key(route: str, direction: str) -> tuple[str, str]:
    return _normalize_direction_mapping_value(route), _normalize_direction_mapping_value(direction)


def _is_wildcard_pattern(value: str) -> bool:
    return "?" in value or "*" in value


def _matches_direction_pattern(
    route_key: str,
    direction_key: str,
    patterns: list[tuple[str, str, str]],
) -> Optional[str]:
    for route_pattern, direction_pattern, label in patterns:
        if fnmatch.fnmatchcase(route_key, route_pattern) and fnmatch.fnmatchcase(direction_key, direction_pattern):
            return label
    return None


def _sanitize_direction_mapping_field(value: str) -> str:
    text = re.sub(r"[\r\n]+", " ", str(value or "")).strip()
    if DIRECTION_MAPPING_SEPARATOR:
        text = text.replace(DIRECTION_MAPPING_SEPARATOR, "/")
    return re.sub(r"\s+", " ", text).strip()


def load_direction_mapping_file(
    path: str,
) -> tuple[dict[tuple[str, str], str], list[tuple[str, str, str]], set[tuple[str, str]], Optional[str]]:
    target = Path(path)
    mapping: dict[tuple[str, str], str] = {}
    patterns: list[tuple[str, str, str]] = []
    known_keys: set[tuple[str, str]] = set()

    try:
        with DIRECTION_MAPPING_FILE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("# route|direction|label\n", encoding="utf-8")
                return mapping, patterns, known_keys, None

            lines = target.read_text(encoding="utf-8").splitlines()

        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(DIRECTION_MAPPING_SEPARATOR)
            if len(parts) < 2:
                continue
            route = _sanitize_direction_mapping_field(parts[0])
            direction = _sanitize_direction_mapping_field(parts[1])
            label = _sanitize_direction_mapping_field(parts[2]) if len(parts) >= 3 else ""
            if not route or not direction:
                continue

            route_key = _normalize_direction_mapping_value(route)
            direction_key = _normalize_direction_mapping_value(direction)
            key = (route_key, direction_key)
            known_keys.add(key)

            if _is_wildcard_pattern(route_key) or _is_wildcard_pattern(direction_key):
                patterns.append((route_key, direction_key, label))
            else:
                mapping[key] = label
        return mapping, patterns, known_keys, None
    except Exception as exc:
        return mapping, patterns, known_keys, f"direction mapping load failed: {exc}"


def append_direction_mapping_entries(
    path: str,
    observed_entries: list[tuple[str, str]],
    known_keys: set[tuple[str, str]],
) -> tuple[int, set[tuple[str, str]], Optional[str]]:
    if not observed_entries:
        return 0, set(), None

    target = Path(path)
    existing_keys = set(known_keys)
    added_keys: set[tuple[str, str]] = set()
    added_count = 0

    try:
        with DIRECTION_MAPPING_FILE_LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("# route|direction|label\n", encoding="utf-8")

            for raw_line in target.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(DIRECTION_MAPPING_SEPARATOR)
                if len(parts) < 2:
                    continue
                route = _sanitize_direction_mapping_field(parts[0])
                direction = _sanitize_direction_mapping_field(parts[1])
                if not route or not direction:
                    continue
                existing_keys.add(_normalize_direction_mapping_key(route, direction))

            with target.open("a", encoding="utf-8") as handle:
                for route_raw, direction_raw in observed_entries:
                    route = _sanitize_direction_mapping_field(route_raw)
                    direction = _sanitize_direction_mapping_field(direction_raw)
                    if not route or not direction:
                        continue
                    key = _normalize_direction_mapping_key(route, direction)
                    if key in existing_keys or key in added_keys:
                        continue
                    handle.write(f"{route}{DIRECTION_MAPPING_SEPARATOR}{direction}{DIRECTION_MAPPING_SEPARATOR}\n")
                    added_keys.add(key)
                    added_count += 1
        return added_count, added_keys, None
    except Exception as exc:
        return added_count, added_keys, f"direction mapping append failed: {exc}"


def apply_direction_labels(
    departures: list[Departure],
    direction_labels: dict[tuple[str, str], str],
    direction_label_patterns: list[tuple[str, str, str]],
) -> dict[tuple[str, str], tuple[str, str]]:
    observed: dict[tuple[str, str], tuple[str, str]] = {}
    for dep in departures:
        dep.direction_label = None
        route = str(dep.route or "").strip()
        direction = str(dep.direction or "").strip()
        if not route or not direction:
            continue
        key = _normalize_direction_mapping_key(route, direction)
        label = str(direction_labels.get(key, "") or "").strip()
        if not label and direction_label_patterns:
            wildcard_label = _matches_direction_pattern(key[0], key[1], direction_label_patterns)
            if wildcard_label is not None:
                label = str(wildcard_label or "").strip()
        if label:
            dep.direction_label = label
        observed.setdefault(key, (route, direction))
    return observed


async def refresh_direction_mapping_if_due(state: RuntimeState, force: bool = False) -> None:
    now_monotonic = time.monotonic()
    async with state.lock:
        should_reload = (
            force
            or not state.known_direction_keys
            or now_monotonic >= state.next_direction_mapping_reload_monotonic
        )
    if not should_reload:
        return

    mapping, patterns, known_keys, load_error = await asyncio.to_thread(load_direction_mapping_file, DIRECTION_MAPPING_PATH)
    async with state.lock:
        state.direction_labels = mapping
        state.direction_label_patterns = patterns
        state.known_direction_keys = known_keys
        state.direction_mapping_error = load_error
        state.next_direction_mapping_reload_monotonic = time.monotonic() + max(5, DIRECTION_MAPPING_RELOAD_SECONDS)

    debug_log(
        "direction_mapping:refresh "
        f"keys={len(known_keys)} labels={sum(1 for value in mapping.values() if value)} patterns={len(patterns)} "
        f"error={bool(load_error)}"
    )


async def register_observed_direction_entries(
    state: RuntimeState,
    observed: dict[tuple[str, str], tuple[str, str]],
) -> None:
    if not observed:
        return

    async with state.lock:
        known_keys = set(state.known_direction_keys)
        direction_label_patterns = list(state.direction_label_patterns)

    missing_entries: list[tuple[str, str]] = []
    for key, raw_value in observed.items():
        if key in known_keys:
            continue
        if _matches_direction_pattern(key[0], key[1], direction_label_patterns) is not None:
            continue
        missing_entries.append(raw_value)

    if not missing_entries:
        return

    added_count, added_keys, write_error = await asyncio.to_thread(
        append_direction_mapping_entries,
        DIRECTION_MAPPING_PATH,
        missing_entries,
        known_keys,
    )

    async with state.lock:
        if write_error:
            state.direction_mapping_error = write_error
        for key in added_keys:
            state.known_direction_keys.add(key)
            state.direction_labels.setdefault(key, "")

    debug_log(
        "direction_mapping:append "
        f"observed={len(observed)} missing={len(missing_entries)} added={added_count} error={bool(write_error)}"
    )


@dataclass
class ServerConfig:
    host: str
    port: int


@dataclass
class FeedConfig:
    url: str
    refresh_seconds: int
    http_timeout_seconds: int


@dataclass
class DebugConfig:
    enabled: bool
    log_path: str


@dataclass
class WidgetConfig:
    id: str
    title: str
    stop_ids: list[str]
    route_short_names: Optional[list[str]]
    source: str
    db_eva_no: Optional[str]
    direction_contains: Optional[list[str]]
    required_stops: Optional[list[str]]
    db_lookahead_hours: int
    db_only_trains: bool
    db_use_fchg: bool
    gtfs_lookahead_hours: int
    max_departures: int
    show_delay: bool
    show_feed_age: bool


@dataclass
class MappingConfig:
    trip_route_map_csv: str
    reload_every_seconds: int


@dataclass
class AppConfig:
    server: ServerConfig
    feed: FeedConfig
    debug: DebugConfig
    widgets: list[WidgetConfig]
    mapping: MappingConfig


@dataclass
class Departure:
    route: str
    direction: str
    platform: Optional[str]
    stop_id: str
    time_epoch: int
    time_local: str
    in_min: int
    delay_s: Optional[int]
    trip_id: str
    direction_label: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "direction": self.direction,
            "direction_label": self.direction_label,
            "platform": self.platform,
            "stop_id": self.stop_id,
            "time_epoch": self.time_epoch,
            "time_local": self.time_local,
            "in_min": self.in_min,
            "delay_s": self.delay_s,
            "trip_id": self.trip_id,
        }


@dataclass
class StaticFallbackIndex:
    stop_entries: dict[str, list[tuple[str, int]]]
    trip_route: dict[str, str]
    trip_direction: dict[str, str]
    event_direction: dict[tuple[str, int, str], str]
    target_stop_name: dict[str, str]
    trip_service: dict[str, str]
    service_weekdays: dict[str, tuple[int, int, int, int, int, int, int]]
    service_date_range: dict[str, tuple[date, date]]
    service_exceptions: dict[str, dict[date, int]]


@dataclass
class RuntimeState:
    config: AppConfig
    departures_by_widget: dict[str, list[Departure]] = field(default_factory=dict)
    fetched_at_epoch: Optional[int] = None
    errors_by_widget: dict[str, list[str]] = field(default_factory=dict)
    route_map: dict[str, str] = field(default_factory=dict)
    trip_destination_map: dict[str, str] = field(default_factory=dict)
    mapping_error: Optional[str] = None
    next_mapping_reload_monotonic: float = 0.0
    next_refresh_due_monotonic: float = 0.0
    known_stop_ids: set[str] = field(default_factory=set)
    known_stop_ids_error: Optional[str] = None
    next_known_stop_ids_reload_monotonic: float = 0.0
    static_fallback_index: Optional[StaticFallbackIndex] = None
    static_fallback_error: Optional[str] = None
    next_static_fallback_reload_monotonic: float = 0.0
    direction_labels: dict[tuple[str, str], str] = field(default_factory=dict)
    direction_label_patterns: list[tuple[str, str, str]] = field(default_factory=list)
    known_direction_keys: set[tuple[str, str]] = field(default_factory=set)
    direction_mapping_error: Optional[str] = None
    next_direction_mapping_reload_monotonic: float = 0.0
    extended_departures_cache: dict[str, tuple[float, list[Departure], list[str], Optional[int]]] = field(
        default_factory=dict
    )
    refresh_task: Optional[asyncio.Task] = None
    startup_task: Optional[asyncio.Task] = None
    startup_ready: bool = False
    startup_error: Optional[str] = None
    startup_ready_since_epoch: Optional[int] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def load_yaml(path: str) -> dict:
    target = Path(path)
    if target.is_dir():
        candidates = [
            target / "config.yaml",
            target / "config.yml",
            target / "config.yaml.example",
            Path(FALLBACK_CONFIG_PATH),
        ]
        replacement = next((candidate for candidate in candidates if candidate.is_file()), None)
        if replacement is None:
            raise ValueError(
                f"CONFIG_PATH points to directory '{path}', and no fallback config file was found. "
                f"Checked: {[str(candidate) for candidate in candidates]}"
            )
        debug_log(f"load_yaml: CONFIG_PATH is directory ({path}); using fallback file {replacement}")
        target = replacement

    if not target.is_file():
        fallback = Path(FALLBACK_CONFIG_PATH)
        if fallback.is_file():
            debug_log(f"load_yaml: config file not found at {target}; using fallback file {fallback}")
            target = fallback
        else:
            raise ValueError(f"config file not found: {target}")
    with target.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def _get_section(data: dict, key: str) -> dict:
    section = data.get(key, {})
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(f"{key} must be a mapping")
    return section


def _to_int(value, default: int, key: str, min_value: int = 0, max_value: Optional[int] = None) -> int:
    candidate = default if value is None else value
    try:
        parsed = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < min_value:
        raise ValueError(f"{key} must be >= {min_value}")
    if max_value is not None and parsed > max_value:
        raise ValueError(f"{key} must be <= {max_value}")
    return parsed


def _to_str(value, default: str) -> str:
    candidate = default if value is None else value
    return str(candidate).strip()


def _to_non_empty_str(value, default: str, key: str) -> str:
    text = _to_str(value, default)
    if not text:
        raise ValueError(f"{key} must not be empty")
    return text


def _to_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _to_str_list(value, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _normalize_widget_source(value, key: str) -> str:
    text = _to_non_empty_str(value, "gtfs_rt", key).lower()
    if text in {"gtfs_rt", "gtfs", "gtfs-realtime"}:
        return "gtfs_rt"
    if text in {"db_iris", "db_timetables", "db"}:
        return "db_iris"
    raise ValueError(f"{key} has unsupported source '{text}'")


def parse_config(data: dict) -> AppConfig:
    server_data = _get_section(data, "server")
    feed_data = _get_section(data, "feed")
    debug_data = _get_section(data, "debug")
    mapping_data = _get_section(data, "mapping")

    server = ServerConfig(
        host=_to_str(server_data.get("host"), "0.0.0.0"),
        port=_to_int(server_data.get("port"), 8000, "server.port", min_value=1, max_value=65535),
    )
    feed = FeedConfig(
        url=_to_str(feed_data.get("url"), ""),
        refresh_seconds=_to_int(feed_data.get("refresh_seconds"), 30, "feed.refresh_seconds", min_value=1),
        http_timeout_seconds=_to_int(
            feed_data.get("http_timeout_seconds"), 15, "feed.http_timeout_seconds", min_value=1
        ),
    )
    debug = DebugConfig(
        enabled=_to_bool(debug_data.get("enabled"), False),
        log_path=_to_non_empty_str(debug_data.get("log_path"), DEFAULT_DEBUG_LOG_PATH, "debug.log_path"),
    )
    widgets_data = data.get("widgets")
    if widgets_data is None:
        raise ValueError("widgets section is required; old single-widget config format is no longer supported")
    if not isinstance(widgets_data, list):
        raise ValueError("widgets must be a list")

    widgets: list[WidgetConfig] = []
    for idx, widget_data in enumerate(widgets_data):
        if not isinstance(widget_data, dict):
            raise ValueError(f"widgets[{idx}] must be a mapping")
        route_short_names: Optional[list[str]] = None
        if "route_short_names" in widget_data and widget_data.get("route_short_names") is not None:
            route_short_names = _to_str_list(
                widget_data.get("route_short_names"), f"widgets[{idx}].route_short_names"
            )
        direction_contains: Optional[list[str]] = None
        if "direction_contains" in widget_data and widget_data.get("direction_contains") is not None:
            direction_contains = _to_str_list(
                widget_data.get("direction_contains"), f"widgets[{idx}].direction_contains"
            )
        required_stops: Optional[list[str]] = None
        if "required_stops" in widget_data and widget_data.get("required_stops") is not None:
            required_stops = _to_str_list(
                widget_data.get("required_stops"), f"widgets[{idx}].required_stops"
            )
        widgets.append(
            WidgetConfig(
                id=_to_non_empty_str(widget_data.get("id"), str(idx + 1), f"widgets[{idx}].id"),
                title=_to_non_empty_str(widget_data.get("title"), f"Widget {idx + 1}", f"widgets[{idx}].title"),
                stop_ids=_to_str_list(widget_data.get("stop_ids", []), f"widgets[{idx}].stop_ids"),
                route_short_names=route_short_names,
                source=_normalize_widget_source(widget_data.get("source", "gtfs_rt"), f"widgets[{idx}].source"),
                db_eva_no=_to_str(widget_data.get("db_eva_no"), "") or None,
                direction_contains=direction_contains,
                required_stops=required_stops,
                db_lookahead_hours=_to_int(
                    widget_data.get("db_lookahead_hours"), 24, f"widgets[{idx}].db_lookahead_hours", min_value=1, max_value=24
                ),
                db_only_trains=_to_bool(widget_data.get("db_only_trains"), False),
                db_use_fchg=_to_bool(widget_data.get("db_use_fchg"), True),
                gtfs_lookahead_hours=_to_int(
                    widget_data.get("gtfs_lookahead_hours"),
                    24,
                    f"widgets[{idx}].gtfs_lookahead_hours",
                    min_value=1,
                    max_value=48,
                ),
                max_departures=_to_int(
                    widget_data.get("max_departures"), 8, f"widgets[{idx}].max_departures", min_value=1
                ),
                show_delay=_to_bool(widget_data.get("show_delay"), True),
                show_feed_age=_to_bool(widget_data.get("show_feed_age"), True),
            )
        )

    if not widgets:
        raise ValueError("widgets is empty")

    widget_ids = [widget.id for widget in widgets]
    if len(set(widget_ids)) != len(widget_ids):
        raise ValueError("widget ids must be unique")
    for widget in widgets:
        if widget.source == "db_iris" and not widget.db_eva_no:
            raise ValueError(f"widget {widget.id}: db_eva_no must be set for source=db_iris")
    mapping = MappingConfig(
        trip_route_map_csv=_to_str(mapping_data.get("trip_route_map_csv"), "/data/trip_route_map.csv"),
        reload_every_seconds=_to_int(
            mapping_data.get("reload_every_seconds"), 300, "mapping.reload_every_seconds", min_value=1
        ),
    )

    return AppConfig(server=server, feed=feed, debug=debug, widgets=widgets, mapping=mapping)


def all_widget_stop_ids(config: AppConfig, source: Optional[str] = None) -> list[str]:
    merged: set[str] = set()
    for widget in config.widgets:
        if source and widget.source != source:
            continue
        merged.update(widget.stop_ids)
    return sorted(merged)


def find_widget(config: AppConfig, widget_id: str) -> Optional[WidgetConfig]:
    candidate = str(widget_id).strip()
    for widget in config.widgets:
        if widget.id == candidate:
            return widget
    return None


def load_trip_route_map(csv_path: str) -> tuple[dict[str, str], dict[str, str], Optional[str]]:
    started_at = time.monotonic()
    if not csv_path:
        debug_log("mapping_csv:skipped reason=empty_path")
        return {}, {}, None
    path = Path(csv_path)
    if not path.exists():
        debug_log(f"mapping_csv:missing path={csv_path}")
        return {}, {}, None

    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                debug_log(f"mapping_csv:invalid_header path={csv_path}")
                return {}, {}, f"mapping CSV has no header: {csv_path}"
            required = {"trip_id", "route_short_name"}
            if not required.issubset(set(reader.fieldnames)):
                debug_log(f"mapping_csv:missing_columns path={csv_path} columns={reader.fieldnames}")
                return {}, {}, f"mapping CSV missing columns {sorted(required)}: {csv_path}"

            mapping: dict[str, str] = {}
            destinations: dict[str, str] = {}
            for row in reader:
                trip_id = str(row.get("trip_id", "")).strip()
                route_short_name = str(row.get("route_short_name", "")).strip()
                destination = str(row.get("direction", "")).strip() or str(row.get("destination", "")).strip()
                if trip_id and route_short_name:
                    mapping[trip_id] = route_short_name
                if trip_id and destination:
                    destinations[trip_id] = destination
            debug_log(
                "mapping_csv:loaded "
                f"path={csv_path} routes={len(mapping)} destinations={len(destinations)} "
                f"duration_s={time.monotonic() - started_at:.2f}"
            )
            return mapping, destinations, None
    except Exception as exc:
        debug_log(
            f"mapping_csv:load_failed path={csv_path} error={exc} duration_s={time.monotonic() - started_at:.2f}"
        )
        return {}, {}, f"mapping load failed: {exc}"
def persist_trip_maps_to_csv(csv_path: str, route_map: dict[str, str], trip_destination_map: dict[str, str]) -> None:
    if not csv_path or not route_map:
        return
    path = Path(csv_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["trip_id", "route_short_name", "direction"])
        writer.writeheader()
        for trip_id in sorted(route_map):
            writer.writerow(
                {
                    "trip_id": trip_id,
                    "route_short_name": route_map.get(trip_id, ""),
                    "direction": trip_destination_map.get(trip_id, ""),
                }
            )


def load_trip_route_map_from_static_gtfs(
    stop_ids: list[str], timeout_seconds: int
) -> tuple[dict[str, str], dict[str, str], Optional[str]]:
    started_at = time.monotonic()
    target_stop_ids = {stop_id.strip() for stop_id in stop_ids if stop_id.strip()}
    if not target_stop_ids:
        return {}, {}, None
    debug_log(
        f"mapping_fallback:start stop_ids={len(target_stop_ids)} timeout_s={timeout_seconds} url={GTFS_STATIC_URL}"
    )

    download_started = time.monotonic()
    app_log(
        f"external_fetch:start source=gtfs_static purpose=mapping_fallback url={GTFS_STATIC_URL} timeout_s={timeout_seconds}"
    )
    try:
        response = httpx.get(GTFS_STATIC_URL, timeout=timeout_seconds, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except Exception as exc:
        app_log(f"external_fetch:error source=gtfs_static purpose=mapping_fallback error={exc}")
        debug_log(f"mapping_fallback:download_failed error={exc}")
        return {}, {}, f"mapping fallback download failed: {exc}"
    download_elapsed = time.monotonic() - download_started
    app_log(
        "external_fetch:ok "
        f"source=gtfs_static purpose=mapping_fallback status={response.status_code} bytes={len(response.content)} duration_s={download_elapsed:.2f}"
    )
    debug_log(
        f"mapping_fallback:download_ok bytes={len(response.content)} duration_s={download_elapsed:.2f}"
    )

    try:
        parse_started = time.monotonic()
        with zipfile.ZipFile(io.BytesIO(response.content), "r") as archive:
            stop_times_first_pass_started = time.monotonic()
            trip_ids: set[str] = set()
            with archive.open("stop_times.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    stop_id = str(row.get("stop_id", "")).strip()
                    if stop_id in target_stop_ids:
                        trip_id = str(row.get("trip_id", "")).strip()
                        if trip_id:
                            trip_ids.add(trip_id)
            stop_times_first_pass_elapsed = time.monotonic() - stop_times_first_pass_started
            debug_log(
                "mapping_fallback:stop_times_first_pass "
                f"trips={len(trip_ids)} duration_s={stop_times_first_pass_elapsed:.2f}"
            )

            if not trip_ids:
                debug_log("mapping_fallback:no_trips_for_configured_stop_ids")
                return {}, {}, "mapping fallback: keine Trips fÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¼r konfigurierte stop_ids gefunden"

            trips_started = time.monotonic()
            trip_to_route_id: dict[str, str] = {}
            trip_to_headsign: dict[str, str] = {}
            with archive.open("trips.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    trip_id = str(row.get("trip_id", "")).strip()
                    if trip_id in trip_ids:
                        route_id = str(row.get("route_id", "")).strip()
                        trip_headsign = str(row.get("trip_headsign", "")).strip()
                        if route_id:
                            trip_to_route_id[trip_id] = route_id
                        if trip_headsign:
                            trip_to_headsign[trip_id] = trip_headsign
            trips_elapsed = time.monotonic() - trips_started
            debug_log(
                "mapping_fallback:trips_loaded "
                f"routes={len(trip_to_route_id)} with_headsign={len(trip_to_headsign)} duration_s={trips_elapsed:.2f}"
            )

            routes_started = time.monotonic()
            route_id_to_short_name: dict[str, str] = {}
            with archive.open("routes.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    route_id = str(row.get("route_id", "")).strip()
                    route_short_name = str(row.get("route_short_name", "")).strip()
                    if route_id and route_short_name:
                        route_id_to_short_name[route_id] = route_short_name
            routes_elapsed = time.monotonic() - routes_started
            debug_log(
                f"mapping_fallback:routes_loaded routes={len(route_id_to_short_name)} duration_s={routes_elapsed:.2f}"
            )

            route_map: dict[str, str] = {}
            for trip_id, route_id in trip_to_route_id.items():
                route_short_name = route_id_to_short_name.get(route_id, "").strip()
                if route_short_name:
                    route_map[trip_id] = route_short_name

            missing_destination_trip_ids = {trip_id for trip_id in trip_ids if trip_id not in trip_to_headsign}
            trip_last_stop: dict[str, tuple[int, str]] = {}
            stop_id_to_name: dict[str, str] = {}

            if missing_destination_trip_ids:
                stop_times_second_pass_started = time.monotonic()
                with archive.open("stop_times.txt", "r") as handle:
                    reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                    for row in reader:
                        trip_id = str(row.get("trip_id", "")).strip()
                        if trip_id not in missing_destination_trip_ids:
                            continue
                        stop_id = str(row.get("stop_id", "")).strip()
                        if not stop_id:
                            continue
                        try:
                            seq = int(str(row.get("stop_sequence", "")).strip())
                        except (TypeError, ValueError):
                            continue
                        previous = trip_last_stop.get(trip_id)
                        if previous is None or seq > previous[0]:
                            trip_last_stop[trip_id] = (seq, stop_id)
                stop_times_second_pass_elapsed = time.monotonic() - stop_times_second_pass_started

                needed_stop_ids = {stop_data[1] for stop_data in trip_last_stop.values()}
                stops_started = time.monotonic()
                with archive.open("stops.txt", "r") as handle:
                    reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                    for row in reader:
                        stop_id = str(row.get("stop_id", "")).strip()
                        if stop_id not in needed_stop_ids:
                            continue
                        stop_name = str(row.get("stop_name", "")).strip()
                        if stop_name:
                            stop_id_to_name[stop_id] = stop_name
                stops_elapsed = time.monotonic() - stops_started

                debug_log(
                    "mapping_fallback:destination_fallback_loaded "
                    f"missing_trips={len(missing_destination_trip_ids)} last_stops={len(trip_last_stop)} "
                    f"named_stops={len(stop_id_to_name)} stop_times_s={stop_times_second_pass_elapsed:.2f} "
                    f"stops_s={stops_elapsed:.2f}"
                )
            else:
                debug_log("mapping_fallback:destination_fallback_skipped reason=headsign_complete")

            trip_destination_map: dict[str, str] = {}
            for trip_id in trip_ids:
                destination = trip_to_headsign.get(trip_id, "").strip()
                if not destination:
                    last = trip_last_stop.get(trip_id)
                    if last:
                        destination = stop_id_to_name.get(last[1], "").strip()
                if destination:
                    trip_destination_map[trip_id] = destination

            if not route_map:
                debug_log(
                    "mapping_fallback:no_route_short_name_mapping "
                    f"trips={len(trip_ids)} destinations={len(trip_destination_map)} "
                    f"parse_s={time.monotonic() - parse_started:.2f}"
                )
                return {}, trip_destination_map, "mapping fallback: keine route_short_name-Zuordnung erstellt"

            total_elapsed = time.monotonic() - started_at
            debug_log(
                "mapping_fallback:ok "
                f"trips={len(trip_ids)} routes={len(route_map)} destinations={len(trip_destination_map)} "
                f"parse_s={time.monotonic() - parse_started:.2f} total_s={total_elapsed:.2f}"
            )
            return route_map, trip_destination_map, None
    except Exception as exc:
        debug_log(f"mapping_fallback:parse_failed error={exc}")
        return {}, {}, f"mapping fallback parse failed: {exc}"
def _extract_time_epoch(stop_update) -> Optional[int]:
    dep_time = None
    arr_time = None
    if stop_update.HasField("departure") and stop_update.departure.HasField("time"):
        dep_time = int(stop_update.departure.time)
    if stop_update.HasField("arrival") and stop_update.arrival.HasField("time"):
        arr_time = int(stop_update.arrival.time)
    if dep_time is not None:
        return dep_time
    return arr_time


def _extract_delay_s(stop_update) -> Optional[int]:
    if stop_update.HasField("departure") and stop_update.departure.HasField("delay"):
        return int(stop_update.departure.delay)
    if stop_update.HasField("arrival") and stop_update.arrival.HasField("delay"):
        return int(stop_update.arrival.delay)
    return None


def _matches_widget_text_filters(
    widget: WidgetConfig, direction: str, path_stops: Optional[list[str]] = None
) -> bool:
    haystack_parts: list[str] = [direction]
    if path_stops:
        haystack_parts.extend(path_stops)
    haystack = " | ".join(part for part in haystack_parts if part).lower()

    if widget.direction_contains:
        direction_terms = [term.lower() for term in widget.direction_contains if term.strip()]
        if direction_terms and not any(term in haystack for term in direction_terms):
            return False

    if widget.required_stops:
        required_terms = [term.lower() for term in widget.required_stops if term.strip()]
        if required_terms and not all(term in haystack for term in required_terms):
            return False

    return True


DBIrisEventChange = ProviderDBIrisEventChange


def parse_db_iris_timestamp(raw_value: str) -> Optional[int]:
    return provider_parse_db_iris_timestamp(raw_value, local_timezone=LOCAL_TIMEZONE)


def parse_db_iris_fchg_changes(payload: bytes) -> dict[str, DBIrisEventChange]:
    return provider_parse_db_iris_fchg_changes(payload)


def parse_db_iris_plan_departures(
    widget: WidgetConfig,
    payload: bytes,
    now_epoch: int,
    changes_by_event_id: Optional[dict[str, DBIrisEventChange]] = None,
) -> list[Departure]:
    return provider_parse_db_iris_plan_departures(
        widget,
        payload,
        now_epoch,
        local_timezone=LOCAL_TIMEZONE,
        to_local_datetime_fn=to_local_datetime,
        match_widget_text_filters_fn=_matches_widget_text_filters,
        departure_factory=Departure,
        changes_by_event_id=changes_by_event_id,
    )


async def fetch_db_iris_departures(widget: WidgetConfig, timeout_seconds: int, now_epoch: int) -> list[Departure]:
    return await fetch_db_timetables_departures(
        widget,
        timeout_seconds,
        now_epoch,
        resolve_db_credentials_fn=resolve_db_credentials,
        base_url=DB_TIMETABLES_BASE_URL,
        user_agent=USER_AGENT,
        local_timezone=LOCAL_TIMEZONE,
        app_log_fn=app_log,
        debug_log_fn=debug_log,
        to_local_datetime_fn=to_local_datetime,
        match_widget_text_filters_fn=_matches_widget_text_filters,
        departure_factory=Departure,
    )


def extract_departures(
    feed_message: gtfs_realtime_pb2.FeedMessage,
    widget: WidgetConfig,
    route_map: dict[str, str],
    trip_destination_map: dict[str, str],
    now_epoch: int,
) -> list[Departure]:
    stop_ids = set(widget.stop_ids)
    route_filter = set(widget.route_short_names or [])
    max_time_epoch = now_epoch + max(1, widget.gtfs_lookahead_hours) * 3600
    departures: list[Departure] = []

    for entity in feed_message.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip_id = str(trip_update.trip.trip_id or "").strip()
        route = route_map.get(trip_id, "")
        direction = trip_destination_map.get(trip_id, "")
        if not _matches_widget_text_filters(widget, direction):
            continue

        if route_filter:
            if not route:
                continue
            if route not in route_filter:
                continue

        for stop_update in trip_update.stop_time_update:
            stop_id = str(stop_update.stop_id or "").strip()
            if stop_id not in stop_ids:
                continue

            time_epoch = _extract_time_epoch(stop_update)
            if time_epoch is None:
                continue
            if time_epoch < now_epoch:
                continue
            if time_epoch > max_time_epoch:
                continue

            delay_s = _extract_delay_s(stop_update) if widget.show_delay else None
            in_min = int((time_epoch - now_epoch) // 60)
            departures.append(
                Departure(
                    route=route,
                    direction=direction,
                    platform=None,
                    stop_id=stop_id,
                    time_epoch=time_epoch,
                    time_local=to_local_datetime(time_epoch).strftime("%H:%M"),
                    in_min=max(0, in_min),
                    delay_s=delay_s,
                    trip_id=trip_id,
                )
            )

    departures.sort(key=lambda item: item.time_epoch)
    return departures[: widget.max_departures]



def load_static_gtfs_archive_bytes(timeout_seconds: int) -> tuple[Optional[bytes], Optional[str]]:
    return provider_load_static_gtfs_archive_bytes(
        timeout_seconds,
        static_url=GTFS_STATIC_URL,
        cache_path=GTFS_STATIC_CACHE_PATH,
        cache_max_age_seconds=GTFS_STATIC_CACHE_MAX_AGE_SECONDS,
        user_agent=USER_AGENT,
        app_log_fn=app_log,
        debug_log_fn=debug_log,
    )


def load_known_stop_ids_from_static_gtfs(timeout_seconds: int) -> tuple[set[str], Optional[str]]:
    started_at = time.monotonic()
    payload, load_error = load_static_gtfs_archive_bytes(timeout_seconds)
    if payload is None:
        return set(), load_error or "stops index payload unavailable"

    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            stop_ids: set[str] = set()
            with archive.open("stops.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    stop_id = str(row.get("stop_id", "")).strip()
                    if stop_id:
                        stop_ids.add(stop_id)
        debug_log(
            "mapping_static:stops_indexed "
            f"count={len(stop_ids)} duration_s={time.monotonic() - started_at:.2f}"
        )
        return stop_ids, None
    except Exception as exc:
        debug_log(f"mapping_static:stops_index_failed error={exc}")
        return set(), f"stops index parse failed: {exc}"


def _parse_gtfs_hms_to_seconds(raw_value: str) -> Optional[int]:
    text = str(raw_value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
    except (TypeError, ValueError):
        return None
    if hours < 0 or minutes < 0 or minutes > 59 or seconds < 0 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds



def _static_archive_cache_token() -> str:
    cache_path = Path(GTFS_STATIC_CACHE_PATH)
    if not cache_path.is_file():
        return ""
    try:
        stat = cache_path.stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _load_static_fallback_index_cache(cache_token: str, stop_ids: set[str]) -> Optional[StaticFallbackIndex]:
    if not cache_token:
        return None

    target_path = Path(STATIC_FALLBACK_INDEX_CACHE_PATH)
    if not target_path.is_file():
        return None

    expected_stop_ids = tuple(sorted(stop_ids))
    try:
        with target_path.open("rb") as handle:
            payload = pickle.load(handle)
    except Exception as exc:
        debug_log(f"fallback_static:cache_read_failed path={target_path} error={exc}")
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("version") != STATIC_FALLBACK_INDEX_CACHE_VERSION:
        return None
    if payload.get("cache_token") != cache_token:
        return None
    if tuple(payload.get("stop_ids") or ()) != expected_stop_ids:
        return None

    index = payload.get("index")
    if not isinstance(index, StaticFallbackIndex):
        return None

    app_log(
        "perf:fallback_index_cache_hit "
        f"path={target_path} stop_ids={len(expected_stop_ids)}"
    )
    return index


def _save_static_fallback_index_cache(cache_token: str, stop_ids: set[str], index: StaticFallbackIndex) -> None:
    if not cache_token:
        return

    target_path = Path(STATIC_FALLBACK_INDEX_CACHE_PATH)
    payload = {
        "version": STATIC_FALLBACK_INDEX_CACHE_VERSION,
        "cache_token": cache_token,
        "stop_ids": tuple(sorted(stop_ids)),
        "index": index,
    }
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as handle:
            pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        debug_log(f"fallback_static:cache_write_failed path={target_path} error={exc}")

def load_static_fallback_index_for_stop_ids(
    stop_ids: list[str], timeout_seconds: int
) -> tuple[Optional[StaticFallbackIndex], Optional[str]]:
    started_at = time.monotonic()
    target_stop_ids = {stop_id.strip() for stop_id in stop_ids if stop_id.strip()}
    cpu_started_at = time.process_time()
    app_log(
        "perf:fallback_index_build_start "
        f"stop_ids={len(target_stop_ids)} timeout_s={timeout_seconds}"
    )
    empty_index = StaticFallbackIndex(
        stop_entries={},
        trip_route={},
        trip_direction={},
        event_direction={},
        target_stop_name={},
        trip_service={},
        service_weekdays={},
        service_date_range={},
        service_exceptions={},
    )
    if not target_stop_ids:
        return empty_index, None

    payload, load_error = load_static_gtfs_archive_bytes(timeout_seconds)
    if payload is None:
        return None, load_error or "static fallback payload unavailable"

    cache_token = _static_archive_cache_token() or f"memory:{len(payload)}"
    cached_index = _load_static_fallback_index_cache(cache_token, target_stop_ids)
    if cached_index is not None:
        app_log(
            "perf:fallback_index_build_done "
            f"stop_ids={len(target_stop_ids)} entries={sum(len(items) for items in cached_index.stop_entries.values())} "
            f"wall_s={time.monotonic() - started_at:.2f} cpu_s={time.process_time() - cpu_started_at:.2f} cache_hit=1"
        )
        return cached_index, None

    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            stop_entries: dict[str, list[tuple[str, int]]] = {stop_id: [] for stop_id in target_stop_ids}
            relevant_trip_ids: set[str] = set()
            relevant_target_events: list[tuple[str, str, int, Optional[int]]] = []
            trip_stop_sequences: dict[str, list[tuple[int, str]]] = {}
            event_sequence_lookup: dict[tuple[str, str, int], int] = {}

            stop_times_started = time.monotonic()
            with archive.open("stop_times.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    trip_id = str(row.get("trip_id", "")).strip()
                    if not trip_id:
                        continue

                    stop_id = str(row.get("stop_id", "")).strip()
                    sequence_raw = str(row.get("stop_sequence", "")).strip()
                    sequence: Optional[int] = None
                    if sequence_raw:
                        try:
                            sequence = int(sequence_raw)
                        except ValueError:
                            sequence = None

                    if stop_id in target_stop_ids:
                        departure_seconds = _parse_gtfs_hms_to_seconds(
                            str(row.get("departure_time", "")).strip() or str(row.get("arrival_time", "")).strip()
                        )
                        if departure_seconds is not None:
                            stop_entries.setdefault(stop_id, []).append((trip_id, departure_seconds))
                            relevant_trip_ids.add(trip_id)
                            relevant_target_events.append((trip_id, stop_id, departure_seconds, sequence))

                    # Collect stop sequence rows once trip became relevant.
                    if trip_id in relevant_trip_ids and stop_id and sequence is not None:
                        trip_stop_sequences.setdefault(trip_id, []).append((sequence, stop_id))
                        sequence_departure_seconds = _parse_gtfs_hms_to_seconds(
                            str(row.get("departure_time", "")).strip() or str(row.get("arrival_time", "")).strip()
                        )
                        if sequence_departure_seconds is not None:
                            event_sequence_lookup[(trip_id, stop_id, sequence_departure_seconds)] = sequence
            stop_times_elapsed = time.monotonic() - stop_times_started

            if not relevant_trip_ids:
                debug_log(
                    "fallback_static:no_relevant_trips "
                    f"stop_ids={len(target_stop_ids)} stop_times_s={stop_times_elapsed:.2f}"
                )
                return empty_index, None

            trip_route_id: dict[str, str] = {}
            trip_direction: dict[str, str] = {}
            trip_service: dict[str, str] = {}
            needed_route_ids: set[str] = set()

            trips_started = time.monotonic()
            with archive.open("trips.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    trip_id = str(row.get("trip_id", "")).strip()
                    if trip_id not in relevant_trip_ids:
                        continue
                    route_id = str(row.get("route_id", "")).strip()
                    service_id = str(row.get("service_id", "")).strip()
                    headsign = str(row.get("trip_headsign", "")).strip()
                    if route_id:
                        trip_route_id[trip_id] = route_id
                        needed_route_ids.add(route_id)
                    if service_id:
                        trip_service[trip_id] = service_id
                    if headsign:
                        trip_direction[trip_id] = headsign
            trips_elapsed = time.monotonic() - trips_started

            for trip_id in trip_stop_sequences:
                trip_stop_sequences[trip_id].sort(key=lambda item: item[0])

            trip_max_sequence: dict[str, int] = {
                trip_id: sequence_rows[-1][0] for trip_id, sequence_rows in trip_stop_sequences.items() if sequence_rows
            }
            filtered_terminal_departures = 0
            for stop_id in list(stop_entries.keys()):
                filtered_entries: list[tuple[str, int]] = []
                for trip_id, departure_seconds in stop_entries.get(stop_id, []):
                    event_sequence = event_sequence_lookup.get((trip_id, stop_id, departure_seconds))
                    max_sequence = trip_max_sequence.get(trip_id)
                    if event_sequence is not None and max_sequence is not None and event_sequence >= max_sequence:
                        filtered_terminal_departures += 1
                        continue
                    filtered_entries.append((trip_id, departure_seconds))
                stop_entries[stop_id] = filtered_entries

            needed_stop_ids: set[str] = set(target_stop_ids)
            for sequence_rows in trip_stop_sequences.values():
                for _, stop_id in sequence_rows:
                    needed_stop_ids.add(stop_id)

            stop_id_to_name: dict[str, str] = {}
            stops_lookup_elapsed = 0.0
            if needed_stop_ids:
                stops_lookup_started = time.monotonic()
                try:
                    with archive.open("stops.txt", "r") as handle:
                        reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                        for row in reader:
                            stop_id = str(row.get("stop_id", "")).strip()
                            if stop_id not in needed_stop_ids:
                                continue
                            stop_name = str(row.get("stop_name", "")).strip()
                            if stop_name:
                                stop_id_to_name[stop_id] = stop_name
                except KeyError:
                    pass
                stops_lookup_elapsed = time.monotonic() - stops_lookup_started

            derived_terminal_directions = 0
            for trip_id in relevant_trip_ids:
                if trip_direction.get(trip_id):
                    continue
                sequence_rows = trip_stop_sequences.get(trip_id, [])
                if not sequence_rows:
                    continue
                direction_name = ""
                for _, stop_id in reversed(sequence_rows):
                    stop_name = stop_id_to_name.get(stop_id, "").strip()
                    if stop_name:
                        direction_name = stop_name
                        break
                if direction_name:
                    trip_direction[trip_id] = direction_name
                    derived_terminal_directions += 1

            event_direction: dict[tuple[str, int, str], str] = {}
            derived_event_directions = 0
            for trip_id, stop_id, departure_seconds, stop_sequence in relevant_target_events:
                sequence_rows = trip_stop_sequences.get(trip_id, [])
                if not sequence_rows:
                    continue

                resolved_sequence = stop_sequence
                if resolved_sequence is None:
                    resolved_sequence = event_sequence_lookup.get((trip_id, stop_id, departure_seconds))
                if resolved_sequence is None:
                    continue

                current_name = stop_id_to_name.get(stop_id, "").strip().lower()
                direction_name = ""

                # Prefer the last future stop name different from the current stop name.
                for sequence_value, sequence_stop_id in sequence_rows:
                    if sequence_value <= resolved_sequence:
                        continue
                    stop_name = stop_id_to_name.get(sequence_stop_id, "").strip()
                    if not stop_name:
                        continue
                    if current_name and stop_name.lower() == current_name:
                        continue
                    direction_name = stop_name

                if not direction_name:
                    for sequence_value, sequence_stop_id in sequence_rows:
                        if sequence_value <= resolved_sequence:
                            continue
                        stop_name = stop_id_to_name.get(sequence_stop_id, "").strip()
                        if stop_name:
                            direction_name = stop_name

                if direction_name:
                    event_direction[(trip_id, departure_seconds, stop_id)] = direction_name
                    derived_event_directions += 1

            route_id_to_short: dict[str, str] = {}
            routes_started = time.monotonic()
            with archive.open("routes.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    route_id = str(row.get("route_id", "")).strip()
                    if route_id not in needed_route_ids:
                        continue
                    short_name = str(row.get("route_short_name", "")).strip()
                    if short_name:
                        route_id_to_short[route_id] = short_name
            routes_elapsed = time.monotonic() - routes_started

            trip_route: dict[str, str] = {}
            for trip_id, route_id in trip_route_id.items():
                short_name = route_id_to_short.get(route_id, "").strip()
                if short_name:
                    trip_route[trip_id] = short_name

            needed_service_ids = {service_id for service_id in trip_service.values() if service_id}
            service_weekdays: dict[str, tuple[int, int, int, int, int, int, int]] = {}
            service_date_range: dict[str, tuple[date, date]] = {}
            service_exceptions: dict[str, dict[date, int]] = {}

            calendar_started = time.monotonic()
            try:
                with archive.open("calendar.txt", "r") as handle:
                    reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                    for row in reader:
                        service_id = str(row.get("service_id", "")).strip()
                        if service_id not in needed_service_ids:
                            continue
                        start_raw = str(row.get("start_date", "")).strip()
                        end_raw = str(row.get("end_date", "")).strip()
                        try:
                            start_date = datetime.strptime(start_raw, "%Y%m%d").date()
                            end_date = datetime.strptime(end_raw, "%Y%m%d").date()
                        except ValueError:
                            continue
                        weekday_flags = (
                            int(str(row.get("monday", "0")).strip() or "0"),
                            int(str(row.get("tuesday", "0")).strip() or "0"),
                            int(str(row.get("wednesday", "0")).strip() or "0"),
                            int(str(row.get("thursday", "0")).strip() or "0"),
                            int(str(row.get("friday", "0")).strip() or "0"),
                            int(str(row.get("saturday", "0")).strip() or "0"),
                            int(str(row.get("sunday", "0")).strip() or "0"),
                        )
                        service_weekdays[service_id] = weekday_flags
                        service_date_range[service_id] = (start_date, end_date)
            except KeyError:
                pass
            calendar_elapsed = time.monotonic() - calendar_started

            calendar_dates_started = time.monotonic()
            try:
                with archive.open("calendar_dates.txt", "r") as handle:
                    reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                    for row in reader:
                        service_id = str(row.get("service_id", "")).strip()
                        if service_id not in needed_service_ids:
                            continue
                        date_raw = str(row.get("date", "")).strip()
                        exception_raw = str(row.get("exception_type", "")).strip()
                        try:
                            service_date = datetime.strptime(date_raw, "%Y%m%d").date()
                            exception_type = int(exception_raw)
                        except (TypeError, ValueError):
                            continue
                        service_exceptions.setdefault(service_id, {})[service_date] = exception_type
            except KeyError:
                pass
            calendar_dates_elapsed = time.monotonic() - calendar_dates_started

            for stop_id in stop_entries:
                stop_entries[stop_id].sort(key=lambda item: item[1])

            target_stop_name = {stop_id: stop_id_to_name.get(stop_id, "").strip() for stop_id in target_stop_ids}

            index = StaticFallbackIndex(
                stop_entries=stop_entries,
                trip_route=trip_route,
                trip_direction=trip_direction,
                event_direction=event_direction,
                target_stop_name=target_stop_name,
                trip_service=trip_service,
                service_weekdays=service_weekdays,
                service_date_range=service_date_range,
                service_exceptions=service_exceptions,
            )

            total_entries = sum(len(items) for items in stop_entries.values())
            debug_log(
                "fallback_static:index_ready "
                f"stop_ids={len(target_stop_ids)} entries={total_entries} trips={len(relevant_trip_ids)} "
                f"with_routes={len(trip_route)} with_service={len(trip_service)} "
                f"with_direction={len(trip_direction)} terminal_direction={derived_terminal_directions} "
                f"event_direction={len(event_direction)} derived_event_direction={derived_event_directions} filtered_terminal={filtered_terminal_departures} "
                f"stop_times_s={stop_times_elapsed:.2f} trips_s={trips_elapsed:.2f} routes_s={routes_elapsed:.2f} "
                f"stops_lookup_s={stops_lookup_elapsed:.2f} calendar_s={calendar_elapsed:.2f} "
                f"calendar_dates_s={calendar_dates_elapsed:.2f} total_s={time.monotonic() - started_at:.2f}"
            )
            _save_static_fallback_index_cache(cache_token, target_stop_ids, index)
            app_log(
                "perf:fallback_index_build_done "
                f"stop_ids={len(target_stop_ids)} entries={total_entries} wall_s={time.monotonic() - started_at:.2f} "
                f"cpu_s={time.process_time() - cpu_started_at:.2f} cache_hit=0"
            )
            return index, None
    except Exception as exc:
        debug_log(f"fallback_static:index_failed error={exc}")
        return None, f"static fallback parse failed: {exc}"



def _service_runs_on_date(index: StaticFallbackIndex, service_id: str, service_date: date) -> bool:
    exceptions = index.service_exceptions.get(service_id)
    if exceptions and service_date in exceptions:
        return exceptions[service_date] == 1

    weekday_flags = index.service_weekdays.get(service_id)
    date_range = index.service_date_range.get(service_id)
    if weekday_flags is None or date_range is None:
        return False

    start_date, end_date = date_range
    if service_date < start_date or service_date > end_date:
        return False

    weekday = service_date.weekday()
    if weekday < 0 or weekday > 6:
        return False
    return bool(weekday_flags[weekday])


def extract_static_schedule_departures(
    widget: WidgetConfig,
    index: StaticFallbackIndex,
    now_epoch: int,
) -> list[Departure]:
    stop_ids = [stop_id for stop_id in widget.stop_ids if stop_id]
    if not stop_ids:
        return []

    route_filter = set(widget.route_short_names or [])
    max_time_epoch = now_epoch + max(1, widget.gtfs_lookahead_hours) * 3600
    now_local = to_local_datetime(now_epoch)
    max_local = to_local_datetime(max_time_epoch)
    start_service_date = (now_local - timedelta(days=1)).date()
    end_service_date = max_local.date()

    departures: list[Departure] = []
    seen_keys: set[tuple[str, int, str]] = set()
    service_dates_cache: dict[str, list[date]] = {}

    def service_dates_for(service_id: str) -> list[date]:
        if service_id in service_dates_cache:
            return service_dates_cache[service_id]
        dates: list[date] = []
        cursor = start_service_date
        while cursor <= end_service_date:
            if _service_runs_on_date(index, service_id, cursor):
                dates.append(cursor)
            cursor += timedelta(days=1)
        service_dates_cache[service_id] = dates
        return dates

    for stop_id in stop_ids:
        for trip_id, departure_seconds in index.stop_entries.get(stop_id, []):
            route = index.trip_route.get(trip_id, "")
            direction = index.event_direction.get((trip_id, departure_seconds, stop_id), index.trip_direction.get(trip_id, ""))
            source_stop_name = index.target_stop_name.get(stop_id, "").strip()
            if source_stop_name and direction and direction.strip().lower() == source_stop_name.lower():
                direction = ""

            if route_filter:
                if not route:
                    continue
                if route not in route_filter:
                    continue

            if not _matches_widget_text_filters(widget, direction):
                continue

            service_id = index.trip_service.get(trip_id, "")
            if not service_id:
                continue

            for service_day in service_dates_for(service_id):
                departure_local = datetime.combine(service_day, datetime.min.time(), tzinfo=LOCAL_TIMEZONE) + timedelta(
                    seconds=departure_seconds
                )
                departure_epoch = int(departure_local.timestamp())
                if departure_epoch < now_epoch or departure_epoch > max_time_epoch:
                    continue

                dedup_key = (trip_id, departure_epoch, stop_id)
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                in_min = max(0, int((departure_epoch - now_epoch) // 60))
                departures.append(
                    Departure(
                        route=route,
                        direction=direction,
                        platform=None,
                        stop_id=stop_id,
                        time_epoch=departure_epoch,
                        time_local=departure_local.strftime("%H:%M"),
                        in_min=in_min,
                        delay_s=None,
                        trip_id=trip_id,
                    )
                )

    departures.sort(key=lambda item: item.time_epoch)
    return departures


def merge_departures_realtime_with_fallback(
    realtime_departures: list[Departure],
    fallback_departures: list[Departure],
    max_departures: int,
) -> list[Departure]:
    def canonical_trip_id(value: str) -> str:
        trip_id = str(value or "").strip().casefold()
        if not trip_id:
            return ""
        # tolerate common suffix variants from different sources (e.g. date/realtime suffixes)
        trip_id = re.sub(r"([_#-])(rt|realtime)$", "", trip_id)
        trip_id = re.sub(r"([_#-])\d{8,}$", "", trip_id)
        return trip_id

    def route_direction_stop_key(dep: Departure) -> tuple[str, str, str]:
        route = re.sub(r"\s+", " ", str(dep.route or "").strip()).casefold()
        direction = re.sub(r"\s+", " ", str(dep.direction or "").strip()).casefold()
        stop_id = str(dep.stop_id or "").strip()
        return route, direction, stop_id

    merged: list[Departure] = []
    seen_exact: set[tuple[str, int, str, str]] = set()
    realtime_trip_stop_keys: set[tuple[str, str]] = set()
    realtime_planned_by_rds: dict[tuple[str, str, str], list[int]] = {}
    realtime_actual_by_rds: dict[tuple[str, str, str], list[int]] = {}

    for dep in sorted(realtime_departures, key=lambda item: item.time_epoch):
        exact_key = (dep.trip_id, dep.time_epoch, dep.stop_id, dep.route)
        if exact_key in seen_exact:
            continue
        seen_exact.add(exact_key)
        merged.append(dep)

        stop_id = str(dep.stop_id or "").strip()
        if stop_id:
            trip_id = str(dep.trip_id or "").strip()
            if trip_id:
                realtime_trip_stop_keys.add((trip_id, stop_id))
                canonical = canonical_trip_id(trip_id)
                if canonical and canonical != trip_id.casefold():
                    realtime_trip_stop_keys.add((canonical, stop_id))

        rds_key = route_direction_stop_key(dep)
        realtime_actual_by_rds.setdefault(rds_key, []).append(dep.time_epoch)
        if dep.delay_s is not None:
            planned_epoch = dep.time_epoch - dep.delay_s
            realtime_planned_by_rds.setdefault(rds_key, []).append(planned_epoch)

    for dep in sorted(fallback_departures, key=lambda item: item.time_epoch):
        stop_id = str(dep.stop_id or "").strip()
        trip_id = str(dep.trip_id or "").strip()
        if trip_id and stop_id:
            if (trip_id, stop_id) in realtime_trip_stop_keys:
                continue
            canonical = canonical_trip_id(trip_id)
            if canonical and (canonical, stop_id) in realtime_trip_stop_keys:
                continue

        rds_key = route_direction_stop_key(dep)
        # Prefer realtime if the same service is already present with delay-shifted time.
        planned_candidates = realtime_planned_by_rds.get(rds_key, [])
        if planned_candidates and any(abs(dep.time_epoch - candidate) <= 120 for candidate in planned_candidates):
            continue
        # Fallback safety net for cases without delay info in realtime.
        actual_candidates = realtime_actual_by_rds.get(rds_key, [])
        if actual_candidates and any(abs(dep.time_epoch - candidate) <= 60 for candidate in actual_candidates):
            continue

        exact_key = (dep.trip_id, dep.time_epoch, dep.stop_id, dep.route)
        if exact_key in seen_exact:
            continue
        seen_exact.add(exact_key)
        merged.append(dep)
        if len(merged) >= max_departures:
            break

    merged.sort(key=lambda item: item.time_epoch)
    return merged[: max_departures]


def collect_realtime_trip_context(
    feed_message: gtfs_realtime_pb2.FeedMessage,
    configured_stop_ids: set[str],
) -> tuple[set[str], dict[str, str]]:
    if not configured_stop_ids:
        return set(), {}

    trip_ids: set[str] = set()
    trip_last_stop_ids: dict[str, str] = {}

    for entity in feed_message.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        trip_id = str(trip_update.trip.trip_id or "").strip()
        if not trip_id:
            continue

        has_configured_stop = False
        last_stop_id = ""
        for stop_update in trip_update.stop_time_update:
            stop_id = str(stop_update.stop_id or "").strip()
            if stop_id:
                last_stop_id = stop_id
            if stop_id in configured_stop_ids:
                has_configured_stop = True

        if has_configured_stop:
            trip_ids.add(trip_id)
            if last_stop_id:
                trip_last_stop_ids[trip_id] = last_stop_id

    return trip_ids, trip_last_stop_ids


def load_trip_maps_for_trip_ids_from_static_gtfs(
    trip_ids: set[str],
    trip_last_stop_ids: dict[str, str],
    timeout_seconds: int,
) -> tuple[dict[str, str], dict[str, str], Optional[str]]:
    started_at = time.monotonic()
    target_trip_ids = {trip_id.strip() for trip_id in trip_ids if trip_id.strip()}
    if not target_trip_ids:
        return {}, {}, None

    payload, load_error = load_static_gtfs_archive_bytes(timeout_seconds)
    if payload is None:
        return {}, {}, load_error or "mapping static payload unavailable"

    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            trips_started = time.monotonic()
            trip_to_route_id: dict[str, str] = {}
            trip_to_headsign: dict[str, str] = {}
            with archive.open("trips.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    trip_id = str(row.get("trip_id", "")).strip()
                    if trip_id not in target_trip_ids:
                        continue
                    route_id = str(row.get("route_id", "")).strip()
                    if route_id:
                        trip_to_route_id[trip_id] = route_id
                    trip_headsign = str(row.get("trip_headsign", "")).strip()
                    if trip_headsign:
                        trip_to_headsign[trip_id] = trip_headsign
            debug_log(
                "mapping_enrich:trips_loaded "
                f"requested={len(target_trip_ids)} matched={len(trip_to_route_id)} "
                f"with_headsign={len(trip_to_headsign)} duration_s={time.monotonic() - trips_started:.2f}"
            )

            routes_started = time.monotonic()
            route_id_to_short_name: dict[str, str] = {}
            with archive.open("routes.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    route_id = str(row.get("route_id", "")).strip()
                    short_name = str(row.get("route_short_name", "")).strip()
                    if route_id and short_name:
                        route_id_to_short_name[route_id] = short_name
            debug_log(
                f"mapping_enrich:routes_loaded routes={len(route_id_to_short_name)} duration_s={time.monotonic() - routes_started:.2f}"
            )

            route_map: dict[str, str] = {}
            for trip_id, route_id in trip_to_route_id.items():
                short_name = route_id_to_short_name.get(route_id, "").strip()
                if short_name:
                    route_map[trip_id] = short_name

            trip_destination_map: dict[str, str] = dict(trip_to_headsign)
            missing_destination_trip_ids = {
                trip_id for trip_id in target_trip_ids if trip_id not in trip_destination_map
            }
            if missing_destination_trip_ids:
                needed_stop_ids = {
                    trip_last_stop_ids[trip_id]
                    for trip_id in missing_destination_trip_ids
                    if trip_id in trip_last_stop_ids and trip_last_stop_ids[trip_id]
                }
                if needed_stop_ids:
                    stops_started = time.monotonic()
                    stop_id_to_name: dict[str, str] = {}
                    with archive.open("stops.txt", "r") as handle:
                        reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                        for row in reader:
                            stop_id = str(row.get("stop_id", "")).strip()
                            if stop_id not in needed_stop_ids:
                                continue
                            stop_name = str(row.get("stop_name", "")).strip()
                            if stop_name:
                                stop_id_to_name[stop_id] = stop_name
                    for trip_id in missing_destination_trip_ids:
                        stop_id = trip_last_stop_ids.get(trip_id, "")
                        if not stop_id:
                            continue
                        stop_name = stop_id_to_name.get(stop_id, "").strip()
                        if stop_name:
                            trip_destination_map[trip_id] = stop_name
                    debug_log(
                        "mapping_enrich:stops_loaded "
                        f"needed={len(needed_stop_ids)} named={len(stop_id_to_name)} "
                        f"duration_s={time.monotonic() - stops_started:.2f}"
                    )

            debug_log(
                "mapping_enrich:ok "
                f"requested={len(target_trip_ids)} routes={len(route_map)} destinations={len(trip_destination_map)} "
                f"duration_s={time.monotonic() - started_at:.2f}"
            )
            return route_map, trip_destination_map, None
    except Exception as exc:
        debug_log(f"mapping_enrich:failed error={exc}")
        return {}, {}, f"mapping enrich parse failed: {exc}"
async def fetch_feed_bytes(url: str, timeout_seconds: int) -> bytes:
    return await provider_fetch_feed_bytes(
        url,
        timeout_seconds,
        user_agent=USER_AGENT,
        app_log_fn=app_log,
    )


def age_seconds(fetched_at_epoch: Optional[int]) -> Optional[int]:
    if fetched_at_epoch is None:
        return None
    return max(0, int(time.time()) - fetched_at_epoch)


async def reload_mapping_if_due(state: RuntimeState) -> None:
    now_monotonic = time.monotonic()
    if now_monotonic < state.next_mapping_reload_monotonic:
        return

    started_at = time.monotonic()
    debug_log("mapping_reload:started")
    route_map, trip_destination_map, mapping_error = load_trip_route_map(state.config.mapping.trip_route_map_csv)
    reload_in_seconds = state.config.mapping.reload_every_seconds

    async with state.lock:
        had_existing_mapping = bool(state.route_map) or bool(state.trip_destination_map)
        if had_existing_mapping and not route_map and not trip_destination_map:
            # Keep in-memory mapping if file reload fails/returns empty to avoid repeated expensive re-enrichment.
            route_map = dict(state.route_map)
            trip_destination_map = dict(state.trip_destination_map)
            if mapping_error:
                mapping_error = f"{mapping_error} | Mapping-Reuse aus Arbeitsspeicher aktiv"
            else:
                mapping_error = "Mapping-Datei leer/nicht verfuegbar; verwende Mapping aus Arbeitsspeicher"
            app_log(
                "mapping_reload:memory_reuse "
                f"path={state.config.mapping.trip_route_map_csv} routes={len(route_map)} destinations={len(trip_destination_map)}"
            )

        state.route_map = route_map
        state.trip_destination_map = trip_destination_map
        state.mapping_error = mapping_error
        state.next_mapping_reload_monotonic = now_monotonic + reload_in_seconds

    debug_log(
        "mapping_reload:finished "
        f"routes={len(route_map)} destinations={len(trip_destination_map)} error={bool(mapping_error)} "
        f"next_in_s={reload_in_seconds} duration_s={time.monotonic() - started_at:.2f}"
    )


def _polling_deps() -> PollingDeps:
    return PollingDeps(
        debug_log=debug_log,
        app_log=app_log,
        refresh_direction_mapping_if_due=refresh_direction_mapping_if_due,
        reload_mapping_if_due=reload_mapping_if_due,
        fetch_feed_bytes=fetch_feed_bytes,
        all_widget_stop_ids=all_widget_stop_ids,
        refresh_known_stop_ids_if_due=refresh_known_stop_ids_if_due,
        collect_realtime_trip_context=collect_realtime_trip_context,
        load_trip_maps_for_trip_ids_from_static_gtfs=load_trip_maps_for_trip_ids_from_static_gtfs,
        persist_trip_maps_to_csv=persist_trip_maps_to_csv,
        refresh_static_fallback_index_if_due=refresh_static_fallback_index_if_due,
        extract_departures=extract_departures,
        extract_static_schedule_departures=extract_static_schedule_departures,
        merge_departures_realtime_with_fallback=merge_departures_realtime_with_fallback,
        apply_direction_labels=apply_direction_labels,
        register_observed_direction_entries=register_observed_direction_entries,
        fetch_db_iris_departures=fetch_db_iris_departures,
    )


async def poll_once(state: RuntimeState) -> None:
    await service_poll_once(state, _polling_deps())


async def ensure_data_fresh(state: RuntimeState, force: bool = False) -> None:
    await service_ensure_data_fresh(state, _polling_deps(), force=force)


def _build_24h_widget(widget: WidgetConfig) -> WidgetConfig:
    max_departures_24h = max(widget.max_departures, 4096)
    return replace(
        widget,
        gtfs_lookahead_hours=24,
        db_lookahead_hours=24,
        max_departures=max_departures_24h,
    )


async def get_widget_departures_for_view(
    state: RuntimeState,
    widget: WidgetConfig,
    view_mode: str = "default",
) -> tuple[list[Departure], Optional[int], list[str]]:
    normalized_mode = (view_mode or "").strip().lower()

    await ensure_data_fresh(state)
    await refresh_direction_mapping_if_due(state)

    async with state.lock:
        direction_labels = dict(state.direction_labels)
        direction_label_patterns = list(state.direction_label_patterns)

    if normalized_mode != "24h":
        async with state.lock:
            departures = list(state.departures_by_widget.get(widget.id, []))
            fetched_at_epoch = state.fetched_at_epoch
            errors = list(state.errors_by_widget.get(widget.id, []))
        observed = apply_direction_labels(departures, direction_labels, direction_label_patterns)
        await register_observed_direction_entries(state, observed)
        return departures, fetched_at_epoch, errors

    cache_key = f"{widget.id}:{normalized_mode}"
    now_monotonic = time.monotonic()
    cache_hit_payload: Optional[tuple[list[Departure], list[str], Optional[int]]] = None

    async with state.lock:
        cached_entry = state.extended_departures_cache.get(cache_key)
        if cached_entry and now_monotonic < cached_entry[0]:
            _expires_monotonic, cached_departures, cached_errors, cached_fetched_at = cached_entry
            cache_hit_payload = ([replace(dep) for dep in cached_departures], list(cached_errors), cached_fetched_at)

    if cache_hit_payload is not None:
        departures, cached_errors, cached_fetched_at = cache_hit_payload
        observed = apply_direction_labels(departures, direction_labels, direction_label_patterns)
        await register_observed_direction_entries(state, observed)
        debug_log(
            "view_cache:hit "
            f"widget={widget.id} mode={normalized_mode} departures={len(departures)}"
        )
        return departures, cached_fetched_at, cached_errors

    now_epoch = int(time.time())
    departures: list[Departure] = []
    errors: list[str] = []
    fetched_at_epoch: Optional[int] = None

    if widget.source == "gtfs_rt":
        await refresh_static_fallback_index_if_due(state)
        async with state.lock:
            realtime_departures = list(state.departures_by_widget.get(widget.id, []))
            errors = list(state.errors_by_widget.get(widget.id, []))
            static_fallback_index = state.static_fallback_index
            static_fallback_error = state.static_fallback_error
            fetched_at_epoch = state.fetched_at_epoch

        if static_fallback_index is not None:
            widget_24h = _build_24h_widget(widget)
            fallback_departures = extract_static_schedule_departures(widget_24h, static_fallback_index, now_epoch)
            merged_limit = max(widget_24h.max_departures, len(realtime_departures) + len(fallback_departures))
            departures = merge_departures_realtime_with_fallback(
                realtime_departures,
                fallback_departures,
                merged_limit,
            )
        else:
            departures = realtime_departures
            if static_fallback_error:
                errors.append(
                    "24h-Ansicht: Statischer Fahrplan-Fallback nicht verf?gbar: "
                    f"{static_fallback_error}"
                )

        max_time_epoch = now_epoch + 24 * 3600
        departures = [dep for dep in departures if now_epoch <= dep.time_epoch <= max_time_epoch]
        departures.sort(key=lambda item: item.time_epoch)
    else:
        widget_24h = _build_24h_widget(widget)
        try:
            departures = await fetch_db_iris_departures(widget_24h, state.config.feed.http_timeout_seconds, now_epoch)
            max_time_epoch = now_epoch + 24 * 3600
            departures = [dep for dep in departures if now_epoch <= dep.time_epoch <= max_time_epoch]
            departures.sort(key=lambda item: item.time_epoch)
            fetched_at_epoch = int(time.time())
            async with state.lock:
                errors = list(state.errors_by_widget.get(widget.id, []))
        except Exception as exc:
            async with state.lock:
                departures = list(state.departures_by_widget.get(widget.id, []))
                fetched_at_epoch = state.fetched_at_epoch
                errors = list(state.errors_by_widget.get(widget.id, []))
            errors.append(f"24h-Abfrage fehlgeschlagen: {exc}")

    observed = apply_direction_labels(departures, direction_labels, direction_label_patterns)
    await register_observed_direction_entries(state, observed)

    cache_ttl_s = max(10, state.config.feed.refresh_seconds)
    expires_monotonic = time.monotonic() + cache_ttl_s
    async with state.lock:
        state.extended_departures_cache[cache_key] = (
            expires_monotonic,
            [replace(dep) for dep in departures],
            list(errors),
            fetched_at_epoch,
        )
    debug_log(
        "view_cache:store "
        f"widget={widget.id} mode={normalized_mode} departures={len(departures)} ttl_s={cache_ttl_s}"
    )
    return departures, fetched_at_epoch, errors


async def refresh_known_stop_ids_if_due(state: RuntimeState, force: bool = False) -> None:
    now_monotonic = time.monotonic()
    async with state.lock:
        should_reload = force or not state.known_stop_ids or now_monotonic >= state.next_known_stop_ids_reload_monotonic
        timeout_seconds = max(30, state.config.feed.http_timeout_seconds * 2)
        reload_in_seconds = max(300, min(state.config.mapping.reload_every_seconds, 3600))
        cached_count = len(state.known_stop_ids)
    if not should_reload:
        return

    started_at = time.monotonic()
    stop_ids, load_error = await asyncio.to_thread(load_known_stop_ids_from_static_gtfs, timeout_seconds)
    async with state.lock:
        if stop_ids:
            state.known_stop_ids = set(stop_ids)
            state.known_stop_ids_error = None
        elif load_error:
            state.known_stop_ids_error = load_error
        state.next_known_stop_ids_reload_monotonic = time.monotonic() + reload_in_seconds

    debug_log(
        "mapping_static:known_stops_refresh "
        f"loaded={len(stop_ids)} cached_before={cached_count} had_error={bool(load_error)} "
        f"next_in_s={reload_in_seconds} duration_s={time.monotonic() - started_at:.2f}"
    )


async def refresh_static_fallback_index_if_due(state: RuntimeState, force: bool = False) -> None:
    target_stop_ids = all_widget_stop_ids(state.config, source="gtfs_rt")
    if not target_stop_ids:
        return

    now_monotonic = time.monotonic()
    async with state.lock:
        should_reload = (
            force
            or state.static_fallback_index is None
            or now_monotonic >= state.next_static_fallback_reload_monotonic
        )
        timeout_seconds = max(45, state.config.feed.http_timeout_seconds * 6)
        reload_in_seconds = max(900, min(state.config.mapping.reload_every_seconds * 6, 7200))
        cached_entries = (
            sum(len(items) for items in state.static_fallback_index.stop_entries.values())
            if state.static_fallback_index is not None
            else 0
        )
    if not should_reload:
        return

    started_at = time.monotonic()
    index, load_error = await asyncio.to_thread(
        load_static_fallback_index_for_stop_ids,
        target_stop_ids,
        timeout_seconds,
    )

    loaded_entries = 0
    if index is not None:
        loaded_entries = sum(len(items) for items in index.stop_entries.values())

    async with state.lock:
        if index is not None:
            state.static_fallback_index = index
            state.static_fallback_error = None
        elif load_error:
            state.static_fallback_error = load_error
        state.next_static_fallback_reload_monotonic = time.monotonic() + reload_in_seconds

    debug_log(
        "fallback_static:refresh "
        f"stop_ids={len(target_stop_ids)} loaded_entries={loaded_entries} cached_before={cached_entries} "
        f"had_error={bool(load_error)} next_in_s={reload_in_seconds} duration_s={time.monotonic() - started_at:.2f}"
    )


async def run_static_cache_warmup(state: RuntimeState) -> None:
    started_at = time.monotonic()
    gtfs_widgets = [widget for widget in state.config.widgets if widget.source == "gtfs_rt"]
    if not gtfs_widgets:
        debug_log("warmup_static_cache:skipped reason=no_gtfs_widgets")
        return

    async with state.lock:
        has_realtime_cache = state.fetched_at_epoch is not None

    if not has_realtime_cache:
        # Skip static warmup on cold start to avoid competing with first realtime fetch.
        debug_log("warmup_static_cache:skipped_until_first_fetch")
        return

    timeout_seconds = max(30, state.config.feed.http_timeout_seconds * 4)
    payload, load_error = await asyncio.to_thread(load_static_gtfs_archive_bytes, timeout_seconds)
    if payload is None:
        debug_log(
            "warmup_static_cache:failed "
            f"error={load_error} duration_s={time.monotonic() - started_at:.2f}"
        )
        return

    debug_log(
        "warmup_static_cache:done "
        f"bytes={len(payload)} widgets={len(gtfs_widgets)} duration_s={time.monotonic() - started_at:.2f}"
    )

    await refresh_known_stop_ids_if_due(state, force=True)
    await refresh_static_fallback_index_if_due(state, force=True)


async def run_startup_warmup(state: RuntimeState) -> None:
    await service_run_startup_warmup(state, _polling_deps())


def ensure_log_file_exists(path: str) -> Optional[str]:
    global ACTIVE_LOG_PATH
    resolved_path, path_warning = _prepare_writable_log_path(path)
    if resolved_path is None:
        return "Logdatei konnte nicht vorbereitet werden."
    ACTIVE_LOG_PATH = str(resolved_path)
    if path_warning:
        app_log(path_warning)
    return None


def read_log_tail_lines(path: str, max_lines: int = LOG_TAIL_LINES) -> tuple[list[str], Optional[str]]:
    prepare_error = ensure_log_file_exists(path)
    if prepare_error:
        return [], prepare_error

    target = Path(ACTIVE_LOG_PATH)
    try:
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            tail = deque(handle, maxlen=max(1, int(max_lines)))
        return [line.rstrip("\r\n") for line in tail], None
    except Exception as exc:
        return [], f"Logdatei konnte nicht gelesen werden: {exc}"


def render_logs_html(
    base_url: str,
    log_path: str,
    lines: list[str],
    read_error: Optional[str],
    startup_ready: bool,
    startup_error: Optional[str],
    startup_ready_since_epoch: Optional[int],
) -> str:
    return views_render_logs_html(
        base_url,
        log_path,
        lines,
        read_error,
        startup_ready,
        startup_error,
        startup_ready_since_epoch,
        app_version=APP_VERSION,
        log_tail_lines=LOG_TAIL_LINES,
        to_local_datetime_fn=to_local_datetime,
    )


def render_widget_html(
    widget: WidgetConfig,
    departures: list[Departure],
    fetched_at_epoch: Optional[int],
    json_url: str,
    errors: Optional[list[str]] = None,
) -> str:
    return views_render_widget_html(
        widget,
        departures,
        fetched_at_epoch,
        json_url,
        errors,
        app_version=APP_VERSION,
        age_seconds_fn=age_seconds,
        to_local_datetime_fn=to_local_datetime,
    )


def render_widget_index_html(config: AppConfig, base_url: str) -> str:
    return views_render_widget_index_html(config, base_url, app_version=APP_VERSION)


def render_service_index_html(
    config: AppConfig,
    base_url: str,
    startup_ready: bool,
    startup_error: Optional[str],
    startup_ready_since_epoch: Optional[int],
) -> str:
    return views_render_service_index_html(
        config,
        base_url,
        startup_ready,
        startup_error,
        startup_ready_since_epoch,
        app_version=APP_VERSION,
        log_tail_lines=LOG_TAIL_LINES,
        to_local_datetime_fn=to_local_datetime,
    )



def render_switch_debug_mode_html(
    base_url: str,
    debug_status: dict,
    message: Optional[str],
    message_ok: bool,
) -> str:
    return views_render_switch_debug_mode_html(
        base_url,
        debug_status,
        message,
        message_ok,
        app_version=APP_VERSION,
    )
def build_config_excerpt(config: AppConfig) -> dict:
    return {
        "server": {
            "host": config.server.host,
            "port": config.server.port,
        },
        "feed": {
            "url": config.feed.url,
            "refresh_seconds": config.feed.refresh_seconds,
            "http_timeout_seconds": config.feed.http_timeout_seconds,
        },
        "debug": {
            "enabled": config.debug.enabled,
            "log_path": config.debug.log_path,
        },
        "widgets": [
            {
                "id": widget.id,
                "title": widget.title,
                "source": widget.source,
                "stop_ids": widget.stop_ids,
                "db_eva_no": widget.db_eva_no,
                "direction_contains": widget.direction_contains,
                "required_stops": widget.required_stops,
                "db_lookahead_hours": widget.db_lookahead_hours,
                "db_only_trains": widget.db_only_trains,
                "db_use_fchg": widget.db_use_fchg,
                "gtfs_lookahead_hours": widget.gtfs_lookahead_hours,
                "route_short_names": widget.route_short_names,
                "max_departures": widget.max_departures,
                "show_delay": widget.show_delay,
                "show_feed_age": widget.show_feed_age,
            }
            for widget in config.widgets
        ],
        "mapping": {
            "trip_route_map_csv": config.mapping.trip_route_map_csv,
            "reload_every_seconds": config.mapping.reload_every_seconds,
        },
    }


def create_app(config: AppConfig) -> FastAPI:
    state = RuntimeState(config=config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async def _run_startup_tasks() -> None:
            app_log("startup_background:begin")
            try:
                if WARMUP_STATIC_CACHE_ON_START:
                    await run_static_cache_warmup(state)
                if WARMUP_ON_START:
                    await run_startup_warmup(state)
            except Exception as exc:
                app_log(f"startup_background:error error={exc}")
                async with state.lock:
                    state.startup_error = str(exc)
            finally:
                async with state.lock:
                    state.startup_ready = True
                    state.startup_ready_since_epoch = int(time.time())
                    state.startup_task = None
                app_log("startup_background:done")

        async with state.lock:
            state.startup_ready = False
            state.startup_error = None
            state.startup_ready_since_epoch = None

        startup_task = asyncio.create_task(_run_startup_tasks())
        async with state.lock:
            state.startup_task = startup_task

        try:
            yield
        finally:
            async with state.lock:
                task = state.startup_task
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="timetable-widget", version=APP_VERSION, lifespan=lifespan)
    app.state.runtime = state

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if not request.url.path.startswith("/logs"):
                app_log(
                    f"request:error method={request.method} path={request.url.path} duration_ms={duration_ms} error={exc}"
                )
            raise
        duration_ms = int((time.perf_counter() - started) * 1000)
        if not request.url.path.startswith("/logs"):
            app_log(
                f"request method={request.method} path={request.url.path} status={response.status_code} duration_ms={duration_ms}"
            )
        return response
    @app.get("/", response_class=HTMLResponse)
    async def get_service_index(request: Request) -> HTMLResponse:
        async with state.lock:
            startup_ready = state.startup_ready
            startup_error = state.startup_error
            startup_ready_since_epoch = state.startup_ready_since_epoch
        return HTMLResponse(render_service_index_html(config, str(request.base_url), startup_ready, startup_error, startup_ready_since_epoch))
    @app.get("/logs", response_class=HTMLResponse)
    async def get_logs(request: Request, format: str = "html"):
        lines, read_error = await asyncio.to_thread(read_log_tail_lines, ACTIVE_LOG_PATH, LOG_TAIL_LINES)
        async with state.lock:
            startup_ready = state.startup_ready
            startup_error = state.startup_error
            startup_ready_since_epoch = state.startup_ready_since_epoch

        payload_lines = list(lines)
        if read_error:
            payload_lines = [read_error, "", *payload_lines]
        text_payload = "\n".join(payload_lines)

        output_format = (format or "html").strip().lower()
        if output_format == "text":
            return PlainTextResponse(text_payload)
        if output_format == "json":
            return JSONResponse(
                {
                    "app_version": APP_VERSION,
                    "log_path": ACTIVE_LOG_PATH,
                    "tail_lines": lines,
                    "read_error": read_error,
                    "startup_ready": startup_ready,
                    "startup_error": startup_error,
                    "startup_ready_since_epoch": startup_ready_since_epoch,
                }
            )
        return HTMLResponse(
            render_logs_html(
                str(request.base_url),
                ACTIVE_LOG_PATH,
                lines,
                read_error,
                startup_ready,
                startup_error,
                startup_ready_since_epoch,
            )
        )

    @app.get("/widget", response_class=HTMLResponse)
    async def get_widget_overview(request: Request) -> HTMLResponse:
        return HTMLResponse(render_widget_index_html(config, str(request.base_url)))
    async def _build_json_payload(widget: WidgetConfig, view_mode: str) -> dict:
        departures, fetched_at_epoch, errors = await get_widget_departures_for_view(state, widget, view_mode)
        return {
            "widget_id": widget.id,
            "widget_title": widget.title,
            "view_mode": view_mode,
            "fetched_at": fetched_at_epoch,
            "age_s": age_seconds(fetched_at_epoch),
            "departures": [dep.to_dict() for dep in departures],
            "errors": errors,
            "app_version": APP_VERSION,
            "widgets": [{"id": w.id, "title": w.title} for w in config.widgets],
            "config": build_config_excerpt(config),
        }
    @app.get("/widget/{widget_id}", response_class=HTMLResponse)
    async def get_widget(widget_id: str) -> HTMLResponse:
        widget = find_widget(config, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail=f"Widget-ID {widget_id} nicht gefunden.")
        departures, fetched_at_epoch, errors = await get_widget_departures_for_view(state, widget, "default")
        return HTMLResponse(render_widget_html(widget, departures, fetched_at_epoch, f"/json/{widget.id}", errors))
    @app.get("/widget/{widget_id}/24h", response_class=HTMLResponse)
    async def get_widget_24h(widget_id: str) -> HTMLResponse:
        widget = find_widget(config, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail=f"Widget-ID {widget_id} nicht gefunden.")
        departures, fetched_at_epoch, errors = await get_widget_departures_for_view(state, widget, "24h")
        return HTMLResponse(render_widget_html(widget, departures, fetched_at_epoch, f"/json/{widget.id}/24h", errors))
    @app.get("/json/{widget_id}", response_class=JSONResponse)
    async def get_json(widget_id: str) -> JSONResponse:
        widget = find_widget(config, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail=f"Widget-ID {widget_id} nicht gefunden.")
        payload = await _build_json_payload(widget, "default")
        return JSONResponse(payload)
    @app.get("/json/{widget_id}/24h", response_class=JSONResponse)
    async def get_json_24h(widget_id: str) -> JSONResponse:
        widget = find_widget(config, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail=f"Widget-ID {widget_id} nicht gefunden.")
        payload = await _build_json_payload(widget, "24h")
        return JSONResponse(payload)
    @app.get("/health", response_class=JSONResponse)
    async def get_health() -> JSONResponse:
        async with state.lock:
            fetched_at_epoch = state.fetched_at_epoch
            aggregated_errors = []
            for widget in config.widgets:
                for error in state.errors_by_widget.get(widget.id, []):
                    aggregated_errors.append(f"[widget {widget.id}] {error}")
        return JSONResponse(
            {
                "ok": True,
                "app_version": APP_VERSION,
                "age_s": age_seconds(fetched_at_epoch),
                "errors": aggregated_errors,
            }
        )


    @app.get("/switchdebugmode", response_class=HTMLResponse)
    async def get_switch_debug_mode(request: Request, message: Optional[str] = None, status: Optional[str] = None) -> HTMLResponse:
        message_ok = (status or "ok").strip().lower() != "error"
        return HTMLResponse(
            render_switch_debug_mode_html(
                str(request.base_url),
                get_debug_status(),
                message,
                message_ok,
            )
        )

    @app.post("/switchdebugmode", response_class=HTMLResponse)
    async def post_switch_debug_mode(request: Request) -> HTMLResponse:
        raw_body = (await request.body()).decode("utf-8", errors="ignore")
        form_data = parse_qs(raw_body, keep_blank_values=True)
        mode = str(form_data.get("debug_mode", [""])[0]).strip().lower()

        if mode == "on":
            ok, message = configure_debug_logger(True)
        elif mode == "off":
            ok, message = configure_debug_logger(False)
        else:
            ok = False
            message = "Ungültiger Wert für Debug-Modus. Erlaubt sind on oder off."

        return HTMLResponse(
            render_switch_debug_mode_html(
                str(request.base_url),
                get_debug_status(),
                message,
                ok,
            )
        )
    @app.get("/debug", response_class=JSONResponse)
    async def get_debug_config() -> JSONResponse:
        return JSONResponse(get_debug_status())

    @app.post("/debug/on", response_class=JSONResponse)
    async def enable_debug(log_path: Optional[str] = None) -> JSONResponse:
        ok, message = configure_debug_logger(True, log_path)
        return JSONResponse(
            {
                "ok": ok,
                "message": message,
                **get_debug_status(),
            }
        )

    @app.post("/debug/off", response_class=JSONResponse)
    async def disable_debug() -> JSONResponse:
        _ok, message = configure_debug_logger(False)
        return JSONResponse(
            {
                "ok": True,
                "message": message,
                **get_debug_status(),
            }
        )

    return app


def main() -> None:
    config_data = load_yaml(CONFIG_PATH)
    config = parse_config(config_data)
    configure_debug_logger(config.debug.enabled, config.debug.log_path)
    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()
















































































