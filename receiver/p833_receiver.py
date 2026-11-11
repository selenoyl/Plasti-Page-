#!/usr/bin/env python3
# P833 Call Receiver
# By Ethan Brady & Ryan Raikes
#
# Receives "call for help" messages from the P833 cell boards over the network,
# checks them, drops repeats, turns them into a spoken page and hands that to
# the pager. In dry-run mode the pager only logs what it would send.
#
# Web pages:
#   /        status (open to anyone on the network, read-only)
#   /admin   send test calls, add and remove boards (needs the admin password)
#
# Standard library only. Works on Python 3.6 and newer (AlmaLinux 8 and 9).

import argparse
import base64
import csv
import hmac
import html
import json
import logging
import os
import queue
import re
import socketserver
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit

VERSION = "0.2.0"
LOG = logging.getLogger("p833")

CSV_FIELDS = ["time", "board", "cell", "press", "code", "who", "message",
              "result", "attempts", "detail"]
BOARD_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


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


def now_text(t=None):
    return datetime.fromtimestamp(t or time.time()).isoformat(sep=" ", timespec="seconds")


# ----------------------------------------------------------------- state ---

class State:
    """Everything the receiver remembers. One lock guards it all.

    The board list lives in <state dir>/board_list.json and is edited on the
    admin page. On the very first start it is filled from the config's "boards".
    """

    def __init__(self, cfg, state_dir, log_dir):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.seen = {}        # (board, boot, seq) -> time first seen
        self.recent = {}      # (board, code, press) -> time last accepted
        self.heard_info = {}  # board -> last heard, ip, fw, rssi ...
        self.registry = {}    # board -> {"cell": name}
        self.calls = []       # last 50 call records, newest last
        self.ui_seq = 0
        self.heard_file = os.path.join(state_dir, "boards.json")
        self.registry_file = os.path.join(state_dir, "board_list.json")
        self.settings_file = os.path.join(state_dir, "settings.json")
        self.csv_file = os.path.join(log_dir, "calls.csv")
        self._load()

    def set_dry_run(self, on, who=""):
        with self.lock:
            self.dry_run = bool(on)
            self._write_json(self.settings_file, {"dry_run": self.dry_run})
        LOG.warning("dry run turned %s on the admin page (%s)%s", "ON" if on else "OFF", who,
                    "" if on else ": PAGES WILL BE DIALED")

    def _load(self):
        # Dry run: the admin page's choice wins; the config value is the default.
        settings = self._read_json(self.settings_file) or {}
        self.dry_run = bool(settings.get("dry_run", self.cfg["dry_run"]))
        self.heard_info = self._read_json(self.heard_file) or {}
        for b in self.heard_info.values():
            b["silent_logged"] = False
        reg = self._read_json(self.registry_file)
        if reg is None:
            reg = {k: {"cell": v.get("cell", k)} for k, v in self.cfg["boards"].items()}
            self.registry = reg
            self._write_json(self.registry_file, reg)
            LOG.info("board list started from config: %s", ", ".join(sorted(reg)) or "none")
        else:
            self.registry = reg
        try:
            with open(self.csv_file, "r", encoding="utf-8", newline="") as f:
                self.calls = list(csv.DictReader(f))[-50:]
        except OSError:
            self.calls = []

    @staticmethod
    def _read_json(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write_json(path, data):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, path)
            return True
        except OSError as e:
            LOG.warning("could not save %s: %s", path, e)
            return False

    def cell_name(self, board):
        return self.registry.get(board, {}).get("cell", board + " (unknown board)")

    # -- board list (admin page)
    def add_board(self, board, cell):
        with self.lock:
            existed = board in self.registry
            self.registry[board] = {"cell": cell}
            self._write_json(self.registry_file, self.registry)
        LOG.info("board %s %s as '%s' (admin page)", board, "renamed" if existed else "added", cell)
        return existed

    def remove_board(self, board):
        with self.lock:
            found = self.registry.pop(board, None) is not None
            found = self.heard_info.pop(board, None) is not None or found
            self._write_json(self.registry_file, self.registry)
            self._write_json(self.heard_file, self.heard_info)
        if found:
            LOG.info("board %s removed (admin page)", board)
        return found

    # -- calls and heartbeats
    def heard(self, board, ip, extra=None):
        with self.lock:
            b = self.heard_info.setdefault(board, {})
            if b.get("silent_logged"):
                LOG.info("board %s is back after being silent", board)
            b.update({"last_heard": time.time(), "ip": ip, "silent_logged": False})
            if extra:
                b.update(extra)
            self._write_json(self.heard_file, self.heard_info)

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

    def next_ui_seq(self):
        with self.lock:
            self.ui_seq += 1
            return self.ui_seq

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
            for name, b in self.heard_info.items():
                if now - b.get("last_heard", 0) > limit and not b.get("silent_logged"):
                    LOG.warning("board %s (%s) not heard for over %d s",
                                name, self.cell_name(name), limit)
                    b["silent_logged"] = True


