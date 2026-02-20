# timetable-widget

Container-Projekt fuer ein konfigurierbares Abfahrts-Widget (HTML) plus JSON-Endpoint für Integration in Drittsysteme.

## Setup

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

In `.env` die DB-Zugangsdaten für den DB API Marketplace eintragen (free subscription plan verfügbar):

```env
DB_CLIENT_ID=<deine_client_id>
DB_API_KEY=<dein_api_key>
```

`config.yaml` anpassen, insbesondere `widgets`.
Standard in diesem Projekt: Die Konfiguration wird aus `/app/config.example.yaml` im Image geladen.
Wenn `config.example.yaml` lokal geaendert wird, danach neu bauen/starten (`docker compose up -d --build`).

Hinweis zu Windows/Docker Desktop:
- In manchen Setups (insbesondere bei Netzlaufwerken) wird ein Datei-Bind-Mount als Verzeichnis interpretiert.
- Deshalb ist der Compose-Standard hier ohne Datei-Bind-Mount umgesetzt.
- Falls `CONFIG_PATH` auf ein Verzeichnis zeigt, nutzt die App automatisch einen Fallback (`FALLBACK_CONFIG_PATH`).

```bash
docker compose up -d --build
```

## URLs

- `http://localhost:8000/widget` (Uebersicht aller Widgets inkl. Widget-URL, JSON-URL und Stop-IDs)
- `http://localhost:8000/widget/1` (konkretes Widget nach ID)
- `http://localhost:8000/json/1` (JSON fuer konkretes Widget nach ID)
- `http://localhost:8000/health`

## Widget-Konfiguration

Mehrere Widgets werden in `widgets` konfiguriert. Jedes Widget hat eine eigene `id`, einen frei waehlbaren `title`
und eine Datenquelle (`source`).

```yaml
widgets:
  - id: "1"
    title: "Dechbetten/TELIS FINANZ"
    source: "gtfs_rt"
    stop_ids: ["122010", "36191"]
    gtfs_lookahead_hours: 24
    max_departures: 8
    show_delay: true
    show_feed_age: true

  - id: "2"
    title: "Bahnhof Pruefening"
    source: "db_iris"
    db_eva_no: "8004983"
    direction_contains: ["Regensburg Hbf"]
    db_only_trains: true
    db_use_fchg: true
    db_lookahead_hours: 24
    max_departures: 8
    show_delay: true
    show_feed_age: true
```

### Optionen Pro Widget (Pro Linie/Station)

Pflicht je Widget:

- `id`: eindeutige Widget-ID (String), z. B. `"1"`; muss ueber alle Widgets eindeutig sein.
- `title`: frei waehlbarer Anzeigename im Widget-Kopf.
- `source`: Datenquelle, entweder `"gtfs_rt"` oder `"db_iris"`.

Allgemeine Optionen (fuer beide Quellen):

- `max_departures` (Standard: `8`): maximale Anzahl angezeigter Verbindungen (`>= 1`).
- `show_delay` (Standard: `true`): zeigt die Verspaetungsspalte.
- `show_feed_age` (Standard: `true`): zeigt Feed-Zeitstempel und Alter.
- `direction_contains` (optional): OR-Filter fuer Richtung/Laufweg; mindestens ein Begriff muss vorkommen.
- `required_stops` (optional): AND-Filter fuer Richtung/Laufweg; alle Begriffe muessen vorkommen.

GTFS-Widget (`source: "gtfs_rt"`):

- `stop_ids` (wichtig): Liste der Stop-IDs, fuer die Abfahrten gesucht werden.
- `route_short_names` (optional): zusaetzlicher Linienfilter, z. B. `["4","10"]`.
- `gtfs_lookahead_hours` (Standard: `24`, Bereich `1..48`): Zeitfenster fuer zukunftige Abfahrten.

DB-Widget (`source: "db_iris"`):

- `db_eva_no` (Pflicht): EVA-Nummer des Bahnhofs, z. B. `"8004983"`.
- `db_lookahead_hours` (Standard: `24`, Bereich `1..24`): Anzahl Stunden, die per `plan` abgefragt werden.
- `db_only_trains` (Standard: `false`): zeigt nur Zugprodukte.
- `db_use_fchg` (Standard: `true`): zieht zusaetzliche Aenderungen (`fchg`) mit ein.

