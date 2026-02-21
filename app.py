import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from google.transit import gtfs_realtime_pb2


CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/config.yaml")
FALLBACK_CONFIG_PATH = os.getenv("FALLBACK_CONFIG_PATH", "/app/config.example.yaml")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
)
GTFS_STATIC_URL = os.getenv("GTFS_STATIC_URL", "https://download.gtfs.de/germany/nv_free/latest.zip")
GTFS_STATIC_CACHE_PATH = os.getenv("GTFS_STATIC_CACHE_PATH", "/tmp/nv_free_latest.zip")
try:
    GTFS_STATIC_CACHE_MAX_AGE_SECONDS = int(os.getenv("GTFS_STATIC_CACHE_MAX_AGE_SECONDS", "43200"))
except ValueError:
    GTFS_STATIC_CACHE_MAX_AGE_SECONDS = 43200
DB_IRIS_BASE_URL = os.getenv("DB_IRIS_BASE_URL", "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1")
DB_TIMETABLES_BASE_URL = os.getenv("DB_TIMETABLES_BASE_URL", DB_IRIS_BASE_URL).rstrip("/")
DB_CLIENT_ID = os.getenv("DB_CLIENT_ID", "").strip()
DB_API_KEY = os.getenv("DB_API_KEY", "").strip()
DEBUG_MODE = str(os.getenv("DEBUG_MODE", "0")).strip().lower() in {"1", "true", "yes", "on"}
DEBUG_LOG_PATH = os.getenv("DEBUG_LOG_PATH", "/logs/logfile.txt")
WARMUP_ON_START = str(os.getenv("WARMUP_ON_START", "0")).strip().lower() in {"1", "true", "yes", "on"}
WARMUP_STATIC_CACHE_ON_START = str(os.getenv("WARMUP_STATIC_CACHE_ON_START", "1")).strip().lower() in {"1", "true", "yes", "on"}
LOCAL_TIMEZONE_NAME = os.getenv("LOCAL_TIMEZONE", "Europe/Berlin")
try:
    LOCAL_TIMEZONE = ZoneInfo(LOCAL_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    LOCAL_TIMEZONE = ZoneInfo("UTC")


def to_local_datetime(epoch_seconds: int) -> datetime:
    return datetime.fromtimestamp(epoch_seconds, tz=LOCAL_TIMEZONE)


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


def configure_debug_logger(enabled: bool, log_path: Optional[str] = None) -> tuple[bool, str]:
    global DEBUG_ENABLED
    global DEBUG_LOGGER
    global DEBUG_LOG_PATH

    with DEBUG_LOGGER_LOCK:
        if log_path is not None:
            candidate = str(log_path).strip()
            if candidate:
                DEBUG_LOG_PATH = candidate
        if not DEBUG_LOG_PATH:
            DEBUG_LOG_PATH = "/logs/logfile.txt"

        logger = logging.getLogger("timetable_widget_debug")
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if not enabled:
            _close_logger_handlers(logger)
            DEBUG_LOGGER = None
            DEBUG_ENABLED = False
            return False, "Debug-Modus deaktiviert."

        try:
            resolved_path = Path(DEBUG_LOG_PATH)
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            _close_logger_handlers(logger)
            handler = logging.FileHandler(resolved_path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            logger.info("Debug mode enabled. Log path: %s", resolved_path)
            DEBUG_LOGGER = logger
            DEBUG_ENABLED = True
            return True, f"Debug-Modus aktiviert. Log-Datei: {resolved_path}"
        except Exception as exc:
            _close_logger_handlers(logger)
            DEBUG_LOGGER = None
            DEBUG_ENABLED = False
            return False, f"Debug-Modus konnte nicht aktiviert werden: {exc}"


def get_debug_status() -> dict:
    with DEBUG_LOGGER_LOCK:
        return {
            "enabled": DEBUG_ENABLED,
            "log_path": DEBUG_LOG_PATH,
            "active_logger": DEBUG_LOGGER is not None,
        }


configure_debug_logger(DEBUG_MODE, DEBUG_LOG_PATH)


def debug_log(message: str) -> None:
    logger = DEBUG_LOGGER
    if logger:
        logger.info(message)


def build_db_timetables_headers() -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml",
    }
    if DB_CLIENT_ID:
        headers["DB-Client-Id"] = DB_CLIENT_ID
    if DB_API_KEY:
        headers["DB-Api-Key"] = DB_API_KEY
    return headers


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

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "direction": self.direction,
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
    refresh_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def load_yaml(path: str) -> dict:
    target = Path(path)
    if target.is_dir():
        candidates = [
            target / "config.yaml",
            target / "config.yml",
            target / "config.example.yaml",
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

    return AppConfig(server=server, feed=feed, widgets=widgets, mapping=mapping)


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
    try:
        response = httpx.get(GTFS_STATIC_URL, timeout=timeout_seconds, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except Exception as exc:
        debug_log(f"mapping_fallback:download_failed error={exc}")
        return {}, {}, f"mapping fallback download failed: {exc}"
    download_elapsed = time.monotonic() - download_started
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
                return {}, {}, "mapping fallback: keine Trips für konfigurierte stop_ids gefunden"

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


@dataclass
class DBIrisEventChange:
    tl: dict[str, str] = field(default_factory=dict)
    ar: dict[str, str] = field(default_factory=dict)
    dp: dict[str, str] = field(default_factory=dict)
    message_types: set[str] = field(default_factory=set)


def parse_db_iris_timestamp(raw_value: str) -> Optional[int]:
    text = str(raw_value or "").strip()
    if len(text) != 10:
        return None
    try:
        dt_local = datetime.strptime(text, "%y%m%d%H%M").replace(tzinfo=LOCAL_TIMEZONE)
    except ValueError:
        return None
    return int(dt_local.timestamp())


def _is_db_train_departure(route: str, category: str, train_number: str) -> bool:
    line = str(route or "").strip().upper().replace(" ", "")
    cat = str(category or "").strip().upper()
    train_no = str(train_number or "").strip()

    explicit_line_prefixes = (
        "ICE",
        "IC",
        "EC",
        "ECE",
        "RJX",
        "RJ",
        "EN",
        "NJ",
        "RE",
        "RB",
        "IRE",
        "IR",
        "FLX",
        "ALX",
        "MEX",
        "MRB",
        "ERB",
        "TLX",
        "TL",
        "DPN",
        "ABR",
        "HEX",
        "ME",
    )
    if line.startswith(explicit_line_prefixes):
        return True
    if re.match(r"^S\d", line):
        return True
    if re.match(r"^U\d", line):
        return True

    explicit_categories = {
        "ICE",
        "IC",
        "EC",
        "ECE",
        "RJ",
        "RJX",
        "EN",
        "NJ",
        "RE",
        "RB",
        "IRE",
        "IR",
        "S",
        "U",
        "FLX",
        "ALX",
        "MEX",
        "TL",
        "TLX",
        "AG",
    }
    if cat in explicit_categories:
        return True
    if cat in {"BUS", "SEV", "TRAM", "STR"}:
        return False

    if train_no and cat and cat not in {"BUS", "SEV", "TRAM", "STR"}:
        return True

    return False


def parse_db_iris_fchg_changes(payload: bytes) -> dict[str, DBIrisEventChange]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"DB-IRIS fchg XML parse failed: {exc}") from exc

    changes: dict[str, DBIrisEventChange] = {}
    for stop_event in root.findall("s"):
        event_id = str(stop_event.attrib.get("id", "")).strip()
        if not event_id:
            continue
        change = DBIrisEventChange()
        tl_node = stop_event.find("tl")
        ar_node = stop_event.find("ar")
        dp_node = stop_event.find("dp")
        if tl_node is not None:
            change.tl.update({str(k): str(v) for k, v in tl_node.attrib.items()})
        if ar_node is not None:
            change.ar.update({str(k): str(v) for k, v in ar_node.attrib.items()})
        if dp_node is not None:
            change.dp.update({str(k): str(v) for k, v in dp_node.attrib.items()})
        for message_node in stop_event.findall("m"):
            msg_type = str(message_node.attrib.get("t", "")).strip().lower()
            if msg_type:
                change.message_types.add(msg_type)
        changes[event_id] = change
    return changes


def parse_db_iris_plan_departures(
    widget: WidgetConfig,
    payload: bytes,
    now_epoch: int,
    changes_by_event_id: Optional[dict[str, DBIrisEventChange]] = None,
) -> list[Departure]:
    departures: list[Departure] = []
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ValueError(f"DB-IRIS XML parse failed: {exc}") from exc

    route_filter = set(widget.route_short_names or [])
    for stop_event in root.findall("s"):
        event_id = str(stop_event.attrib.get("id", "")).strip()
        departure_node = stop_event.find("dp")
        arrival_node = stop_event.find("ar")
        train_node = stop_event.find("tl")
        if departure_node is None:
            continue

        change = (changes_by_event_id or {}).get(event_id)
        train_attrs = dict(train_node.attrib) if train_node is not None else {}
        departure_attrs = dict(departure_node.attrib)
        arrival_attrs = dict(arrival_node.attrib) if arrival_node is not None else {}
        if change:
            train_attrs.update(change.tl)
            departure_attrs.update(change.dp)
            arrival_attrs.update(change.ar)

        cancel_state = str(departure_attrs.get("cs") or arrival_attrs.get("cs") or "").strip().lower()
        if cancel_state == "c":
            continue
        if change and "c" in change.message_types:
            continue

        planned_epoch = parse_db_iris_timestamp(departure_attrs.get("pt", "") or arrival_attrs.get("pt", ""))
        changed_epoch = parse_db_iris_timestamp(departure_attrs.get("ct", "") or arrival_attrs.get("ct", ""))
        time_epoch = changed_epoch or planned_epoch
        if time_epoch is None or time_epoch < now_epoch:
            continue

        route = str(departure_attrs.get("l", "") or arrival_attrs.get("l", "")).strip()
        if not route:
            route = str(train_attrs.get("c", "")).strip()
        if not route:
            route = str(train_attrs.get("n", "")).strip()

        if widget.db_only_trains and not _is_db_train_departure(
            route,
            str(train_attrs.get("c", "")),
            str(train_attrs.get("n", "")),
        ):
            continue
        if route_filter:
            if not route or route not in route_filter:
                continue

        path_text = str(
            departure_attrs.get("cpth")
            or departure_attrs.get("ppth")
            or arrival_attrs.get("cpth")
            or arrival_attrs.get("ppth")
            or ""
        )
        path_stops = [part.strip() for part in path_text.split("|") if part.strip()]
        direction = path_stops[0] if path_stops else ""
        if not _matches_widget_text_filters(widget, direction, path_stops):
            continue

        platform = str(
            departure_attrs.get("cp")
            or departure_attrs.get("pp")
            or arrival_attrs.get("cp")
            or arrival_attrs.get("pp")
            or ""
        ).strip() or None

        delay_s: Optional[int] = None
        if widget.show_delay and planned_epoch is not None and changed_epoch is not None:
            delay_s = changed_epoch - planned_epoch
        in_min = max(0, int((time_epoch - now_epoch) // 60))
        departures.append(
            Departure(
                route=route,
                direction=direction,
                platform=platform,
                stop_id=str(stop_event.attrib.get("eva", widget.db_eva_no or "")).strip(),
                time_epoch=time_epoch,
                time_local=to_local_datetime(time_epoch).strftime("%H:%M"),
                in_min=in_min,
                delay_s=delay_s,
                trip_id=event_id,
            )
        )

    departures.sort(key=lambda item: item.time_epoch)
    return departures


async def fetch_db_iris_departures(widget: WidgetConfig, timeout_seconds: int, now_epoch: int) -> list[Departure]:
    started_at = time.monotonic()
    eva_no = str(widget.db_eva_no or "").strip()
    if not eva_no:
        raise ValueError(f"Widget {widget.id}: db_eva_no fehlt.")

    now_local = to_local_datetime(now_epoch).replace(minute=0, second=0, microsecond=0)
    merged: list[Departure] = []
    if not DB_CLIENT_ID or not DB_API_KEY:
        raise ValueError("DB API credentials fehlen. Setze DB_CLIENT_ID und DB_API_KEY als Environment-Variablen.")

    debug_log(
        f"db_iris:fetch_start widget={widget.id} eva={eva_no} lookahead_h={widget.db_lookahead_hours}"
    )

    async with httpx.AsyncClient(timeout=timeout_seconds, headers=build_db_timetables_headers()) as client:
        changes_by_event_id: dict[str, DBIrisEventChange] = {}
        if widget.db_use_fchg:
            fchg_started = time.monotonic()
            try:
                fchg_url = f"{DB_TIMETABLES_BASE_URL}/fchg/{eva_no}"
                fchg_response = await client.get(fchg_url)
                fchg_response.raise_for_status()
                changes_by_event_id = parse_db_iris_fchg_changes(fchg_response.content)
                debug_log(
                    "db_iris:fchg_ok "
                    f"widget={widget.id} changes={len(changes_by_event_id)} duration_s={time.monotonic() - fchg_started:.2f}"
                )
            except Exception as exc:
                debug_log(f"db_iris:fchg_unavailable widget={widget.id} error={exc}")

        requested_plan_requests = 0
        successful_plan_requests = 0
        not_found_plan_requests = 0
        for offset in range(widget.db_lookahead_hours):
            slot = now_local + timedelta(hours=offset)
            date_token = slot.strftime("%y%m%d")
            hour_token = slot.strftime("%H")
            url = f"{DB_TIMETABLES_BASE_URL}/plan/{eva_no}/{date_token}/{hour_token}"
            slot_started = time.monotonic()
            requested_plan_requests += 1
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Some stations/hours have no dataset chunk; skip instead of failing whole widget.
                    not_found_plan_requests += 1
                    continue
                raise
            successful_plan_requests += 1
            parsed_slot_departures = parse_db_iris_plan_departures(widget, response.content, now_epoch, changes_by_event_id)
            merged.extend(parsed_slot_departures)
            debug_log(
                "db_iris:plan_slot_ok "
                f"widget={widget.id} slot={date_token}{hour_token} departures={len(parsed_slot_departures)} "
                f"duration_s={time.monotonic() - slot_started:.2f}"
            )
            if len(merged) >= widget.max_departures:
                break

        if successful_plan_requests == 0:
            raise ValueError(f"DB-IRIS plan returned no available time slices for EVA {eva_no}.")

    merged.sort(key=lambda item: item.time_epoch)
    dedup: dict[tuple[str, int, str], Departure] = {}
    for item in merged:
        dedup[(item.trip_id, item.time_epoch, item.route)] = item
    result = list(sorted(dedup.values(), key=lambda item: item.time_epoch))[: widget.max_departures]
    debug_log(
        "db_iris:fetch_done "
        f"widget={widget.id} eva={eva_no} requested={requested_plan_requests} ok={successful_plan_requests} "
        f"not_found={not_found_plan_requests} merged={len(merged)} deduped={len(dedup)} result={len(result)} "
        f"duration_s={time.monotonic() - started_at:.2f}"
    )
    return result
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
    cache_path = Path(GTFS_STATIC_CACHE_PATH)
    now_epoch = time.time()

    if cache_path.is_file():
        try:
            age_s = max(0, int(now_epoch - cache_path.stat().st_mtime))
        except OSError:
            age_s = GTFS_STATIC_CACHE_MAX_AGE_SECONDS + 1
        if age_s <= GTFS_STATIC_CACHE_MAX_AGE_SECONDS:
            try:
                payload = cache_path.read_bytes()
                debug_log(
                    f"mapping_static:cache_hit path={cache_path} bytes={len(payload)} age_s={age_s}"
                )
                return payload, None
            except Exception as exc:
                debug_log(f"mapping_static:cache_read_failed path={cache_path} error={exc}")

    download_started = time.monotonic()
    try:
        response = httpx.get(GTFS_STATIC_URL, timeout=timeout_seconds, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        payload = response.content
        debug_log(
            "mapping_static:download_ok "
            f"url={GTFS_STATIC_URL} bytes={len(payload)} duration_s={time.monotonic() - download_started:.2f}"
        )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(payload)
            debug_log(f"mapping_static:cache_write_ok path={cache_path} bytes={len(payload)}")
        except Exception as exc:
            debug_log(f"mapping_static:cache_write_failed path={cache_path} error={exc}")
        return payload, None
    except Exception as exc:
        debug_log(f"mapping_static:download_failed error={exc}")
        if cache_path.is_file():
            try:
                payload = cache_path.read_bytes()
                debug_log(
                    f"mapping_static:stale_cache_used path={cache_path} bytes={len(payload)}"
                )
                return payload, None
            except Exception as cache_exc:
                debug_log(f"mapping_static:stale_cache_read_failed path={cache_path} error={cache_exc}")
        return None, f"mapping static download failed: {exc}"


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


def load_static_fallback_index_for_stop_ids(
    stop_ids: list[str], timeout_seconds: int
) -> tuple[Optional[StaticFallbackIndex], Optional[str]]:
    started_at = time.monotonic()
    target_stop_ids = {stop_id.strip() for stop_id in stop_ids if stop_id.strip()}
    empty_index = StaticFallbackIndex(
        stop_entries={},
        trip_route={},
        trip_direction={},
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

    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            stop_entries: dict[str, list[tuple[str, int]]] = {stop_id: [] for stop_id in target_stop_ids}
            relevant_trip_ids: set[str] = set()

            stop_times_started = time.monotonic()
            with archive.open("stop_times.txt", "r") as handle:
                reader = csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8", newline=""))
                for row in reader:
                    stop_id = str(row.get("stop_id", "")).strip()
                    if stop_id not in target_stop_ids:
                        continue
                    trip_id = str(row.get("trip_id", "")).strip()
                    if not trip_id:
                        continue
                    departure_seconds = _parse_gtfs_hms_to_seconds(
                        str(row.get("departure_time", "")).strip() or str(row.get("arrival_time", "")).strip()
                    )
                    if departure_seconds is None:
                        continue
                    stop_entries.setdefault(stop_id, []).append((trip_id, departure_seconds))
                    relevant_trip_ids.add(trip_id)
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

            index = StaticFallbackIndex(
                stop_entries=stop_entries,
                trip_route=trip_route,
                trip_direction=trip_direction,
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
                f"stop_times_s={stop_times_elapsed:.2f} trips_s={trips_elapsed:.2f} routes_s={routes_elapsed:.2f} "
                f"calendar_s={calendar_elapsed:.2f} calendar_dates_s={calendar_dates_elapsed:.2f} "
                f"total_s={time.monotonic() - started_at:.2f}"
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
            direction = index.trip_direction.get(trip_id, "")

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
    merged: list[Departure] = list(realtime_departures)
    seen_keys = {(dep.trip_id, dep.time_epoch, dep.stop_id) for dep in merged}

    for dep in fallback_departures:
        dedup_key = (dep.trip_id, dep.time_epoch, dep.stop_id)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        merged.append(dep)

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
    if not url:
        raise ValueError("feed.url is empty")
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


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
        state.route_map = route_map
        state.trip_destination_map = trip_destination_map
        state.mapping_error = mapping_error
        state.next_mapping_reload_monotonic = now_monotonic + reload_in_seconds
    debug_log(
        "mapping_reload:finished "
        f"routes={len(route_map)} destinations={len(trip_destination_map)} error={bool(mapping_error)} "
        f"next_in_s={reload_in_seconds} duration_s={time.monotonic() - started_at:.2f}"
    )

async def poll_once(state: RuntimeState) -> None:
    started_at = time.monotonic()
    debug_log("poll_once:started")
    if not state.config.widgets:
        async with state.lock:
            state.errors_by_widget = {}
            state.departures_by_widget = {}
            state.next_refresh_due_monotonic = time.monotonic() + state.config.feed.refresh_seconds
        debug_log("poll_once:widgets_empty")
        return

    now_epoch = int(time.time())
    gtfs_widgets = [widget for widget in state.config.widgets if widget.source == "gtfs_rt"]
    db_widgets = [widget for widget in state.config.widgets if widget.source == "db_iris"]

    route_map: dict[str, str] = {}
    trip_destination_map: dict[str, str] = {}
    mapping_error: Optional[str] = None
    if gtfs_widgets:
        mapping_stage_started = time.monotonic()
        await reload_mapping_if_due(state)
        async with state.lock:
            route_map = dict(state.route_map)
            trip_destination_map = dict(state.trip_destination_map)
            mapping_error = state.mapping_error
        debug_log(
            "poll_once:mapping_ready "
            f"routes={len(route_map)} destinations={len(trip_destination_map)} "
            f"has_error={bool(mapping_error)} duration_s={time.monotonic() - mapping_stage_started:.2f}"
        )

    departures_by_widget: dict[str, list[Departure]] = {widget.id: [] for widget in state.config.widgets}
    errors_by_widget: dict[str, list[str]] = {widget.id: [] for widget in state.config.widgets}
    total_departures = 0

    if gtfs_widgets:
        try:
            feed_fetch_started = time.monotonic()
            feed_bytes = await fetch_feed_bytes(state.config.feed.url, state.config.feed.http_timeout_seconds)
            feed_fetch_elapsed = time.monotonic() - feed_fetch_started

            parse_started = time.monotonic()
            feed_message = gtfs_realtime_pb2.FeedMessage()
            feed_message.ParseFromString(feed_bytes)
            parse_elapsed = time.monotonic() - parse_started

            configured_stop_ids = set(all_widget_stop_ids(state.config, source="gtfs_rt"))
            await refresh_known_stop_ids_if_due(state)
            async with state.lock:
                known_stop_ids = set(state.known_stop_ids)
                known_stop_ids_error = state.known_stop_ids_error
            debug_log(
                "poll_once:gtfs_feed_ready "
                f"bytes={len(feed_bytes)} entities={len(feed_message.entity)} known_stop_ids={len(known_stop_ids)} "
                f"fetch_s={feed_fetch_elapsed:.2f} parse_s={parse_elapsed:.2f}"
            )

            enrich_started = time.monotonic()
            relevant_trip_ids, relevant_last_stops = collect_realtime_trip_context(feed_message, configured_stop_ids)
            missing_route_trip_ids = {trip_id for trip_id in relevant_trip_ids if trip_id not in route_map}
            missing_destination_trip_ids = {trip_id for trip_id in relevant_trip_ids if trip_id not in trip_destination_map}
            if missing_route_trip_ids or missing_destination_trip_ids:
                enrich_route_map, enrich_destination_map, enrich_error = await asyncio.to_thread(
                    load_trip_maps_for_trip_ids_from_static_gtfs,
                    relevant_trip_ids,
                    relevant_last_stops,
                    max(30, state.config.feed.http_timeout_seconds * 4),
                )
                added_routes = 0
                added_destinations = 0
                for trip_id, line in enrich_route_map.items():
                    if trip_id not in route_map and line:
                        route_map[trip_id] = line
                        added_routes += 1
                for trip_id, direction in enrich_destination_map.items():
                    if trip_id not in trip_destination_map and direction:
                        trip_destination_map[trip_id] = direction
                        added_destinations += 1

                if added_routes or added_destinations:
                    try:
                        persist_trip_maps_to_csv(state.config.mapping.trip_route_map_csv, route_map, trip_destination_map)
                        debug_log(
                            "poll_once:mapping_persisted "
                            f"path={state.config.mapping.trip_route_map_csv} routes={len(route_map)} "
                            f"destinations={len(trip_destination_map)}"
                        )
                    except Exception as exc:
                        debug_log(f"poll_once:mapping_persist_failed error={exc}")
                    async with state.lock:
                        state.route_map = dict(route_map)
                        state.trip_destination_map = dict(trip_destination_map)

                if enrich_error:
                    mapping_error = f"{mapping_error} | {enrich_error}" if mapping_error else enrich_error

                debug_log(
                    "poll_once:mapping_enriched "
                    f"relevant_trips={len(relevant_trip_ids)} missing_routes={len(missing_route_trip_ids)} "
                    f"missing_destinations={len(missing_destination_trip_ids)} added_routes={added_routes} "
                    f"added_destinations={added_destinations} duration_s={time.monotonic() - enrich_started:.2f}"
                )
            else:
                debug_log(
                    "poll_once:mapping_enriched "
                    f"relevant_trips={len(relevant_trip_ids)} missing_routes=0 missing_destinations=0 "
                    f"duration_s={time.monotonic() - enrich_started:.2f}"
                )

            static_fallback_index: Optional[StaticFallbackIndex] = None
            static_fallback_error: Optional[str] = None
            static_fallback_loaded = False

            for widget in gtfs_widgets:
                widget_started = time.monotonic()
                widget_errors = errors_by_widget[widget.id]
                if mapping_error:
                    widget_errors.append(mapping_error)
                if not widget.stop_ids:
                    widget_errors.append(f"Widget {widget.id}: stop_ids ist leer; keine Treffer möglich.")
                if widget.route_short_names and not route_map:
                    widget_errors.append(
                        f"Widget {widget.id}: route_short_names ist gesetzt, aber Mapping ist leer/nicht verfügbar."
                    )
                if not known_stop_ids and known_stop_ids_error:
                    widget_errors.append(f"Stop-ID-Validierung aktuell nicht verfügbar: {known_stop_ids_error}")
                for stop_id in widget.stop_ids:
                    if known_stop_ids and stop_id not in known_stop_ids:
                        widget_errors.append(f"Falsche Konfiguration: Stop-ID {stop_id} nicht gefunden.")

                departures = extract_departures(feed_message, widget, route_map, trip_destination_map, now_epoch)
                realtime_count = len(departures)

                if realtime_count < widget.max_departures and widget.stop_ids:
                    if not static_fallback_loaded:
                        await refresh_static_fallback_index_if_due(state)
                        async with state.lock:
                            static_fallback_index = state.static_fallback_index
                            static_fallback_error = state.static_fallback_error
                        static_fallback_loaded = True

                    if static_fallback_index is not None:
                        fallback_departures = extract_static_schedule_departures(widget, static_fallback_index, now_epoch)
                        departures = merge_departures_realtime_with_fallback(
                            departures,
                            fallback_departures,
                            widget.max_departures,
                        )
                        debug_log(
                            "poll_once:gtfs_fallback "
                            f"widget={widget.id} realtime={realtime_count} fallback_candidates={len(fallback_departures)} "
                            f"merged={len(departures)}"
                        )
                    elif static_fallback_error and realtime_count == 0:
                        widget_errors.append(f"Statischer Fahrplan-Fallback nicht verfügbar: {static_fallback_error}")

                departures_by_widget[widget.id] = departures
                total_departures += len(departures)
                debug_log(
                    "poll_once:gtfs_widget_done "
                    f"widget={widget.id} departures={len(departures)} errors={len(widget_errors)} "
                    f"duration_s={time.monotonic() - widget_started:.2f}"
                )
        except Exception as exc:
            for widget in gtfs_widgets:
                errors_by_widget[widget.id].append(f"GTFS feed fetch failed: {exc}")
            debug_log(f"poll_once:gtfs_fetch_error error={exc}")

    for widget in db_widgets:
        widget_started = time.monotonic()
        if not widget.db_eva_no:
            errors_by_widget[widget.id].append(f"Widget {widget.id}: db_eva_no fehlt.")
            continue
        try:
            departures = await fetch_db_iris_departures(widget, state.config.feed.http_timeout_seconds, now_epoch)
            departures_by_widget[widget.id] = departures
            total_departures += len(departures)
            debug_log(
                "poll_once:db_widget_done "
                f"widget={widget.id} departures={len(departures)} duration_s={time.monotonic() - widget_started:.2f}"
            )
        except Exception as exc:
            errors_by_widget[widget.id].append(f"DB-IRIS Abruf fehlgeschlagen: {exc}")
            debug_log(f"poll_once:db_iris_error widget={widget.id} error={exc}")

    async with state.lock:
        state.departures_by_widget = departures_by_widget
        state.fetched_at_epoch = now_epoch
        state.errors_by_widget = errors_by_widget
        state.next_refresh_due_monotonic = time.monotonic() + state.config.feed.refresh_seconds
    debug_log(
        "poll_once:ok "
        f"widgets={len(state.config.widgets)} gtfs={len(gtfs_widgets)} db={len(db_widgets)} "
        f"departures={total_departures} duration_s={time.monotonic() - started_at:.2f}"
    )

async def ensure_data_fresh(state: RuntimeState, force: bool = False) -> None:
    task: Optional[asyncio.Task] = None

    async with state.lock:
        now_monotonic = time.monotonic()
        is_stale = state.fetched_at_epoch is None or now_monotonic >= state.next_refresh_due_monotonic
        if not force and not is_stale:
            debug_log("ensure_data_fresh:cache_hit")
            return

        if state.refresh_task and not state.refresh_task.done():
            task = state.refresh_task
            debug_log("ensure_data_fresh:await_existing_refresh_task")
        else:
            task = asyncio.create_task(poll_once(state))
            state.refresh_task = task
            debug_log("ensure_data_fresh:start_new_refresh_task")

    if task:
        try:
            await task
        finally:
            async with state.lock:
                if state.refresh_task is task and task.done():
                    state.refresh_task = None


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
    started_at = time.monotonic()
    debug_log("warmup_on_start:begin")
    await ensure_data_fresh(state, force=True)
    async with state.lock:
        departures_count = sum(len(items) for items in state.departures_by_widget.values())
        errors_count = sum(len(items) for items in state.errors_by_widget.values())
        has_fetch = state.fetched_at_epoch is not None
    debug_log(
        "warmup_on_start:done "
        f"has_fetch={has_fetch} departures={departures_count} errors={errors_count} "
        f"duration_s={time.monotonic() - started_at:.2f}"
    )


def _format_delay(delay_s: Optional[int]) -> str:
    if delay_s is None:
        return ""
    delay_min = int(round(delay_s / 60))
    sign = "+" if delay_min > 0 else ""
    return f"{sign}{delay_min} min"


def _format_direction_with_platform(direction: str, platform: Optional[str]) -> str:
    direction_text = (direction or "").strip() or "-"
    platform_text = (platform or "").strip()
    if not platform_text:
        return direction_text
    return f"{direction_text} (Gleis: {platform_text})"


def _format_in_label(total_minutes: int) -> str:
    minutes = max(0, int(total_minutes))
    if minutes < 60:
        return f"in {minutes} min"
    hours = minutes // 60
    rest_minutes = minutes % 60
    if rest_minutes == 0:
        return f"in {hours} h"
    return f"in {hours} h {rest_minutes} min"


def _format_fetched_line(fetched_at_epoch: Optional[int], age_s: Optional[int]) -> str:
    if fetched_at_epoch is None or age_s is None:
        return "Feed: keine erfolgreichen Daten"
    fetched_local = to_local_datetime(fetched_at_epoch).strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"Feed: {fetched_local} | Alter: {age_s}s"


def render_widget_html(
    widget: WidgetConfig,
    departures: list[Departure],
    fetched_at_epoch: Optional[int],
    json_url: str,
    errors: Optional[list[str]] = None,
) -> str:
    errors = errors or []
    rows: list[str] = []
    for dep in departures:
        route = html.escape(dep.route or "-")
        in_label = _format_in_label(dep.in_min)
        time_epoch_attr = html.escape(str(dep.time_epoch))
        direction = html.escape(_format_direction_with_platform(dep.direction, dep.platform))
        delay_label = _format_delay(dep.delay_s) if widget.show_delay else ""
        delay_class = "delay positive-delay" if widget.show_delay and dep.delay_s is not None and dep.delay_s > 0 else "delay"
        rows.append(
            (
                "<tr>"
                f"<td>{route}</td>"
                f"<td>{direction}</td>"
                f"<td>{html.escape(dep.time_local)}</td>"
                f"<td class='in-min' data-time-epoch='{time_epoch_attr}'>{html.escape(in_label)}</td>"
                f"<td class='{delay_class}'>{html.escape(delay_label)}</td>"
                "</tr>"
            )
        )

    if not rows and errors:
        error_text = " | ".join(html.escape(error) for error in errors)
        rows.append(f"<tr><td colspan='5'>{error_text}</td></tr>")
    elif not rows:
        rows.append("<tr><td colspan='5'>Keine Abfahrten verfügbar.</td></tr>")

    rows_html = "".join(rows)
    meta_block = ""
    meta_line = ""
    if widget.show_feed_age:
        meta_line = _format_fetched_line(fetched_at_epoch, age_seconds(fetched_at_epoch))
        meta_block = f"<div class='meta' id='feed-meta'>{html.escape(meta_line)}</div>"

    initial_payload = json.dumps(
        {
            "fetched_at": fetched_at_epoch,
            "departures": [dep.to_dict() for dep in departures],
            "errors": errors,
        },
        ensure_ascii=False,
    )
    show_delay_js = "true" if widget.show_delay else "false"
    show_feed_age_js = "true" if widget.show_feed_age else "false"
    json_url_js = json.dumps(json_url, ensure_ascii=False)
    _ = meta_line  # keeps variable explicit for readability

    title = html.escape(widget.title)
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #d1d5db;
      --accent: #0b4f8a;
    }}
    html, body {{
      margin: 0;
      padding: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", Tahoma, sans-serif;
    }}
    .wrap {{
      box-sizing: border-box;
      padding: 12px;
      width: 100%;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .title {{
      font-size: 18px;
      font-weight: 700;
      padding: 10px 12px;
      background: var(--accent);
      color: #ffffff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }}
    th, td {{
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
      text-align: left;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      background: #f9fafb;
    }}
    td.delay, th.delay {{
      text-align: right;
      white-space: nowrap;
    }}
    td.positive-delay {{
      color: #b91c1c;
      font-weight: 700;
    }}
    .meta {{
      font-size: 12px;
      color: var(--muted);
      padding: 8px 10px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="title">{title}</div>
      <table>
        <thead>
          <tr>
            <th>Linie</th>
            <th>Fahrtrichtung</th>
            <th>Zeit</th>
            <th>In</th>
            <th class="delay">Versp.</th>
          </tr>
        </thead>
        <tbody id="departures-body">{rows_html}</tbody>
      </table>
      {meta_block}
    </div>
  </div>
  <script>
    const showDelay = {show_delay_js};
    const showFeedAge = {show_feed_age_js};
    const jsonUrl = {json_url_js};
    let payload = {initial_payload};

    function escapeHtml(value) {{
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function formatDelay(delaySeconds) {{
      if (delaySeconds === null || delaySeconds === undefined) {{
        return "";
      }}
      const delayMinutes = Math.round(Number(delaySeconds) / 60);
      const sign = delayMinutes > 0 ? "+" : "";
      return `${{sign}}${{delayMinutes}} min`;
    }}

    function formatDirection(directionValue, platformValue) {{
      const directionText = String(directionValue || "").trim() || "-";
      const platformText = String(platformValue || "").trim();
      if (!platformText) {{
        return directionText;
      }}
      return `${{directionText}} (Gleis: ${{platformText}})`;
    }}

    function formatInLabel(totalMinutes) {{
      const minutes = Math.max(0, Number(totalMinutes || 0));
      if (minutes < 60) {{
        return `in ${{minutes}} min`;
      }}
      const hours = Math.floor(minutes / 60);
      const restMinutes = minutes % 60;
      if (restMinutes === 0) {{
        return `in ${{hours}} h`;
      }}
      return `in ${{hours}} h ${{restMinutes}} min`;
    }}

    function formatFeedLine() {{
      if (!showFeedAge) {{
        return "";
      }}
      const fetchedAt = payload.fetched_at;
      if (!fetchedAt) {{
        return "Feed: keine erfolgreichen Daten";
      }}
      const fetchedDate = new Date(Number(fetchedAt) * 1000);
      const dateText = fetchedDate.toLocaleString("sv-SE", {{
        timeZone: "Europe/Berlin",
        hour12: false
      }});
      const ageSeconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(fetchedAt)));
      return `Feed: ${{dateText}} | Alter: ${{ageSeconds}}s`;
    }}

    function renderRows() {{
      const body = document.getElementById("departures-body");
      if (!body) {{
        return;
      }}

      const departures = Array.isArray(payload.departures) ? payload.departures : [];
      const errors = Array.isArray(payload.errors) ? payload.errors : [];

      if (departures.length > 0) {{
        body.innerHTML = departures.map((dep) => {{
          const route = dep.route ? escapeHtml(dep.route) : "-";
          const direction = escapeHtml(formatDirection(dep.direction, dep.platform));
          const timeLocal = escapeHtml(dep.time_local || "");
          const timeEpoch = Number(dep.time_epoch || 0);
          const inMin = timeEpoch > 0
            ? Math.max(0, Math.floor((timeEpoch - Date.now() / 1000) / 60))
            : Math.max(0, Number(dep.in_min || 0));
          const inLabel = formatInLabel(inMin);
          const delaySeconds = dep.delay_s === null || dep.delay_s === undefined ? null : Number(dep.delay_s);
          const delay = showDelay ? escapeHtml(formatDelay(delaySeconds)) : "";
          const delayClass = showDelay && delaySeconds !== null && delaySeconds > 0 ? "delay positive-delay" : "delay";
          return `<tr><td>${{route}}</td><td>${{direction}}</td><td>${{timeLocal}}</td><td class="in-min" data-time-epoch="${{timeEpoch}}">${{escapeHtml(inLabel)}}</td><td class="${{delayClass}}">${{delay}}</td></tr>`;
        }}).join("");
        return;
      }}

      if (errors.length > 0) {{
        body.innerHTML = `<tr><td colspan="5">${{escapeHtml(errors.join(" | "))}}</td></tr>`;
        return;
      }}

      body.innerHTML = "<tr><td colspan='5'>Keine Abfahrten verfügbar.</td></tr>";
    }}

    function renderMeta() {{
      if (!showFeedAge) {{
        return;
      }}
      const meta = document.getElementById("feed-meta");
      if (!meta) {{
        return;
      }}
      meta.textContent = formatFeedLine();
    }}

    function updateRelativeTimes() {{
      const cells = document.querySelectorAll("#departures-body td.in-min[data-time-epoch]");
      cells.forEach((cell) => {{
        const timeEpoch = Number(cell.getAttribute("data-time-epoch") || 0);
        if (!timeEpoch) {{
          return;
        }}
        const inMin = Math.max(0, Math.floor((timeEpoch - Date.now() / 1000) / 60));
        cell.textContent = formatInLabel(inMin);
      }});
    }}

    async function refreshData() {{
      try {{
        const response = await fetch(jsonUrl, {{ cache: "no-store" }});
        if (!response.ok) {{
          return;
        }}
        const next = await response.json();
        payload = {{
          fetched_at: next.fetched_at,
          departures: next.departures || [],
          errors: next.errors || []
        }};
        renderRows();
        renderMeta();
      }} catch (_error) {{
        // Keep current payload when refresh fails.
      }}
    }}

    renderRows();
    renderMeta();
    updateRelativeTimes();
    setInterval(refreshData, 30000);
    setInterval(updateRelativeTimes, 1000);
    setInterval(renderMeta, 1000);
  </script>
</body>
</html>
"""


def render_widget_index_html(config: AppConfig, base_url: str) -> str:
    rows: list[str] = []
    root = base_url.rstrip("/")
    for widget in config.widgets:
        widget_url = f"{root}/widget/{widget.id}"
        json_url = f"{root}/json/{widget.id}"
        stop_ids = ", ".join(widget.stop_ids) if widget.stop_ids else "-"
        source_label = widget.source
        if widget.source == "db_iris" and widget.db_eva_no:
            source_label = f"{widget.source} (eva={widget.db_eva_no})"
        rows.append(
            "<tr>"
            f"<td>{html.escape(widget.id)}</td>"
            f"<td>{html.escape(widget.title)}</td>"
            f"<td>{html.escape(source_label)}</td>"
            f"<td><a href='{html.escape(widget_url)}'>{html.escape(widget_url)}</a></td>"
            f"<td><a href='{html.escape(json_url)}'>{html.escape(json_url)}</a></td>"
            f"<td>{html.escape(stop_ids)}</td>"
            "</tr>"
        )

    table_rows = "".join(rows) if rows else "<tr><td colspan='6'>Keine Widgets konfiguriert.</td></tr>"
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Widget-Übersicht</title>
  <style>
    body {{ font-family: "Segoe UI", Tahoma, sans-serif; margin: 16px; background: #f5f7fb; color: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d1d5db; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; font-size: 14px; }}
    th {{ background: #0b4f8a; color: #fff; }}
    a {{ color: #0b4f8a; text-decoration: none; }}
  </style>
</head>
<body>
  <h2>Verfügbare Widgets</h2>
  <p>Direkter Aufruf je Widget-ID: <code>/widget/&lt;id&gt;</code></p>
  <table>
    <thead>
      <tr><th>ID</th><th>Titel</th><th>Quelle</th><th>Widget-URL</th><th>JSON-URL</th><th>Stop-IDs</th></tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</body>
</html>
"""


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
        if WARMUP_STATIC_CACHE_ON_START:
            await run_static_cache_warmup(state)
        if WARMUP_ON_START:
            await run_startup_warmup(state)
        yield

    app = FastAPI(title="timetable-widget", lifespan=lifespan)
    app.state.runtime = state

    @app.get("/widget", response_class=HTMLResponse)
    async def get_widget_overview(request: Request) -> HTMLResponse:
        return HTMLResponse(render_widget_index_html(config, str(request.base_url)))

    @app.get("/widget/{widget_id}", response_class=HTMLResponse)
    async def get_widget(widget_id: str) -> HTMLResponse:
        widget = find_widget(config, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail=f"Widget-ID {widget_id} nicht gefunden.")
        await ensure_data_fresh(state)
        async with state.lock:
            departures = list(state.departures_by_widget.get(widget.id, []))
            fetched_at_epoch = state.fetched_at_epoch
            errors = list(state.errors_by_widget.get(widget.id, []))
        return HTMLResponse(render_widget_html(widget, departures, fetched_at_epoch, f"/json/{widget.id}", errors))

    @app.get("/json/{widget_id}", response_class=JSONResponse)
    async def get_json(widget_id: str) -> JSONResponse:
        widget = find_widget(config, widget_id)
        if widget is None:
            raise HTTPException(status_code=404, detail=f"Widget-ID {widget_id} nicht gefunden.")
        await ensure_data_fresh(state)
        async with state.lock:
            departures = [dep.to_dict() for dep in state.departures_by_widget.get(widget.id, [])]
            fetched_at_epoch = state.fetched_at_epoch
            errors = list(state.errors_by_widget.get(widget.id, []))
        payload = {
            "widget_id": widget.id,
            "widget_title": widget.title,
            "fetched_at": fetched_at_epoch,
            "age_s": age_seconds(fetched_at_epoch),
            "departures": departures,
            "errors": errors,
            "widgets": [{"id": w.id, "title": w.title} for w in config.widgets],
            "config": build_config_excerpt(config),
        }
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
                "age_s": age_seconds(fetched_at_epoch),
                "errors": aggregated_errors,
            }
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
    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)


if __name__ == "__main__":
    main()




















