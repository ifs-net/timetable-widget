# timetable-widget

Container-Projekt für ein konfigurierbares Abfahrts-Widget (HTML) plus JSON-Endpoint für Integration in Drittsysteme. Ursprünglich war es für die Integration in ein openHAB Smart-Home-System gedacht, siehe hierzu auch https://www.forwardme.de/2026/02/20/openhab-oepnv-timetable-widget-per-docker-container-integriert/

Konkret integriert werden sollten für meinen Fall Bahnverbindungen eines nahen Bahnhofs Richtung Regensburg Zentrum sowie die Abfahrtszeiten und Live-Daten zu den Busverbindungen des RVV (Regensburger Verkehrsverbunds) zweier Haltestellen nahe meines Wohnorts. Zudem wollte ich bestimmte Richtungen mit Erklärungen belegen, damit die Kinder wissen, welche Verbindungen für Schule / Stadt / ... die relevanten sind.

Die Lösung ist aber nicht auf den RVV beschränkt, sondern universell einsetzbar für alle in den genutzten Quellen enthaltenen Verkehrsverbunde.

## Setup

```bash
cp config/config.yaml.example config/config.yaml
cp config/direction_overrides.txt.example config/direction_overrides.txt
cp config/.dbapikey.example config/.dbapikey
```

In `config/.dbapikey` die DB-Zugangsdaten für den DB API Marketplace eintragen (free subscription plan verfügbar):

```env
DB_CLIENT_ID=<deine_client_id>
DB_API_KEY=<dein_api_key>
```

`config/config.yaml` anpassen, insbesondere `widgets`.
Compose-Standard: Die Konfiguration wird aus `/config/config.yaml` geladen (Host-Mount `${CONFIG_DIR:-./config}:/config`).
Wenn `config/config.yaml` lokal geändert wird, reicht ein Container-Neustart (`docker compose restart`).
Hinweis: Beim ersten Start nach einem Build kann die App länger in `Waiting for application startup` stehen,
da der statische GTFS-Fallback-Index aufgebaut wird.

Hinweis zu Windows/Docker Desktop:
- In manchen Setups (insbesondere bei Netzlaufwerken) wird ein Datei-Bind-Mount als Verzeichnis interpretiert.
- Deshalb ist der Compose-Standard hier ohne Datei-Bind-Mount umgesetzt.
- Falls `CONFIG_PATH` auf ein Verzeichnis zeigt, nutzt die App automatisch einen Fallback (`FALLBACK_CONFIG_PATH`).
- `docker-compose.yml` nutzt standardmäßig lokale Projektordner (`./config`, `./data`, `./logs`). Optional per Shell-Environment: `CONFIG_DIR`, `DATA_DIR`, `LOGS_DIR`.

```bash
docker compose pull && docker compose up -d
```

## URLs

- `http://<Docker-Container-IP>:8000/widget` (Übersicht aller Widgets inkl. Standard- und 24h-URLs)
- `http://<Docker-Container-IP>:8000/widget/1` (konkretes Widget nach ID, Standardansicht mit `max_departures`)
- `http://<Docker-Container-IP>:8000/widget/1/24h` (konkretes Widget nach ID, alle Abfahrten der nächsten 24h)
- `http://<Docker-Container-IP>:8000/json/1` (JSON für konkretes Widget nach ID, Standardansicht)
- `http://<Docker-Container-IP>:8000/json/1/24h` (JSON für konkretes Widget nach ID, 24h-Ansicht)
- `http://<Docker-Container-IP>:8000/` (technische Startseite mit Endpunkt-Übersicht)
- `http://<Docker-Container-IP>:8000/health`


## Hinweise zu Overrides und Logs

- Die Datei `config/direction_overrides.txt` muss als echte Datei vorhanden sein.
  Nur `config/direction_overrides.txt.example` reicht nicht aus.
