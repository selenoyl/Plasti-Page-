#!/usr/bin/env python3
# P833 Call Receiver - Voice lab speech helper (runs Piper on the laptop).
# By Ethan Brady & Ryan Raikes
#
# Runs in the Piper virtual environment made by install/install_tts.sh
# (Python 3.9+), not in the receiver itself, which stays standard-library only.
# The receiver calls it as a separate process:
#
#   <tts venv>/bin/python p833_tts.py list  <voices dir>
#       -> JSON list of installed voices
#   <tts venv>/bin/python p833_tts.py speak <voices dir>   (JSON on stdin)
#       stdin:  {"voice": "en_US-joe-medium", "length_scale": 1.25,
#                "segments": [{"text": "Maintenance requested to press two."},
#                             {"pause_ms": 350}, {"text": "Seven."}]}
#       stdout: WAV, 8 kHz 16-bit mono, phone band (200-3400 Hz), silence trimmed
#   <tts venv>/bin/python p833_tts.py serve <voices dir>
#       Stays running and keeps the voices it has used loaded (loading one takes
#       a second or two; speaking a sentence then takes a fraction of a second).
#       Each request is one JSON line on stdin, as for speak. Each answer is one
#       JSON line on stdout, {"ok": true, "bytes": N} followed by N bytes of WAV,
#       or {"ok": false, "error": "..."}. Ends when stdin closes.
#
# Each text segment is spoken as its own utterance; pauses are exact.

import collections
import io
import json
import os
import sys
import wave

import numpy as np

RATE = 8000


def lowpass_taps(cutoff, rate, n=101):
    """Windowed-sinc low-pass filter taps."""
    t = np.arange(n) - (n - 1) / 2.0
    h = np.sinc(2 * cutoff / rate * t) * np.hamming(n)
    return h / h.sum()


def to_phone(samples, rate):
    """Float audio at the voice's rate -> 8 kHz float, 200-3400 Hz, trimmed."""
    x = np.asarray(samples, dtype=np.float64)
    if not len(x):
        return x
    x = np.convolve(x, lowpass_taps(3400.0, rate), mode="same")          # no aliasing below 8 kHz
    n_out = int(len(x) * RATE / float(rate))
    x = np.interp(np.arange(n_out) * (rate / float(RATE)), np.arange(len(x)), x)
    # 200 Hz high-pass (one-pole), like the phone line
    a = np.exp(-2 * np.pi * 200.0 / RATE)
    y = np.empty_like(x)
    prev_x = prev_y = 0.0
    for i, v in enumerate(x):
        prev_y = a * (prev_y + v - prev_x)
        prev_x = v
        y[i] = prev_y
    # trim leading/trailing silence (-50 dB), keeping 60 ms
    thr = 10 ** (-50 / 20.0)
    loud = np.where(np.abs(y) > thr)[0]
    if not len(loud):
        return y[:0]
    keep = int(0.06 * RATE)
    return y[max(0, loud[0] - keep):loud[-1] + keep]


def list_voices(voices_dir):
    out = []
    for name in sorted(os.listdir(voices_dir)) if os.path.isdir(voices_dir) else []:
        if name.endswith(".onnx") and os.path.exists(os.path.join(voices_dir, name + ".json")):
            out.append(name[:-5])
    return out


_LOADED = collections.OrderedDict()   # voice name -> loaded PiperVoice (serve keeps them), last used last
MAX_LOADED = 3          # about 150 to 300 MB each; the least recently used goes first


def load_voice(voices_dir, voice_name):
    from piper import PiperVoice
    if voice_name not in list_voices(voices_dir):
        raise ValueError("voice not installed: %s" % voice_name)
    if voice_name in _LOADED:
        _LOADED.move_to_end(voice_name)
    else:
        if len(_LOADED) >= MAX_LOADED:
            _LOADED.popitem(last=False)
        _LOADED[voice_name] = PiperVoice.load(os.path.join(voices_dir, voice_name + ".onnx"))
    return _LOADED[voice_name]


def speak(voices_dir, req):
    from piper import SynthesisConfig
    voice = load_voice(voices_dir, str(req["voice"]))
    syn = SynthesisConfig(length_scale=max(0.5, min(2.5, float(req.get("length_scale", 1.0)))))
    parts = []
    for seg in req.get("segments", []):
        if "pause_ms" in seg:
            ms = max(0, min(5000, int(seg["pause_ms"])))
            parts.append(np.zeros(int(RATE * ms / 1000)))
            continue
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        chunks = [c.audio_float_array for c in voice.synthesize(text, syn)]
        if chunks:
            rate = voice.config.sample_rate
            parts.append(to_phone(np.concatenate(chunks), rate))
    audio = np.concatenate(parts) if parts else np.zeros(0)
    peak = np.max(np.abs(audio)) if len(audio) else 0
    if peak > 0:
        audio = audio * (0.89 / peak)          # normalise to -1 dBFS; the receiver applies the page volume
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def serve(voices_dir):
    # If memory runs out, the kernel should stop this helper (the receiver then uses the
    # recorded clips and starts it again), not the receiver.
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("800")
    except OSError:
        pass
    out = sys.stdout.buffer
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            wav = speak(voices_dir, json.loads(line.decode("utf-8")))
            head = {"ok": True, "bytes": len(wav)}
        except Exception as e:          # report it and keep serving
            wav, head = b"", {"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)}
        out.write((json.dumps(head) + "\n").encode("utf-8") + wav)
        out.flush()


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("list", "speak", "serve"):
        raise SystemExit("usage: p833_tts.py list|speak|serve <voices dir>")
    voices_dir = sys.argv[2]
    if sys.argv[1] == "list":
        print(json.dumps(list_voices(voices_dir)))
        return
    if sys.argv[1] == "serve":
        serve(voices_dir)
        return
    try:
        req = json.loads(sys.stdin.read())
        sys.stdout.buffer.write(speak(voices_dir, req))
    except ValueError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
