# Tests for the optional voip.ms text messages (receiver/p833_sms.py).
# By Ethan Brady & Ryan Raikes

import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "receiver"))
import p833_sms as T    # noqa: E402


class FakeVoipMs:
    """Stands in for the HTTPS call to voip.ms: records requests, answers with a status."""

    def __init__(self, statuses=None, fail_network=0):
        self.requests = []
        self.statuses = list(statuses or [])
        self.fail_network = fail_network

    def __call__(self, url, params, timeout):
        self.requests.append((url, dict(params), timeout))
        if self.fail_network:
            self.fail_network -= 1
            raise OSError("network down")
        status = self.statuses.pop(0) if self.statuses else "success"
        return json.dumps({"status": status}).encode()


def cfg_with(**sms):
    s = dict(T.DEFAULTS, enabled=True, api_username="ops@example.com", did="555-123-4567",
             to=["5550001111", "(555) 000-2222"], stagger_s=0)
    s.update(sms)
    return {"sms": s}


class Sms(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def sender(self, cfg, fake):
        s = T.SmsSender(cfg, self.tmp, self.tmp, opener=fake)
        s.set_password("api-secret-123")
        return s

    def test_numbers(self):
        self.assertEqual(T.clean_number("(555) 123-4567"), "5551234567")
        self.assertEqual(T.clean_number("+1 555 123 4567"), "5551234567")
        self.assertEqual(T.clean_number("123"), "")
        self.assertEqual(T.clean_number("25551234567"), "")

    def test_compose(self):
        rec = {"message": "Maintenance requested to press 27 for camera fault.", "cell": "RSP LH", "press": 27,
               "who": "Maintenance", "code": 8}
        self.assertEqual(T.compose(cfg_with(), rec), "Chapar: Maintenance requested to press 27 for camera fault.")
        self.assertEqual(T.compose(cfg_with(template="{prefix} {cell}: {message}"), rec),
                         "Chapar: RSP LH: Maintenance requested to press 27 for camera fault.")
        self.assertEqual(len(T.compose(cfg_with(), {"message": "x" * 300})), T.MAX_SMS)
        self.assertEqual(T.compose(cfg_with(template="{nope}"), rec)[:8], "Chapar: ")   # bad template falls back

    def test_send_to_each_number(self):
        fake = FakeVoipMs()
        s = self.sender(cfg_with(), fake)
        ok, detail = s.send("Chapar: test")
        self.assertTrue(ok, detail)
        self.assertEqual([r[1]["dst"] for r in fake.requests], ["5550001111", "5550002222"])
        p = fake.requests[0][1]
        self.assertEqual((p["method"], p["did"], p["api_username"], p["message"]),
                         ("sendSMS", "5551234567", "ops@example.com", "Chapar: test"))
        self.assertEqual(p["api_password"], "api-secret-123")
        self.assertTrue(fake.requests[0][0].startswith("https://voip.ms/api/v1/rest.php"))

    def test_errors_are_reported(self):
        s = self.sender(cfg_with(), FakeVoipMs(statuses=["invalid_credentials", "success"]))
        ok, detail = s.send("hi")
        self.assertFalse(ok)
        self.assertIn("invalid_credentials", detail)
        self.assertEqual([r["result"] for r in s.recent], ["FAILED", "sent"])

    def test_network_error_is_retried_once(self):
        fake = FakeVoipMs(fail_network=1)
        s = self.sender(cfg_with(to=["5550001111"]), fake)
        t0 = time.monotonic()
        ok, _ = s.send("hi")
        self.assertTrue(ok)
        self.assertEqual(len(fake.requests), 2)
        self.assertGreater(time.monotonic() - t0, 2.5)

    def test_missing_settings(self):
        for over, word in (({"api_username": ""}, "e-mail"), ({"did": ""}, "sending number"),
                           ({"to": []}, "no numbers")):
            s = self.sender(cfg_with(**over), FakeVoipMs())
            ok, detail = s.send("hi")
            self.assertFalse(ok)
            self.assertIn(word, detail)
        s = T.SmsSender(cfg_with(), self.tmp, self.tmp, opener=FakeVoipMs())
        s.set_password("")
        self.assertIn("password", s.send("hi")[1])

    def test_password_storage_is_private_and_never_logged(self):
        fake = FakeVoipMs()
        s = self.sender(cfg_with(), fake)
        path = os.path.join(self.tmp, "sms_secret.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(s.has_password())
        s.send("hi")
        with open(os.path.join(self.tmp, "texts.csv"), encoding="utf-8") as f:
            self.assertNotIn("api-secret-123", f.read())
        self.assertNotIn("api-secret-123", json.dumps(s.recent))
        s.set_password("")
        self.assertFalse(os.path.exists(path))
        self.assertFalse(s.has_password())

    def test_staggered(self):
        fake = FakeVoipMs()
        s = self.sender(cfg_with(to=["5550001111", "5550002222", "5550003333"], stagger_s=1), fake)
        t0 = time.monotonic()
        ok, _ = s.send("hi")
        self.assertTrue(ok)
        self.assertGreater(time.monotonic() - t0, 1.9)          # two pauses of 1 s
        self.assertEqual(len(fake.requests), 3)

    def test_hourly_cap(self):
        fake = FakeVoipMs()
        s = self.sender(cfg_with(to=["5550001111", "5550002222", "5550003333"], max_per_hour=2), fake)
        ok, detail = s.send("hi")
        self.assertFalse(ok)
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual([r["result"] for r in s.recent], ["sent", "sent", "SKIPPED"])
        self.assertIn("hourly limit", detail)

    def test_test_text_is_queued(self):
        fake = FakeVoipMs()
        s = self.sender(cfg_with(), fake)
        ok, detail = s.test_text()
        self.assertTrue(ok)
        self.assertIn("queued for 2", detail)
        self.assertEqual(s.jobs.get_nowait(), "Chapar: Test text from the paging system.")
        self.assertFalse(self.sender(cfg_with(to=[]), fake).test_text()[0])

    def test_https_only(self):
        with self.assertRaises(ValueError):
            T.SmsSender._https("http://voip.ms/api/v1/rest.php", {}, 1)

    def test_queue_respects_switches(self):
        fake = FakeVoipMs()
        cfg = cfg_with(enabled=False)
        s = self.sender(cfg, fake)
        s.text_for_call({"message": "m"})
        self.assertTrue(s.jobs.empty())                       # off: nothing queued
        cfg["sms"]["enabled"] = True
        s.text_for_call({"message": "m"}, test_call=True)
        self.assertTrue(s.jobs.empty())                       # admin tests: only if asked
        cfg["sms"]["admin_tests"] = True
        s.text_for_call({"message": "m"}, test_call=True)
        self.assertEqual(s.jobs.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
