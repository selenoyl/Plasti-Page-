# User accounts and sign-in sessions for the receiver's pages (the paging page and the admin page).
# By Ethan Brady & Ryan Raikes
#
# Accounts: a name, a role and a password. The password is kept only as a salted
# PBKDF2-SHA256 hash, in <state dir>/users.json (readable by this service only). Roles:
#   supervisor  pages people to the floor, and typed messages to the floor
#   office      the same, and typed messages to the office too
#   admin       the same as office, and the admin page; 30 s (not 5 minutes) between pages
#               of one department
# Sessions: signing in gives the browser a random token (a cookie). Only its SHA-256 is kept,
# in <state dir>/sessions.json, so that file can't be used to sign in. A session ends after
# SESSION_DAYS without use, on Sign out, or when its password is changed or its account removed.
# Standard library only (Python 3.6+).

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

ROLES = ("supervisor", "office", "admin")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'-]{0,31}$")
PW_MIN, PW_MAX = 8, 64
ITERATIONS = 200000           # PBKDF2-SHA256 rounds (about a tenth of a second per sign-in)
SESSION_DAYS = 14             # signed in on a device for this long after it was last used
MAX_SESSIONS = 500
MAX_PER_PERSON = 20           # sessions kept per account (or per address for the shared and built-in passwords)
RESERVED = ("admin",)         # the built-in admin: its password is P833_ADMIN_PASSWORD in secrets.env
_DUMMY = {"salt": "00" * 16, "iterations": ITERATIONS, "hash": "00" * 32}


def key_of(name):
    """An account's key: its name, case and extra spaces ignored ("Sam  Lee" = "sam lee")."""
    return " ".join(str(name or "").split()).lower()


def hash_password(pw, iterations=ITERATIONS):
    salt = os.urandom(16)
    return {"salt": salt.hex(), "iterations": iterations,
            "hash": hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations).hex()}


def check_password(rec, pw):
    try:
        got = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(rec["salt"]), int(rec["iterations"]))
        return hmac.compare_digest(got, bytes.fromhex(rec["hash"]))
    except (KeyError, ValueError, TypeError):
        return False


def name_problem(name):
    name = " ".join(str(name or "").split())
    if not NAME_RE.match(name):
        return "a name is 1 to 32 letters, numbers, spaces, . _ ' or -, starting with a letter or number"
    if key_of(name) in RESERVED:
        return "the name admin is kept for the built-in admin (its password is in secrets.env)"
    return ""


def password_problem(pw):
    if not PW_MIN <= len(pw) <= PW_MAX or not pw.isprintable() or pw != pw.strip():
        return "a password is %d to %d characters, with no space at the start or end" % (PW_MIN, PW_MAX)
    return ""


def _good_user(key, u):
    """An account record from users.json that can be used (a damaged one is left out)."""
    try:
        return (isinstance(u, dict) and u.get("role") in ROLES and isinstance(u.get("name"), str)
                and key_of(u["name"]) == key and isinstance(u.get("pw_set"), int)
                and isinstance(u.get("iterations"), int) and bool(bytes.fromhex(u["salt"]))
                and len(bytes.fromhex(u["hash"])) == 32)
    except (KeyError, TypeError, ValueError):
        return False


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, data):
    """Write a JSON file readable by this service only, all at once."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Users:
    """The accounts, in users.json: {key: {name, role, salt, iterations, hash, pw_set, created, last_login}}."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        raw = _read(path).get("users")
        raw = raw if isinstance(raw, dict) else {}
        self.users = {k: u for k, u in raw.items() if _good_user(k, u)}

    def _save(self):
        _write(self.path, {"users": self.users})

    def _save_or_undo(self, before):
        """Save; if the disk refuses (full?), put the accounts back as they were and say so."""
        try:
            self._save()
            return ""
        except OSError as e:
            self.users = before
            return "the accounts couldn't be saved (%s)" % e

    def _copy(self):
        return {k: dict(u) for k, u in self.users.items()}

    def get(self, key):
        u = self.users.get(key)
        return dict(u) if u else None

    def list(self):
        """Every account, without its password hash, sorted by name."""
        with self.lock:
            return [{k: v for k, v in dict(u, key=key).items() if k not in ("salt", "hash", "iterations")}
                    for key, u in sorted(self.users.items())]

    def add(self, name, pw, role):
        """Add an account. Returns a problem ("" when added)."""
        name = " ".join(str(name or "").split())
        problem = name_problem(name) or password_problem(pw) or ("" if role in ROLES else "pick a role")
        if problem:
            return problem
        with self.lock:
            if key_of(name) in self.users:
                return "there is already an account called %s" % self.users[key_of(name)]["name"]
            now, before = int(time.time()), self._copy()
            self.users[key_of(name)] = dict(hash_password(pw), name=name, role=role, pw_set=now, created=now,
                                            last_login=0)
            return self._save_or_undo(before)

    def set_password(self, key, pw):
        problem = password_problem(pw)
        if problem:
            return problem
        with self.lock:
            if key not in self.users:
                return "no such account"
            # a new password also ends that account's sessions (they remember pw_set)
            before = self._copy()
            self.users[key].update(hash_password(pw), pw_set=max(int(time.time()), self.users[key]["pw_set"] + 1))
            return self._save_or_undo(before)

    def set_role(self, key, role):
        if role not in ROLES:
            return "pick a role"
        with self.lock:
            if key not in self.users:
                return "no such account"
            before = self._copy()
            self.users[key]["role"] = role
            return self._save_or_undo(before)

    def remove(self, key):
        with self.lock:
            before = self._copy()
            if self.users.pop(key, None) is None:
                return "no such account"
            return self._save_or_undo(before)

    def check(self, name, pw):
        """The account if the name and password match, else None. Takes about as long either way,
        so the time doesn't tell whether a name exists."""
        u = self.users.get(key_of(name))
        ok = check_password(u if u else _DUMMY, pw)
        return dict(u, key=key_of(name)) if (u and ok) else None

    def admins(self):
        return [u["name"] for u in self.users.values() if u["role"] == "admin"]

    def note_login(self, key):
        """Note when someone last signed in (a full disk mustn't stop them signing in)."""
        with self.lock:
            if key in self.users:
                self.users[key]["last_login"] = int(time.time())
                try:
                    self._save()
                except OSError:
                    pass


