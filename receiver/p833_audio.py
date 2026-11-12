# P833 Call Receiver - builds the spoken page from the recorded clips.
# By Ethan Brady & Ryan Raikes
#
# The clips are made by tools/make_audio.py (see its header for the layout).
# Every message is one whole recorded sentence per code, press and language, so
# it flows naturally (nothing is joined mid-sentence). A page is: department
# tone, the English sentence, a pause, the Spanish sentence, and the whole thing
# repeated. Tones are in <audio>/tones/single; a code with no tone gets the chime.
# Clips may be 16-bit PCM or 8-bit mu-law WAV (phone quality, half the size).
#
# With the Piper page voice switched on (admin page, Page voice; see p833_voice.py)
# the sentences are spoken live instead, and kept; the tones still come from here,
# and any sentence Piper can't make in time falls back to its recorded clip.
# Standard library only; Python 3.6+.

import collections
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import unicodedata
import wave
from array import array

RATE = 8000
# Pauses inside a page, in ms (defaults; lang_gap/repeat_gap/tone_gap can be set on
# the admin page). Paging feedback eliminators end a page after a short silence, so
# every pause stays well under a second.
DEFAULT_GAPS = {"lang_gap_ms": 400, "repeat_gap_ms": 500, "tone_gap_ms": 250, "tone_speech_gap_ms": 400}
# PA clarity (admin page): shape the speech for a paging horn instead of for listening.
# Cut the bass (mud through a horn), lift 2-4 kHz (where consonants are: "press" vs
# "tech"), even out the loudness so every syllable is at the same level, and keep the
# peaks below full scale so nothing down the line (HT813, ATA2, Norstar, PA amplifier)
# is overdriven. When it is on, the page volume (gain_db) isn't used: pa_level_db sets
# the peak level instead.
CLARITY_DEFAULTS = {"pa_process": False, "pa_highpass_hz": 300, "pa_presence_db": 4, "pa_compress": "medium",
                    "pa_level_db": -3}
# evenness: (compressor threshold below the peak dB, ratio, aimed loud-to-average spread dB)
COMPRESS = {"off": None, "light": (-18.0, 2.0, 15.0), "medium": (-24.0, 3.0, 12.0), "strong": (-30.0, 5.0, 10.0)}
# How press numbers are spoken: languages listed in audio.speak_digits are read
# digit by digit ("two, seven"); the others as a whole number ("veintisiete").
DIGIT_WORDS = {
    "en": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"],
    "es": ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve"],
}


class AudioError(Exception):
    pass


def split_text(text):
    """Same rule as tools/make_audio.py: text around {press}, punctuation-only tail dropped."""
    pre, post = text.split("{press}", 1)
    post = post.strip()
    return pre.strip(), post if any(ch.isalnum() for ch in post) else ""


