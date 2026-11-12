# P833 Call Receiver - optional text messages through the voip.ms API.
# By Ethan Brady & Ryan Raikes
#
# One-way: when a cell calls, the same message is sent as a text, e.g.
#   "Chapar: Maintenance requested to press 27 for camera fault."
# Off until it is switched on (admin page, Text messages). Texts are sent by a
# background thread with its own queue, so a slow or failed text never delays
# or blocks a page.
#
# The voip.ms API password is entered on the admin page and stored in
# <state dir>/sms_secret.json, mode 0600, readable only by the service user
# (and root). It is never shown back, written to a log or put in a URL.
#
# Standard library only; Python 3.6+.

import collections
import csv
import json
import logging
import os
import queue
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

LOG = logging.getLogger("p833")

DEFAULTS = {
    "enabled": False,
    "api_url": "https://voip.ms/api/v1/rest.php",
    "api_username": "",          # the voip.ms account e-mail
    "did": "",                   # the SMS-enabled voip.ms number texts come from (10 digits)
    "to": [],                    # destination numbers (10 digits each)
    "prefix": "Chapar:",
    "template": "{prefix} {message}",
    "timeout_s": 15,
    "admin_tests": False,        # also text for test calls sent from the admin page
    "stagger_min_s": 1,          # random pause between texts to different numbers, from this...
    "stagger_max_s": 10,         # ...to this many seconds (no fixed rhythm, avoid spam flags)
    "max_per_hour": 60,          # cap on texts sent in any hour (cost and spam guard)
}
MAX_SMS = 160                    # voip.ms sendSMS limit (characters)
CSV_FIELDS = ["time", "to", "text", "result", "detail"]


def clean_number(s):
    """'(555) 123-4567' -> '5551234567'; '' if it isn't a 10- or 11-digit number."""
    d = re.sub(r"\D", "", str(s))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def settings_of(cfg):
    s = dict(DEFAULTS)
    s.update(cfg.get("sms", {}))
    return s


def compose(cfg, rec):
    """The text for one call record."""
    s = settings_of(cfg)
    fields = {"prefix": s["prefix"], "message": rec.get("message", ""), "cell": rec.get("cell", ""),
              "press": rec.get("press", ""), "who": rec.get("who", ""), "code": rec.get("code", "")}
    try:
        text = s["template"].format(**fields)
    except (KeyError, IndexError, ValueError):
        text = "%s %s" % (s["prefix"], fields["message"])
    text = " ".join(text.split())                 # one line, single spaces
    return text[:MAX_SMS]


