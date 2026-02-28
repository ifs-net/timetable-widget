# Changelog

Alle relevanten Änderungen dieses Projekts werden hier dokumentiert.

## [1.4.0] - 2026-02-28

### Geändert
- GTFS-Widgets werden jetzt primär über `station_selector` aufgelöst; passende Stop-IDs werden bei Reload der statischen GTFS-Daten automatisch neu ermittelt.
- Falsche oder veraltete GTFS-Stop-IDs werden dadurch zur Laufzeit selbst geheilt, ohne dass ein Container-Neustart nötig ist.
- GTFS-Realtime-Stopps mit `schedule_relationship=SKIPPED` werden nicht mehr fälschlich durch normale statische Fahrten ersetzt.
- Solche übersprungenen Halte werden stattdessen als `entfällt` in HTML und JSON ausgegeben.
- Während eines laufenden Hintergrund-Refreshes bleiben die zuletzt erfolgreichen Daten sichtbar, bis der neue Stand vollständig verarbeitet ist.
- README und Beispielkonfiguration auf die neue GTFS-Konfiguration über `station_selector` aktualisiert.

### Behoben
- Fachlich falsche Fremdhalte aus anderen Städten werden nicht mehr durch manuell veraltete Stop-IDs angezeigt.
- Widget-Ansichten zeigen beim laufenden Aufbau des statischen Fallback-Index keine irreführenden Ersatzfahrten mehr an.
- Mehrere sichtbare Text- und Kodierungsfehler in Widget-UI, Fehlermeldungen und Dokumentation korrigiert.

## [1.3.4] - 2026-02-24

### Geändert
- Statischer GTFS-Fallback wird bei Bedarf asynchron im Hintergrund aktualisiert, damit Requests bei der Initialisierung nicht unnötig blockieren.
- Polling-Enrichment verarbeitet nur noch fehlende Trip-IDs statt pauschal aller relevanten Trips.
- 24h-Ansicht und GTFS-Widget melden klar, wenn der statische Fahrplan-Fallback noch initialisiert wird.
- Verspätungsanzeige vereinheitlicht: > 0 Minuten rot, 0 Minuten neutral, < 0 Minuten grün, inklusive konsistenter Rundung.
- Mehrere fehlerhafte Mojibake-Texte im Python-Code korrigiert.

## [1.3.3] - 2026-02-24

### Hinzugefügt
- Neuer API-Endpunkt `/version` mit Build-Metadaten (`app_version`, `app_git_sha`, `app_build_date`).
- Neues Release-Skript `scripts/release_push_and_verify.ps1` für Build, Push und Digest-Prüfung.

### Geändert
- `/health` und `/logs` liefern jetzt ebenfalls Build-Metadaten (`app_git_sha`, `app_build_date`).
- Dockerfile um Build-Argumente und Umgebungsvariablen für `APP_GIT_SHA` und `APP_BUILD_DATE` erweitert.
- OCI-Labels im Dockerfile um `org.opencontainers.image.revision` und `org.opencontainers.image.created` erweitert.
- README um `/version` sowie den verifizierten Release-Workflow ergänzt.

## [1.3.2] - 2026-02-24

### Hinzugefügt
- Neuer Endpunkt `/switchdebugmode` mit GUI (Dropdown + Anwenden) zum Umschalten des Debug-Modus.

### Geändert
- Startseite (`/`) um Link zum Debug-Umschalter ergänzt.
- README um Hinweise zu `/switchdebugmode` sowie zum erwartbaren Kaltstart-Verhalten beim statischen GTFS-Fallback ergänzt.

## [1.3.1] - 2026-02-24

### Geändert
- `docker-compose.yml` auf registry-basierten Betrieb umgestellt (`image: ifsnet/timetable-widget:latest` statt lokalem `build`).
- Release-/Synology-Hinweise präzisiert: Update-Erkennung erfolgt digest-basiert über Docker Hub.

## [1.3.0] - 2026-02-24

### Geändert
- Provider-Logik aufgeteilt: `gtfs_rt` und `db_timetables` in eigene Module ausgelagert.
- Service-/Polling-Logik in separates Modul ausgelagert.
- `/logs`-Ansicht verbessert: Auto-Scroll standardmäßig aktiv, per Checkbox deaktivierbar.
- README ergänzt: klare Hinweise zu `direction_overrides.txt` und Log-Auto-Scroll.

### Performance
- Kaltstart-Pfad optimiert, damit erste Antworten schneller verfügbar sind.
- CPU-Spitzen reduziert durch gezieltere Verarbeitung und Entkopplung schwerer Schritte vom ersten Request.
- Zusätzliche `perf:*`-Logausgaben ergänzt, um Wall- und CPU-Zeiten nachvollziehbar zu messen.

### Behoben
- Stabilere Wiederverwendung von bereits geladenen Mapping-Daten im Arbeitsspeicher.
- Bessere Sichtbarkeit von Schreibproblemen bei Cache- und Mapping-Dateien im Basis-Log.

## [1.2.1] - 2026-02-24

### Geändert
- Release-Pipeline für Synology-Update-Erkennung abgesichert: Docker-Buildx-Push ohne Attestations.
- Docker-Hub-Push für `latest` und Versions-Tag erfolgt mit `--provenance=false` und `--sbom=false`.

### Behoben
- DB-API-Credentials werden für DB-IRIS zusätzlich direkt aus `/config/.dbapikey` (oder `DB_APIKEY_FILE`) geladen.
- Synology-Container ohne `env_file` können Bahnhof-Widgets damit wieder ohne Fehler bei den DB-Credentials ausliefern.

## [1.2.0] - 2026-02-24

### Geändert
- DB-Credentials werden jetzt aus `config/.dbapikey` geladen statt aus einer Root-`.env`.
- Compose nutzt dafür `env_file` mit `DB_APIKEY_FILE`-Override.
- Debug-Konfiguration wurde in die YAML verschoben (`debug.enabled`, `debug.log_path`).
- Root-Endpunkt `/` bleibt als technische Endpunkt-übersicht verfügbar.

### Hinzugefügt
- Neue Vorlage `config/.dbapikey.example`.
- Beispielkonfiguration `config/config.yaml.example` um den Abschnitt `debug` erweitert.

## [1.1.2] - 2026-02-24

### Geändert
- Root-Endpunkt `/` liefert eine klickbare technische übersicht der verfügbaren Endpunkte.
- Docker-/Synology-Dokumentation um Hinweise zu Registry-Pull und Update-Prüfung ergänzt.

## [1.1.1] - 2026-02-24

### Geändert
- Container läuft jetzt als Non-Root-User.
- OCI-Labels und Healthcheck im Dockerfile ergänzt.
- Release-/Publish-Hinweise für Docker Hub konkretisiert.

## [1.1.0] - 2026-02-24

### Hinzugefügt
- Richtungs-Overrides aus Datei (`config/direction_overrides.txt`) mit automatischer Erkennung neuer Einträge.
- 24h-Ansicht für Widgets und JSON-Endpunkte.
- Live-Logansicht und Debug-Steuerung über Web-Endpunkte.

### Geändert
- Doppelte GTFS-Einträge aus Echtzeit und statischem Fallback stärker dedupliziert.
- Dokumentation auf aktuelle Funktionen erweitert, inklusive Synology-Setup.

## [1.0.0] - 2026-02-24

### Hinzugefügt
- Initiales Release.
