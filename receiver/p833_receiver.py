#!/usr/bin/env python3
# P833 Call Receiver
# By Ethan Brady & Ryan Raikes
#
# Receives "call for help" messages from the P833 cell boards over the network,
# checks them, drops repeats, turns them into a spoken page and hands that to
# the pager. In dry-run mode the pager only logs what it would send.
#
# Standard library only. Works on Python 3.6 and newer (AlmaLinux 8 and 9).

import argparse
import csv
import html
import json
import logging
import os
import queue
import socketserver
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = "0.1.0"
LOG = logging.getLogger("p833")

CSV_FIELDS = ["time", "board", "cell", "press", "code", "who", "message",
              "result", "attempts", "detail"]


# ---------------------------------------------------------------- config ---

def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Codes come in as strings from JSON; key them by int.
    cfg["codes"] = {int(k): v for k, v in cfg["codes"].items()}
    for code in range(2, 10):
        if code not in cfg["codes"]:
            raise ValueError("config: code %d is missing" % code)
        for field in ("who", "zone", "en", "es"):
            if field not in cfg["codes"][code]:
                raise ValueError("config: code %d has no '%s'" % (code, field))
        zone = cfg["codes"][code]["zone"]
        if zone not in cfg["zones"]:
            raise ValueError("config: code %d uses unknown zone '%s'" % (code, zone))
    cfg.setdefault("boards", {})
    cfg.setdefault("dry_run", True)
    cfg.setdefault("repeat_window_s", 600)
    cfg.setdefault("same_call_window_s", 15)
    cfg.setdefault("board_silent_after_s", 180)
    cfg.setdefault("page_retries", 3)
    cfg.setdefault("page_retry_wait_s", 10)
    return cfg


# -------------------------------------------------------------- messages ---

def build_message(cfg, code, press):
    """Return (english, spanish) page text for this code and press."""
    c = cfg["codes"][code]
    if press == 0:
        en = c.get("en_no_press", c["en"])
        es = c.get("es_no_press", c["es"])
    else:
        en, es = c["en"], c["es"]
    return en.replace("{press}", str(press)), es.replace("{press}", str(press))


def check_call(body):
    """Check a call message. Return (call dict, None) or (None, error text)."""
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    board = body.get("board")
    if not isinstance(board, str) or not board or len(board) > 32:
        return None, "board must be a short text ID"
    out = {"board": board}
    for name, lo, hi in (("code", 2, 9), ("press", 0, 999), ("seq", 0, 2**31)):
        v = body.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return None, "%s must be a whole number %d to %d" % (name, lo, hi)
        out[name] = v
    out["boot"] = str(body.get("boot", ""))[:32]
    age = body.get("age_ms", 0)
    out["age_ms"] = age if isinstance(age, int) and 0 <= age < 86400000 else 0
    return out, None


# ----------------------------------------------------------------- state ---

