from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from google.transit import gtfs_realtime_pb2


@dataclass
class PollingDeps:
    debug_log: Callable[[str], None]
    app_log: Callable[[str], None]
    refresh_direction_mapping_if_due: Callable[[Any], Awaitable[None]]
    reload_mapping_if_due: Callable[[Any], Awaitable[None]]
    fetch_feed_bytes: Callable[[str, int], Awaitable[bytes]]
    all_widget_stop_ids: Callable[..., list[str]]
    refresh_known_stop_ids_if_due: Callable[[Any], Awaitable[None]]
    collect_realtime_trip_context: Callable[[Any, set[str]], tuple[set[str], dict[str, str]]]
    load_trip_maps_for_trip_ids_from_static_gtfs: Callable[[set[str], dict[str, str], int], tuple[dict[str, str], dict[str, str], Optional[str]]]
    persist_trip_maps_to_csv: Callable[[str, dict[str, str], dict[str, str]], None]
    refresh_static_fallback_index_if_due: Callable[[Any], Awaitable[None]]
    extract_departures: Callable[[Any, Any, dict[str, str], dict[str, str], int], list[Any]]
    extract_static_schedule_departures: Callable[[Any, Any, int], list[Any]]
    merge_departures_realtime_with_fallback: Callable[[list[Any], list[Any], int], list[Any]]
    apply_direction_labels: Callable[[list[Any], dict[str, str], list[tuple[str, str, str]]], dict[tuple[str, str], tuple[str, str]]]
    register_observed_direction_entries: Callable[[Any, dict[tuple[str, str], tuple[str, str]]], Awaitable[None]]
    fetch_db_iris_departures: Callable[[Any, int, int], Awaitable[list[Any]]]


