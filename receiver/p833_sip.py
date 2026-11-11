# P833 Call Receiver - the phone pager: calls the Grandstream HT813's FXO port
# directly (SIP over UDP, no phone server), dials the page code on the Norstar
# line through the ATA2, plays the page audio and hangs up.
# By Ethan Brady & Ryan Raikes
#
#   laptop --SIP/RTP--> HT813 FXO --line--> Nortel ATA2 --> Norstar page zone
#
# 2-stage dialing (default): the HT813 answers by taking the line off-hook;
# the laptop then sends the dial digits as DTMF tones in the audio stream
# (RFC 2833 events, which the HT813 plays onto the line). Digits: 0-9 * #,
# "," = 0.5 s pause, "F" = hook flash.
# 1-stage: the digits go in the call's address and the HT813 dials them itself.
#
# Standard library only; Python 3.6+.

import io
import random
import re
import select
import socket
import struct
import threading
import time
import wave

RATE = 8000
FRAME = 160                # 20 ms of audio at 8 kHz
PT_PCMU = 0
PT_EVENT = 101             # telephone-event (RFC 2833) payload type we offer
DTMF_EVENTS = {str(d): d for d in range(10)}
DTMF_EVENTS.update({"*": 10, "#": 11, "F": 16})


# ------------------------------------------------------------- G.711 mu-law ---

def _ulaw_byte(sample):
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, CLIP) + BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


_ULAW = bytes(_ulaw_byte(s) for s in range(-32768, 32768))   # table, index sample+32768


def pcm16_to_ulaw(pcm):
    n = len(pcm) // 2
    samples = struct.unpack("<%dh" % n, pcm[:n * 2])
    return bytes(_ULAW[s + 32768] for s in samples)


def ulaw_to_pcm16_sample(u):
    u = ~u & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 7, u & 0x0F
    s = ((mantissa << 3) + 0x84) << exponent
    s -= 0x84
    return -s if sign else s


def wav_to_pcm(wav_bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, 2, 1):
            raise ValueError("page audio must be 8 kHz 16-bit mono")
        return w.readframes(w.getnframes())


# ------------------------------------------------------------------- SIP ---

def _tag():
    return "%08x" % random.getrandbits(32)


def parse_sip(data):
    """Return (first line, {lower header: value}, body) or None."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    if not lines or not lines[0]:
        return None
    headers = {}
    for line in lines[1:]:
        k, sep, v = line.partition(":")
        if sep:
            headers.setdefault(k.strip().lower(), v.strip())
    return lines[0], headers, body


def sdp_target(body):
    """(ip, port) of the far end's audio from an SDP body, or None."""
    ip = port = None
    for line in body.splitlines():
        if line.startswith("c=IN IP4 "):
            ip = line.split()[2]
        elif line.startswith("m=audio "):
            port = int(line.split()[1])
    return (ip, port) if ip and port else None


