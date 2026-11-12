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
import io
import json
import logging
import os
import queue
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from p833_audio import AudioError, AudioLibrary, tone_style  # noqa: E402
from p833_sip import SipPager  # noqa: E402
import p833_sms  # noqa: E402
import p833_voice  # noqa: E402
from p833_voice import tts_env, tts_ready  # noqa: E402,F401

VERSION = "0.11.0"
LOG = logging.getLogger("p833")

CSV_FIELDS = ["time", "board", "cell", "press", "code", "who", "message",
              "result", "attempts", "detail"]
BOARD_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
BOOT_ID = re.compile(r"^[A-Za-z0-9_-]{0,32}$")
MAX_CONNECTIONS = 64      # open web connections at once; more are closed at once
MAX_PER_ADDRESS = 8       # ...from any one address
RESERVED_FOR_BOARDS = 16  # ...of which this many are kept for boards (and the laptop itself)
REQUEST_DEADLINE_S = 15   # the whole request (TLS hello, headers, body) must arrive in this time
MAX_WAITING_PAGES = 20    # pages queued but not yet sent; calls over it get "busy" and the board retries
MAX_CALLS_PER_MIN = 6     # accepted calls from one board ID in a minute
MAX_UNKNOWN_BOARDS = 50   # boards not on the list that are remembered on the status page
LOGIN_FAILS = 10          # wrong admin passwords from one address...
LOGIN_WINDOW_S = 600      # ...in this many seconds lock that address out for this long


csv_safe = p833_sms.csv_safe


def plain(v, n=32):
    """Text from the network, made safe for logs and pages: printable, no control characters."""
    return re.sub(r"[^A-Za-z0-9 ._:/+-]", "?", str(v))[:n]


# ---------------------------------------------------------------- config ---

# Phone settings, used by the HT813 phone step. Editable on the admin page.
PHONE_DEFAULTS = {
    "ht813_ip": "192.168.83.20",
    "sip_port": 5062,
    "local_sip_port": 5070,
    "dial_method": "2-stage",
    "wait_after_answer_ms": 1500,
    "dtmf_ms": 120,
    "dtmf_gap_ms": 120,
    "wait_after_dial_ms": 2000,
    "max_call_s": 90,
    "ht813_user": "",      # SIP user to call; empty = call the HT813 by address (direct IP call)
    "ht813_auth_id": "",   # user for the HT813's password check (config file only); default ht813_user or p833
    "min_gap_s": 6,        # gap after a call before the next, so the HT813 and Norstar release the line
    "line_check": "warn",  # after dialing, listen for dial/busy/ringing tone: retry, warn or off
}
# (key, label, low, high, help) for the number fields on the admin page.
PHONE_NUMBERS = [
    ("sip_port", "HT813 FXO SIP port", 1, 65535,
     "The HT813's SIP port for its FXO (line) port. Often 5062; check the HT813's FXO settings page."),
    ("local_sip_port", "Laptop SIP port", 1024, 65535, "The port the laptop calls from."),
    ("wait_after_answer_ms", "Wait after the HT813 answers (ms)", 0, 10000,
     "Time for the Norstar dial tone before dialing (2-stage)."),
    ("dtmf_ms", "Tone length (ms)", 40, 1000, "How long each dialed digit is held."),
    ("dtmf_gap_ms", "Gap between tones (ms)", 40, 1000, "Silence between dialed digits."),
    ("wait_after_dial_ms", "Wait after dialing before speaking (ms)", 0, 15000,
     "Time for the page zone to open (the paging beep) before the message plays."),
    ("max_call_s", "Longest call (s)", 10, 300, "Hang up after this, whatever happens."),
    ("min_gap_s", "Gap between calls (s)", 0, 60,
     "Wait at least this long after a call before the next, so the line is released (the HT813 itself "
     "waits 4 s between outgoing calls)."),
]
DIAL_RE = re.compile(r"^[0-9*#,F]{0,32}$")
IP_RE = re.compile(r"^((25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(25[0-5]|2[0-4]\d|1?\d?\d)$")


def tuning_of(cfg):
    """The settings the admin page can change, as they are in cfg now."""
    return {"phone": dict(cfg["phone"]),
            "zone_dial": {z: v.get("dial", "") for z, v in cfg["zones"].items()},
            "code_zone": {str(c): cfg["codes"][c]["zone"] for c in range(2, 10)},
            "code_tone": {str(c): cfg["codes"][c].get("tone", "") for c in range(2, 10)},
            "audio": {"repeat": cfg["audio"]["repeat"], "tones": tone_style(cfg),
                      "lead_silence_ms": cfg["audio"]["lead_silence_ms"],
                      "keep_dry_run_pages": cfg["audio"]["keep_dry_run_pages"],
                      "gain_db": cfg["audio"]["gain_db"],
                      "lang_gap_ms": cfg["audio"]["lang_gap_ms"],
                      "repeat_gap_ms": cfg["audio"]["repeat_gap_ms"],
                      "tone_gap_ms": cfg["audio"]["tone_gap_ms"]}}


def apply_tuning(cfg, t):
    for k, v in t.get("phone", {}).items():
        if k in PHONE_DEFAULTS:
            cfg["phone"][k] = v
    for z, d in t.get("zone_dial", {}).items():
        if z in cfg["zones"]:
            cfg["zones"][z]["dial"] = d
    for c, z in t.get("code_zone", {}).items():
        if c.isdigit() and int(c) in cfg["codes"] and z in cfg["zones"]:
            cfg["codes"][int(c)]["zone"] = z
    for c, name in t.get("code_tone", {}).items():
        if c.isdigit() and int(c) in cfg["codes"]:
            cfg["codes"][int(c)]["tone"] = name
    a = t.get("audio", {})
    if "repeat" in a:
        cfg["audio"]["repeat"] = a["repeat"]
    if "lead_silence_ms" in a:
        cfg["audio"]["lead_silence_ms"] = a["lead_silence_ms"]
    if "keep_dry_run_pages" in a:
        cfg["audio"]["keep_dry_run_pages"] = a["keep_dry_run_pages"]
    for k in ("gain_db", "lang_gap_ms", "repeat_gap_ms", "tone_gap_ms"):
        if k in a:
            cfg["audio"][k] = a[k]
    if a.get("tones") in ("once", "twice", "off"):
        cfg["audio"]["tones"] = a["tones"]
    elif "chime" in a:                        # settings saved by v0.3.x
        cfg["audio"]["tones"] = "once" if a["chime"] else "off"


