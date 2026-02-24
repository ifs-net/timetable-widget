# Changelog

Alle relevanten Änderungen dieses Projekts werden hier dokumentiert.

## [1.3.0] - 2026-02-24

### Geändert
- Provider-Logik aufgeteilt: `gtfs_rt` und `db_timetables` in eigene Module ausgelagert.
- Service-/Polling-Logik in separates Modul ausgelagert.
- `/logs`-Ansicht verbessert: Auto-Scroll standardmäßig aktiv, per Checkbox deaktivierbar.

### Performance
- Kaltstart-Pfad optimiert, damit erste Antworten schneller verfügbar sind.
- CPU-Spitzen reduziert durch gezieltere Verarbeitung und Entkopplung schwerer Schritte vom ersten Request.
- Zusätzliche `perf:*`-Logausgaben ergänzt, um Wall-/CPU-Zeiten nachvollziehbar zu messen.

### Behoben
- Stabilere Wiederverwendung von bereits geladenen Mapping-Daten im Arbeitsspeicher.
- Bessere Sichtbarkeit von Schreibproblemen bei Cache-/Mapping-Dateien im Basis-Log.

## [1.2.1] - 2026-02-24

### Geändert
- Release-Pipeline für Synology-Update-Erkennung abgesichert: Docker-Buildx-Push ohne Attestations.
- Docker-Hub-Push für `latest` und Versionstag erfolgt mit `--provenance=false` und `--sbom=false`.

### Behoben
- DB-API-Credentials werden für DB-IRIS zusätzlich direkt aus `/config/.dbapikey` (oder `DB_APIKEY_FILE`) geladen.
- Synology-Container ohne `env_file` können Bahnhof-Widgets damit wieder ohne "DB API credentials fehlen" ausliefern.

## [1.2.0] - 2026-02-24

### Geändert
- DB-Credentials werden jetzt aus `config/.dbapikey` geladen (statt Root-`.env`).
- Compose nutzt dafür `env_file` mit `DB_APIKEY_FILE`-Override.
- Debug-Konfiguration wurde in die YAML verschoben (`debug.enabled`, `debug.log_path`).
- Root-Endpunkt `/` bleibt als technische Endpunkt-Übersicht verfügbar.

### Hinzugefügt
- Neue Vorlage `config/.dbapikey.example`.
- Beispielkonfiguration `config/config.yaml.example` um `debug`-Abschnitt erweitert.

### Entfernt
- Root-Datei `.env.example` wurde entfernt.

## [1.1.2] - 2026-02-23

### Geändert
- Root-Endpunkt (`/`) liefert jetzt eine technische Startseite mit Links auf Widget-, JSON-, Health- und Debug-Endpunkte.
- `docker-compose.yml` nutzt standardmäßig projektlokale Mounts (`./config`, `./data`, `./logs`) und kann per `CONFIG_DIR`, `DATA_DIR`, `LOGS_DIR` übersteuert werden.
- `.env.example` um die neuen optionalen Mount-Variablen ergänzt.
- README auf neue Root-URL und Compose-Mount-Defaults aktualisiert.

### Behoben
- Konfigurationsladen fällt jetzt auf `FALLBACK_CONFIG_PATH` zurück, wenn `CONFIG_PATH` auf eine nicht vorhandene Datei zeigt.

## [1.1.1] - 2026-02-23

### Geändert
- Konfigurationsvorlage vereinheitlicht: nur noch `config/config.yaml.example` wird verwendet.
- Dockerfile auf Non-Root-Betrieb (`app`, UID/GID 10001) und OCI-Labels erweitert.
- Docker-Healthcheck für `/health` ergänzt.
- Compose-/ENV-Fallback-Pfade auf `config/config.yaml.example` umgestellt.

### Behoben
- Deduplizierung von Echtzeit- und statischen GTFS-Abfahrten verbessert.

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