class Sessions:
    """Signed-in browsers: {sha256(token): {kind, key, name, check, created, seen}}. kind is "user"
    (an account: check = its pw_set), "admin" (the built-in admin: check = a fingerprint of the
    admin password) or "shared" (the old shared paging password: check = when it was set)."""

    def __init__(self, path, days=SESSION_DAYS):
        self.path, self.max_age = path, days * 86400
        self.lock = threading.Lock()
        data = _read(path)
        try:
            self.secret = bytes.fromhex(data["secret"])
        except (KeyError, ValueError, TypeError):
            self.secret = os.urandom(16)
        raw = data.get("sessions")
        raw = raw if isinstance(raw, dict) else {}
        self.sessions = {k: v for k, v in raw.items() if isinstance(v, dict) and isinstance(v.get("kind"), str)
                         and isinstance(v.get("seen"), int) and isinstance(v.get("created"), int)}

    def _save(self):
        try:
            _write(self.path, {"secret": self.secret.hex(), "sessions": self.sessions})
        except OSError:
            pass                                   # still signed in until the next restart

    def fingerprint(self, text):
        """A short keyed fingerprint (to notice that a password changed, without keeping it)."""
        return hmac.new(self.secret, str(text).encode("utf-8"), "sha256").hexdigest()[:24]

    @staticmethod
    def _digest(token):
        return hashlib.sha256(str(token).encode("utf-8", "replace")).hexdigest()

    def create(self, kind, key, name, check, address=""):
        """A new session. Each person keeps at most MAX_PER_PERSON (an account; for the shared and
        built-in passwords, each address), so signing in over and over can't push others out."""
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        who = "user:" + key if kind == "user" else "%s@%s" % (kind, address)
        with self.lock:
            self.sessions[self._digest(token)] = {"kind": kind, "key": key, "name": name, "check": check,
                                                  "who": who, "created": now, "seen": now}
            mine = sorted((d for d, s in self.sessions.items() if s.get("who") == who),
                          key=lambda d: self.sessions[d]["seen"])
            for d in mine[:max(0, len(mine) - MAX_PER_PERSON)]:
                del self.sessions[d]
            if len(self.sessions) > MAX_SESSIONS:          # forget the least recently used
                for d in sorted(self.sessions, key=lambda d: self.sessions[d]["seen"])[:len(self.sessions) - MAX_SESSIONS]:
                    del self.sessions[d]
            self._save()
        return token

    def get(self, token):
        """The session for this token, or None (unknown or expired). Notes its use."""
        if not token or len(token) > 100:
            return None
        d, now = self._digest(token), int(time.time())
        with self.lock:
            s = self.sessions.get(d)
            if not s:
                return None
            if now - s["seen"] > self.max_age:
                del self.sessions[d]
                self._save()
                return None
            if now - s["seen"] > 3600:                    # (written down at most once an hour)
                s["seen"] = now
                self._save()
            return dict(s)

    def drop(self, token):
        with self.lock:
            if self.sessions.pop(self._digest(token), None) is not None:
                self._save()

    def drop_for(self, kind, key):
        """End every session of one account (removed)."""
        with self.lock:
            gone = [d for d, s in self.sessions.items() if s["kind"] == kind and s.get("key") == key]
            for d in gone:
                del self.sessions[d]
            if gone:
                self._save()