Hinweise zur Wirkung:

- `stop_ids` wird nur bei `gtfs_rt` ausgewertet.
- `db_eva_no`, `db_only_trains`, `db_use_fchg`, `db_lookahead_hours` werden nur bei `db_iris` ausgewertet.
- `source` akzeptiert intern auch Aliase (`gtfs`, `gtfs-realtime`, `db`, `db_timetables`), empfohlen sind aber `gtfs_rt` bzw. `db_iris`.
- Falls ein GTFS-Widget `route_short_names` setzt, aber kein Mapping verfuegbar ist, erscheinen dazu klare Fehlhinweise im Widget.

## DB API Credentials

Fuer `source: "db_iris"` sind folgende Environment-Variablen erforderlich:

- `DB_CLIENT_ID`
- `DB_API_KEY`

Optionale DB-Variablen:

- `DB_TIMETABLES_BASE_URL` (Standard: `https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1`)
- `DB_IRIS_BASE_URL` (Legacy-Fallback, wird intern weiterhin akzeptiert)

## openHAB MainUI Webframe Beispiel

```yaml
component: oh-webframe-card
config:
  title: "Bus - Dechbetten/TELIS FINANZ"
  src: "http://<server>:8000/widget/1"
  height: 420
```

## Hinweise

- Optional fuer schnelleres GTFS-Mapping: `GTFS_STATIC_CACHE_PATH` (Standard: `/tmp/nv_free_latest.zip`) und `GTFS_STATIC_CACHE_MAX_AGE_SECONDS` (Standard: `43200`).
- GTFS-Realtime ist Protobuf; DB Timetables API liefert XML. Der Container bereitet beides fuer HTML/JSON auf.
- Standard-Setup ohne Datei-Bind-Mount: die App liest aus `/app/config.example.yaml`.
- Aenderungen an der Config werden nach Container-Rebuild/Restart wirksam.
- Fuer Requests kann `USER_AGENT` gesetzt werden (Compose-Env).

## Synology Container Manager (Schritt fuer Schritt)

Ziel: Das Projekt als Container auf einer Synology NAS laufen lassen. Du kannst entweder das Compose-Projekt
importieren oder das Image bauen und dann manuell einen Container erstellen.

Variante A: Container Manager Projekt (empfohlen)

1. Projektordner auf die NAS kopieren (z. B. nach `/volume1/docker/timetable-widget`).
2. In Synology: Container Manager -> Projekt -> Erstellen.
3. Quelle: Lokales Verzeichnis auswaehlen und den Projektordner waehlen.
4. `.env` im Projektordner anlegen (aus `.env.example`) und DB-Werte setzen:
   - `DB_CLIENT_ID=<deine_client_id>`
   - `DB_API_KEY=<dein_api_key>`
5. `docker-compose.yml` wird erkannt. Optional Environment setzen:
   - `USER_AGENT` falls gewuenscht
   - `DEBUG_MODE=1` und `DEBUG_LOG_PATH=/logs/logfile.txt` falls Debug
   - `WARMUP_STATIC_CACHE_ON_START=1` fuer Static-Cache-Warmup
   - `WARMUP_ON_START=1` fuer kompletten Daten-Warmup
6. Projekt starten. Danach ist das Widget unter `http://<NAS-IP>:8000/widget` erreichbar.

Hinweis: In diesem Standard-Setup wird `config.example.yaml` im Image genutzt. Wenn du die Konfiguration
anpassen willst, aendere lokal `config.example.yaml`, dann das Projekt neu bauen/starten.

Variante B: Konfiguration ueber Volume (optional)

Wenn du die Konfigurationsdatei ohne Image-Rebuild anpassen willst:

1. Auf der NAS einen Ordner fuer Config anlegen, z. B. `/volume1/docker/timetable-widget/config`.
2. `config.yaml` in diesen Ordner legen (basiert auf `config.example.yaml`).
3. In Container Manager beim Projekt oder Container eine Volume-Zuordnung setzen:
   - Host: `/volume1/docker/timetable-widget/config`
   - Container: `/config`
