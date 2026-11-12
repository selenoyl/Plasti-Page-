#!/usr/bin/env python3
# P833 Call Receiver - make the "ding dong" doorbell tone (Maintenance's department tone).
# By Ethan Brady & Ryan Raikes
#
#   python3 tools/make_doorbell.py            -> audio/tones/single/6_maintenance_doorbell.wav
#
# Two struck chime bars, like a classic door chime: "ding" on E5 (659 Hz), then "dong"
# a major third lower on C5 (523 Hz), the first still ringing under the second. Each bar
# has the partials of a free metal bar (1, 2.76 and 5.40 times the note), the higher ones
# fading faster. 8 kHz, 16-bit mono, the same loudness as the other department tones
# (about -20 dBFS average), no gap inside it and a short ring at the end: a paging
# feedback eliminator ends a page at about 0.7 s of near-silence. Standard library only.

import math
import os
import struct
import sys
import wave

RATE = 8000
LENGTH_S = 1.5
DING, DONG = 659.25, 523.25        # E5, C5
DONG_AT_S = 0.45
PARTIALS = [(1.0, 1.0, 1.0), (2.756, 0.45, 0.55), (5.404, 0.18, 0.3)]   # (ratio, level, decay factor)
TARGET_DB = -20.0                  # average level of the loud part, like the other tones


def strike(freq, start, decay_s, level, n):
    out = [0.0] * n
    s0 = int(start * RATE)
    for ratio, amp, dk in PARTIALS:
        f = freq * ratio
        if f >= RATE / 2 - 200:            # nothing near or above the 4 kHz limit
            continue
        tau = decay_s * dk
        for i in range(s0, n):
            t = (i - s0) / RATE
            attack = min(1.0, t / 0.003)   # 3 ms: struck, not clicked
            out[i] += level * amp * attack * math.exp(-t / tau) * math.sin(2 * math.pi * f * t)
    return out


def make(path):
    n = int(LENGTH_S * RATE)
    ding = strike(DING, 0.0, 0.3, 1.0, n)
    dong = strike(DONG, DONG_AT_S, 0.35, 1.0, n)
    x = [a + b for a, b in zip(ding, dong)]
    fade = int(0.08 * RATE)                # no click at the end
    for i in range(fade):
        x[n - 1 - i] *= i / fade
    loud = [v * v for v in x if abs(v) > 1e-3 * max(abs(v) for v in x)]
    g = 10 ** (TARGET_DB / 20.0) / math.sqrt(sum(loud) / len(loud))
    peak = max(abs(v) for v in x) * g
    if peak > 0.89:                        # never above -1 dBFS
        g *= 0.89 / peak
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(round(v * g * 32767))) for v in x))


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "audio", "tones", "single",
                                                              "6_maintenance_doorbell.wav")
    make(out)
    print("wrote", os.path.normpath(out))