async def poll_once(state: Any, deps: PollingDeps) -> None:
    started_at = time.monotonic()
    cpu_started_at = time.process_time()
    deps.debug_log("poll_once:started")
    if not state.config.widgets:
        async with state.lock:
            state.errors_by_widget = {}
            state.departures_by_widget = {}
            state.extended_departures_cache = {}
            state.next_refresh_due_monotonic = time.monotonic() + state.config.feed.refresh_seconds
        deps.debug_log("poll_once:widgets_empty")
        deps.app_log("perf:poll_once widgets=0 wall_s=0.00 cpu_s=0.00")
        return

    async with state.lock:
        had_cached_data = state.fetched_at_epoch is not None

    now_epoch = int(time.time())
    gtfs_widgets = [widget for widget in state.config.widgets if widget.source == "gtfs_rt"]
    db_widgets = [widget for widget in state.config.widgets if widget.source == "db_iris"]

    await deps.refresh_direction_mapping_if_due(state)
    async with state.lock:
        direction_labels = dict(state.direction_labels)
        direction_label_patterns = list(state.direction_label_patterns)

    observed_direction_entries: dict[tuple[str, str], tuple[str, str]] = {}

    route_map: dict[str, str] = {}
    trip_destination_map: dict[str, str] = {}
    mapping_error: Optional[str] = None
    if gtfs_widgets:
        mapping_stage_started = time.monotonic()
        await deps.reload_mapping_if_due(state)
        async with state.lock:
            route_map = dict(state.route_map)
            trip_destination_map = dict(state.trip_destination_map)
            mapping_error = state.mapping_error
        deps.debug_log(
            "poll_once:mapping_ready "
            f"routes={len(route_map)} destinations={len(trip_destination_map)} "
            f"has_error={bool(mapping_error)} duration_s={time.monotonic() - mapping_stage_started:.2f}"
        )

    departures_by_widget: dict[str, list[Any]] = {widget.id: [] for widget in state.config.widgets}
    errors_by_widget: dict[str, list[str]] = {widget.id: [] for widget in state.config.widgets}
    total_departures = 0

    if gtfs_widgets:
        try:
            feed_fetch_started = time.monotonic()
            feed_bytes = await deps.fetch_feed_bytes(
                state.config.feed.url,
                state.config.feed.http_timeout_seconds,
            )
            feed_fetch_elapsed = time.monotonic() - feed_fetch_started

            parse_started = time.monotonic()
            feed_message = gtfs_realtime_pb2.FeedMessage()
            feed_message.ParseFromString(feed_bytes)
            parse_elapsed = time.monotonic() - parse_started

            configured_stop_ids = set(deps.all_widget_stop_ids(state.config, source="gtfs_rt"))
            if had_cached_data:
                await deps.refresh_known_stop_ids_if_due(state)
                async with state.lock:
                    known_stop_ids = set(state.known_stop_ids)
                    known_stop_ids_error = state.known_stop_ids_error
            else:
                known_stop_ids = set()
                known_stop_ids_error = None
                deps.debug_log("poll_once:skip_known_stop_validation_cold_start")

            deps.debug_log(
                "poll_once:gtfs_feed_ready "
                f"bytes={len(feed_bytes)} entities={len(feed_message.entity)} known_stop_ids={len(known_stop_ids)} "
                f"fetch_s={feed_fetch_elapsed:.2f} parse_s={parse_elapsed:.2f}"
            )

            enrich_started = time.monotonic()
            relevant_trip_ids, relevant_last_stops = deps.collect_realtime_trip_context(feed_message, configured_stop_ids)
            missing_route_trip_ids = {trip_id for trip_id in relevant_trip_ids if trip_id not in route_map}
            missing_destination_trip_ids = {trip_id for trip_id in relevant_trip_ids if trip_id not in trip_destination_map}
            cold_start_can_skip_enrich = (not had_cached_data) and (bool(route_map) or bool(trip_destination_map))
            if missing_route_trip_ids or missing_destination_trip_ids:
                if cold_start_can_skip_enrich:
                    deps.debug_log(
                        "poll_once:mapping_enrich_skipped_cold_start "
                        f"relevant_trips={len(relevant_trip_ids)} missing_routes={len(missing_route_trip_ids)} "
                        f"missing_destinations={len(missing_destination_trip_ids)}"
                    )
                else:
                    enrich_route_map, enrich_destination_map, enrich_error = await asyncio.to_thread(
                        deps.load_trip_maps_for_trip_ids_from_static_gtfs,
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
                            deps.persist_trip_maps_to_csv(
                                state.config.mapping.trip_route_map_csv,
                                route_map,
                                trip_destination_map,
                            )
                            deps.debug_log(
                                "poll_once:mapping_persisted "
                                f"path={state.config.mapping.trip_route_map_csv} routes={len(route_map)} "
                                f"destinations={len(trip_destination_map)}"
                            )
                        except Exception as exc:
                            deps.debug_log(f"poll_once:mapping_persist_failed error={exc}")
                        async with state.lock:
                            state.route_map = dict(route_map)
                            state.trip_destination_map = dict(trip_destination_map)

                    if enrich_error:
                        mapping_error = f"{mapping_error} | {enrich_error}" if mapping_error else enrich_error

                    deps.debug_log(
                        "poll_once:mapping_enriched "
                        f"relevant_trips={len(relevant_trip_ids)} missing_routes={len(missing_route_trip_ids)} "
                        f"missing_destinations={len(missing_destination_trip_ids)} added_routes={added_routes} "
                        f"added_destinations={added_destinations} duration_s={time.monotonic() - enrich_started:.2f}"
                    )
            else:
                deps.debug_log(
                    "poll_once:mapping_enriched "
                    f"relevant_trips={len(relevant_trip_ids)} missing_routes=0 missing_destinations=0 "
                    f"duration_s={time.monotonic() - enrich_started:.2f}"
                )

            static_fallback_index: Optional[Any] = None
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

                departures = deps.extract_departures(feed_message, widget, route_map, trip_destination_map, now_epoch)
                realtime_count = len(departures)

                if realtime_count < widget.max_departures and widget.stop_ids:
                    if (not had_cached_data) and realtime_count > 0:
                        deps.debug_log(
                            "poll_once:gtfs_fallback_skipped_cold_start "
                            f"widget={widget.id} realtime={realtime_count} max={widget.max_departures}"
                        )
                    else:
                        if not static_fallback_loaded:
                            await deps.refresh_static_fallback_index_if_due(state)
                            async with state.lock:
                                static_fallback_index = state.static_fallback_index
                                static_fallback_error = state.static_fallback_error
                            static_fallback_loaded = True

                        if static_fallback_index is not None:
                            fallback_departures = deps.extract_static_schedule_departures(widget, static_fallback_index, now_epoch)
                            departures = deps.merge_departures_realtime_with_fallback(
                                departures,
                                fallback_departures,
                                widget.max_departures,
                            )
                            deps.debug_log(
                                "poll_once:gtfs_fallback "
                                f"widget={widget.id} realtime={realtime_count} fallback_candidates={len(fallback_departures)} "
                                f"merged={len(departures)}"
                            )
                        elif static_fallback_error and realtime_count == 0:
                            widget_errors.append(f"Statischer Fahrplan-Fallback nicht verfügbar: {static_fallback_error}")

                observed_direction_entries.update(
                    deps.apply_direction_labels(departures, direction_labels, direction_label_patterns)
                )
                departures_by_widget[widget.id] = departures
                total_departures += len(departures)
                deps.debug_log(
                    "poll_once:gtfs_widget_done "
                    f"widget={widget.id} departures={len(departures)} errors={len(widget_errors)} "
                    f"duration_s={time.monotonic() - widget_started:.2f}"
                )
        except Exception as exc:
            for widget in gtfs_widgets:
                errors_by_widget[widget.id].append(f"GTFS feed fetch failed: {exc}")
            deps.debug_log(f"poll_once:gtfs_fetch_error error={exc}")

    for widget in db_widgets:
        widget_started = time.monotonic()
        if not widget.db_eva_no:
            errors_by_widget[widget.id].append(f"Widget {widget.id}: db_eva_no fehlt.")
            continue
        try:
            departures = await deps.fetch_db_iris_departures(
                widget,
                state.config.feed.http_timeout_seconds,
                now_epoch,
            )
            observed_direction_entries.update(
                deps.apply_direction_labels(departures, direction_labels, direction_label_patterns)
            )
            departures_by_widget[widget.id] = departures
            total_departures += len(departures)
            deps.debug_log(
                "poll_once:db_widget_done "
                f"widget={widget.id} departures={len(departures)} duration_s={time.monotonic() - widget_started:.2f}"
            )
        except Exception as exc:
            errors_by_widget[widget.id].append(f"DB-IRIS Abruf fehlgeschlagen: {exc}")
            deps.debug_log(f"poll_once:db_iris_error widget={widget.id} error={exc}")

    await deps.register_observed_direction_entries(state, observed_direction_entries)

    async with state.lock:
        state.departures_by_widget = departures_by_widget
        state.fetched_at_epoch = now_epoch
        state.errors_by_widget = errors_by_widget
        state.extended_departures_cache = {}
        state.next_refresh_due_monotonic = time.monotonic() + state.config.feed.refresh_seconds
    deps.app_log(
        "perf:poll_once "
        f"widgets={len(state.config.widgets)} gtfs={len(gtfs_widgets)} db={len(db_widgets)} "
        f"departures={total_departures} had_cache={had_cached_data} "
        f"wall_s={time.monotonic() - started_at:.2f} cpu_s={time.process_time() - cpu_started_at:.2f}"
    )
    deps.debug_log(
        "poll_once:ok "
        f"widgets={len(state.config.widgets)} gtfs={len(gtfs_widgets)} db={len(db_widgets)} "
        f"departures={total_departures} duration_s={time.monotonic() - started_at:.2f}"
    )


async def ensure_data_fresh(state: Any, deps: PollingDeps, force: bool = False) -> None:
    task: Optional[asyncio.Task] = None
    should_wait = False

    async with state.lock:
        now_monotonic = time.monotonic()
        has_cached_data = state.fetched_at_epoch is not None
        is_stale = not has_cached_data or now_monotonic >= state.next_refresh_due_monotonic

        if state.refresh_task and state.refresh_task.done():
            state.refresh_task = None

        if not force and not is_stale:
            deps.debug_log("ensure_data_fresh:cache_hit")
            return

        if state.refresh_task and not state.refresh_task.done():
            task = state.refresh_task
            should_wait = force or not has_cached_data
            if should_wait:
                deps.debug_log("ensure_data_fresh:await_existing_refresh_task")
            else:
                deps.debug_log("ensure_data_fresh:serve_stale_while_refreshing")
        else:
            task = asyncio.create_task(poll_once(state, deps))
            state.refresh_task = task
            should_wait = force or not has_cached_data
            if should_wait:
                deps.debug_log("ensure_data_fresh:start_new_refresh_task_wait")
            else:
                deps.debug_log("ensure_data_fresh:start_new_refresh_task_background")

    if task and should_wait:
        try:
            await task
        finally:
            async with state.lock:
                if state.refresh_task is task and task.done():
                    state.refresh_task = None


async def run_startup_warmup(state: Any, deps: PollingDeps) -> None:
    started_at = time.monotonic()
    deps.debug_log("warmup_on_start:begin")
    await ensure_data_fresh(state, deps, force=True)
    async with state.lock:
        departures_count = sum(len(items) for items in state.departures_by_widget.values())
        errors_count = sum(len(items) for items in state.errors_by_widget.values())
        has_fetch = state.fetched_at_epoch is not None
    deps.debug_log(
        "warmup_on_start:done "
        f"has_fetch={has_fetch} departures={departures_count} errors={errors_count} "
        f"duration_s={time.monotonic() - started_at:.2f}"
    )


