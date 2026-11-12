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
import math
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


_ULAW_DECODE = [ulaw_to_pcm16_sample(u) for u in range(256)]


def ulaw_to_pcm16(data):
    return struct.pack("<%dh" % len(data), *[_ULAW_DECODE[b] for b in data])


def pcm_to_wav(pcm):
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()


# ------------------------------------------------------------- line check ---
# After dialing, listen to what the Norstar line sends back. North American call
# progress tones: dial tone 350+440 Hz (steady), busy 480+620 (0.5 s on/off),
# reorder 480+620 (0.25 s), ringback 440+480 (2 s on, 4 s off). An open page is
# quiet (or a short page beep). 80 ms blocks with a Hann window separate 440/480.

BLOCK = 640
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (BLOCK - 1)) for i in range(BLOCK)]


def _goertzel(x, freq):
    k = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = 0.0
    for v, w in zip(x, _HANN):
        s0 = v * w + k * s1 - s2
        s2, s1 = s1, s0
    return s1 * s1 + s2 * s2 - k * s1 * s2


_CAL = _goertzel([math.sin(2 * math.pi * 1000 * i / RATE) for i in range(BLOCK)], 1000)   # unit sine


def classify_block(x):
    """'dial', 'busy', 'ring', 'quiet' or 'other' for one 80 ms block of samples."""
    ms = sum(v * v for v in x) / len(x)
    if ms < 150 ** 2:                    # about -47 dBFS: nothing there
        return "quiet"
    r = {f: _goertzel(x, f) / (_CAL * 2 * ms) for f in (350, 440, 480, 620)}
    if r[350] > 0.2 and r[440] > 0.2 and r[480] < 0.12 and r[620] < 0.12:
        return "dial"
    if r[480] > 0.2 and r[620] > 0.2 and r[350] < 0.12:
        return "busy"
    if r[440] > 0.2 and r[480] > 0.2 and r[350] < 0.12 and r[620] < 0.12:
        return "ring"
    return "other"


