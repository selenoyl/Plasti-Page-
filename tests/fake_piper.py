#!/usr/bin/env python3
# A stand-in for receiver/p833_tts.py, for tests: the same commands and the same
# "serve" protocol, but no real speech. Each text segment becomes 80 frames
# (10 ms) of a steady sample, each pause its exact length of silence.
# By Ethan Brady & Ryan Raikes
#
# Voices: en_US-test-voice, en_US-other-voice, es_MX-test-voice.
# A text containing SLOW makes it wait 3 s; DIE makes it exit at once;
# NOSOUND makes it answer with no audio. Each request it speaks is appended to
# requests.log in the voices folder it was given (a test's temporary folder).

import io
import json
import os
import sys
import time
import wave

VOICES = ["en_US-other-voice", "en_US-test-voice", "es_MX-test-voice"]


def speak(req):
    if req.get("voice") not in VOICES:
        raise ValueError("voice not installed: %s" % req.get("voice"))
    texts = " ".join(s.get("text", "") for s in req.get("segments", []))
    if "SLOW" in texts:
        time.sleep(3)
    if "DIE" in texts:
        os._exit(1)
    n = 0 if "NOSOUND" in texts else sum(80 if "text" in s else 8 * int(s["pause_ms"]) for s in req["segments"])
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x30\x00" * n)
    with open(os.path.join(sys.argv[2], "requests.log"), "a", encoding="utf-8") as f:
        f.write(json.dumps(req) + "\n")
    return out.getvalue()


def main():
    cmd = sys.argv[1]
    if cmd == "list":
        print(json.dumps(VOICES))
    elif cmd == "speak":
        try:
            sys.stdout.buffer.write(speak(json.loads(sys.stdin.read())))
        except ValueError as e:
            sys.stderr.write("%s\n" % e)
            sys.exit(1)
    elif cmd == "serve":
        for line in sys.stdin.buffer:
            try:
                wav = speak(json.loads(line))
                head = {"ok": True, "bytes": len(wav)}
            except Exception as e:
                wav, head = b"", {"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)}
            sys.stdout.buffer.write((json.dumps(head) + "\n").encode() + wav)
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