class State:
    """Everything the receiver remembers. One lock guards it all."""

    def __init__(self, cfg, state_dir, log_dir):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.seen = {}        # (board, boot, seq) -> time first seen
        self.recent = {}      # (board, code, press) -> time last accepted
        self.boards = {}      # board -> info dict
        self.calls = []       # last 50 call records, newest last
        self.state_file = os.path.join(state_dir, "boards.json")
        self.csv_file = os.path.join(log_dir, "calls.csv")
        self._load()

    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.boards = json.load(f)
        except (OSError, ValueError):
            self.boards = {}
        for b in self.boards.values():
            b["silent_logged"] = False
        try:
            with open(self.csv_file, "r", encoding="utf-8", newline="") as f:
                self.calls = list(csv.DictReader(f))[-50:]
        except OSError:
            self.calls = []

    def _save_boards(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.boards, f, indent=1)
            os.replace(tmp, self.state_file)
        except OSError as e:
            LOG.warning("could not save board state: %s", e)

    def cell_name(self, board):
        return self.cfg["boards"].get(board, {}).get("cell", board + " (unknown board)")

    def heard(self, board, ip, extra=None):
        now = time.time()
        with self.lock:
            b = self.boards.setdefault(board, {})
            if b.get("silent_logged"):
                LOG.info("board %s is back after being silent", board)
            b.update({"last_heard": now, "ip": ip, "silent_logged": False})
            if extra:
                b.update(extra)
            self._save_boards()

    def is_repeat(self, call):
        """True if this exact message, or the same call, was already accepted."""
        now = time.time()
        with self.lock:
            w1 = self.cfg["repeat_window_s"]
            w2 = self.cfg["same_call_window_s"]
            self.seen = {k: t for k, t in self.seen.items() if now - t < w1}
            self.recent = {k: t for k, t in self.recent.items() if now - t < w2}
            k1 = (call["board"], call["boot"], call["seq"])
            k2 = (call["board"], call["code"], call["press"])
            if k1 in self.seen or k2 in self.recent:
                self.seen.setdefault(k1, now)
                return True
            self.seen[k1] = now
            self.recent[k2] = now
            return False

    def record(self, rec):
        """Add one line to the call log (CSV) and the recent list."""
        with self.lock:
            new = not os.path.exists(self.csv_file)
            try:
                with open(self.csv_file, "a", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow({k: rec.get(k, "") for k in CSV_FIELDS})
            except OSError as e:
                LOG.error("could not write call log: %s", e)
            self.calls.append({k: str(rec.get(k, "")) for k in CSV_FIELDS})
            self.calls = self.calls[-50:]

    def check_silent(self):
        """Log once when a board goes quiet for too long."""
        now = time.time()
        limit = self.cfg["board_silent_after_s"]
        with self.lock:
            for name, b in self.boards.items():
                if now - b.get("last_heard", 0) > limit and not b.get("silent_logged"):
                    LOG.warning("board %s (%s) not heard for over %d s",
                                name, self.cell_name(name), limit)
                    b["silent_logged"] = True


# ---------------------------------------------------------------- pagers ---

class DryRunPager:
    """Logs the page it would send. Never dials."""

    def page(self, zone, dial, en, es):
        LOG.info("DRY RUN page: zone=%s dial=%s | %s | %s", zone, dial, en, es)
        return True, "dry run, not dialed"


def make_pager(cfg):
    if cfg["dry_run"]:
        return DryRunPager()
    # The HT813 phone pager is the next build step. Until it exists, refuse to
    # start with dry_run off rather than silently not paging.
    raise SystemExit("dry_run is false but no phone pager is installed yet. "
                     "Set \"dry_run\": true in the config.")


class PageWorker(threading.Thread):
    """Sends pages one at a time (there is one phone line), with retries."""

    def __init__(self, cfg, state, pager):
        super().__init__(daemon=True)
        self.cfg, self.state, self.pager = cfg, state, pager
        self.jobs = queue.Queue()

    def run(self):
        while True:
            rec = self.jobs.get()
            code = int(rec["code"])
            zone = self.cfg["codes"][code]["zone"]
            dial = self.cfg["zones"][zone]["dial"]
            tries = self.cfg["page_retries"]
            ok, detail, n = False, "", 0
            for n in range(1, tries + 1):
                try:
                    ok, detail = self.pager.page(zone, dial, rec["en"], rec["es"])
                except Exception as e:  # a pager bug must not stop the worker
                    ok, detail = False, "error: %s" % e
                if ok:
                    break
                LOG.warning("page try %d of %d failed: %s", n, tries, detail)
                if n < tries:
                    time.sleep(self.cfg["page_retry_wait_s"])
            rec["attempts"] = n
            rec["detail"] = detail
            if ok:
                rec["result"] = "dry-run" if self.cfg["dry_run"] else "sent"
            else:
                rec["result"] = "FAILED"
                LOG.error("PAGE FAILED after %d tries: %s", n, rec["message"])
            self.state.record(rec)


# ------------------------------------------------------------ web server ---

class Handler(BaseHTTPRequestHandler):
    server_version = "P833Receiver/" + VERSION
    app = None  # set in main()

    def log_message(self, fmt, *args):  # keep the journal quiet
        pass

    def _reply(self, status, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n <= 0 or n > 4096:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _key_ok(self):
        key = self.app["key"]
        return not key or self.headers.get("X-P833-Key", "") == key

    def do_POST(self):
        if self.path not in ("/call", "/heartbeat"):
            return self._reply(404, {"ok": False, "error": "not found"})
        if not self._key_ok():
            LOG.warning("rejected %s from %s: wrong or missing key",
                        self.path, self.client_address[0])
            return self._reply(403, {"ok": False, "error": "wrong key"})
        body = self._body()
        if body is None:
            return self._reply(400, {"ok": False, "error": "bad JSON body"})
        if self.path == "/heartbeat":
            return self._heartbeat(body)
        return self._call(body)

    def _heartbeat(self, body):
        board = body.get("board")
        if not isinstance(board, str) or not board or len(board) > 32:
            return self._reply(400, {"ok": False, "error": "board missing"})
        extra = {}
        for k in ("fw", "rssi", "uptime_s", "boot"):
            if k in body:
                extra[k] = body[k] if isinstance(body[k], (int, float)) else str(body[k])[:32]
        self.app["state"].heard(board, self.client_address[0], extra)
        return self._reply(200, {"ok": True})

    def _call(self, body):
        st, cfg = self.app["state"], self.app["cfg"]
        call, err = check_call(body)
        if err:
            LOG.warning("bad call from %s: %s (%s)", self.client_address[0], err, body)
            return self._reply(400, {"ok": False, "error": err})
        st.heard(call["board"], self.client_address[0])
        cell = st.cell_name(call["board"])
        when = datetime.fromtimestamp(time.time() - call["age_ms"] / 1000.0)
        en, es = build_message(cfg, call["code"], call["press"])
        rec = {"time": when.isoformat(sep=" ", timespec="seconds"),
               "board": call["board"], "cell": cell, "press": call["press"],
               "code": call["code"], "who": cfg["codes"][call["code"]]["who"],
               "message": en, "en": en, "es": es}
        if st.is_repeat(call):
            LOG.info("repeat dropped: %s code %d press %d seq %d",
                     call["board"], call["code"], call["press"], call["seq"])
            rec.update(result="repeat dropped", attempts=0, detail="seq %d" % call["seq"])
            st.record(rec)
            return self._reply(200, {"ok": True, "repeat": True})
        if call["board"] not in cfg["boards"]:
            LOG.warning("call from unknown board %s; paging anyway", call["board"])
        LOG.info("CALL %s (%s) code %d press %d: %s",
                 call["board"], cell, call["code"], call["press"], en)
        self.app["worker"].jobs.put(rec)
        return self._reply(200, {"ok": True, "repeat": False})

    def do_GET(self):
        if self.path == "/status.json":
            return self._reply(200, status_obj(self.app))
        if self.path in ("/", "/index.html"):
            page = status_html(self.app).encode("utf-8")
            return self._reply(200, page, "text/html; charset=utf-8")
        return self._reply(404, {"ok": False, "error": "not found"})


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def status_obj(app):
    st, cfg = app["state"], app["cfg"]
    now = time.time()
    with st.lock:
        names = sorted(set(cfg["boards"]) | set(st.boards))
        boards = []
        for name in names:
            b = st.boards.get(name, {})
            last = b.get("last_heard")
            boards.append({
                "board": name, "cell": st.cell_name(name), "ip": b.get("ip", ""),
                "last_heard": datetime.fromtimestamp(last).isoformat(sep=" ", timespec="seconds") if last else "never",
                "seconds_ago": int(now - last) if last else None,
                "ok": bool(last) and now - last <= cfg["board_silent_after_s"],
                "fw": b.get("fw", ""), "rssi": b.get("rssi", ""),
            })
        calls = list(reversed(st.calls))
    return {"version": VERSION, "dry_run": cfg["dry_run"],
            "pages_waiting": app["worker"].jobs.qsize(),
            "boards": boards, "calls": calls}


def status_html(app):
    s = status_obj(app)
    e = html.escape
    rows = []
    for b in s["boards"]:
        ago = "never" if b["seconds_ago"] is None else "%d s ago" % b["seconds_ago"]
        rows.append("<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        "ok" if b["ok"] else "bad", e(b["cell"]), e(b["board"]),
                        "OK" if b["ok"] else ("NOT SEEN" if b["seconds_ago"] is None else "SILENT"), e(b["last_heard"]), e(ago),
                        e(b["ip"]), e(str(b["rssi"]))))
    crow = []
    for c in s["calls"]:
        cls = "bad" if c.get("result") == "FAILED" else ""
        crow.append("<tr class='%s'>%s</tr>" % (cls, "".join(
            "<td>%s</td>" % e(str(c.get(k, ""))) for k in
            ("time", "cell", "press", "code", "who", "message", "result", "attempts"))))
    mode = ("DRY RUN: pages are logged, not dialed" if s["dry_run"]
            else "LIVE: pages are dialed")
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10"><title>P833 Call Receiver</title>
<style>
body{font-family:sans-serif;margin:16px;background:#fff;color:#111}
table{border-collapse:collapse;margin-bottom:24px;width:100%%}
td,th{border:1px solid #bbb;padding:4px 8px;text-align:left;font-size:14px}
th{background:#eee} tr.bad td{background:#fdd} tr.ok td:nth-child(3){color:#070;font-weight:bold}
.w{overflow-x:auto}
.mode{padding:6px 10px;display:inline-block;background:%s;color:#fff;font-weight:bold}
</style></head><body>
<h1>P833 Call Receiver</h1>
<p><span class="mode">%s</span> &nbsp; version %s &nbsp; pages waiting: %d</p>
<h2>Boards</h2>
<div class=w><table><tr><th>Cell</th><th>Board</th><th>Status</th><th>Last heard</th><th>Age</th><th>IP</th><th>Wi-Fi dBm</th></tr>
%s</table></div>
<h2>Recent calls (newest first)</h2>
<div class=w><table><tr><th>Time</th><th>Cell</th><th>Press</th><th>Code</th><th>Who</th><th>Message</th><th>Result</th><th>Tries</th></tr>
%s</table></div>
<p>Refreshes every 10 s. Full log: calls.csv in the log folder.</p>
</body></html>""" % ("#b60" if s["dry_run"] else "#070", mode, VERSION,
                     s["pages_waiting"], "\n".join(rows) or "<tr><td colspan=7>none</td></tr>",
                     "\n".join(crow) or "<tr><td colspan=8>none yet</td></tr>")


# ------------------------------------------------------------------ main ---

def main(argv=None):
    p = argparse.ArgumentParser(description="P833 call receiver")
    p.add_argument("--config", default="/etc/p833-receiver/config.json")
    p.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/p833-receiver"))
    p.add_argument("--log-dir", default=os.environ.get("LOGS_DIRECTORY", "/var/log/p833-receiver"))
    p.add_argument("--dry-run", action="store_true", help="force dry run, whatever the config says")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    if args.dry_run:
        cfg["dry_run"] = True
    os.makedirs(args.state_dir, exist_ok=True)
    os.makedirs(args.log_dir, exist_ok=True)

    key = os.environ.get("P833_BOARD_KEY", "")
    if not key:
        LOG.warning("P833_BOARD_KEY is not set: any device can send calls")

    state = State(cfg, args.state_dir, args.log_dir)
    worker = PageWorker(cfg, state, make_pager(cfg))
    worker.start()
    Handler.app = {"cfg": cfg, "state": state, "worker": worker, "key": key}

    def watch():
        while True:
            time.sleep(15)
            state.check_silent()
    threading.Thread(target=watch, daemon=True).start()

    srv = Server((cfg.get("listen", "0.0.0.0"), int(cfg.get("port", 8833))), Handler)
    LOG.info("P833 receiver %s listening on %s:%s (%s)", VERSION, cfg.get("listen", "0.0.0.0"),
             cfg.get("port", 8833), "DRY RUN" if cfg["dry_run"] else "LIVE")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
