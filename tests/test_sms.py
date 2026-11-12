# Tests for the optional voip.ms text messages (receiver/p833_sms.py).
# By Ethan Brady & Ryan Raikes

import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "receiver"))
import p833_sms as T    # noqa: E402


class FakeVoipMs:
    """Stands in for the HTTPS call to voip.ms: records sendSMS requests, answers with a status.
    fail_network: requests that never get there; timeouts: requests sent but not answered."""

    def __init__(self, statuses=None, fail_network=0, timeouts=0, ip="203.0.113.5"):
        self.requests, self.ip_lookups = [], 0
        self.statuses = list(statuses or [])
        self.fail_network, self.timeouts, self.ip = fail_network, timeouts, ip

    def __call__(self, url, params, timeout):
        if params.get("method") == "getIP":
            self.ip_lookups += 1
            return json.dumps({"status": "success", "ip": self.ip}).encode()
        self.requests.append((url, dict(params), timeout))
        if self.fail_network:
            self.fail_network -= 1
            raise urllib.error.URLError(OSError("network down"))
        if self.timeouts:
            self.timeouts -= 1
            raise socket.timeout("timed out")
        status = self.statuses.pop(0) if self.statuses else "success"
        if status[:1] in ("{", "<", "["):
            return status.encode()                      # a raw answer
        return json.dumps({"status": status, "sms": 23434} if status == "success" else {"status": status}).encode()


