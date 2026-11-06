#!/usr/bin/env python3
"""render_preview.py — rendert docs/preview.svg aus der aktuellen carlog.db.
Nach Demo-/Sim-Daten-Aenderung ausfuehren, sonst stimmt die README-Vorschau
nicht mehr mit der DB ueberein. Nutzt die UI-Farben und de-Formatierung.
"""
import math
import sqlite3
from datetime import datetime

DB = "/home/hermes/carlog/carlog.db"
OUT = "/home/hermes/carlog/docs/preview.svg"
VEHICLE = "Toyota GR Yaris"

def de(v, dec=0, dash="–"):
    if v is None:
        return dash
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
fills = c.execute("SELECT * FROM fillups ORDER BY date, odometer, id").fetchall()
n_gps = c.execute("SELECT COUNT(*) n FROM gps_points").fetchone()["n"]
n_obd = c.execute("SELECT COUNT(*) n FROM obd_readings").fetchone()["n"]
n_mnt = c.execute("SELECT COUNT(*) n FROM maintenance").fetchone()["n"]
gpts = c.execute("SELECT ts, lat, lon FROM gps_points ORDER BY ts").fetchall()
gps_km = 0.0
for a, b in zip(gpts, gpts[1:]):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    gps_km += 2 * 6371.0 * math.asin(math.sqrt(h))
c.close()

# Voll-zu-Voll-Statistik (wie server.py: Teilfuellungs-Liter akkumulieren,
# km vom vorherigen VOLL-Tanken — sonst kippen die Werte auf 3-22 l/100km)
rows = []
prev_full = None
liters_acc = cost_acc = 0.0
span_lit = span_cost = 0.0
first_full_odo = last_full_odo = None
for f in fills:
    lit, odo = f["liters"], f["odometer"]
    cost = f["total_price"] if f["total_price"] is not None else (f["price_per_l"] or 0) * (lit or 0)
    pending = f["total_price"] is None and f["price_per_l"] is None
    km = l_100 = e_100 = None
    if not pending and odo is not None and prev_full is not None and odo > prev_full["odometer"]:
        km = odo - prev_full["odometer"]
        l_100 = (liters_acc + lit) / km * 100
        e_100 = (cost_acc + cost) / km * 100
    rows.append({"date": f["date"], "odometer": odo, "delta": km, "liters": lit,
                 "eul": f["price_per_l"], "cost": cost, "l_100": l_100,
                 "e_100": e_100, "note": f["note"] or ""})
    if pending or odo is None:
        continue
    is_first_full = f["full"] and first_full_odo is None
    if not is_first_full and first_full_odo is not None:
        span_lit += lit
        span_cost += cost
    if f["full"]:
        if is_first_full:
            first_full_odo = odo
        else:
            last_full_odo = odo
        prev_full = {"odometer": odo}
        liters_acc = cost_acc = 0.0
    else:
        liters_acc += lit
        cost_acc += cost
total_km = (last_full_odo - first_full_odo) if first_full_odo is not None and last_full_odo is not None else 0
total_lit = sum(r["liters"] for r in rows)
total_cost = sum(r["cost"] for r in rows)
avg_l = span_lit / total_km * 100 if total_km > 0 else 0
avg_e = span_cost / total_km * 100 if total_km > 0 else 0
valid = [r for r in rows if r["l_100"] is not None]

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;")

# Chart-Skala
vals = [r["l_100"] for r in valid]
lo, hi = min(vals), max(vals)
pad = (hi - lo) * 0.15 or 0.1
lo_s, hi_s = lo - pad, hi + pad
def cy(v):
    return 444 - (v - lo_s) / (hi_s - lo_s) * (444 - 262)
def cx(i):
    return 86 + i / max(1, len(rows) - 1) * 852

grid = [lo_s + (hi_s - lo_s) * k / 3 for k in range(4)]
S = []
H = 568 + len(rows) * 28 + 44
S.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="980" height="{H}" viewBox="0 0 980 {H}" font-family="-apple-system, \'Segoe UI\', system-ui, sans-serif">')
S.append('<rect width="980" height="720" rx="14" fill="#0f1216"/>')
S.append('<rect x="0.5" y="0.5" width="979" height="719" rx="13.5" fill="none" stroke="#242c36"/>')
S.append('<text x="26" y="42" font-size="20" font-weight="700" fill="#e8ecf1">🚗 carlog</text>')
S.append(f'<text x="136" y="42" font-size="13" fill="#8b96a5">{VEHICLE}</text>')
S.append(f'<text x="954" y="42" font-size="12" fill="#8b96a5" text-anchor="end">· {len(fills)} Tankungen · {n_gps} GPS-Punkte · {n_obd} OBD-Messungen</text>')
for x, w, label, active in ((26, 72, "Tanken", 0), (106, 80, "Wartung", 0), (194, 86, "Statistik", 1), (288, 94, "Automatik", 0)):
    stroke = ' stroke="#ff5252" stroke-width="1.5"' if active else ' stroke="#242c36" stroke-width="1"'
    fill = '#e8ecf1' if active else '#8b96a5'
    S.append(f'<rect x="{x}" y="60" width="{w}" height="30" rx="9" fill="#171c23"{stroke}/>')
    S.append(f'<text x="{x + w/2:.1f}" y="79" font-size="13" fill="{fill}" text-anchor="middle">{label}</text>')