- In Docker muss die Config in den Container gemountet sein (Standard: `${CONFIG_DIR:-./config}:/config`).
- Die Log-Ansicht unter `/logs` scrollt standardmäßig automatisch zu den neuesten Einträgen.
  Das Verhalten kann über die Checkbox `Auto Scroll` direkt in der Ansicht deaktiviert werden.
## Widget-Konfiguration

Mehrere Widgets werden in `widgets` konfiguriert. Jedes Widget hat eine eigene `id`, einen frei wählbaren `title`
und eine Datenquelle (`source`).

- Es wird nur noch das Mehrfach-Widget-Format `widgets:` unterstützt.
- Das frühere Single-Format mit `widget:` und `filter:` ist entfernt.
- JSON wird pro Widget über `/json/<id>` aufgerufen (z. B. `/json/1`).
- Die 24h-Ansicht ist je Widget über `/widget/<id>/24h` und `/json/<id>/24h` erreichbar.

```yaml
widgets:
  - id: "1"
    title: "Dechbetten/TELIS FINANZ"
    source: "gtfs_rt"
    stop_ids:
      - "27741"   # Dechbetten/TELIS FINANZ (aktive Richtung 1, Regensburg)
      - "647898"  # Dechbetten/TELIS FINANZ (aktive Richtung 2, Regensburg)
    gtfs_lookahead_hours: 24
    max_departures: 8
    show_delay: true
    show_feed_age: true

  - id: "2"
    title: "Bahnhof Prüfening"
    source: "db_iris"
    db_eva_no: "8004983"  # DB/EVA: Regensburg-Prüfening
    direction_contains: ["Regensburg Hbf"]
    db_only_trains: true
    db_use_fchg: true
    db_lookahead_hours: 24
    max_departures: 8
    show_delay: true
    show_feed_age: true

  - id: "3"
    title: "Lilienthalstraße"
    source: "gtfs_rt"
    stop_ids:
      - "406702"  # Lilienthalstraße (Regensburg, Richtung A)
      - "8593"    # Lilienthalstraße (Regensburg, Richtung B)
      - "86805"   # Lilienthalstraße (Regensburg, weitere Steig-/Richtungs-ID)
    gtfs_lookahead_hours: 24
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

- `max_departures` (Standard: `8`): maximale Anzahl angezeigter Verbindungen (`>= 1`) in der Standardansicht (`/widget/<id>` und `/json/<id>`).
- In der 24h-Ansicht (`/widget/<id>/24h`, `/json/<id>/24h`) werden alle Abfahrten der nächsten 24 Stunden geliefert.
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
- Sobald eine passende Echtzeitfahrt vorhanden ist, wird der entsprechende statische Eintrag unterdrückt (auch bei abweichender Minutenlage durch Verspätung).

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

## Richtungs-Mapping (Custom Labels)

Du kannst je `Linie + Fahrtrichtung` einen frei wählbaren Zusatztext hinterlegen, der im Widget in Klammern hinter der Fahrtrichtung erscheint.

Beispiel in der Mapping-Datei:

```
4|Universitaet|VMG
4|Universit?t|VMG
1|Pommernstraße|Stadt/Goethe
```

Bedeutung: `Linie|Fahrtrichtung|Custom-Zusatz`

Vorlage im Repository: `config/direction_overrides.txt.example`

- Datei-Pfad per ENV: `DIRECTION_MAPPING_PATH` (Standard: `/config/direction_overrides.txt`)
- Trennzeichen per ENV: `DIRECTION_MAPPING_SEPARATOR` (Standard: `|`)
- Reload-Intervall per ENV: `DIRECTION_MAPPING_RELOAD_SECONDS` (Standard: `15`)
- Wildcard-Toleranz: `?` (ein Zeichen) und `*` (beliebig viele Zeichen) werden in Route und Fahrtrichtung unterstützt.

Verhalten:

- Der Container legt die Datei automatisch an, wenn sie noch nicht existiert.
- Neue Kombinationen aus Linie/Fahrtrichtung werden automatisch als neue Zeile ergänzt, z. B. `4|Universität|`
- Bestehende Einträge bleiben erhalten und werden nicht gelöscht.
- Wenn der dritte Wert leer ist, wird kein Zusatz angezeigt.

## DB API Credentials

Für `source: "db_iris"` sind folgende Environment-Variablen erforderlich (Compose lädt sie aus `config/.dbapikey`):

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
  # Alternativ 24h-Ansicht: http://<server>:8000/widget/1/24h
  height: 420
```

