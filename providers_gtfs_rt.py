from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx


_STATIC_ARCHIVE_LOCK = threading.Lock()
_STATIC_ARCHIVE_MEMORY_CACHE: Optional[tuple[float, bytes]] = None


def load_static_gtfs_archive_bytes(
    timeout_seconds: int,
    *,
    static_url: str,
    cache_path: str,
    cache_max_age_seconds: int,
    user_agent: str,
    app_log_fn: Callable[[str], None],
    debug_log_fn: Callable[[str], None],
) -> tuple[Optional[bytes], Optional[str]]:
    global _STATIC_ARCHIVE_MEMORY_CACHE

    target_path = Path(cache_path)

    with _STATIC_ARCHIVE_LOCK:
        now_epoch = time.time()

        if target_path.is_file():
            try:
                age_s = max(0, int(now_epoch - target_path.stat().st_mtime))
            except OSError:
                age_s = cache_max_age_seconds + 1
            if age_s <= cache_max_age_seconds:
                try:
                    payload = target_path.read_bytes()
                    _STATIC_ARCHIVE_MEMORY_CACHE = (now_epoch, payload)
                    debug_log_fn(
                        f"mapping_static:cache_hit path={target_path} bytes={len(payload)} age_s={age_s}"
                    )
                    return payload, None
                except Exception as exc:
                    debug_log_fn(f"mapping_static:cache_read_failed path={target_path} error={exc}")

        if _STATIC_ARCHIVE_MEMORY_CACHE is not None:
            cached_at, payload = _STATIC_ARCHIVE_MEMORY_CACHE
            age_s = max(0, int(now_epoch - cached_at))
            if age_s <= cache_max_age_seconds and payload:
                debug_log_fn(
                    f"mapping_static:memory_cache_hit bytes={len(payload)} age_s={age_s}"
                )
                return payload, None

        download_started = time.monotonic()
        app_log_fn(
            f"external_fetch:start source=gtfs_static purpose=static_archive url={static_url} timeout_s={timeout_seconds}"
        )
        try:
            response = httpx.get(static_url, timeout=timeout_seconds, headers={"User-Agent": user_agent})
            response.raise_for_status()
            payload = response.content
            _STATIC_ARCHIVE_MEMORY_CACHE = (time.time(), payload)
            app_log_fn(
                "external_fetch:ok "
                f"source=gtfs_static purpose=static_archive status={response.status_code} bytes={len(payload)} duration_s={time.monotonic() - download_started:.2f}"
            )
            debug_log_fn(
                "mapping_static:download_ok "
                f"url={static_url} bytes={len(payload)} duration_s={time.monotonic() - download_started:.2f}"
            )
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(payload)
                debug_log_fn(f"mapping_static:cache_write_ok path={target_path} bytes={len(payload)}")
            except Exception as exc:
                msg = f"mapping_static:cache_write_failed path={target_path} error={exc}"
                debug_log_fn(msg)
                app_log_fn(msg)
            return payload, None
        except Exception as exc:
            app_log_fn(f"external_fetch:error source=gtfs_static purpose=static_archive error={exc}")
            debug_log_fn(f"mapping_static:download_failed error={exc}")
            if target_path.is_file():
                try:
                    payload = target_path.read_bytes()
                    _STATIC_ARCHIVE_MEMORY_CACHE = (time.time(), payload)
                    debug_log_fn(
                        f"mapping_static:stale_cache_used path={target_path} bytes={len(payload)}"
                    )
                    return payload, None
                except Exception as cache_exc:
                    debug_log_fn(f"mapping_static:stale_cache_read_failed path={target_path} error={cache_exc}")
            if _STATIC_ARCHIVE_MEMORY_CACHE is not None:
                _cached_at, payload = _STATIC_ARCHIVE_MEMORY_CACHE
                if payload:
                    debug_log_fn(
                        f"mapping_static:memory_cache_stale_used bytes={len(payload)}"
                    )
                    return payload, None
            return None, f"mapping static download failed: {exc}"


async def fetch_feed_bytes(
    url: str,
    timeout_seconds: int,
    *,
    user_agent: str,
    app_log_fn: Callable[[str], None],
) -> bytes:
    if not url:
        raise ValueError("feed.url is empty")
    headers = {"User-Agent": user_agent}
    async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
        started_at = time.monotonic()
        app_log_fn(f"external_fetch:start source=gtfs_rt url={url} timeout_s={timeout_seconds}")
        try:
            response = await client.get(url)
            response.raise_for_status()
        except Exception as exc:
            app_log_fn(f"external_fetch:error source=gtfs_rt url={url} error={exc}")
            raise
        app_log_fn(
            "external_fetch:ok "
            f"source=gtfs_rt url={url} status={response.status_code} bytes={len(response.content)} duration_s={time.monotonic() - started_at:.2f}"
        )
        return response.content
