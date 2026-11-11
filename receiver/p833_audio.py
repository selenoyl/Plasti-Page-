# P833 Call Receiver - builds the spoken page from the recorded clips.
# By Ethan Brady & Ryan Raikes
#
# The clips are made by tools/make_audio.py (see its header for the layout).
# A page is: department tone, English, pause, Spanish, and the whole thing
# repeated. Tones are in <audio>/tones/single (played once) and
# <audio>/tones/twice (the tone played twice); a code with no tone gets the chime.
# Standard library only; Python 3.6+.

import io
import json
import os
import wave

RATE = 8000
TONE_DIRS = {"once": "single", "twice": "twice"}
PIECE_GAP_MS = 40    # pause between the pieces of one sentence


class AudioError(Exception):
    pass


def split_text(text):
    """Same rule as tools/make_audio.py: text around {press}, punctuation-only tail dropped."""
    pre, post = text.split("{press}", 1)
    post = post.strip()
    return pre.strip(), post if any(ch.isalnum() for ch in post) else ""


def shared_lead(cfg, lang):
    """The words every message shares just before the press number, e.g.
    'requested to press' or 'en la prensa'. They are recorded together with
    each number ('requested to press 27'), which sounds far more natural than
    a number said on its own. At least one word is left for each message."""
    pres = [split_text(cfg["codes"][c][lang])[0].split() for c in sorted(cfg["codes"])]
    k = 0
    while all(len(p) > k + 1 for p in pres) and len({tuple(p[-(k + 1):]) for p in pres}) == 1:
        k += 1
    return pres[0][len(pres[0]) - k:] if k else []


def parts(cfg, code, lang):
    """(message start, shared lead before the number, tail after the number)."""
    pre, post = split_text(cfg["codes"][code][lang])
    lead = shared_lead(cfg, lang)
    words = pre.split()
    return " ".join(words[:len(words) - len(lead)]), " ".join(lead), post


def number_text(lead, press):
    return ("%s %d" % (lead, press)).strip()


def silence(ms):
    return b"\x00\x00" * int(RATE * ms / 1000)


class AudioLibrary:
    def __init__(self, folder):
        self.folder = folder
        self.manifest = {}
        self.cache = {}
        self.load_error = ""
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
            with wave.open(path, "rb") as w:
                if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, 2, 1):
                    raise AudioError("%s is not 8 kHz 16-bit mono" % key)
                data = w.readframes(w.getnframes())
        except (OSError, EOFError, wave.Error) as e:
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
            key = "tones/%s/%s.wav" % (TONE_DIRS[style], name)
            try:
                return self._frames(key) + silence(300), []
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
        head, lead, post = parts(cfg, code, lang)
        keys, expect = [], {}
        if head:
            keys.append("%s/c%d_pre.wav" % (lang, code))
            expect[keys[-1]] = head
        keys.append("%s/n/%d.wav" % (lang, press))
        expect[keys[-1]] = number_text(lead, press)
        if post:
            keys.append("%s/c%d_post.wav" % (lang, code))
            expect[keys[-1]] = post
        for k, t in expect.items():
            if self.clips.get(k) not in (None, t):
                warn.append(k)
        return keys, warn

    def build(self, cfg, code, press):
        """Return (WAV bytes, list of notes: out-of-date clips, missing tone, ...)."""
        if self.load_error:
            raise AudioError(self.load_error)
        opts = cfg.get("audio", {})
        langs = [l for l in ("en", "es") if l in opts.get("languages", ["en", "es"])]
        body, notes = b"", []
        if press > self.max_press:
            notes.append("press %d is above the recorded 1-%d, so the page says press number unknown"
                         % (press, self.max_press))
        for i, lang in enumerate(langs):
            keys, warn = self.pieces(cfg, code, press, lang)
            notes += ["clip %s no longer matches the config wording" % k for k in warn]
            if i:
                body += silence(700)
            for j, k in enumerate(keys):
                if j:
                    body += silence(PIECE_GAP_MS)
                body += self._frames(k)
        lead, tone_notes = self.lead_in(cfg, code)
        return self._wrap(lead, body, opts), notes + tone_notes

    def build_custom(self, cfg, en, es):
        raise AudioError("custom text can't be spoken yet: only the recorded messages exist")

    def _wrap(self, lead, body, opts):
        once = lead + body
        repeat = max(1, int(opts.get("repeat", 2)))
        frames = (once + silence(1200)) * (repeat - 1) + once + silence(300)
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
                        if k not in self.clips:
                            problems.append("missing clip %s" % k)
                    for k in warn:
                        problems.append("clip %s no longer matches the config wording" % k)
            style = tone_style(cfg)
            name = cfg["codes"][code].get("tone", "")
            if style != "off" and name:
                key = "tones/%s/%s.wav" % (TONE_DIRS[style], name)
                if not os.path.exists(os.path.join(self.folder, key)):
                    problems.append("code %d tone file %s is missing (the chime plays instead)" % (code, key))
        return sorted(set(problems))


def tone_style(cfg):
    """'once', 'twice' or 'off'. Older configs had "chime": false for off."""
    opts = cfg.get("audio", {})
    style = opts.get("tones")
    if style not in ("once", "twice", "off"):
        style = "once" if opts.get("chime", True) else "off"
    return style
