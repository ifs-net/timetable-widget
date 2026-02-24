from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import httpx


@dataclass
class DBIrisEventChange:
    tl: dict[str, str] = field(default_factory=dict)
    ar: dict[str, str] = field(default_factory=dict)
    dp: dict[str, str] = field(default_factory=dict)
    message_types: set[str] = field(default_factory=set)


def build_db_timetables_headers(client_id: str, api_key: str, *, user_agent: str) -> dict[str, str]:
    return {
        "DB-Client-Id": str(client_id or "").strip(),
        "DB-Api-Key": str(api_key or "").strip(),
        "Accept": "application/xml",
        "User-Agent": user_agent,
    }


def parse_db_iris_timestamp(raw_value: str, *, local_timezone) -> Optional[int]:
    text = str(raw_value or "").strip()
    if len(text) != 10:
        return None
    try:
        dt_local = datetime.strptime(text, "%y%m%d%H%M").replace(tzinfo=local_timezone)
    except ValueError:
        return None
    return int(dt_local.timestamp())


def is_db_train_departure(route: str, category: str, train_number: str) -> bool:
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
    widget: Any,
    payload: bytes,
    now_epoch: int,
    *,
    local_timezone,
    to_local_datetime_fn: Callable[[int], datetime],
    match_widget_text_filters_fn: Callable[[Any, str, Optional[list[str]]], bool],
    departure_factory: Callable[..., Any],
    changes_by_event_id: Optional[dict[str, DBIrisEventChange]] = None,
) -> list[Any]:
    departures: list[Any] = []
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

        planned_epoch = parse_db_iris_timestamp(
            departure_attrs.get("pt", "") or arrival_attrs.get("pt", ""),
            local_timezone=local_timezone,
        )
        changed_epoch = parse_db_iris_timestamp(
            departure_attrs.get("ct", "") or arrival_attrs.get("ct", ""),
            local_timezone=local_timezone,
        )
        time_epoch = changed_epoch or planned_epoch
        if time_epoch is None or time_epoch < now_epoch:
            continue

        route = str(departure_attrs.get("l", "") or arrival_attrs.get("l", "")).strip()
        if not route:
            route = str(train_attrs.get("c", "")).strip()
        if not route:
            route = str(train_attrs.get("n", "")).strip()

        if widget.db_only_trains and not is_db_train_departure(
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
        if not match_widget_text_filters_fn(widget, direction, path_stops):
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
            departure_factory(
                route=route,
                direction=direction,
                platform=platform,
                stop_id=str(stop_event.attrib.get("eva", widget.db_eva_no or "")).strip(),
                time_epoch=time_epoch,
                time_local=to_local_datetime_fn(time_epoch).strftime("%H:%M"),
                in_min=in_min,
                delay_s=delay_s,
                trip_id=event_id,
            )
        )

    departures.sort(key=lambda item: item.time_epoch)
    return departures


