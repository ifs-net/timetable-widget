# timetable-widget

Container-Projekt für ein konfigurierbares Abfahrts-Widget (HTML) plus JSON-Endpoint für Integration in Drittsysteme. Ursprünglich war es für die Integration in ein openHAB Smart-Home-System gedacht, siehe hierzu auch https://www.forwardme.de/2026/02/20/openhab-oepnv-timetable-widget-per-docker-container-integriert/

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
Wenn `config.example.yaml` lokal geändert wird, danach neu bauen/starten (`docker compose up -d --build`).
Hinweis: Beim ersten Start nach einem Build kann die App länger in `Waiting for application startup` stehen,
da der statische GTFS-Fallback-Index aufgebaut wird.

Hinweis zu Windows/Docker Desktop:
- In manchen Setups (insbesondere bei Netzlaufwerken) wird ein Datei-Bind-Mount als Verzeichnis interpretiert.
- Deshalb ist der Compose-Standard hier ohne Datei-Bind-Mount umgesetzt.
- Falls `CONFIG_PATH` auf ein Verzeichnis zeigt, nutzt die App automatisch einen Fallback (`FALLBACK_CONFIG_PATH`).
- `docker-compose.yml` muss an deine Umgebung angepasst werden (insbesondere Volume-Pfade für `config.yaml`, `data`, `logs` und ggf. Port-Mapping).

```bash
docker compose up -d --build
```

## URLs

- `http://<Docker-Container-IP>:8000/widget` (Übersicht aller Widgets inkl. Widget-URL, JSON-URL und Stop-IDs)
- `http://<Docker-Container-IP>:8000/widget/1` (konkretes Widget nach ID)
- `http://<Docker-Container-IP>:8000/json/1` (JSON für konkretes Widget nach ID)
- `http://<Docker-Container-IP>:8000/health`

## Widget-Konfiguration

Mehrere Widgets werden in `widgets` konfiguriert. Jedes Widget hat eine eigene `id`, einen frei wählbaren `title`
und eine Datenquelle (`source`).

- Es wird nur noch das Mehrfach-Widget-Format `widgets:` unterstützt.
- Das frühere Single-Format mit `widget:` und `filter:` ist entfernt.
- JSON wird pro Widget über `/json/<id>` aufgerufen (z. B. `/json/1`).

