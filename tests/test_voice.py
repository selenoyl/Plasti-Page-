# Tests for the Piper page voice (receiver/p833_voice.py) and how pages use it.
# By Ethan Brady & Ryan Raikes

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "receiver"))
sys.path.insert(0, HERE)
import p833_audio as A        # noqa: E402
import p833_receiver as R     # noqa: E402
import p833_voice as V        # noqa: E402
from test_receiver import CONFIG, make_fake_audio   # noqa: E402

FAKE = os.path.join(HERE, "fake_piper.py")
REAL_PY = "/opt/p833-receiver/tts/bin/python"
REAL_VOICES = "/opt/p833-receiver/voices"


def piper_cfg(tmp, **voice):
    cfg = R.load_config(CONFIG)
    voices = os.path.join(tmp, "voices")
    os.makedirs(voices, exist_ok=True)
    cfg["audio"].update(tts_python=sys.executable, tts_script=FAKE, tts_voices=voices)
    vs = {"source": "piper", "en": dict(V.VOICE_DEFAULTS["en"], voice="en_US-test-voice"),
          "es": dict(V.VOICE_DEFAULTS["es"], voice="es_MX-test-voice")}
    vs.update(voice)
    cfg["audio"]["voice"] = vs
    return cfg


def requests_made(tmp):
    try:
        with open(os.path.join(tmp, "voices", "requests.log"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except OSError:
        return []


class Sentences(unittest.TestCase):
    def setUp(self):
        self.cfg = R.load_config(CONFIG)

    def test_digits_on_off_and_pauses(self):
        en = dict(V.VOICE_DEFAULTS["en"])
        self.assertEqual(V.sentence_segments(self.cfg, en, "en", 2, 27),
                         [{"text": "Maintenance requested to press two, seven."}])   # like the recordings
        self.assertEqual(V.sentence_segments(self.cfg, dict(en, digits=False), "en", 2, 27),
                         [{"text": "Maintenance requested to press 27."}])
        self.assertEqual(V.sentence_segments(self.cfg, dict(en, digit_pause_ms=300), "en", 8, 127),
                         [{"text": "Maintenance requested to press one"}, {"pause_ms": 300}, {"text": "two"},
                          {"pause_ms": 300}, {"text": "seven for camera fault."}])
        self.assertEqual(V.sentence_segments(self.cfg, dict(en, digit_pause_ms=300), "en", 2, 5),
                         [{"text": "Maintenance requested to press five."}])
        es = dict(V.VOICE_DEFAULTS["es"])
        self.assertEqual(V.sentence_segments(self.cfg, es, "es", 9, 42),
                         [{"text": "Se solicita mantenimiento en la prensa 42 por falla de Red Rábit."}])
        self.assertEqual(V.sentence_segments(self.cfg, dict(es, digits=True), "es", 2, 27),
                         [{"text": "Se solicita mantenimiento en la prensa dos, siete."}])

    def test_press_zero_and_text_without_press(self):
        en = dict(V.VOICE_DEFAULTS["en"])
        self.assertEqual(V.sentence_segments(self.cfg, en, "en", 2, 0),
                         [{"text": "Maintenance requested. Press number unknown."}])
        cfg = R.load_config(CONFIG)
        cfg["codes"][3]["en"] = "Technician please."
        self.assertEqual(V.sentence_segments(cfg, en, "en", 3, 12), [{"text": "Technician please."}])

    def test_settings_and_form(self):
        cfg = R.load_config(CONFIG)
        self.assertEqual(V.voice_settings(cfg), V.VOICE_DEFAULTS)            # nothing saved: as shipped
        cfg["audio"]["voice"] = {"source": "weird", "en": {"pace": 1.5, "bogus": 1}}
        vs = V.voice_settings(cfg)
        self.assertEqual((vs["source"], vs["en"]["pace"], "bogus" in vs["en"]), ("recorded", 1.5, False))
        form = {"en_voice": "en_US-a", "en_pace": "1.1", "en_digits": "on", "en_digit_pause_ms": "250",
                "es_voice": "es_MX-b", "es_pace": "1.3"}
        vs, errs = V.parse_voice_form(form, ["en_US-a", "es_MX-b"])
        self.assertEqual(errs, [])
        self.assertEqual(vs, {"source": "piper",
                              "en": {"voice": "en_US-a", "pace": 1.1, "digits": True, "digit_pause_ms": 250},
                              "es": {"voice": "es_MX-b", "pace": 1.3, "digits": False, "digit_pause_ms": 0}})
        bad = dict(form, en_voice="../x", en_pace="9", es_digit_pause_ms="900")
        self.assertEqual(len(V.parse_voice_form(bad, ["en_US-a", "es_MX-b"])[1]), 3)
        en_only = {k: v for k, v in form.items() if k.startswith("en_")}            # an English-only site
        vs, errs = V.parse_voice_form(en_only, ["en_US-a"], langs=["en"])
        self.assertEqual((errs, vs["es"]["voice"]), ([], V.VOICE_DEFAULTS["es"]["voice"]))
        self.assertIn("digit by digit with 250 ms", V.describe(vs))
        self.assertEqual(V.describe(V.VOICE_DEFAULTS), "the recorded clips (as shipped)")

    def test_tampered_settings_are_replaced_by_defaults(self):
        cfg = R.load_config(CONFIG)
        cfg["audio"]["voice"] = {"source": "piper", "en": {"voice": "../../x", "pace": "fast", "digits": "yes",
                                                          "digit_pause_ms": 99999}, "es": "nonsense"}
        vs = V.voice_settings(cfg)
        self.assertEqual(vs["en"], V.VOICE_DEFAULTS["en"])
        self.assertEqual(vs["es"], V.VOICE_DEFAULTS["es"])
        self.assertIn("Piper:", V.describe(vs))                          # no crash formatting it
        cfg["audio"]["voice"] = ["not", "a", "dict"]
        self.assertEqual(V.voice_settings(cfg)["source"], "recorded")

    def test_defaults_follow_the_recordings(self):
        cfg = R.load_config(CONFIG)
        vs = V.voice_settings(cfg, {"voices": {"en": "en_US-joe-medium", "es": "es_MX-ald-medium"},
                                    "length_scale": 1.4})
        self.assertEqual((vs["en"]["voice"], vs["es"]["voice"], vs["en"]["pace"]),
                         ("en_US-joe-medium", "es_MX-ald-medium", 1.4))
        cfg["audio"]["speak_digits"] = ["en", "es"]
        self.assertTrue(V.voice_settings(cfg)["es"]["digits"])


class FakeEngine:
    """Counts what would be spoken; returns 10 ms of sound per text segment."""

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def ready(self):
        return True

    def wait_turn(self, timeout=30):
        pass

    def speak(self, voice, pace, segments, timeout, urgent=False):
        self.calls.append((voice, pace, json.dumps(segments), urgent))
        if self.fail:
            raise A.AudioError(self.fail)
        import io
        import wave
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00\x10" * 800 * len([s for s in segments if "text" in s]))
        return out.getvalue()


class KeptAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = piper_cfg(self.tmp)
        self.engine = FakeEngine()
        self.pv = V.PageVoice(self.cfg, self.tmp, engine=self.engine)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_spoken_once_then_kept(self):
        a = self.pv.sentence("en", 2, 27)
        b = self.pv.sentence("en", 2, 27)
        self.assertEqual(a, b)
        self.assertEqual(len(self.engine.calls), 1)                 # the second came from disk
        self.assertTrue(self.engine.calls[0][3])                    # a page asks urgently
        files = [f for _, _, fs in os.walk(self.pv.cache_dir) for f in fs]
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.pv.cache_dir, os.listdir(self.pv.cache_dir)[0], files[0]), "rb") as f:
            self.assertEqual(f.read(22)[20:22], b"\x07\x00")         # stored as mu-law, like the clips
        self.assertEqual(self.pv.stats()[0], 1)

    def test_any_setting_change_speaks_again(self):
        self.pv.sentence("en", 2, 27)
        self.cfg["audio"]["voice"]["en"]["pace"] = 1.4
        self.pv.sentence("en", 2, 27)
        self.cfg["audio"]["voice"]["en"]["digits"] = False
        self.pv.sentence("en", 2, 27)
        self.cfg["audio"]["voice"]["en"]["voice"] = "en_US-other-voice"
        self.pv.sentence("en", 2, 27)
        self.cfg["codes"][2]["en"] = "Maintenance to press {press} now."
        self.pv.sentence("en", 2, 27)
        self.assertEqual(len(self.engine.calls), 5)
        self.pv.sentence("en", 2, 27)
        self.assertEqual(len(self.engine.calls), 5)

    def test_saving_drops_audio_kept_for_other_settings(self):
        self.pv.sentence("en", 2, 27)
        self.cfg["audio"]["voice"]["en"]["pace"] = 1.4
        self.pv.sentence("en", 2, 27)
        self.assertEqual(len(os.listdir(self.pv.cache_dir)), 2)
        self.pv.saved_settings_changed()
        self.assertEqual(len(os.listdir(self.pv.cache_dir)), 1)
        self.pv.clear()
        self.assertFalse(os.path.exists(self.pv.cache_dir))

    def test_cut_short_and_leftover_files(self):
        self.pv.sentence("en", 2, 27)
        d = os.path.join(self.pv.cache_dir, os.listdir(self.pv.cache_dir)[0])
        path = os.path.join(d, os.listdir(d)[0])
        with open(path, "rb") as f:
            whole = f.read()
        with open(path, "wb") as f:
            f.write(whole[:60])                                  # a power cut mid-write
        with open(os.path.join(d, "x.wav.tmp123"), "wb") as f:
            f.write(b"half")
        self.pv.sentence("en", 2, 27)
        self.assertEqual(len(self.engine.calls), 2)               # made again, not played cut short
        V.PageVoice(self.cfg, self.tmp, engine=self.engine)        # the next start removes leftovers
        self.assertFalse(os.path.exists(os.path.join(d, "x.wav.tmp123")))

    def test_size_cap_removes_least_recently_used(self):
        for press in range(1, 6):
            self.pv.sentence("en", 2, press)
            time.sleep(0.02)
        old = V.MAX_CACHE_MB
        V.MAX_CACHE_MB = 3 * 900 / 1e6                            # room for 3 of these
        try:
            self.pv.sentence("en", 2, 1)                          # used again: now the newest
            self.pv._trim()
        finally:
            V.MAX_CACHE_MB = old
        self.assertEqual(self.pv.stats()[0], 3)
        n = len(self.engine.calls)
        self.pv.sentence("en", 2, 1)
        self.assertEqual(len(self.engine.calls), n)               # the recently used one was kept

    def test_digit_settings_get_their_own_folder(self):
        self.pv.sentence("en", 2, 27)
        self.cfg["audio"]["voice"]["en"]["digits"] = False
        self.pv.sentence("en", 2, 27)
        self.assertEqual(len(os.listdir(self.pv.cache_dir)), 2)
        self.pv.saved_settings_changed()
        self.assertEqual(self.pv.stats()[0], 1)                   # only the saved settings' audio counts

    def test_no_sound_is_an_error_not_kept(self):
        class Silent(FakeEngine):
            def speak(self, *a, **k):
                return FakeEngine.speak(self, a[0], a[1], [], a[3])
        pv = V.PageVoice(self.cfg, self.tmp, engine=Silent())
        with self.assertRaises(A.AudioError):
            pv.sentence("en", 2, 27)
        self.assertFalse(os.path.exists(pv.cache_dir) and any(fs for _, _, fs in os.walk(pv.cache_dir)))

    def test_make_all_ahead(self):
        self.pv.sentence("en", 2, 1)                       # one already kept
        self.assertTrue(self.pv.start_making_all(max_press=2))
        for _ in range(200):
            if not self.pv.job["running"]:
                break
            time.sleep(0.02)
        job = self.pv.job
        self.assertEqual(job["total"], 8 * 3 * 2)          # codes 2-9, presses 0-2, both languages
        self.assertEqual((job["done"], job["made"], job["failed"]), (48, 47, 0))
        self.assertTrue(all(not c[3] for c in self.engine.calls[1:]))   # background: not urgent
        self.assertEqual(self.pv.stats()[0], 48)

    def test_make_all_gives_up_when_piper_fails(self):
        pv = V.PageVoice(self.cfg, self.tmp, engine=FakeEngine(fail="broken"))
        pv.start_making_all(max_press=100)
        for _ in range(200):
            if not pv.job["running"]:
                break
            time.sleep(0.02)
        self.assertEqual(pv.job["failed"], 5)               # stops early instead of trying 1,616 times
        self.assertIn("broken", pv.job["error"])

    def test_make_all_waits_for_pages(self):
        busy = {"on": True}
        self.pv.page_busy = lambda: busy["on"]
        self.pv.start_making_all(max_press=0)
        time.sleep(0.3)
        self.assertEqual(self.engine.calls, [])           # a page is going out: nothing yet
        busy["on"] = False
        for _ in range(200):
            if not self.pv.job["running"]:
                break
            time.sleep(0.02)
        self.assertEqual(self.pv.job["made"], 16)