## Hinweise

## GTFS-RT Und Statischer Fallback

- Für `source: "gtfs_rt"` wird zuerst ausschließlich aus dem Echtzeit-Feed gelesen.
- Falls weniger Treffer als `max_departures` vorhanden sind, wird mit statischen GTFS-Fahrplänen bis zum Limit ergänzt.
- In der 24h-Ansicht werden Realtime und statischer Fallback ebenfalls zusammengeführt, aber ohne Begrenzung auf `max_departures`.
- Die 24h-Berechnung ist serverseitig kurz gecacht (TTL = `feed.refresh_seconds`), damit wiederholte Ajax-Refreshes performant bleiben.
- Deduplizierung priorisiert Echtzeitdaten. Statische Fahrten werden verworfen, wenn eine passende Live-Fahrt bereits vorhanden ist (über `trip_id+stop_id` sowie zusätzlich Linie/Richtung/Stop im Zeitfenster mit Verspätungsabgleich).
- Der Fallback gilt nur für GTFS-Widgets; DB-Widgets (`source: "db_iris"`) sind davon unberührt.
- Optional für schnelleres GTFS-Mapping: `GTFS_STATIC_CACHE_PATH` (Standard: `/data/nv_free_latest.zip`) und `GTFS_STATIC_CACHE_MAX_AGE_SECONDS` (Standard: `43200`).
- GTFS-Realtime ist Protobuf; DB Timetables API liefert XML. Der Container bereitet beides für HTML/JSON auf.
- Compose-Standard: die App liest aus `/config/config.yaml` (Volume-Mount `/config`).
- Änderungen an `config/config.yaml` und `config/direction_overrides.txt` werden nach Container-Restart wirksam.
- Für Requests kann `USER_AGENT` gesetzt werden (Compose-Env).

## Synology Container Manager (Schritt für Schritt)

Ziel: Das Projekt als Container auf einer Synology NAS laufen lassen. Du kannst entweder das Compose-Projekt
importieren oder das Image bauen und dann manuell einen Container erstellen.

Variante A: Container Manager Projekt (empfohlen)

1. Projektordner auf die NAS kopieren (z. B. nach `/volume1/docker/timetable-widget`).
2. In Synology: Container Manager -> Projekt -> Erstellen.
3. Quelle: Lokales Verzeichnis auswählen und den Projektordner wählen.
4. `config/.dbapikey` anlegen (aus `config/.dbapikey.example`) und DB-Werte setzen:
   - `DB_CLIENT_ID=<deine_client_id>`
   - `DB_API_KEY=<dein_api_key>`
5. `docker-compose.yml` wird erkannt. Vor dem Start Pfade und Mounts an die eigene NAS-Struktur anpassen (Config/Logs/Data). Optional Environment setzen:
   - `USER_AGENT` falls gewünscht
   - Debug über `config/config.yaml` steuern (`debug.enabled`, `debug.log_path`)
   - `WARMUP_STATIC_CACHE_ON_START=1` für Static-Cache-Warmup
   - `WARMUP_ON_START=1` für kompletten Daten-Warmup
   - `DIRECTION_MAPPING_PATH=/config/direction_overrides.txt` für Custom-Richtungslabels
   - `DIRECTION_MAPPING_SEPARATOR=|` falls ein anderes Trennzeichen genutzt werden soll
   - `LOG_INSTANCE_IP=<optional>` falls eine feste Instanz-IP im Debug-Log erzwungen werden soll