def slug(text):
    """'Se solicita un técnico en la prensa' -> 'se_solicita_un_tecnico_en_la_prensa'."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "_".join(re.findall(r"[a-z0-9]+", t))[:60] or "x"


def speak_digits(cfg, lang):
    return lang in cfg.get("audio", {}).get("speak_digits", ["en"])


def spoken_press(cfg, press, lang):
    """How the press number is said: '2, 7' style words or the plain number."""
    if speak_digits(cfg, lang) and lang in DIGIT_WORDS:
        return ", ".join(DIGIT_WORDS[lang][int(d)] for d in str(press))
    return str(press)


def spoken_text(cfg, code, press, lang):
    """The exact sentence recorded for this code, press and language."""
    return cfg["codes"][code][lang].replace("{press}", spoken_press(cfg, press, lang))


def sentence_key(lang, code, press):
    return "%s/s/c%d/%d.wav" % (lang, code, press)


def read_wav(path):
    """16-bit PCM samples from a mono 8 kHz WAV that is either 16-bit PCM or
    8-bit mu-law (format 7), which Python's wave module can't read."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, body = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        chunk = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = (int.from_bytes(chunk[0:2], "little"), int.from_bytes(chunk[2:4], "little"),
                   int.from_bytes(chunk[4:8], "little"), int.from_bytes(chunk[14:16], "little"))
        elif cid == b"data":
            body = chunk
        pos += 8 + size + (size & 1)
    if not fmt or body is None:
        raise ValueError("WAV has no fmt or data")
    tag, channels, rate, bits = fmt
    if channels != 1 or rate != RATE:
        raise ValueError("not 8 kHz mono")
    if tag == 1 and bits == 16:
        return body[:len(body) // 2 * 2]
    if tag == 7 and bits == 8:
        return _ulaw_decode(body)
    raise ValueError("WAV format %d/%d-bit not supported" % (tag, bits))


def _ulaw_sample(u):
    u = ~u & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 7, u & 0x0F
    s = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -s if sign else s


_ULAW_TO_PCM = None


def _ulaw_decode(data):
    global _ULAW_TO_PCM
    if _ULAW_TO_PCM is None:
        _ULAW_TO_PCM = [_ulaw_sample(u) for u in range(256)]
    a = array("h", [_ULAW_TO_PCM[b] for b in data])
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


_GAIN_TABLES = {}


def _gain_table(gain_db):
    """Sample -> louder sample, with a soft limiter: below 60% of full scale the
    gain is plain; above it, peaks are rounded off smoothly instead of clipping."""
    key = round(float(gain_db), 1)
    if key not in _GAIN_TABLES:
        g = 10 ** (key / 20.0)
        knee = 0.6
        table = []
        for s in range(-32768, 32768):
            x = s / 32768.0 * g
            ax = abs(x)
            if ax > knee:
                ax = knee + (1 - knee) * math.tanh((ax - knee) / (1 - knee))
            table.append(int(max(-32767, min(32767, round(math.copysign(ax, x) * 32767)))))
        _GAIN_TABLES[key] = table
    return _GAIN_TABLES[key]


def apply_gain(frames, gain_db):
    """Make 16-bit audio louder (or quieter) by gain_db, without hard clipping."""
    if not gain_db:
        return frames
    a = array("h")
    a.frombytes(frames)
    if sys.byteorder != "little":
        a.byteswap()
    table = _gain_table(gain_db)
    out = array("h", [table[v + 32768] for v in a])
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def silence(ms):
    return b"\x00\x00" * int(RATE * ms / 1000)


# Keypad tones (DTMF). The Nortel ATA2 listens to the line for these, as from an analog
# phone. A tone with a steady pair of overtones near a key's two tones can be heard as a
# key press during a page. Measured: the packaging pluck holds a "4" (its 792 and 1188 Hz
# overtones, within 3% of 770 and 1209 Hz) for 0.34 s; phone equipment needs about 40 ms.
# No other tone holds one, and speech at most 0.06 s (and is never changed).
DTMF_LOW = (697, 770, 852, 941)
DTMF_HIGH = (1209, 1336, 1477, 1633)
DTMF_KEYS = ("123A", "456B", "789C", "*0#D")
DTMF_HOLD_S = 0.04          # a key pair held this long can count as a key press


def _goertzel_power(x, f):
    w = 2 * math.pi * f / RATE
    c, s1, s2 = 2 * math.cos(w), 0.0, 0.0
    for v in x:
        s1, s2 = v + c * s1 - s2, s1
    return s1 * s1 + s2 * s2 - c * s1 * s2


_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / 319) for i in range(320)]
_STEPS = (0.97, 0.985, 1.0, 1.015, 1.03)   # phone equipment accepts about 3% off


def dtmf_check(pcm):
    """(seconds, key, upper frequencies): the longest a keypad pair is held steady in the
    sound, as a phone's key-press receiver hears it (40 ms windows every 20 ms; the two
    tones within 8 dB of each other and at least half the sound), and the exact upper-group
    pitches found in those stretches."""
    x = _floats(pcm)
    n, step = 320, 160
    best, run, prev, found, now_f = 0, 0, None, set(), []
    for i in range(0, len(x) - n + 1, step):
        b = [v * h for v, h in zip(x[i:i + n], _HANN)]      # a window, so nearby notes don't leak in
        energy = sum(v * v for v in b)
        key = None
        if energy > 1e-7:
            lo, li = max((_goertzel_power(b, f * k), j) for j, f in enumerate(DTMF_LOW) for k in _STEPS)
            hi, hf, hj = max((_goertzel_power(b, f * k), f * k, j) for j, f in enumerate(DTMF_HIGH) for k in _STEPS)
            if lo > 0 and hi > 0 and abs(10 * math.log10(lo / hi)) <= 8 \
                    and (lo + hi) / (energy * n / 2.0 * 0.667) >= 0.5:
                key = DTMF_KEYS[li][hj]
        if key is not None and key == prev:
            run += 1
            now_f.append(hf)
        else:
            run, now_f = (1, [hf]) if key else (0, [])
        prev = key
        if run * step / RATE >= DTMF_HOLD_S:
            found.update(round(f) for f in now_f)
        best = max(best, run)
    return best * step / RATE, sorted(found)