def parse_tuning(form, cfg, tone_names=None):
    """Check the admin page's phone form. Return (tuning, list of errors)."""
    errs = []
    phone = {}
    ip = form.get("ht813_ip", "")
    if IP_RE.match(ip):
        phone["ht813_ip"] = ip
    else:
        errs.append("HT813 address must be an IP address like 192.168.83.20")
    method = form.get("dial_method", "")
    if method in ("1-stage", "2-stage"):
        phone["dial_method"] = method
    else:
        errs.append("dial method must be 1-stage or 2-stage")
    check = form.get("line_check", "retry")
    if check in ("retry", "warn", "off"):
        phone["line_check"] = check
    else:
        errs.append("line check must be retry, warn or off")
    for key, label, lo, hi, _ in PHONE_NUMBERS:
        try:
            v = int(form.get(key, ""))
            if not lo <= v <= hi:
                raise ValueError
            phone[key] = v
        except ValueError:
            errs.append("%s must be a whole number %d to %d" % (label, lo, hi))
    dials = {}
    for z in cfg["zones"]:
        d = form.get("dial_" + z, "").replace(" ", "").upper()
        if DIAL_RE.match(d):
            dials[z] = d
        else:
            errs.append("zone %s dial: only 0-9 * # , F, up to 32" % z)
    zones = {}
    for c in range(2, 10):
        z = form.get("zone_%d" % c, "")
        if z in cfg["zones"]:
            zones[str(c)] = z
        else:
            errs.append("code %d: pick a zone" % c)
    tones = {}
    for c in range(2, 10):
        name = form.get("tone_%d" % c, "")
        if name == "" or (tone_names is not None and name in tone_names):
            tones[str(c)] = name
        else:
            errs.append("code %d: unknown tone %s" % (c, name))
    audio = {"tones": form.get("tones", "")}
    if audio["tones"] not in ("once", "twice", "off"):
        errs.append("tone must be once, twice or off")
    try:
        audio["repeat"] = int(form.get("repeat", ""))
        if not 1 <= audio["repeat"] <= 5:
            raise ValueError
    except ValueError:
        errs.append("plays must be 1 to 5")
    try:
        audio["lead_silence_ms"] = int(form.get("lead_silence_ms", ""))
        if not 0 <= audio["lead_silence_ms"] <= 10000:
            raise ValueError
    except ValueError:
        errs.append("silence at the start must be 0 to 10000 ms")
    try:
        audio["keep_dry_run_pages"] = int(form.get("keep_dry_run_pages", "0") or "0")
        if not 0 <= audio["keep_dry_run_pages"] <= 100:
            raise ValueError
    except ValueError:
        errs.append("dry-run pages to keep must be 0 to 100")
    try:
        audio["gain_db"] = int(form.get("gain_db", "0") or "0")
        if not -6 <= audio["gain_db"] <= 12:
            raise ValueError
    except ValueError:
        errs.append("page volume must be -6 to 12 dB")
    for key, label, default in (("lang_gap_ms", "pause between English and Spanish", 400),
                                ("repeat_gap_ms", "pause before the repeat", 500),
                                ("tone_gap_ms", "pause between the two tones", 250)):
        try:
            audio[key] = int(form.get(key, str(default)) or str(default))
            if not 0 <= audio[key] <= 3000:
                raise ValueError
        except ValueError:
            errs.append("%s must be 0 to 3000 ms" % label)
    return {"phone": phone, "zone_dial": dials, "code_zone": zones, "code_tone": tones,
            "audio": audio}, errs


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
    # The admin page over HTTPS (same port; boards keep plain HTTP). install.sh makes the
    # certificate. Without it the admin page stays on plain HTTP and says so.
    cfg.setdefault("admin_https", True)
    cfg.setdefault("tls_cert", "/etc/p833-receiver/tls/cert.pem")
    cfg.setdefault("tls_key", "/etc/p833-receiver/tls/key.pem")
    audio = cfg.setdefault("audio", {})
    here = os.path.dirname(os.path.abspath(__file__))
    # Installed: /opt/p833-receiver/audio. Run from the repo: <repo>/audio.
    audio.setdefault("dir", next((d for d in (os.path.join(here, "audio"), os.path.join(here, "..", "audio"))
                                  if os.path.isdir(d)), os.path.join(here, "audio")))
    audio.setdefault("tones", "once" if audio.get("chime", True) else "off")
    audio.setdefault("repeat", 2)
    audio.setdefault("languages", ["en", "es"])
    audio.setdefault("keep_dry_run_pages", 0)
    audio.setdefault("lead_silence_ms", 0)
    audio.setdefault("gain_db", 0)
    audio.setdefault("lang_gap_ms", 400)
    audio.setdefault("repeat_gap_ms", 500)
    audio.setdefault("tone_gap_ms", 250)
    audio.setdefault("speak_digits", ["en"])     # languages whose press numbers are read digit by digit
    # Voice lab (Piper on the laptop, set up by install/install_tts.sh)
    audio.setdefault("tts_python", os.path.join(here, "tts", "bin", "python"))
    audio.setdefault("tts_script", os.path.join(here, "p833_tts.py"))
    audio.setdefault("tts_voices", os.path.join(here, "voices"))
    sms = cfg.setdefault("sms", {})
    for k, v in p833_sms.DEFAULTS.items():
        sms.setdefault(k, v)
    phone = cfg.setdefault("phone", {})
    for k, v in PHONE_DEFAULTS.items():
        phone.setdefault(k, v)
    for z in cfg["zones"].values():
        if z.get("dial") == "TO-VERIFY":      # placeholder in older configs = not set
            z["dial"] = ""
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
    if not isinstance(board, str) or not BOARD_ID.match(board):
        return None, "board must be 1 to 32 letters, numbers, _ or -"
    out = {"board": board}
    for name, lo, hi in (("code", 2, 9), ("press", 0, 999), ("seq", 0, 2**31)):
        v = body.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            return None, "%s must be a whole number %d to %d" % (name, lo, hi)
        out[name] = v
    out["boot"] = str(body.get("boot", ""))
    if not BOOT_ID.match(out["boot"]):
        return None, "boot must be up to 32 letters, numbers, _ or -"
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
        self.ui_boot = "admin-%x" % int(time.time())   # admin test calls: unique per start
        self.boots = {}       # board -> [[boot, highest seq accepted], ...] newest last (kept across restarts)
        self.rate = {}        # board -> times of accepted calls in the last minute
        self.heard_file = os.path.join(state_dir, "boards.json")
        self.boots_file = os.path.join(state_dir, "seen_calls.json")
        self.registry_file = os.path.join(state_dir, "board_list.json")
        self.settings_file = os.path.join(state_dir, "settings.json")
        self.csv_file = os.path.join(log_dir, "calls.csv")
        self._load()

    def _save_settings(self):
        self._write_json(self.settings_file, self.settings)

    def set_dry_run(self, on, who=""):
        with self.lock:
            self.dry_run = bool(on)
            self.settings["dry_run"] = self.dry_run
            self._save_settings()
        LOG.warning("dry run turned %s on the admin page (%s)%s", "ON" if on else "OFF", who,
                    "" if on else ": PAGES WILL BE DIALED")

    def _load(self):
        # Dry run: the admin page's choice wins; the config value is the default.
        self.settings = self._read_json(self.settings_file) or {}
        self.dry_run = bool(self.settings.get("dry_run", self.cfg["dry_run"]))
        # Phone and paging settings: config values are the defaults; the admin
        # page's saved values (if any) go on top.
        self.defaults = json.loads(json.dumps(tuning_of(self.cfg)))
        if isinstance(self.settings.get("tuning"), dict):
            apply_tuning(self.cfg, self.settings["tuning"])
        if isinstance(self.settings.get("voice"), dict):
            self.cfg["audio"]["voice"] = self.settings["voice"]
        if isinstance(self.settings.get("sms"), dict):
            self.cfg["sms"].update({k: v for k, v in self.settings["sms"].items()
                                    if k in p833_sms.DEFAULTS and k != "api_url"})

        self.heard_info = self._read_json(self.heard_file) or {}
        for b in self.heard_info.values():
            b["silent_logged"] = False
        boots = self._read_json(self.boots_file) or {}
        self.boots = {k: v for k, v in boots.items() if isinstance(v, list)}
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

    def set_voice(self, voice, who=""):
        with self.lock:
            self.cfg["audio"]["voice"] = voice
            self.settings["voice"] = voice
            self._save_settings()
        LOG.info("page voice saved on the admin page (%s): %s", who,
                 p833_voice.describe(p833_voice.voice_settings(self.cfg)))

    def set_sms(self, sms, who=""):
        with self.lock:
            self.cfg["sms"].update(sms)
            self.settings["sms"] = dict(self.settings.get("sms", {}), **sms)
            self._save_settings()
        LOG.info("text message settings saved on the admin page (%s): enabled=%s, %d number(s)", who,
                 self.cfg["sms"]["enabled"], len(self.cfg["sms"]["to"]))

    def set_tuning(self, t, who=""):
        with self.lock:
            apply_tuning(self.cfg, t)
            self.settings["tuning"] = t
            self._save_settings()
        LOG.info("phone and paging settings saved on the admin page (%s): %s", who, json.dumps(t))

    def reset_tuning(self, who=""):
        with self.lock:
            apply_tuning(self.cfg, self.defaults)
            self.settings.pop("tuning", None)
            self._save_settings()
        LOG.info("phone and paging settings reset to the config defaults (%s)", who)

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
            if board not in self.heard_info and board not in self.registry and \
                    sum(1 for k in self.heard_info if k not in self.registry) >= MAX_UNKNOWN_BOARDS:
                return          # don't let made-up board IDs grow the list without end
            b = self.heard_info.setdefault(board, {})
            if b.get("silent_logged"):
                LOG.info("board %s is back after being silent", board)
            b.update({"last_heard": time.time(), "ip": ip, "silent_logged": False})
            if extra:
                b.update(extra)
            self._write_json(self.heard_file, self.heard_info)

    def replayed(self, call):
        """True if this board already sent this message (same boot, same or higher seq).
        Boards number their calls 1, 2, 3... after each start (boot is random per start),
        so an old message sent again, even days later, is caught. Kept across restarts."""
        with self.lock:
            hist = self.boots.get(call["board"], [])
            for boot, top in hist:
                if boot == call["boot"]:
                    return call["seq"] <= top
            return False

    def remember(self, call):
        with self.lock:
            hist = [x for x in self.boots.get(call["board"], []) if x[0] != call["boot"]]
            top = max([call["seq"]] + [t for b, t in self.boots.get(call["board"], []) if b == call["boot"]])
            self.boots[call["board"]] = (hist + [[call["boot"], top]])[-20:]
            if len(self.boots) > 200:
                for k in list(self.boots)[:len(self.boots) - 200]:
                    del self.boots[k]
            self._write_json(self.boots_file, self.boots)

    def seq_jump(self, call):
        """True if seq is far beyond what this board sent in this boot (a forged message that
        would otherwise make every real call after it look like an old one)."""
        with self.lock:
            top = next((t for b, t in self.boots.get(call["board"], []) if b == call["boot"]), 0)
            return call["seq"] > top + MAX_SEQ_JUMP

    def seen_recently(self, call):
        with self.lock:
            t = self.seen.get((call["board"], call["boot"], call["seq"]))
            return t is not None and time.time() - t < self.cfg["repeat_window_s"]

    def too_fast(self, board):
        """True if this board ID already had MAX_CALLS_PER_MIN calls accepted in the last minute."""
        now = time.time()
        with self.lock:
            times = [t for t in self.rate.get(board, []) if now - t < 60]
            self.rate = {k: v for k, v in self.rate.items() if v and now - v[-1] < 60}
            self.rate[board] = times
            if len(times) >= MAX_CALLS_PER_MIN:
                return True
            times.append(now)
            return False

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
                    w.writerow({k: csv_safe(rec.get(k, "")) for k in CSV_FIELDS})
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


class Busy(Exception):
    """Too many calls: the board should try again later (HTTP 503)."""


class Refused(Exception):
    """A call that will never be paged (HTTP 400): the board drops it."""


MAX_SEQ_JUMP = 1000   # a board numbers its calls 1, 2, 3...; a far bigger jump is a forged message


def accept_call(app, call, ip, note=""):
    """Handle one checked call from a board or the admin page.

    Returns (repeat: bool, english text); raises Busy when it can't take the call now."""
    st, cfg = app["state"], app["cfg"]
    if call["boot"] and not note and st.seq_jump(call):
        LOG.warning("call from %s refused: seq %d jumps more than %d past the last one from that boot "
                    "(forged?)", call["board"], call["seq"], MAX_SEQ_JUMP)
        raise Refused("seq jumps too far")
    if st.replayed(call) and not st.seen_recently(call):
        # handled long ago (inside the window it's logged as a repeat below): journal only
        LOG.info("old message dropped: %s seq %d (boot %s) was already handled", call["board"],
                 call["seq"], call["boot"])
        return True, ""
    if app["worker"].jobs.qsize() >= MAX_WAITING_PAGES:
        LOG.warning("call from %s refused for now: %d pages already waiting", call["board"], MAX_WAITING_PAGES)
        raise Busy("too many pages waiting")
    if not note and st.too_fast(call["board"]):
        LOG.warning("call from %s refused for now: more than %d calls in a minute", call["board"],
                    MAX_CALLS_PER_MIN)
        raise Busy("too many calls from this board")
    if ip:
        st.heard(call["board"], ip)
    cell = st.cell_name(call["board"])
    en, es = build_message(cfg, call["code"], call["press"])
    rec = {"time": now_text(time.time() - call["age_ms"] / 1000.0),
           "board": call["board"], "cell": cell, "press": call["press"],
           "code": call["code"], "who": cfg["codes"][call["code"]]["who"],
           "message": en, "en": en, "es": es, "note": note,
           "zone": cfg["codes"][call["code"]]["zone"]}
    repeat = st.is_repeat(call)
    if call["boot"]:
        st.remember(call)
    if repeat:
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
    if app.get("sms"):
        if call["board"] in st.registry or note:
            app["sms"].text_for_call(rec, test_call=bool(note))
        else:   # only listed boards text the crew: a made-up board ID can't spend the texts
            LOG.warning("no text for the call from unknown board %s (add it on the admin page)", call["board"])
    return False, en


# ---------------------------------------------------------------- pagers ---

class DryRunPager:
    """Logs the page it would send. Never dials.

    It can also keep the audio of the last N dry-run pages for listening back
    (audio.keep_dry_run_pages, set on the admin page; 0 = off, the default).
    The setting is read for each page, so a change applies at once."""

    def __init__(self, save_dir=None, cfg=None):
        self.save_dir, self.cfg = save_dir, cfg

    def keep(self):
        try:
            return max(0, int((self.cfg or {}).get("audio", {}).get("keep_dry_run_pages", 0)))
        except (TypeError, ValueError):
            return 0

    def prune(self, keep):
        """Delete saved dry-run audio beyond the newest `keep` files (all of them for 0)."""
        if not self.save_dir or not os.path.isdir(self.save_dir):
            return 0
        old = sorted(n for n in os.listdir(self.save_dir) if n.endswith(".wav"))
        gone = old[:len(old) - keep] if keep < len(old) else []
        for n in gone:
            try:
                os.remove(os.path.join(self.save_dir, n))
            except OSError as e:
                LOG.warning("could not delete old dry-run audio %s: %s", n, e)
        return len(gone)

    def page(self, zone, dial, en, es, audio=None):
        LOG.info("DRY RUN page: zone=%s dial=%s | %s | %s", zone, dial, en, es)
        keep = self.keep()
        if audio and self.save_dir and keep > 0:
            try:
                os.makedirs(self.save_dir, exist_ok=True)
                name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".wav"
                with open(os.path.join(self.save_dir, name), "wb") as f:
                    f.write(audio)
                self.prune(keep)
                return True, "dry run, not dialed; audio saved as %s" % name
            except OSError as e:
                LOG.warning("could not save dry-run audio: %s", e)
        return True, "dry run, not dialed"