6. Projekt starten. Danach ist das Widget unter `http://<NAS-IP>:8000/widget` erreichbar.

Hinweis: Im Compose-Standard wird `config/config.yaml` über den `/config`-Mount genutzt. Änderungen werden nach Container-Restart wirksam.

Variante B: Konfiguration über Volume (optional)

Wenn du die Konfigurationsdatei ohne Image-Rebuild anpassen willst:

1. Auf der NAS einen Ordner für Config anlegen, z. B. `/volume1/docker/timetable-widget/config`.
2. `config.yaml` in diesen Ordner legen (z. B. aus `config/config.yaml.example`).
3. Optional zusätzlich `direction_overrides.txt` in denselben Ordner legen (z. B. aus `config/direction_overrides.txt.example`).
4. In Container Manager beim Projekt oder Container eine Volume-Zuordnung setzen:
   - Host: `/volume1/docker/timetable-widget/config`
   - Container: `/config`
5. Environment setzen:
   - `CONFIG_PATH=/config/config.yaml`
6. Container neu starten. Änderungen in `config.yaml` und `direction_overrides.txt` werden nach Restart wirksam.

Variante C: Image bauen und Container manuell erstellen

1. Image per CLI bauen (SSH auf die NAS):
   ```bash
   docker build -t timetable-widget:latest .
   ```
2. Container starten:
   ```bash
   docker run -d --name timetable-widget -p 8000:8000 --env-file config/.dbapikey timetable-widget:latest
   ```
3. Optional Volumes/Environment wie in Variante B setzen, falls externe Config oder Logs genutzt werden sollen.


## Updates Aus Git Einspielen

Repository: `https://github.com/ifs-net/timetable-widget.git`

Voraussetzungen:

- Das Projekt liegt lokal in einem Git-Checkout.
- `config/.dbapikey` (mit API-Keys) und ggf. externe Config-Dateien bleiben erhalten und werden nicht aus Git überschrieben.

### Standard Update (CLI)

```bash
cd /pfad/zu/timetable-widget
git fetch origin
git checkout main
git pull --ff-only origin main
docker compose pull && docker compose up -d
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
docker compose pull && docker compose up -d
```

### Docker Hub Publish (Synology-sicher)

Für Releases sollte der Multi-Arch-Push ohne Attestations erfolgen, damit Synology Updates für `latest` zuverlässig erkennt:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --provenance=false \
  --sbom=false \
  -t ifsnet/timetable-widget:<version> \
  -t ifsnet/timetable-widget:latest \
  --push .
```

### Update Auf Synology Container Manager

Wenn das Projekt als Ordner auf der NAS liegt (z. B. `/volume1/docker/timetable-widget`):

1. Per SSH auf die NAS verbinden.
2. In den Projektordner wechseln.
3. `git pull --ff-only origin main` ausführen.
4. Container neu bauen/starten:
   `docker compose pull && docker compose up -d`
5. Optional im Container Manager den Projektstatus kontrollieren.

### Falls lokale Änderungen vorhanden sind

Wenn `git pull` wegen lokaler Änderungen fehlschlägt:

```bash
git status
git stash
git pull --ff-only origin main
docker compose pull && docker compose up -d
```

Danach bei Bedarf eigene Änderungen wiederherstellen:

```bash
git stash pop
```

### Schneller Rollback Auf Letzten Commit

```bash
git log --oneline -n 5
git checkout <commit_hash>
docker compose pull && docker compose up -d
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

- Startwert über YAML: `debug.enabled: true|false` in `config/config.yaml`.
- Standard-Logpfad über YAML: `debug.log_path: "/logs/logfile.txt"`.
- Optional feste Kennung je Instanz: `LOG_INSTANCE_IP` (sonst automatische Erkennung).
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