class SmsSender(threading.Thread):
    """Sends texts one at a time from a queue; keeps a log of results."""

    def __init__(self, cfg, state_dir, log_dir, opener=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.secret_file = os.path.join(state_dir, "sms_secret.json")
        self.csv_file = os.path.join(log_dir, "texts.csv")
        self.jobs = queue.Queue()
        self.recent = []
        self.sent_times = collections.deque()      # when each text went out, for the hourly cap
        self.lock = threading.Lock()
        self.opener = opener or self._https

    # -- the API password (stored apart from the config, never shown)
    def password(self):
        try:
            with open(self.secret_file, encoding="utf-8") as f:
                return json.load(f).get("api_password", "")
        except (OSError, ValueError):
            return os.environ.get("P833_VOIPMS_API_PASSWORD", "")

    def has_password(self):
        return bool(self.password())

    def set_password(self, pw):
        """Store (or with '' remove) the API password, readable only by this service."""
        if not pw:
            try:
                os.remove(self.secret_file)
            except OSError:
                pass
            return
        tmp = self.secret_file + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"api_password": pw}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.secret_file)

    # -- queueing
    def text_for_call(self, rec, test_call=False):
        s = settings_of(self.cfg)
        if not s["enabled"] or (test_call and not s["admin_tests"]):
            return
        self.jobs.put((compose(self.cfg, rec), None))

    def test_text(self):
        """Queue a test text to every number (even when texts are switched off).
        Returns (ok, detail); the results show in the recent texts."""
        s = settings_of(self.cfg)
        n = len([x for x in s["to"] if clean_number(x)])
        if not n:
            return False, "no numbers to text"
        text = " ".join(("%s Test text from the paging system." % s["prefix"]).split())[:MAX_SMS]
        self.jobs.put((text, None))
        return True, ("queued for %d number(s), %s to %s s apart; refresh in a moment and see Recent texts"
                      % (n, s["stagger_min_s"], s["stagger_max_s"]))

    def manual_text(self, number, message, add_prefix=True):
        """Queue one text to one number, typed on the admin page (works with texts off).
        Returns (ok, detail)."""
        dst = clean_number(number)
        if not dst:
            return False, "the number must be 10 digits"
        message = " ".join(str(message).split())
        if not message or not message.isprintable():
            return False, "type a message"
        s = settings_of(self.cfg)
        text = ("%s %s" % (s["prefix"], message) if add_prefix and s["prefix"] else message)[:MAX_SMS]
        self.jobs.put((text, [dst]))
        return True, "queued to %s: %s" % (dst, text)

    def run(self):
        while True:
            text, dsts = self.jobs.get()
            try:
                self.send(text, dsts)
            except Exception as e:      # a bug here must never stop the thread
                LOG.exception("text: unexpected error: %s", e)

    # -- sending
    def send(self, text, dsts=None):
        """Send to the given numbers, or to the configured list when dsts is None."""
        s = settings_of(self.cfg)
        user, pw, did = s["api_username"].strip(), self.password(), clean_number(s["did"])
        dsts = [clean_number(n) for n in (s["to"] if dsts is None else dsts)]
        dsts = [n for n in dsts if n]
        problem = ("no voip.ms account e-mail" if not user else "no voip.ms API password" if not pw
                   else "no sending number (DID)" if not did else "no numbers to text" if not dsts else "")
        if problem:
            self._record("-", text, "FAILED", problem)
            return False, problem
        results = []
        for i, dst in enumerate(dsts):
            if i:
                time.sleep(self.pause_s(s))                     # spread texts out, at random
            if not self._under_cap(int(s["max_per_hour"])):
                detail = "skipped: hourly limit of %s texts reached" % s["max_per_hour"]
                self._record(dst, text, "SKIPPED", detail)
                results.append((dst, False, detail))
                continue
            ok, detail = self._send_one(s, user, pw, did, dst, text)
            if ok:
                with self.lock:
                    self.sent_times.append(time.monotonic())
            self._record(dst, text, "sent" if ok else "FAILED", detail)
            results.append((dst, ok, detail))
        n_ok = sum(1 for _, ok, _ in results if ok)
        summary = "; ".join("%s: %s" % (d, "sent" if ok else det) for d, ok, det in results)
        return n_ok == len(results), "%d of %d sent (%s)" % (n_ok, len(results), summary)

    @staticmethod
    def pause_s(s):
        """A random pause between two texts, between the min and max settings."""
        lo = max(0.0, float(s["stagger_min_s"]))
        hi = max(lo, float(s["stagger_max_s"]))
        return random.uniform(lo, hi)

    def _under_cap(self, per_hour):
        now = time.monotonic()
        with self.lock:
            while self.sent_times and now - self.sent_times[0] > 3600:
                self.sent_times.popleft()
            return len(self.sent_times) < per_hour

    def _send_one(self, s, user, pw, did, dst, text):
        params = {"api_username": user, "api_password": pw, "method": "sendSMS",
                  "did": did, "dst": dst, "message": text}
        for attempt in (1, 2):
            try:
                body = self.opener(s["api_url"], params, float(s["timeout_s"]))
            except (urllib.error.URLError, OSError, ValueError) as e:
                detail = "could not reach voip.ms (%s)" % getattr(e, "reason", e)
                if attempt == 1:
                    time.sleep(3)
                    continue
                return False, detail
            try:
                status = json.loads(body.decode("utf-8", "replace")).get("status", "")
            except ValueError:
                return False, "voip.ms sent an answer that isn't JSON"
            if status == "success":
                return True, "sent"
            return False, "voip.ms said %s" % (status or "nothing")
        return False, "failed"

    @staticmethod
    def _https(url, params, timeout):
        """POST the request over HTTPS with certificate checking. The password goes in
        the request body, never in the URL."""
        if not url.startswith("https://"):
            raise ValueError("the voip.ms API address must start with https://")
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "P833-Receiver"})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read(65536)

    # -- log of texts (no passwords in it)
    def _record(self, dst, text, result, detail):
        row = {"time": datetime.now().isoformat(sep=" ", timespec="seconds"), "to": dst, "text": text,
               "result": result, "detail": detail}
        (LOG.info if result == "sent" else LOG.warning)("text to %s: %s (%s)", dst, result, detail)
        with self.lock:
            self.recent.append(row)
            self.recent = self.recent[-30:]
            new = not os.path.exists(self.csv_file)
            try:
                with open(self.csv_file, "a", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow(row)
            except OSError as e:
                LOG.warning("could not write the text log: %s", e)