cards = [("km gesamt (Tanklog)", de(total_km)), ("Ø l/100 km", de(avg_l, 2)),
         ("Ø €/100 km", de(avg_e, 2)), ("Kosten gesamt", f"{de(total_cost, 2)} €"),
         ("km gesamt (GPS)", de(gps_km)), ("GPS-Punkte", de(n_gps))]
for i, (label, val) in enumerate(cards):
    x, y = (26 + (i % 3) * 328), (106 + (i // 3) * 62)
    S.append(f'<rect x="{x}" y="{y}" width="300" height="52" rx="12" fill="#171c23" stroke="#242c36"/>')
    S.append(f'<text x="{x + 16}" y="{y + 20}" font-size="11" fill="#8b96a5">{label}</text>')
    S.append(f'<text x="{x + 16}" y="{y + 42}" font-size="19" font-weight="700" fill="#e8ecf1">{val}</text>')
S.append('<rect x="26" y="236" width="928" height="230" rx="12" fill="#171c23" stroke="#242c36"/>')
S.append('<text x="42" y="260" font-size="11" fill="#8b96a5" letter-spacing="0.06em">VERBRAUCH L/100 KM (VOLL-ZU-VOLL)</text>')
S.append('<rect x="858" y="251" width="10" height="10" rx="2" fill="#ff5252"/>')
S.append('<text x="874" y="260" font-size="11" fill="#8b96a5">l/100 km</text>')
for g in grid:
    S.append(f'<line x1="86" y1="{cy(g):.1f}" x2="938" y2="{cy(g):.1f}" stroke="#242c36"/>')
    S.append(f'<text x="78" y="{cy(g) + 4:.1f}" font-size="10" fill="#8b96a5" text-anchor="end">{de(g, 1)}</text>')
pts = " ".join(f"{cx(i):.1f},{cy(r['l_100']):.1f}" for i, r in enumerate(rows) if r["l_100"] is not None)
S.append(f'<polyline fill="none" stroke="#ff5252" stroke-width="2.5" points="{pts}"/>')
for i, r in enumerate(rows):
    if r["l_100"] is not None:
        S.append(f'<circle cx="{cx(i):.1f}" cy="{cy(r["l_100"]):.1f}" r="3.5" fill="#ff5252"/>')
    S.append(f'<text x="{cx(i):.1f}" y="462" font-size="10" fill="#8b96a5" text-anchor="middle">{r["date"]}</text>')
S.append('<rect x="26" y="482" width="928" height="208" rx="12" fill="#171c23" stroke="#242c36"/>')
S.append('<text x="42" y="506" font-size="11" fill="#8b96a5" letter-spacing="0.06em">VERLAUF · VOLL-ZU-VOLL</text>')
cols = [(42, "start", "Datum"), (210, "end", "km"), (282, "end", "Δ km"), (352, "end", "Liter"),
        (414, "end", "€/l"), (474, "end", "€"), (554, "end", "l/100km"), (644, "end", "€/100km"),
        (684, "start", "Notiz"), (938, "end", "✕")]
for x, anc, label in cols:
    S.append(f'<text x="{x}" y="534" font-size="12.5" font-weight="600" fill="#8b96a5" text-anchor="{anc}">{label}</text>')
S.append('<line x1="42" y1="544" x2="938" y2="544" stroke="#242c36"/>')
for i, r in enumerate(rows):
    y = 568 + i * 28
    if i:
        S.append(f'<line x1="42" y1="{y - 10}" x2="938" y2="{y - 10}" stroke="#1d242e"/>')
    vals = [r["date"], de(r["odometer"]), de(r["delta"], 1), de(r["liters"], 2),
            de(r["eul"], 3), de(r["cost"], 2), de(r["l_100"], 2), de(r["e_100"], 2),
            r["note"] or "", "✕"]
    for (x, anc, _), v in zip(cols, vals):
        col = "#e8ecf1" if anc == "start" or anc == "end" and x in (210, 352, 474, 554) else "#8b96a5"
        col = "#e8ecf1" if x in (42, 210, 282, 352, 414, 474, 554, 644) else "#8b96a5"
        S.append(f'<text x="{x}" y="{y}" font-size="13" fill="{col}" text-anchor="{anc}">{esc(v)}</text>')
S.append(f'<text x="954" y="{H - 12}" font-size="10" fill="#4a5563" text-anchor="end">Beispieldaten · Vorschau</text>')
S.append('</svg>')
open(OUT, "w").write("\n".join(S) + "\n")
print(f"preview.svg geschrieben: {len(fills)} Tankungen, {avg_l:.2f} l/100km, {total_km:.0f} km")
