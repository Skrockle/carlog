#!/usr/bin/env python3
"""
carlog - selbst gehosteter Fahrzeug-Tracker.

Tank-/Wartungslog (Voll-zu-Voll-Verbrauch, EUR/100 km, Monatswerte) plus
automatische Ingest-Endpunkte fuer GPS-Logger-Apps (OsmAnd-Protokoll) und
OBD2-Apps (Torque-Webserver-Format). Nur Python-Stdlib + SQLite, kein
Cloud-Bezug - alle Daten liegen in einer einzigen Datei.
"""
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

DB_PATH = os.environ.get("CARLOG_DB", "/data/carlog.db")
PORT = int(os.environ.get("CARLOG_PORT", "8077"))
VEHICLE = os.environ.get("CARLOG_VEHICLE", "Toyota GR Yaris")
TOKEN = os.environ.get("CARLOG_TOKEN", "").strip()
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TZ_OFFSET_HOURS = float(os.environ.get("CARLOG_TZ_OFFSET", "1"))
MAX_GPS_JUMP_KM = 150.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS fillups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    odometer REAL NOT NULL,
    liters REAL NOT NULL,
    total_price REAL,
    price_per_l REAL,
    full INTEGER NOT NULL DEFAULT 1,
    note TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    odometer REAL,
    kind TEXT NOT NULL,
    cost REAL,
    note TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gps_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    speed REAL,
    altitude REAL,
    device TEXT
);
CREATE INDEX IF NOT EXISTS idx_gps_ts ON gps_points(ts);
CREATE TABLE IF NOT EXISTS obd_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    session TEXT,
    device TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obd_ts ON obd_readings(ts);
