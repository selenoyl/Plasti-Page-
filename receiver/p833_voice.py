# P833 Call Receiver - the page voice: speaks pages live with Piper on the laptop,
# with the voice settings saved on the admin page, and keeps every sentence it
# speaks, so the same message with the same settings plays from disk next time.
# By Ethan Brady & Ryan Raikes
#
#   source "recorded": pages use the clips made by tools/make_audio.py (as shipped).
#   source "piper":    each sentence is spoken by Piper with the saved settings,
#                      kept in <state dir>/voice_cache, and reused after that.
#
# A page never fails because of Piper: a page spends at most PAGE_BUDGET_S on it in
# all; if Piper isn't installed, stops, or is too slow, that sentence comes from the
# recorded clips instead (and the log says so). After Piper hangs or crashes on a page,
# pages skip it for COOL_OFF_S while it is restarted in the background.
#
# Piper runs as one helper process (receiver/p833_tts.py serve) in its own Python
# environment, with the voices it uses kept loaded (loading one takes a second or
# two; a sentence then takes a fraction of a second). It runs at a lower CPU
# priority than the receiver, with none of the receiver's secrets in its environment
# (it runs as the same user, so Piper is pinned and its voices checksummed by
# install_tts.sh). With the Piper voice in use it stays running; otherwise it stops
# after 10 idle minutes. Pages go ahead of previews, the voice lab and Make all.
#
# Standard library only; Python 3.6+.

import hashlib
import io
import json
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
import wave

from p833_audio import DIGIT_WORDS, RATE, AudioError, _ulaw_decode, read_wav

LOG = logging.getLogger("p833")

LANGS = ("en", "es")
VOICE_DEFAULTS = {
    "source": "recorded",
    # The defaults match the recorded clips, so switching to Piper sounds the same.
    "en": {"voice": "en_US-hfc_female-medium", "pace": 1.25, "digits": True, "digit_pause_ms": 0},
    "es": {"voice": "es_MX-claude-high", "pace": 1.25, "digits": False, "digit_pause_ms": 0},
}
# How a voice should SAY a word it would otherwise mispronounce (the page text
# stays as written). tools/make_audio.py uses the same list for the recordings.
PRONOUNCE = {
    "es": {"Red Rabbit": "Red Rábit"},
}
CACHE_FORMAT = 1          # change when the way sentences are made changes: old kept audio is then unused
PAGE_BUDGET_S = 10        # a live page spends at most this long on Piper in all, then uses the recorded clips
SLOW_TIMEOUT_S = 60       # previews, the voice lab and "make all messages"
COOL_OFF_S = 60           # after Piper hangs or crashes on a page, pages skip it this long while it recovers
MAX_DIGIT_PAUSE_MS = 800  # longer silences can make the paging feedback eliminator end the page
MAX_CACHE_MB = 300        # kept audio beyond this: the least recently used is removed
MIN_ANSWER_S = 3          # a page asks Piper only with at least this long left (else: the recorded clip)
PAGE_LOCK_WAIT_S = 3      # ...and waits at most this long for Piper to be free (a preview or the voice lab)
IDLE_EXIT_S = 600         # the helper stops after this long unused (unless the Piper voice is in use)
MAX_PREVIEW_DIRS = 8      # kept-audio folders for settings only previewed, not saved


# ------------------------------------------------------------- settings ---

def voice_settings(cfg, recorded=None):
    """The page voice settings: defaults (how the recordings were made, when known), with the
    saved ones on top. Every value is checked: a bad one (a hand-edited settings.json) is
    replaced by the default, never passed on."""
    a = cfg.get("audio", {})
    saved = a.get("voice", {})
    saved = saved if isinstance(saved, dict) else {}
    recorded = recorded or {}
    out = {"source": saved.get("source") if saved.get("source") in ("recorded", "piper") else "recorded"}
    for lang in LANGS:
        d = dict(VOICE_DEFAULTS[lang])
        if isinstance(recorded.get("voices"), dict) and isinstance(recorded["voices"].get(lang), str):
            d["voice"] = recorded["voices"][lang]
        if isinstance(recorded.get("length_scale"), (int, float)):
            d["pace"] = float(recorded["length_scale"])
        if isinstance(a.get("speak_digits"), list):
            d["digits"] = lang in a["speak_digits"]
        got = saved.get(lang) if isinstance(saved.get(lang), dict) else {}
        lv = dict(d)
        if isinstance(got.get("voice"), str) and re.match(r"^[A-Za-z0-9_.-]{1,80}$", got["voice"]):
            lv["voice"] = got["voice"]
        try:
            if 0.5 <= float(got.get("pace", d["pace"])) <= 2.5:
                lv["pace"] = round(float(got.get("pace", d["pace"])), 2)
        except (TypeError, ValueError):
            pass
        if isinstance(got.get("digits"), bool):
            lv["digits"] = got["digits"]
        try:
            if 0 <= int(got.get("digit_pause_ms", 0)) <= MAX_DIGIT_PAUSE_MS:
                lv["digit_pause_ms"] = int(got.get("digit_pause_ms", 0))
        except (TypeError, ValueError):
            pass
        out[lang] = lv
    return out


