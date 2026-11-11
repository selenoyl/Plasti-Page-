# Tests for the P833 receiver. Run:  python3 -m unittest discover -s tests -v
# By Ethan Brady & Ryan Raikes

import base64
import csv
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "receiver"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "install"))
sys.path.insert(0, HERE)
import p833_receiver as R          # noqa: E402
import p833_test_sender as S       # noqa: E402
import p833_audio as A             # noqa: E402
import make_audio as M             # noqa: E402  (stdlib part only; piper not needed)


def make_fake_audio(folder, cfg_path=None, drop=(), change=None):
    """A fake clip library: each clip is a short beep whose length encodes nothing in
    particular; enough to test joining, checks and missing clips."""
    import wave as _w
    with open(cfg_path or CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    plan = M.clip_plan(cfg)
    clips = {}
    for key, (lang, text) in plan.items():
        clips[key] = text
        if key in drop:
            continue
        path = os.path.join(folder, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _w.open(path, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"\x10\x00" * 80)          # 10 ms
    M.make_chime(os.path.join(folder, "chime.wav"))
    for code in cfg["codes"].values():            # fake department tones: 100 / 200 frames
        for sub, n in (("single", 100), ("twice", 200)):
            path = os.path.join(folder, "tones", sub, code["tone"] + ".wav")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with _w.open(path, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
                w.writeframes(b"\x20\x00" * n)
    if change:
        clips.update(change)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"rate": 8000, "voices": {"en": "fake-en", "es": "fake-es"}, "clips": clips}, f)
    return folder


class Audio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.lib = A.AudioLibrary(make_fake_audio(cls.tmp))
        cls.cfg = R.load_config(CONFIG)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def frames(self, wav):
        import io, wave as _w
        with _w.open(io.BytesIO(wav)) as w:
            self.assertEqual((w.getframerate(), w.getsampwidth(), w.getnchannels()), (8000, 2, 1))
            return w.getnframes()

    def test_split_matches_make_audio(self):
        for code in range(2, 10):
            for lang in ("en", "es"):
                t = self.cfg["codes"][code][lang]
                self.assertEqual(A.split_text(t), M.split_text(t))
        self.assertEqual(A.split_text("Maintenance requested to press {press}."),
                         ("Maintenance requested to press", ""))
        self.assertEqual(A.split_text("Maintenance requested to press {press} for camera fault."),
                         ("Maintenance requested to press", "for camera fault."))

    def test_split_and_plan(self):
        self.assertEqual(A.parts(self.cfg, 8, "en"), ("Maintenance requested to press number", "for camera fault."))
        self.assertEqual(A.parts(self.cfg, 4, "es"), ("Se solicita un supervisor en la prensa número", ""))
        self.assertEqual(A.slug("Se solicita un técnico en la prensa número"), "se_solicita_un_tecnico_en_la_prensa_numero")
        plan = M.clip_plan(json.load(open(CONFIG, encoding="utf-8")))
        self.assertEqual(plan["en/lead/maintenance_requested_to_press_number.wav"],
                         ("en", "Maintenance requested to press number"))
        self.assertEqual(plan["es/lead/se_solicita_un_tecnico_en_la_prensa_numero.wav"],
                         ("es", "Se solicita un técnico en la prensa número"))
        self.assertEqual(plan["en/n/27.wav"], ("en", "27"))
        self.assertEqual(plan["en/c8_post.wav"], ("en", "for camera fault."))
        # 5 department phrases + 100 numbers per language, + 6 reasons + 16 press-unknown messages
        self.assertEqual(len(plan), 232)
        self.assertEqual(len(M.clip_plan(json.load(open(CONFIG, encoding="utf-8")), 10)), 52)

    def test_pieces(self):
        keys, warn = self.lib.pieces(self.cfg, 8, 27, "en")
        self.assertEqual(keys, ["en/lead/maintenance_requested_to_press_number.wav", "en/n/27.wav", "en/c8_post.wav"])
        self.assertEqual(self.lib.pieces(self.cfg, 2, 27, "es")[0],
                         ["es/lead/se_solicita_mantenimiento_en_la_prensa_numero.wav", "es/n/27.wav"])
        self.assertEqual(self.lib.pieces(self.cfg, 2, 0, "en")[0], ["en/c2_nopress.wav"])
        self.assertEqual(warn, [])

    def test_wording_press_number(self):
        self.assertEqual(R.build_message(self.cfg, 2, 27),
                         ("Maintenance requested to press number 27.", "Se solicita mantenimiento en la prensa número 27."))
        self.assertEqual(R.build_message(self.cfg, 2, 0)[0], "Maintenance requested. Press number unknown.")

    def test_build_lengths(self):
        cfg = json.loads(json.dumps(self.cfg, default=str))
        cfg["codes"] = self.cfg["codes"]
        ngap, pgap = 8 * A.NUMBER_GAP_MS, 8 * A.PIECE_GAP_MS
        lead = 8 * self.cfg["audio"]["lead_silence_ms"]
        langgap, repgap = 8 * 400, 8 * 500           # default pauses: English->Spanish, before the repeat
        # tone 100 frames + 0.3 s; per language: lead, number gap, number, piece gap, reason
        lang = 80 + ngap + 80 + pgap + 80
        once = 100 + 2400 + lang + langgap + lang
        cfg["audio"] = dict(self.cfg["audio"], repeat=1)
        self.assertEqual(self.frames(self.lib.build(cfg, 8, 27)[0]), lead + once + 2400)
        cfg["audio"] = dict(self.cfg["audio"], repeat=2)
        self.assertEqual(self.frames(self.lib.build(cfg, 8, 27)[0]), lead + once + repgap + once + 2400)
        # the pauses follow the settings
        cfg["audio"] = dict(self.cfg["audio"], repeat=2, lang_gap_ms=200, repeat_gap_ms=300)
        once2 = 100 + 2400 + lang + 1600 + lang
        self.assertEqual(self.frames(self.lib.build(cfg, 8, 27)[0]), lead + once2 + 2400 + once2 + 2400)
        cfg["audio"] = dict(self.cfg["audio"], repeat=1, lead_silence_ms=0, tones="off", languages=["en"])
        self.assertEqual(self.frames(self.lib.build(cfg, 2, 0)[0]), 80 + 2400)
        self.assertEqual(self.frames(self.lib.build(cfg, 2, 5)[0]), 80 + ngap + 80 + 2400)
        # twice = the single tone (100 frames) twice with the tone gap (250 ms) between
        cfg["audio"] = dict(self.cfg["audio"], repeat=1, lead_silence_ms=3000, tones="twice", languages=["en"])
        self.assertEqual(self.frames(self.lib.build(cfg, 2, 0)[0]), 24000 + 100 + 2000 + 100 + 2400 + 80 + 2400)
        # older config: "chime": false means no tone; no lead silence set = 2.5 s
        cfg["audio"] = {"repeat": 1, "chime": False, "languages": ["en"]}
        self.assertEqual(self.frames(self.lib.build(cfg, 2, 0)[0]), lead + 80 + 2400)

    def test_volume_gain_and_limiter(self):
        import io, math, wave as _w
        from array import array
        self.assertEqual(A.apply_gain(b"\x10\x00\xf0\xff", 0), b"\x10\x00\xf0\xff")   # 0 dB = untouched
        # a loud sine: +12 dB must get louder on average but never clip past full scale
        loud = array("h", [int(30000 * math.sin(i / 3.0)) for i in range(8000)]).tobytes()
        quiet = array("h", [int(3000 * math.sin(i / 3.0)) for i in range(8000)]).tobytes()
        def rms(b):
            a = array("h"); a.frombytes(b)
            return math.sqrt(sum(x * x for x in a) / len(a))
        up = A.apply_gain(loud, 12)
        a = array("h"); a.frombytes(up)
        self.assertLessEqual(max(abs(x) for x in a), 32767)
        self.assertGreater(rms(up), rms(loud))
        # quiet audio below the limiter's knee gets the plain gain: +6 dB = about x2
        self.assertAlmostEqual(rms(A.apply_gain(quiet, 6)) / rms(quiet), 10 ** (6 / 20.0), delta=0.02)
        self.assertLess(rms(A.apply_gain(quiet, -6)), rms(quiet))
        # the page itself follows the setting
        cfg = dict(self.cfg, audio=dict(self.cfg["audio"], gain_db=0, lead_silence_ms=0))
        w0, _ = self.lib.build(cfg, 2, 27)
        cfg["audio"]["gain_db"] = 6
        w6, _ = self.lib.build(cfg, 2, 27)
        def body(w):
            with _w.open(io.BytesIO(w)) as f:
                return f.readframes(f.getnframes())
        self.assertEqual(len(w0), len(w6))
        self.assertGreater(rms(body(w6)), rms(body(w0)))

    def test_lead_silence_is_silent(self):
        import io, wave as _w
        wav, _ = self.lib.build(self.cfg, 2, 5)
        with _w.open(io.BytesIO(wav)) as w:
            start = w.readframes(2500 * 8)
        self.assertEqual(set(start), {0})

    def test_tones(self):
        self.assertEqual(self.lib.tone_names(), ["1_maintenance_bell", "2_technician_wood",
                                                 "3_supervisor_glide", "4_quality_flute", "5_packaging_pluck"])
        self.assertEqual(self.cfg["codes"][8]["tone"], "1_maintenance_bell")
        self.assertEqual(self.cfg["codes"][7]["tone"], "3_supervisor_glide")
        # a missing tone falls back to the chime, with a note and a check problem
        cfg = dict(self.cfg, codes={c: dict(v) for c, v in self.cfg["codes"].items()})
        cfg["codes"][5]["tone"] = "nope"
        wav, notes = self.lib.build(cfg, 5, 3)
        self.assertTrue(any("tone" in n and "chime used" in n for n in notes), notes)
        self.assertTrue(any("code 5 tone" in p for p in self.lib.check(cfg)))
        cfg["codes"][5]["tone"] = ""           # no tone set: chime, no complaint
        self.assertEqual(self.lib.build(cfg, 5, 3)[1], [])

    def test_press_above_recorded_range(self):
        tmp = tempfile.mkdtemp()
        try:
            lib = A.AudioLibrary(make_fake_audio(tmp))
            lib.manifest["max_press"] = 100
            wav, notes = lib.build(self.cfg, 2, 345)
            self.assertTrue(any("above the recorded 1-100" in n for n in notes), notes)
            self.assertEqual(lib.pieces(self.cfg, 2, 345, "en")[0], ["en/c2_nopress.wav"])
            self.assertEqual(lib.build(self.cfg, 2, 100)[1], [])
        finally:
            shutil.rmtree(tmp)

    def test_check_ok_and_stale(self):
        self.assertEqual(self.lib.check(self.cfg), [])
        tmp = tempfile.mkdtemp()
        try:
            lib = A.AudioLibrary(make_fake_audio(tmp, drop=("es/n/1.wav",),
                                                 change={"en/lead/maintenance_requested_to_press_number.wav":
                                                         "Maintenance needed at press"}))
            probs = lib.check(self.cfg)
            self.assertTrue(any("missing clip es/n/1.wav" in p for p in probs), probs)
            wav, notes = lib.build(self.cfg, 2, 5)
            self.assertEqual(notes, ["clip en/lead/maintenance_requested_to_press_number.wav no longer matches the config wording"])
            with self.assertRaises(A.AudioError):
                lib.build(self.cfg, 2, 1)          # the Spanish press-1 clip is missing
        finally:
            shutil.rmtree(tmp)

    def test_no_library(self):
        lib = A.AudioLibrary("/nonexistent/p833")
        self.assertTrue(lib.check(self.cfg))
        with self.assertRaises(A.AudioError):
            lib.build(self.cfg, 2, 5)


CONFIG = os.path.join(ROOT, "receiver", "config.example.json")
KEY = "test-key-123"
ADMIN_PW = "admin-pw-456"


class Messages(unittest.TestCase):
    def setUp(self):
        self.cfg = R.load_config(CONFIG)

    def test_all_codes_have_text(self):
        for code in range(2, 10):
            en, es = R.build_message(self.cfg, code, 27)
            self.assertIn("27", en)
            self.assertIn("27", es)

    def test_press_zero_says_unknown(self):
        for code in range(2, 10):
            en, es = R.build_message(self.cfg, code, 0)
            self.assertIn("unknown", en)
            self.assertIn("desconocida", es)
            self.assertNotIn(" 0", en)

    def test_wording_requested_to(self):
        self.assertEqual(R.build_message(self.cfg, 2, 27)[0], "Maintenance requested to press number 27.")
        self.assertEqual(R.build_message(self.cfg, 7, 5)[0],
                         "Supervisor requested to press number 5 for audit check-in.")
        self.assertEqual(R.build_message(self.cfg, 8, 5)[0],
                         "Maintenance requested to press number 5 for camera fault.")
        self.assertEqual(R.build_message(self.cfg, 9, 5)[0],
                         "Maintenance requested to press number 5 for Red Rabbit failure.")

    def test_check_call(self):
        ok = {"board": "RSP_LH", "seq": 1, "code": 2, "press": 27}
        self.assertIsNone(R.check_call(ok)[1])
        for bad in ({**ok, "code": 1}, {**ok, "code": 10}, {**ok, "press": -1},
                    {**ok, "press": 1000}, {**ok, "code": "2"}, {**ok, "code": True},
                    {**ok, "board": ""}, {k: v for k, v in ok.items() if k != "seq"}, []):
            self.assertIsNotNone(R.check_call(bad)[1], bad)

    def test_pulses_match_spec(self):
        # Brief: Maintenance (code 2) at press 27 = 1500, 500, 700, 1200 ms.
        self.assertEqual(S.pulses(2, 27), [1500, 500, 700, 1200])


class ConfigUpdate(unittest.TestCase):
    """install/migrate_config.py brings an old installed config up to date."""

    def test_old_wording_replaced_own_edits_kept(self):
        import subprocess
        tmp = tempfile.mkdtemp()
        try:
            with open(CONFIG, encoding="utf-8") as f:
                cfg = json.load(f)
            # Make it look like a 0.1.0 install with one hand edit.
            cfg["codes"]["2"]["en"] = "Maintenance needed at press {press}."
            cfg["codes"]["8"]["es"] = "Se requiere mantenimiento en la prensa {press} por falla de cámara."
            cfg["codes"]["3"]["en"] = "Tech to press {press} please."
            cfg["codes"]["4"]["es"] = "Se solicita un supervisor en la prensa {press}."   # 0.4.1 wording
            del cfg["codes"]["7"]["why"]
            cfg["zones"]["floor"]["dial"] = "##61"
            cfg["audio"]["keep_dry_run_pages"] = 20           # the old default
            del cfg["audio"]["lead_silence_ms"]               # a setting the laptop doesn't have yet
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            script = os.path.join(ROOT, "install", "migrate_config.py")
            subprocess.check_output([sys.executable, script, path, CONFIG])
            new = R.load_config(path)
            self.assertEqual(new["codes"][2]["en"], "Maintenance requested to press number {press}.")
            self.assertEqual(new["codes"][8]["es"], "Se solicita mantenimiento en la prensa número {press} por falla de cámara.")
            self.assertEqual(new["codes"][3]["en"], "Tech to press {press} please.")
            self.assertEqual(new["codes"][4]["es"], "Se solicita un supervisor en la prensa número {press}.")
            self.assertEqual(new["codes"][7]["why"], "audit check-in")
            self.assertEqual(new["zones"]["floor"]["dial"], "##61")
            self.assertEqual(new["audio"]["keep_dry_run_pages"], 0)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["audio"]["lead_silence_ms"], 2500)
            out = subprocess.check_output([sys.executable, script, path, CONFIG]).decode()
            self.assertIn("already up to date", out)
        finally:
            shutil.rmtree(tmp)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = R.load_config(CONFIG)
        cfg["page_retry_wait_s"] = 0
        cfg["board_silent_after_s"] = 1
        self.cfg = cfg
        self.state = R.State(cfg, self.tmp, self.tmp)
        self.pager = FakePager()
        self.state.dry_run = False          # route pages to the fake pager
        self.audio_dir = make_fake_audio(os.path.join(self.tmp, "audio"))
        self.worker = R.PageWorker(cfg, self.state, self.pager,
                                   dry_pager=R.DryRunPager(os.path.join(self.tmp, "last_pages"), cfg),
                                   audio=A.AudioLibrary(self.audio_dir))
        self.worker.start()
        R.Handler.app = {"cfg": cfg, "state": self.state, "worker": self.worker, "key": KEY,
                         "admin_password": ADMIN_PW, "state_dir": self.tmp}
        self.srv = R.Server(("127.0.0.1", 0), R.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp)

    def post(self, path, body, key=KEY):
        return S.post("http://127.0.0.1:%d%s" % (self.port, path), body, key)

    def get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.read().decode()

    def wait_pages(self, n=None):
        """Wait until every queued page has been sent and logged."""
        self.worker.jobs.join()

    def wait_row(self):
        """Wait until the call log has a row (the file can exist a moment before its row)."""
        for _ in range(200):
            try:
                rows = self.log_rows()
            except OSError:
                rows = []
            if rows:
                return rows[-1]
            time.sleep(0.02)
        self.fail("no row in the call log")

    def log_rows(self):
        with open(os.path.join(self.tmp, "calls.csv"), newline="") as f:
            return list(csv.DictReader(f))

    def test_call_is_paged_and_logged(self):
        st, txt = self.post("/call", {"board": "RSP_LH", "boot": "b1", "seq": 5, "code": 2, "press": 27})
        self.assertEqual(st, 200, txt)
        self.wait_pages(1)
        self.assertEqual(self.pager.sent, [("floor", "F,*60,2,1", "Maintenance requested to press number 27.",
                                            "Se solicita mantenimiento en la prensa número 27.")])
        row = self.log_rows()[-1]
        self.assertEqual((row["cell"], row["press"], row["code"], row["result"]),
                         ("RSP LH", "27", "2", "sent"))

    def test_resend_is_dropped(self):
        body = {"board": "RSP_LH", "boot": "b1", "seq": 6, "code": 3, "press": 12}
        for _ in range(3):
            st, txt = self.post("/call", body)
            self.assertEqual(st, 200)
        self.wait_pages(1)
        time.sleep(0.2)
        self.assertEqual(len(self.pager.sent), 1)
        results = [r["result"] for r in self.log_rows()]
        self.assertEqual(results.count("repeat dropped"), 2)

    def test_same_call_after_board_reboot_is_dropped(self):
        self.post("/call", {"board": "RSP_LH", "boot": "b1", "seq": 1, "code": 8, "press": 4})
        st, txt = self.post("/call", {"board": "RSP_LH", "boot": "b2", "seq": 1, "code": 8, "press": 4})
        self.assertIn('"repeat": true', txt)

    def test_different_calls_both_page(self):
        self.post("/call", {"board": "RSP_LH", "boot": "b1", "seq": 1, "code": 9, "press": 4})
        self.post("/call", {"board": "RSP_RH", "boot": "b1", "seq": 1, "code": 9, "press": 4})
        self.post("/call", {"board": "RSP_LH", "boot": "b1", "seq": 2, "code": 7, "press": 4})
        self.wait_pages(3)
        self.assertEqual(len(self.pager.sent), 3)

    def test_wrong_key_rejected(self):
        st, _ = self.post("/call", {"board": "RSP_LH", "seq": 1, "code": 2, "press": 1}, key="nope")
        self.assertEqual(st, 403)
        self.assertEqual(self.pager.sent, [])

    def test_bad_calls_rejected(self):
        st, _ = self.post("/call", {"board": "RSP_LH", "seq": 1, "code": 11, "press": 1})
        self.assertEqual(st, 400)
        st, _ = self.post("/nope", {})
        self.assertEqual(st, 404)

    def test_failed_page_is_retried_and_logged(self):
        self.pager.fail = 99
        self.post("/call", {"board": "RSP_LH", "boot": "b1", "seq": 9, "code": 5, "press": 3})
        row = self.wait_row()
        self.assertEqual((row["result"], row["attempts"]), ("FAILED", "3"))

    def test_heartbeat_and_status(self):
        st, _ = self.post("/heartbeat", {"board": "RSP_RH", "fw": "1.0", "rssi": -60})
        self.assertEqual(st, 200)
        s = json.loads(self.get("/status.json"))
        rh = [b for b in s["boards"] if b["board"] == "RSP_RH"][0]
        self.assertTrue(rh["ok"])
        self.assertEqual(rh["cell"], "RSP RH")
        never = [b for b in s["boards"] if b["board"] == "FSP_LH"][0]
        self.assertEqual(never["last_heard"], "never")
        self.assertIn("P833 Call Receiver", self.get("/"))

    def test_board_goes_silent(self):
        self.post("/heartbeat", {"board": "RSP_RH"})
        time.sleep(1.2)
        with self.assertLogs("p833", "WARNING"):
            self.state.check_silent()
        s = json.loads(self.get("/status.json"))
        self.assertFalse([b for b in s["boards"] if b["board"] == "RSP_RH"][0]["ok"])

    def test_restart_keeps_boards_and_log(self):
        self.post("/heartbeat", {"board": "RSP_LH"})
        self.post("/call", {"board": "RSP_LH", "boot": "b1", "seq": 1, "code": 2, "press": 1})
        self.wait_pages(1)
        again = R.State(self.cfg, self.tmp, self.tmp)
        self.assertIn("RSP_LH", again.heard_info)
        self.assertEqual(len(again.calls), 1)

    def test_html_is_escaped(self):
        self.post("/heartbeat", {"board": "<b>x</b>"})
        self.assertNotIn("<b>x</b>", self.get("/"))


    # ---- admin page
    def admin(self, path, form=None, pw=ADMIN_PW, origin=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(url, data=data)
        if pw is not None:
            req.add_header("Authorization", "Basic " + base64.b64encode(("admin:" + pw).encode()).decode())
        if origin:
            req.add_header("Origin", origin)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req) as r:
                return r.status, r.headers.get("Location", ""), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location", ""), e.read().decode()

    def test_admin_needs_password(self):
        self.assertEqual(self.admin("/admin", pw=None)[0], 401)
        self.assertEqual(self.admin("/admin", pw="wrong")[0], 401)
        self.assertEqual(self.admin("/admin/board/add", {"board": "X1", "cell": "X"}, pw=None)[0], 401)
        self.assertNotIn("X1", self.state.registry)
        st, _, body = self.admin("/admin")
        self.assertEqual(st, 200)
        self.assertIn("Send a test call", body)
        self.assertNotIn("Send a test call", self.get("/"))

    def test_admin_refuses_other_site(self):
        st, _, _ = self.admin("/admin/board/add", {"board": "X1", "cell": "X"}, origin="http://evil.example")
        self.assertEqual(st, 403)
        self.assertNotIn("X1", self.state.registry)

    def test_add_rename_remove_board(self):
        st, loc, _ = self.admin("/admin/board/add", {"board": "PRESS_40", "cell": "Press 40 cell"})
        self.assertEqual(st, 303)
        self.assertIn("added", urllib.parse.unquote(loc))
        self.assertEqual(self.state.registry["PRESS_40"], {"cell": "Press 40 cell"})
        self.admin("/admin/board/add", {"board": "PRESS_40", "cell": "P40"})
        self.assertEqual(self.state.cell_name("PRESS_40"), "P40")
        # kept through a restart
        self.assertIn("PRESS_40", R.State(self.cfg, self.tmp, self.tmp).registry)
        self.admin("/admin/board/remove", {"board": "PRESS_40"})
        self.assertNotIn("PRESS_40", self.state.registry)
        self.assertNotIn("PRESS_40", R.State(self.cfg, self.tmp, self.tmp).registry)

    def test_bad_board_id_refused(self):
        for bad in ("", "has space", "x" * 33, "<b>"):
            _, loc, _ = self.admin("/admin/board/add", {"board": bad, "cell": "X"})
            self.assertIn("not added", urllib.parse.unquote(loc))
        self.assertEqual(sorted(self.state.registry), ["FSP_LH", "FSP_RH", "RSP_LH", "RSP_RH"])

    def test_admin_test_call(self):
        st, loc, _ = self.admin("/admin/test", {"board": "FSP_RH", "code": "8", "press": "12"})
        self.assertEqual(st, 303)
        self.wait_pages(1)
        self.assertEqual(self.pager.sent[0][2], "Maintenance requested to press number 12 for camera fault.")
        row = self.wait_row()
        self.assertEqual((row["cell"], row["code"], row["result"]), ("FSP RH", "8", "sent"))
        self.assertIn("admin page test", row["detail"])
        # same again at once is a repeat
        _, loc, _ = self.admin("/admin/test", {"board": "FSP_RH", "code": "8", "press": "12"})
        self.assertIn("repeat", urllib.parse.unquote(loc))
        # bad values
        _, loc, _ = self.admin("/admin/test", {"board": "FSP_RH", "code": "1", "press": "12"})
        self.assertIn("not sent", urllib.parse.unquote(loc))

    def test_custom_page(self):
        st, loc, _ = self.admin("/admin/custom", {"zone": "floor", "en": "Test page one.", "es": "Prueba uno."})
        self.assertEqual(st, 303)
        self.wait_pages(1)
        self.assertEqual(self.pager.sent, [])        # live: no audio for custom text yet
        row = self.wait_row()
        self.assertEqual((row["code"], row["result"]), ("custom", "FAILED"))
        self.state.dry_run = True
        self.admin("/admin/custom", {"zone": "floor", "en": "Test page two."})
        self.wait_pages()
        self.assertEqual(self.log_rows()[-1]["result"], "dry-run")
        _, loc, _ = self.admin("/admin/custom", {"zone": "nowhere", "en": "x"})
        self.assertIn("not sent", urllib.parse.unquote(loc))
        _, loc, _ = self.admin("/admin/custom", {"zone": "floor", "en": ""})
        self.assertIn("not sent", urllib.parse.unquote(loc))

    def test_dry_run_switch(self):
        self.admin("/admin/dryrun", {"dry_run": "on"})
        self.assertTrue(self.state.dry_run)
        self.assertTrue(json.loads(self.get("/status.json"))["dry_run"])
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 3})
        row = self.wait_row()
        self.assertEqual(row["result"], "dry-run")
        self.assertEqual(self.pager.sent, [])           # never reached the live pager
        self.assertTrue(R.State(self.cfg, self.tmp, self.tmp).dry_run)   # kept on restart
        self.admin("/admin/dryrun", {"dry_run": "off"})
        self.assertFalse(R.State(self.cfg, self.tmp, self.tmp).dry_run)

    def test_live_without_phone_pager_fails_loudly(self):
        self.worker.live = R.NotBuiltPager()
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 3})
        row = self.wait_row()
        self.assertEqual(row["result"], "FAILED")
        self.assertIn("not built", row["detail"])

    def test_admin_page_escapes_message(self):
        _, _, body = self.admin("/admin?msg=%3Cscript%3Ex%3C/script%3E")
        self.assertNotIn("<script>x</script>", body)

    def test_live_page_carries_audio(self):
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 9, "press": 27})
        self.wait_pages()
        self.assertTrue(self.pager.audio[0].startswith(b"RIFF"))

    def test_dry_run_saves_no_audio_by_default(self):
        self.assertEqual(self.cfg["audio"]["keep_dry_run_pages"], 0)
        self.state.dry_run = True
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 3})
        self.wait_pages()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "last_pages")))
        self.assertEqual(self.wait_row()["detail"], "dry run, not dialed")

    def test_dry_run_keeps_last_n_when_asked(self):
        self.state.dry_run = True
        self.cfg["audio"]["keep_dry_run_pages"] = 2
        for seq, press in ((1, 3), (2, 4), (3, 5)):
            self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": seq, "code": 2, "press": press})
            self.wait_pages()
            time.sleep(0.01)
        saved = sorted(os.listdir(os.path.join(self.tmp, "last_pages")))
        self.assertEqual(len(saved), 2)
        self.assertIn("audio saved as", self.log_rows()[-1]["detail"])
        # back to 0: the next start removes what was saved
        self.cfg["audio"]["keep_dry_run_pages"] = 0
        self.assertEqual(self.worker.dry.prune(self.worker.dry.keep()), 2)
        self.assertEqual(os.listdir(os.path.join(self.tmp, "last_pages")), [])

    def test_keep_setting_on_admin_page(self):
        self.admin("/admin/tuning", self.tuning_form(keep_dry_run_pages="15"))
        self.assertEqual(self.cfg["audio"]["keep_dry_run_pages"], 15)
        _, loc, _ = self.admin("/admin/tuning", self.tuning_form(keep_dry_run_pages="500"))
        self.assertIn("Not saved", urllib.parse.unquote(loc))
        self.assertIn("name=keep_dry_run_pages", self.admin("/admin")[2])

    def test_missing_audio_fails_live_page(self):
        os.remove(os.path.join(self.audio_dir, "en", "n", "3.wav"))
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 3})
        self.wait_pages()
        row = self.wait_row()
        self.assertEqual((row["result"], row["attempts"]), ("FAILED", "0"))
        self.assertIn("no audio", row["detail"])
        self.assertEqual(self.pager.sent, [])

    def test_custom_page_live_has_no_audio_yet(self):
        self.admin("/admin/custom", {"zone": "floor", "en": "Hello."})
        self.wait_pages()
        self.assertEqual(self.wait_row()["result"], "FAILED")

    def test_listen(self):
        st, _, _ = self.admin("/admin/listen?code=8&press=27", pw=None)
        self.assertEqual(st, 401)
        url = "http://127.0.0.1:%d/admin/listen?code=8&press=27" % self.port
        req = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(
            ("a:" + ADMIN_PW).encode()).decode()})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["Content-Type"], "audio/wav")
            self.assertTrue(r.read().startswith(b"RIFF"))
        self.assertEqual(self.admin("/admin/listen?code=1&press=27")[0], 400)
        self.assertEqual(self.pager.sent, [])          # listening never pages
        self.assertIn("all messages recorded", self.admin("/admin")[2])

    def tuning_form(self, **over):
        f = {"ht813_ip": "192.168.83.21", "dial_method": "2-stage", "sip_port": "5062",
             "local_sip_port": "5070", "wait_after_answer_ms": "1000", "dtmf_ms": "100",
             "dtmf_gap_ms": "100", "wait_after_dial_ms": "2500", "max_call_s": "60",
             "dial_floor": "F*61", "dial_office": "", "repeat": "3", "tones": "once",
             "lead_silence_ms": "3000", "gain_db": "6"}
        for c in range(2, 10):
            f["zone_%d" % c] = "floor"
            f["tone_%d" % c] = self.cfg["codes"][c]["tone"]
        f.update(over)
        return f

    def test_tuning_saved_used_kept_and_reset(self):
        st, loc, _ = self.admin("/admin/tuning", self.tuning_form(zone_4="office"))
        self.assertIn("saved", urllib.parse.unquote(loc))
        self.assertEqual(self.cfg["phone"]["ht813_ip"], "192.168.83.21")
        self.assertEqual(self.cfg["zones"]["floor"]["dial"], "F*61")
        self.assertEqual(self.cfg["codes"][4]["zone"], "office")
        self.assertEqual(self.cfg["audio"]["repeat"], 3)
        self.assertEqual(self.cfg["audio"]["lead_silence_ms"], 3000)
        self.assertEqual(self.cfg["audio"]["gain_db"], 6)
        # change a tone and turn tones to twice
        self.admin("/admin/tuning", self.tuning_form(zone_4="office", tone_6="1_maintenance_bell", tones="twice"))
        self.assertEqual((self.cfg["codes"][6]["tone"], self.cfg["audio"]["tones"]), ("1_maintenance_bell", "twice"))
        # the next page uses the new zone and dial digits
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 7})
        self.wait_pages()
        self.assertEqual(self.pager.sent[-1][:2], ("floor", "F*61"))
        # kept through a restart
        cfg2 = R.load_config(CONFIG)
        R.State(cfg2, self.tmp, self.tmp)
        self.assertEqual((cfg2["zones"]["floor"]["dial"], cfg2["phone"]["dtmf_ms"], cfg2["codes"][4]["zone"]),
                         ("F*61", 100, "office"))
        # the admin page shows the saved values
        self.assertIn("value='F*61'", self.admin("/admin")[2])
        # reset
        self.admin("/admin/tuning/reset", {})
        self.assertEqual((self.cfg["zones"]["floor"]["dial"], self.cfg["codes"][4]["zone"],
                          self.cfg["phone"]["ht813_ip"]), ("F,*60,2,1", "floor", "192.168.83.20"))
        cfg3 = R.load_config(CONFIG)
        R.State(cfg3, self.tmp, self.tmp)
        self.assertEqual(cfg3["zones"]["floor"]["dial"], "F,*60,2,1")

    def test_tuning_bad_values_not_saved(self):
        for bad in ({"ht813_ip": "300.1.1.1"}, {"dial_floor": "PAGE1"}, {"dtmf_ms": "5"},
                    {"tone_2": "no_such_tone"}, {"tones": "loud"}, {"gain_db": "20"}, {"lead_silence_ms": "20000"},
                    {"zone_3": "nowhere"}, {"repeat": "9"}, {"dial_method": "3-stage"}):
            _, loc, _ = self.admin("/admin/tuning", self.tuning_form(**bad))
            self.assertIn("Not saved", urllib.parse.unquote(loc), bad)
        self.assertEqual(self.cfg["phone"]["ht813_ip"], "192.168.83.20")
        self.assertNotIn("tuning", self.state.settings)

    def test_dial_lowercase_f_and_spaces_accepted(self):
        self.admin("/admin/tuning", self.tuning_form(dial_floor="f * 6 1"))
        self.assertEqual(self.cfg["zones"]["floor"]["dial"], "F*61")

    def test_dry_run_switch_keeps_tuning(self):
        self.admin("/admin/tuning", self.tuning_form())
        self.admin("/admin/dryrun", {"dry_run": "on"})
        self.assertIn("tuning", R.State(self.cfg, self.tmp, self.tmp).settings)

    def preview(self, query):
        st, _, body = self.admin("/admin/preview?" + query)
        self.assertEqual(st, 200)
        return json.loads(body)

    def test_preview_test_call(self):
        p = self.preview("kind=test&board=RSP_LH&code=8&press=27")
        self.assertTrue(p["ok"])
        self.assertEqual(p["en"], "Maintenance requested to press number 27 for camera fault.")
        self.assertEqual(p["es"], "Se solicita mantenimiento en la prensa número 27 por falla de cámara.")
        self.assertEqual((p["from"], p["zone"], p["tone"], p["plays"]), ("RSP LH (RSP_LH)", "floor", "maintenance bell", 2))
        self.assertEqual(p["audio"], "/admin/listen?code=8&press=27")
        self.assertEqual(p["notes"], [])
        self.assertEqual(self.pager.sent, [])                 # previewing never pages
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.csv")))   # nor logs a call
        self.assertEqual(self.preview("kind=test&board=RSP_LH&code=4&press=3")["tone"], "supervisor glide")

    def test_preview_bad_and_password(self):
        self.assertFalse(self.preview("kind=test&board=RSP_LH&code=1&press=27")["ok"])
        self.assertFalse(self.preview("kind=test&board=RSP_LH&code=2&press=x")["ok"])
        self.assertEqual(self.admin("/admin/preview?kind=test&code=2&press=1", pw=None)[0], 401)

    def test_preview_custom(self):
        p = self.preview("kind=custom&zone=floor&en=Hello+there.&es=Hola.")
        self.assertTrue(p["ok"])
        self.assertEqual((p["en"], p["es"], p["audio"]), ("Hello there.", "Hola.", None))
        self.assertTrue(p["notes"])
        self.assertFalse(self.preview("kind=custom&zone=floor&en=")["ok"])

    def test_preview_missing_audio(self):
        os.remove(os.path.join(self.audio_dir, "es", "n", "5.wav"))
        p = self.preview("kind=test&board=RSP_LH&code=2&press=5")
        self.assertIsNone(p["audio"])
        self.assertIn("would fail", p["notes"][0])

    def test_admin_page_has_preview(self):
        body = self.admin("/admin")[2]
        self.assertIn("p833Preview('test')", body)
        self.assertIn("p833Preview('custom')", body)

    # ---- the real phone pager against a fake HT813
    def use_fake_ht813(self, **kw):
        from fake_ht813 import FakeHT813
        ht = FakeHT813(**kw)
        self.addCleanup(ht.close)
        self.cfg["phone"].update(ht813_ip="127.0.0.1", sip_port=ht.port, local_sip_port=0,
                                 wait_after_answer_ms=60, dtmf_ms=60, dtmf_gap_ms=40, wait_after_dial_ms=60)
        self.cfg["audio"].update(lead_silence_ms=0, repeat=1)
        self.worker.live = R.SipPager(self.cfg)
        self.worker.state_dir = self.tmp
        return ht

    def test_live_page_through_ht813(self):
        ht = self.use_fake_ht813()
        self.cfg["zones"]["floor"]["dial"] = "F61"
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 27})
        self.wait_pages()
        row = self.wait_row()
        self.assertEqual(row["result"], "sent", row["detail"])
        self.assertIn("dialed F61", row["detail"])
        self.assertEqual(ht.digits, ["F", "6", "1"])
        self.assertGreater(ht.audio_frames, 0)
        self.assertTrue(ht.wait_bye())
        with open(os.path.join(self.tmp, "last_page_line.wav"), "rb") as f:   # what the line sent back
            self.assertTrue(f.read().startswith(b"RIFF"))
        self.assertIn("Dial digits in use now", self.admin("/admin")[2])
        self.assertIn("floor: F61", self.admin("/admin")[2])

    def test_live_page_without_dial_digits_fails(self):
        ht = self.use_fake_ht813()
        self.cfg["zones"]["floor"]["dial"] = ""
        self.post("/call", {"board": "RSP_LH", "boot": "b", "seq": 1, "code": 2, "press": 27})
        self.wait_pages()
        row = self.wait_row()
        self.assertEqual(row["result"], "FAILED")
        self.assertIn("no dial digits", row["detail"])
        self.assertEqual(ht.invites, [])

    def test_phone_connection_test_button(self):
        ht = self.use_fake_ht813()
        self.state.dry_run = True                      # works with dry run on
        _, loc, _ = self.admin("/admin/phonetest", {})
        msg = urllib.parse.unquote(loc)
        self.assertIn("Phone test OK", msg)
        self.assertIn("Nothing was dialed", msg)
        self.assertEqual(ht.digits, [])
        self.assertTrue(ht.wait_bye())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "calls.csv")))   # not a page, not logged

    def test_try_page_digits(self):
        ht = self.use_fake_ht813()
        self.state.dry_run = True                      # works with dry run on
        _, loc, _ = self.admin("/admin/phonetest", {"digits": "f,*62"})
        msg = urllib.parse.unquote(loc)
        self.assertIn("Phone test OK", msg)
        self.assertIn("dialed F,*62", msg)
        self.assertEqual(ht.digits, ["F", "*", "6", "2"])
        self.assertIn("Recorded", msg)                   # what the line sent back was kept
        with open(os.path.join(self.tmp, "last_phone_test.wav"), "rb") as f:
            self.assertTrue(f.read().startswith(b"RIFF"))
        req = urllib.request.Request("http://127.0.0.1:%d/admin/last_phone_test.wav" % self.port,
                                     headers={"Authorization": "Basic " + base64.b64encode(
                                         ("a:" + ADMIN_PW).encode()).decode()})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.headers["Content-Type"], "audio/wav")
            self.assertTrue(r.read().startswith(b"RIFF"))
        self.assertEqual(ht.audio_frames, 2)           # fake tone 100 samples x 2 (with a silent gap) = 2 packets
        self.assertTrue(ht.wait_bye())
        # the same trial, playing a full test page
        _, loc, _ = self.admin("/admin/phonetest", {"digits": "F61", "play": "page"})
        msg = urllib.parse.unquote(loc)
        self.assertIn("a full test page", msg)
        self.assertEqual(ht.digits[-3:], ["F", "6", "1"])
        self.assertGreater(ht.audio_frames, 2)             # more than the bell alone (fake clips are tiny)
        # the digits box is prefilled with the floor dial (or the known working code)
        self.assertIn("name=digits required maxlength=32 size=12 value='F,*60,2,1'", self.admin("/admin")[2])
        _, loc, _ = self.admin("/admin/phonetest", {"digits": "PAGE1"})
        self.assertIn("FAILED", urllib.parse.unquote(loc))
        self.assertIn("Try page digits", self.admin("/admin")[2])

    def test_phone_connection_test_reports_no_answer(self):
        self.use_fake_ht813(mode="silent")
        self.cfg["phone"]["max_call_s"] = 2
        _, loc, _ = self.admin("/admin/phonetest", {})
        self.assertIn("Phone test FAILED", urllib.parse.unquote(loc))
        self.assertIn("did not answer", urllib.parse.unquote(loc))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class FakePager:
    def __init__(self):
        self.sent = []
        self.audio = []
        self.fail = 0

    def page(self, zone, dial, en, es, audio=None):
        self.audio.append(audio)
        if self.fail:
            self.fail -= 1
            return False, "fake failure"
        self.sent.append((zone, dial, en, es))
        return True, "test-ok"


if __name__ == "__main__":
    unittest.main()
