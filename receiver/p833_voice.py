# P833 Call Receiver - the page voice: speaks pages live with Piper on the laptop,
# with the voice settings saved on the admin page, and keeps every sentence it
# speaks, so the same message with the same settings plays from disk next time.
# By Ethan Brady & Ryan Raikes
#
#   source "recorded": pages use the clips made by tools/make_audio.py (as shipped).
#   source "piper":    each sentence is spoken by Piper with the saved settings,
#                      kept in <state dir>/voice_cache, and reused after that.
#
# A page never fails because of Piper: if it isn't installed, stops, or takes too
# long, that sentence comes from the recorded clips instead (and the log says so).
#
# Piper runs as one helper process (receiver/p833_tts.py serve) in its own Python
# environment, with the voices it uses kept loaded (loading one takes a second or
# two; a sentence then takes a fraction of a second). It runs at a lower CPU
# priority than the receiver, without the receiver's secrets. With the Piper
# voice in use it stays running; otherwise it stops after 10 idle minutes.
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

from p833_audio import DIGIT_WORDS, RATE, AudioError, read_wav

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
PAGE_TIMEOUT_S = 15       # a live page waits this long for a sentence, then uses the recorded clip
SLOW_TIMEOUT_S = 60       # previews, the voice lab and "make all messages"
IDLE_EXIT_S = 600         # the helper stops after this long unused (unless the Piper voice is in use)
MAX_PREVIEW_DIRS = 8      # kept-audio folders for settings only previewed, not saved


# ------------------------------------------------------------- settings ---

def voice_settings(cfg):
    """The page voice settings: defaults, with the saved ones on top."""
    saved = cfg.get("audio", {}).get("voice", {}) or {}
    out = {"source": saved.get("source", VOICE_DEFAULTS["source"])}
    if out["source"] not in ("recorded", "piper"):
        out["source"] = "recorded"
    for lang in LANGS:
        out[lang] = dict(VOICE_DEFAULTS[lang])
        if isinstance(saved.get(lang), dict):
            out[lang].update({k: v for k, v in saved[lang].items() if k in VOICE_DEFAULTS[lang]})
    return out


def parse_voice_form(form, voices):
    """The page voice settings from the admin form. Returns (settings, errors)."""
    errs, out = [], {"source": "piper"}
    for lang, name in (("en", "English"), ("es", "Spanish")):
        v = form.get(lang + "_voice", "")
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
            if not 0 <= pause <= 1500:
                raise ValueError
        except ValueError:
            errs.append("%s pause between digits must be 0 to 1500 ms" % name)
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
    pause = int(lv.get("digit_pause_ms", 0))
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
    """Free text (a custom page): [350] marks a pause, as in the voice lab."""
    segs = []
    for i, part in enumerate(re.split(r"\[(\d{1,4})\]", text)):
        if i % 2:
            segs.append({"pause_ms": min(5000, int(part))})
        elif part.strip():
            segs.append({"text": say(lang, part.strip())})
    return segs


# ---------------------------------------------------------------- Piper ---

def tts_env():
    """A plain environment for Piper: none of the service's secrets."""
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "HOME": os.environ.get("STATE_DIRECTORY", "/tmp")}


def tts_ready(cfg):
    a = cfg["audio"]
    return os.path.exists(a["tts_python"]) and os.path.exists(a["tts_script"]) and os.path.isdir(a["tts_voices"])


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
        threading.Thread(target=self._idle_watch, daemon=True).start()

    def ready(self):
        return tts_ready(self.cfg)

    def wait_turn(self, timeout=30):
        """Background work calls this first: it waits while a page needs Piper."""
        end = time.monotonic() + timeout
        with self.turn:
            while self.urgent and time.monotonic() < end:
                self.turn.wait(0.5)

    def speak(self, voice, pace, segments, timeout, urgent=False):
        """WAV bytes (8 kHz 16-bit mono), or AudioError."""
        if not self.ready():
            raise AudioError("Piper isn't installed on this laptop (sudo bash install/install_tts.sh)")
        end = time.monotonic() + timeout
        if urgent:
            with self.turn:
                self.urgent += 1
        try:
            if not self.lock.acquire(timeout=max(0.1, timeout)):
                raise AudioError("Piper was busy for %d s" % timeout)
            try:
                return self._ask({"voice": voice, "length_scale": pace, "segments": segments}, end)
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
                raise AudioError("Piper took too long")
            except (EOFError, BrokenPipeError, OSError, ValueError, KeyError) as e:
                self._stop(kill=True)
                if attempt == 2 or time.monotonic() >= end:
                    raise AudioError("Piper stopped (%s)" % e.__class__.__name__)
        raise AudioError("Piper failed")

    def _idle_watch(self):
        while True:
            time.sleep(30)
            if self.proc is None or self.keep_running():
                continue
            if time.monotonic() - self.last_used > IDLE_EXIT_S and self.lock.acquire(blocking=False):
                try:
                    if time.monotonic() - self.last_used > IDLE_EXIT_S:
                        self._stop()
                        LOG.info("Piper helper stopped (idle)")
                finally:
                    self.lock.release()