def cfg_with(**sms):
    s = dict(T.DEFAULTS, enabled=True, api_username="ops@example.com", did="555-123-4567",
             to=["5550001111", "(555) 000-2222"], stagger_min_s=0, stagger_max_s=0)
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
        long = T.compose(cfg_with(), {"message": "x" * 300})
        self.assertEqual((len(long), long[-3:]), (T.MAX_SMS, "..."))
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
        s = self.sender(cfg_with(), FakeVoipMs(statuses=["invalid_dst", "success"]))
        ok, detail = s.send("hi")
        self.assertFalse(ok)
        self.assertIn("invalid_dst", detail)
        self.assertEqual([r["result"] for r in s.recent], ["FAILED", "sent"])   # one bad number: keep going
        self.assertIn("voip.ms id 23434", s.recent[1]["detail"])

    def test_account_problem_stops_the_batch(self):
        fake = FakeVoipMs(statuses=["invalid_credentials"])
        s = self.sender(cfg_with(to=["5550001111", "5550002222", "5550003333"]), fake)
        ok, detail = s.send("hi")
        self.assertFalse(ok)
        self.assertEqual(len(fake.requests), 1)                  # no point trying the others
        self.assertEqual([r["result"] for r in s.recent], ["FAILED", "SKIPPED", "SKIPPED"])
        self.assertIn("API password", s.recent[0]["detail"])     # with a hint what to fix

    def test_ip_not_allowed_shows_the_address(self):
        fake = FakeVoipMs(statuses=["ip_not_enabled"])
        s = self.sender(cfg_with(to=["5550001111"]), fake)
        ok, detail = s.send("hi")
        self.assertFalse(ok)
        self.assertEqual(fake.ip_lookups, 1)
        self.assertIn("voip.ms sees this site as 203.0.113.5", detail)

    def test_busy_is_retried_once(self):
        fake = FakeVoipMs(statuses=["api_limit_exceeded", "success"])
        s = self.sender(cfg_with(to=["5550001111"]), fake)
        s.busy_retry_s = 0.05
        self.assertTrue(s.send("hi")[0])
        self.assertEqual(len(fake.requests), 2)
        fake = FakeVoipMs(statuses=["sms_failed", "success"])    # not a busy answer: no retry
        s = self.sender(cfg_with(to=["5550001111"]), fake)
        self.assertFalse(s.send("hi")[0])
        self.assertEqual(len(fake.requests), 1)

    def test_no_retry_once_the_request_may_have_arrived(self):
        fake = FakeVoipMs(timeouts=1)
        s = self.sender(cfg_with(to=["5550001111"]), fake)
        ok, detail = s.send("hi")
        self.assertFalse(ok)
        self.assertEqual(len(fake.requests), 1)                  # a retry could send it twice
        self.assertIn("may or may not", detail)

    def test_bad_answers(self):
        for raw, word in (("<soap:Fault/>", "isn't JSON"), ("{}", "no status"), ('["x"]', "no status")):
            s = self.sender(cfg_with(to=["5550001111"]), FakeVoipMs(statuses=[raw]))
            ok, detail = s.send("hi")
            self.assertFalse(ok)
            self.assertIn(word, detail)

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
        s = self.sender(cfg_with(to=["5550001111", "5550002222", "5550003333"], stagger_min_s=1,
                                 stagger_max_s=1), fake)
        t0 = time.monotonic()
        ok, _ = s.send("hi")
        self.assertTrue(ok)
        self.assertGreater(time.monotonic() - t0, 1.9)          # two pauses of 1 s
        self.assertEqual(len(fake.requests), 3)
        # the pause is random within the range, and varies
        pauses = [T.SmsSender.pause_s(dict(T.DEFAULTS)) for _ in range(200)]
        self.assertTrue(all(1 <= p <= 10 for p in pauses))
        self.assertGreater(len(set(round(p, 2) for p in pauses)), 50)
        self.assertEqual(T.SmsSender.pause_s(dict(T.DEFAULTS, stagger_min_s=5, stagger_max_s=2)), 5)

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
        self.assertIn("0 to 0 s apart", detail)              # this test sets no pause
        self.assertEqual(s.jobs.get_nowait(), ("Chapar: Test text from the paging system.", None))
        self.assertFalse(self.sender(cfg_with(to=[]), fake).test_text()[0])

    def test_https_only_and_only_to_voipms(self):
        for url in ("http://voip.ms/api/v1/rest.php", "https://voip.ms.evil.example/api/v1/rest.php",
                    "https://evil.example/?x=https://voip.ms/", "https://voip.ms@evil.example/",
                    "https://voip.ms/api/v1/rest.php?method=x"):
            with self.assertRaises(ValueError, msg=url):
                T.SmsSender._https(url, {}, 1)

    def test_request_is_a_get(self):
        seen = []

        class Opener:
            def open(self, req, timeout):
                seen.append((req, timeout))
                raise urllib.error.URLError("stop here")

        real = T.urllib.request.build_opener
        T.urllib.request.build_opener = lambda *a: Opener()
        try:
            with self.assertRaises(urllib.error.URLError):
                T.SmsSender._https("https://voip.ms/api/v1/rest.php",
                                   {"api_password": "p&ss#w+rd", "method": "sendSMS", "message": "a b"}, 7)
        finally:
            T.urllib.request.build_opener = real
        req, timeout = seen[0]
        self.assertEqual((req.get_method(), req.data, timeout), ("GET", None, 7))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        self.assertEqual((q["api_password"], q["message"]), (["p&ss#w+rd"], ["a b"]))   # encoded safely

    def test_redirects_are_refused(self):
        class Req:
            full_url = "https://voip.ms/api/v1/rest.php?api_password=secret"
        with self.assertRaises(urllib.error.HTTPError) as c:
            T._NoRedirect().redirect_request(Req(), None, 301, "Moved", {}, "http://voip.ms/x")
        self.assertNotIn("secret", c.exception.geturl())

    def test_plain_ascii(self):
        self.assertEqual(T.finish("Ma\u00f1ana \u2013 \u201cpress\u201d it\u2019s\u2026 \U0001f527"),
                         'Manana - "press" it\'s...')
        self.assertEqual(T.mask("5551234567"), "...4567")

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

    def test_manual_text(self):
        fake = FakeVoipMs()
        s = self.sender(cfg_with(enabled=False), fake)       # works with automatic texts off
        ok, detail = s.manual_text("(555) 444-3333", "  Lunch  truck   is here ")
        self.assertTrue(ok, detail)
        text, dsts = s.jobs.get_nowait()
        self.assertEqual((text, dsts), ("Chapar: Lunch truck is here", ["5554443333"]))
        ok, detail = s.send(text, dsts)
        self.assertTrue(ok)
        self.assertEqual([r[1]["dst"] for r in fake.requests], ["5554443333"])   # only that number
        self.assertEqual(s.manual_text("5554443333", "no prefix", add_prefix=False)[1], "queued to 5554443333: no prefix")
        self.assertFalse(s.manual_text("123", "hi")[0])
        self.assertFalse(s.manual_text("5554443333", "   ")[0])
        self.assertFalse(s.manual_text("5554443333", "\U0001f527")[0])     # nothing left once made ASCII
        self.assertEqual(len(s.manual_text("5554443333", "x" * 300)[1].split(": ", 1)[1]), T.MAX_SMS)


if __name__ == "__main__":
    unittest.main()