4. Environment setzen:
   - `CONFIG_PATH=/config/config.yaml`
5. Container neu starten. Aenderungen in `config.yaml` werden nach Restart wirksam.

Variante C: Image bauen und Container manuell erstellen

1. Image per CLI bauen (SSH auf die NAS):
   ```bash
   docker build -t timetable-widget:latest .
   ```
2. Container starten:
   ```bash
   docker run -d --name timetable-widget -p 8000:8000 --env-file .env timetable-widget:latest
   ```
3. Optional Volumes/Environment wie in Variante B setzen, falls externe Config oder Logs genutzt werden sollen.


## Updates Aus Git Einspielen

Repository: `https://github.com/ifs-net/timetable-widget.git`

Voraussetzungen:

- Das Projekt liegt lokal in einem Git-Checkout.
- `.env` (mit API-Keys) und ggf. externe Config-Dateien bleiben erhalten und werden nicht aus Git ueberschrieben.

### Standard Update (CLI)

```bash
cd /pfad/zu/timetable-widget
git fetch origin
git checkout main
git pull --ff-only origin main
docker compose up -d --build
```

Optional danach aufraeumen:

```bash
docker image prune -f
```

### Update Auf Windows (PowerShell)

```powershell
cd U:\timetable-widget
git fetch origin
git checkout main
git pull --ff-only origin main
docker compose up -d --build
```

### Update Auf Synology Container Manager

Wenn das Projekt als Ordner auf der NAS liegt (z. B. `/volume1/docker/timetable-widget`):

1. Per SSH auf die NAS verbinden.
2. In den Projektordner wechseln.
3. `git pull --ff-only origin main` ausfuehren.
4. Container neu bauen/starten:
   `docker compose up -d --build`
5. Optional im Container Manager den Projektstatus kontrollieren.

### Falls Lokale Aenderungen Vorhanden Sind

Wenn `git pull` wegen lokaler Aenderungen fehlschlaegt:

```bash
git status
git stash
git pull --ff-only origin main
docker compose up -d --build
```

Danach bei Bedarf eigene Aenderungen wiederherstellen:

```bash
git stash pop
```

### Schneller Rollback Auf Letzten Commit

```bash
git log --oneline -n 5
git checkout <commit_hash>
docker compose up -d --build
```

Hinweis: Fuer den Rueckweg auf aktuellen Stand wieder `git checkout main` und `git pull --ff-only origin main` nutzen.
## Quellen und Nutzungsbedingungen

Stand: 2026-02-20. Bitte vor produktiver Nutzung immer erneut pruefen.

Verwendete Datenquellen:

- GTFS.de NV Feed: `https://download.gtfs.de/germany/nv_free/latest.zip`
- GTFS.de Realtime Feed: `https://realtime.gtfs.de/realtime-free.pb`
- DB API Marketplace Timetables API (Widget-Quelle `db_iris`): `https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/...`

Bitte bezüglich des eigenen Einsatzes selbst jeweils die rechtlichen Rahmenbedingungen für eingebundene Quellen prüfen!

## Debug-Modus

- Debug aktivieren: `DEBUG_MODE=1`
- Logpfad: `DEBUG_LOG_PATH=/logs/logfile.txt`
- Bei Compose ist `./logs:/logs` gemountet, Logdatei lokal unter `logs/logfile.txt`.
- Die Debug-Logs enthalten Stage-Timings fuer Analyse: `mapping_csv:*`, `mapping_static:*`, `mapping_enrich:*`, `poll_once:*`, `db_iris:*`.

## Warmup beim Start

- `WARMUP_STATIC_CACHE_ON_START=1` (Standard in Compose): laedt beim Start den GTFS-Static-Cache (`/tmp/nv_free_latest.zip`) vor.
- Vorteil: Der erste Widget-Aufruf muss den grossen Static-Download nicht mehr selbst ausloesen.
- `WARMUP_ON_START=1` (optional): fuehrt zusaetzlich einen kompletten Daten-Warmup aus.
- Hinweis: Warmup verlagert Wartezeit auf den Container-Start und kann den Start verlangsamen.