"""


def db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    # WAL: parallele Lesezugriffe (z. B. Grafana) blockieren die Schreibseite nicht
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    c = db()
    try:
        c.executescript(SCHEMA)
        c.commit()
    finally:
        c.close()


def month_of_date(date_str):
    return (date_str or "")[:7]


def month_of_ts(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)
    return dt.strftime("%Y-%m")


def norm_ts(raw, default=None):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v > 1e12:
        v /= 1000.0
    return int(v)


def fill_cost(row):
    if row["total_price"] is not None:
        return float(row["total_price"])
    if row["price_per_l"] is not None:
        return float(row["price_per_l"]) * float(row["liters"])
    return 0.0


def fillups_with_stats():
    c = db()
    try:
        rows = c.execute("SELECT * FROM fillups ORDER BY date, odometer, id").fetchall()
    finally:
        c.close()

    items = [dict(r) for r in rows]
    prev_any = prev_full = None
    total_cost = 0.0
    for it in items:
        it["full"] = bool(it["full"])
        it["l_100"] = None
        it["e_100"] = None
        it["km"] = None
        it["cost"] = round(fill_cost(it), 2)
        total_cost += it["cost"]
        if prev_any is not None and it["odometer"] > prev_any["odometer"]:
            it["km"] = round(it["odometer"] - prev_any["odometer"], 1)
        if it["full"]:
            if prev_full is not None:
                delta = it["odometer"] - prev_full["odometer"]
                if delta > 0:
                    it["l_100"] = round(it["liters"] * 100.0 / delta, 2)
                    it["e_100"] = round(it["cost"] * 100.0 / delta, 2)
            prev_full = it
        prev_any = it

    overall = {
        "fills": len(items),
        "total_cost": round(total_cost, 2),
        "total_liters": round(sum(it["liters"] for it in items), 2),
        "total_km": None,
        "avg_l_100": None,
        "avg_e_100": None,
        "first_date": items[0]["date"] if items else None,
        "last_date": items[-1]["date"] if items else None,
        "last_odometer": items[-1]["odometer"] if items else None,
    }
    if len(items) >= 2:
        overall["total_km"] = round(items[-1]["odometer"] - items[0]["odometer"], 1)
    fulls = [it for it in items if it["full"]]
    if len(fulls) >= 2:
        km = fulls[-1]["odometer"] - fulls[0]["odometer"]
        liters = sum(it["liters"] for it in fulls[1:])
        cost = sum(it["cost"] for it in fulls[1:])
        if km > 0:
            overall["avg_l_100"] = round(liters * 100.0 / km, 2)
            overall["avg_e_100"] = round(cost * 100.0 / km, 2)
    return {"items": items, "overall": overall}


def monthly_from_fillups():
    data = fillups_with_stats()
    months = {}
    for it in data["items"]:
        m = month_of_date(it["date"])
        mm = months.setdefault(m, {"month": m, "km": 0.0, "liters": 0.0,
                                   "cost": 0.0, "fills": 0})
        mm["liters"] += it["liters"]
        mm["cost"] += it["cost"]
        mm["fills"] += 1
        if it["km"]:
            mm["km"] += it["km"]
    out = []
    for m in sorted(months):
        mm = months[m]
        mm["l_100"] = round(mm["liters"] * 100.0 / mm["km"], 2) if mm["km"] > 0 else None
        mm["km"] = round(mm["km"], 1)
        mm["liters"] = round(mm["liters"], 2)
        mm["cost"] = round(mm["cost"], 2)
        out.append(mm)
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0088
    p = math.pi / 180.0
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p)
         * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 2 * r * math.asin(math.sqrt(max(0.0, a)))


def gps_stats():
    c = db()
    try:
        rows = c.execute("SELECT ts, lat, lon FROM gps_points ORDER BY ts").fetchall()
        n = c.execute("SELECT COUNT(*) AS n FROM gps_points").fetchone()["n"]
        last = c.execute(
            "SELECT ts, lat, lon, speed FROM gps_points ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    finally:
        c.close()
    monthly, total, skipped = {}, 0.0, 0
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        d = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
        if d > MAX_GPS_JUMP_KM:
            skipped += 1
            continue
        total += d
        m = month_of_ts(b["ts"])
        monthly[m] = monthly.get(m, 0.0) + d
    return {
        "points": n,
        "skipped_jumps": skipped,
        "total_km": round(total, 1),
        "monthly_km": [{"month": m, "km": round(monthly[m], 1)} for m in sorted(monthly)],
        "last": dict(last) if last else None,
    }


def insert_fillup(body):
    date = str(body.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("date muss YYYY-MM-DD sein")
    odometer = float(body["odometer"])
    liters = float(body["liters"])
    if odometer <= 0 or liters <= 0:
        raise ValueError("odometer und liters muessen > 0 sein")
    total = float(body["total_price"]) if body.get("total_price") not in (None, "") else None
    per_l = float(body["price_per_l"]) if body.get("price_per_l") not in (None, "") else None
    if total is None and per_l is not None:
        total = round(per_l * liters, 2)
    if per_l is None and total is not None:
        per_l = round(total / liters, 3)
    full = 1 if body.get("full", True) else 0
    note = str(body.get("note") or "")[:200] or None
    c = db()
    try:
        cur = c.execute(
            "INSERT INTO fillups (date, odometer, liters, total_price, price_per_l,"
            " full, note, created) VALUES (?,?,?,?,?,?,?,?)",
            (date, odometer, liters, total, per_l, full, note, time.time()),
        )
        c.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        c.close()


def insert_maintenance(body):
    date = str(body.get("date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("date muss YYYY-MM-DD sein")
    kind = str(body.get("kind") or "").strip()
    if not kind:
        raise ValueError("kind fehlt")
    odometer = float(body["odometer"]) if body.get("odometer") not in (None, "") else None
    cost = float(body["cost"]) if body.get("cost") not in (None, "") else None
    note = str(body.get("note") or "")[:200] or None
    c = db()
    try:
        cur = c.execute(
            "INSERT INTO maintenance (date, odometer, kind, cost, note, created)"
            " VALUES (?,?,?,?,?,?)",
            (date, odometer, kind, cost, note, time.time()),
        )
        c.commit()
        return {"ok": True, "id": cur.lastrowid}
    finally:
        c.close()


def batch_ingest(body):
    """Bulk-Ingest fuer den Pi-Daemon: N GPS-Punkte + M OBD-Messungen in einem Request.

    Erwartet {"gps": [{lat, lon, timestamp, speed, altitude, device}, ...],
              "obd": [{time, session, device, data}, ...]}. Ungueltige Zeilen werden
    uebersprungen und gezaehlt (der Daemon loescht sie trotzdem aus seinem Puffer).
    """
    gps_items = body.get("gps") or []
    obd_items = body.get("obd") or []
    if not isinstance(gps_items, list) or not isinstance(obd_items, list):
        raise ValueError("gps/obd muessen Listen sein")
    if len(gps_items) > 5000 or len(obd_items) > 5000:
        raise ValueError("zu viele Eintraege (max 5000 je Liste)")

    def _fnum(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _now():
        return int(datetime.now(tz=timezone.utc).timestamp())

    gps_rows, obd_rows = [], []
    for it in gps_items:
        if not isinstance(it, dict):
            continue
        try:
            lat, lon = float(it["lat"]), float(it["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        ts = norm_ts(it.get("timestamp", it.get("time")))
        if ts is None:
            ts = _now()
        gps_rows.append((ts, lat, lon, _fnum(it.get("speed")),
                         _fnum(it.get("altitude")), str(it.get("device", "-"))[:60]))
    for it in obd_items:
        if not isinstance(it, dict):
            continue
        ts = norm_ts(it.get("time", it.get("timestamp")))
        if ts is None:
            ts = _now()
        data = it.get("data")
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False)
        else:
            data = str(data if data is not None else "{}")
        obd_rows.append((ts, it.get("session"), str(it.get("device", "-"))[:60], data))

    c = db()
    try:
        c.executemany(
            "INSERT INTO gps_points (ts, lat, lon, speed, altitude, device) VALUES (?,?,?,?,?,?)",
            gps_rows)
        c.executemany(
            "INSERT INTO obd_readings (ts, session, device, data) VALUES (?,?,?,?)",
            obd_rows)
        c.commit()
        return {
            "ok": True,
            "gps": len(gps_rows),
            "obd": len(obd_rows),
            "skipped": (len(gps_items) - len(gps_rows)) + (len(obd_items) - len(obd_rows)),
        }
    finally:
        c.close()


def list_maintenance():
    c = db()
    try:
        rows = c.execute("SELECT * FROM maintenance ORDER BY date, id").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


SERIES_KINDS = {
    "rpm": ("obd_readings", "CAST(json_extract(data, '$.rpm') AS REAL)"),
    "oil_bar": ("obd_readings", "CAST(json_extract(data, '$.oil_bar') AS REAL)"),
    "gear_c": ("obd_readings", "CAST(json_extract(data, '$.gear_c') AS REAL)"),
    "coolant": ("obd_readings", "CAST(json_extract(data, '$.coolant') AS REAL)"),
    "intake": ("obd_readings", "CAST(json_extract(data, '$.intake') AS REAL)"),
    "load": ("obd_readings", "CAST(json_extract(data, '$.load') AS REAL)"),
    "map": ("obd_readings", "CAST(json_extract(data, '$.map') AS REAL)"),
    "speed": ("obd_readings", "CAST(json_extract(data, '$.speed') AS REAL)"),
    "bplus": ("obd_readings", "CAST(json_extract(data, '$.bplus') AS REAL)"),
    "voltage": ("obd_readings", "CAST(json_extract(data, '$.voltage') AS REAL)"),
    "gps_speed": ("gps_points", "speed"),
}


def series_rows(kind, t_from, t_to):
    """Zeitreihe [(epoch_s, wert)] fuer Grafana (Infinity/JSON-Datenquelle)."""
    c = db()
    try:
        if kind == "fillup_cost":
            rows = c.execute(
                "SELECT CAST(strftime('%s', date || ' 12:00:00') AS INTEGER) AS ts, "
                "COALESCE(total_price, ROUND(price_per_l * liters, 2)) AS v "
                "FROM fillups WHERE (total_price IS NOT NULL OR price_per_l IS NOT NULL) "
                "AND date >= date(?, 'unixepoch') AND date <= date(?, 'unixepoch') ORDER BY date",
                (t_from, t_to)).fetchall()
        else:
            table, expr = SERIES_KINDS[kind]
            rows = c.execute(
                "SELECT ts, %s AS v FROM %s WHERE ts BETWEEN ? AND ? AND %s IS NOT NULL ORDER BY ts"
                % (expr, table, expr), (t_from, t_to)).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        try:
            v = float(r["v"])
        except (TypeError, ValueError):
            continue
        out.append((int(r["ts"]), v))
    return out


def delete_row(table, row_id):
    if table not in ("fillups", "maintenance"):
        raise ValueError("ungueltige Tabelle")
    c = db()
    try:
        cur = c.execute("DELETE FROM %s WHERE id = ?" % table, (row_id,))
        c.commit()
        return {"ok": True, "deleted": cur.rowcount}
    finally:
        c.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "carlog/1.0"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            self._json({"error": "bad path"}, 404)
            return
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            self._json({"error": "not found"}, 404)
            return
        ctype = ("text/html; charset=utf-8" if name.endswith(".html")
                 else "application/javascript; charset=utf-8" if name.endswith(".js")
                 else "text/plain; charset=utf-8")
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _csv(self, qs):
        kind = (qs.get("kind", ["fillups"])[0] or "fillups").lower()
        c = db()
        try:
            if kind == "fillups":
                rows = c.execute("SELECT * FROM fillups ORDER BY date, id").fetchall()
                cols = ["date", "odometer", "liters", "total_price", "price_per_l",
                        "full", "note"]
            elif kind == "maintenance":
                rows = c.execute("SELECT * FROM maintenance ORDER BY date, id").fetchall()
                cols = ["date", "odometer", "kind", "cost", "note"]
            elif kind == "gps":
                rows = c.execute("SELECT * FROM gps_points ORDER BY ts").fetchall()
                cols = ["ts", "lat", "lon", "speed", "altitude", "device"]
            elif kind == "obd":
                rows = c.execute("SELECT * FROM obd_readings ORDER BY ts").fetchall()
                cols = ["ts", "session", "device", "data"]
            else:
                self._json({"error": "unbekanntes kind"}, 400)
                return
        finally:
            c.close()
        lines = [",".join(cols)]
        for r in rows:
            vals = []
            for col in cols:
                v = r[col]
                s = "" if v is None else str(v)
                if "," in s or '"' in s or "\n" in s:
                    s = '"' + s.replace('"', '""') + '"'
                vals.append(s)
            lines.append(",".join(vals))
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         'attachment; filename="carlog_%s.csv"' % kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, qs):
        if not TOKEN:
            return True
        return (qs.get("token", [""])[0] == TOKEN
                or self.headers.get("X-Carlog-Token") == TOKEN)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            return body if isinstance(body, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def gps_ingest(self, qs):
        def fnum(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        try:
            lat = float(qs.get("lat", [None])[0])
            lon = float(qs.get("lon", [None])[0])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
        except (TypeError, ValueError):
            self._json({"error": "lat/lon fehlen oder ungueltig"}, 400)
            return
        ts = norm_ts(qs["timestamp"][0] if "timestamp" in qs else
                     (qs["time"][0] if "time" in qs else None))
        if ts is None:
            ts = int(datetime.now(tz=timezone.utc).timestamp())
        device = qs.get("id", qs.get("deviceid", qs.get("device", ["-"]))[0])
        c = db()
        try:
            c.execute(
                "INSERT INTO gps_points (ts, lat, lon, speed, altitude, device)"
                " VALUES (?,?,?,?,?,?)",
                (ts, lat, lon, fnum(qs["speed"][0] if "speed" in qs else
                                    (qs["s"][0] if "s" in qs else None)),
                 fnum(qs["altitude"][0] if "altitude" in qs else
                      (qs["alt"][0] if "alt" in qs else None)),
                 str(device)[:60]),
            )
            c.commit()
            n = c.execute("SELECT COUNT(*) AS n FROM gps_points").fetchone()["n"]
        finally:
            c.close()
        self._json({"ok": True, "points": n})

    def obd_ingest(self, qs):
        ts = norm_ts(qs["time"][0] if "time" in qs else None)
        if ts is None:
            ts = int(datetime.now(tz=timezone.utc).timestamp())
        device = qs.get("id", ["-"])[0]
        session = qs.get("session", [None])[0]
        data = {k: v[0] for k, v in qs.items()
                if k not in ("time", "session", "id", "token")}
        c = db()
        try:
            c.execute(
                "INSERT INTO obd_readings (ts, session, device, data) VALUES (?,?,?,?)",
                (ts, session, str(device)[:60], json.dumps(data, ensure_ascii=False)),
            )
            c.commit()
            n = c.execute("SELECT COUNT(*) AS n FROM obd_readings").fetchone()["n"]
        finally:
            c.close()
        self._json({"ok": True, "pids": len(data), "readings": n})

    def telemetry(self, qs):
        kind = (qs.get("kind", ["gps"])[0] or "gps").lower()
        try:
            limit = min(int(qs.get("limit", ["100"])[0]), 1000)
        except ValueError:
            limit = 100
        c = db()
        try:
            if kind == "obd":
                rows = c.execute(
                    "SELECT * FROM obd_readings ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
                items = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["data"] = json.loads(d["data"])
                    except ValueError:
                        pass
                    items.append(d)
                return {"kind": "obd", "items": items}
            rows = c.execute(
                "SELECT * FROM gps_points ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return {"kind": "gps", "items": [dict(r) for r in rows]}
        finally:
            c.close()

    def series(self, qs, path):
        kind = (qs.get("kind", ["rpm"])[0] or "rpm").lower()
        if kind not in SERIES_KINDS and kind != "fillup_cost":
            self._json({"error": "unbekanntes kind; gueltig: %s"
                        % ", ".join(sorted(SERIES_KINDS) + ["fillup_cost"])}, 400)
            return
        t_from = norm_ts(qs.get("from", [None])[0])
        t_to = norm_ts(qs.get("to", [None])[0])
        if t_from is None:
            t_from = int(time.time()) - 7 * 86400
        if t_to is None:
            t_to = int(time.time())
        rows = series_rows(kind, t_from, t_to)
        if path.endswith(".csv"):
            lines = ["time,value"]
            for ts, v in rows:
                iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                lines.append("%s,%s" % (iso, v))
            body = ("\n".join(lines) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json([{"time": ts * 1000, "value": v} for ts, v in rows])

    def health(self):
        c = db()
        try:
            return {
                "ok": True,
                "vehicle": VEHICLE,
                "fillups": c.execute("SELECT COUNT(*) n FROM fillups").fetchone()["n"],
                "maintenance": c.execute(
                    "SELECT COUNT(*) n FROM maintenance").fetchone()["n"],
                "gps_points": c.execute(
                    "SELECT COUNT(*) n FROM gps_points").fetchone()["n"],
                "obd_readings": c.execute(
                    "SELECT COUNT(*) n FROM obd_readings").fetchone()["n"],
            }
        finally:
            c.close()

    def do_GET(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        qs = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                self._file("index.html")
            elif path == "/api/health":
                self._json(self.health())
            elif path == "/api/fillups":
                self._json(fillups_with_stats())
            elif path == "/api/maintenance":
                self._json({"items": list_maintenance()})
            elif path == "/api/stats":
                self._json({
                    "vehicle": VEHICLE,
                    "fillups": fillups_with_stats()["overall"],
                    "monthly_fillups": monthly_from_fillups(),
                    "gps": gps_stats(),
                })
            elif path == "/api/export.csv":
                self._csv(qs)
            elif path == "/api/gps":
                if not self._auth(qs):
                    self._json({"error": "unauthorized"}, 401)
                else:
                    self.gps_ingest(qs)
            elif path == "/api/obd":
                if not self._auth(qs):
                    self._json({"error": "unauthorized"}, 401)
                else:
                    self.obd_ingest(qs)
            elif path == "/api/telemetry":
                self._json(self.telemetry(qs))
            elif path in ("/api/series", "/api/series.csv"):
                self.series(qs, path)
            elif path.startswith("/api/"):
                self._json({"error": "unbekannter Endpunkt"}, 404)
            else:
                self._file(path.lstrip("/") or "index.html")
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        qs = parse_qs(u.query)
        try:
            if not self._auth(qs):
                self._json({"error": "unauthorized"}, 401)
                return
            body = self._read_body()
            if path == "/api/fillups":
                self._json(insert_fillup(body))
            elif path == "/api/maintenance":
                self._json(insert_maintenance(body))
            elif path == "/api/ingest/batch":
                self._json(batch_ingest(body))
            else:
                self._json({"error": "unbekannter Endpunkt"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)

    def do_DELETE(self):
        u = urlparse(self.path)
        path = unquote(u.path)
        qs = parse_qs(u.query)
        try:
            if not self._auth(qs):
                self._json({"error": "unauthorized"}, 401)
                return
            try:
                row_id = int(qs.get("id", ["0"])[0])
            except ValueError:
                row_id = 0
            if path == "/api/fillups" and row_id:
                self._json(delete_row("fillups", row_id))
            elif path == "/api/maintenance" and row_id:
                self._json(delete_row("maintenance", row_id))
            else:
                self._json({"error": "id fehlt oder unbekannter Endpunkt"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("carlog laeuft auf 0.0.0.0:%d (Fahrzeug: %s, DB: %s)" % (PORT, VEHICLE, DB_PATH),
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


if __name__ == "__main__":
    main()
