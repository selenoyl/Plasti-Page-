# P833 Call Receiver - optional text messages through the voip.ms API.
# By Ethan Brady & Ryan Raikes
#
# One-way: when a cell calls, the same message is sent as a text, e.g.
#   "Chapar: Maintenance requested to press 27 for camera fault."
# Off until it is switched on (admin page, Text messages). Texts are sent by a
# background thread with its own queue, so a slow or failed text never delays
# or blocks a page.
#
# voip.ms facts this follows (from its API docs and working clients; see AAR D57):
#   - one HTTPS GET per text to https://voip.ms/api/v1/rest.php, method=sendSMS,
#     did (from), dst (ONE number: no group texts), message (160 characters max)
#   - errors come back as HTTP 200 with {"status": "<code>"}; only "success" is OK
#   - the API must be enabled and this site's public IP allowed at voip.ms
#
# The voip.ms API password is entered on the admin page and stored in
# <state dir>/sms_secret.json, mode 0600, readable only by the service user
# (and root). It is never shown back or written to a log. voip.ms's API needs it
# in the HTTPS query string, so it is sent only to https://voip.ms, with the
# certificate checked and redirects refused.
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
import unicodedata
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
MAX_QUEUED = 30                  # texts waiting to go out; more are dropped (and logged)
CSV_FIELDS = ["time", "to", "text", "result", "detail"]
API_HOSTS = ("voip.ms",)         # the password is only ever sent here

# voip.ms answers that mean every other text would fail the same way: stop the batch.
STOP_ALL = {"invalid_credentials", "missing_credentials", "api_not_enabled", "ip_not_enabled",
            "missing_method", "invalid_method", "missing_did", "invalid_did", "non_sufficient_funds",
            "limit_reached"}
# Answers worth one more try after a pause.
TRANSIENT = {"api_limit_exceeded", "method_maintenance", "provider_outofservice", "tls_error",
             "unavailable_info"}
HINTS = {
    "invalid_credentials": "check the account e-mail and the API password",
    "missing_credentials": "check the account e-mail and the API password",
    "api_not_enabled": "turn the API on at voip.ms: Main Menu, SOAP and REST/JSON API",
    "ip_not_enabled": "add this site's public IP to the allowed list at voip.ms",
    "invalid_did": "check the sending number, and that SMS is turned on for it at voip.ms",
    "non_sufficient_funds": "add funds to the voip.ms account",
    "limit_reached": "voip.ms daily text limit reached (100 a day unless voip.ms raises it)",
    "invalid_dst": "voip.ms won't text that number",
    "sms_toolong": "over 160 characters",
    "sms_failed": "voip.ms could not send it",
    "api_limit_exceeded": "too many requests a minute",
}
_ASCII = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
          "\u2026": "...", "\u00a0": " "}


def clean_number(s):
    """'(555) 123-4567' -> '5551234567'; '' if it isn't a 10- or 11-digit number."""
    d = re.sub(r"\D", "", str(s))
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def finish(text):
    """One line of plain ASCII (accents dropped: n for \u00f1), cut to 160 characters."""
    text = "".join(_ASCII.get(c, c) for c in str(text))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = " ".join(text.split())
    return text if len(text) <= MAX_SMS else text[:MAX_SMS - 3].rstrip() + "..."


def csv_safe(v):
    """A value as it goes in a CSV log: text that a spreadsheet would run as a formula
    (=, +, -, @ at the start) gets a leading apostrophe."""
    if isinstance(v, str) and len(v) > 1 and v[0] in "=+-@\t\r":
        return "'" + v
    return v


def mask(n):
    """A number as it goes in the system log: only the last 4 digits."""
    return "..." + n[-4:] if len(n) > 4 else n


def settings_of(cfg):
    s = dict(DEFAULTS)
    s.update(cfg.get("sms", {}))
    return s


def compose(cfg, rec):
    """The text for one call record."""
    s = settings_of(cfg)
    fields = {"prefix": s["prefix"], "message": rec.get("message", ""), "cell": rec.get("cell", ""),
              "press": rec.get("press", ""), "who": rec.get("who", ""), "code": rec.get("code", "")}
    # Plain {name} swaps only: str.format would also obey format codes like {message:>999999999}.
    template = str(s["template"])[:200]
    if "{message}" not in template or re.search(r"\{(?!(%s)\})" % "|".join(fields), template):
        template = "{prefix} {message}"
    text = re.sub(r"\{(%s)\}" % "|".join(fields), lambda m: str(fields[m.group(1)]), template)
    return finish(text)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: the request URL holds the API password."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url.split("?")[0], code, "redirect refused", headers, fp)