def dtmf_risk(pcm):
    """The longest a keypad pair is held in the sound, in seconds (see dtmf_check)."""
    return dtmf_check(pcm)[0]


def _notch(x, f, q=10.0):
    """Cut a narrow band around f (RBJ notch)."""
    w = 2 * math.pi * f / RATE
    al, cw = math.sin(w) / (2 * q), math.cos(w)
    a0 = 1 + al
    return _biquad(x, 1 / a0, -2 * cw / a0, 1 / a0, -2 * cw / a0, (1 - al) / a0)


def dtmf_safe(pcm):
    """A tone that can't be heard as a key press. A key press needs one upper and one lower
    keypad tone held together, so where a tone holds such a pair, its upper one is cut out
    at the exact pitch found, until no pair is held. Safe tones are returned as they are."""
    for _ in range(4):
        held, freqs = dtmf_check(pcm)
        if held < DTMF_HOLD_S or not freqs:
            return pcm
        x = _floats(pcm)
        for f in freqs:
            x = _notch(_notch(x, f), f)
        pcm = _pcm(x)
    return pcm


def clarity_of(opts):
    """The PA clarity settings from the audio options, each value checked."""
    c = dict(CLARITY_DEFAULTS)
    c["pa_process"] = opts.get("pa_process") is True
    for key, lo, hi in (("pa_highpass_hz", 0, 600), ("pa_presence_db", 0, 10), ("pa_level_db", -20, 0)):
        try:
            v = int(opts.get(key, c[key]))
            if lo <= v <= hi:
                c[key] = v
        except (TypeError, ValueError):
            pass
    if opts.get("pa_compress") in COMPRESS:
        c["pa_compress"] = opts["pa_compress"]
    return c


def _floats(pcm):
    a = array("h")
    a.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        a.byteswap()
    return [v / 32768.0 for v in a]


def _pcm(x):
    a = array("h", [int(max(-32767, min(32767, round(v * 32767)))) for v in x])
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def _biquad(x, b0, b1, b2, a1, a2):
    out, x1, x2, y1, y2 = [], 0.0, 0.0, 0.0, 0.0
    append = out.append
    for v in x:
        y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, v, y1, y
        append(y)
    return out


def _highpass(x, fc, q=0.7071):
    """2nd-order high-pass (RBJ cookbook)."""
    w = 2 * math.pi * fc / RATE
    al, cw = math.sin(w) / (2 * q), math.cos(w)
    a0 = 1 + al
    return _biquad(x, (1 + cw) / 2 / a0, -(1 + cw) / a0, (1 + cw) / 2 / a0, -2 * cw / a0, (1 - al) / a0)


def _presence(x, gain_db, fc=2500.0, q=1.0):
    """Peaking boost around fc (RBJ cookbook)."""
    w = 2 * math.pi * fc / RATE
    al, cw, a = math.sin(w) / (2 * q), math.cos(w), 10 ** (gain_db / 40.0)
    a0 = 1 + al / a
    return _biquad(x, (1 + al * a) / a0, -2 * cw / a0, (1 - al * a) / a0, -2 * cw / a0, (1 - al / a) / a0)


def _compress(x, threshold_db, ratio, attack_ms=3.0, release_ms=60.0):
    """Even out the loudness: above the threshold (relative to the peak), level rises
    only 1/ratio as fast. A peak follower with fast attack and slower release."""
    peak = max((abs(v) for v in x), default=0.0)
    if peak <= 0:
        return x
    thr = peak * 10 ** (threshold_db / 20.0)
    att, rel = math.exp(-1.0 / (RATE * attack_ms / 1000.0)), math.exp(-1.0 / (RATE * release_ms / 1000.0))
    slope, env, out = 1.0 / ratio - 1.0, 0.0, []
    append = out.append
    for v in x:
        av = abs(v)
        env = att * env + (1 - att) * av if av > env else rel * env + (1 - rel) * av
        append(v * (env / thr) ** slope if env > thr else v)
    return out


