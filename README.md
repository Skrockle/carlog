<div align="center">

# 🚗 carlog

**Selbst gehosteter Fahrzeug-Tracker — Tanken, Wartung & Telemetrie, automatisch per Pi im Auto.**

Ein Container auf dem eigenen Server, alle Daten in **einer** SQLite-Datei. Ein Raspberry Pi im Auto
liest OBD2 + GPS, **puffert lokal** und synchronisiert per Batch — Handy und Cloud optional.

[![CI](https://github.com/Skrockle/carlog/actions/workflows/ci.yml/badge.svg)](https://github.com/Skrockle/carlog/actions/workflows/ci.yml)
[![Release](https://github.com/Skrockle/carlog/actions/workflows/release.yml/badge.svg)](https://github.com/Skrockle/carlog/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](server.py)
[![Datenbank](https://img.shields.io/badge/Datenbank-SQLite-003B57?logo=sqlite&logoColor=white)](https://sqlite.org)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Cloud](https://img.shields.io/badge/Cloud-keine-success)](#setup-auf-dem-tower)
[![Unraid](https://img.shields.io/badge/Unraid-ready-f15a24?logo=unraid&logoColor=white)](#setup-auf-dem-tower)

![carlog Vorschau](docs/preview.svg)

*Vorschau (Statistik-Ansicht) mit Beispieldaten — das echte UI läuft komplett lokal unter `http://TOWER-IP:8077`.*

</div>

## Architektur

![Architektur](docs/architecture.svg)

**Der Pi im Auto** (Zero 2 W + USB-ELM327 + USB-GPS) pollt OBD2 (Standard- + GR-Yaris-Enhanced-PIDs)
und GPS, puffert alles in lokalem SQLite und synct per Batch, sobald er das Heim-WLAN sieht —
optional per IoT-SIM auch unterwegs. Der komplette Plan mit Hardware, Bestellliste und
GR-Yaris-PIDs: **[docs/PLAN.md](docs/PLAN.md)**.

## Features

- 🚗 **Pi-Daemon** (`pi/pilogger.py`): OBD2-Poller (Drehzahl, Kühl-/Ansaugtemperatur, Öldruck, Getriebetemp, Bordspannung — GR-Yaris-Enhanced-PIDs inklusive) + GPS/NMEA, Zündungs-Erkennung mit Spannungs-Wache
- 📦 **Batch-Sync**: lokaler SQLite-Puffer auf dem Pi, `POST /api/ingest/batch` an den Tower — nichts geht verloren, WLAN-Lücken sind egal
- ⛽ **Tanklog** mit Voll-zu-Voll-Verbrauch: l/100 km, €/100 km, €/Monat — serverseitig gerechnet
- 🔧 **Wartungshistorie** mit km-Stand, Kosten und Notizen
- 📊 **Statistik-Tab** mit SVG-Charts + **Live-OBD-Anzeige** (letzte Empfangswerte vom Pi, mit Alter)
- 📱 **Dunkles, deutschsprachiges Web-UI**, PWA-fähig — Handy optional, keine Pflicht-App
- 📤 **CSV-Export** für alle Datenarten · 🔐 optionaler **Token-Schutz**
- ✅ **CI**: E2E-Kette (Simulator → Daemon → carlog) läuft bei jedem Push — ohne echtes Auto

## Was macht Sinn zu tracken (GR Yaris, Benziner)

| Was | Wie | Automatisch? |
|---|---|---|
| Verbrauch l/100 km, €/100 km, €/Monat | Tanklog (immer voll, Liter + Preis + km) | nein – 2 Taps im Formular |
| Wartungshistorie, Reifen-Lebensdauer | Wartungs-Formular | nein – kurz eintragen |
| km/Monat, Fahrten, Jahresleistung | Pi + USB-GPS-Stick | **ja** |
| Drehzahl, Kühl-/Öltemperatur, Öldruck, 12V-Spannung | Pi + USB-ELM327 | **ja** |
| Rohdaten für spätere Auswertung | bleiben in SQLite/CSV | – |

Nicht ohne Cloud möglich: Tankfüllung selbst (kein Tanksensor ohne Hardware-Umbau)
und Toyota-myT-Daten – bewusst weggelassen, weil Cloud.

## Setup auf dem Tower

1. Image ziehen (nach dem ersten Release-Tag) oder lokal bauen:
   ```sh
   docker pull ghcr.io/skrockle/carlog:latest
   # oder: git clone git@github.com:Skrockle/carlog.git && cd carlog
   ```
2. `docker compose up -d` (Unraid ≥ 7 hat Compose integriert) **oder** CA →
   „Add Container“: Port `8077:8077`, Volume `/mnt/user/appdata/carlog:/data`.
3. Öffnen: `http://TOWER-IP:8077` → am Handy „Zum Homescreen“ (PWA-fähig).

Backup: nur die Datei `/mnt/user/appdata/carlog/carlog.db` sichern – fertig.

## Automatische Erfassung einrichten

**Primär: der Pi-Daemon** — Architektur, Hardware-Bestellliste und Betrieb: [docs/PLAN.md](docs/PLAN.md).
Am Pi (Zero 2 W; ELM327 + GPS am micro-USB-OTG-Hub, Strom über OBD-Port oder zündungsgesteuert + UPS):

```sh
sudo -S -p '' apt install python3-serial
python3 pi/pilogger.py --obd-port /dev/ttyUSB0 --gps-port /dev/ttyACM0 --url http://TOWER-IP:8077 --verbose
```

Als Dienst: `pi/carlog-pilogger.service` nach `/etc/systemd/system/`, dann `systemctl enable --now carlog-pilogger`.

**Optional: Handy-Apps** — das Handy ist kein Pflicht-Teil, die Endpunkte funktionieren weiterhin:
- GPSLogger/OsmAnd/Traccar → `http://TOWER-IP:8077/api/gps?lat=%LAT&lon=%LON&timestamp=%TIMESTAMP&s=%SPD&alt=%ALT&id=GRYaris&battery=%BATT`
- Torque Pro (Android) → `http://TOWER-IP:8077/api/obd?id=GRYaris`

Optional TOKEN in der compose setzen → dann `?token=...` an alle schreibenden Endpunkte anhängen.

## API (kurz)

| Endpunkt | Zweck |
|---|---|
| `GET /api/health` | Status |
| `GET/POST/DELETE /api/fillups` | Tanklog (Verbrauch wird serverseitig gerechnet) |
| `GET/POST/DELETE /api/maintenance` | Wartung |
| `GET /api/stats` | Gesamt- + Monatswerte (Tanklog + GPS) |
| `POST /api/ingest/batch` | Bulk-Ingest vom Pi (N GPS-Punkte + M OBD-Messungen in einem Request) |
| `GET /api/gps?lat=&lon=&timestamp=&s=&id=` | GPS-Ingest (OsmAnd/GPSLogger/Traccar-Protokoll) |
| `GET /api/obd?time=&session=&id=&v<pid>=` | OBD-Ingest (Torque-Format) |
| `GET /api/telemetry?kind=obd\|gps&limit=N` | letzte Rohwerte |
| `GET /api/export.csv?kind=fillups\|maintenance\|gps\|obd` | CSV-Export |

## Pi-Daemon testen — ganz ohne Auto

Der ELM327+GR-Yaris-Simulator liegt bei (`test/elm327-sim.py`):

```sh
python3 test/elm327-sim.py &    # ELM327 + GR Yaris auf 127.0.0.1:35000
python3 pi/pilogger.py --obd-port tcp://127.0.0.1:35000 --url http://127.0.0.1:8079 --once --verbose
```

## CI/CD

- **CI** (`.github/workflows/ci.yml`): bei jedem Push — Syntax-Check + E2E-Kette (Simulator → Daemon → carlog → DB)
- **Release** (`.github/workflows/release.yml`): bei Tag `v*` — Docker-Image → GHCR (`ghcr.io/skrockle/carlog`) + GitHub Release

## Entwicklung

Lokal ohne Docker:
```sh
CARLOG_DB=./carlog.db python3 server.py   # -> http://127.0.0.1:8077
```
