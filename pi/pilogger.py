#!/usr/bin/env python3
"""pilogger – carlog-Daemon für den Pi im Auto.

Liest OBD2 (ELM327, seriell oder TCP) + GPS (NMEA), puffert alles in einer
lokalen SQLite-Datei und synchronisiert per Batch, sobald carlog erreichbar
ist (Heim-WLAN oder optional LTE). Nur Python-Stdlib; für serielle Ports wird
pyserial gebraucht (apt install python3-serial).

Beispiele:
    # am Pi (echte Hardware):
    python3 pilogger.py --obd-port /dev/ttyUSB0 --gps-port /dev/ttyACM0 \
        --url http://TOWER-IP:8077 --verbose

    # ohne Auto, gegen den Simulator:
    python3 pilogger.py --obd-port tcp://127.0.0.1:35000 --url http://127.0.0.1:8079 --once --verbose

Alle Optionen auch per Umgebungsvariable: PILOG_OBD_PORT, PILOG_GPS_PORT,
PILOG_GPS_BAUD, PILOG_DB, PILOG_URL, PILOG_DEVICE, PILOG_TOKEN, PILOG_INTERVAL,
PILOG_GPS_INTERVAL, PILOG_SYNC_INTERVAL.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from urllib import request as urlrequest

SCHEMA = """
CREATE TABLE IF NOT EXISTS gps_buf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL,
    speed REAL, altitude REAL, device TEXT);
