# Tests for PA clarity: shaping the page speech for paging horns (receiver/p833_audio.py).
# By Ethan Brady & Ryan Raikes

import math
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "receiver"))
sys.path.insert(0, HERE)
import p833_audio as A        # noqa: E402
import p833_receiver as R     # noqa: E402
from test_receiver import CONFIG, make_fake_audio   # noqa: E402

ON = dict(A.CLARITY_DEFAULTS, pa_process=True)


def tone(freq, seconds, level=0.5):
    return A._pcm([level * math.sin(2 * math.pi * freq * i / 8000.0) for i in range(int(8000 * seconds))])


def db(pcm, skip=0.1):
    x = A._floats(pcm)[int(8000 * skip):]          # past the filter's start-up
    return 20 * math.log10(math.sqrt(sum(v * v for v in x) / len(x)))


def peak_db(pcm):
    return 20 * math.log10(max(abs(v) for v in A._floats(pcm)))


class Shaping(unittest.TestCase):
    def test_bass_cut(self):
        low, mid = tone(100, 1), tone(1000, 1)
        cut = db(A._pcm(A._highpass(A._floats(low), 300))) - db(low)
        kept = db(A._pcm(A._highpass(A._floats(mid), 300))) - db(mid)
        self.assertLess(cut, -15)                  # 100 Hz: mud, mostly gone
        self.assertGreater(kept, -1)               # 1 kHz: kept

    def test_clarity_boost(self):
        lift = db(A._pcm(A._presence(A._floats(tone(2500, 1)), 4))) - db(tone(2500, 1))
        low = db(A._pcm(A._presence(A._floats(tone(500, 1)), 4))) - db(tone(500, 1))
        self.assertAlmostEqual(lift, 4, delta=0.5)
        self.assertLess(abs(low), 1)

    def test_peaks_held_at_the_output_level(self):
        loud = tone(1000, 0.5, 0.95) + tone(1000, 0.5, 0.05)
        for level in (-3, -6, -12):
            out = A.pa_process(loud, dict(ON, pa_level_db=level))
            self.assertLessEqual(peak_db(out), level + 0.05)
            self.assertGreater(peak_db(out), level - 1)

    def test_loud_and_quiet_syllables_brought_closer(self):
        quiet, loud = tone(1000, 0.4, 0.03), tone(1000, 0.4, 0.6)            # 26 dB apart
        out = A.pa_process(loud + quiet, dict(ON, pa_highpass_hz=0, pa_presence_db=0, pa_compress="strong"))
        half = len(out) // 2
        gap = db(out[:half]) - db(out[half:])
        self.assertLess(gap, 20)                    # the quiet part comes up

    def test_fast_enough(self):
        speech = A.read_wav(os.path.join(ROOT, "audio", "es", "s", "c8", "63.wav"))
        t0 = time.time()
        A.pa_process(speech, dict(ON, pa_level_db=-4))
        self.assertLess(time.time() - t0, 1.0)

    def test_settings_checked(self):
        c = A.clarity_of({"pa_process": "yes", "pa_highpass_hz": 5000, "pa_presence_db": "x",
                          "pa_compress": "max", "pa_level_db": 6})
        self.assertEqual(c, A.CLARITY_DEFAULTS)     # anything odd: the default (and off)


class Keypad(unittest.TestCase):
    """Tones must not look like a keypad press (DTMF) to the ATA2: the packaging pluck did."""

    def key(self, low, high, seconds=0.5):
        return A._pcm([0.3 * math.sin(2 * math.pi * low * i / 8000.0) + 0.3 * math.sin(2 * math.pi * high * i / 8000.0)
                       for i in range(int(8000 * seconds))])

    def test_a_key_press_is_spotted_and_broken_up(self):
        four = self.key(770, 1209)
        self.assertGreater(A.dtmf_risk(four), 0.4)                              # held the whole 0.5 s
        self.assertEqual(A.dtmf_check(four)[1][:1], [1209])
        self.assertGreater(A.dtmf_risk(self.key(770 * 1.025, 1209 * 0.98)), 0.4)     # 2-3% off still counts
        self.assertLess(A.dtmf_risk(A.dtmf_safe(four)), A.DTMF_HOLD_S)
        self.assertLess(A.dtmf_risk(self.key(770, 1209, 0.03)), A.DTMF_HOLD_S)   # 30 ms is too short to count

    def test_safe_tones_are_left_alone(self):
        note = tone(1000, 0.5)
        self.assertEqual(A.dtmf_risk(note), 0)
        self.assertEqual(A.dtmf_safe(note), note)
        self.assertEqual(A.dtmf_risk(self.key(600, 1209)), 0)                     # 600 Hz is no key

    def test_real_tones(self):
        lib = A.AudioLibrary(os.path.join(ROOT, "audio"))
        pluck = lib._frames("tones/single/5_packaging_pluck.wav")
        held, freqs = A.dtmf_check(pluck)
        self.assertGreater(held, 0.2)                                # the culprit, as shipped: a "4"
        self.assertTrue(any(1170 < f < 1250 for f in freqs), freqs)  # its "4" (1188 Hz near 1209)
        for name in lib.tone_names() + ["chime"]:
            self.assertLess(A.dtmf_risk(lib.tone(name)), A.DTMF_HOLD_S, name)
            if name != "5_packaging_pluck":                          # the others are left exactly as they are
                self.assertEqual(lib.tone(name), lib._frames("tones/single/%s.wav" % name if name != "chime"
                                                             else "chime.wav"), name)
        self.assertGreater(db(lib.tone("5_packaging_pluck"), 0) - db(pluck, 0), -3)   # still sounds as loud

    def test_pages_use_the_safe_tone(self):
        lib = A.AudioLibrary(os.path.join(ROOT, "audio"))
        cfg = R.load_config(CONFIG)
        cfg["audio"]["languages"] = ["en"]
        wav, _ = lib.build(cfg, 6, 27)                               # packaging
        import io
        import wave
        with wave.open(io.BytesIO(wav)) as w:
            pcm = w.readframes(w.getnframes())
        self.assertLess(A.dtmf_risk(pcm[:2 * 8000 * 2]), A.DTMF_HOLD_S)   # the tone at the start


class Pages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lib = A.AudioLibrary(make_fake_audio(os.path.join(self.tmp, "audio")))
        self.cfg = R.load_config(CONFIG)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_page_volume_not_used_with_clarity(self):
        cfg = dict(self.cfg, audio=dict(self.cfg["audio"], gain_db=12, **ON))
        wav, _ = self.lib.build(cfg, 2, 27)
        import io
        import wave
        with wave.open(io.BytesIO(wav)) as w:
            pcm = w.readframes(w.getnframes())
        self.assertLessEqual(peak_db(pcm), -3 + 0.05)       # +12 dB would have hit the top
        cfg["audio"]["pa_process"] = False
        wav2, _ = self.lib.build(cfg, 2, 27)
        self.assertNotEqual(wav, wav2)


if __name__ == "__main__":
    unittest.main()
