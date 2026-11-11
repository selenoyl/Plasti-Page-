#!/usr/bin/env python3
# P833 Call Receiver - make the spoken page clips.
# By Ethan Brady & Ryan Raikes
#
# Reads the page wording from a config.json and records every piece with the
# Piper offline voice, in English and Spanish, as phone-quality WAV files
# (8 kHz, 16-bit, mono, trimmed, 200-3400 Hz):
#
#   <out>/<lang>/c<code>_post.wav    "for camera fault."  (codes with a reason)
#   <out>/<lang>/c<code>_nopress.wav the whole press-0 message
#   <out>/<lang>/lead/<start>.wav    "Maintenance requested to press number", one per
#                                    department (distinct start of the message)
#   <out>/<lang>/n/<1..100>.wav      the press number, shared by all departments
#                                    (--max-press; higher presses say "unknown")
#   <out>/chime.wav                  attention chime played before a page
#   <out>/manifest.json              the text of every clip, so the receiver can
#                                    tell when the clips no longer match the config
#
# The receiver joins pieces at page time: chime, English, Spanish, (repeat).
# Only clips whose text changed are re-made, so a wording change is quick.
#
# Needs: Python 3.9+, piper-tts (pip install piper-tts), ffmpeg, and internet
# the first time to download the two voices from huggingface.co.
#
# Example:
#   python3 tools/make_audio.py --config receiver/config.example.json --out audio

import argparse
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

RATE = 8000
DEFAULT_VOICES = {"en": "en_US-hfc_female-medium", "es": "es_MX-claude-high"}
MAX_PRESS = 100     # press numbers recorded: 1 to this; higher presses page as "press unknown"
# Trim silence gently: -60 dB and keep 60 ms before the first sound, so soft
# starts ("five", "six", "seis") are not clipped.
FILTER = ("silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.06,areverse,"
          "silenceremove=start_periods=1:start_threshold=-60dB:start_silence=0.06,areverse,"
          "highpass=f=200,lowpass=f=3400")
# How a voice should SAY a word it would otherwise mispronounce. Only the
# recording changes; the page text stays as written.
PRONOUNCE = {
    "es": {"Red Rabbit": "Red Rábit"},
}


def spoken(lang, text):
    for word, say in PRONOUNCE.get(lang, {}).items():
        text = text.replace(word, say)
    return text


# The receiver's own splitting rules, so the clips always match how it joins them.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "receiver"))
from p833_audio import lead_key, number_key, parts, split_text  # noqa: E402,F401


def clip_plan(cfg, max_press=MAX_PRESS):
    """Every clip to make: {relative path: (lang, text)}.

    'Maintenance requested to press number 27 for camera fault.' is played as
    'Maintenance requested to press number' + '27' + 'for camera fault.'
    The first part is one recording per department (codes with the same start
    share it); the numbers are shared by every department."""
    cfg = dict(cfg, codes={int(k): v for k, v in cfg["codes"].items()})
    plan = {}
    for code, c in sorted(cfg["codes"].items()):
        for lang in ("en", "es"):
            lead, post = parts(cfg, code, lang)
            plan[lead_key(lang, lead)] = (lang, lead)
            if post:
                plan["%s/c%d_post.wav" % (lang, code)] = (lang, post)
            plan["%s/c%d_nopress.wav" % (lang, code)] = (lang, c.get(lang + "_no_press", c[lang]))
    for lang in ("en", "es"):
        for n in range(1, max_press + 1):
            plan[number_key(lang, n)] = (lang, str(n))
    return plan


def make_chime(path):
    """Two soft tones (G5 then E5), 0.25 s each."""
    frames = bytearray()
    for freq in (784.0, 659.3):
        n = int(RATE * 0.25)
        for i in range(n):
            env = min(1.0, i / 200.0, (n - i) / 400.0)
            s = 0.45 * env * math.sin(2 * math.pi * freq * i / RATE)
            frames += struct.pack("<h", int(s * 32767))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))