class NotBuiltPager:
    """Stands in for the HT813 phone pager until that step is built.

    With dry run off, every page fails loudly (logged FAILED) instead of
    silently going nowhere."""
    built = False

    def page(self, zone, dial, en, es, audio=None):
        return False, "phone pager not built yet (HT813 step); nothing was dialed"


def make_live_pager(cfg, ht813_password=""):
    return SipPager(cfg, LOG.info, password=ht813_password)


class PageWorker(threading.Thread):
    """Sends pages one at a time (there is one phone line), with retries.

    Dry run is checked for each page, so the admin switch takes effect at once."""

    def __init__(self, cfg, state, live_pager, dry_pager=None, audio=None):
        super().__init__(daemon=True)
        self.cfg, self.state = cfg, state
        self.live, self.dry = live_pager, dry_pager or DryRunPager()
        self.audio = audio or AudioLibrary(cfg["audio"]["dir"])
        self.jobs = queue.Queue()
        self.state_dir = None      # where the last live page's line recording is kept

    def make_audio(self, rec):
        """Return (WAV bytes or None, note for the log)."""
        try:
            if rec["code"] == "custom":
                return self.audio.build_custom(self.cfg, rec["en"], rec["es"]), ""
            wav, notes = self.audio.build(self.cfg, int(rec["code"]), int(rec["press"]))
            if notes:
                LOG.warning("audio: %s", "; ".join(notes))
                return wav, "audio: " + "; ".join(notes)
            return wav, ""
        except AudioError as e:
            return None, "no audio: %s" % e

    def run(self):
        while True:
            rec = self.jobs.get()
            zone = rec["zone"]
            dial = self.cfg["zones"][zone]["dial"]
            dry = self.state.dry_run
            pager = self.dry if dry else self.live
            audio, audio_note = self.make_audio(rec)
            tries = self.cfg["page_retries"] if (audio or dry) else 0
            ok, detail, n = False, audio_note, 0
            for n in range(1, tries + 1):
                try:
                    ok, detail = pager.page(zone, dial, rec["en"], rec["es"], audio)
                except Exception as e:  # a pager bug must not stop the worker
                    ok, detail = False, "error: %s" % e
                if not ok and not dry and self.state_dir and getattr(pager, "last_recording", b""):
                    try:   # keep what the line did on the failed try, to listen to on the admin page
                        with open(os.path.join(self.state_dir, "last_failed_page_line.wav"), "wb") as f:
                            f.write(pager.last_recording)
                    except OSError as e:
                        LOG.warning("could not save the failed page's line recording: %s", e)
                if ok:
                    break
                LOG.warning("page try %d of %d failed: %s", n, tries, detail)
                if n < tries:
                    time.sleep(self.cfg["page_retry_wait_s"])
            rec["attempts"] = n
            heard = getattr(pager, "last_recording", b"") if not dry else b""
            if heard and self.state_dir:
                try:
                    with open(os.path.join(self.state_dir, "last_page_line.wav"), "wb") as f:
                        f.write(heard)
                except OSError as e:
                    LOG.warning("could not save the page's line recording: %s", e)
            if audio_note and audio_note not in detail:
                detail = audio_note + "; " + detail
            rec["detail"] = (rec["note"] + "; " if rec.get("note") else "") + detail
            if ok:
                rec["result"] = "dry-run" if dry else "sent"
            else:
                rec["result"] = "FAILED"
                LOG.error("PAGE FAILED after %d tries: %s", n, rec["message"])
            self.state.record(rec)
            self.jobs.task_done()


# ------------------------------------------------------------- voice lab ---
# Speak any text with Piper on the laptop, to try voices, pace and pauses.

LAB_DEFAULT_TEXT = "Maintenance requested to press two. [350] Seven."


_voices_cache = {"at": 0, "key": None, "list": []}


def tts_voices(cfg):
    """Installed voices; asked of Piper at most once a minute (the admin page reloads often)."""
    if not tts_ready(cfg):
        return []
    a = cfg["audio"]
    key = (a["tts_python"], a["tts_script"], a["tts_voices"])
    if _voices_cache["key"] == key and time.time() - _voices_cache["at"] < 60:
        return _voices_cache["list"]
    try:
        out = subprocess.run([a["tts_python"], a["tts_script"], "list", a["tts_voices"]],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=tts_env())
        voices = json.loads(out.stdout.decode() or "[]") if out.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        voices = []
    _voices_cache.update(at=time.time(), key=key, list=voices)
    return voices


def lab_segments(text):
    """'press two. [350] Seven.' -> [{'text': 'press two.'}, {'pause_ms': 350}, {'text': 'Seven.'}]"""
    segs = []
    for i, part in enumerate(re.split(r"\[(\d{1,4})\]", text)):
        if i % 2:
            segs.append({"pause_ms": min(5000, int(part))})
        elif part.strip():
            segs.append({"text": part.strip()})
    return segs


def page_voice(app):
    """The Piper page voice (made on first use if the receiver was started without one)."""
    if app.get("voice") is None:
        app["voice"] = p833_voice.PageVoice(app["cfg"], app["state_dir"])
        app["voice"].installed = lambda: tts_voices(app["cfg"])
        app["worker"].audio.voice = app["voice"]
    return app["voice"]


def tts_speak(app, voice, length_scale, text):
    """WAV bytes (8 kHz 16-bit) of the text, or AudioError. Uses the same Piper helper as pages."""
    if not tts_ready(app["cfg"]):
        raise AudioError("the voice lab isn't installed on this laptop (run: sudo bash install/install_tts.sh)")
    if voice not in tts_voices(app["cfg"]):
        raise AudioError("voice not installed: %s" % voice)
    segs = lab_segments(text)
    if not any("text" in x for x in segs):
        raise AudioError("there is no text to speak")
    return page_voice(app).engine.speak(voice, length_scale, segs, p833_voice.SLOW_TIMEOUT_S)


def lab_wav(app, form):
    """The voice lab's audio as it would page: optional tone, the text, page volume."""
    cfg, lib = app["cfg"], app["worker"].audio
    voice = form.get("voice", "")
    try:
        pace = float(form.get("pace", "1.25"))
        if not 0.5 <= pace <= 2.5:
            raise ValueError
        plays = int(form.get("plays", "1"))
        if not 1 <= plays <= 3:
            raise ValueError
    except ValueError:
        raise AudioError("pace must be 0.5 to 2.5, plays 1 to 3")
    text = form.get("text", "")[:1500]
    speech = tts_speak(app, voice, pace, text)
    with wave.open(io.BytesIO(speech)) as w:
        body = w.readframes(w.getnframes())
    lead = b""
    tone = form.get("tone", "")
    if tone:
        if tone not in lib.tone_names():
            raise AudioError("unknown tone")
        lead = lib._frames("tones/single/%s.wav" % tone) + b"\x00\x00" * 2400
    return lib._wrap(lead, body, {"repeat": plays, "lead_silence_ms": 0,
                                  "repeat_gap_ms": cfg["audio"].get("repeat_gap_ms", 500),
                                  "gain_db": cfg["audio"].get("gain_db", 0)})


# ------------------------------------------------------------ web server ---

