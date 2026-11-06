#!/usr/bin/env python3
"""simulate_month.py — erzeugt realistische Fahrdaten (Standard: 1 Jahr) und schickt
sie an die carlog-API. Fahrprofil nach deutschen Mittelwerten:

  DAT Report 2025/2026: 13.140-15.000 km/Jahr; Arbeitsweg 36 %, privat 59 %.
  infas MiD 2023: werktags 1,8 Fahrten/Pkw (27,8 km/Tag), Wochenende 23 km/Tag.
  Fahrzeug: Toyota GR Yaris 1.6T — WLTP 8,2 l/100 km, 50-l-Tank, Super Plus.
  plus ~1 Roadtrip/Monat (Autobahn, Hamburg/Bremen/Baltic/Kassel, 240-320 km rund)
  -> Ziel ~15.000 km/Jahr.

Autobahn + Vollgas:
  Roadtrips: Stadt -> Autobahn-Cruise 118-142 km/h -> Stadt. Alle ~90-150 s ein
  Vollgas-Burst (8-16 s, Beschleunigung 9-14 km/h pro 2-s-Schritt auf 165-185).
  WOT in sens(): Runterziehen -> rpm bis ~6200, throttle 95-100 %, load 95-100,
  MAP 180-220 kPa. Verbrauch je Fahrmodus: Stadt 8,8 / Autobahn 7,6 /
  Vollgas 16,5 l/100km, Leerlauf 0,9 l/h -> der Tankstands-Verlauf zeigt
  Vollgas-Passagen (die Karten-Metrik "Verbrauch" faerbt sie rot).

Tanken:
  Trigger: Tank < 25 % -> 65 % voll (bis 92 %), 35 % teilweis (bis 45-70 %).
  Tankfuellungen der letzten PENDING_DAYS (28) Tage bekommen KEINEN Preis in der
  fillups-Tabelle -> detect_fillups() erkennt die Spruenge und legt sie als
  ausstehend an (Preis-Nachtrag im WebUI). Aeltere Tankungen: manuelle Eintraege
  mit Preis (Voll-zu-Voll-Verbrauch fuellbar).

Sensorik-Modell:
  - Kaltstart: Kuehlmittel = Aussentemp, Drehzahl 1100-1350 fuer 60-90 s,
    Oel kalt -> hoeherer Druck, Getriebe waermt auf ~52 C.
  - Warm: Leerlauf 750-880 rpm, cruise rpm ~ speed*23 (6. Gang),
    Oeldruck warm Leerlauf 0,9-1,2 bar skaliert mit Drehzahl, MAP-Spitzen bis ~220 kPa.
"""
import argparse, json, math, random, urllib.request
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=1))  # CET
DEVICE = "GRYaris"
STEP_S = 2.0  # Abtastrate OBD/GPS (Torque ~1 Hz, Pi-Daemon 5 s via PILOG_INTERVAL)
TANK_MAX_L = 50.0
ODO_START = 42150.0
PENDING_DAYS = 28     # Tankungen juenger -> kein Preis (ausstehend, Nachtrag im WebUI)
FUEL_TRIGGER_PCT = 25.0
FUEL_FLOOR_PCT = 3.0
L100_CITY, L100_ABW, L100_WOT, L100_IDLE = 8.8, 7.6, 16.5, 0.9

HOME = (52.3790, 9.7410)
def _far(p, f):
    """Ziel vom HOME aus weiter weg schieben (Faktor f)."""
    return (HOME[0] + (p[0] - HOME[0]) * f, HOME[1] + (p[1] - HOME[1]) * f)
WORK = _far((52.3100, 9.8300), 1.40)
MALL = _far((52.4050, 9.7950), 1.40)
LEISURE = _far((52.4700, 9.3300), 1.40)
LONG = _far((52.2700, 9.2100), 1.40)   # lange Ausfahrt ~77 km one-way
ERRAND = _far((52.3930, 9.6830), 1.40)
# Autobahn-Roadtrips (140-180 km one-way -> 280-360 km rund, reicht aus einer Tankung)
ROADS = [_far(p, 1.15) for p in [(53.5510, 9.9930),   # Hamburg ~176 km
                                  (53.0790, 8.8010),   # Bremen ~137 km
                                  (52.8800, 11.5000),  # Baltic/Wendland ~180 km
                                  (51.3130, 9.4930)]]  # Kassel ~163 km

def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 2 * 6371.0 * math.asin(math.sqrt(h))