```yaml
widgets:
  - id: "1"
    title: "Dechbetten/TELIS FINANZ"
    source: "gtfs_rt"
    stop_ids: ["27741", "647898"]
    gtfs_lookahead_hours: 24
    max_departures: 8
    show_delay: true
    show_feed_age: true

  - id: "2"
    title: "Bahnhof Prüfening"
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

- `id`: eindeutige Widget-ID (String), z. B. `"1"`; muss über alle Widgets eindeutig sein.
- `title`: frei wählbarer Anzeigename im Widget-Kopf.
- `source`: Datenquelle, entweder `"gtfs_rt"` oder `"db_iris"`.

Allgemeine Optionen (für beide Quellen):

- `max_departures` (Standard: `8`): maximale Anzahl angezeigter Verbindungen (`>= 1`).
- `show_delay` (Standard: `true`): zeigt die Verspätungsspalte.
- `show_feed_age` (Standard: `true`): zeigt Feed-Zeitstempel und Alter.
- `direction_contains` (optional): OR-Filter für Richtung/Laufweg; mindestens ein Begriff muss vorkommen.
- `required_stops` (optional): AND-Filter für Richtung/Laufweg; alle Begriffe müssen vorkommen.

GTFS-Widget (`source: "gtfs_rt"`):

- `stop_ids` (wichtig): Liste der Stop-IDs, für die Abfahrten gesucht werden.
- `route_short_names` (optional): zusätzlicher Linienfilter, z. B. `["4","10"]`.
- `gtfs_lookahead_hours` (Standard: `24`, Bereich `1..48`): Zeitfenster für zukünftige Abfahrten.

- Wenn Echtzeit weniger als `max_departures` liefert, ergänzt die App weitere Abfahrten aus statischen GTFS-Fahrplänen (ohne Live-Verspätung).
- Echtzeitdaten haben Priorität; statische Fahrten werden nur ergänzend bis `max_departures` genutzt.
- Für statische Ergänzungen bleibt `delay_s` leer (`null`), weil dafür keine Live-Verspätung vorliegt.
- Die Fahrtrichtung im Fallback kommt aus `trip_headsign` aus den GTFS-Static-Daten.

DB-Widget (`source: "db_iris"`):

- `db_eva_no` (Pflicht): EVA-Nummer des Bahnhofs, z. B. `"8004983"`.
- `db_lookahead_hours` (Standard: `24`, Bereich `1..24`): Anzahl Stunden, die per `plan` abgefragt werden.
- `db_only_trains` (Standard: `false`): zeigt nur Zugprodukte.
- `db_use_fchg` (Standard: `true`): zieht zusätzliche Änderungen (`fchg`) mit ein.

Hinweise zur Wirkung:

- `stop_ids` wird nur bei `gtfs_rt` ausgewertet.
- Die Meldung `Falsche Konfiguration: Stop-ID ... nicht gefunden.` basiert auf `stops.txt` aus den statischen GTFS-Daten, nicht auf einem einzelnen Live-Snapshot.
- Wenn eine Stop-ID im Live-Feed temporär keine Fahrten hat, wird sie dadurch nicht mehr fälschlich als Konfigurationsfehler markiert.
- `db_eva_no`, `db_only_trains`, `db_use_fchg`, `db_lookahead_hours` werden nur bei `db_iris` ausgewertet.
- `source` akzeptiert intern auch Aliase (`gtfs`, `gtfs-realtime`, `db`, `db_timetables`), empfohlen sind aber `gtfs_rt` bzw. `db_iris`.
- Falls ein GTFS-Widget `route_short_names` setzt, aber kein Mapping verfügbar ist, erscheinen dazu klare Fehlhinweise im Widget.

## DB API Credentials

Für `source: "db_iris"` sind folgende Environment-Variablen erforderlich:

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

## GTFS-RT Und Statischer Fallback

- Für `source: "gtfs_rt"` wird zuerst ausschließlich aus dem Echtzeit-Feed gelesen.
- Falls weniger Treffer als `max_departures` vorhanden sind, wird mit statischen GTFS-Fahrplänen bis zum Limit ergänzt.
- Deduplizierung erfolgt über `trip_id + time_epoch + stop_id`, damit Einträge nicht doppelt erscheinen.
- Der Fallback gilt nur für GTFS-Widgets; DB-Widgets (`source: "db_iris"`) sind davon unberührt.

- Optional für schnelleres GTFS-Mapping: `GTFS_STATIC_CACHE_PATH` (Standard: `/tmp/nv_free_latest.zip`) und `GTFS_STATIC_CACHE_MAX_AGE_SECONDS` (Standard: `43200`).
- GTFS-Realtime ist Protobuf; DB Timetables API liefert XML. Der Container bereitet beides für HTML/JSON auf.
- Standard-Setup ohne Datei-Bind-Mount: die App liest aus `/app/config.example.yaml`.
- Änderungen an der Config werden nach Container-Rebuild/Restart wirksam.
- Für Requests kann `USER_AGENT` gesetzt werden (Compose-Env).

## Synology Container Manager (Schritt für Schritt)

Ziel: Das Projekt als Container auf einer Synology NAS laufen lassen. Du kannst entweder das Compose-Projekt
importieren oder das Image bauen und dann manuell einen Container erstellen.

Variante A: Container Manager Projekt (empfohlen)

1. Projektordner auf die NAS kopieren (z. B. nach `/volume1/docker/timetable-widget`).
2. In Synology: Container Manager -> Projekt -> Erstellen.
3. Quelle: Lokales Verzeichnis auswählen und den Projektordner wählen.
4. `.env` im Projektordner anlegen (aus `.env.example`) und DB-Werte setzen:
   - `DB_CLIENT_ID=<deine_client_id>`
   - `DB_API_KEY=<dein_api_key>`
5. `docker-compose.yml` wird erkannt. Vor dem Start Pfade und Mounts an die eigene NAS-Struktur anpassen (Config/Logs/Data). Optional Environment setzen:
   - `USER_AGENT` falls gewünscht
   - `DEBUG_MODE=1` und `DEBUG_LOG_PATH=/logs/logfile.txt` falls Debug
   - `WARMUP_STATIC_CACHE_ON_START=1` für Static-Cache-Warmup
   - `WARMUP_ON_START=1` für kompletten Daten-Warmup
6. Projekt starten. Danach ist das Widget unter `http://<NAS-IP>:8000/widget` erreichbar.

