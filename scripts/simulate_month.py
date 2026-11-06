#!/usr/bin/env python3
"""simulate_month.py — erzeugt 1 Monat realistischer Fahrdaten und schickt sie
an die carlog-API. Fahrprofil nach deutschen Mittelwerten:

  DAT Report 2025/2026: 13.140 km/Jahr (= ~1.095 km/Monat); Arbeitsweg 36 %,
  privat 59 %, geschaeftlich 5 %.
  infas MiD 2023: werktags 1,8 Fahrten/Pkw (27,8 km/Tag), Wochenende 23 km/Tag,
  60 % Nutzung werk-, 44 % wochentags.
  Fahrzeug: Toyota GR Yaris 1.6T — WLTP 8,2 l/100 km, 50-l-Tank, Super Plus.

Sensorik-Modell:
  - Kaltstart: Kuehlmittel = Aussentemp, Drehzahl 1100-1350 fuer 60-90 s,
    Oel kalt -> hoeherer Druck (dickes Oel), Getriebe waermt langsam auf ~52 C.
  - Warm: Leerlauf 750-880 rpm, cruise rpm ~ speed*23 (6. Gang),
    Oeldruck warm Leerlauf 0,9-1,2 bar, skaliert mit Drehzahl, Turbo-MAP-Spitzen
    beim Beschleunigen bis ~190 kPa, Lade-/Bordspannung 14,0-14,4 V laufend.
"""
import argparse, json, math, random, urllib.request
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=1))  # CET
DEVICE = "GRYaris"
STEP_S = 2.0  # Abtastrate OBD/GPS (Torque ~1 Hz, Pi-Daemon 5 s via PILOG_INTERVAL)
TANK_MAX_L = 50.0
BASE_CONSUMPTION = 8.2  # l/100 km WLTP
ODO_START = 42150.0

HOME = (52.3790, 9.7410)
WORK = (52.3100, 9.8300)
MALL = (52.4050, 9.7950)
LEISURE = (52.4700, 9.3300)
LONG = (52.2700, 9.2100)  # lange Ausfahrt ~55 km one-way
ERRAND = (52.3930, 9.6830)

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
    """[(t_s, kmh)] alle STEP_S; Stops (0 km/h) in Stadt-Phasen."""
    r = random.Random(seed)
    out, t, cur, stop_left = [], 0.0, 0.0, 0
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
            out.append((t, round(cur)))
            t += STEP_S
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
    else:  # errand
        seg(0.00, 1.01, 22, 45, 3)
    out.append((t, round(cur)))
    return out

def sens(t, kmh, prev, cold_s, ambient, warm):
    """Eine OBD-Messung; prev = dict mit coolant/oil_t/gear_t/intake."""
    rpm_idle_cold, rpm_idle = 1200, 810
    if kmh <= 0:
        rpm = r_idle = (rpm_idle_cold if cold_s < 75 else rpm_idle) + random.uniform(-40, 70)
        load = random.uniform(18, 28)
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
    if load < 30: map_kpa = random.uniform(28, 36)
    elif load < 65: map_kpa = random.uniform(52, 88)
    else: map_kpa = random.uniform(95, 190)
    volt = random.uniform(14.02, 14.36) if cold_s > 3 else random.uniform(12.2, 12.6)
    if kmh <= 0:
        throttle = random.uniform(3, 9)
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