def parse_voice_form(form, voices, langs=LANGS, defaults=None):
    """The page voice settings from the admin form. Returns (settings, errors). Only the
    languages pages use (audio.languages) need a voice."""
    errs, out = [], {"source": "piper"}
    for lang, name in (("en", "English"), ("es", "Spanish")):
        v = form.get(lang + "_voice", "")
        if lang not in langs and v not in voices:
            out[lang] = dict((defaults or VOICE_DEFAULTS)[lang])     # not used: keep the default
            continue
        if v not in voices:
            errs.append("pick an installed %s voice" % name)
        try:
            pace = round(float(form.get(lang + "_pace", "1.25")), 2)
            if not 0.5 <= pace <= 2.5:
                raise ValueError
        except ValueError:
            errs.append("%s speed must be 0.5 to 2.5" % name)
            pace = 1.25
        try:
            pause = int(form.get(lang + "_digit_pause_ms", "0") or 0)
            if not 0 <= pause <= MAX_DIGIT_PAUSE_MS:
                raise ValueError
        except ValueError:
            errs.append("%s pause between digits must be 0 to %d ms (a longer silence can end the page)"
                        % (name, MAX_DIGIT_PAUSE_MS))
            pause = 0
        out[lang] = {"voice": v, "pace": pace, "digits": form.get(lang + "_digits") == "on",
                     "digit_pause_ms": pause}
    return out, errs


def describe(vs):
    """One line about a voice setting, for the admin page and the log."""
    if vs["source"] != "piper":
        return "the recorded clips (as shipped)"
    parts = []
    for lang, name in (("en", "English"), ("es", "Spanish")):
        lv = vs[lang]
        parts.append("%s %s at %.2f, press number %s" % (
            name, lv["voice"], lv["pace"],
            ("digit by digit" + (" with %d ms between" % lv["digit_pause_ms"] if lv["digit_pause_ms"] else ""))
            if lv["digits"] else "as a number"))
    return "Piper: " + "; ".join(parts)


# ------------------------------------------------------------ sentences ---

def say(lang, text):
    for word, spoken in PRONOUNCE.get(lang, {}).items():
        text = text.replace(word, spoken)
    return " ".join(text.split())


def sentence_segments(cfg, lv, lang, code, press):
    """What Piper says for this code, press and language: a list of segments
    ({"text": ...} and {"pause_ms": ...}) as p833_tts.py takes them."""
    c = cfg["codes"][code]
    if press == 0:
        return [{"text": say(lang, c.get(lang + "_no_press", c[lang]).replace("{press}", "0"))}]
    template = c[lang]
    if "{press}" not in template:
        return [{"text": say(lang, template)}]
    pre, post = template.split("{press}", 1)
    if not (lv.get("digits") and lang in DIGIT_WORDS):
        return [{"text": say(lang, pre + str(press) + post)}]
    words = [DIGIT_WORDS[lang][int(d)] for d in str(press)]
    pause = max(0, min(MAX_DIGIT_PAUSE_MS, int(lv.get("digit_pause_ms", 0))))
    if not pause:
        return [{"text": say(lang, pre + ", ".join(words) + post)}]     # like the recordings
    pieces = [pre + words[0]] + words[1:]
    pieces[-1] += post
    segs = []
    for i, piece in enumerate(pieces):
        if i:
            segs.append({"pause_ms": pause})
        segs.append({"text": say(lang, piece)})
    return segs


def text_segments(lang, text):
    """Free text (a custom page), said as written: "[2]" is text here, not a pause."""
    text = say(lang, str(text))
    return [{"text": text}] if text else []


