from __future__ import annotations

import html
import json
from typing import Any, Callable, Optional


TimeFn = Callable[[int], Any]
AgeFn = Callable[[Optional[int]], Optional[int]]


def _format_delay(delay_s: Optional[int]) -> str:
    if delay_s is None:
        return ""
    delay_min = int(round(delay_s / 60))
    sign = "+" if delay_min > 0 else ""
    return f"{sign}{delay_min} min"


def _format_direction_with_platform(direction: str, platform: Optional[str], direction_label: Optional[str] = None) -> str:
    direction_text = (direction or "").strip() or "-"
    custom_label = (direction_label or "").strip()
    platform_text = (platform or "").strip()
    if custom_label:
        direction_text = f"{direction_text} ({custom_label})"
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


def _format_fetched_line(
    fetched_at_epoch: Optional[int],
    age_s: Optional[int],
    show_feed_age: bool,
    app_version: str,
    to_local_datetime_fn: TimeFn,
) -> str:
    version_text = f"Version: {app_version}"
    if not show_feed_age:
        return version_text
    if fetched_at_epoch is None or age_s is None:
        return f"Feed: keine erfolgreichen Daten | {version_text}"
    fetched_local = to_local_datetime_fn(fetched_at_epoch).strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"Feed: {fetched_local} | Alter: {age_s}s | {version_text}"