def _speech_rms(x):
    loud = [v * v for v in x if abs(v) > 1e-3]
    return math.sqrt(sum(loud) / len(loud)) if loud else 0.0


def _limit(x, ceiling, look=40, release_ms=50.0):
    """Look-ahead limiter: the level is turned down smoothly just before each peak (5 ms
    ahead) and back up after it, so peaks never pass the ceiling and nothing is clipped."""
    n = len(x)
    need = [min(1.0, ceiling / abs(v)) if v else 1.0 for v in x]
    ahead, dq = [1.0] * n, collections.deque()
    for i in range(n - 1, -1, -1):              # the lowest level needed over the next 5 ms
        while dq and need[dq[-1]] >= need[i]:
            dq.pop()
        dq.append(i)
        while dq[0] > i + look:
            dq.popleft()
        ahead[i] = need[dq[0]]
    rel = math.exp(-1.0 / (RATE * release_ms / 1000.0))
    g, gains = 1.0, []
    for a in ahead:                             # down at once (it is already 5 ms early), up slowly
        g = a if a < g else rel * g + (1 - rel) * a
        gains.append(g)
    half, out, acc = look // 4, [], 0.0           # a short average smooths each step
    window = collections.deque()
    for i, v in enumerate(x):
        j = i + half
        if j < n:
            window.append(gains[j])
            acc += gains[j]
        if len(window) > 2 * half + 1:
            acc -= window.popleft()
        out.append(v * min(gains[i], acc / len(window)))
    return [max(-ceiling, min(ceiling, v)) for v in out]   # (a last guard; rarely touches anything)


_CLARITY_CACHE = collections.OrderedDict()


def pa_process(pcm, c):
    """Speech shaped for the PA (see CLARITY_DEFAULTS). Remembered for the last few hundred
    sentences, since the same messages come again and again."""
    key = (hashlib.sha1(pcm).hexdigest(), c["pa_highpass_hz"], c["pa_presence_db"], c["pa_compress"],
           c["pa_level_db"])
    if key in _CLARITY_CACHE:
        _CLARITY_CACHE.move_to_end(key)
        return _CLARITY_CACHE[key]
    x = _floats(pcm)
    if c["pa_highpass_hz"]:
        x = _highpass(x, c["pa_highpass_hz"])
    if c["pa_presence_db"]:
        x = _presence(x, c["pa_presence_db"])
    spread = 15.0
    if COMPRESS.get(c["pa_compress"]):
        thr, ratio, spread = COMPRESS[c["pa_compress"]]
        x = _compress(x, thr, ratio)
    rms = _speech_rms(x)
    if rms > 0:                                 # bring the average up, then hold the peaks down
        g = 10 ** ((c["pa_level_db"] - spread) / 20.0) / rms
        x = _limit([v * g for v in x], 10 ** (c["pa_level_db"] / 20.0))
    out = _pcm(x)
    _CLARITY_CACHE[key] = out
    while len(_CLARITY_CACHE) > 400:
        _CLARITY_CACHE.popitem(last=False)
    return out


def level_only(pcm, level_db):
    """Tones with PA clarity on: just brought to the same peak level as the speech."""
    x = _floats(pcm)
    peak = max((abs(v) for v in x), default=0.0)
    if peak <= 0:
        return pcm
    g = 10 ** (level_db / 20.0) / peak
    return _pcm([v * g for v in x])