def line_check(ulaw_frames):
    """Look at what the line sent back while we waited after dialing.
    Returns '' if nothing says the page failed, or the reason it didn't open."""
    pcm = b"".join(ulaw_frames)
    samples = [_ULAW_DECODE[b] for b in pcm]
    blocks = [samples[i:i + BLOCK] for i in range(0, len(samples) - BLOCK + 1, BLOCK)]
    if len(blocks) < 5:                  # under 0.4 s heard: not enough to judge
        return ""
    kinds = [classify_block(b) for b in blocks]
    n = float(len(kinds))
    if kinds.count("dial") / n >= 0.6:
        return "the line still had dial tone after dialing (the hook flash or the digits weren't taken)"
    if kinds.count("busy") / n >= 0.3:
        return "the line sent a busy or reorder tone after dialing (page zone busy, or the code was refused)"
    if kinds.count("ring") / n >= 0.3:
        return "the line was ringing after dialing (it called a phone instead of opening the page)"
    return ""


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
        self.last_end = 0.0               # when the last call hung up (for the gap between calls)
        self.last_check = ""              # what the line check found on the last call
        self.last_recording = b""         # what the line sent back during the last test (WAV)

    # -- the public entry point used by the receiver's page worker
    def page(self, zone, dial, en, es, audio=None):
        with self.line:
            return self._page(zone, dial, audio)

    def connection_test(self, seconds=3, digits="", wav=None, what="the bell"):
        """With no digits: call the HT813 and hold the line for a few seconds
        WITHOUT dialing, so nobody is paged (proves laptop -> HT813 -> line).
        With digits: dial them, then play `wav` (a short tone), to find the
        page code by trying candidates."""
        ph = self.cfg["phone"]
        if digits and not re.match(r"^[0-9*#,F]{1,32}$", digits):
            return False, "digits may only be 0-9 * # , F (got %s)" % digits
        try:
            pcm = wav_to_pcm(wav) if (digits and wav) else b"\x00\x00" * int(RATE * seconds)
        except ValueError as e:
            return False, str(e)
        with self.line:
            self.last_recording = b""
            try:
                secs = self.call(ph, digits, pcm, record=True)
                if digits:
                    return True, ("dialed %s through HT813 %s:%s, waited %.1f s, then played %s for %.0f s and "
                                  "hung up. Did the floor speakers play it?"
                                  % (digits, ph["ht813_ip"], ph["sip_port"],
                                     int(ph["wait_after_dial_ms"]) / 1000.0, what, secs))
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
            self.last_recording = b""
            secs = self.call(ph, dial, pcm, record=True)     # keep what the line sent back, for checking
            warn = ("; WARNING: " + self.last_check) if self.last_check else ""
            return True, "dialed %s via HT813 %s, waited %.1f s, played %.0f s%s" % (
                dial, ph["ht813_ip"], int(ph["wait_after_dial_ms"]) / 1000.0, secs, warn)
        except CallFailed as e:
            return False, "phone: %s" % e
        except OSError as e:
            return False, "phone: network error: %s" % e

    # -- one call
    def call(self, ph, dial, pcm, record=False):
        host, port = ph["ht813_ip"], int(ph["sip_port"])
        lport = int(ph["local_sip_port"])
        deadline = time.monotonic() + int(ph["max_call_s"])
        two_stage = ph.get("dial_method", "2-stage") == "2-stage"
        # 2-stage: call the HT813 by plain address (like Grandstream's direct IP call),
        # or as a user if one is set. 1-stage: the digits are the user part.
        user = ph.get("ht813_user", "") if two_stage else dial.replace(",", "").replace("F", "")
        me = local_ip_towards(host)
        # Give the HT813 and the Norstar time to release the line after the last call
        # (the HT813 waits 4 s between outgoing calls by default).
        wait = self.last_end + float(ph.get("min_gap_s", 6)) - time.monotonic()
        if wait > 0:
            self.log("phone: waiting %.1f s for the line to settle after the last call" % wait)
            time.sleep(wait)
        self.last_check = ""

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
                stream = RtpStream(rtp, far, record)
                stream.silence(int(ph["wait_after_answer_ms"]), d)
                if two_stage:
                    for ch in dial:
                        if ch == ",":
                            stream.silence(500, d)
                        else:
                            stream.dtmf(DTMF_EVENTS[ch], int(ph["dtmf_ms"]), d)
                            stream.silence(int(ph["dtmf_gap_ms"]), d)
                mark = len(stream.got)
                stream.silence(int(ph["wait_after_dial_ms"]), d)
                if two_stage and dial and ph.get("line_check", "warn") != "off":
                    found = line_check(stream.got[mark:])
                    if found:
                        self.last_check = found
                        self.log("phone: line check: %s" % found)
                        if ph.get("line_check", "warn") == "retry":
                            raise CallFailed("page did not open: " + found)
                played = stream.play(pcm, d, deadline)
            finally:
                d.bye()
                self.last_end = time.monotonic()
                if record:
                    self.last_recording = pcm_to_wav(stream.heard())
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

    def __init__(self, sock, far, record=False):
        self.sock, self.far = sock, far
        self.record = True          # always keep what the line sends: the line check needs it
        self.got = []              # PCMU payloads received from the HT813 (what the line sends back)
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
        self._drain()

    def _drain(self):
        """Read (and, when recording, keep) the audio the HT813 sends us."""
        while True:
            rd, _, _ = select.select([self.sock], [], [], 0)
            if not rd:
                return
            try:
                data, _ = self.sock.recvfrom(2048)
            except OSError:
                return
            if self.record and len(data) > 12 and (data[1] & 0x7F) == PT_PCMU and len(self.got) < 3000:
                cc = data[0] & 0x0F
                self.got.append(data[12 + 4 * cc:])

    def heard(self):
        return ulaw_to_pcm16(b"".join(self.got))

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
