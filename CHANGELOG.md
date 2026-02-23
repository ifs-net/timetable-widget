# Changelog

Alle relevanten Änderungen dieses Projekts werden hier dokumentiert.

## [1.1.0] - 2026-02-23

### Hinzugefügt
- Richtungs-Overrides aus Datei (`config/direction_overrides.txt`) mit automatischer Erkennung neuer Einträge.
- Konfigurationsstruktur unter `config/` mit Vorlagen für `config.yaml` und Richtungs-Overrides.
- Zusätzliche Umgebungsvariablen: `DIRECTION_MAPPING_SEPARATOR` und `LOG_INSTANCE_IP`.

### Geändert
- Docker-Setup auf gemeinsamen Config-Mount (`/config`) umgestellt.
- Doku auf aktuelle Funktionen erweitert (24h-Ansicht, Mapping, Synology-Setup, Debug-Optionen).
- Beispiele für Richtungs-Overrides aktualisiert (`VMG`, `Stadt/Goethe`).

### Behoben
- Doppelte GTFS-Einträge aus Echtzeit + statischem Fallback stärker dedupliziert (Echtzeit hat Vorrang, auch bei Zeitabweichungen).
- Umlaute und Zeichenkodierung in der Dokumentation bereinigt.

## [1.0.0] - 2026-02-21

### Initiales Release
- Erstveröffentlichung von `timetable-widget`.
- Mehrere Widgets über `widgets`-Konfiguration.
- GTFS-Realtime- und DB-Timetables-Integration.
- HTML-Widget und JSON-Endpunkte pro Widget-ID.
- On-Demand-Refresh mit Caching, Warmup-Optionen und Debug-Logging.
- Statischer GTFS-Fallback inklusive Fahrtrichtungsermittlung.