# ---------------------------------------------------------------- Piper ---

def tts_env():
    """A plain environment for Piper: none of the service's secrets."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": os.environ.get("STATE_DIRECTORY", "/tmp")}


def tts_ready(cfg):
    a = cfg["audio"]
    return os.path.exists(a["tts_python"]) and os.path.exists(a["tts_script"]) and os.path.isdir(a["tts_voices"])


def _stuck(what):
    """An AudioError meaning the helper itself hung or died (not a bad request)."""
    e = AudioError("Piper %s" % what)
    e.stuck = True
    return e


class PiperEngine:
    """One Piper helper process, used by one request at a time. A page asks with
    urgent=True: background work ("make all messages") waits while a page wants it."""

    def __init__(self, cfg, keep_running=lambda: False):
        self.cfg = cfg
        self.keep_running = keep_running          # True while pages use the Piper voice
        self.lock = threading.Lock()              # one request at a time
        self.turn = threading.Condition()
        self.urgent = 0
        self.proc = None
        self.buf = b""
        self.last_used = 0.0
        self.started = 0
        self.cool_until = 0.0                     # pages skip Piper until then (it hung or crashed)
        self.on_trouble = lambda: None            # set by PageVoice: reload the voices in the background
        threading.Thread(target=self._idle_watch, daemon=True).start()

    def ready(self):
        return tts_ready(self.cfg)

    def wait_turn(self, timeout=30):
        """Background work calls this first: it waits while a page needs Piper."""
        end = time.monotonic() + timeout
        with self.turn:
            while self.urgent and time.monotonic() < end:
                self.turn.wait(0.5)

    def cooling(self):
        return time.monotonic() < self.cool_until

    def speak(self, voice, pace, segments, timeout, urgent=False):
        """WAV bytes (8 kHz 16-bit mono), or AudioError. timeout covers waiting for the helper
        to be free AND its answer; only a slow answer counts as the helper being stuck.
        urgent (a page): skipped while Piper is recovering; other requests wait for pages."""
        if not self.ready():
            raise AudioError("Piper isn't installed on this laptop (sudo bash install/install_tts.sh)")
        end = time.monotonic() + timeout
        if urgent:
            if self.cooling():
                raise AudioError("Piper is recovering from a problem; recorded clips are used meanwhile")
            with self.turn:
                self.urgent += 1
        else:
            self.wait_turn(timeout)               # a page waiting for Piper goes first
        try:
            wait = end - time.monotonic()
            if urgent:
                wait = min(wait, PAGE_LOCK_WAIT_S)
            if not self.lock.acquire(timeout=max(0.05, wait)):
                raise AudioError("Piper was busy (another request)")
            try:
                if urgent and end - time.monotonic() < MIN_ANSWER_S:
                    raise AudioError("not enough time left for Piper on this page")
                wav = self._ask({"voice": voice, "length_scale": pace, "segments": segments}, end)
                self.cool_until = 0.0
                return wav
            except AudioError as e:
                if getattr(e, "stuck", False) and urgent:
                    self.cool_until = time.monotonic() + COOL_OFF_S
                    LOG.warning("Piper %s during a page; pages use the recorded clips for %d s", e, COOL_OFF_S)
                    threading.Thread(target=self.on_trouble, daemon=True).start()
                raise
            finally:
                self.last_used = time.monotonic()
                self.lock.release()
        finally:
            if urgent:
                with self.turn:
                    self.urgent -= 1
                    self.turn.notify_all()

    # -- the helper process (only touched with self.lock held)
    def _start(self):
        a = self.cfg["audio"]
        cmd = [a["tts_python"], a["tts_script"], "serve", a["tts_voices"]]
        nice = shutil.which("nice", path="/usr/bin:/bin")
        if nice:
            cmd = [nice, "-n", "10"] + cmd            # pages and the phone line come first
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=tts_env(), cwd="/")
        self.buf = b""
        self.started += 1
        LOG.info("Piper helper started (pid %d)", self.proc.pid)

    def _stop(self, kill=False):
        """Stop the helper: politely (close its input), or at once when it's stuck."""
        p, self.proc, self.buf = self.proc, None, b""
        if not p:
            return
        if kill:
            p.kill()
        try:
            p.stdin.close()
        except OSError:
            pass
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        try:
            p.stdout.close()
        except OSError:
            pass

    def _read(self, n, end):
        """Exactly n bytes, or up to a newline when n is None, from the helper by `end`."""
        fd = self.proc.stdout.fileno()
        while True:
            if n is None:
                i = self.buf.find(b"\n")
                if i >= 0:
                    out, self.buf = self.buf[:i], self.buf[i + 1:]
                    return out
            elif len(self.buf) >= n:
                out, self.buf = self.buf[:n], self.buf[n:]
                return out
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                raise TimeoutError
            chunk = os.read(fd, 65536)
            if not chunk:
                raise EOFError
            self.buf += chunk

    def _ask(self, req, end):
        for attempt in (1, 2):                    # once more if the helper had stopped
            if self.proc is None or self.proc.poll() is not None:
                self._stop()
                try:
                    self._start()
                except OSError as e:
                    raise AudioError("could not start Piper: %s" % e)
            try:
                self.proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
                head = json.loads(self._read(None, end).decode("utf-8"))
                if not head.get("ok"):
                    raise AudioError("Piper: %s" % str(head.get("error", "failed"))[:200])
                return self._read(int(head["bytes"]), end)
            except TimeoutError:
                self._stop(kill=True)
                raise _stuck("took too long")
            except (EOFError, BrokenPipeError, OSError, ValueError, KeyError) as e:
                self._stop(kill=True)
                if attempt == 2 or time.monotonic() >= end:
                    raise _stuck("stopped (%s)" % e.__class__.__name__)
        raise _stuck("failed")

    def _idle_watch(self):
        while True:
            time.sleep(30)
            self._idle_check()

    def _idle_check(self):
        """Stop the helper if it has been unused a while, unless pages use the Piper voice."""
        if self.proc is None or self.keep_running():
            return
        if time.monotonic() - self.last_used >= IDLE_EXIT_S and self.lock.acquire(blocking=False):
            try:
                if time.monotonic() - self.last_used >= IDLE_EXIT_S:
                    self._stop()
                    LOG.info("Piper helper stopped (idle)")
            finally:
                self.lock.release()


