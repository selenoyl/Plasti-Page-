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
import p833_receiver as R          # noqa: E402
import p833_test_sender as S       # noqa: E402

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

    def test_wording_from_brief(self):
        self.assertEqual(R.build_message(self.cfg, 2, 27)[0], "Maintenance needed at press 27.")
        self.assertEqual(R.build_message(self.cfg, 7, 5)[0],
                         "Supervisor required at press 5 for audit check-in.")
        self.assertEqual(R.build_message(self.cfg, 8, 5)[0],
                         "Maintenance required at press 5 for camera fault.")
        self.assertEqual(R.build_message(self.cfg, 9, 5)[0],
                         "Maintenance required at press 5 for Red Rabbit failure.")

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
        self.worker = R.PageWorker(cfg, self.state, self.pager)
        self.worker.start()
        R.Handler.app = {"cfg": cfg, "state": self.state, "worker": self.worker, "key": KEY,
                         "admin_password": ADMIN_PW}
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
        self.assertEqual(self.pager.sent, [("floor", "TO-VERIFY", "Maintenance needed at press 27.",
                                            "Se necesita mantenimiento en la prensa 27.")])
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
        self.assertEqual(self.pager.sent[0][2], "Maintenance required at press 12 for camera fault.")
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
        self.assertEqual(self.pager.sent, [("floor", "TO-VERIFY", "Test page one.", "Prueba uno.")])
        row = self.wait_row()
        self.assertEqual((row["code"], row["result"]), ("custom", "sent"))
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


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class FakePager:
    def __init__(self):
        self.sent = []
        self.fail = 0

    def page(self, zone, dial, en, es):
        if self.fail:
            self.fail -= 1
            return False, "fake failure"
        self.sent.append((zone, dial, en, es))
        return True, "test-ok"


if __name__ == "__main__":
    unittest.main()
