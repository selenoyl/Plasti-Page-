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

import io
import json
import math
import os
import re
import sys
import unicodedata
import wave
from array import array

RATE = 8000
# Pauses inside a page, in ms (defaults; lang_gap/repeat_gap/tone_gap can be set on
# the admin page). Paging feedback eliminators end a page after a short silence, so
# every pause stays well under a second.
DEFAULT_GAPS = {"lang_gap_ms": 400, "repeat_gap_ms": 500, "tone_gap_ms": 250}
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
                tone = self._frames(key)
                if style == "twice":
                    # The single tone twice with a short gap (the "twice" files have long
                    # gaps inside that a feedback eliminator can take as the end of the page).
                    tone = tone + silence(_gap(cfg, "tone_gap_ms")) + tone
                return tone + silence(300), []
            except AudioError:
                note = "tone %s missing, chime used" % key
        else:
            note = ""
        return self._frames("chime.wav") + silence(300), [note] if note else []

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
        vs: voice settings to use instead of the saved ones (a preview)."""
        vs = self.piper_settings(vs)
        if self.load_error and not vs:
            raise AudioError(self.load_error)
        opts = cfg.get("audio", {})
        langs = [l for l in ("en", "es") if l in opts.get("languages", ["en", "es"])]
        body, notes = b"", []
        for i, lang in enumerate(langs):
            if i:
                body += silence(_gap(cfg, "lang_gap_ms"))
            if vs:
                try:
                    body += self.voice.sentence(lang, code, press, vs=vs, urgent=urgent,
                                                **({"timeout": timeout} if timeout else {}))
                    continue
                except AudioError as e:
                    notes.append("Piper voice failed (%s), so the %s sentence is the recorded clip" % (e, lang))
                    if self.load_error:
                        raise AudioError("%s; and %s" % (e, self.load_error))
            if press > self.max_press and "press %d is above" % press not in " ".join(notes):
                notes.append("press %d is above the recorded 1-%d, so the page says press number unknown"
                             % (press, self.max_press))
            keys, warn = self.pieces(cfg, code, press, lang)
            notes += ["clip %s no longer matches the config wording" % k for k in warn]
            for k in keys:
                body += self._frames(k)
        lead, tone_notes = self._lead_in_safe(cfg, code, bool(vs))
        return self._wrap(lead, body, opts), notes + tone_notes

    def _lead_in_safe(self, cfg, code, piper):
        try:
            return self.lead_in(cfg, code)
        except AudioError as e:
            if not piper:
                raise
            return b"", ["no tone (%s)" % e]

    def build_custom(self, cfg, en, es, vs=None):
        """A page of free text, spoken by Piper with the page voice (or, while pages use the
        recorded clips, with the voices the clips were made with)."""
        if self.voice is None or not self.voice.ready():
            raise AudioError("custom text needs Piper on the laptop (sudo bash install/install_tts.sh)")
        vs = vs or self.voice.settings()
        opts = cfg.get("audio", {})
        body = self.voice.text("en", en, vs=vs)
        if es and "es" in opts.get("languages", ["en", "es"]):
            body += silence(_gap(cfg, "lang_gap_ms")) + self.voice.text("es", es, vs=vs)
        lead, notes = self._lead_in_safe(cfg, None, True)
        return self._wrap(lead, body, opts)

    def _wrap(self, lead, body, opts):
        once = lead + body
        repeat = max(1, int(opts.get("repeat", 2)))
        # Silence first, so the paging system is open before the tone starts.
        gap = silence(int(opts.get("repeat_gap_ms", DEFAULT_GAPS["repeat_gap_ms"])))
        frames = (silence(int(opts.get("lead_silence_ms", 0)))
                  + (once + gap) * (repeat - 1) + once + silence(300))
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