CREATE INDEX IF NOT EXISTS idx_gpsbuf_ts ON gps_buf(ts);
CREATE TABLE IF NOT EXISTS obd_buf (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL, session TEXT, device TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_obdbuf_ts ON obd_buf(ts);
"""


def log(msg):
    print("%s %s" % (datetime.now(tz=timezone.utc).strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- ELM327 ---
class ElmOBD:
    """ELM327 über seriell (/dev/ttyUSB0) oder TCP (tcp://host:port)."""

    ECHO = {"01": "41", "02": "42", "09": "49", "22": "62", "21": "61"}

    def __init__(self, portstr, verbose=False):
        self.portstr = portstr
        self.verbose = verbose
        self.tcp = None
        self.ser = None
        self.buf = b""
        if portstr.startswith("tcp://"):
            host, _, port = portstr[6:].partition(":")
            self.tcp = (host, int(port))
        self.mode_echo = "41"  # wird je Abfrage gesetzt

    def _open(self):
        if self.tcp:
            import socket
            self.sock = socket.create_connection(self.tcp, timeout=6)
            self.sock.settimeout(6)
        else:
            try:
                import serial
            except ImportError:
                raise RuntimeError("pyserial fehlt: apt install python3-serial (oder pip install pyserial)")
            self.ser = serial.Serial(self.portstr, 38400, timeout=6)

    def _write(self, raw):
        data = (raw + "\r").encode("ascii")
        if self.ser:
            self.ser.write(data)
        else:
            self.sock.sendall(data)

    def _read(self, timeout=5):
        """Lesen bis '>' oder Timeout; Rückgabe: dekodierter Text."""
        buf = b"" if not self.ser else b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ser:
                chunk = self.ser.read(256)
            else:
                self.sock.settimeout(max(0.2, deadline - time.time()))
                try:
                    chunk = self.sock.recv(256)
                except OSError:
                    chunk = b""
            if chunk:
                buf += chunk
                if b">" in buf:
                    break
        return buf.decode("ascii", "ignore")

    def cmd(self, raw, timeout=5):
        self._write(raw)
        text = self._read(timeout)
        lines = []
        for ln in text.replace(">", "").split("\r"):
            ln = ln.strip()
            if ln and ln.upper() != raw.upper():  # Echo ausblenden
                lines.append(ln)
        return lines

    def connect(self):
        self._open()
        self._read(3)  # Connect-Banner verwerfen
        for init in ("ATZ", "ATE0", "ATL0", "ATS0", "ATSP0"):
            out = self.cmd(init, timeout=6 if init == "ATZ" else 3)
            if self.verbose:
                log("ELM init %s -> %s" % (init, out))
            if init == "ATZ" and not any("ELM" in ln.upper() for ln in out):
                raise RuntimeError("kein ELM327: %r" % out)
        return True

    def poll(self, pid):
        """PID abfragen (z. B. '010C' oder '221074'); Rückgabe: Hex-Datenstring oder None."""
        mode = pid[:2]
        echo = self.ECHO.get(mode, "4" + mode[1])
        out = self.cmd(pid, timeout=5)
        for ln in out:
            ls = ln.replace(" ", "").upper()
            if ls.startswith("NO") or ls == "?" or "ERROR" in ls:
                return None
            if ls.startswith(echo) and len(ls) > len(pid):
                data = ls[len(pid):]
                if data and all(c in "0123456789ABCDEF" for c in data):
                    return data
        return None

    def voltage(self):
        for ln in self.cmd("ATRV", timeout=4):
            v = ln.strip().upper().replace("V", "")
            try:
                return float(v)
            except ValueError:
                continue
        return None

    def close(self):
        try:
            if self.ser:
                self.ser.close()
            if self.tcp and hasattr(self, "sock"):
                self.sock.close()
        except OSError:
            pass


def parse_pid(rec, data):
    """Payload-Hex -> physikalischer Wert (deklarative Tabelle, CANgauge-Muster)."""
    try:
        b = bytes.fromhex(data)
    except (ValueError, TypeError):
        return None
    if len(b) < rec["bytes"]:
        return None
    raw = 0
    for i in range(rec["bytes"]):
        raw = (raw << 8) | b[i]
    v = raw * rec["scale"] + rec["offset"]
    v = round(v, rec["dec"])
    return int(v) if rec["dec"] == 0 else v


SCAN_BASE = [("0100", 0x01), ("0120", 0x21), ("0140", 0x41), ("0160", 0x61)]


def scan_pids(obd):
    """Abholtag-Check: Support-Bitmuster (Mode 01, PIDs 00/20/40/60) lesen und
    unterstuetzte Standard-PIDs dekodieren (Muster: CANgauge prv_get_available_pids).
    Plus Probe der Toyota-Enhanced-PIDs (am echten Auto unbestaetigt)."""
    supported = []
    log("-- PID-Support-Scan (Abholtag-Check) --")
    for pid, base in SCAN_BASE:
        raw = obd.poll(pid)
        if not raw:
            log("  %s: keine Antwort" % pid)
            continue
        try:
            b = bytes.fromhex(raw)
        except ValueError:
            log("  %s: ungueltig %r" % (pid, raw))
            continue
        if len(b) < 4:
            log("  %s: zu kurz (%s)" % (pid, raw))
            continue
        bits = int.from_bytes(b[:4], "big")
        log("  %s -> %08X" % (pid, bits))
        for bit in range(32):
            if bits >> (31 - bit) & 1:
                code = "01%02X" % (base + bit)
                rec = BY_PID.get(code)
                supported.append(code)
                print("  %s unterstuetzt (%s)" % (code, rec["key"] if rec else "-"))
    print("  gesamt unterstuetzt: %d" % len(supported))
    log("-- Enhanced-Probe (am echten Auto unbestaetigt) --")
    for code in ("221074", "221638", "221021"):
        raw = obd.poll(code)
        rec = BY_PID[code]
        v = parse_pid(rec, raw) if raw else None
        print("  %s (%s): %s" % (code, rec["key"], v if v is not None else "keine Antwort"))
    return supported


# ------------------------------------------------------------------ GPS ---
class GpsReader:
    """NMEA vom u-blox-Stick; nur RMC (Position/Speed) + GGA (Höhe)."""

    def __init__(self, portstr, baud=9600, verbose=False):
        self.portstr = portstr
        self.baud = baud
        self.verbose = verbose
        self.ser = None
        self.fix = None  # (ts, lat, lon, speed_kmh, altitude)

    def connect(self):
        try:
            import serial
        except ImportError:
            raise RuntimeError("pyserial fehlt: apt install python3-serial")
        self.ser = serial.Serial(self.portstr, self.baud, timeout=2)
        return True

    @staticmethod
    def _deg(raw, hemi):
        if not raw:
            return None
        try:
            v = float(raw)
        except ValueError:
            return None
        deg = int(v // 100) + (v % 100) / 60.0
        if hemi in ("S", "W"):
            deg = -deg
        return deg

    def poll(self):
        if not self.ser:
            return self.fix
        end = time.time() + 1.2
        while time.time() < end:
            try:
                line = self.ser.readline().decode("ascii", "ignore").strip()
            except (OSError, UnicodeDecodeError):
                break
            if not line or not line.startswith("$"):
                continue
            p = line.split("*")[0].split(",")
            try:
                if p[0].endswith("RMC") and p[2] == "A":
                    lat = self._deg(p[3], p[4])
                    lon = self._deg(p[5], p[6])
                    if lat is None or lon is None:
                        continue
                    kmh = round(float(p[7]) * 1.852, 1) if p[7] else None
                    alt = self.fix[4] if self.fix else None
                    self.fix = (int(time.time()), lat, lon, kmh, alt)
                elif p[0].endswith("GGA") and p[6] in ("1", "2"):
                    alt = float(p[9]) if p[9] else None
                    if self.fix:
                        self.fix = (self.fix[0], self.fix[1], self.fix[2], self.fix[3], alt)
            except (IndexError, ValueError):
                continue
        return self.fix

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except OSError:
                pass


# --------------------------------------------------------------- Puffer ---
class Buffer:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.c = sqlite3.connect(path)
        self.c.row_factory = sqlite3.Row
        self.c.executescript(SCHEMA)
        self.c.commit()

    def add_gps(self, ts, lat, lon, speed, altitude, device):
        self.c.execute("INSERT INTO gps_buf (ts, lat, lon, speed, altitude, device) VALUES (?,?,?,?,?,?)",
                       (ts, lat, lon, speed, altitude, device))
        self.c.commit()

    def add_obd(self, ts, device, data, session=None):
        self.c.execute("INSERT INTO obd_buf (ts, session, device, data) VALUES (?,?,?,?)",
                       (ts, session, device, json.dumps(data, ensure_ascii=False)))
        self.c.commit()

    def counts(self):
        g = self.c.execute("SELECT COUNT(*) n FROM gps_buf").fetchone()["n"]
        o = self.c.execute("SELECT COUNT(*) n FROM obd_buf").fetchone()["n"]
        return g, o

    def take(self, limit=500):
        gps = [dict(r) for r in self.c.execute(
            "SELECT * FROM gps_buf ORDER BY ts LIMIT ?", (limit,))]
        obd = [dict(r) for r in self.c.execute(
            "SELECT * FROM obd_buf ORDER BY ts LIMIT ?", (limit,))]
        return gps, obd

    def drop(self, gps_ids, obd_ids):
        if gps_ids:
            self.c.executemany("DELETE FROM gps_buf WHERE id = ?", [(i,) for i in gps_ids])
        if obd_ids:
            self.c.executemany("DELETE FROM obd_buf WHERE id = ?", [(i,) for i in obd_ids])
        self.c.commit()

    def close(self):
        self.c.close()


# --------------------------------------------------------------- Sync -----
def sync(buf, cfg):
    """Puffer an carlog schieben; True, wenn leergezogen (oder nichts da)."""
    try:
        urlrequest.urlopen(cfg.url.rstrip("/") + "/api/health", timeout=4)
    except OSError:
        return False  # offline -> später erneut
    for _ in range(40):
        gps, obd = buf.take(500)
        if not gps and not obd:
            return True
        payload = {
            "gps": [{"lat": r["lat"], "lon": r["lon"], "timestamp": r["ts"],
                     "speed": r["speed"], "altitude": r["altitude"],
                     "device": r["device"]} for r in gps],
            "obd": [{"time": r["ts"] * 1000, "session": r["session"],
                     "device": r["device"], "data": json.loads(r["data"])} for r in obd],
        }
        req = urlrequest.Request(
            cfg.url.rstrip("/") + "/api/ingest/batch",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        if cfg.token:
            req.add_header("X-Carlog-Token", cfg.token)
        resp = urlrequest.urlopen(req, timeout=15)
        body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            log("Sync-Antwort: %r" % body)
            return False
        buf.drop([r["id"] for r in gps], [r["id"] for r in obd])
        g, o = buf.counts()
        if cfg.verbose:
            log("Sync: %d GPS + %d OBD gesendet (Rest: %d/%d)" % (len(gps), len(obd), g, o))
        if g == 0 and o == 0:
            return True
    return True


# ---------------------------------------------------------------- Main ----
# Deklarative PID-Tabelle (Muster: CANgauge-base can_obd_def.c) —
# Decode: value = raw(Big-Endian, bytes) * scale + offset, gerundet auf dec.
# Enhanced-PIDs (221074/221638/221021) sind community-reverse-engineert und am
# echten Auto NOCH NICHT bestaetigt (Abholtag-Check: --scan-pids).
PID_TABLE = [
    {"pid": "0104", "key": "load",      "bytes": 1, "scale": 100/255, "offset": 0,   "dec": 1, "unit": "%"},
    {"pid": "0105", "key": "coolant",   "bytes": 1, "scale": 1,       "offset": -40, "dec": 0, "unit": "C"},
    {"pid": "010B", "key": "map",       "bytes": 1, "scale": 1,       "offset": 0,   "dec": 0, "unit": "kPa"},
    {"pid": "010C", "key": "rpm",       "bytes": 2, "scale": 0.25,    "offset": 0,   "dec": 0, "unit": "rpm"},
    {"pid": "010D", "key": "speed",     "bytes": 1, "scale": 1,       "offset": 0,   "dec": 1, "unit": "km/h"},
    {"pid": "010F", "key": "intake",    "bytes": 1, "scale": 1,       "offset": -40, "dec": 0, "unit": "C"},
    {"pid": "0111", "key": "throttle",  "bytes": 1, "scale": 100/255, "offset": 0,   "dec": 1, "unit": "%"},
    {"pid": "012F", "key": "fuel",      "bytes": 1, "scale": 100/255, "offset": 0,   "dec": 1, "unit": "%"},
    {"pid": "221074", "key": "oil_bar", "bytes": 2, "scale": 1/1280,  "offset": 0,   "dec": 2, "unit": "bar"},
    {"pid": "221638", "key": "gear_c",  "bytes": 2, "scale": 1/256,   "offset": -40, "dec": 1, "unit": "C"},
    {"pid": "221021", "key": "bplus",   "bytes": 1, "scale": 0.1,     "offset": 0,   "dec": 1, "unit": "V"},
]
BY_PID = {rec["pid"]: rec for rec in PID_TABLE}


def cycle(obd, gps, buf, cfg, st):
    """Eine Messrunde; st hält miss-Zähler + Schlafmodus."""
    if obd:
        data = {}
        rpm = None
        raw = obd.poll("010C")
        if raw:
            rpm = parse_pid(BY_PID["010C"], raw)
        if rpm and rpm > 0:
            st["miss"] = 0
            for rec in PID_TABLE:
                d = obd.poll(rec["pid"])
                v = parse_pid(rec, d) if d else None
                if v is not None:
                    data[rec["key"]] = v
            volt = obd.voltage()
            if volt is not None:
                data["voltage"] = volt
            if data:
                buf.add_obd(int(time.time()), cfg.device, data)
                if cfg.verbose:
                    log("OBD: %s" % data)
        else:
            st["miss"] += 1
            if st["miss"] >= 3 and not st["sleeping"]:
                st["sleeping"] = True
                log("Motor aus (RPM %s) -> Schlafmodus mit ATRV-Wache" % rpm)
            if st["sleeping"]:
                volt = obd.voltage()
                if volt is not None and volt >= 13.5:
                    st["sleeping"] = False
                    st["miss"] = 0
                    log("Ladespannung %.1f V -> aufwachen" % volt)
    if gps:
        fix = gps.poll()
        if fix and fix[1] is not None:
            ts, lat, lon, kmh, alt = fix
            buf.add_gps(ts, lat, lon, kmh, alt, cfg.device)
            if cfg.verbose:
                log("GPS: %.4f,%.4f %.0f km/h" % (lat, lon, kmh or 0))


def main():
    ap = argparse.ArgumentParser(description="carlog Pi-Daemon (OBD + GPS -> Batch-Sync)")
    ap.add_argument("--obd-port", default=os.environ.get("PILOG_OBD_PORT", "none"))
    ap.add_argument("--gps-port", default=os.environ.get("PILOG_GPS_PORT", "none"))
    ap.add_argument("--gps-baud", type=int, default=int(os.environ.get("PILOG_GPS_BAUD", "9600")))
    ap.add_argument("--db", default=os.environ.get("PILOG_DB", "puffer.db"))
    ap.add_argument("--url", default=os.environ.get("PILOG_URL", "http://192.168.1.210:8077"))
    ap.add_argument("--device", default=os.environ.get("PILOG_DEVICE", "GRYaris"))
    ap.add_argument("--token", default=os.environ.get("PILOG_TOKEN", ""))
    ap.add_argument("--interval", type=float, default=float(os.environ.get("PILOG_INTERVAL", "5")))
    ap.add_argument("--sync-interval", type=float, default=float(os.environ.get("PILOG_SYNC_INTERVAL", "60")))
    ap.add_argument("--once", action="store_true", help="eine Runde + Sync, dann Ende (Tests)")
    ap.add_argument("--status", action="store_true", help="nur Pufferstand zeigen")
    ap.add_argument("--scan-pids", action="store_true", help="Abholtag-Check: PID-Support-Bitmuster + Enhanced-Probe")
    ap.add_argument("--verbose", action="store_true")
    cfg = ap.parse_args()
    cfg.gps_interval = float(os.environ.get("PILOG_GPS_INTERVAL", "1"))

    buf = Buffer(cfg.db)
    if cfg.status:
        g, o = buf.counts()
        print("Puffer: %d GPS, %d OBD (%s)" % (g, o, cfg.db))
        return
    if cfg.scan_pids:
        if not cfg.obd_port or cfg.obd_port == "none":
            print("--scan-pids braucht --obd-port")
            return
        obd = ElmOBD(cfg.obd_port, cfg.verbose)
        try:
            obd.connect()
            scan_pids(obd)
        finally:
            obd.close()
        return

    obd = None
    if cfg.obd_port and cfg.obd_port != "none":
        obd = ElmOBD(cfg.obd_port, cfg.verbose)
    gps = None
    if cfg.gps_port and cfg.gps_port != "none":
        gps = GpsReader(cfg.gps_port, cfg.gps_baud, cfg.verbose)

    try:
        if obd:
            obd.connect()
            log("OBD verbunden: %s" % cfg.obd_port)
        if gps:
            gps.connect()
            log("GPS verbunden: %s" % cfg.gps_port)
    except (OSError, RuntimeError) as exc:
        log("Verbindungsfehler: %s" % exc)
        if not cfg.once:
            sys.exit(1)

    st = {"miss": 0, "sleeping": False, "last_sync": 0.0}
    try:
        while True:
            cycle(obd, gps, buf, cfg, st)
            if time.time() - st["last_sync"] >= cfg.sync_interval:
                if sync(buf, cfg):
                    g, o = buf.counts()
                    if cfg.verbose or cfg.once:
                        log("Sync ok (Puffer: %d GPS, %d OBD)" % (g, o))
                    st["last_sync"] = time.time()
                else:
                    if cfg.verbose:
                        log("carlog nicht erreichbar -> puffern")
            if cfg.once:
                g, o = buf.counts()
                log("Ende (--once): Puffer %d GPS, %d OBD" % (g, o))
                break
            time.sleep(cfg.interval)
    except KeyboardInterrupt:
        log("gestoppt")
    finally:
        if obd:
            obd.close()
        if gps:
            gps.close()
        buf.close()


if __name__ == "__main__":
    main()