def main():
    p = argparse.ArgumentParser(description="Make the P833 page audio clips")
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True, help="audio folder to write")
    p.add_argument("--voices-dir", default=os.path.expanduser("~/.cache/p833-voices"))
    p.add_argument("--voice-en", default=DEFAULT_VOICES["en"])
    p.add_argument("--voice-es", default=DEFAULT_VOICES["es"])
    p.add_argument("--length-scale", type=float, default=1.05,
                   help="speaking pace; above 1 is slower (clearer on a PA)")
    p.add_argument("--max-press", type=int, default=MAX_PRESS,
                   help="record press numbers 1 to this (1-999); higher presses page as press unknown")
    p.add_argument("--all", action="store_true", help="re-make every clip, not just changed ones")
    a = p.parse_args()

    try:
        from piper import PiperVoice, SynthesisConfig
        from piper.download_voices import download_voice
    except ImportError:
        sys.exit("Piper is not installed. Run: python3 -m pip install piper-tts")
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is not installed.")

    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    if not 1 <= a.max_press <= 999:
        sys.exit("--max-press must be 1 to 999")
    plan = clip_plan(cfg, a.max_press)
    voices = {"en": a.voice_en, "es": a.voice_es}

    os.makedirs(a.out, exist_ok=True)
    man_path = os.path.join(a.out, "manifest.json")
    try:
        with open(man_path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    same_voices = (old.get("voices") == voices and old.get("length_scale") == a.length_scale
                   and old.get("filter") == FILTER and old.get("pronounce") == PRONOUNCE)
    old_clips = old.get("clips", {}) if same_voices and not a.all else {}

    todo = [k for k, (lang, text) in plan.items()
            if old_clips.get(k) != text or not os.path.exists(os.path.join(a.out, k))]
    print("%d clips in the plan, %d to make" % (len(plan), len(todo)))

    from pathlib import Path
    loaded = {}
    for lang in ("en", "es"):
        if any(plan[k][0] == lang for k in todo):
            Path(a.voices_dir).mkdir(parents=True, exist_ok=True)
            download_voice(voices[lang], Path(a.voices_dir))
            loaded[lang] = PiperVoice.load(os.path.join(a.voices_dir, voices[lang] + ".onnx"))
    syn = SynthesisConfig(length_scale=a.length_scale)

    tmpdir = tempfile.mkdtemp()
    start = time.time()
    try:
        for i, key in enumerate(todo, 1):
            lang, text = plan[key]
            raw = os.path.join(tmpdir, "raw.wav")
            with wave.open(raw, "wb") as w:
                loaded[lang].synthesize_wav(spoken(lang, text), w, syn_config=syn)
            dest = os.path.join(a.out, key)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", raw, "-af", FILTER,
                            "-ar", str(RATE), "-ac", "1", "-c:a", "pcm_s16le",
                            "-bitexact", "-map_metadata", "-1", dest], check=True)
            if i % 200 == 0 or i == len(todo):
                print("  %d/%d (%.0f s)" % (i, len(todo), time.time() - start))
    finally:
        shutil.rmtree(tmpdir)

    # Remove clips no longer in the plan (for example a reason that was dropped).
    for key in old.get("clips", {}):
        if key not in plan and os.path.exists(os.path.join(a.out, key)):
            os.remove(os.path.join(a.out, key))

    # Remove folders left empty by an older clip layout (never the tones).
    for root, dirs, files in os.walk(a.out, topdown=False):
        if root != a.out and not os.listdir(root) and "tones" not in root:
            os.rmdir(root)

    make_chime(os.path.join(a.out, "chime.wav"))
    manifest = {"_about": "P833 page audio. By Ethan Brady & Ryan Raikes. Made by tools/make_audio.py; "
                          "do not edit by hand.",
                "rate": RATE, "voices": voices, "length_scale": a.length_scale, "max_press": a.max_press,
                "filter": FILTER, "pronounce": PRONOUNCE,
                "clips": {k: t for k, (_, t) in sorted(plan.items())}}
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("Done: %s" % a.out)


if __name__ == "__main__":
    main()