def accept_call(app, call, ip, note=""):
    """Handle one checked call from a board or the admin page.

    Returns (repeat: bool, english text)."""
    st, cfg = app["state"], app["cfg"]
    if ip:
        st.heard(call["board"], ip)
    cell = st.cell_name(call["board"])
    en, es = build_message(cfg, call["code"], call["press"])
    rec = {"time": now_text(time.time() - call["age_ms"] / 1000.0),
           "board": call["board"], "cell": cell, "press": call["press"],
           "code": call["code"], "who": cfg["codes"][call["code"]]["who"],
           "message": en, "en": en, "es": es, "note": note,
           "zone": cfg["codes"][call["code"]]["zone"]}
    if st.is_repeat(call):
        LOG.info("repeat dropped: %s code %d press %d seq %d",
                 call["board"], call["code"], call["press"], call["seq"])
        rec.update(result="repeat dropped", attempts=0,
                   detail=(note + "; " if note else "") + "seq %d" % call["seq"])
        st.record(rec)
        return True, en
    if call["board"] not in st.registry:
        LOG.warning("call from unknown board %s; paging anyway", call["board"])
    LOG.info("CALL %s (%s) code %d press %d: %s%s", call["board"], cell,
             call["code"], call["press"], en, " [%s]" % note if note else "")
    app["worker"].jobs.put(rec)
    return False, en


# ---------------------------------------------------------------- pagers ---

class DryRunPager:
    """Logs the page it would send. Never dials."""

    def page(self, zone, dial, en, es):
        LOG.info("DRY RUN page: zone=%s dial=%s | %s | %s", zone, dial, en, es)
        return True, "dry run, not dialed"


class NotBuiltPager:
    """Stands in for the HT813 phone pager until that step is built.

    With dry run off, every page fails loudly (logged FAILED) instead of
    silently going nowhere."""
    built = False

    def page(self, zone, dial, en, es):
        return False, "phone pager not built yet (HT813 step); nothing was dialed"


def make_live_pager(cfg):
    return NotBuiltPager()


class PageWorker(threading.Thread):
    """Sends pages one at a time (there is one phone line), with retries.

    Dry run is checked for each page, so the admin switch takes effect at once."""

    def __init__(self, cfg, state, live_pager, dry_pager=None):
        super().__init__(daemon=True)
        self.cfg, self.state = cfg, state
        self.live, self.dry = live_pager, dry_pager or DryRunPager()
        self.jobs = queue.Queue()

    def run(self):
        while True:
            rec = self.jobs.get()
            zone = rec["zone"]
            dial = self.cfg["zones"][zone]["dial"]
            dry = self.state.dry_run
            pager = self.dry if dry else self.live
            tries = self.cfg["page_retries"]
            ok, detail, n = False, "", 0
            for n in range(1, tries + 1):
                try:
                    ok, detail = pager.page(zone, dial, rec["en"], rec["es"])
                except Exception as e:  # a pager bug must not stop the worker
                    ok, detail = False, "error: %s" % e
                if ok:
                    break
                LOG.warning("page try %d of %d failed: %s", n, tries, detail)
                if n < tries:
                    time.sleep(self.cfg["page_retry_wait_s"])
            rec["attempts"] = n
            rec["detail"] = (rec["note"] + "; " if rec.get("note") else "") + detail
            if ok:
                rec["result"] = "dry-run" if dry else "sent"
            else:
                rec["result"] = "FAILED"
                LOG.error("PAGE FAILED after %d tries: %s", n, rec["message"])
            self.state.record(rec)
            self.jobs.task_done()


# ------------------------------------------------------------ web server ---