def route_distance(p1, p2, factor=1.18):
    return haversine_km(p1, p2) * factor  # Strassenfaktor

def lerp(a, b, f):
    return a + (b - a) * f

def build_route(p1, p2, total_km, seed):
    """Punkte entlang der Route (endpunktnah, sinus-Jitter quer)."""
    r = random.Random(seed)
    n = max(3, int(total_km / 0.18))  # ~alle 180 m
    pts = []
    for i in range(n + 1):
        f = i / n
        lat = lerp(p1[0], p2[0], f) + math.sin(f * math.pi * 2 + seed % 7) * 0.0040
        lon = lerp(p1[1], p2[1], f) + math.cos(f * math.pi * 2 + seed % 5) * 0.0055
        pts.append((lat, lon))
    return pts

def speed_profile(trip_type, dur_s, seed):
    """[(t_s, kmh, wot)] alle STEP_S; Stops (0 km/h) in Stadt-Phasen,
    wot=True fuer Vollgas-Schritte (Autobahn-Bursts)."""
    r = random.Random(seed)
    out, t, cur = [], 0.0, 0.0
    stop_left, wot_left = 0, 0

    def push(wot=False):
        nonlocal t
        out.append((t, round(cur), wot))
        t += STEP_S

    def seg(f0, f1, lo, hi, stops):
        nonlocal t, cur, stop_left
        t1 = dur_s * f1
        while t < t1:
            if stops and cur > 0 and stop_left == 0 and r.random() < 0.009:
                stop_left = r.randint(4, 20)  # Ampel: 8-40 s stehen
            if stop_left > 0:
                cur = 0.0
                stop_left -= 1
            elif cur == 0 and t > 2:
                cur = r.uniform(lo, hi) * 0.5
            else:
                target = r.uniform(lo, hi)
                cur = max(0.0, min(cur + r.uniform(-6, 8), target))
            push()
    if trip_type in ("work_to", "work_from"):
        seg(0.00, 0.20, 28, 48, 2)      # Stadt (Start/Stopp)
        seg(0.20, 0.80, 62, 95, 0)      # Landstrasse
        seg(0.80, 1.01, 28, 50, 2)      # Stadt (Ankunft)
    elif trip_type == "shopping":
        seg(0.00, 1.01, 22, 48, 5)
    elif trip_type == "leisure":
        seg(0.00, 0.15, 25, 45, 2)
        seg(0.15, 0.85, 68, 100, 0)
        seg(0.85, 1.01, 25, 48, 1)
    elif trip_type == "roadtrip":
        seg(0.00, 0.08, 25, 48, 1)      # Stadt raus
        t1 = dur_s * 0.90
        while t < t1:                    # Autobahn mit Vollgas-Bursts
            if wot_left > 0:
                cur = min(185.0, cur + r.uniform(9, 14))  # ~1.4-2 m/s^2
                wot_left -= 1
                push(wot=True)
            elif cur > 150.0:            # Ausrollen nach Vollgas
                cur = max(115.0, cur - r.uniform(6, 10))
                push()
            else:
                if wot_left == 0 and t > 40 and r.random() < 0.010:
                    wot_left = r.randint(4, 8)  # 8-16 s Vollgas
                target = r.uniform(118, 142)
                cur = max(80.0, min(cur + r.uniform(-4, 5), target))
                push()
        seg(0.90, 1.01, 25, 50, 1)      # Stadt rein
    else:  # errand
        seg(0.00, 1.01, 22, 45, 3)
    out.append((t, round(cur), False))
    return out