Hinweis: In diesem Standard-Setup wird `config.example.yaml` im Image genutzt. Wenn du die Konfiguration
anpassen willst, ändere lokal `config.example.yaml`, dann das Projekt neu bauen/starten.

Variante B: Konfiguration über Volume (optional)

Wenn du die Konfigurationsdatei ohne Image-Rebuild anpassen willst:

1. Auf der NAS einen Ordner für Config anlegen, z. B. `/volume1/docker/timetable-widget/config`.
2. `config.yaml` in diesen Ordner legen (basiert auf `config.example.yaml`).
3. In Container Manager beim Projekt oder Container eine Volume-Zuordnung setzen:
   - Host: `/volume1/docker/timetable-widget/config`
   - Container: `/config`
4. Environment setzen:
   - `CONFIG_PATH=/config/config.yaml`
5. Container neu starten. Änderungen in `config.yaml` werden nach Restart wirksam.

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
- `.env` (mit API-Keys) und ggf. externe Config-Dateien bleiben erhalten und werden nicht aus Git überschrieben.

### Standard Update (CLI)

```bash
cd /pfad/zu/timetable-widget
git fetch origin
git checkout main
git pull --ff-only origin main
docker compose up -d --build
```

Optional danach aufräumen:

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
3. `git pull --ff-only origin main` ausführen.
4. Container neu bauen/starten:
   `docker compose up -d --build`
5. Optional im Container Manager den Projektstatus kontrollieren.

### Falls lokale Änderungen vorhanden sind

Wenn `git pull` wegen lokaler Änderungen fehlschlägt:

```bash
git status
git stash
git pull --ff-only origin main
docker compose up -d --build
```

Danach bei Bedarf eigene Änderungen wiederherstellen:

```bash
git stash pop
```

### Schneller Rollback Auf Letzten Commit

```bash
git log --oneline -n 5
git checkout <commit_hash>
docker compose up -d --build
```

Hinweis: Für den Rückweg auf aktuellen Stand wieder `git checkout main` und `git pull --ff-only origin main` nutzen.
## Quellen und Nutzungsbedingungen

Stand: 2026-02-20. Bitte vor produktiver Nutzung immer erneut prüfen.

Verwendete Datenquellen:

- GTFS.de NV Feed: `https://download.gtfs.de/germany/nv_free/latest.zip`
- GTFS.de Realtime Feed: `https://realtime.gtfs.de/realtime-free.pb`
- DB API Marketplace Timetables API (Widget-Quelle `db_iris`): `https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1/...`

Bitte bezüglich des eigenen Einsatzes selbst jeweils die rechtlichen Rahmenbedingungen für eingebundene Quellen prüfen!

## Debug-Modus

- Startwert per Umgebung: `DEBUG_MODE=1` (oder `0`).
- Standard-Logpfad: `DEBUG_LOG_PATH=/logs/logfile.txt`.
- Bei Compose ist `./logs:/logs` gemountet, Logdatei lokal unter `logs/logfile.txt`.
- Laufzeit-Umschaltung ohne Container-Neustart:
- `GET /debug` zeigt aktuellen Zustand.
- `POST /debug/on` aktiviert Debug-Logging.
- `POST /debug/on?log_path=/logs/mein-debug.log` aktiviert Debug mit anderem Logpfad.
- `POST /debug/off` deaktiviert Debug-Logging.
- Die Debug-Logs enthalten Stage-Timings für Analyse: `mapping_csv:*`, `mapping_static:*`, `mapping_enrich:*`, `poll_once:*`, `db_iris:*`, `fallback_static:*`.
- Hinweis: Die Debug-Endpunkte sind aktuell nicht authentifiziert und sollten nur in vertrauenswürdigen Netzen erreichbar sein.

## Warmup beim Start

- `WARMUP_STATIC_CACHE_ON_START=1` (Standard in Compose): lädt beim Start den GTFS-Static-Cache (`/tmp/nv_free_latest.zip`) vor.
- Zusätzlich wird beim Start der statische Fallback-Index für konfigurierte GTFS-Stop-IDs aufgebaut.
- Vorteil: Der erste Widget-Aufruf muss den großen Static-Download nicht mehr selbst auslösen.
- `WARMUP_ON_START=1` (optional): führt zusätzlich einen kompletten Daten-Warmup aus.
- Hinweis: Warmup verlagert Wartezeit auf den Container-Start und kann den Start verlangsamen.
