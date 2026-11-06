#!/usr/bin/env python3
"""elm327-sim – ELM327-Adapter + GR Yaris als TCP-Simulator.

Emuliert einen ELM327 am CAN-Bus eines GR Yaris, damit sich die ganze Kette
(pilogger -> carlog) ohne echtes Auto testen laesst. Standard-PIDs plus die
community-reverse-engineerten Toyota-Enhanced-PIDs (Header 700/7C0).

Nutzung:
    python3 test/elm327-sim.py                    # 127.0.0.1:35000, Motor an
    ELM_SIM_RUNNING=0 python3 test/elm327-sim.py  # Motor aus (RPM 0, 12 V)

Der Daemon verbindet sich mit PILOG_OBD_PORT=tcp://127.0.0.1:35000.
"""
import os
import random
import socketserver
import threading

HOST = os.environ.get("ELM_SIM_HOST", "127.0.0.1")
PORT = int(os.environ.get("ELM_SIM_PORT", "35000"))
RUNNING = os.environ.get("ELM_SIM_RUNNING", "1") != "0"

state = {"rpm": 1726.0, "speed": 48.0, "coolant": 44.0, "intake": 25.0,
         "load": 31.0, "throttle": 14.5, "fuel": 39.0, "map": 98.0,
         "oil_bar": 1.0, "gear_c": 55.0, "bplus": 14.2, "volt": 13.9}
lock = threading.Lock()


def drift():
    """Werte leicht schwanken lassen (random walk)."""
    with lock:
        if RUNNING:
            state["rpm"] = min(2600, max(760, state["rpm"] + random.uniform(-40, 40)))
            state["speed"] = min(62, max(38, state["speed"] + random.uniform(-2, 2)))
            state["oil_bar"] = min(1.4, max(0.7, state["oil_bar"] + random.uniform(-0.03, 0.03)))
            state["gear_c"] = min(62, max(48, state["gear_c"] + random.uniform(-0.4, 0.4)))
            state["coolant"] = min(92, state["coolant"] + random.uniform(0, 0.2))
        else:
            state["rpm"] = 0.0
            state["speed"] = 0.0
            state["volt"] = max(11.9, state["volt"] - 0.001)


def h(n, width=2):
    return format(int(round(n)), "0%dX" % width)


def handle(line):
    """Eine ELM-Zeile beantworten; Rueckgabe ohne Echo."""
    line = line.strip()
    if not line:
        return ""
    up = line.upper()
    if up.startswith("AT"):
        if up == "ATZ":
            return "ELM327 v1.5"
        if up == "ATRV":
            drift()
            with lock:
                return "%.1fV" % state["volt"]
        return "OK"
    drift()
    with lock:
        if up == "0100":
            return "4100BE3FA813"
        if up == "0120":
            return "412001000000"
        if up == "0104":
            return "4104" + h(state["load"] * 255 / 100)
        if up == "0105":
            return "4105" + h(state["coolant"] + 40)
        if up == "010B":
            return "410B" + h(state["map"])
        if up == "010C":
            v = int(state["rpm"] * 4)
            return "410C" + h(v >> 8) + h(v & 0xFF)
        if up == "010D":
            return "410D" + h(state["speed"])
        if up == "010F":
            return "410F" + h(state["intake"] + 40)
        if up == "0111":
            return "4111" + h(state["throttle"] * 255 / 100)
        if up == "012F":
            return "412F" + h(state["fuel"] * 255 / 100)
        if up == "221074":   # Oeldruck, Header 700
            v = int(state["oil_bar"] * 1280)
            return "621074" + h(v >> 8) + h(v & 0xFF)
        if up == "221638":   # Getriebeoel-Temp, Header 700
            v = int((state["gear_c"] + 40) * 256)
            return "621638" + h(v >> 8) + h(v & 0xFF)
        if up == "221021":   # +B-Spannung, Header 7C0
            return "621021" + h(state["bplus"] * 10)
    return "NO DATA"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        buf = b""
        self.request.sendall(b"ELM327 v1.5\r\r>")
        while True:
            try:
                data = self.request.recv(256)
            except OSError:
                break
            if not data:
                break
            buf += data
            while True:
                idx_r, idx_n = buf.find(b"\r"), buf.find(b"\n")
                cands = [i for i in (idx_r, idx_n) if i != -1]
                if not cands:
                    break
                idx = min(cands)
                line, buf = buf[:idx], buf[idx + 1:]
                try:
                    out = handle(line.decode("ascii", "ignore"))
                except Exception:
                    out = "NO DATA"
                self.request.sendall((out + "\r>").encode("ascii"))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    print("elm327-sim auf %s:%d (Motor %s)" % (HOST, PORT, "AN" if RUNNING else "AUS"), flush=True)
    Server((HOST, PORT), Handler).serve_forever()