# ---------------------------------------------------------- kept audio ---

def _ulaw_wav(pcm):
    """16-bit PCM -> an 8-bit mu-law WAV (like the recorded clips: half the size, and
    exactly what the phone line carries)."""
    from p833_sip import pcm16_to_ulaw
    data = pcm16_to_ulaw(pcm)
    fmt = (7).to_bytes(2, "little") + (1).to_bytes(2, "little") + RATE.to_bytes(4, "little") + \
        RATE.to_bytes(4, "little") + (1).to_bytes(2, "little") + (8).to_bytes(2, "little")
    body = b"WAVE" + b"fmt " + len(fmt).to_bytes(4, "little") + fmt + \
        b"data" + len(data).to_bytes(4, "little") + data + (b"\x00" if len(data) % 2 else b"")
    return b"RIFF" + len(body).to_bytes(4, "little") + body


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
        self.lock = threading.Lock()
        self.job = {"running": False}             # "make all messages" progress

    def settings(self):
        return voice_settings(self.cfg)

    def active(self):
        return self.settings()["source"] == "piper"

    def ready(self):
        return self.engine.ready()

    # -- where a sentence is kept
    @staticmethod
    def _dir_key(lv):
        k = json.dumps({"format": CACHE_FORMAT, "voice": lv["voice"], "pace": float(lv["pace"])}, sort_keys=True)
        return hashlib.sha256(k.encode("utf-8")).hexdigest()[:16]

    def _path(self, lang, lv, segments):
        t = hashlib.sha256(json.dumps(segments, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, "%s-%s" % (lang, self._dir_key(lv)), t[:32] + ".wav")

    def is_kept(self, lang, lv, segments):
        return os.path.exists(self._path(lang, lv, segments))

    # -- speaking
    def frames(self, lang, lv, segments, urgent=True, timeout=PAGE_TIMEOUT_S):
        """16-bit PCM of the segments in this voice: kept audio, or made now and kept."""
        path = self._path(lang, lv, segments)
        try:
            pcm = read_wav(path)
            try:
                os.utime(path)                     # recently used: kept longest
            except OSError:
                pass
            return pcm
        except (OSError, ValueError):
            pass
        wav = self.engine.speak(lv["voice"], float(lv["pace"]), segments, timeout, urgent)
        pcm = _wav_pcm(wav)
        if not pcm.strip(b"\x00"):                 # empty or all silence: nothing was said
            raise AudioError("Piper made no sound")
        data = _ulaw_wav(pcm)
        try:
            new_dir = not os.path.isdir(os.path.dirname(path))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp%d" % threading.get_ident()
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            if new_dir:
                self._prune_dirs()
        except OSError as e:
            LOG.warning("could not keep the spoken sentence: %s", e)
        return read_wav(path) if os.path.exists(path) else pcm

    def sentence(self, lang, code, press, vs=None, urgent=True, timeout=PAGE_TIMEOUT_S):
        vs = vs or self.settings()
        lv = vs[lang]
        return self.frames(lang, lv, sentence_segments(self.cfg, lv, lang, code, press), urgent, timeout)

    def text(self, lang, text, vs=None, urgent=True, timeout=PAGE_TIMEOUT_S):
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

    def saved_settings_changed(self):
        """Called after new settings are saved: drop audio kept for other settings."""
        self._prune_dirs(keep_only_current=True)

    def clear(self):
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
        threading.Thread(target=self._make_all, args=(max_press,), daemon=True).start()
        return True

    def stop_making_all(self):
        self.job["stop"] = True

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
