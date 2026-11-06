# carlog – Ausbauplan: Pi-Reader im Auto (v4)

Status: **verabschiedet** (Nov 2026), Umsetzung läuft. Stand des Trackers: siehe unten.

![Architektur](architecture.svg)

## Ziel

Ein **Raspberry Pi im Auto** ist der eigentliche Reader: Er liest OBD2 (Standard- + Toyota-Enhanced-PIDs)
und GPS, **puffert alles lokal in SQLite** und synchronisiert per Batch, sobald er das Heim-WLAN sieht —
oder optional per LTE (IoT-SIM), wenn unterwegs live gesendet werden soll. Das Handy ist **kein
Pflicht-Teil** mehr (nur optional als Browser-Anzeige).

## Hardware

| Komponente | Kosten (ca.) | Status |
|---|---|---|
| Raspberry Pi Zero 2 W + SD-Karte | ~25 € | zu kaufen |
| USB-ELM327-Adapter | ~12 € | zu kaufen |
| USB-GPS-Stick (u-blox NEO) | ~20 € | zu kaufen |
| **Optional:** LTE-Modem (USB) + IoT-SIM (z. B. 1NCE) | ~20–30 € + ~10 €/Jahr | optional – Live-Sync unterwegs |
| **Optional:** kleines Display (SPI) für den Pi | ~15 € | später |

Entfallene Teile: **Vgate iCar Pro** (war nur für die iPhone-Route nötig — der USB-ELM327 am Pi macht dasselbe)
und **Handy-Apps** (GPSLogger/Traccar/Car Scanner) als Pflichtkomponente.

## Software-Komponenten

**Auto-Seite (Pi):**
- `pi/pilogger.py` — systemd-Daemon (nur Python-Stdlib):
  - **OBD-Modul:** ELM327 über seriell (`/dev/ttyUSB0`) oder TCP (`tcp://host:port` für den Simulator).
    Pollt Standard-PIDs (Drehzahl 010C, Speed 010D, Kühltemp 0105, Ansaugtemp 010F, Motorlast 0104,
    Bordspannung ATRV) und GR-Yaris-Enhanced-PIDs (Öldruck 221074, Getriebetemp 221638, +B 221021).
    Zündungs-Erkennung: Drehzahl 0 / Poll-Fehler → Schlafmodus mit ATRV-Wache.
  - **GPS-Modul:** NMEA (GPRMC/GPGGA) vom u-blox-Stick, 1 Hz.
  - **SQLite-Puffer:** `puffer.db` im selben Schema wie der Batch-Endpunkt — nichts geht verloren.
  - **Sync-Modul:** sobald carlog erreichbar → `POST /api/ingest/batch` (Chunks à 500 Zeilen),
    danach Puffer leeren. Transportunabhängig: WLAN **oder** LTE — der Daemon merkt keinen Unterschied.

**Tower-Seite (Unraid):**
- `server.py`: neuer Batch-Endpunkt `POST /api/ingest/batch` (N GPS-Punkte + M OBD-Messungen in einem Request).
- Web-UI: **Live-OBD-Werte** im Automatik-Tab (letzte Messungen + Alter in Sekunden, Auto-Refresh).
- Test: `test/elm327-sim.py` — **ELM327-Simulator über TCP** mit GR-Yaris-PIDs: die ganze Kette lässt sich
  ohne echtes Auto testen (später auch in CI statt elmulator, da GR-Yaris-spezifisch).

## Datenfluss (Batch-Sync)

1. Zündung an → Pi pollt OBD (5 s) + GPS (1 Hz) → lokaler Puffer
2. Zündung aus / Motor aus → Pi schläft (ATRV-Wache, weckt bei Ladespannung)
3. Heim-WLAN erreichbar → Batch-POST an den Tower → Puffer geleert
4. WLAN-Reichweite bis zum Parkplatz ist **unkritisch**: ein paar Minuten Empfang reichen; sonst
   Access Point am Fenster Richtung Parkplatz (~20–30 €)

## GR-Yaris-PIDs (Recherche)

Quellen: [gr-yaris.co.uk „Reverse engineering PIDs"](https://www.gr-yaris.co.uk/threads/reverse-engineering-pids.4680),
[2m.lt](https://2m.lt/blog/reverse-engineering-pids), [2m/gry-obd-pids](https://github.com/2m/gry-obd-pids).

| Wert | PID (Header 700/7C0) | Formel |
|---|---|---|
| Drehzahl | 010C | ((A·256)+B)/4 |
| Geschwindigkeit | 010D | A |
| Kühlmitteltemp | 0105 | A−40 |
| Ansauglufttemp | 010F | A−40 |
| Bordspannung (ELM) | ATRV | V |
| Öldruck Motor | 221074 | ((A·256)+B)/1280 bar |
| Getriebeöl-Temp | 221638 | A+B/256−40 °C |
| +B Bordspannung | 221021 | A/10 V |
| Ladedruck (Intake MAP) | 221F87 | Formel noch offen |

Motoröl-Temp ist nicht bestätigt. **Am Abholtag prüfen:** Support-Bitmuster 0100/0120/0140/0160 lesen,
Enhanced-PIDs plausibilisieren (Getriebetemp warm ~50–55 °C, +B 12,2 V Zündung an / ~14,2 V laufend,
Öldruck Leerlauf warm 0,9–1 bar).

## Entscheidungen

- **Batch-Sync statt Live:** lokaler Puffer verliert nichts; WLAN-Lücken sind egal.
- **Meshtastic verworfen** (für diesen Zweck): Best-Effort ohne Verlustgarantie, winzige LoRa-Bandbreite
  (EU-Duty-Cycle < 1 %), ~40–60 € Zusatzhardware, nur brauchbar wenn der Parkplatz in Meshweite liegt.
- **IoT-SIM als Option aufgenommen:** LTE-Modem + 1NCE (~10 €/Jahr) direkt am Pi — der Daemon sync't
  transportunabhängig, kein Code-Unterschied zu WLAN.
- **Vgate entfallen:** USB-ELM327 am Pi deckt alles ab.

## Phasen

| Phase | Inhalt | Status |
|---|---|---|
| P1 | carlog-Batch-Endpunkt `/api/ingest/batch` | in Arbeit |
| P2 | Pi-Daemon `pilogger.py` + ELM327-Simulator, E2E gegen Simulator | in Arbeit |
| P3 | Hardware bestellen & aufbauen, systemd-Unit auf dem Pi | offen |
| P4 | Abholtag-Verifikation (Support-Bitmuster + Enhanced-PIDs) | offen |
| P5 | Optional: LTE-Modem + IoT-SIM für Live-Sync unterwegs | offen |