class Pages(unittest.TestCase):
    """Whole pages built with the Piper voice, and the fallback to the recorded clips."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = piper_cfg(self.tmp)
        self.lib = A.AudioLibrary(make_fake_audio(os.path.join(self.tmp, "audio")))
        self.engine = FakeEngine()
        self.lib.voice = V.PageVoice(self.cfg, self.tmp, engine=self.engine)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def frames(self, wav):
        import io
        import wave
        with wave.open(io.BytesIO(wav)) as w:
            return w.getnframes()

    def test_page_spoken_by_piper(self):
        wav, notes = self.lib.build(self.cfg, 2, 27)
        self.assertEqual(notes, [])
        self.assertEqual([json.loads(c[2])[0]["text"] for c in self.engine.calls],
                         ["Maintenance requested to press two, seven.",
                          "Se solicita mantenimiento en la prensa 27."])
        # tone 100 + 400 ms pause, en 800, gap 400 ms, es 800; twice with a 500 ms gap; 300 ms end
        once = 100 + 3200 + 800 + 3200 + 800
        self.assertEqual(self.frames(wav), once * 2 + 4000 + 2400)

    def test_press_above_100_is_spoken(self):
        wav, notes = self.lib.build(self.cfg, 2, 512)
        self.assertEqual(notes, [])
        self.assertIn("five, one, two", self.engine.calls[0][2])

    def test_falls_back_to_the_recorded_clips(self):
        self.engine.fail = "Piper took too long"
        wav, notes = self.lib.build(self.cfg, 2, 27)
        self.assertEqual(len([n for n in notes if "Piper voice failed (Piper took too long)" in n]), 1)
        self.assertEqual(len(self.engine.calls), 1)          # after one failure, the rest of the page doesn't wait
        once = 100 + 3200 + 80 + 3200 + 80                  # the fake recorded clips are 10 ms each
        self.assertEqual(self.frames(wav), once * 2 + 4000 + 2400)

    def test_unexpected_error_still_pages(self):
        class Buggy(FakeEngine):
            def speak(self, *a, **k):
                raise KeyError("a bug")
        self.lib.voice.engine = Buggy()
        wav, notes = self.lib.build(self.cfg, 2, 27)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn("KeyError", notes[0])

    def test_no_recordings_and_one_language_fails(self):
        class EnglishOnly(FakeEngine):
            def speak(self, voice, pace, segments, timeout, urgent=False):
                if voice.startswith("es_"):
                    raise A.AudioError("no Spanish")
                return FakeEngine.speak(self, voice, pace, segments, timeout, urgent)
        lib = A.AudioLibrary(os.path.join(self.tmp, "nothing-here"))
        lib.voice = V.PageVoice(self.cfg, self.tmp, engine=EnglishOnly())
        wav, notes = lib.build(self.cfg, 2, 27)                   # still pages, in English
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn("no es sentence", " ".join(notes))
        lib.voice.engine.fail = "nothing works"
        lib.voice.clear()
        with self.assertRaises(A.AudioError):
            lib.build(self.cfg, 3, 9)

    def test_recorded_source_does_not_use_piper(self):
        self.cfg["audio"]["voice"]["source"] = "recorded"
        self.lib.build(self.cfg, 2, 27)
        self.assertEqual(self.engine.calls, [])

    def test_custom_page_is_spoken(self):
        wav = self.lib.build_custom(self.cfg, "Line [2] is down.", "Lleg\u00f3 la comida.")
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(len(self.engine.calls), 2)
        self.assertEqual(json.loads(self.engine.calls[0][2]), [{"text": "Line [2] is down."}])   # said as written
        self.lib.voice = None
        with self.assertRaises(A.AudioError):
            self.lib.build_custom(self.cfg, "x", "")


class Engine(unittest.TestCase):
    """The real helper protocol, with the fake Piper (tests/fake_piper.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = piper_cfg(self.tmp)
        self.engine = V.PiperEngine(self.cfg)

    def tearDown(self):
        self.engine._stop()
        shutil.rmtree(self.tmp)

    def test_speaks_and_stays_running(self):
        wav = self.engine.speak("en_US-test-voice", 1.25, [{"text": "a"}, {"pause_ms": 100}, {"text": "b"}], 10)
        self.assertEqual(len(wav), 44 + 2 * (80 + 800 + 80))
        pid = self.engine.proc.pid
        self.engine.speak("en_US-test-voice", 1.25, [{"text": "c"}], 10)
        self.assertEqual(self.engine.proc.pid, pid)                  # the same helper
        self.assertEqual(self.engine.started, 1)

    def test_errors_and_restart(self):
        with self.assertRaises(A.AudioError) as c:
            self.engine.speak("nope", 1.25, [{"text": "a"}], 10)
        self.assertIn("voice not installed", str(c.exception))
        self.engine.speak("en_US-test-voice", 1.25, [{"text": "fine"}], 10)
        with self.assertRaises(A.AudioError):
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "DIE"}], 10)
        wav = self.engine.speak("en_US-test-voice", 1.25, [{"text": "back again"}], 10)   # restarted
        self.assertTrue(wav.startswith(b"RIFF"))

    def test_too_slow_is_cut_off(self):
        t0 = time.monotonic()
        with self.assertRaises(A.AudioError) as c:
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "SLOW"}], 1)
        self.assertIn("too long", str(c.exception))
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assertIsNone(self.engine.proc)                           # the stuck helper was stopped
        self.engine.speak("en_US-test-voice", 1.25, [{"text": "ok"}], 10)

    def test_a_page_goes_before_background_work(self):
        self.engine.speak("en_US-test-voice", 1.25, [{"text": "warm"}], 10)
        order = []

        def background():
            time.sleep(0.2)
            self.engine.wait_turn()
            order.append("background")

        def page():
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "SLOW page"}], 10, urgent=True)
            order.append("page")
        threads = [threading.Thread(target=page), threading.Thread(target=background)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(order, ["page", "background"])

    def test_waiting_for_a_turn_does_not_kill_a_healthy_helper(self):
        self.engine.speak("en_US-test-voice", 1.25, [{"text": "warm"}], 10)
        pid = self.engine.proc.pid
        lab = threading.Thread(target=self.engine.speak,
                               args=("en_US-test-voice", 1.25, [{"text": "SLOW lab text"}], 10))
        lab.start()
        time.sleep(0.3)
        t0 = time.monotonic()
        with self.assertRaises(A.AudioError) as c:          # the page gives up waiting after 3 s...
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "page"}], 10, urgent=True)
        self.assertIn("busy", str(c.exception))
        self.assertLess(time.monotonic() - t0, V.PAGE_LOCK_WAIT_S + 0.5)
        lab.join(10)
        self.assertEqual(self.engine.proc.pid, pid)            # ...but the helper wasn't killed
        self.assertFalse(self.engine.cooling())

    def test_a_hang_on_a_page_cools_off_then_recovers(self):
        self.engine.speak("en_US-test-voice", 1.25, [{"text": "warm"}], 10)
        recovered = threading.Event()
        self.engine.on_trouble = lambda: (self.engine.speak("en_US-test-voice", 1.25, [{"text": "reload"}], 10),
                                          recovered.set())
        with self.assertRaises(A.AudioError):
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "SLOW page"}], 4, urgent=True)
        self.assertTrue(recovered.wait(10))                    # reloaded in the background...
        self.assertFalse(self.engine.cooling())                 # ...so pages may use Piper again
        old = V.COOL_OFF_S
        try:
            self.engine.on_trouble = lambda: None
            V.COOL_OFF_S = 60
            with self.assertRaises(A.AudioError):
                self.engine.speak("en_US-test-voice", 1.25, [{"text": "SLOW page"}], 4, urgent=True)
            t0 = time.monotonic()
            with self.assertRaises(A.AudioError) as c:            # while cooling off: no wait at all
                self.engine.speak("en_US-test-voice", 1.25, [{"text": "next page"}], 10, urgent=True)
            self.assertLess(time.monotonic() - t0, 0.2)
            self.assertIn("recovering", str(c.exception))
        finally:
            V.COOL_OFF_S = old

    def test_page_budget_whole_page(self):
        cfg = piper_cfg(self.tmp)
        cfg["codes"][2]["en"] = "SLOW maintenance to press {press}."
        lib = A.AudioLibrary(make_fake_audio(os.path.join(self.tmp, "audio")))
        lib.voice = V.PageVoice(cfg, self.tmp)
        lib.voice.page_budget = 4
        lib.voice.engine.on_trouble = lambda: None
        t0 = time.monotonic()
        wav, notes = lib.build(cfg, 2, 27)
        took = time.monotonic() - t0
        lib.voice.engine._stop()
        self.assertLess(took, 5)                                # within the page's budget...
        self.assertEqual(len([n for n in notes if "recorded clip" in n]), 1)   # ...from the recordings
        self.assertIn("took too long", notes[0])
        self.assertTrue(wav.startswith(b"RIFF"))

    def test_helper_stops_when_idle_unless_in_use(self):
        old = V.IDLE_EXIT_S
        V.IDLE_EXIT_S = 0
        try:
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "a"}], 10)
            self.engine.keep_running = lambda: True
            self.engine._idle_check()
            self.assertIsNotNone(self.engine.proc)               # the Piper voice is in use: stays
            self.engine.keep_running = lambda: False
            self.engine._idle_check()
            self.assertIsNone(self.engine.proc)                  # not in use: stopped
        finally:
            V.IDLE_EXIT_S = old

    def test_not_installed(self):
        self.cfg["audio"]["tts_python"] = os.path.join(self.tmp, "nothing")
        with self.assertRaises(A.AudioError) as c:
            self.engine.speak("en_US-test-voice", 1.25, [{"text": "a"}], 5)
        self.assertIn("isn't installed", str(c.exception))


