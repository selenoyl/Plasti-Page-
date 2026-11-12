# A fake Grandstream HT813 FXO port for testing the phone pager without the
# real box: answers SIP calls over UDP, records the DTMF digits (RFC 2833
# events) and audio it receives, and answers BYE.
# By Ethan Brady & Ryan Raikes
#
# mode: "answer" (normal), "busy" (486), "silent" (never answers),
#       "hangup" (answers, then hangs up after hangup_after seconds)

import socket
import struct
import threading
import time

EVENT_CHARS = {d: str(d) for d in range(10)}
EVENT_CHARS.update({10: "*", 11: "#", 16: "F"})


class FakeHT813:
    def __init__(self, mode="answer", hangup_after=0.5, line_tone="quiet"):
        # line_tone: what the "line" sends back during the call: quiet (an open page),
        # dial (dial tone: flash or digits not taken), busy, or ring (called a phone)
        self.mode, self.hangup_after, self.line_tone = mode, hangup_after, line_tone
        self.calls = 0
        self.sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sip.bind(("127.0.0.1", 0))
        self.port = self.sip.getsockname()[1]
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", 0))
        self.rtp_port = self.rtp.getsockname()[1]
        self.invites, self.requests = [], []
        self.digits = []              # completed DTMF events, in order
        self.audio_frames = 0
        self.timeline = []            # ("audio"|"event"|"silence", time)
        self.got_bye = False
        self.got_ack = False
        self._last_event_ts = None
        self._stop = False
        self._peer = None
        threading.Thread(target=self._sip_loop, daemon=True).start()
        threading.Thread(target=self._rtp_loop, daemon=True).start()

    def close(self):
        self._stop = True
        self.sip.close()
        self.rtp.close()

    def _reply(self, addr, code, text, h, body="", extra=""):
        to = h.get("to", "")
        if code >= 200 and "tag=" not in to:
            to += ";tag=fake1"
        msg = ("SIP/2.0 %d %s\r\nVia: %s\r\nFrom: %s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %s\r\n%s"
               "Content-Length: %d\r\n\r\n%s" % (code, text, h.get("via", ""), h.get("from", ""), to,
                                                h.get("call-id", ""), h.get("cseq", ""), extra, len(body), body))
        self.sip.sendto(msg.encode(), addr)

    def _sip_loop(self):
        while not self._stop:
            try:
                data, addr = self.sip.recvfrom(8192)
            except OSError:
                return
            text = data.decode("utf-8", "replace")
            head, _, body = text.partition("\r\n\r\n")
            lines = head.split("\r\n")
            h = {}
            for line in lines[1:]:
                k, _, v = line.partition(":")
                h.setdefault(k.strip().lower(), v.strip())
            first = lines[0]
            self.requests.append(first)
            if first.startswith("INVITE "):
                self.invites.append((first, h, body))
                self.calls += 1
                self._peer = (addr, h)
                if self.mode == "silent":
                    continue
                self._reply(addr, 100, "Trying", h)
                if self.mode == "busy":
                    self._reply(addr, 486, "Busy Here", h)
                    continue
                sdp = ("v=0\r\no=ht 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                       "m=audio %d RTP/AVP 0 101\r\na=rtpmap:101 telephone-event/8000\r\n" % self.rtp_port)
                self._reply(addr, 200, "OK", h, sdp, "Contact: <sip:fxo@127.0.0.1:%d>\r\n"
                            "Content-Type: application/sdp\r\n" % self.port)
                far = None
                for line in body.splitlines():
                    if line.startswith("m=audio "):
                        far = ("127.0.0.1", int(line.split()[1]))
                if far:
                    threading.Thread(target=self._send_tone_back, args=(far,), daemon=True).start()
                if self.mode == "hangup":
                    threading.Timer(self.hangup_after, self._hang_up, args=(addr, h)).start()
            elif first.startswith("ACK "):
                self.got_ack = True
            elif first.startswith("BYE "):
                self.got_bye = True
                self._reply(addr, 200, "OK", h)
            elif first.startswith("CANCEL "):
                self._reply(addr, 200, "OK", h)

    def _send_tone_back(self, far, seconds=12):
        """Send what the line would: quiet, or a call-progress tone, until the call ends."""
        import math, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "receiver"))
        import p833_sip
        freqs = {"dial": (350, 440), "busy": (480, 620), "ring": (440, 480)}.get(self.line_tone)
        bye_seen = self.got_bye
        for i in range(int(seconds / 0.02)):
            if self._stop or (self.got_bye and not bye_seen):
                return
            t0 = i * 160
            if freqs and (self.line_tone != "busy" or (t0 // 4000) % 2 == 0):   # busy: 0.5 s on/off
                samples = [int(3000 * sum(math.sin(2 * math.pi * f * (t0 + k) / 8000.0) for f in freqs))
                           for k in range(160)]
            else:
                samples = [0] * 160
            payload = p833_sip.pcm16_to_ulaw(struct.pack("<160h", *samples))
            pkt = struct.pack("!BBHII", 0x80, 0, i & 0xFFFF, t0 & 0xFFFFFFFF, 0x1234) + payload
            try:
                self.rtp.sendto(pkt, far)
            except OSError:
                return
            time.sleep(0.02)

    def _hang_up(self, addr, h):
        msg = ("BYE sip:p833@%s SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:%d;branch=z9hG4bKfakebye\r\n"
               "From: %s;tag=fake1\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: 1 BYE\r\nContent-Length: 0\r\n\r\n" % (
                   addr[0], self.port, h.get("to", ""), h.get("from", ""), h.get("call-id", "")))
        try:
            self.sip.sendto(msg.encode(), addr)
        except OSError:
            pass

    def _rtp_loop(self):
        while not self._stop:
            try:
                data, _ = self.rtp.recvfrom(2048)
            except OSError:
                return
            if len(data) < 12:
                continue
            pt = data[1] & 0x7F
            ts = struct.unpack("!I", data[4:8])[0]
            payload = data[12:]
            now = time.monotonic()
            if pt == 0:
                silent = payload == b"\xff" * len(payload)
                self.timeline.append(("silence" if silent else "audio", now))
                if not silent:
                    self.audio_frames += 1
            elif pt == 101 and len(payload) >= 4:
                event, flags = payload[0], payload[1]
                if flags & 0x80 and ts != self._last_event_ts:     # end packet, count once
                    self._last_event_ts = ts
                    self.digits.append(EVENT_CHARS.get(event, "?"))
                self.timeline.append(("event", now))

    def wait_bye(self, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.got_bye:
            time.sleep(0.02)
        return self.got_bye