class SmsSender(threading.Thread):
    """Sends texts one at a time from a queue; keeps a log of results."""
    network_retry_s = 3              # pause before the one retry when voip.ms couldn't be reached
    busy_retry_s = 5                 # ...or when it said it was busy (api_limit_exceeded and the like)

    def __init__(self, cfg, state_dir, log_dir, opener=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.secret_file = os.path.join(state_dir, "sms_secret.json")
        self.csv_file = os.path.join(log_dir, "texts.csv")
        self.jobs = queue.Queue()
        self.recent = []
        self.sent_times = collections.deque(self._sent_last_hour())   # for the hourly cap (wall clock)
        self.lock = threading.Lock()
        self.opener = opener or self._https

    def _sent_last_hour(self):
        """Times of texts sent in the last hour, from the text log, so a restart doesn't reset the cap."""
        out, now = [], time.time()
        try:
            with open(self.csv_file, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("result") != "sent":
                        continue
                    try:
                        t = time.mktime(datetime.strptime(row.get("time", ""), "%Y-%m-%d %H:%M:%S").timetuple())
                    except ValueError:
                        continue
                    if 0 <= now - t < 3600:
                        out.append(t)
        except OSError:
            pass
        return sorted(out)

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
    def _queue(self, text, dsts):
        if self.jobs.qsize() >= MAX_QUEUED:
            self._record("-", text, "SKIPPED", "skipped: %d texts already waiting" % MAX_QUEUED)
            return False
        self.jobs.put((text, dsts))
        return True

    def text_for_call(self, rec, test_call=False):
        s = settings_of(self.cfg)
        if not s["enabled"] or (test_call and not s["admin_tests"]):
            return
        self._queue(compose(self.cfg, rec), None)

    def test_text(self):
        """Queue a test text to every number (even when texts are switched off).
        Returns (ok, detail); the results show in the recent texts."""
        s = settings_of(self.cfg)
        n = len([x for x in s["to"] if clean_number(x)])
        if not n:
            return False, "no numbers to text"
        text = finish("%s Test text from the paging system." % s["prefix"])
        if not self._queue(text, None):
            return False, "too many texts already waiting"
        return True, ("queued for %d number(s), %s to %s s apart; refresh in a moment and see Recent texts"
                      % (n, s["stagger_min_s"], s["stagger_max_s"]))

    def manual_text(self, number, message, add_prefix=True):
        """Queue one text to one number, typed on the admin page (works with texts off).
        Returns (ok, detail)."""
        dst = clean_number(number)
        if not dst:
            return False, "the number must be 10 digits"
        message = " ".join(str(message).split())
        message = finish(message) if message.isprintable() else ""
        if not message:
            return False, "type a message"
        s = settings_of(self.cfg)
        text = finish("%s %s" % (s["prefix"], message) if add_prefix and s["prefix"] else message)
        if not self._queue(text, [dst]):
            return False, "too many texts already waiting"
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
        results, stop, tried = [], "", False
        for dst in dsts:
            if stop:                                            # the account can't send: don't keep trying
                self._record(dst, text, "SKIPPED", "skipped: " + stop)
                results.append((dst, False, "skipped"))
                continue
            if not self._under_cap(int(s["max_per_hour"])):         # checked first: no pause for a skip
                detail = "skipped: hourly limit of %s texts reached" % s["max_per_hour"]
                self._record(dst, text, "SKIPPED", detail)
                results.append((dst, False, detail))
                continue
            if tried:
                time.sleep(self.pause_s(s))                     # spread texts out, at random
            tried = True
            ok, detail, stop_all = self._send_one(s, user, pw, did, dst, text)
            if ok:
                with self.lock:
                    self.sent_times.append(time.time())
            elif stop_all:
                stop = detail
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
        now = time.time()
        with self.lock:
            while self.sent_times and now - self.sent_times[0] > 3600:
                self.sent_times.popleft()
            return len(self.sent_times) < per_hour

    def _call(self, s, params):
        """One API request. Returns (status, answer dict), or raises:
        urllib.error.URLError when the request never reached voip.ms (safe to retry),
        OSError when it may have got there but no answer came (not safe: could double-send),
        ValueError for an unusable answer or address."""
        body = self.opener(s["api_url"], params, float(s["timeout_s"]))
        try:
            answer = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise ValueError("voip.ms sent an answer that isn't JSON")
        if not isinstance(answer, dict) or not answer.get("status"):
            raise ValueError("voip.ms sent an answer with no status")
        return str(answer["status"]), answer

    def _send_one(self, s, user, pw, did, dst, text):
        """Returns (ok, detail, stop_all). stop_all: the rest of the batch would fail too."""
        params = {"api_username": user, "api_password": pw, "method": "sendSMS",
                  "did": did, "dst": dst, "message": text}
        for attempt in (1, 2):
            try:
                status, answer = self._call(s, params)
            except urllib.error.HTTPError as e:
                return False, "voip.ms answered HTTP %s" % e.code, False
            except urllib.error.URLError as e:
                if attempt == 1:                            # never got there: one more try
                    time.sleep(self.network_retry_s)
                    continue
                return False, self._scrub("could not reach voip.ms (%s)" % (e.reason,), pw), False
            except ValueError as e:
                return False, self._scrub(str(e), pw), False
            except OSError as e:
                # Sent, but no answer in time. voip.ms may have taken it, so a retry could
                # send it twice; don't.
                return False, self._scrub("no answer from voip.ms (%s); the text may or may not "
                                          "have gone out" % (e,), pw), False
            if status == "success":
                sms_id = answer.get("sms")
                return True, "sent (voip.ms id %s)" % sms_id if sms_id not in (None, "", 0, "0") \
                    else "sent (voip.ms gave no message id)", False
            if status in TRANSIENT and attempt == 1:
                time.sleep(self.busy_retry_s)
                continue
            detail = "voip.ms said %s" % status
            if status in HINTS:
                detail += ": " + HINTS[status]
            if status == "ip_not_enabled":
                ip = self.public_ip(s, user, pw)
                if ip:
                    detail += " (voip.ms sees this site as %s)" % ip
            return False, detail, status in STOP_ALL
        return False, "failed", False

    def public_ip(self, s, user, pw):
        """The internet address voip.ms sees this laptop coming from ('' if unknown).
        getIP is the one API method that works before the address is allowed."""
        try:
            status, answer = self._call(s, {"api_username": user, "api_password": pw, "method": "getIP"})
        except (OSError, ValueError):
            return ""
        ip = str(answer.get("ip", ""))
        return ip if status == "success" and re.match(r"^[0-9a-fA-F.:]{3,45}$", ip) else ""

    @staticmethod
    def _scrub(detail, pw):
        """Make sure no error text carries the password."""
        for v in {pw, urllib.parse.quote_plus(pw), urllib.parse.quote(pw)}:
            if v:
                detail = detail.replace(v, "***")
        return detail

    @staticmethod
    def _https(url, params, timeout):
        """GET the API over HTTPS, as voip.ms documents it (a form POST can land on its SOAP
        handler). Certificates are checked, only voip.ms is ever contacted, and redirects
        are refused so the request can't be bounced elsewhere or onto plain HTTP. The URL
        carries the password, so it is never logged."""
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in API_HOSTS or parts.query:
            raise ValueError("the voip.ms API address must be https://voip.ms/...")
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "P833-Receiver", "Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()),
                                             _NoRedirect)
        with opener.open(req, timeout=timeout) as r:
            return r.read(65536)

    # -- log of texts (no passwords in it)
    def _record(self, dst, text, result, detail):
        row = {"time": datetime.now().isoformat(sep=" ", timespec="seconds"), "to": dst, "text": text,
               "result": result, "detail": detail}
        (LOG.info if result == "sent" else LOG.warning)("text to %s: %s (%s)", mask(dst), result, detail)
        with self.lock:
            self.recent.append(row)
            self.recent = self.recent[-30:]
            new = not os.path.exists(self.csv_file)
            try:
                with open(self.csv_file, "a", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                    if new:
                        w.writeheader()
                    w.writerow({k: csv_safe(v) for k, v in row.items()})
            except OSError as e:
                LOG.warning("could not write the text log: %s", e)