@unittest.skipUnless(os.path.exists(REAL_PY) and os.path.exists(os.path.join(REAL_VOICES, "en_US-joe-medium.onnx")),
                     "real Piper not installed here")
class RealPiper(unittest.TestCase):
    """With Piper installed (the laptop after install_tts.sh): real speech, timed."""

    def test_real_voice(self):
        tmp = tempfile.mkdtemp()
        try:
            cfg = R.load_config(CONFIG)
            cfg["audio"].update(tts_python=REAL_PY, tts_script=os.path.join(ROOT, "receiver", "p833_tts.py"),
                                tts_voices=REAL_VOICES)
            cfg["audio"]["voice"] = {"source": "piper", "en": dict(V.VOICE_DEFAULTS["en"], voice="en_US-joe-medium")}
            pv = V.PageVoice(cfg, tmp)
            t0 = time.monotonic()
            first = pv.sentence("en", 2, 27, timeout=60)
            t1 = time.monotonic()
            pv.sentence("en", 2, 28, timeout=60)
            t2 = time.monotonic()
            pv.sentence("en", 2, 27)
            t3 = time.monotonic()
            self.assertGreater(len(first) / 2 / 8000.0, 1.5)              # over 1.5 s of speech
            self.assertLess(t2 - t1, 5)                                    # loaded: a fraction of a second here
            self.assertLess(t3 - t2, 0.5)                                  # kept: from disk
            sys.stderr.write("\n  real Piper: first sentence %.2f s (voice load), next %.2f s, kept %.3f s\n"
                             % (t1 - t0, t2 - t1, t3 - t2))
            pv.engine._stop()
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
