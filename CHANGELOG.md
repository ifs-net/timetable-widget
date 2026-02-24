# Changelog

Alle relevanten Ã„nderungen dieses Projekts werden hier dokumentiert.

## [1.2.1] - 2026-02-24

### Geaendert
- Release-Pipeline fuer Synology-Update-Erkennung abgesichert: Docker-Buildx-Push ohne Attestations.
- Docker Hub Push fuer `latest` und Versionstag erfolgt mit `--provenance=false` und `--sbom=false`.

### Behoben
- DB-API-Credentials werden fuer DB-IRIS jetzt zusaetzlich direkt aus `/config/.dbapikey` (oder `DB_APIKEY_FILE`) geladen.
- Synology-Container ohne `env_file` koennen damit Bahnhof-Widgets wieder ohne "DB API credentials fehlen" ausliefern.

## [1.2.0] - 2026-02-24

### Geaendert
- DB-Credentials werden jetzt aus `config/.dbapikey` geladen (statt Root-`.env`).
- Compose nutzt dafuer `env_file` mit `DB_APIKEY_FILE`-Override.
- Debug-Konfiguration wurde in die YAML verschoben (`debug.enabled`, `debug.log_path`).
- Root-Endpoint `/` bleibt als technische Endpunkt-Uebersicht verfuegbar.

### Hinzugefuegt
- Neue Vorlage `config/.dbapikey.example`.
- Beispielkonfiguration `config/config.yaml.example` um `debug`-Abschnitt erweitert.

### Entfernt
- Root-Datei `.env.example` wurde entfernt.


## [1.1.2] - 2026-02-23

### Geaendert
- Root-Endpunkt (`/`) liefert jetzt eine technische Startseite mit Links auf Widget-, JSON-, Health- und Debug-Endpunkte.
- `docker-compose.yml` nutzt standardmaessig projektlokale Mounts (`./config`, `./data`, `./logs`) und kann per `CONFIG_DIR`, `DATA_DIR`, `LOGS_DIR` uebersteuert werden.
- `.env.example` um die neuen optionalen Mount-Variablen ergaenzt.
- README auf neue Root-URL und Compose-Mount-Defaults aktualisiert.

### Behoben
- Konfigurationsladen faellt jetzt auf `FALLBACK_CONFIG_PATH` zurueck, wenn `CONFIG_PATH` auf eine nicht vorhandene Datei zeigt.
## [1.1.1] - 2026-02-23

### Ge?ndert
- Konfigurationsvorlage vereinheitlicht: nur noch `config/config.yaml.example` wird verwendet.
- Dockerfile auf non-root-Betrieb (`app`, UID/GID 10001) und OCI-Labels erweitert.
- Docker-Healthcheck f?r `/health` erg?nzt.
- Compose-/ENV-Fallback-Pfade auf `config/config.yaml.example` umgestellt.

### Behoben
- Deduplizierung von Echtzeit- und statischen GTFS-Abfahrten verbessert.

## [1.1.0] - 2026-02-23

### HinzugefÃ¼gt
- Richtungs-Overrides aus Datei (`config/direction_overrides.txt`) mit automatischer Erkennung neuer EintrÃ¤ge.
- Konfigurationsstruktur unter `config/` mit Vorlagen fÃ¼r `config.yaml` und Richtungs-Overrides.
- ZusÃ¤tzliche Umgebungsvariablen: `DIRECTION_MAPPING_SEPARATOR` und `LOG_INSTANCE_IP`.

### GeÃ¤ndert
- Docker-Setup auf gemeinsamen Config-Mount (`/config`) umgestellt.
- Doku auf aktuelle Funktionen erweitert (24h-Ansicht, Mapping, Synology-Setup, Debug-Optionen).
- Beispiele fÃ¼r Richtungs-Overrides aktualisiert (`VMG`, `Stadt/Goethe`).

### Behoben
- Doppelte GTFS-EintrÃ¤ge aus Echtzeit + statischem Fallback stÃ¤rker dedupliziert (Echtzeit hat Vorrang, auch bei Zeitabweichungen).
- Umlaute und Zeichenkodierung in der Dokumentation bereinigt.

## [1.0.0] - 2026-02-21

### Initiales Release
- ErstverÃ¶ffentlichung von `timetable-widget`.
- Mehrere Widgets Ã¼ber `widgets`-Konfiguration.
- GTFS-Realtime- und DB-Timetables-Integration.
- HTML-Widget und JSON-Endpunkte pro Widget-ID.
- On-Demand-Refresh mit Caching, Warmup-Optionen und Debug-Logging.
- Statischer GTFS-Fallback inklusive Fahrtrichtungsermittlung.