def gen_month(start_date, days, seed):
    r = random.Random(seed)
    trips, odo = [], ODO_START
    start = datetime.strptime(start_date, "%Y-%m-%d")
    homeoffice = set(r.sample(range(days), 2))
    long_done = False
    for d in range(days):
        day = start + timedelta(days=d)
        wd = day.weekday()
        ambient = round(r.uniform(2.5, 9.5) if wd and d < 20 else r.uniform(4, 12), 1)
        def add(hh, mm, ttype, p1, p2):
            nonlocal odo
            km = route_distance(p1, p2)
            dur = km / (random.uniform(38, 52) / 60.0) * 60  # ~40-52 km/h Schnitt
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
        if wd == 5 and r.random() < 0.75:
            round_trip(r.randint(10, 11), r.randint(0, 55), "shopping", MALL)
        if wd == 6:
            if not long_done and d > 12 and r.random() < 0.4:
                long_done = True
                round_trip(r.randint(9, 11), r.randint(0, 59), "leisure", LONG)
            elif r.random() < 0.7:
                round_trip(r.randint(10, 14), r.randint(0, 59), "leisure", LEISURE)
        if wd < 5 and d not in homeoffice and r.random() < 0.35:
            add(19, r.randint(10, 50), "errand", HOME, ERRAND)
            add(19, r.randint(55, 59), "errand", ERRAND, HOME)
    return trips

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8077")
    ap.add_argument("--start", default="2026-10-07")
    ap.add_argument("--days", type=int, default=31)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    random.seed(a.seed)
    trips = gen_month(a.start, a.days, a.seed)

    gps_all, obd_all = [], []
    odo = ODO_START
    fillups, km_since_fill, last_fill_km = [], 0.0, ODO_START
    start_date = datetime.strptime(a.start, "%Y-%m-%d")
    for i, tr in enumerate(trips, 1):
        seed = a.seed * 1000 + i
        sp = speed_profile(tr["type"], tr["dur"], seed)
        fuel = max(6.0, 92.0 - km_since_fill * (BASE_CONSUMPTION / 100.0) / TANK_MAX_L * 100.0)
        fuel_step = tr["km"] * (BASE_CONSUMPTION / 100.0) / TANK_MAX_L * 100.0 / max(1, len(sp) - 1)
        prev = {"coolant": tr["ambient"], "oil_t": tr["ambient"],
                "gear_t": tr["ambient"], "intake": tr["ambient"], "fuel": fuel}
        route = build_route(tr["p1"], tr["p2"], tr["km"], seed)
        total_pts = len(sp)
        cum = 0.0
        cold = 0
        for t_s, kmh in sp:
            cold += STEP_S
            s, prev = sens(t_s, kmh, prev, cold, tr["ambient"], kmh > 0 or cold > 90)
            prev["fuel"] = max(4.0, prev["fuel"] - fuel_step)
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
        km_since_fill += cum
        # Tankung faellig? (rechnerisch leer bei ~480 km pro ~45 l)
        if (len(fillups) == 0 and tr is trips[0]) or km_since_fill >= 420:
            if len(fillups) == 0:
                liters = random.uniform(45.5, 47.5)  # Baseline: volles Tanken
            else:
                liters = km_since_fill * random.uniform(7.9, 8.6) / 100.0
            liters = min(TANK_MAX_L - 3.0, max(32.0, liters))
            price = round(random.uniform(1.83, 1.94), 2)
            fill_date = tr["start"]
            fillups.append({"date": fill_date.strftime("%Y-%m-%d"),
                            "odometer": round(odo, 1), "liters": round(liters, 2),
                            "price_per_l": price, "total_price": round(liters * price, 2),
                            "full": 1, "note": "Sim: Volltankung"})
            if len(fillups) > 1:
                print(f"  Tankung {fill_date:%d.%m.}: {liters:.1f} l / "
                      f"{km_since_fill:.0f} km = {liters/km_since_fill*100:.2f} l/100km "
                      f"@ {price} EUR/l")
            km_since_fill = 0.0
        odo = tr["odo_before"] + tr["km_actual"]

    total_km = sum(t["km_actual"] for t in trips)
    print(f"Fahrten: {len(trips)} | km: {total_km:.0f} | GPS: {len(gps_all)} | "
          f"OBD: {len(obd_all)} | Tankungen: {len(fillups)}")
    if a.dry_run:
        return

    def post(path, payload):
        req = urllib.request.Request(a.url + path, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)

    # Batch in 400er-Chunks
    for i in range(0, len(gps_all), 400):
        r = post("/api/ingest/batch", {"gps": gps_all[i:i+400], "obd": []})
        assert r.get("ok")
    for i in range(0, len(obd_all), 400):
        r = post("/api/ingest/batch", {"gps": [], "obd": obd_all[i:i+400]})
        assert r.get("ok")
    for f in fillups:
        post("/api/fillups", f)
    post("/api/maintenance", {"date": (start_date + timedelta(days=12)).strftime("%Y-%m-%d"),
                              "odometer": ODO_START + 320, "kind": "Oelwechsel + Filter",
                              "cost": 89.90, "note": "Sim: 10.000er-Service"})
    with urllib.request.urlopen(a.url + "/api/health", timeout=30) as resp:
        print("Ingest fertig:", json.load(resp))

if __name__ == "__main__":
    main()
