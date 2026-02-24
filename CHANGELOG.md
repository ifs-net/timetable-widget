# Changelog

Alle relevanten Änderungen dieses Projekts werden hier dokumentiert.

## [1.3.4] - 2026-02-24

### Ge?ndert
- Statischer GTFS-Fallback wird bei Bedarf asynchron im Hintergrund aktualisiert, damit Requests bei der Initialisierung nicht unn?tig blockieren.
- Polling-Enrichment verarbeitet nur noch fehlende Trip-IDs statt pauschal aller relevanten Trips.
- 24h-Ansicht und GTFS-Widget melden klar, wenn der statische Fahrplan-Fallback noch initialisiert wird.
- Versp?tungsanzeige vereinheitlicht: > 0 Minuten rot, 0 Minuten neutral, < 0 Minuten gr?n (inklusive konsistenter Rundung).
- Mehrere fehlerhafte Mojibake-Texte im Python-Code korrigiert.

## [1.3.3] - 2026-02-24

### Hinzugefuegt
- Neuer API-Endpunkt `/version` mit Build-Metadaten (`app_version`, `app_git_sha`, `app_build_date`).
- Neues Release-Skript `scripts/release_push_and_verify.ps1` fuer Build, Push und Digest-Pruefung.

### Geaendert
- `/health` und `/logs` liefern jetzt ebenfalls Build-Metadaten (`app_git_sha`, `app_build_date`).
- Dockerfile um Build-Argumente und Umgebungsvariablen fuer `APP_GIT_SHA` und `APP_BUILD_DATE` erweitert.
- OCI-Labels im Dockerfile um `org.opencontainers.image.revision` und `org.opencontainers.image.created` erweitert.
- README um `/version` sowie den verifizierten Release-Workflow ergaenzt.

## [1.3.2] - 2026-02-24

### Hinzugefügt
- Neuer Endpunkt `/switchdebugmode` mit GUI (Dropdown + Anwenden) zum Umschalten des Debug-Modus.

### Geändert
- Startseite (`/`) um Link zum Debug-Umschalter ergänzt.
- README um Hinweise zu `/switchdebugmode` sowie zum erwartbaren Kaltstart-Verhalten beim statischen GTFS-Fallback ergänzt.

## [1.3.1] - 2026-02-24

### Geändert
- docker-compose.yml auf registry-basierten Betrieb umgestellt (image: ifsnet/timetable-widget:latest statt lokalem build).
- Release-/Synology-Hinweise präzisiert: Update-Erkennung erfolgt digest-basiert über Docker Hub.

## [1.3.0] - 2026-02-24

### Geändert
- Provider-Logik aufgeteilt: `gtfs_rt` und `db_timetables` in eigene Module ausgelagert.
- Service-/Polling-Logik in separates Modul ausgelagert.
- `/logs`-Ansicht verbessert: Auto-Scroll standardmäßig aktiv, per Checkbox deaktivierbar.
- README ergänzt: klare Hinweise zu direction_overrides.txt (echte Datei erforderlich) und Log-Auto-Scroll.

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