async def fetch_db_timetables_departures(
    widget: Any,
    timeout_seconds: int,
    now_epoch: int,
    *,
    resolve_db_credentials_fn: Callable[[], tuple[str, str]],
    base_url: str,
    user_agent: str,
    local_timezone,
    app_log_fn: Callable[[str], None],
    debug_log_fn: Callable[[str], None],
    to_local_datetime_fn: Callable[[int], datetime],
    match_widget_text_filters_fn: Callable[[Any, str, Optional[list[str]]], bool],
    departure_factory: Callable[..., Any],
) -> list[Any]:
    started_at = time.monotonic()
    eva_no = str(widget.db_eva_no or "").strip()
    if not eva_no:
        raise ValueError(f"Widget {widget.id}: db_eva_no fehlt.")

    now_local = to_local_datetime_fn(now_epoch).replace(minute=0, second=0, microsecond=0)
    merged: list[Any] = []
    client_id, api_key = resolve_db_credentials_fn()
    if not client_id or not api_key:
        raise ValueError(
            "DB API credentials fehlen. Setze DB_CLIENT_ID und DB_API_KEY als Environment-Variablen "
            "oder lege sie in /config/.dbapikey (bzw. DB_APIKEY_FILE) ab."
        )

    debug_log_fn(
        f"db_iris:fetch_start widget={widget.id} eva={eva_no} lookahead_h={widget.db_lookahead_hours}"
    )
    app_log_fn(
        f"external_fetch:start source=db_timetables widget={widget.id} eva={eva_no} lookahead_h={widget.db_lookahead_hours}"
    )

    headers = build_db_timetables_headers(client_id, api_key, user_agent=user_agent)
    async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
        changes_by_event_id: dict[str, DBIrisEventChange] = {}
        if widget.db_use_fchg:
            fchg_started = time.monotonic()
            try:
                fchg_url = f"{base_url}/fchg/{eva_no}"
                app_log_fn(
                    f"external_fetch:start source=db_timetables endpoint=fchg widget={widget.id} url={fchg_url}"
                )
                fchg_response = await client.get(fchg_url)
                fchg_response.raise_for_status()
                changes_by_event_id = parse_db_iris_fchg_changes(fchg_response.content)
                debug_log_fn(
                    "db_iris:fchg_ok "
                    f"widget={widget.id} changes={len(changes_by_event_id)} duration_s={time.monotonic() - fchg_started:.2f}"
                )
            except Exception as exc:
                app_log_fn(
                    f"external_fetch:error source=db_timetables endpoint=fchg widget={widget.id} error={exc}"
                )
                debug_log_fn(f"db_iris:fchg_unavailable widget={widget.id} error={exc}")

        requested_plan_requests = 0
        successful_plan_requests = 0
        not_found_plan_requests = 0
        for offset in range(widget.db_lookahead_hours):
            slot = now_local + timedelta(hours=offset)
            date_token = slot.strftime("%y%m%d")
            hour_token = slot.strftime("%H")
            url = f"{base_url}/plan/{eva_no}/{date_token}/{hour_token}"
            slot_started = time.monotonic()
            requested_plan_requests += 1
            try:
                app_log_fn(
                    f"external_fetch:start source=db_timetables endpoint=plan widget={widget.id} slot={date_token}{hour_token}"
                )
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    not_found_plan_requests += 1
                    app_log_fn(
                        f"external_fetch:skip source=db_timetables endpoint=plan widget={widget.id} slot={date_token}{hour_token} status=404"
                    )
                    continue
                raise
            successful_plan_requests += 1
            parsed_slot_departures = parse_db_iris_plan_departures(
                widget,
                response.content,
                now_epoch,
                local_timezone=local_timezone,
                to_local_datetime_fn=to_local_datetime_fn,
                match_widget_text_filters_fn=match_widget_text_filters_fn,
                departure_factory=departure_factory,
                changes_by_event_id=changes_by_event_id,
            )
            merged.extend(parsed_slot_departures)
            debug_log_fn(
                "db_iris:plan_slot_ok "
                f"widget={widget.id} slot={date_token}{hour_token} departures={len(parsed_slot_departures)} "
                f"duration_s={time.monotonic() - slot_started:.2f}"
            )
            if len(merged) >= widget.max_departures:
                break

        if successful_plan_requests == 0:
            raise ValueError(f"DB-IRIS plan returned no available time slices for EVA {eva_no}.")

    merged.sort(key=lambda item: item.time_epoch)
    dedup: dict[tuple[str, int, str], Any] = {}
    for item in merged:
        dedup[(item.trip_id, item.time_epoch, item.route)] = item
    result = list(sorted(dedup.values(), key=lambda item: item.time_epoch))[: widget.max_departures]
    debug_log_fn(
        "db_iris:fetch_done "
        f"widget={widget.id} eva={eva_no} requested={requested_plan_requests} ok={successful_plan_requests} "
        f"not_found={not_found_plan_requests} merged={len(merged)} deduped={len(dedup)} result={len(result)} "
        f"duration_s={time.monotonic() - started_at:.2f}"
    )
    app_log_fn(
        "external_fetch:done "
        f"source=db_timetables widget={widget.id} eva={eva_no} requested={requested_plan_requests} ok={successful_plan_requests} not_found={not_found_plan_requests} result={len(result)} duration_s={time.monotonic() - started_at:.2f}"
    )
    return result