def render_logs_html(
    base_url: str,
    log_path: str,
    lines: list[str],
    read_error: Optional[str],
    startup_ready: bool,
    startup_error: Optional[str],
    startup_ready_since_epoch: Optional[int],
    *,
    app_version: str,
    log_tail_lines: int,
    to_local_datetime_fn: TimeFn,
) -> str:
    root = base_url.rstrip("/")
    status_text = "Dienst online" if startup_ready else "Dienst wird initialisiert - Details siehe Log"
    status_class = "status-online" if startup_ready else "status-starting"
    if startup_ready and startup_ready_since_epoch is not None:
        since_local = to_local_datetime_fn(startup_ready_since_epoch).strftime("%Y-%m-%d %H:%M:%S %Z")
        status_text = f"{status_text} seit {since_local}"
    if startup_error:
        status_text = f"{status_text} | Startup-Hinweis: {startup_error}"

    content_lines: list[str] = []
    if read_error:
        content_lines.append(read_error)
    if lines:
        content_lines.extend(lines)
    elif not read_error:
        content_lines.append("(Keine Log-Eintraege vorhanden)")
    content_text = "\n".join(content_lines)

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Logs - timetable-widget v{html.escape(app_version)}</title>
  <style>
    body {{ font-family: "Segoe UI", Tahoma, sans-serif; margin: 16px; background: #f5f7fb; color: #1f2937; }}
    h2 {{ margin: 0 0 8px; }}
    .meta {{ margin: 8px 0; color: #374151; }}
    .status {{ margin: 10px 0; padding: 10px 12px; border-radius: 8px; font-weight: 600; }}
    .status-starting {{ background: #fff7ed; border: 1px solid #fdba74; color: #9a3412; }}
    .status-online {{ background: #ecfdf5; border: 1px solid #6ee7b7; color: #065f46; }}
    .log-controls {{ margin: 8px 0 10px; font-size: 13px; color: #374151; }}
    .log-controls label {{ display: inline-flex; align-items: center; gap: 6px; user-select: none; }}
    pre {{ white-space: pre-wrap; background: #111827; color: #e5e7eb; padding: 12px; border-radius: 8px; min-height: 280px; max-height: 70vh; overflow-y: auto; }}
    a {{ color: #0b4f8a; text-decoration: none; }}
  </style>
</head>
<body>
  <h2>Log-Ansicht</h2>
  <div class="meta">Datei: <code>{html.escape(log_path)}</code> | Letzte {log_tail_lines} Eintraege | Version: {html.escape(app_version)}</div>
  <div class="status {status_class}">{html.escape(status_text)} - <a href="{html.escape(f'{root}/')}">Zur Startseite</a></div>
  <div class="log-controls">
    <label><input type="checkbox" id="auto-scroll" checked /> Auto Scroll</label>
  </div>
  <pre id="log-body">{html.escape(content_text)}</pre>
  <script>
    const logUrl = {json.dumps(f"{root}/logs?format=text", ensure_ascii=False)};
    function shouldAutoScroll() {{
      const checkbox = document.getElementById("auto-scroll");
      return !!(checkbox && checkbox.checked);
    }}

    function scrollLogsToBottom(force = false) {{
      const target = document.getElementById("log-body");
      if (!target) {{
        return;
      }}
      if (force || shouldAutoScroll()) {{
        target.scrollTop = target.scrollHeight;
      }}
    }}

    async function refreshLogs() {{
      try {{
        const response = await fetch(logUrl, {{ cache: "no-store" }});
        if (!response.ok) {{
          return;
        }}
        const text = await response.text();
        const target = document.getElementById("log-body");
        if (target) {{
          target.textContent = text;
          scrollLogsToBottom(false);
        }}
      }} catch (_error) {{
        // Keep last visible log content on transient errors.
      }}
    }}

    document.addEventListener("DOMContentLoaded", () => {{
      const checkbox = document.getElementById("auto-scroll");
      if (checkbox) {{
        checkbox.addEventListener("change", () => {{
          if (checkbox.checked) {{
            scrollLogsToBottom(true);
          }}
        }});
      }}
      scrollLogsToBottom(true);
    }});

    setInterval(refreshLogs, 3000);
  </script>
</body>
</html>
"""


def render_widget_html(
    widget: Any,
    departures: list[Any],
    fetched_at_epoch: Optional[int],
    json_url: str,
    errors: Optional[list[str]] = None,
    *,
    app_version: str,
    age_seconds_fn: AgeFn,
    to_local_datetime_fn: TimeFn,
) -> str:
    errors = errors or []
    rows: list[str] = []
    for dep in departures:
        route = html.escape(dep.route or "-")
        in_label = _format_in_label(dep.in_min)
        time_epoch_attr = html.escape(str(dep.time_epoch))
        direction = html.escape(_format_direction_with_platform(dep.direction, dep.platform, dep.direction_label))
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
        rows.append("<tr><td colspan='5'>Keine Abfahrten verfuegbar.</td></tr>")

    rows_html = "".join(rows)
    meta_line = _format_fetched_line(fetched_at_epoch, age_seconds_fn(fetched_at_epoch), widget.show_feed_age, app_version, to_local_datetime_fn)
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
    app_version_js = json.dumps(app_version, ensure_ascii=False)

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
    const appVersion = {app_version_js};
    const jsonUrl = {json_url_js};
    let payload = {initial_payload};

    function escapeHtml(value) {{
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
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

    function formatDirection(directionValue, platformValue, customLabelValue) {{
      let directionText = String(directionValue || "").trim() || "-";
      const customLabelText = String(customLabelValue || "").trim();
      const platformText = String(platformValue || "").trim();
      if (customLabelText) {{
        directionText = `${{directionText}} (${{customLabelText}})`;
      }}
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
      const versionText = `Version: ${{appVersion}}`;
      if (!showFeedAge) {{
        return versionText;
      }}
      const fetchedAt = payload.fetched_at;
      if (!fetchedAt) {{
        return `Feed: keine erfolgreichen Daten | ${{versionText}}`;
      }}
      const fetchedDate = new Date(Number(fetchedAt) * 1000);
      const dateText = fetchedDate.toLocaleString("sv-SE", {{
        timeZone: "Europe/Berlin",
        hour12: false
      }});
      const ageSeconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(fetchedAt)));
      return `Feed: ${{dateText}} | Alter: ${{ageSeconds}}s | ${{versionText}}`;
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
          const direction = escapeHtml(formatDirection(dep.direction, dep.platform, dep.direction_label));
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

      body.innerHTML = "<tr><td colspan='5'>Keine Abfahrten verfuegbar.</td></tr>";
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


def render_widget_index_html(config: Any, base_url: str, *, app_version: str) -> str:
    rows: list[str] = []
    root = base_url.rstrip("/")
    for widget in config.widgets:
        widget_url = f"{root}/widget/{widget.id}"
        widget_24h_url = f"{root}/widget/{widget.id}/24h"
        json_url = f"{root}/json/{widget.id}"
        json_24h_url = f"{root}/json/{widget.id}/24h"
        stop_ids = ", ".join(widget.stop_ids) if widget.stop_ids else "-"
        source_label = widget.source
        if widget.source == "db_iris" and widget.db_eva_no:
            source_label = f"{widget.source} (eva={widget.db_eva_no})"
        rows.append(
            "<tr>"
            f"<td>{html.escape(widget.id)}</td>"
            f"<td>{html.escape(widget.title)}</td>"
            f"<td>{html.escape(source_label)}</td>"
            f"<td><a href='{html.escape(widget_url)}'>{html.escape(widget_url)}</a><br/><a href='{html.escape(widget_24h_url)}'>{html.escape(widget_24h_url)}</a></td>"
            f"<td><a href='{html.escape(json_url)}'>{html.escape(json_url)}</a><br/><a href='{html.escape(json_24h_url)}'>{html.escape(json_24h_url)}</a></td>"
            f"<td>{html.escape(stop_ids)}</td>"
            "</tr>"
        )
    table_rows = "".join(rows) if rows else "<tr><td colspan='6'>Keine Widgets konfiguriert.</td></tr>"
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Widget-Uebersicht - v{html.escape(app_version)}</title>
  <style>
    body {{ font-family: "Segoe UI", Tahoma, sans-serif; margin: 16px; background: #f5f7fb; color: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d1d5db; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; font-size: 14px; }}
    th {{ background: #0b4f8a; color: #fff; }}
    a {{ color: #0b4f8a; text-decoration: none; }}
  </style>
</head>
<body>
  <h2>Verfuegbare Widgets</h2>
  <p>Direkter Aufruf je Widget-ID: <code>/widget/&lt;id&gt;</code> | 24h-Ansicht: <code>/widget/&lt;id&gt;/24h</code></p>
  <p><strong>Version:</strong> {html.escape(app_version)}</p>
  <table>
    <thead>
      <tr><th>ID</th><th>Titel</th><th>Quelle</th><th>Widget-URL</th><th>JSON-URL</th><th>Stop-IDs</th></tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</body>
</html>
"""


def render_service_index_html(
    config: Any,
    base_url: str,
    startup_ready: bool,
    startup_error: Optional[str],
    startup_ready_since_epoch: Optional[int],
    *,
    app_version: str,
    log_tail_lines: int,
    to_local_datetime_fn: TimeFn,
) -> str:
    root = base_url.rstrip("/")
    status_text = "Dienst online" if startup_ready else "Dienst wird initialisiert - Details siehe Log"
    status_class = "status-online" if startup_ready else "status-starting"
    if startup_ready and startup_ready_since_epoch is not None:
        since_local = to_local_datetime_fn(startup_ready_since_epoch).strftime("%Y-%m-%d %H:%M:%S %Z")
        status_text = f"{status_text} seit {since_local}"
    if startup_error:
        status_text = f"{status_text} | Startup-Hinweis: {startup_error}"

    endpoint_rows = [
        ("Widget-Uebersicht", f"{root}/widget", "Alle konfigurierten Widgets mit Direkt-URLs"),
        ("Widget (Standard)", f"{root}/widget/<id>", "Naechste Abfahrten je Widget-ID"),
        ("Widget (24h)", f"{root}/widget/<id>/24h", "Alle Abfahrten der naechsten 24 Stunden"),
        ("JSON (Standard)", f"{root}/json/<id>", "JSON-Daten fuer Standardansicht"),
        ("JSON (24h)", f"{root}/json/<id>/24h", "JSON-Daten fuer 24h-Ansicht"),
        ("Health", f"{root}/health", "Technischer Status und Feed-Alter"),
        ("Debug-Status", f"{root}/debug", "Aktueller Debug-Modus"),
        ("Logs", f"{root}/logs", f"Letzte {log_tail_lines} Log-Eintraege (Live-Ansicht)"),
        ("OpenAPI", f"{root}/docs", "Interaktive API-Dokumentation"),
    ]

    widget_links = "".join(
        f"<li><a href='{html.escape(f'{root}/widget/{widget.id}')}'>{html.escape(widget.title)}"
        f" (ID {html.escape(widget.id)})</a></li>"
        for widget in config.widgets
    )
    if not widget_links:
        widget_links = "<li>Keine Widgets konfiguriert.</li>"

    example_widget_id = config.widgets[0].id if config.widgets else "1"
    endpoint_html = "".join(
        "<tr>"
        f"<td>{html.escape(name)}</td>"
        f"<td><a href='{html.escape(url.replace('<id>', example_widget_id))}'><code>{html.escape(url)}</code></a></td>"
        f"<td>{html.escape(description)}</td>"
        "</tr>"
        for name, url, description in endpoint_rows
    )

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>timetable-widget API - v{html.escape(app_version)}</title>
  <style>
    body {{ font-family: "Segoe UI", Tahoma, sans-serif; margin: 16px; background: #f5f7fb; color: #1f2937; }}
    h2 {{ margin: 0 0 10px; }}
    .hint {{ margin-bottom: 12px; color: #374151; }}
    .status {{ margin: 10px 0; padding: 10px 12px; border-radius: 8px; font-weight: 600; }}
    .status-starting {{ background: #fff7ed; border: 1px solid #fdba74; color: #9a3412; }}
    .status-online {{ background: #ecfdf5; border: 1px solid #6ee7b7; color: #065f46; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d1d5db; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e5e7eb; text-align: left; font-size: 14px; vertical-align: top; }}
    th {{ background: #0b4f8a; color: #fff; }}
    code {{ background: #eef2ff; padding: 1px 4px; border-radius: 4px; }}
    a {{ color: #0b4f8a; text-decoration: none; }}
  </style>
</head>
<body>
  <h2>timetable-widget</h2>
  <p class="hint">Technische Startseite. Fuer die Widget-Ansicht direkt <a href="{html.escape(f'{root}/widget')}"><code>/widget</code></a> aufrufen.</p>
  <p><strong>Version:</strong> {html.escape(app_version)}</p>
  <div id="service-status" class="status {status_class}">{html.escape(status_text)} - <a href="{html.escape(f'{root}/logs')}">Log anzeigen</a></div>
  <table>
    <thead>
      <tr><th>Endpunkt</th><th>URL</th><th>Beschreibung</th></tr>
    </thead>
    <tbody>{endpoint_html}</tbody>
  </table>
  <h3>Konfigurierte Widgets</h3>
  <ul>{widget_links}</ul>
  <script>
    const serviceStatusUrl = {json.dumps(f"{root}/logs?format=json", ensure_ascii=False)};
    function setServiceStatus(ready, startupError, startupReadySinceEpoch) {{
      const target = document.getElementById("service-status");
      if (!target) {{
        return;
      }}
      const logLink = `<a href="{html.escape(f'{root}/logs')}">Log anzeigen</a>`;
      let text = ready ? "Dienst online" : "Dienst wird initialisiert - Details siehe Log";
      if (ready && startupReadySinceEpoch) {{
        const sinceDate = new Date(Number(startupReadySinceEpoch) * 1000);
        if (!Number.isNaN(sinceDate.getTime())) {{
          text = `${{text}} seit ${{sinceDate.toLocaleString("de-DE")}}`;
        }}
      }}
      if (startupError) {{
        text = `${{text}} | Startup-Hinweis: ${{startupError}}`;
      }}
      target.className = ready ? "status status-online" : "status status-starting";
      target.innerHTML = `${{text}} - ${{logLink}}`;
    }}
    async function refreshServiceStatus() {{
      try {{
        const response = await fetch(serviceStatusUrl, {{ cache: "no-store" }});
        if (!response.ok) {{
          return;
        }}
        const payload = await response.json();
        setServiceStatus(Boolean(payload.startup_ready), payload.startup_error || "", payload.startup_ready_since_epoch || 0);
      }} catch (_error) {{
        // Keep previous status on transient errors.
      }}
    }}
    setInterval(refreshServiceStatus, 5000);
  </script>
</body>
</html>
"""