def local_ip_towards(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class CallFailed(Exception):
    pass


class SipPager:
    """Places one page call at a time. Settings are read from cfg["phone"]
    for every call, so changes on the admin page apply to the next page."""

    built = True

    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self.line = threading.Lock()      # one phone line: one call at a time

    # -- the public entry point used by the receiver's page worker
    def page(self, zone, dial, en, es, audio=None):
        with self.line:
            return self._page(zone, dial, audio)

    def connection_test(self, seconds=3):
        """Call the HT813 and hold the line for a few seconds WITHOUT dialing
        anything, so nobody is paged. Proves laptop -> HT813 -> line works."""
        ph = self.cfg["phone"]
        with self.line:
            try:
                secs = self.call(ph, "", b"\x00\x00" * int(RATE * seconds))
                return True, ("HT813 %s:%s answered, held the line %.0f s and hung up. Nothing was dialed."
                              % (ph["ht813_ip"], ph["sip_port"], secs))
            except CallFailed as e:
                return False, "phone: %s" % e
            except OSError as e:
                return False, "phone: network error: %s" % e

    def _page(self, zone, dial, audio):
        ph = self.cfg["phone"]
        if not dial:
            return False, "zone %s has no dial digits set (admin page, Phone and paging setup)" % zone
        if not re.match(r"^[0-9*#,F]+$", dial):
            return False, "zone %s dial digits are not valid: %s" % (zone, dial)
        if not audio:
            return False, "no page audio"
        try:
            pcm = wav_to_pcm(audio)
            secs = self.call(ph, dial, pcm)
            return True, "dialed %s via HT813 %s, played %.0f s" % (dial, ph["ht813_ip"], secs)
        except CallFailed as e:
            return False, "phone: %s" % e
        except OSError as e:
            return False, "phone: network error: %s" % e

    # -- one call
    def call(self, ph, dial, pcm):
        host, port = ph["ht813_ip"], int(ph["sip_port"])
        lport = int(ph["local_sip_port"])
        deadline = time.monotonic() + int(ph["max_call_s"])
        two_stage = ph.get("dial_method", "2-stage") == "2-stage"
        # 2-stage: call the HT813 by plain address (like Grandstream's direct IP call),
        # or as a user if one is set. 1-stage: the digits are the user part.
        user = ph.get("ht813_user", "") if two_stage else dial.replace(",", "").replace("F", "")
        me = local_ip_towards(host)

        sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sip.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sip.bind(("0.0.0.0", lport))
        lport = sip.getsockname()[1]          # the port really in use (0 = any free one)
        rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp.bind(("0.0.0.0", 0))
        rport = rtp.getsockname()[1]
        try:
            d = Dialog(sip, me, lport, host, port, user)
            far = d.invite(rport, deadline)
            self.log("phone: HT813 answered; audio to %s:%d" % far)
            try:
                stream = RtpStream(rtp, far)
                stream.silence(int(ph["wait_after_answer_ms"]), d)
                if two_stage:
                    for ch in dial:
                        if ch == ",":
                            stream.silence(500, d)
                        else:
                            stream.dtmf(DTMF_EVENTS[ch], int(ph["dtmf_ms"]), d)
                            stream.silence(int(ph["dtmf_gap_ms"]), d)
                stream.silence(int(ph["wait_after_dial_ms"]), d)
                played = stream.play(pcm, d, deadline)
            finally:
                d.bye()
            return played
        finally:
            sip.close()
            rtp.close()


class Dialog:
    """The SIP side of one outgoing call (UAC), just what a page needs."""

    def __init__(self, sock, me, lport, host, port, user):
        self.sock, self.me, self.lport = sock, me, lport
        self.host, self.port, self.user = host, port, user
        self.call_id = "%s@%s" % (_tag() + _tag(), me)
        self.ftag = _tag()
        self.ttag = ""
        self.cseq = 1
        self.uri = ("sip:%s@%s:%d" % (user, host, port)) if user else ("sip:%s:%d" % (host, port))
        self.remote_target = self.uri
        self.hung_up = False

    def _send(self, text):
        self.sock.sendto(text.encode("utf-8"), (self.host, self.port))

    def _headers(self, method, branch, cseq, to_tag=True):
        to = "<%s>" % self.uri + (";tag=%s" % self.ttag if to_tag and self.ttag else "")
        return ("Via: SIP/2.0/UDP %s:%d;branch=z9hG4bK%s;rport\r\n"
                "Max-Forwards: 70\r\n"
                "From: \"P833 Pager\" <sip:p833@%s:%d>;tag=%s\r\n"
                "To: %s\r\n"
                "Call-ID: %s\r\n"
                "CSeq: %d %s\r\n"
                "Contact: <sip:p833@%s:%d>\r\n"
                "User-Agent: P833-Receiver\r\n" % (
                    self.me, self.lport, branch, self.me, self.lport, self.ftag, to, self.call_id,
                    cseq, method, self.me, self.lport))

    def invite(self, rport, deadline):
        sdp = ("v=0\r\no=p833 %d 1 IN IP4 %s\r\ns=P833 page\r\nc=IN IP4 %s\r\nt=0 0\r\n"
               "m=audio %d RTP/AVP %d %d\r\na=rtpmap:0 PCMU/8000\r\n"
               "a=rtpmap:%d telephone-event/8000\r\na=fmtp:%d 0-16\r\na=ptime:20\r\na=sendrecv\r\n" % (
                   random.getrandbits(31), self.me, self.me, rport, PT_PCMU, PT_EVENT, PT_EVENT, PT_EVENT))
        self.branch = _tag() + _tag()
        msg = ("INVITE %s SIP/2.0\r\n" % self.uri + self._headers("INVITE", self.branch, self.cseq, False) +
               "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS\r\n"
               "Content-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s" % (len(sdp), sdp))
        self._send(msg)
        resend_at, interval, got_provisional = time.monotonic() + 0.5, 0.5, False
        answer_by = min(deadline, time.monotonic() + 15)
        while True:
            now = time.monotonic()
            if now > answer_by:
                self._cancel()
                raise CallFailed("HT813 at %s:%d did not answer the call (check its address, FXO SIP "
                                 "port, and that it accepts direct calls)" % (self.host, self.port))
            if not got_provisional and now >= resend_at:      # UDP retransmit until we hear anything
                self._send(msg)
                interval = min(interval * 2, 4)
                resend_at = now + interval
            r = self._recv(min(0.2, max(0.01, answer_by - now)))
            if not r:
                continue
            first, h, body = r
            if not first.startswith("SIP/2.0 ") or not h.get("cseq", "").endswith("INVITE"):
                continue
            code = int(first.split()[1])
            if code < 200:
                got_provisional = True
                continue
            self.ttag = _param(h.get("to", ""), "tag")
            if code >= 300:
                self._ack_failure(h)
                raise CallFailed("HT813 refused the call: %s" % first[8:])
            m = re.search(r"<([^>]+)>", h.get("contact", ""))
            if m:
                self.remote_target = m.group(1)
            self._ack()
            far = sdp_target(body)
            if not far:
                self.bye()
                raise CallFailed("HT813 answered without an audio address")
            return far

    def _ack(self):
        self._send("ACK %s SIP/2.0\r\n" % self.remote_target + self._headers("ACK", _tag() + _tag(), self.cseq) +
                   "Content-Length: 0\r\n\r\n")

    def _ack_failure(self, h):
        # ACK for a non-2xx final response reuses the INVITE's branch and To tag.
        self._send("ACK %s SIP/2.0\r\n" % self.uri + self._headers("ACK", self.branch, self.cseq) +
                   "Content-Length: 0\r\n\r\n")

    def _cancel(self):
        self._send("CANCEL %s SIP/2.0\r\n" % self.uri + self._headers("CANCEL", self.branch, self.cseq, False) +
                   "Content-Length: 0\r\n\r\n")

    def bye(self):
        if self.hung_up:
            return
        self.hung_up = True
        self.cseq += 1
        msg = ("BYE %s SIP/2.0\r\n" % self.remote_target + self._headers("BYE", _tag() + _tag(), self.cseq) +
               "Content-Length: 0\r\n\r\n")
        for wait in (0.5, 1.0, 1.5):            # send, then resend twice if not answered
            self._send(msg)
            end = time.monotonic() + wait
            while time.monotonic() < end:
                r = self._recv(0.1)
                if r and r[0].startswith("SIP/2.0 ") and r[1].get("cseq", "").endswith("BYE"):
                    return

    def check(self):
        """Handle anything the HT813 sends mid-call. Raises if it hung up."""
        while True:
            r = self._recv(0)
            if not r:
                return
            first, h, _ = r
            if first.startswith("BYE "):
                self._reply_ok(h)
                self.hung_up = True
                raise CallFailed("HT813 hung up during the page (line dropped or busy?)")
            if first.startswith(("OPTIONS ", "INFO ", "NOTIFY ")):
                self._reply_ok(h)

    def _reply_ok(self, h):
        self._send("SIP/2.0 200 OK\r\nVia: %s\r\nFrom: %s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %s\r\n"
                   "Content-Length: 0\r\n\r\n" % (h.get("via", ""), h.get("from", ""), h.get("to", ""),
                                                  h.get("call-id", ""), h.get("cseq", "")))

    def _recv(self, timeout):
        rd, _, _ = select.select([self.sock], [], [], timeout)
        if not rd:
            return None
        data, _ = self.sock.recvfrom(8192)
        r = parse_sip(data)
        if r and r[1].get("call-id") not in (None, self.call_id):
            return None
        return r


def _param(header, name):
    m = re.search(r";\s*%s=([^;>\s]+)" % name, header)
    return m.group(1) if m else ""


class RtpStream:
    """Sends 20 ms RTP packets, paced by the clock."""

    def __init__(self, sock, far):
        self.sock, self.far = sock, far
        self.seq = random.getrandbits(16)
        self.ts = random.getrandbits(32)
        self.ssrc = random.getrandbits(32)
        self.next_at = time.monotonic()
        self.first = True

    def _packet(self, pt, payload, marker=False, advance=True):
        hdr = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, self.seq & 0xFFFF,
                          self.ts & 0xFFFFFFFF, self.ssrc)
        self.seq += 1
        if advance:
            self.ts += FRAME
        return hdr + payload

    def _wait_slot(self):
        delay = self.next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_at += 0.020

    def _send_frame(self, ulaw, dialog):
        self._wait_slot()
        self.sock.sendto(self._packet(PT_PCMU, ulaw, marker=self.first), self.far)
        self.first = False
        dialog.check()

    def silence(self, ms, dialog):
        for _ in range(max(0, ms) // 20):
            self._send_frame(b"\xff" * FRAME, dialog)

    def dtmf(self, event, ms, dialog):
        """One RFC 2833 tone: packets every 20 ms with a growing duration, then 3 end packets."""
        start_ts = self.ts
        frames = max(2, ms // 20)
        for i in range(1, frames + 1):
            self._wait_slot()
            payload = struct.pack("!BBH", event, 10, min(i * FRAME, 0xFFFF))      # volume -10 dBm0
            hdr = struct.pack("!BBHII", 0x80, (0x80 if i == 1 else 0) | PT_EVENT, self.seq & 0xFFFF,
                              start_ts & 0xFFFFFFFF, self.ssrc)
            self.seq += 1
            self.sock.sendto(hdr + payload, self.far)
            dialog.check()
        end = struct.pack("!BBH", event, 0x80 | 10, min(frames * FRAME, 0xFFFF))
        for _ in range(3):
            hdr = struct.pack("!BBHII", 0x80, PT_EVENT, self.seq & 0xFFFF, start_ts & 0xFFFFFFFF, self.ssrc)
            self.seq += 1
            self.sock.sendto(hdr + end, self.far)
        self.ts = start_ts + frames * FRAME
        self.first = True        # next audio packet starts a new talkspurt

    def play(self, pcm, dialog, deadline):
        ulaw = pcm16_to_ulaw(pcm)
        n = 0
        for i in range(0, len(ulaw), FRAME):
            if time.monotonic() > deadline:
                raise CallFailed("page longer than the longest call setting; cut off")
            chunk = ulaw[i:i + FRAME]
            if len(chunk) < FRAME:
                chunk += b"\xff" * (FRAME - len(chunk))
            self._send_frame(chunk, dialog)
            n += 1
        return n * 0.020