def sens(t, kmh, prev, cold_s, ambient, warm, wot=False):
    """Eine OBD-Messung; prev = dict mit coolant/oil_t/gear_t/intake."""
    rpm_idle_cold, rpm_idle = 1200, 810
    if kmh <= 0:
        rpm = (rpm_idle_cold if cold_s < 75 else rpm_idle) + random.uniform(-40, 70)
        load = random.uniform(18, 28)
    elif wot:
        rpm = min(6200, kmh * 23 * random.uniform(1.70, 1.95))  # Runterziehen
        load = random.uniform(95, 100)
    else:
        rpm = kmh * 23 * random.uniform(0.97, 1.09)
        if cold_s < 75: rpm += random.uniform(150, 400)
        rpm = min(max(rpm, 1100), 3400)
        load = min(100, 32 + kmh * 0.28 + random.uniform(-6, 26))
        if random.random() < 0.016:  # Beschleunigungsphase
            rpm = min(3400, rpm + random.uniform(500, 1000))
            load = min(100, load + random.uniform(20, 45))
    # Temperaturen
    coolant = prev["coolant"]
    if coolant < 90 and warm:
        coolant = min(90.5, coolant + random.uniform(0.22, 0.34))  # ~7-10 C/min
    elif coolant < 88:
        coolant = min(88.5, coolant + random.uniform(0.18, 0.28))
    else:
        # Thermostat haelt 88-92: mean reversion statt Random-Walk-Drift
        coolant = max(88.8, min(91.5, coolant + random.uniform(-0.05, 0.05)))
    coolant += random.uniform(-0.01, 0.01)
    oil_t = min(prev["oil_t"] + random.uniform(0.044, 0.080), max(coolant - 4, 20))
    gear_t = min(prev["gear_t"] + random.uniform(0.036, 0.064), 52.0 + random.uniform(0, 2))
    intake = min(prev["intake"] + random.uniform(0.024, 0.044), ambient + random.uniform(9, 15))
    # Oeldruck: warm Leerlauf 0,9-1,2; mit Drehzahl; kalt deutlich hoeher
    base = 0.95 + rpm / 1400.0 * (0.85 if rpm > 1000 else 0.0)
    if rpm <= 1000: base = 0.95 + random.uniform(-0.08, 0.25)
    cold_extra = max(0.0, (32.0 - oil_t) / 32.0) * 2.2
    oil_bar = max(0.4, base + cold_extra + random.uniform(-0.06, 0.06))
    # MAP/Ladedruck
    if wot: map_kpa = random.uniform(180, 220)
    elif load < 30: map_kpa = random.uniform(28, 36)
    elif load < 65: map_kpa = random.uniform(52, 88)
    else: map_kpa = random.uniform(95, 190)
    volt = random.uniform(14.02, 14.36) if cold_s > 3 else random.uniform(12.2, 12.6)
    if kmh <= 0:
        throttle = random.uniform(3, 9)
    elif wot:
        throttle = random.uniform(95, 100)
    else:
        throttle = min(100, max(3, load * random.uniform(0.65, 1.0)))
    fuel = prev.get("fuel")
    return {
        "rpm": round(rpm), "speed": round(kmh), "coolant": round(coolant, 1),
        "oil_bar": round(oil_bar, 2), "gear_c": round(gear_t, 1),
        "intake": round(intake, 1), "load": round(load),
        "map": round(map_kpa), "voltage": round(volt, 2),
        "bplus": round(volt - random.uniform(0.0, 0.15), 2),
        "throttle": round(throttle, 1), "fuel": round(fuel, 1) if fuel is not None else None,
    }, {"coolant": coolant, "oil_t": oil_t, "gear_t": gear_t, "intake": intake, "fuel": fuel}

# Profil-Durchschnittsgeschwindigkeiten je Fahrttyp (km/h) — das Timing muss die
# TATSACHELICHE Fahrtdauer nutzen, sonst startet die Rueckfahrt vor dem Ende der
# Hinfahrt und die GPS-Punkte ping-pong-en zwischen den Zielen (Januar-Fix).
TYPE_AVG = {"work_to": 58, "work_from": 58, "shopping": 30,
            "errand": 30, "leisure": 70, "roadtrip": 115}