class Handler(BaseHTTPRequestHandler):
    server_version = "P833Receiver/" + VERSION
    app = None  # set in main()
    timeout = 20          # seconds a connection may sit idle or trickle in; then it's closed
    login_fails = {}      # address -> times of wrong admin passwords
    login_lock = threading.Lock()

    def log_message(self, fmt, *args):  # keep the journal quiet
        pass

    def _reply(self, status, obj, ctype="application/json", headers=None):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _raw_body(self, limit=8192):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if n <= 0 or n > limit:
            self._request_read()
            return None
        data = self.rfile.read(n)
        self._request_read()
        return data

    def _request_read(self):
        f = getattr(self.server, "request_read", None)
        if f:
            f()

    def _json_body(self):
        raw = self._raw_body()
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _key_ok(self):
        """The board key must be set and match. No key set means no calls (fail closed)."""
        key = self.app["key"]
        given = self.headers.get("X-P833-Key", "")
        return bool(key) and hmac.compare_digest(given.encode("utf-8", "replace"), key.encode("utf-8"))

    # -- admin password (HTTP Basic; any user name, the password is checked)
    def _admin_ok(self):
        """No password set means no admin page (fail closed). Wrong passwords are logged,
        and an address with LOGIN_FAILS of them in LOGIN_WINDOW_S is locked out that long."""
        pw = self.app["admin_password"]
        if not pw:
            return False
        ip = self.client_address[0]
        now = time.time()
        with self.login_lock:
            fails = [t for t in self.login_fails.get(ip, []) if now - t < LOGIN_WINDOW_S]
            self.login_fails[ip] = fails
            if len(fails) >= LOGIN_FAILS:
                return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            given = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)[1]
        except (ValueError, IndexError, UnicodeDecodeError):
            given = None
        if given is not None and hmac.compare_digest(given.encode("utf-8"), pw.encode("utf-8")):
            return True
        with self.login_lock:
            fails = self.login_fails.setdefault(ip, [])
            fails.append(now)
            if len(self.login_fails) > 1000:          # keep the table small
                self.login_fails.clear()
        LOG.warning("wrong admin password from %s%s", ip,
                    "; locked out for %d min" % (LOGIN_WINDOW_S // 60) if len(fails) >= LOGIN_FAILS else "")
        return False

    def _ask_password(self):
        if not self.app["admin_password"]:
            return self._reply(503, b"The admin page is off: no admin password is set. On the laptop run: "
                               b"sudo bash install/install.sh", "text/plain")
        with self.login_lock:
            locked = len(self.login_fails.get(self.client_address[0], [])) >= LOGIN_FAILS
        if locked:
            return self._reply(429, b"Too many wrong passwords from this address. Try again in 10 minutes.",
                               "text/plain")
        return self._reply(401, b"Admin password needed.", "text/plain",
                           {"WWW-Authenticate": 'Basic realm="P833 receiver admin"'})

    def _same_site(self):
        """Refuse admin form posts that don't come from the admin page itself. Browsers send
        Origin (or at least Referer) with a form post; a post with neither is refused."""
        host = self.headers.get("Host", "")
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if v:
                return bool(host) and urlsplit(v).netloc == host
        return False

    def _back(self, msg):
        return self._reply(303, b"", "text/plain", {"Location": "/admin?msg=" + quote(msg)})

    def _https(self):
        return isinstance(self.connection, ssl.SSLSocket)

    def _needs_https(self):
        """True when the admin page must be used over HTTPS and this connection is plain."""
        return self.server is not None and getattr(self.server, "tls", None) is not None and not self._https()

    def _to_https(self):
        host = self.headers.get("Host", "")
        if not re.match(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$", host):
            return self._reply(400, b"Use https:// for the admin page.", "text/plain")
        return self._reply(302, b"", "text/plain", {"Location": "https://%s%s" % (host, self.path)})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/admin/") and self._needs_https():
            return self._reply(403, b"Use https:// for the admin page.", "text/plain")
        if path.startswith("/admin/"):
            try:
                return self._admin_post(path)
            except Exception as e:      # never drop the connection on a bug; show and log it
                LOG.exception("admin page error on %s", path)
                return self._reply(500, ("Error: %s: %s (details in the receiver log)"
                                         % (e.__class__.__name__, e)).encode(), "text/plain; charset=utf-8")
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
        if not isinstance(board, str) or not BOARD_ID.match(board):
            return self._reply(400, {"ok": False, "error": "board missing"})
        extra = {}
        for k in ("fw", "rssi", "uptime_s", "boot", "dropped"):
            if k in body:
                v = body[k]
                extra[k] = v if isinstance(v, (int, float)) and not isinstance(v, bool) else plain(v)
        prev = self.app["state"].heard_info.get(board, {})
        before = prev.get("dropped", 0) if prev.get("boot") == extra.get("boot") else 0
        if isinstance(extra.get("dropped"), int) and isinstance(before, int) and extra["dropped"] > before:
            LOG.warning("board %s (%s) lost %d call(s): its queue was full (could it reach the receiver?)",
                        board, self.app["state"].cell_name(board), extra["dropped"] - before)
        self.app["state"].heard(board, self.client_address[0], extra)
        return self._reply(200, {"ok": True})

    def _call(self, body):
        call, err = check_call(body)
        if err:
            LOG.warning("bad call from %s: %s (%s)", self.client_address[0], err, json.dumps(body)[:200])   # escaped: no line breaks
            return self._reply(400, {"ok": False, "error": err})
        try:
            repeat, _ = accept_call(self.app, call, self.client_address[0])
        except Busy as e:
            return self._reply(503, {"ok": False, "error": str(e)}, headers={"Retry-After": "10"})
        except Refused as e:
            return self._reply(400, {"ok": False, "error": str(e)})
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
            call, err = check_call({"board": board, "boot": st.ui_boot, "seq": st.next_ui_seq(),
                                    "code": code, "press": press})
            if err:
                return self._back("Test call not sent: " + err)
            LOG.info("test call from admin page (%s)", who)
            try:
                repeat, en = accept_call(self.app, call, None, note="admin page test")
            except Busy as e:
                return self._back("Test call not sent: %s; try again in a moment." % e)
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
            if self.app["worker"].jobs.qsize() >= MAX_WAITING_PAGES:
                return self._back("Custom page not sent: too many pages waiting; try again in a moment.")
            LOG.info("CUSTOM page from admin page (%s) zone %s: %s | %s", who, zone, en, es)
            self.app["worker"].jobs.put(rec)
            if self.app.get("sms"):
                self.app["sms"].text_for_call(rec, test_call=True)
            return self._back("Custom page sent to zone %s: %s" % (zone, en))

        if path == "/admin/tuning":
            t, errs = parse_tuning(form, self.app["cfg"], self.app["worker"].audio.tone_names())
            if errs:
                return self._back("Not saved: " + "; ".join(errs) + ".")
            st.set_tuning(t, who)
            return self._back("Phone and paging settings saved.")

        if path == "/admin/tuning/reset":
            st.reset_tuning(who)
            return self._back("Phone and paging settings reset to the config file's values.")

        if path == "/admin/phonetest":
            live = self.app["worker"].live
            if not hasattr(live, "connection_test"):
                return self._back("The phone pager isn't available.")
            digits = form.get("digits", "").replace(" ", "").upper()
            wav, what = None, "the bell"
            if digits:
                lib = self.app["worker"].audio
                try:
                    if form.get("play", "").startswith("page"):
                        # Exactly what a real call of that code at press 27 would play.
                        try:
                            code = int(form.get("play")[4:] or 2)
                        except ValueError:
                            code = 2
                        if code not in self.app["cfg"]["codes"]:
                            code = 2
                        wav, _notes = lib.build(self.app["cfg"], code, 27)
                        what = "a full test page (code %d %s, press 27)" % (code, self.app["cfg"]["codes"][code]["who"])
                    else:
                        # The first code's department tone (the maintenance bell), twice.
                        tone = self.app["cfg"]["codes"][2].get("tone", "")
                        one = lib._frames("tones/single/%s.wav" % tone) if tone else lib._frames("chime.wav")
                        gap = b"\x00\x00" * (8 * int(self.app["cfg"]["audio"].get("tone_gap_ms", 250)))
                        wav = lib._wrap(b"", one + gap + one, {"repeat": 1, "lead_silence_ms": 0,
                                                               "gain_db": self.app["cfg"]["audio"].get("gain_db", 0)})
                except AudioError as e:
                    return self._back("Phone test not run: no audio to play (%s)" % e)
            LOG.info("phone %s from admin page (%s)", "digit trial %s" % digits if digits else "connection test", who)
            ok, detail = live.connection_test(digits=digits, wav=wav, what=what)
            if ok and getattr(live, "last_check", ""):
                detail += " WARNING: " + live.last_check
            LOG.info("phone connection test: %s", detail)
            st.record({"time": now_text(), "board": "admin", "cell": "Admin page (%s)" % who,
                       "code": "phone test", "who": "Admin", "message": "phone test: " + what,
                       "result": "sent" if ok else "FAILED", "attempts": 1,
                       "detail": "dialed %s; %s" % (digits or "nothing", detail)})
            rec = getattr(live, "last_recording", b"")
            note = ""
            if rec:
                try:
                    with open(os.path.join(self.app["state_dir"], "last_phone_test.wav"), "wb") as f:
                        f.write(rec)
                    note = (" Recorded %.1f s of what the line sent back: listen under 'Last phone test' below."
                            % ((len(rec) - 44) / 16000.0))
                except OSError as e:
                    LOG.warning("could not save the phone test recording: %s", e)
            return self._back(("Phone test OK: " if ok else "Phone test FAILED: ") + detail + note)

        if path == "/admin/voicelab":
            try:
                wav = lab_wav(self.app, form)
            except AudioError as e:
                if form.get("action") == "preview":
                    return self._reply(400, ("Voice lab: %s" % e).encode(), "text/plain; charset=utf-8")
                return self._back("Voice lab: %s" % e)
            if form.get("action") == "preview":
                return self._reply(200, wav, "audio/wav")
            live = self.app["worker"].live
            zone = form.get("zone", "floor")
            dial = self.app["cfg"]["zones"].get(zone, {}).get("dial", "")
            if not hasattr(live, "connection_test") or not dial:
                return self._back("Voice lab: zone %s has no dial digits set." % zone)
            LOG.info("voice lab page from admin page (%s), voice %s: %s", who, form.get("voice"),
                     form.get("text", "")[:1500])
            ok, detail = live.connection_test(digits=dial, wav=wav, what="the voice lab text")
            st.record({"time": now_text(), "board": "admin", "cell": "Admin page (%s)" % who,
                       "code": "voice lab", "who": "Admin", "message": form.get("text", "")[:1500],
                       "result": "sent" if ok else "FAILED", "attempts": 1, "detail": detail})
            return self._back(("Voice lab page OK: " if ok else "Voice lab page FAILED: ") + detail)

        if path.startswith("/admin/voice/"):
            return self._voice_post(path, form, who)

        if path == "/admin/sms":
            sms_form, errs = parse_sms_form(form)
            if errs:
                return self._back("Text settings not saved: " + "; ".join(errs) + ".")
            pw = form.get("api_password", "")
            if form.get("clear_password") == "on":
                self.app["sms"].set_password("")
                LOG.info("voip.ms API password removed on the admin page (%s)", who)
            elif pw and not (self._https() or who in ("127.0.0.1", "::1")):
                st.set_sms(sms_form, who)
                return self._back("Text settings saved, but NOT the API password: this admin page isn't "
                                  "encrypted (no HTTPS certificate), so it would cross the network as plain "
                                  "text. Run sudo bash install/install.sh on the laptop, then enter it again.")
            elif pw:
                self.app["sms"].set_password(pw)
                LOG.info("voip.ms API password changed on the admin page (%s)", who)
            st.set_sms(sms_form, who)
            return self._back("Text message settings saved. Texts are %s." % (
                "ON" if sms_form["enabled"] else "off"))

        if path == "/admin/sms/send":
            message = form.get("message", "")
            if len(message) > 300:
                return self._back("Text not sent: the message is too long.")
            ok, detail = self.app["sms"].manual_text(form.get("number", ""), message,
                                                     add_prefix=form.get("add_prefix") == "on")
            if ok:
                LOG.info("manual text from the admin page (%s): %s", who, detail)
            return self._back(("Text " if ok else "Text not sent: ") + detail)

        if path == "/admin/sms/test":
            LOG.info("test text from the admin page (%s)", who)
            ok, detail = self.app["sms"].test_text()
            return self._back(("Test text OK: " if ok else "Test text FAILED: ") + detail)

        if path == "/admin/dryrun":
            if form.get("dry_run") not in ("on", "off"):
                return self._back("Dry run not changed.")
            on = form.get("dry_run") == "on"
            st.set_dry_run(on, who)
            return self._back("Dry run is now %s." % ("ON: pages are logged, not dialed" if on
                                                       else "OFF: pages will be dialed"))

        return self._reply(404, {"ok": False, "error": "not found"})

    def _voice_post(self, path, form, who):
        """The Page voice card: preview, save, back to the recordings, make all, clear."""
        cfg, st, lib = self.app["cfg"], self.app["state"], self.app["worker"].audio
        pv = page_voice(self.app)
        if path == "/admin/voice/recorded":
            vs = p833_voice.voice_settings(cfg)
            vs["source"] = "recorded"
            pv.stop_making_all()
            st.set_voice(vs, who)
            return self._back("Pages use the recorded clips again. (Your Piper settings are kept in the form.)")
        if path == "/admin/voice/stop":
            pv.stop_making_all()
            return self._back("Stopped making messages ahead of time.")
        if path == "/admin/voice/clear":
            pv.stop_making_all()
            pv.clear()
            return self._back("Kept page audio removed. Pages will be spoken again as they come in.")
        if path == "/admin/voice/make_all":
            if not pv.active():
                return self._back("Save a Piper page voice first.")
            if not pv.start_making_all():
                return self._back("Already making the messages; see the Page voice box.")
            return self._back("Making every message (codes 2-9, presses 0-100, English and Spanish) in the "
                              "background. Pages go first; progress is in the Page voice box.")
        preview = path == "/admin/voice/preview"
        if not tts_ready(cfg):
            msg = "Piper isn't installed on this laptop (sudo bash install/install_tts.sh)."
            return (self._reply(400, msg.encode(), "text/plain; charset=utf-8") if preview
                    else self._back("Page voice not saved: " + msg))
        vs, errs = p833_voice.parse_voice_form(form, tts_voices(cfg))
        if preview:
            try:
                code, press = int(form.get("code", "2")), int(form.get("press", "27"))
                if code not in cfg["codes"] or not 0 <= press <= 999:
                    raise ValueError
            except ValueError:
                errs.append("pick a code, and a press from 0 to 999")
            if errs:
                return self._reply(400, ("Page voice: " + "; ".join(errs) + ".").encode(), "text/plain; charset=utf-8")
            try:
                wav, notes = lib.build(cfg, code, press, vs=vs, urgent=False, timeout=p833_voice.SLOW_TIMEOUT_S)
            except AudioError as e:
                return self._reply(400, ("Page voice: %s" % e).encode(), "text/plain; charset=utf-8")
            failed = [n for n in notes if n.startswith("Piper voice failed")]
            if failed:
                return self._reply(400, ("Page voice: " + failed[0]).encode(), "text/plain; charset=utf-8")
            return self._reply(200, wav, "audio/wav")
        if path == "/admin/voice/save":
            if errs:
                return self._back("Page voice not saved: " + "; ".join(errs) + ".")
            pv.stop_making_all()
            st.set_voice(vs, who)
            pv.saved_settings_changed()
            pv.warm_up()
            return self._back("Page voice saved: %s. Pages are now spoken live and each message is kept for next "
                              "time. To have every message ready ahead, click Make all messages now."
                              % p833_voice.describe(vs))
        return self._reply(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        self._request_read()
        parts = urlsplit(self.path)
        if (parts.path == "/admin" or parts.path.startswith("/admin/")) and self._needs_https():
            return self._to_https()
        if parts.path == "/status.json":
            return self._reply(200, status_obj(self.app))
        if parts.path in ("/", "/index.html"):
            return self._reply(200, page_html(self.app, False).encode(), "text/html; charset=utf-8")
        if parts.path == "/admin/preview":
            if not self._admin_ok():
                return self._ask_password()
            q = {k: v[0].strip() for k, v in parse_qs(parts.query).items()}
            return self._reply(200, preview_obj(self.app, q))
        if parts.path in ("/admin/last_phone_test.wav", "/admin/last_page_line.wav",
                          "/admin/last_failed_page_line.wav"):
            if not self._admin_ok():
                return self._ask_password()
            try:
                with open(os.path.join(self.app["state_dir"], parts.path.rsplit("/", 1)[1]), "rb") as f:
                    return self._reply(200, f.read(), "audio/wav")
            except OSError:
                return self._reply(404, b"Nothing recorded yet.", "text/plain")
        if parts.path == "/admin/listen":
            if not self._admin_ok():
                return self._ask_password()
            q = parse_qs(parts.query)
            if q.get("kind", [""])[0] == "custom":
                en, es = q.get("en", [""])[0].strip(), q.get("es", [""])[0].strip()
                if not en or len(en) > 200 or len(es) > 200 or not (en + es).isprintable():
                    return self._reply(400, b"English text is needed, each text up to 200 characters",
                                       "text/plain")
                try:
                    wav = self.app["worker"].audio.build_custom(self.app["cfg"], en, es)
                except AudioError as e:
                    return self._reply(404, ("No audio: %s" % e).encode(), "text/plain; charset=utf-8")
                return self._reply(200, wav, "audio/wav")
            try:
                code, press = int(q.get("code", [""])[0]), int(q.get("press", [""])[0])
                if not (2 <= code <= 9 and 0 <= press <= 999):
                    raise ValueError
            except ValueError:
                return self._reply(400, b"code 2-9 and press 0-999 needed", "text/plain")
            try:
                wav, _notes = self.app["worker"].audio.build(self.app["cfg"], code, press)
            except AudioError as e:
                return self._reply(404, ("No audio: %s" % e).encode(), "text/plain; charset=utf-8")
            return self._reply(200, wav, "audio/wav")
        if parts.path == "/admin":
            if not self._admin_ok():
                return self._ask_password()
            msg = parse_qs(parts.query).get("msg", [""])[0]
            return self._reply(200, page_html(self.app, True, msg).encode(), "text/html; charset=utf-8")
        return self._reply(404, {"ok": False, "error": "not found"})


def preview_obj(app, q):
    """What a test call or custom page would say and play, without sending it."""
    cfg, st, lib = app["cfg"], app["state"], app["worker"].audio
    mode = ("dry run: will be logged, not dialed" if st.dry_run else "LIVE: will be dialed")
    plays = int(cfg["audio"].get("repeat", 2))
    if q.get("kind") == "custom":
        zone, en, es = q.get("zone", ""), q.get("en", ""), q.get("es", "")
        if zone not in cfg["zones"]:
            return {"ok": False, "error": "pick a zone"}
        if not en or len(en) > 200 or len(es) > 200:
            return {"ok": False, "error": "English text is needed, each text up to 200 characters"}
        if tts_ready(cfg) and lib.voice is not None:
            audio = "/admin/listen?" + urlencode({"kind": "custom", "en": en, "es": es})
            notes = ["Spoken by Piper with the page voice (%s)." % p833_voice.describe(lib.voice.settings())]
        else:
            audio, notes = None, ["Custom text needs Piper on the laptop to be spoken (sudo bash "
                                  "install/install_tts.sh). Without it, a live custom page fails."]
        return {"ok": True, "from": "Admin page", "en": en, "es": es, "zone": zone,
                "dial": cfg["zones"][zone].get("dial", ""), "tone": "chime", "plays": plays, "mode": mode,
                "audio": audio, "notes": notes}
    try:
        code, press = int(q.get("code", "")), int(q.get("press", ""))
    except ValueError:
        return {"ok": False, "error": "call and press must be numbers"}
    board = q.get("board", "")
    call, err = check_call({"board": board or "x", "seq": 0, "code": code, "press": press})
    if err:
        return {"ok": False, "error": err}
    en, es = build_message(cfg, code, press)
    c = cfg["codes"][code]
    style = tone_style(cfg)
    tone = "off" if style == "off" else (re.sub(r"^\d+_", "", c.get("tone", "")).replace("_", " ") or "chime")
    if style == "twice" and tone != "chime":
        tone += " (twice)"
    notes, audio = [], "/admin/listen?code=%d&press=%d" % (code, press)
    try:
        _, notes = lib.build(cfg, code, press)
    except AudioError as e:
        notes, audio = ["No audio: %s. A live page would fail." % e], None
    if press > lib.max_press and not lib.piper_settings():
        en, es = build_message(cfg, code, 0)
    langs = cfg["audio"].get("languages", ["en", "es"])
    return {"ok": True, "from": "%s (%s)" % (st.cell_name(board), board) if board else "-",
            "en": en if "en" in langs else "", "es": es if "es" in langs else "",
            "zone": c["zone"], "dial": cfg["zones"][c["zone"]].get("dial", ""), "tone": tone,
            "plays": plays, "mode": mode, "audio": audio, "notes": notes}


def tls_context(cfg):
    """The HTTPS settings for the admin page, or (None, reason) if it can't be used."""
    if not cfg.get("admin_https", True):
        return None, "admin_https is off in the config"
    cert, key = cfg.get("tls_cert", ""), cfg.get("tls_key", "")
    if not (os.path.exists(cert) and os.path.exists(key)):
        return None, "no certificate at %s (run: sudo bash install/install.sh)" % cert
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if hasattr(ctx, "minimum_version"):
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2   # novm (3.7+, checked above)
    else:                                                    # Python 3.6
        ctx.options |= ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1
    try:
        ctx.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as e:
        return None, "could not load the certificate: %s" % e
    return ctx, ""


class Server(socketserver.ThreadingMixIn, HTTPServer):
    """One thread per connection, at most MAX_CONNECTIONS at once: more are closed straight
    away, so a flood of idle connections can't use up the service (each also times out).

    With a TLS context, each connection's first byte decides: a TLS hello (0x16) gets
    HTTPS, anything else plain HTTP. So the admin page can use HTTPS on the same port
    while the boards keep sending plain HTTP, with no firmware change."""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *a, tls=None, **k):
        self.tls = tls
        self.conn_lock = threading.Lock()
        self.open_by_ip = {}           # address -> open connections
        self.refused_logged = {}       # address -> when a refusal was last logged
        self._local = threading.local()
        super().__init__(*a, **k)

    # -- how many connections, from whom
    def _known(self, ip):
        """Boards the receiver has heard from, and the laptop itself, get the reserved slots."""
        if ip in ("127.0.0.1", "::1"):
            return True
        app = getattr(self.RequestHandlerClass, "app", None) or {}
        st = app.get("state")
        return bool(st) and any(b.get("ip") == ip for b in list(st.heard_info.values()))

    def _take_slot(self, ip):
        with self.conn_lock:
            total = sum(self.open_by_ip.values())
            mine = self.open_by_ip.get(ip, 0)
            limit = MAX_CONNECTIONS if self._known(ip) else MAX_CONNECTIONS - RESERVED_FOR_BOARDS
            if total >= limit or mine >= MAX_PER_ADDRESS:
                now = time.time()
                if now - self.refused_logged.get(ip, 0) > 60:
                    self.refused_logged[ip] = now
                    if len(self.refused_logged) > 1000:
                        self.refused_logged.clear()
                    LOG.warning("refused connections from %s: %d open from it, %d in all", ip, mine, total)
                return False
            self.open_by_ip[ip] = mine + 1
            return True

    def _free_slot(self, ip):
        with self.conn_lock:
            n = self.open_by_ip.get(ip, 1) - 1
            if n > 0:
                self.open_by_ip[ip] = n
            else:
                self.open_by_ip.pop(ip, None)

    def process_request(self, request, client_address):
        if not self._take_slot(client_address[0]):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._free_slot(client_address[0])
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._free_slot(client_address[0])

    def handle_error(self, request, client_address):
        """A connection cut off (deadline, client gone) is normal: no traceback for it."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, ssl.SSLError)):
            return
        LOG.exception("web request from %s failed", client_address[0])

    # -- the whole request must arrive within REQUEST_DEADLINE_S (a client that trickles a
    # byte now and then can't hold a slot); the handler says when it has read the request.
    def request_read(self):
        t = getattr(self._local, "deadline", None)
        if t:
            t.cancel()

    @staticmethod
    def _cut(box):
        try:
            box[0].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish_request(self, request, client_address):
        box = [request]
        timer = threading.Timer(REQUEST_DEADLINE_S, self._cut, args=(box,))
        timer.daemon = True
        self._local.deadline = timer
        timer.start()
        try:
            self._serve(request, client_address, box)
        finally:
            timer.cancel()

    def _serve(self, request, client_address, box):
        if self.tls is None:
            return super().finish_request(request, client_address)
        request.settimeout(Handler.timeout)
        try:
            first = request.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if first != b"\x16":
            return super().finish_request(request, client_address)
        try:
            conn = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return                      # e.g. the browser said no to the certificate this time
        box[0] = conn
        try:
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ------------------------------------------------------------------ pages ---

def status_obj(app, admin=False):
    """The status for the pages. The open (no password) version leaves out board addresses
    and each call's detail, which names the phone line and the digits that page the floor."""
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
                "ip": b.get("ip", "") if admin else "",
                "last_heard": now_text(last) if last else "never",
                "seconds_ago": int(now - last) if last else None,
                "ok": bool(last) and now - last <= cfg["board_silent_after_s"],
                "fw": b.get("fw", ""), "rssi": b.get("rssi", ""),
            })
        calls = list(reversed(st.calls))
    if not admin:
        calls = [dict(c, detail="", note="") for c in calls]
    return {"version": VERSION, "dry_run": st.dry_run,
            "phone_pager_built": getattr(app["worker"].live, "built", True),
            "audio_voices": app["worker"].audio.manifest.get("voices", {}),
            "audio_problems": app["worker"].audio.check(cfg),
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
.card table td{vertical-align:top}
input,select,button{font-size:15px;padding:4px 6px}
button{cursor:pointer} .msg{padding:8px 12px;border:2px solid var(--good);margin-bottom:16px;max-width:820px}
.small{font-size:13px;opacity:.8} form.inline{display:inline;margin:0}
.preview{margin-top:10px;padding:8px 10px;border-left:4px solid var(--good)} .preview:empty{display:none}
.preview audio{margin-top:8px;width:100%;max-width:520px}
"""

def parse_sms_form(form):
    """Check the admin page's text message settings. Returns (settings, errors). The
    password is handled separately and never put in the settings."""
    import string
    errs = []
    out = {"enabled": form.get("enabled") == "on", "admin_tests": form.get("admin_tests") == "on"}
    user = form.get("api_username", "").strip()
    if len(user) > 100 or (user and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", user)):
        errs.append("the voip.ms account must be an e-mail address")
    out["api_username"] = user
    did = form.get("did", "").strip()
    if did and not p833_sms.clean_number(did):
        errs.append("the sending number must be a 10-digit number")
    out["did"] = p833_sms.clean_number(did) if did else ""
    to = []
    for line in re.split(r"[\n,;]+", form.get("to", "")):
        if line.strip():
            n = p833_sms.clean_number(line)
            if not n:
                errs.append("not a 10-digit number: %s" % line.strip()[:20])
            elif n not in to:
                to.append(n)
    if len(to) > 20:
        errs.append("at most 20 numbers")
    out["to"] = to
    prefix = form.get("prefix", "").strip()
    if len(prefix) > 30 or not prefix.isprintable():
        errs.append("the prefix must be up to 30 characters")
    out["prefix"] = prefix
    template = " ".join(form.get("template", "{prefix} {message}").split())
    try:
        parts = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(template) if f is not None]
    except ValueError:
        parts = None
    names = [f for f, _, _ in parts or []]
    if parts is None or len(template) > 200 or "message" not in names or any(spec or conv for _, spec, conv in parts) \
            or any(n not in ("prefix", "message", "cell", "press", "who", "code") for n in names):
        errs.append("the template must contain {message} and only {prefix} {message} {cell} {press} {who} {code}")
    out["template"] = template
    for key, label, lo, hi, default in (("stagger_min_s", "the shortest pause between texts", 0, 120, 1),
                                        ("stagger_max_s", "the longest pause between texts", 0, 300, 10),
                                        ("max_per_hour", "the hourly limit", 1, 1000, 60)):
        try:
            out[key] = int(form.get(key, str(default)) or str(default))
            if not lo <= out[key] <= hi:
                raise ValueError
        except ValueError:
            errs.append("%s must be %d to %d" % (label, lo, hi))
    if not errs and out["stagger_max_s"] < out["stagger_min_s"]:
        errs.append("the longest pause must be at least the shortest")
    return out, errs


def sms_html(app):
    e = html.escape
    sms = app.get("sms")
    if not sms:
        return ""
    s = p833_sms.settings_of(app["cfg"])
    rows = []
    for r in reversed(sms.recent[-10:]):
        rows.append("<tr class='%s'><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            "bad" if r["result"] != "sent" else "", e(r["time"]), e(r["to"]), e(r["result"]), e(r["detail"])))
    example = p833_sms.compose(app["cfg"], {"message": "Maintenance requested to press 27 for camera fault.",
                                            "cell": "RSP LH", "press": 27, "who": "Maintenance", "code": 8})
    return """
<div class=card><h2>Text messages (voip.ms)</h2>
<h3>Send a text</h3>
<form method=post action='/admin/sms/send'>
<label>To <input name=number required maxlength=20 size=14 placeholder='5551234567'></label>
<label>Message <input name=message required maxlength=160 size=48></label>
<label><input type=checkbox name=add_prefix checked> start with the prefix</label>
<button>Send text</button></form>
<p class=small>One text to one number, through the voip.ms account below. Works even with automatic texts off.
Up to 160 characters, counted against the hourly limit; the result shows under Recent texts.</p>
<h3>Automatic texts for calls</h3>
<p>Texts are <b>%s</b>. When on, every call from a cell is also sent as a text, one way, for example:<br>
<i>%s</i></p>
<form method=post action='/admin/sms'><div class=w><table>
<tr><td>Send texts</td><td><input type=checkbox name=enabled%s></td><td class=small>Off until you're ready.
Pages don't depend on texts: a text that fails never holds up a page.</td></tr>
<tr><td>voip.ms account e-mail</td><td><input name=api_username value='%s' size=28 maxlength=100></td>
<td class=small>voip.ms: Main Menu, SOAP and REST/JSON API. Enable the API there and add this site's
public internet address to its allowed IP list (a test text shows it if it's missing). voip.ms allows
100 texts a day unless you ask messaging@voip.ms for more.</td></tr>
<tr><td>voip.ms API password</td><td><input name=api_password type=password size=28 maxlength=200
 autocomplete=new-password placeholder='%s'><br><label class=small><input type=checkbox name=clear_password>
 remove the stored password</label></td><td class=small>The API password set on that voip.ms page (not your
login password). It's stored on the laptop, readable only by the paging service, and never shown here again.
Leave blank to keep the current one.</td></tr>
<tr><td>Send from (voip.ms number)</td><td><input name=did value='%s' size=14 maxlength=20></td>
<td class=small>Your SMS-enabled voip.ms number (DID), 10 digits.</td></tr>
<tr><td>Text these numbers</td><td><textarea name=to rows=4 cols=18>%s</textarea></td>
<td class=small>One 10-digit number per line, up to 20. Each number gets its own text (voip.ms
doesn't do group chats through its API).</td></tr>
<tr><td>Pause between texts (s)</td><td><input name=stagger_min_s type=number min=0 max=120 value='%d'
 style='width:4em'> to <input name=stagger_max_s type=number min=0 max=300 value='%d' style='width:4em'></td>
<td class=small>Texts go to one number at a time, with a random pause in this range between them, so the
carrier doesn't flag a burst or a fixed rhythm as spam.</td></tr>
<tr><td>Most texts per hour</td><td><input name=max_per_hour type=number min=1 max=1000 value='%d'
 style='width:6em'></td><td class=small>A cap on cost and spam: texts over it are skipped and logged.</td></tr>
<tr><td>Prefix</td><td><input name=prefix value='%s' size=14 maxlength=30></td><td></td></tr>
<tr><td>Message</td><td><input name=template value='%s' size=28 maxlength=200></td>
<td class=small>{prefix} {message} and optionally {cell} {press} {who} {code}. Up to 160 characters are sent.</td></tr>
<tr><td>Text test calls too</td><td><input type=checkbox name=admin_tests%s></td>
<td class=small>Also text for test calls and custom pages sent from this admin page.</td></tr>
</table></div><button>Save text settings</button></form>
<form method=post action='/admin/sms/test' style='margin-top:8px'
 onsubmit="return confirm('Send a test text to every number on the list now? (Works even with texts off.)')">
<button>Send a test text</button></form>
<h3>Recent texts</h3><div class=w><table><tr><th>Time</th><th>To</th><th>Result</th><th>Detail</th></tr>%s</table></div>
<p class=small>Full log: /var/log/p833-receiver/texts.csv. The laptop needs to reach voip.ms on the internet
(HTTPS, port 443) for texts to go out.</p></div>
""" % ("ON" if s["enabled"] else "off", e(example), " checked" if s["enabled"] else "", e(s["api_username"]),
       "set: leave blank to keep" if sms.has_password() else "not set", e(s["did"]), e("\n".join(s["to"])),
       int(s["stagger_min_s"]), int(s["stagger_max_s"]), int(s["max_per_hour"]), e(s["prefix"]), e(s["template"]), " checked" if s["admin_tests"] else "",
       "\n".join(rows) or "<tr><td colspan=4>none yet</td></tr>")


def voice_html(app):
    """The Page voice card: tune Piper settings, hear a page with them, save them for pages."""
    cfg, e = app["cfg"], html.escape
    vs = p833_voice.voice_settings(cfg)
    voices = tts_voices(cfg)
    using = ("<b>Pages now use %s.</b>" % e(p833_voice.describe(vs)))
    if not voices:
        return ("<div class=card><h2>Page voice</h2><p>%s</p><p class=small>To speak pages live with a voice, "
                "speed and number style you choose, install Piper on this laptop once (needs internet the "
                "first time):</p><pre>sudo bash install/install_tts.sh</pre></div>" % using)
    pv = page_voice(app)
    kept, mb = pv.stats()
    job = dict(pv.job)
    if job.get("running"):
        jobline = ("Making messages ahead of time: %d of %d checked, %d made, %d failed. "
                   "<form class=inline method=post action='/admin/voice/stop'><button>Stop</button></form>"
                   % (job.get("done", 0), job.get("total", 0), job.get("made", 0), job.get("failed", 0)))
    elif job.get("finished"):
        jobline = "Last run of Make all messages: %d made, %d failed%s." % (
            job.get("made", 0), job.get("failed", 0), " (%s)" % e(job["error"]) if job.get("error") else "")
    else:
        jobline = ""
    rows = []
    for lang, name, eg in (("en", "English", "two, seven"), ("es", "Spanish", "dos, siete")):
        lv = vs[lang]
        vopts = "".join("<option%s>%s</option>" % (" selected" if v == lv["voice"] else "", e(v))
                        for v in voices if v.startswith(lang + "_"))
        rows.append(
            "<tr><td><b>%s</b></td><td><select name=%s_voice>%s</select></td>"
            "<td><input name=%s_pace type=number min=0.5 max=2.5 step=0.05 value='%.2f' style='width:5em'></td>"
            "<td><label><input type=checkbox name=%s_digits%s> digit by digit (%s)</label></td>"
            "<td><input name=%s_digit_pause_ms type=number min=0 max=1500 step=50 value='%d' style='width:5em'> ms</td></tr>"
            % (name, lang, vopts, lang, float(lv["pace"]), lang, " checked" if lv["digits"] else "", eg,
               lang, int(lv["digit_pause_ms"])))
    copts = "".join("<option value='%d'>%d: %s</option>" % (c, c, e(cfg["codes"][c]["who"])) for c in sorted(cfg["codes"]))
    return """
<div class=card><h2>Page voice</h2>
<p>%s</p>
<p class=small>Try settings here, hear a whole page with them, then save. Once saved, each page is spoken
live by Piper on this laptop and the spoken message is kept, so the same message plays at once next time.
If Piper ever fails or is slow, that sentence uses the recorded clip, so a page never fails because of it.
Speed: 1 is normal, higher is slower. Pause between digits: 0 says them like the recordings ("two, seven");
a number such as 300 says each digit on its own with that much silence between.</p>
<form id=voiceform method=post action='/admin/voice/save'><div class=w><table>
<tr><th></th><th>Voice</th><th>Speed</th><th>Press number</th><th>Pause between digits</th></tr>
%s
</table></div>
<label>Hear code <select name=code>%s</select></label>
<label>at press <input name=press type=number min=0 max=999 value=27 style='width:5em'></label>
<button type=button onclick="p833Voice()">Preview (in this browser)</button>
<button onclick="return confirm('Use these settings for every page from now on?')">Save as the page voice</button>
</form><div id=prev-voice class=preview></div>
<p class=small>Kept for the saved voice: %d message(s); all kept audio: %.1f MB. %s</p>
<form class=inline method=post action='/admin/voice/make_all'><button%s>Make all messages now</button></form>
<form class=inline method=post action='/admin/voice/recorded'><button%s>Go back to the recorded clips</button></form>
<form class=inline method=post action='/admin/voice/clear'
 onsubmit="return confirm('Remove all kept page audio? Pages will be spoken again as they come in.')">
<button>Remove kept audio</button></form>
<p class=small>Make all messages now speaks every message (codes 2-9, presses 0-100, both languages) in the
background, a few minutes of work, so no page waits for Piper. Pages always go first.</p></div>
""" % (using, "\n".join(rows), copts, kept, mb, jobline,
       "" if vs["source"] == "piper" else " disabled", "" if vs["source"] == "piper" else " disabled")


def lab_html(app):
    cfg, e = app["cfg"], html.escape
    voices = tts_voices(cfg)
    if not voices:
        return ("<div class=card><h2>Voice lab</h2><p>Not installed on this laptop. To try voices, speeds and "
                "pauses here, run once (needs internet the first time):</p><pre>sudo bash install/install_tts.sh</pre>"
                "</div>")
    current = cfg["audio"].get("lab_voice") or "en_US-hfc_female-medium"
    vopts = "".join("<option%s>%s</option>" % (" selected" if v == current else "", e(v)) for v in voices)
    tones = app["worker"].audio.tone_names()
    topts = "<option value=''>no tone</option>" + "".join(
        "<option value='%s'>%s</option>" % (e(t), e(re.sub(r"^\d+_", "", t).replace("_", " "))) for t in tones)
    zopts = "".join("<option>%s</option>" % e(z) for z in cfg["zones"])
    return """
<div class=card><h2>Voice lab</h2>
<p class=small>Speak any text with Piper, right here, to try voices, speed and pauses. Write a pause as
<b>[350]</b> (milliseconds); each part between pauses is spoken on its own. Numbers can be written as words
("two. [350] Seven.") or digits. Preview plays it in this browser at the page volume; nothing is sent.
"Page it" dials the zone and plays it on the speakers (works with dry run on).</p>
<form id=labform method=post action='/admin/voicelab'>
<label>Voice <select name=voice>%s</select></label>
<label>Speed <input name=pace type=number min=0.5 max=2.5 step=0.05 value=1.25 style='width:5em'></label>
<span class=small>(1 = normal, higher = slower)</span><br>
<label>Tone first <select name=tone>%s</select></label>
<label>Plays <select name=plays><option>1</option><option>2</option></select></label><br>
<textarea name=text rows=3 cols=70 maxlength=1500>%s</textarea><br>
<button type=button onclick="p833Lab()">Preview (in this browser)</button>
<label>Zone <select name=zone>%s</select></label>
<button name=action value=page onclick="return confirm('Page this on the speakers now?')">Page it</button>
</form><div id=prev-lab class=preview></div></div>
""" % (vopts, topts, e(LAB_DEFAULT_TEXT), zopts)


PREVIEW_JS = """<script>
function p833Lab(){
  dirty=true;
  var f=document.getElementById('labform'), box=document.getElementById('prev-lab');
  var fd=new FormData(f); fd.set('action','preview');
  box.innerHTML='Speaking... (a few seconds)';
  fetch('/admin/voicelab',{method:'POST',body:new URLSearchParams(fd),credentials:'same-origin'}).then(function(r){
    if(!r.ok){return r.text().then(function(t){box.innerHTML=p833Esc(t);});}
    return r.blob().then(function(b){
      box.innerHTML='<audio controls autoplay src="'+URL.createObjectURL(b)+'"></audio>';});
  }).catch(function(e){box.innerHTML='Preview failed: '+p833Esc(e);});
}
function p833Voice(){
  dirty=true;
  var f=document.getElementById('voiceform'), box=document.getElementById('prev-voice');
  box.innerHTML='Speaking... (a few seconds the first time a voice is used)';
  fetch('/admin/voice/preview',{method:'POST',body:new URLSearchParams(new FormData(f)),credentials:'same-origin'}).then(function(r){
    if(!r.ok){return r.text().then(function(t){box.innerHTML=p833Esc(t);});}
    return r.blob().then(function(b){
      box.innerHTML='<audio controls autoplay src="'+URL.createObjectURL(b)+'"></audio>';});
  }).catch(function(e){box.innerHTML='Preview failed: '+p833Esc(e);});
}
function p833Esc(t){var d=document.createElement('div');d.textContent=t==null?'':String(t);return d.innerHTML;}
function p833Preview(kind){
  dirty=true;  // stop the page refreshing while you listen
  var f=document.getElementById(kind+'form'), box=document.getElementById('prev-'+kind);
  var q=new URLSearchParams(new FormData(f)); q.set('kind',kind);
  box.innerHTML='Loading...';
  fetch('/admin/preview?'+q.toString(),{credentials:'same-origin'}).then(function(r){return r.json();}).then(function(p){
    if(!p.ok){box.innerHTML='<b>Not valid:</b> '+p833Esc(p.error);return;}
    var h='<b>This page will say:</b><br>English: &ldquo;'+p833Esc(p.en)+'&rdquo;';
    if(p.es){h+='<br>Spanish: &ldquo;'+p833Esc(p.es)+'&rdquo;';}
    h+='<br><span class=small>From: '+p833Esc(p.from)+' &middot; zone '+p833Esc(p.zone)+' (dial '+p833Esc(p.dial||'not set')+
       ') &middot; tone: '+p833Esc(p.tone)+' &middot; plays '+p833Esc(p.plays)+' time(s) &middot; '+p833Esc(p.mode)+'</span>';
    (p.notes||[]).forEach(function(n){h+='<br><b>Note:</b> '+p833Esc(n);});
    if(p.audio){h+='<br><audio controls autoplay src="'+p.audio+'&t='+Date.now()+'"></audio>';}
    box.innerHTML=h;
  }).catch(function(e){box.innerHTML='Preview failed: '+p833Esc(e);});
}
</script>"""

REFRESH_JS = """<script>
var dirty=false;document.addEventListener('input',function(){dirty=true;});
setInterval(function(){var a=document.activeElement;
if(!dirty&&!(a&&/INPUT|SELECT|TEXTAREA|BUTTON/.test(a.tagName)))location.replace(location.pathname);},10000);
</script>"""


def page_html(app, admin, msg=""):
    s = status_obj(app, admin)
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
                          "onsubmit=\"return confirm(%s)\">"
                          "<input type=hidden name=board value='%s'><button>Remove</button></form></td>"
                          % (e(json.dumps("Remove board %s?" % b["board"])), e(b["board"])))
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
        probs = s["audio_problems"]
        if probs:
            audio_html = "<p><b>Audio problems (%d):</b></p><ul>%s</ul>" % (len(probs), "".join(
                "<li>%s</li>" % e(x) for x in probs[:12])) + ("<p>...</p>" if len(probs) > 12 else "")
            audio_html += ("<p class=small>Clips are made by tools/make_audio.py from the config wording. "
                           "Until fixed, affected pages use the old clips, or fail if a clip is missing.</p>")
        else:
            audio_html = "<p>OK: all messages recorded. Voices: %s.</p>" % e(
                ", ".join("%s %s" % (k, v) for k, v in sorted(s["audio_voices"].items())))
        style = tone_style(cfg)
        audio_html += "<p class=small>Page = %.1f s silence, then %s, English, Spanish, played %d time(s).</p>" % (
            int(cfg["audio"]["lead_silence_ms"]) / 1000.0,
            {"once": "department tone", "twice": "department tone twice", "off": "no tone"}[style],
            int(cfg["audio"].get("repeat", 2)))
        ph = cfg["phone"]
        rows = ["<tr><td>HT813 address</td><td><input name=ht813_ip value='%s' required size=15></td>"
                "<td class=small>The HT813's fixed IP address on the IoT network.</td></tr>" % e(ph["ht813_ip"])]
        rows.append("<tr><td>Line check after dialing</td><td><select name=line_check>%s</select></td>"
                    "<td class=small>Listens to the line while waiting after the digits. Dial tone, a busy or "
                    "reorder tone, or ringing means the page did not open. <b>retry</b>: the page counts as "
                    "failed and is tried again (up to 3 times). <b>warn</b>: plays anyway and notes it in the "
                    "log. <b>off</b>: no check. Needs a wait after dialing of at least 400 ms.</td></tr>" % "".join(
                        "<option%s>%s</option>" % (" selected" if ph.get("line_check", "warn") == m else "", m)
                        for m in ("retry", "warn", "off")))
        rows.append("<tr><td>Dial method</td><td><select name=dial_method>%s</select></td><td class=small>"
                    "2-stage: the HT813 picks up the ATA2 line, the laptop hears dial tone and sends the "
                    "digits as tones (pauses and flash work). 1-stage: the digits go inside the call and the "
                    "HT813 dials them itself.</td></tr>" % "".join(
                        "<option%s>%s</option>" % (" selected" if ph["dial_method"] == m else "", m)
                        for m in ("2-stage", "1-stage")))
        for key, label, lo, hi, help_ in PHONE_NUMBERS:
            rows.append("<tr><td>%s</td><td><input name=%s type=number min=%d max=%d value='%s' required "
                        "style='width:7em'></td><td class=small>%s</td></tr>" % (
                            e(label), key, lo, hi, e(str(ph[key])), e(help_)))
        for z, v in cfg["zones"].items():
            rows.append("<tr><td>Zone <b>%s</b> dial</td><td><input name='dial_%s' value='%s' maxlength=32 "
                        "size=12 placeholder='not set'></td><td class=small>What is dialed on the ATA2 line to "
                        "page this zone. 0-9 * #, a comma = half-second pause, F = hook flash (Link).%s</td></tr>"
                        % (e(z), e(z), e(v.get("dial", "")), " <b>Not set yet.</b>" if not v.get("dial") else ""))
        zone_names = list(cfg["zones"])
        tone_names = app["worker"].audio.tone_names()
        for c in range(2, 10):
            cur = cfg["codes"][c].get("tone", "")
            names = tone_names + ([cur] if cur and cur not in tone_names else [])
            tone_sel = "<option value=''%s>(chime)</option>" % (" selected" if not cur else "") + "".join(
                "<option value='%s'%s>%s</option>" % (e(n), " selected" if n == cur else "",
                                                      e(re.sub(r"^\d+_", "", n).replace("_", " ")))
                for n in names)
            why = cfg["codes"][c].get("why", "")
            rows.append("<tr><td>Code %d %s%s</td><td>zone <select name=zone_%d>%s</select><br>"
                        "tone <select name=tone_%d>%s</select></td><td></td></tr>" % (
                            c, e(cfg["codes"][c]["who"]), " (%s)" % e(why) if why else "", c, "".join(
                                "<option%s>%s</option>" % (" selected" if cfg["codes"][c]["zone"] == z else "", e(z))
                                for z in zone_names), c, tone_sel))
        style = tone_style(cfg)
        rows.append("<tr><td>Department tone</td><td><select name=tones>%s</select></td><td class=small>"
                    "Played before the message each time it plays. Twice = the tone file that plays the "
                    "tone two times.</td></tr>" % "".join(
                        "<option value='%s'%s>%s</option>" % (v, " selected" if style == v else "", label)
                        for v, label in (("once", "once"), ("twice", "twice"), ("off", "off"))))
        rows.append("<tr><td>Silence at the start (ms)</td><td><input name=lead_silence_ms type=number min=0 "
                    "max=10000 step=100 value='%d' style='width:7em'></td><td class=small>Silence at the very "
                    "beginning of the page audio, so the paging system is open before the tone starts. "
                    "2000 to 3000 is usual.</td></tr>" % int(cfg["audio"]["lead_silence_ms"]))
        rows.append("<tr><td>Page volume (dB)</td><td><input name=gain_db type=number min=-6 max=12 value='%d' "
                    "style='width:5em'></td><td class=small>Makes the voice and tone louder before they go out. "
                    "0 = as recorded. 6 is a good start; above that the loud parts are squeezed by a limiter, so "
                    "if it sounds harsh or distorted on the floor, turn it down. Preview and Listen play at this "
                    "volume. Other places to turn it up: the paging amplifier's own volume, and the HT813's FXO "
                    "port <i>Gain TX</i>.</td></tr>" % int(cfg["audio"]["gain_db"]))
        for key, label, help_ in (
                ("tone_gap_ms", "Pause between the two tones (ms)", "When the tone plays twice."),
                ("lang_gap_ms", "Pause between English and Spanish (ms)", ""),
                ("repeat_gap_ms", "Pause before the message repeats (ms)", "")):
            rows.append("<tr><td>%s</td><td><input name=%s type=number min=0 max=3000 step=50 value='%d' "
                        "style='width:6em'></td><td class=small>%s Keep every pause shorter than the time the "
                        "paging feedback eliminator waits before it decides a page has ended (often well under "
                        "a second), or it cuts the page there.</td></tr>" % (
                            label, key, int(cfg["audio"][key]), help_))
        rows.append("<tr><td>Keep audio of the last dry-run pages</td><td><input name=keep_dry_run_pages "
                    "type=number min=0 max=100 value='%d' style='width:5em'></td><td class=small>0 = off (normal). "
                    "Set it to, say, 20 to keep the audio of recent dry-run pages in "
                    "/var/lib/p833-receiver/last_pages/ for listening back. Preview is usually enough.</td></tr>"
                    % int(cfg["audio"]["keep_dry_run_pages"]))
        rows.append("<tr><td>Times the message plays</td><td><input name=repeat type=number min=1 max=5 value='%d' "
                    "style='width:5em'></td><td class=small>Each time: tone, English, Spanish.</td></tr>"
                    % int(cfg["audio"]["repeat"]))
        tuned = "tuning" in app["state"].settings
        phone_html = """
<div class=card><h2>Phone and paging setup</h2>
<p class=small>Change these here while tuning the phone side. They're saved on the laptop, kept through restarts
and updates, and replace the config file's values. %s The HT813 call itself is the next build step;
until then the zones, codes, tones and plays already apply (see Listen and the log).</p>
<form method=post action='/admin/tuning'><div class=w><table>%s</table></div>
<button>Save phone and paging settings</button></form>
<form method=post action='/admin/tuning/reset' style='margin-top:8px'
 onsubmit="return confirm('Throw away the saved phone and paging settings and use the config file values?')">
<button>Reset to config file values</button></form>
<h3>Test the connection to the HT813</h3>
<form method=post action='/admin/phonetest' onsubmit="return confirm('Call the HT813 now? It takes the ATA2 line off-hook for about 3 seconds and dials NOTHING, so nobody is paged. This works with dry run on.')">
<button>Call the HT813 (no dialing)</button></form>
<p class=small>Uses the saved address and SIP port above. OK means the laptop reached the HT813 and it picked up
the line. Save any changed settings first.</p>
<h3>Try page digits</h3>
<form method=post action='/admin/phonetest' onsubmit="return confirm('Dial these digits through the HT813 and play the chosen sound? If the digits open a page zone, that zone hears it. This works with dry run on.')">
<label>Digits <input name=digits required maxlength=32 size=12 value='%s'></label>
<label>then play <select name=play><option value=bell>the bell (about 4 s)</option>
%s</select></label>
<button>Dial and play</button></form>
<h3>Last phone test</h3>
<p>What the line sent back to the HT813 during the last test (dial tone, beeps, error tones, silence):<br>
<audio controls preload=none src='/admin/last_phone_test.wav'></audio></p>
<h3>Last live page</h3>
<p>What the line sent back during the last real (not dry-run) page. Dial digits in use now:
<b>%s</b>.<br><audio controls preload=none src='/admin/last_page_line.wav'></audio></p>
<h3>Last failed page try</h3>
<p>What the line did on the last page try that failed (for example dial tone, busy or ringing after the
digits):<br><audio controls preload=none src='/admin/last_failed_page_line.wav'></audio></p>
<p class=small>If the bell pages but real calls don't: try "a full test page" here with the same digits.
It works: the digits a real call dials are different (see "Dial digits in use now"). It fails: the difference
is the audio; play "Last phone test" to hear what the line did. To find a page code without paging a message, use the bell. 0-9 * #, comma = half-second pause,
F = hook flash (Link). On a Norstar desk phone the floor page is PAGE, SPEAKER, 1; from the ATA2 line try
<b>F,*62</b>, then <b>F,*621</b>. When one works, put it in "Zone floor dial" above and Save.</p></div>
""" % ("<b>Saved settings in use.</b>" if tuned else "Now using the config file's values.", "\n".join(rows),
       e(cfg["zones"].get("floor", {}).get("dial") or "F,*60,2,1"),
       "".join("<option value='page%d'>a full test page: code %d %s%s, press 27</option>" % (
           c, c, e(cfg["codes"][c]["who"]), " (%s)" % e(cfg["codes"][c]["why"]) if cfg["codes"][c].get("why") else "")
           for c in range(2, 10)),
       e(" / ".join("%s: %s" % (z, v.get("dial") or "not set") for z, v in cfg["zones"].items())))
        admin_html = ("<div class=card style='border:2px solid var(--warn)'><b>This admin page is not "
                      "encrypted</b> (%s). Passwords typed here cross the network as plain text.</div>"
                      % e(app["https_problem"]) if app.get("https_problem") else "") + """
<div class=card><h2>Settings</h2>%s
<p class=small>Saved on the laptop and kept through restarts. The config file's "dry_run" is only the default.</p></div>
<div class=card><h2>Send a test call</h2>%s
<form method=post action='/admin/test' id=testform>
<label>From board <select name=board required>%s</select></label>
<label>Call <select name=code>%s</select></label>
<label>Press <input name=press type=number min=0 max=999 value=0 required style='width:6em'></label><br>
<button type=button onclick="p833Preview('test')">1. Preview (hear it, nothing is sent)</button>
<button>2. Send test call</button></form>
<div id=prev-test class=preview></div>
<p class=small>Preview shows the exact words and plays the exact audio this call will page, in this browser only.
Send goes through the same checks, repeat drop, log and pager as a real call from that board.
Press 0 tests the "press number unknown" message.</p></div>
<div class=card><h2>Audio</h2>%s</div>
<div class=card><h2>Add or rename a board</h2>
<form method=post action='/admin/board/add'>
<label>Board ID <input name=board required maxlength=32 pattern='[A-Za-z0-9_-]{1,32}' placeholder='FSP_LH' size=10></label>
<label>Cell name <input name=cell required maxlength=40 placeholder='FSP LH' size=12></label>
<button>Add / rename</button></form>
<p class=small>The board ID must match the ID set in that board's firmware. Letters, numbers, _ and - only.
Use an existing ID to rename its cell. Remove boards in the table below.</p></div>
<div class=card><h2>Send a custom page</h2>%s
<form method=post action='/admin/custom' id=customform>
<label>Zone <select name=zone>%s</select></label><br>
<label>English <input name=en required maxlength=200 size=60 placeholder='Test page. Maintenance requested to press 12.'></label><br>
<label>Spanish <input name=es maxlength=200 size=60 placeholder='Prueba. Se solicita mantenimiento en la prensa 12.'></label><br>
<button type=button onclick="p833Preview('custom')">1. Preview</button>
<button>2. Send custom page</button></form>
<div id=prev-custom class=preview></div>
<p class=small>Your own wording (leave Spanish empty for English only). Logged as code "custom".
Custom text has no recorded audio yet, so with dry run off a custom page fails until on-laptop speech is added.</p></div>
""" % (switch, live_warn, board_opts, code_opts, audio_html, live_warn, zone_opts) + phone_html + sms_html(app) \
            + voice_html(app) + lab_html(app) + PREVIEW_JS

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

    key = os.environ.pop("P833_BOARD_KEY", "").strip()
    if not key:
        LOG.error("P833_BOARD_KEY is not set: calls from boards are REFUSED. Run: sudo bash install/install.sh")
    admin_pw = os.environ.pop("P833_ADMIN_PASSWORD", "").strip()
    if not admin_pw:
        LOG.error("P833_ADMIN_PASSWORD is not set: the admin page is OFF. Run: sudo bash install/install.sh")
    elif len(admin_pw) < 12:
        LOG.warning("the admin password is short (%d characters); 12 or more is safer", len(admin_pw))

    state = State(cfg, args.state_dir, args.log_dir)
    if args.dry_run:
        state.dry_run = True   # for this run only; not saved
    dry_pager = DryRunPager(os.path.join(args.state_dir, "last_pages"), cfg)
    removed = dry_pager.prune(dry_pager.keep())
    if removed:
        LOG.info("removed %d saved dry-run audio file(s) (keeping %d)", removed, dry_pager.keep())
    ht813_pw = os.environ.pop("P833_HT813_PASSWORD", "").strip()   # only if the HT813 asks for one
    worker = PageWorker(cfg, state, make_live_pager(cfg, ht813_pw), dry_pager)
    worker.state_dir = args.state_dir
    if worker.audio.load_error:
        LOG.warning("AUDIO: %s", worker.audio.load_error)
    else:
        problems = worker.audio.check(cfg)
        LOG.info("audio: %d clips, voices %s%s", len(worker.audio.clips),
                 worker.audio.manifest.get("voices"),
                 "; %d problem(s), see the admin page" % len(problems) if problems else "")
    worker.start()
    voice = p833_voice.PageVoice(cfg, args.state_dir)
    voice.page_busy = lambda: worker.jobs.unfinished_tasks > 0
    voice.installed = lambda: tts_voices(cfg)
    worker.audio.voice = voice
    voice.warm_up()
    LOG.info("page voice: %s", p833_voice.describe(voice.settings()))
    sms = p833_sms.SmsSender(cfg, args.state_dir, args.log_dir)
    sms.start()
    Handler.app = {"cfg": cfg, "state": state, "worker": worker, "key": key,
                   "admin_password": admin_pw, "state_dir": args.state_dir, "sms": sms, "voice": voice}

    def watch():
        while True:
            time.sleep(15)
            state.check_silent()
    threading.Thread(target=watch, daemon=True).start()

    tls, why = tls_context(cfg)
    if tls:
        LOG.info("admin page: https://<this laptop>:%s/admin (boards keep plain HTTP)", cfg.get("port", 8833))
    else:
        LOG.warning("admin page is NOT encrypted: %s", why)
    Handler.app["https_problem"] = why
    srv = Server((cfg.get("listen", "0.0.0.0"), int(cfg.get("port", 8833))), Handler, tls=tls)
    LOG.info("P833 receiver %s listening on %s:%s (%s)", VERSION, cfg.get("listen", "0.0.0.0"),
             cfg.get("port", 8833), "DRY RUN" if state.dry_run else "LIVE")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
