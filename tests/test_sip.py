# Tests for the phone pager (receiver/p833_sip.py) against a fake HT813.
# By Ethan Brady & Ryan Raikes

import os
import re
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "receiver"))
sys.path.insert(0, HERE)
import p833_sip as S                # noqa: E402
from fake_ht813 import FakeHT813    # noqa: E402


def tone_wav(seconds):
    import io, math, struct, wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 5.0))) for i in range(int(8000 * seconds))))
    return out.getvalue()


def cfg_for(ht, **over):
    ph = {"ht813_ip": "127.0.0.1", "sip_port": ht.port, "local_sip_port": 0, "dial_method": "2-stage",
          "wait_after_answer_ms": 100, "dtmf_ms": 60, "dtmf_gap_ms": 40, "wait_after_dial_ms": 100,
          "max_call_s": 10, "ht813_user": "", "min_gap_s": 0, "line_check": "retry"}
    ph.update(over)
    return {"phone": ph}


class Pager(unittest.TestCase):
    def setUp(self):
        self.ht = None

    def tearDown(self):
        if self.ht:
            self.ht.close()

    def test_two_stage_page(self):
        self.ht = FakeHT813()
        t0 = time.monotonic()
        ok, detail = S.SipPager(cfg_for(self.ht)).page("floor", "F*61,1", "en", "es", tone_wav(1.0))
        took = time.monotonic() - t0
        self.assertTrue(ok, detail)
        self.assertTrue(self.ht.wait_bye())
        self.assertTrue(self.ht.got_ack)
        self.assertEqual(self.ht.digits, ["F", "*", "6", "1", "1"])
        for _ in range(50):                                        # the fake counts the last
            if self.ht.audio_frames >= 50:                         # packet a moment after BYE
                break
            time.sleep(0.02)
        self.assertEqual(self.ht.audio_frames, 50)                 # 1 s of audio = 50 x 20 ms
        first = self.ht.invites[0][0]
        self.assertTrue(first.startswith("INVITE sip:127.0.0.1:%d SIP/2.0" % self.ht.port), first)
        # paced in real time: 0.1 + 5 digits + 0.5 pause + 0.1 + 1 s of audio
        self.assertGreater(took, 1.8)
        # the digits all come before the audio
        last_event = max(t for k, t in self.ht.timeline if k == "event")
        first_audio = min(t for k, t in self.ht.timeline if k == "audio")
        self.assertLess(last_event, first_audio)

    def test_one_stage_puts_digits_in_the_address(self):
        self.ht = FakeHT813()
        ok, detail = S.SipPager(cfg_for(self.ht, dial_method="1-stage")).page("floor", "61", "en", "es",
                                                                              tone_wav(0.2))
        self.assertTrue(ok, detail)
        self.assertTrue(self.ht.invites[0][0].startswith("INVITE sip:61@127.0.0.1:"))
        self.assertEqual(self.ht.digits, [])

    def test_busy(self):
        self.ht = FakeHT813(mode="busy")
        ok, detail = S.SipPager(cfg_for(self.ht)).page("floor", "61", "en", "es", tone_wav(0.2))
        self.assertFalse(ok)
        self.assertIn("486", detail)

    def test_no_answer(self):
        self.ht = FakeHT813(mode="silent")
        t0 = time.monotonic()
        ok, detail = S.SipPager(cfg_for(self.ht, max_call_s=2)).page("floor", "61", "en", "es", tone_wav(0.2))
        self.assertFalse(ok)
        self.assertIn("did not answer", detail)
        self.assertLess(time.monotonic() - t0, 4)
        self.assertGreater(len(self.ht.invites), 1)       # it retransmitted the INVITE
        for _ in range(50):                               # the fake logs it a moment later
            if any(r.startswith("CANCEL ") for r in self.ht.requests):
                break
            time.sleep(0.02)
        self.assertTrue(any(r.startswith("CANCEL ") for r in self.ht.requests))

    def test_ht813_hangs_up_mid_page(self):
        self.ht = FakeHT813(mode="hangup", hangup_after=0.4)
        ok, detail = S.SipPager(cfg_for(self.ht)).page("floor", "61", "en", "es", tone_wav(3))
        self.assertFalse(ok)
        self.assertIn("hung up", detail)

    def test_page_longer_than_max_call_is_cut(self):
        self.ht = FakeHT813()
        ok, detail = S.SipPager(cfg_for(self.ht, max_call_s=1)).page("floor", "61", "en", "es", tone_wav(3))
        self.assertFalse(ok)
        self.assertIn("cut off", detail)
        self.assertTrue(self.ht.wait_bye())               # still hangs up cleanly

    def test_bad_settings_refused_without_calling(self):
        self.ht = FakeHT813()
        pager = S.SipPager(cfg_for(self.ht))
        self.assertIn("no dial digits", pager.page("floor", "", "en", "es", tone_wav(0.2))[1])
        self.assertIn("not valid", pager.page("floor", "PAGE1", "en", "es", tone_wav(0.2))[1])
        self.assertEqual(self.ht.invites, [])

    def test_line_check_catches_dial_tone(self):
        self.ht = FakeHT813(line_tone="dial")
        ok, detail = S.SipPager(cfg_for(self.ht, wait_after_dial_ms=600)).page("floor", "F61", "en", "es",
                                                                              tone_wav(0.5))
        self.assertFalse(ok)
        self.assertIn("dial tone", detail)
        self.assertEqual(self.ht.audio_frames, 0)          # the page audio wasn't played into nothing
        self.assertTrue(self.ht.wait_bye())

    def test_line_check_busy_and_ring(self):
        for tone_kind, word in (("busy", "busy"), ("ring", "ringing")):
            ht = FakeHT813(line_tone=tone_kind)
            try:
                ok, detail = S.SipPager(cfg_for(ht, wait_after_dial_ms=1000)).page(
                    "floor", "F61", "en", "es", tone_wav(0.3))
            finally:
                ht.close()
            self.assertFalse(ok, tone_kind)
            self.assertIn(word, detail)

    def test_line_check_quiet_line_is_ok_and_warn_mode(self):
        self.ht = FakeHT813(line_tone="quiet")
        ok, detail = S.SipPager(cfg_for(self.ht, wait_after_dial_ms=600)).page("floor", "F61", "en", "es",
                                                                              tone_wav(0.3))
        self.assertTrue(ok, detail)
        self.assertNotIn("WARNING", detail)
        ht2 = FakeHT813(line_tone="dial")
        try:
            ok, detail = S.SipPager(cfg_for(ht2, wait_after_dial_ms=600, line_check="warn")).page(
                "floor", "F61", "en", "es", tone_wav(0.3))
        finally:
            ht2.close()
        self.assertTrue(ok)
        self.assertIn("WARNING: the line still had dial tone", detail)

    def test_gap_between_calls(self):
        self.ht = FakeHT813()
        pager = S.SipPager(cfg_for(self.ht, min_gap_s=1.5))
        pager.page("floor", "61", "en", "es", tone_wav(0.1))
        t0 = time.monotonic()
        ok, _ = pager.page("floor", "61", "en", "es", tone_wav(0.1))
        self.assertTrue(ok)
        self.assertGreater(time.monotonic() - t0, 1.4)

    def test_password_challenge(self):
        self.ht = FakeHT813(password="s3cret")
        ok, detail = S.SipPager(cfg_for(self.ht), password="s3cret").page("floor", "61", "en", "es",
                                                                          tone_wav(0.2))
        self.assertTrue(ok, detail)
        self.assertTrue(self.ht.auth_ok)
        self.assertTrue(self.ht.wait_bye())
        for pw, word in (("wrong", "did not accept"), ("", "wants a password")):
            ht = FakeHT813(password="s3cret")
            try:
                ok, detail = S.SipPager(cfg_for(ht), password=pw).page("floor", "61", "en", "es", tone_wav(0.2))
            finally:
                ht.close()
            self.assertFalse(ok)
            self.assertIn(word, detail)
        self.assertEqual(S.digest_header('Digest realm="r", nonce="n"', "INVITE", "sip:h", "u", "p"),
                         'Digest username="u", realm="r", nonce="n", uri="sip:h", algorithm=MD5, '
                         'response="%s"' % __import__("hashlib").md5(
                             ("%s:n:%s" % (__import__("hashlib").md5(b"u:r:p").hexdigest(),
                                           __import__("hashlib").md5(b"INVITE:sip:h").hexdigest())).encode()
                         ).hexdigest())

    def forged_bye(self, src_ip, from_tag=None):
        """Send a BYE for the current call, as another device would, from src_ip."""
        import socket as _s
        _, h, _ = self.ht.invites[-1]
        our = h["from"]                                   # the pager's From (with its tag)
        bye = ("BYE sip:p833@127.0.0.1 SIP/2.0\r\nVia: SIP/2.0/UDP %s:5060;branch=z9hG4bKforged\r\n"
               "From: <sip:x@%s>;tag=%s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: 9 BYE\r\n"
               "Content-Length: 0\r\n\r\n" % (src_ip, src_ip, from_tag or "fake1", our, h["call-id"]))
        port = int(re.search(r"Contact: <sip:p833@[^:]+:(\d+)>", "\r\n".join(
            "%s: %s" % (k.title(), v) for k, v in h.items())).group(1))
        sock = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        sock.bind((src_ip, 0))
        sock.sendto(bye.encode(), ("127.0.0.1", port))
        sock.close()

    def test_forged_hang_up_is_ignored(self):
        import threading as _t
        for src, tag in (("127.0.0.2", None), ("127.0.0.1", "not-the-ht813-tag")):
            self.ht = FakeHT813()
            result = {}
            pager = S.SipPager(cfg_for(self.ht))
            th = _t.Thread(target=lambda: result.update(r=pager.page("floor", "61", "en", "es", tone_wav(1.5))))
            th.start()
            for _ in range(200):
                if self.ht.got_ack:
                    break
                time.sleep(0.01)
            time.sleep(0.3)
            self.forged_bye(src, tag)
            th.join(10)
            self.assertTrue(result["r"][0], (src, tag, result["r"]))   # the page played to the end
            self.ht.close()
            self.ht = None

    def test_ulaw(self):
        self.assertEqual(S.pcm16_to_ulaw(b"\x00\x00"), b"\xff")
        for v in (0, 100, -100, 5000, -5000, 32000, -32000):
            u = S.pcm16_to_ulaw(v.to_bytes(2, "little", signed=True))[0]
            back = S.ulaw_to_pcm16_sample(u)
            self.assertLessEqual(abs(back - v), max(8, abs(v) // 16), (v, back))


if __name__ == "__main__":
    unittest.main()