def gen_year(start_date, days, seed):
    r = random.Random(seed)
    trips, odo = [], ODO_START
    start = datetime.strptime(start_date, "%Y-%m-%d")
    homeoffice = set(r.sample(range(days), int(days * 0.06)))
    last_road_day = -99
    for d in range(days):
        day = start + timedelta(days=d)
        wd = day.weekday()
        ambient = round(r.uniform(2.5, 9.5) if wd and d < 20 else r.uniform(4, 12), 1)
        def add(hh, mm, ttype, p1, p2):
            nonlocal odo
            km = route_distance(p1, p2)
            # Tatsaechliche Fahrtdauer aus der Profil-Geschwindigkeit des Typs
            dur = km / TYPE_AVG[ttype] * 3600.0
            start_local = day.replace(hour=hh, minute=mm, second=0, tzinfo=TZ)
            trips.append({"type": ttype, "start": start_local, "dur": int(dur),
                          "km": km, "p1": p1, "p2": p2, "ambient": ambient,
                          "odo_before": odo})
            odo += km
            return dur
        def round_trip(hh, mm, ttype, dest):
            dur = add(hh, mm, ttype, HOME, dest)
            pause = r.randint(15, 90)
            t0 = int(hh * 60 + mm + dur / 60.0 + pause)  # dur ist Sekunden!
            add(t0 // 60 % 24, t0 % 60, ttype, dest, HOME)
        if wd < 5 and d not in homeoffice:
            add(7, r.randint(22, 36), "work_to", HOME, WORK)
            add(17, r.randint(5, 35), "work_from", WORK, HOME)
        if wd == 5 and r.random() < 0.78:
            round_trip(r.randint(10, 11), r.randint(0, 55), "shopping", MALL)
        if wd == 6:
            if d - last_road_day >= 35 and r.random() < 0.8:
                # Roadtrip: Autobahn, Hamburg/Bremen/Baltic/Kassel
                last_road_day = d
                round_trip(r.randint(8, 10), r.randint(0, 59), "roadtrip", r.choice(ROADS))
            elif r.random() < 0.58:
                round_trip(r.randint(10, 14), r.randint(0, 59), "leisure", LEISURE)
            elif d - last_road_day >= 12 and r.random() < 0.20:
                last_road_day = d
                round_trip(r.randint(9, 11), r.randint(0, 59), "leisure", LONG)
        if wd < 5 and d not in homeoffice and r.random() < 0.40:
            # round_trip statt fester Slots: die Rueckfahrt startet erst NACH dem
            # Ende der Hinfahrt (feste Slots ueberlappten bei spaetem Start ->
            # GPS-Ping-Pong zwischen Hin- und Rueckroute)
            round_trip(19, r.randint(10, 40), "errand", ERRAND)
    return trips

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8077")
    ap.add_argument("--start", default="2025-11-07")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    random.seed(a.seed)
    trips = gen_year(a.start, a.days, a.seed)

    today = datetime.now(TZ).date()
    pending_since = today - timedelta(days=PENDING_DAYS)

    gps_all, obd_all = [], []
    odo = ODO_START
    fillups = []          # manuelle Eintraege (nur aelter als PENDING_DAYS)
    pending_count = 0     # juengere Tankungen: Sprung in den OBD-Daten -> detect_fillups()
    fuel_level = 92.0     # Tankstand-Kette (echter Verlauf statt Rechen-Naeherung)
    waiting_fill = None   # (liters, target, full) — getankt wird vor der naechsten Fahrt
    start_date = datetime.strptime(a.start, "%Y-%m-%d")
    for i, tr in enumerate(trips, 1):
        # Wartende Tankung vor DIESER Fahrt? -> Tanktag = Tag dieser Fahrt
        if waiting_fill is not None:
            liters, target, full = waiting_fill
            fill_date = tr["start"]
            older = fill_date.date() < pending_since
            price = round(random.uniform(1.78, 1.99), 2)
            if older:
                fillups.append({"date": fill_date.strftime("%Y-%m-%d"),
                                "odometer": round(tr["odo_before"], 1),
                                "liters": round(liters, 2),
                                "price_per_l": price, "total_price": round(liters * price, 2),
                                "full": full,
                                "note": "Sim: Volltankung" if full else "Sim: Teilfuellung"})
            else:
                pending_count += 1
            kind = "voll" if full else "teil (%.0f %%)" % target
            print(f"  Tankung {fill_date:%d.%m.}: {liters:.1f} l ({kind}, "
                  f"{'@ %.2f EUR/l' % price if older else '-- PREIS OFFEN (ausstehend)'})")
            waiting_fill = None
        seed = a.seed * 1000 + i
        # dur iterieren, bis das Speed-Profil exakt die geplante Strecke faehrt
        # (sonst faehrt das Profil schneller als die Route lang ist und Odometer/GPS
        # weichen vom Tanklog ab)
        dur = tr["dur"]
        for _ in range(3):
            sp = speed_profile(tr["type"], dur, seed)
            raw = sum(k * STEP_S for _, k, _ in sp) / 3600.0
            if raw <= 0:
                break
            f = tr["km"] / raw
            if abs(f - 1.0) < 0.02:
                break
            dur = max(60, int(dur * f))
        sp = speed_profile(tr["type"], dur, seed)
        prev = {"coolant": tr["ambient"], "oil_t": tr["ambient"],
                "gear_t": tr["ambient"], "intake": tr["ambient"], "fuel": fuel_level}
        route = build_route(tr["p1"], tr["p2"], tr["km"], seed)
        cum = 0.0
        cold = 0
        for t_s, kmh, wot in sp:
            cold += STEP_S
            s, prev = sens(t_s, kmh, prev, cold, tr["ambient"], kmh > 0 or cold > 90, wot)
            # Verbrauch je Fahrmodus (l/100km -> % Tank pro Schritt)
            if wot: l100 = L100_WOT
            elif kmh >= 100: l100 = L100_ABW
            elif kmh > 0: l100 = L100_CITY
            else: l100 = L100_IDLE
            if kmh > 0:
                drop = l100 / 100.0 / TANK_MAX_L * 100.0 * (kmh * STEP_S / 3600.0)
            else:
                drop = L100_IDLE / 3600.0 * STEP_S / TANK_MAX_L * 100.0
            prev["fuel"] = max(FUEL_FLOOR_PCT, prev["fuel"] - drop)
            ts = int(tr["start"].timestamp()) + int(t_s)
            f = min(1.0, (t_s / sp[-1][0]) if sp[-1][0] else 0)
            idx = min(len(route) - 1, int(f * (len(route) - 1)))
            la, lo = route[idx]
            gps_all.append({"lat": round(la, 5), "lon": round(lo, 5), "timestamp": ts,
                            "speed": round(kmh * random.uniform(0.96, 1.04)),
                            "altitude": round(55 + math.sin(f * 6) * 12, 1),
                            "device": DEVICE})
            obd_all.append({"time": ts * 1000, "session": f"sim-{i:03d}-{tr['type']}",
                            "device": DEVICE, "data": s})
            if t_s > 0 and len(sp) > 1:
                cum += kmh * STEP_S / 3600.0
        tr["km_actual"] = cum
        # Tankung faellig? Tank < 25 % -> voll (65 %) oder teilweis (35 %).
        # Getankt wird VOR der naechsten Fahrt -> Eintrag mit deren Datum.
        if prev["fuel"] < FUEL_TRIGGER_PCT:
            target = 92.0 if random.random() < 0.65 else random.uniform(45, 70)
            full = 1 if target >= 88 else 0
            liters = (target - prev["fuel"]) / 100.0 * TANK_MAX_L
            waiting_fill = (liters, target, full)
            fuel_level = target
        else:
            fuel_level = prev["fuel"]  # Tank-Kette weiterfuehren (sonst startet jeder Tag bei 92 %)
        odo = tr["odo_before"] + tr["km_actual"]

    total_km = sum(t["km_actual"] for t in trips)
    print(f"Fahrten: {len(trips)} | km: {total_km:.0f} | GPS: {len(gps_all)} | "
          f"OBD: {len(obd_all)} | Tankungen: {len(fillups)} manuell + "
          f"{pending_count} ausstehend (Preis offen)")
    if a.dry_run:
        return

    def post(path, payload):
        req = urllib.request.Request(a.url + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.load(resp)

    # Batch in 2000er-Chunks (Endpunkt max 5000/Liste)
    for i in range(0, len(gps_all), 2000):
        r = post("/api/ingest/batch", {"gps": gps_all[i:i+2000], "obd": []})
        assert r.get("ok"), f"GPS-Chunk {i} fehlgeschlagen"
        if (i // 2000) % 20 == 0:
            print(f"  GPS {i}/{len(gps_all)}")
    for i in range(0, len(obd_all), 2000):
        r = post("/api/ingest/batch", {"gps": [], "obd": obd_all[i:i+2000]})
        assert r.get("ok"), f"OBD-Chunk {i} fehlgeschlagen"
        if (i // 2000) % 20 == 0:
            print(f"  OBD {i}/{len(obd_all)}")
    for f in fillups:
        post("/api/fillups", f)
    post("/api/maintenance", {"date": (start_date + timedelta(days=140)).strftime("%Y-%m-%d"),
                              "odometer": ODO_START + 8000, "kind": "Inspektion",
                              "cost": 349.90, "note": "Sim: Jaehrliche-Inspektion"})
    post("/api/maintenance", {"date": (start_date + timedelta(days=40)).strftime("%Y-%m-%d"),
                              "odometer": ODO_START + 2300, "kind": "Oelwechsel + Filter",
                              "cost": 89.90, "note": "Sim: Oelwechsel"})
    with urllib.request.urlopen(a.url + "/api/health", timeout=60) as resp:
        print("Ingest fertig:", json.load(resp))
    print(f"WICHTIG: Server neu starten (frischer _FUEL_SCAN), dann health() -> "
          f"{pending_count} Tankungen werden als ausstehend erkannt.")

if __name__ == "__main__":
    main()