# ---------------------------------------------------------- kept audio ---

def _ulaw_wav(pcm):
    """16-bit PCM -> (an 8-bit mu-law WAV, the 16-bit audio it plays back as). Mu-law is like
    the recorded clips: half the size, and exactly what the phone line carries."""
    from p833_sip import pcm16_to_ulaw
    data = pcm16_to_ulaw(pcm)
    fmt = (7).to_bytes(2, "little") + (1).to_bytes(2, "little") + RATE.to_bytes(4, "little") + \
        RATE.to_bytes(4, "little") + (1).to_bytes(2, "little") + (8).to_bytes(2, "little")
    body = b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt + \
        b"data" + len(data).to_bytes(4, "little") + data + (b"\x00" if len(data) % 2 else b"")
    return b"RIFF" + len(body).to_bytes(4, "little") + body, _ulaw_decode(data)


def _whole_file(path):
    """The file's bytes if it is a complete WAV (its size matches its header), else None."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 44 or data[:4] != b"RIFF" or len(data) < int.from_bytes(data[4:8], "little") + 8:
        return None
    return data


def _wav_pcm(wav):
    with wave.open(io.BytesIO(wav)) as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, 2, 1):
            raise AudioError("Piper's audio isn't 8 kHz 16-bit mono")
        return w.readframes(w.getnframes())


class PageVoice:
    """Speaks page sentences with the page voice and keeps them on disk."""

    def __init__(self, cfg, state_dir, engine=None):
        self.cfg = cfg
        self.cache_dir = os.path.join(state_dir, "voice_cache")
        self.engine = engine or PiperEngine(cfg, keep_running=self.active)
        self.page_busy = lambda: False            # set by the receiver: True while a page is going out
        self.installed = lambda: []               # set by the receiver: the installed voices
        self.recorded = {}                        # set by the receiver: how the recordings were made
        self.lock = threading.Lock()
        self.job = {"running": False}             # "make all messages" progress
        self.job_thread = None
        self.writes = 0
        self.page_budget = PAGE_BUDGET_S
        self.engine.on_trouble = self.warm_up      # after a hang: reload the voices in the background
        self._remove_leftovers()

    def settings(self):
        return voice_settings(self.cfg, self.recorded)

    def active(self):
        return self.settings()["source"] == "piper"

    def ready(self):
        return self.engine.ready()

    # -- where a sentence is kept
    def _dir_key(self, lv):
        """Everything about the voice that changes the audio: the voice and its model file,
        the speed and the number style. (The exact words go in each file's name.)"""
        try:
            st = os.stat(os.path.join(self.cfg["audio"]["tts_voices"], lv["voice"] + ".onnx"))
            model = [st.st_size, int(st.st_mtime)]
        except (OSError, KeyError):
            model = None
        k = json.dumps({"format": CACHE_FORMAT, "voice": lv["voice"], "model": model, "pace": float(lv["pace"]),
                        "digits": bool(lv.get("digits")), "pause": int(lv.get("digit_pause_ms", 0))},
                       sort_keys=True)
        return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16]

    def _path(self, lang, lv, segments):
        t = hashlib.sha256(json.dumps(segments, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, "%s-%s" % (lang, self._dir_key(lv)), t[:32] + ".wav")

    def is_kept(self, lang, lv, segments):
        return self.kept(lang, lv, segments) is not None

    # -- speaking
    def kept(self, lang, lv, segments):
        """The kept audio for these segments (16-bit PCM), or None."""
        path = self._path(lang, lv, segments)
        try:
            data = _whole_file(path)
            if data is None:
                return None                        # cut short (power cut while writing?): made again
            pcm = read_wav(path)
        except (OSError, ValueError):
            return None
        try:
            os.utime(path)                         # recently used: kept longest
        except OSError:
            pass
        return pcm

    def frames(self, lang, lv, segments, urgent=True, timeout=PAGE_BUDGET_S):
        """16-bit PCM of the segments in this voice: kept audio, or made now and kept."""
        pcm = self.kept(lang, lv, segments)
        if pcm is not None:
            return pcm
        wav = self.engine.speak(lv["voice"], float(lv["pace"]), segments, timeout, urgent)
        pcm = _wav_pcm(wav)
        if not pcm.strip(b"\x00"):                 # empty or all silence: nothing was said
            raise AudioError("Piper made no sound")
        data, played = _ulaw_wav(pcm)
        self._keep(self._path(lang, lv, segments), data)
        return played                              # the same audio the kept file will play next time

    def _keep(self, path, data):
        tmp = path + ".tmp%d" % threading.get_ident()
        try:
            new_dir = not os.path.isdir(os.path.dirname(path))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            if new_dir:
                self._prune_dirs()
            self.writes += 1
            if self.writes % 200 == 0:
                self._trim()
        except OSError as e:
            LOG.warning("could not keep the spoken sentence: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def sentence(self, lang, code, press, vs=None, urgent=True, timeout=PAGE_BUDGET_S):
        vs = vs or self.settings()
        lv = vs[lang]
        return self.frames(lang, lv, sentence_segments(self.cfg, lv, lang, code, press), urgent, timeout)

    def text(self, lang, text, vs=None, urgent=True, timeout=PAGE_BUDGET_S):
        """Free text (a custom page). There is no recording to fall back on, so if the chosen
        voice isn't installed, any installed voice for the language is used."""
        vs = vs or self.settings()
        segs = text_segments(lang, text)
        if not segs:
            raise AudioError("no text to speak")
        lv = vs[lang]
        installed = self.installed()
        if installed and lv["voice"] not in installed:
            other = [v for v in installed if v.startswith(lang + "_")]
            if other:
                lv = dict(lv, voice=other[0])
        return self.frames(lang, lv, segs, urgent, timeout)

    # -- housekeeping
    def _current_dirs(self):
        vs = self.settings()
        return {"%s-%s" % (lang, self._dir_key(vs[lang])) for lang in LANGS}

    def _prune_dirs(self, keep_only_current=False):
        """Remove kept audio for settings no longer in use: everything but the saved
        settings when asked, else all but the newest MAX_PREVIEW_DIRS previewed ones."""
        try:
            names = [n for n in os.listdir(self.cache_dir) if os.path.isdir(os.path.join(self.cache_dir, n))]
        except OSError:
            return
        keep = self._current_dirs()
        others = sorted((n for n in names if n not in keep),
                        key=lambda n: os.path.getmtime(os.path.join(self.cache_dir, n)), reverse=True)
        drop = others if keep_only_current else others[MAX_PREVIEW_DIRS:]
        for n in drop:
            shutil.rmtree(os.path.join(self.cache_dir, n), ignore_errors=True)

    def _remove_leftovers(self):
        """Half-written files left by a crash or power cut."""
        for root, _, files in os.walk(self.cache_dir):
            for f in files:
                if ".tmp" in f:
                    try:
                        os.remove(os.path.join(root, f))
                    except OSError:
                        pass

    def _trim(self):
        """Keep all kept audio under MAX_CACHE_MB: remove the least recently used files."""
        files, total = [], 0
        for root, _, names in os.walk(self.cache_dir):
            for f in names:
                path = os.path.join(root, f)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, path))
                total += st.st_size
        for _, size, path in sorted(files):
            if total <= MAX_CACHE_MB * 1e6:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass

    def saved_settings_changed(self):
        """Called after new settings are saved: drop audio kept for other settings."""
        self._prune_dirs(keep_only_current=True)

    def clear(self):
        self.stop_making_all()
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def stats(self):
        """(sentences kept for the saved settings, MB used by all kept audio)."""
        n, size = 0, 0
        current = self._current_dirs()
        for root, _, files in os.walk(self.cache_dir):
            for f in files:
                try:
                    size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
                if os.path.basename(root) in current and f.endswith(".wav"):
                    n += 1
        return n, size / 1e6

    # -- make all messages ahead of time
    def plan(self, max_press=100):
        """Every (lang, code, press) a page can need for presses 0..max_press."""
        langs = [l for l in LANGS if l in self.cfg.get("audio", {}).get("languages", list(LANGS))]
        return [(lang, code, press) for code in sorted(self.cfg["codes"]) for press in range(0, max_press + 1)
                for lang in langs]

    def start_making_all(self, max_press=100):
        with self.lock:
            if self.job.get("running"):
                return False
            self.job = {"running": True, "done": 0, "made": 0, "failed": 0, "total": 0,
                        "started": time.time(), "error": ""}
        self.job_thread = threading.Thread(target=self._make_all, args=(max_press,), daemon=True)
        self.job_thread.start()
        return True

    def stop_making_all(self, wait=10):
        """Stop "make all messages" and wait (a little) until it has stopped."""
        self.job["stop"] = True
        t = self.job_thread
        if t is not None and t is not threading.current_thread():
            t.join(wait)

    def _make_all(self, max_press):
        vs = self.settings()
        todo = self.plan(max_press)
        self.job["total"] = len(todo)
        try:
            for lang, code, press in todo:
                if self.job.get("stop") or self.settings() != vs:
                    self.job["error"] = "stopped" if self.job.get("stop") else "the voice settings changed"
                    break
                lv = vs[lang]
                segs = sentence_segments(self.cfg, lv, lang, code, press)
                if not self.is_kept(lang, lv, segs):
                    while self.page_busy() and not self.job.get("stop"):
                        time.sleep(0.5)             # a page is going out: don't take the CPU
                    self.engine.wait_turn()
                    if self.job.get("stop") or self.settings() != vs:
                        continue                    # (the loop's first check ends it)
                    try:
                        self.frames(lang, lv, segs, urgent=False, timeout=SLOW_TIMEOUT_S)
                        self.job["made"] += 1
                    except AudioError as e:
                        self.job["failed"] += 1
                        self.job["error"] = str(e)
                        if self.job["failed"] >= 5 and not self.job["made"]:
                            break                  # Piper isn't working: don't grind through all of them
                self.job["done"] += 1
        finally:
            self.job["running"] = False
            self.job["finished"] = time.time()
            LOG.info("page voice: made %d message(s) ahead of time, %d failed%s", self.job["made"],
                     self.job["failed"], " (%s)" % self.job["error"] if self.job["error"] else "")

    def warm_up(self):
        """Load the saved voices in the background, so the first page doesn't wait for it."""
        def go():
            vs = self.settings()
            for lang in LANGS:
                try:
                    self.engine.wait_turn()
                    self.engine.speak(vs[lang]["voice"], float(vs[lang]["pace"]), [{"text": "OK."}],
                                      SLOW_TIMEOUT_S)
                except AudioError as e:
                    LOG.warning("page voice: could not load the %s voice: %s", lang, e)
        if self.active() and self.ready():
            threading.Thread(target=go, daemon=True).start()