class AudioLibrary:
    def __init__(self, folder):
        self.folder = folder
        self.manifest = {}
        self.cache = {}
        self.load_error = ""
        self.voice = None          # the Piper page voice (p833_voice.PageVoice), set by the receiver
        try:
            with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
                self.manifest = json.load(f)
        except (OSError, ValueError) as e:
            self.load_error = "no audio clips found in %s (%s)" % (folder, e.__class__.__name__)

    @property
    def clips(self):
        return self.manifest.get("clips", {})

    def _frames(self, key):
        if key in self.cache:
            return self.cache[key]
        path = os.path.join(self.folder, key)
        try:
            data = read_wav(path)
        except (OSError, ValueError) as e:
            raise AudioError("clip %s missing or unreadable (%s)" % (key, e))
        if len(self.cache) < 4000:
            self.cache[key] = data
        return data

    @property
    def max_press(self):
        return int(self.manifest.get("max_press", 999))

    def tone(self, name):
        """A department tone, or "chime", made safe from looking like a keypad press (dtmf_safe)."""
        key = "tones/single/%s.wav" % name if name != "chime" else "chime.wav"
        if ("safe", key) not in self.cache:
            self.cache[("safe", key)] = dtmf_safe(self._frames(key))
        return self.cache[("safe", key)]

    def tone_names(self):
        """Tones available (file names without .wav)."""
        try:
            return sorted(n[:-4] for n in os.listdir(os.path.join(self.folder, "tones", "single"))
                          if n.endswith(".wav"))
        except OSError:
            return []

    def lead_in(self, cfg, code):
        """The sound before each play: the code's department tone, or the chime.
        Returns (frames, notes)."""
        style = tone_style(cfg)
        if style == "off":
            return b"", []
        name = cfg["codes"][code].get("tone", "") if code else ""
        if name:
            key = "tones/single/%s.wav" % name
            try:
                tone = self.tone(name)
                if style == "twice":
                    # The single tone twice with a short gap (the "twice" files have long
                    # gaps inside that a feedback eliminator can take as the end of the page).
                    tone = tone + silence(_gap(cfg, "tone_gap_ms")) + tone
                return self._level_tone(cfg, tone) + silence(_gap(cfg, "tone_speech_gap_ms")), []
            except AudioError:
                note = "tone %s missing, chime used" % key
        else:
            note = ""
        return (self._level_tone(cfg, self.tone("chime")) + silence(_gap(cfg, "tone_speech_gap_ms")),
                [note] if note else [])

    @staticmethod
    def _level_tone(cfg, tone):
        c = clarity_of(cfg.get("audio", {}))
        return level_only(tone, c["pa_level_db"]) if c["pa_process"] else tone

    @staticmethod
    def speech(cfg, pcm):
        """One sentence (or the voice lab's text), shaped for the PA if PA clarity is on."""
        c = clarity_of(cfg.get("audio", {}))
        return pa_process(pcm, c) if c["pa_process"] else pcm

    def pieces(self, cfg, code, press, lang):
        """Clip keys for one language, plus any 'text changed' warnings."""
        c = cfg["codes"][code]
        warn = []
        if press > self.max_press:
            press = 0           # not recorded: say "press number unknown"
        if press == 0:
            key = "%s/c%d_nopress.wav" % (lang, code)
            want = c.get(lang + "_no_press", c[lang])
            keys = [key]
            if self.clips.get(key) not in (None, want):
                warn.append(key)
            return keys, warn
        key = sentence_key(lang, code, press)
        if self.clips.get(key) not in (None, spoken_text(cfg, code, press, lang)):
            warn.append(key)
        return [key], warn

    def piper_settings(self, vs=None):
        """The voice settings to speak with, or None when pages use the recorded clips."""
        if self.voice is None:
            return None
        vs = vs or self.voice.settings()
        return vs if vs["source"] == "piper" else None

    def build(self, cfg, code, press, vs=None, urgent=True, timeout=None):
        """Return (WAV bytes, list of notes: out-of-date clips, missing tone, Piper fallbacks, ...).
        vs: voice settings to use instead of the saved ones (a preview). timeout: the most time
        the page spends on Piper in all (default: the voice's page budget); kept sentences
        take none, and what doesn't fit in it comes from the recorded clips."""
        vs = self.piper_settings(vs)
        piper = bool(vs)
        if self.load_error and not vs:
            raise AudioError(self.load_error)
        opts = cfg.get("audio", {})
        langs = [l for l in ("en", "es") if l in opts.get("languages", ["en", "es"])]
        end = time.monotonic() + (timeout or getattr(self.voice, "page_budget", 10))
        parts, notes = [], []
        for lang in langs:
            if vs:
                try:
                    parts.append(self.voice.sentence(lang, code, press, vs=vs, urgent=urgent,
                                                     timeout=max(0.0, end - time.monotonic())))
                    continue
                except Exception as e:      # whatever went wrong, the page goes on (any bug included)
                    why = e if isinstance(e, AudioError) else "%s: %s" % (e.__class__.__name__, e)
                    if self.load_error:
                        notes.append("Piper voice failed (%s) and there are no recordings, so the page has "
                                     "no %s sentence" % (why, lang))
                        continue
                    notes.append("Piper voice failed (%s), so the %s sentence is the recorded clip" % (why, lang))
                    vs = None                   # and the rest of this page uses the recordings at once
            if press > self.max_press and "press %d is above" % press not in " ".join(notes):
                notes.append("press %d is above the recorded 1-%d, so the page says press number unknown"
                             % (press, self.max_press))
            keys, warn = self.pieces(cfg, code, press, lang)
            notes += ["clip %s no longer matches the config wording" % k for k in warn]
            parts.append(b"".join(self._frames(k) for k in keys))
        if not parts:
            raise AudioError("; ".join(notes) or "nothing to say")
        body = silence(_gap(cfg, "lang_gap_ms")).join(self.speech(cfg, p) for p in parts)
        lead, tone_notes = self._lead_in_safe(cfg, code, piper)
        return self._wrap(lead, body, opts), notes + tone_notes

    def _lead_in_safe(self, cfg, code, piper):
        try:
            return self.lead_in(cfg, code)
        except AudioError as e:
            if not piper:
                raise
            return b"", ["no tone (%s)" % e]

    def build_custom(self, cfg, en, es, vs=None, urgent=True):
        """A page of free text, spoken by Piper with the page voice (or, while pages use the
        recorded clips, with the voices the clips were made with)."""
        if self.voice is None or not self.voice.ready():
            raise AudioError("custom text needs Piper on the laptop (sudo bash install/install_tts.sh)")
        vs = vs or self.voice.settings()
        opts = cfg.get("audio", {})
        end = time.monotonic() + getattr(self.voice, "page_budget", 10)
        try:
            body = self.voice.text("en", en, vs=vs, urgent=urgent, timeout=max(0.0, end - time.monotonic()))
        except AudioError:
            raise
        except Exception as e:
            raise AudioError("%s: %s" % (e.__class__.__name__, e))
        body = self.speech(cfg, body)
        if es and "es" in opts.get("languages", ["en", "es"]):
            try:
                body += silence(_gap(cfg, "lang_gap_ms")) + self.speech(cfg, self.voice.text(
                    "es", es, vs=vs, urgent=urgent, timeout=max(0.0, end - time.monotonic())))
            except Exception:
                pass                         # the English part still goes out
        lead, notes = self._lead_in_safe(cfg, None, True)
        return self._wrap(lead, body, opts)

    def _wrap(self, lead, body, opts):
        once = lead + body
        repeat = max(1, int(opts.get("repeat", 2)))
        # Silence first, so the paging system is open before the tone starts.
        gap = silence(int(opts.get("repeat_gap_ms", DEFAULT_GAPS["repeat_gap_ms"])))
        frames = (silence(int(opts.get("lead_silence_ms", 0)))
                  + (once + gap) * (repeat - 1) + once + silence(300))
        if not clarity_of(opts)["pa_process"]:       # with PA clarity, the level is set already
            frames = apply_gain(frames, float(opts.get("gain_db", 0)))
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(frames)
        return out.getvalue()

    def check(self, cfg):
        """Problems to show on the admin page: missing or out-of-date clips."""
        if self.load_error:
            return [self.load_error]
        problems = []
        for code in range(2, 10):
            for lang in ("en", "es"):
                for press in (0, 1):
                    keys, warn = self.pieces(cfg, code, press, lang)
                    for k in keys:
                        if k not in self.clips or not os.path.exists(os.path.join(self.folder, k)):
                            problems.append("missing clip %s" % k)
                    for k in warn:
                        problems.append("clip %s no longer matches the config wording" % k)
            style = tone_style(cfg)
            name = cfg["codes"][code].get("tone", "")
            if style != "off" and name:
                key = "tones/single/%s.wav" % name
                if not os.path.exists(os.path.join(self.folder, key)):
                    problems.append("code %d tone file %s is missing (the chime plays instead)" % (code, key))
        return sorted(set(problems))


def _gap(cfg, key):
    try:
        return max(0, int(cfg.get("audio", {}).get(key, DEFAULT_GAPS[key])))
    except (TypeError, ValueError):
        return DEFAULT_GAPS[key]


def tone_style(cfg):
    """'once', 'twice' or 'off'. Older configs had "chime": false for off."""
    opts = cfg.get("audio", {})
    style = opts.get("tones")
    if style not in ("once", "twice", "off"):
        style = "once" if opts.get("chime", True) else "off"
    return style