class Handler(BaseHTTPRequestHandler):
    server_version = "P833Receiver/" + VERSION
    app = None  # set in main()

    def log_message(self, fmt, *args):  # keep the journal quiet
        pass

    def _reply(self, status, obj, ctype="application/json", headers=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _raw_body(self, limit=4096):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)

    def _json_body(self):
        raw = self._raw_body()
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _key_ok(self):
        key = self.app["key"]
        return not key or hmac.compare_digest(self.headers.get("X-P833-Key", ""), key)

    # -- admin password (HTTP Basic; any user name, the password is checked)
    def _admin_ok(self):
        pw = self.app["admin_password"]
        if not pw:
            return True
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)[1]
        except (ValueError, IndexError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(given, pw)

    def _ask_password(self):
        return self._reply(401, b"Admin password needed.", "text/plain",
                           {"WWW-Authenticate": 'Basic realm="P833 receiver admin"'})

    def _same_site(self):
        """Refuse admin form posts that come from another web site."""
        host = self.headers.get("Host", "")
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if v:
                return urlsplit(v).netloc == host
        return True

    def _back(self, msg):
        return self._reply(303, b"", "text/plain", {"Location": "/admin?msg=" + quote(msg)})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/admin/"):
            return self._admin_post(path)
        if path not in ("/call", "/heartbeat"):
            return self._reply(404, {"ok": False, "error": "not found"})
        if not self._key_ok():
            LOG.warning("rejected %s from %s: wrong or missing key", path, self.client_address[0])
            return self._reply(403, {"ok": False, "error": "wrong key"})
        body = self._json_body()
        if body is None:
            return self._reply(400, {"ok": False, "error": "bad JSON body"})
        if path == "/heartbeat":
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
        call, err = check_call(body)
        if err:
            LOG.warning("bad call from %s: %s (%s)", self.client_address[0], err, body)
            return self._reply(400, {"ok": False, "error": err})
        repeat, _ = accept_call(self.app, call, self.client_address[0])
        return self._reply(200, {"ok": True, "repeat": repeat})

    def _admin_post(self, path):
        if not self._admin_ok():
            return self._ask_password()
        if not self._same_site():
            return self._reply(403, b"Form came from another site.", "text/plain")
        raw = self._raw_body()
        form = {k: v[0].strip() for k, v in parse_qs((raw or b"").decode("utf-8", "replace")).items()}
        st = self.app["state"]
        who = self.client_address[0]

        if path == "/admin/test":
            try:
                code, press = int(form.get("code", "")), int(form.get("press", ""))
            except ValueError:
                return self._back("Test call not sent: code and press must be numbers.")
            board = form.get("board", "")
            if not BOARD_ID.match(board):
                return self._back("Test call not sent: pick a board.")
            call, err = check_call({"board": board, "boot": "admin", "seq": st.next_ui_seq(),
                                    "code": code, "press": press})
            if err:
                return self._back("Test call not sent: " + err)
            LOG.info("test call from admin page (%s)", who)
            repeat, en = accept_call(self.app, call, None, note="admin page test")
            if repeat:
                return self._back("Dropped as a repeat (same board, code and press within %d s): %s"
                                  % (self.app["cfg"]["same_call_window_s"], en))
            return self._back("Test call sent from %s: %s" % (st.cell_name(board), en))

        if path == "/admin/board/add":
            board, cell = form.get("board", ""), form.get("cell", "")
            if not BOARD_ID.match(board):
                return self._back("Board not added: the ID must be 1 to 32 letters, numbers, _ or -.")
            if not cell or len(cell) > 40 or not cell.isprintable():
                return self._back("Board not added: give a cell name up to 40 characters.")
            existed = st.add_board(board, cell)
            return self._back("Board %s %s as %s." % (board, "renamed" if existed else "added", cell))

        if path == "/admin/board/remove":
            board = form.get("board", "")
            if st.remove_board(board):
                return self._back("Board %s removed." % board)
            return self._back("Board %s was not in the list." % board)

        if path == "/admin/custom":
            zone, en, es = form.get("zone", ""), form.get("en", ""), form.get("es", "")
            if zone not in self.app["cfg"]["zones"]:
                return self._back("Custom page not sent: pick a zone.")
            if not en or len(en) > 200 or len(es) > 200 or not (en + es).isprintable():
                return self._back("Custom page not sent: English text is needed, each text up to 200 characters.")
            rec = {"time": now_text(), "board": "admin", "cell": "Admin page", "press": "",
                   "code": "custom", "who": "Custom", "message": en, "en": en, "es": es,
                   "zone": zone, "note": "custom page from admin page"}
            LOG.info("CUSTOM page from admin page (%s) zone %s: %s | %s", who, zone, en, es)
            self.app["worker"].jobs.put(rec)
            return self._back("Custom page sent to zone %s: %s" % (zone, en))

        if path == "/admin/dryrun":
            on = form.get("dry_run") == "on"
            st.set_dry_run(on, who)
            return self._back("Dry run is now %s." % ("ON: pages are logged, not dialed" if on
                                                       else "OFF: pages will be dialed"))

        return self._reply(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/status.json":
            return self._reply(200, status_obj(self.app))
        if parts.path in ("/", "/index.html"):
            return self._reply(200, page_html(self.app, False).encode(), "text/html; charset=utf-8")
        if parts.path == "/admin":
            if not self._admin_ok():
                return self._ask_password()
            msg = parse_qs(parts.query).get("msg", [""])[0]
            return self._reply(200, page_html(self.app, True, msg).encode(), "text/html; charset=utf-8")
        return self._reply(404, {"ok": False, "error": "not found"})


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ------------------------------------------------------------------ pages ---

def status_obj(app):
    st, cfg = app["state"], app["cfg"]
    now = time.time()
    with st.lock:
        names = sorted(set(st.registry) | set(st.heard_info))
        boards = []
        for name in names:
            b = st.heard_info.get(name, {})
            last = b.get("last_heard")
            boards.append({
                "board": name, "cell": st.cell_name(name), "listed": name in st.registry,
                "ip": b.get("ip", ""),
                "last_heard": now_text(last) if last else "never",
                "seconds_ago": int(now - last) if last else None,
                "ok": bool(last) and now - last <= cfg["board_silent_after_s"],
                "fw": b.get("fw", ""), "rssi": b.get("rssi", ""),
            })
        calls = list(reversed(st.calls))
    return {"version": VERSION, "dry_run": st.dry_run,
            "phone_pager_built": getattr(app["worker"].live, "built", True),
            "pages_waiting": app["worker"].jobs.qsize(),
            "boards": boards, "calls": calls}


PAGE_STYLE = """
:root{--bg:#fff;--fg:#111;--line:#bbb;--head:#eee;--bad:#fdd;--card:#f6f6f6;--good:#070;--warn:#b60}
@media (prefers-color-scheme: dark){:root{--bg:#161616;--fg:#eee;--line:#555;--head:#2a2a2a;--bad:#5a2323;--card:#222;--good:#5c5;--warn:#c80}}
body{font-family:sans-serif;margin:16px;background:var(--bg);color:var(--fg)}
.w{overflow-x:auto} table{border-collapse:collapse;margin-bottom:24px;width:100%}
td,th{border:1px solid var(--line);padding:4px 8px;text-align:left;font-size:14px}
th{background:var(--head)} tr.bad td{background:var(--bad)} td.ok{color:var(--good);font-weight:bold}
.mode{padding:6px 10px;display:inline-block;color:#fff;font-weight:bold}
.card{background:var(--card);border:1px solid var(--line);padding:12px 16px;margin-bottom:20px;max-width:820px}
.card label{display:inline-block;margin:4px 12px 4px 0}
input,select,button{font-size:15px;padding:4px 6px}
button{cursor:pointer} .msg{padding:8px 12px;border:2px solid var(--good);margin-bottom:16px;max-width:820px}
.small{font-size:13px;opacity:.8} form.inline{display:inline;margin:0}
"""

REFRESH_JS = """<script>
var dirty=false;document.addEventListener('input',function(){dirty=true;});
setInterval(function(){var a=document.activeElement;
if(!dirty&&!(a&&/INPUT|SELECT|TEXTAREA|BUTTON/.test(a.tagName)))location.replace(location.pathname);},10000);
</script>"""


def page_html(app, admin, msg=""):
    s = status_obj(app)
    cfg = app["cfg"]
    e = html.escape

    brows = []
    for b in s["boards"]:
        ago = "never" if b["seconds_ago"] is None else "%d s ago" % b["seconds_ago"]
        status = "OK" if b["ok"] else ("NOT SEEN" if b["seconds_ago"] is None else "SILENT")
        action = ""
        if admin:
            if b["listed"]:
                action = ("<td><form class=inline method=post action='/admin/board/remove' "
                          "onsubmit=\"return confirm('Remove board %s?')\">"
                          "<input type=hidden name=board value='%s'><button>Remove</button></form></td>"
                          % (e(b["board"]), e(b["board"])))
            else:
                action = ("<td><form class=inline method=post action='/admin/board/add'>"
                          "<input type=hidden name=board value='%s'>"
                          "<input name=cell placeholder='Cell name' size=10 required maxlength=40> "
                          "<button>Add</button></form></td>" % e(b["board"]))
        brows.append("<tr class='%s'><td>%s</td><td>%s</td><td class='%s'>%s</td><td>%s</td>"
                     "<td>%s</td><td>%s</td><td>%s</td>%s</tr>" % (
                         "" if b["ok"] else "bad", e(b["cell"]), e(b["board"]),
                         "ok" if b["ok"] else "", status, e(b["last_heard"]), e(ago),
                         e(b["ip"]), e(str(b["rssi"])), action))
    crow = []
    for c in s["calls"]:
        cls = "bad" if c.get("result") == "FAILED" else ""
        crow.append("<tr class='%s'>%s</tr>" % (cls, "".join(
            "<td>%s</td>" % e(str(c.get(k, ""))) for k in
            ("time", "cell", "press", "code", "who", "message", "result", "attempts", "detail"))))

    mode = ("DRY RUN: pages are logged, not dialed" if s["dry_run"] else "LIVE: pages are dialed")
    mode_color = "var(--warn)" if s["dry_run"] else "var(--good)"
    nav = ("<a href='/'>Back to status page</a>" if admin
           else "<a href='/admin'>Admin: test calls and boards</a>")

    admin_html = ""
    if admin:
        listed = [b for b in s["boards"] if b["listed"]]
        board_opts = "".join("<option value='%s'>%s (%s)</option>" % (e(b["board"]), e(b["cell"]), e(b["board"]))
                             for b in listed) or "<option value=''>add a board first</option>"
        code_opts = "".join("<option value='%d'>%d: %s%s</option>" % (
            c, c, e(cfg["codes"][c]["who"]),
            "" if c <= 6 else " (cell: %s)" % e(cfg["codes"][c].get("why", ""))
            if cfg["codes"][c].get("why") else " (from the cell)")
            for c in range(2, 10))
        live_warn = ("" if s["dry_run"] else
                     "<p><b>LIVE: a test really pages the zone.</b></p>")
        zone_opts = "".join("<option value='%s'>%s (dial %s)</option>" % (e(z), e(z), e(str(v.get("dial", ""))))
                            for z, v in cfg["zones"].items())
        if s["dry_run"]:
            switch = ("<p>Dry run is <b>ON</b>: pages are logged, not dialed.</p>"
                      "<form method=post action='/admin/dryrun' onsubmit=\"return confirm("
                      "'Turn dry run OFF? Every call, real or test, will then be DIALED to the paging zone.')\">"
                      "<input type=hidden name=dry_run value=off><button>Turn dry run OFF (go live)</button></form>")
            if not s["phone_pager_built"]:
                switch += ("<p class=small>The phone step (HT813) is not built yet. With dry run off, "
                           "pages are logged as FAILED until it is.</p>")
        else:
            switch = ("<p>Dry run is <b>OFF</b>: pages are dialed.</p>"
                      "<form method=post action='/admin/dryrun'><input type=hidden name=dry_run value=on>"
                      "<button>Turn dry run ON (stop dialing)</button></form>")
        admin_html = """
<div class=card><h2>Settings</h2>%s
<p class=small>Saved on the laptop and kept through restarts. The config file's "dry_run" is only the default.</p></div>
<div class=card><h2>Send a test call</h2>%s
<form method=post action='/admin/test'>
<label>From board <select name=board required>%s</select></label>
<label>Call <select name=code>%s</select></label>
<label>Press <input name=press type=number min=0 max=999 value=0 required style='width:6em'></label>
<button>Send test call</button></form>
<p class=small>Goes through the same checks, repeat drop, log and pager as a real call from that board.
Press 0 tests the "press number unknown" message.</p></div>
<div class=card><h2>Add or rename a board</h2>
<form method=post action='/admin/board/add'>
<label>Board ID <input name=board required maxlength=32 pattern='[A-Za-z0-9_-]{1,32}' placeholder='FSP_LH' size=10></label>
<label>Cell name <input name=cell required maxlength=40 placeholder='FSP LH' size=12></label>
<button>Add / rename</button></form>
<p class=small>The board ID must match the ID set in that board's firmware. Letters, numbers, _ and - only.
Use an existing ID to rename its cell. Remove boards in the table below.</p></div>
<div class=card><h2>Send a custom page</h2>%s
<form method=post action='/admin/custom'>
<label>Zone <select name=zone>%s</select></label><br>
<label>English <input name=en required maxlength=200 size=60 placeholder='Test page. Maintenance needed at press 12.'></label><br>
<label>Spanish <input name=es maxlength=200 size=60 placeholder='Prueba. Se necesita mantenimiento en la prensa 12.'></label><br>
<button>Send custom page</button></form>
<p class=small>Your own wording, read English then Spanish (leave Spanish empty for English only). Logged as code "custom".</p></div>
""" % (switch, live_warn, board_opts, code_opts, live_warn, zone_opts)

    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P833 Call Receiver</title><style>%s</style></head><body>
<h1>P833 Call Receiver%s</h1>
<p><span class="mode" style="background:%s">%s</span> &nbsp; version %s &nbsp; pages waiting: %d &nbsp; %s</p>
%s%s
<h2>Boards</h2>
<div class=w><table><tr><th>Cell</th><th>Board</th><th>Status</th><th>Last heard</th><th>Age</th><th>IP</th><th>Wi-Fi dBm</th>%s</tr>
%s</table></div>
<h2>Recent calls (newest first)</h2>
<div class=w><table><tr><th>Time</th><th>Cell</th><th>Press</th><th>Code</th><th>Who</th><th>Message</th><th>Result</th><th>Tries</th><th>Detail</th></tr>
%s</table></div>
<p class=small>Refreshes every 10 s (paused while you type). Full log: /var/log/p833-receiver/calls.csv</p>
%s</body></html>""" % (
        PAGE_STYLE, " - Admin" if admin else "", mode_color, mode, VERSION, s["pages_waiting"], nav,
        "<div class=msg>%s</div>" % e(msg) if msg else "", admin_html,
        "<th></th>" if admin else "",
        "\n".join(brows) or "<tr><td colspan=8>none</td></tr>",
        "\n".join(crow) or "<tr><td colspan=9>none yet</td></tr>", REFRESH_JS)


# ------------------------------------------------------------------ main ---

def main(argv=None):
    p = argparse.ArgumentParser(description="P833 call receiver")
    p.add_argument("--config", default="/etc/p833-receiver/config.json")
    p.add_argument("--state-dir", default=os.environ.get("STATE_DIRECTORY", "/var/lib/p833-receiver"))
    p.add_argument("--log-dir", default=os.environ.get("LOGS_DIRECTORY", "/var/log/p833-receiver"))
    p.add_argument("--dry-run", action="store_true", help="force dry run for this run, whatever the settings say")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)s %(message)s")
    cfg = load_config(args.config)
    os.makedirs(args.state_dir, exist_ok=True)
    os.makedirs(args.log_dir, exist_ok=True)

    key = os.environ.get("P833_BOARD_KEY", "")
    if not key:
        LOG.warning("P833_BOARD_KEY is not set: any device can send calls")
    admin_pw = os.environ.get("P833_ADMIN_PASSWORD", "")
    if not admin_pw:
        LOG.warning("P833_ADMIN_PASSWORD is not set: the admin page is open to anyone")

    state = State(cfg, args.state_dir, args.log_dir)
    if args.dry_run:
        state.dry_run = True   # for this run only; not saved
    worker = PageWorker(cfg, state, make_live_pager(cfg))
    worker.start()
    Handler.app = {"cfg": cfg, "state": state, "worker": worker, "key": key,
                   "admin_password": admin_pw}

    def watch():
        while True:
            time.sleep(15)
            state.check_silent()
    threading.Thread(target=watch, daemon=True).start()

    srv = Server((cfg.get("listen", "0.0.0.0"), int(cfg.get("port", 8833))), Handler)
    LOG.info("P833 receiver %s listening on %s:%s (%s)", VERSION, cfg.get("listen", "0.0.0.0"),
             cfg.get("port", 8833), "DRY RUN" if state.dry_run else "LIVE")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
