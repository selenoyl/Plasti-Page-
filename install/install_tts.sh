#!/bin/bash
# P833 Call Receiver - set up the Voice lab (Piper speech) on the laptop.
# By Ethan Brady & Ryan Raikes
#
# Run once, after install.sh, from the downloaded folder:
#   sudo bash install/install_tts.sh
# Optional: name the voices to install (default: the list below), e.g.
#   sudo bash install/install_tts.sh en_US-joe-medium es_MX-claude-high
#
# Needs internet (Python packages from PyPI, voices from huggingface.co) and
# Python 3.9 or newer. Puts everything under /opt/p833-receiver/tts (Piper) and
# /opt/p833-receiver/voices (about 60 MB per voice). Safe to run again; it only
# adds what's missing. The receiver itself is not changed or restarted.

set -euo pipefail
APP=/opt/p833-receiver
VENV=$APP/tts
VOICES=$APP/voices
SUMS="$(cd "$(dirname "$0")" && pwd)/voices.sha256"
DEFAULT_VOICES="en_US-hfc_female-medium en_US-joe-medium en_US-john-medium en_US-mike-medium
en_US-norman-medium en_US-hfc_male-medium es_MX-claude-high es_MX-ald-medium es_ES-davefx-medium"

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash install/install_tts.sh"; exit 1; fi
if [ ! -d "$APP" ]; then echo "Install the receiver first: sudo bash install/install.sh"; exit 1; fi

# --- A Python 3.9+ (AlmaLinux 8 ships 3.6 as python3; 3.9/3.11/3.12 are in AppStream).
PY=""
for p in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
        PY=$(command -v "$p"); break
    fi
done
if [ -z "$PY" ]; then
    echo "Piper needs Python 3.9 or newer. Install one, then run this again:"
    echo "  sudo dnf install -y python3.11"
    exit 1
fi
echo "Python for Piper: $PY ($($PY --version 2>&1))"

# --- Piper in its own environment (the receiver stays standard-library only).
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV"
fi
# Pinned to the version this was built and tested with (not "newest").
PIPER_VERSION=1.8.0
if ! "$VENV/bin/python" -I -m pip install --quiet "piper-tts==$PIPER_VERSION"; then
    # an old pip may not know the newer package formats; update pip once and retry
    "$VENV/bin/python" -I -m pip install --quiet --upgrade "pip<26"
    "$VENV/bin/python" -I -m pip install --quiet "piper-tts==$PIPER_VERSION"
fi
echo "Piper: $("$VENV/bin/python" -I -m pip show piper-tts | sed -n 's/^Version: //p')"

# --- Voices.
mkdir -p "$VOICES"
if [ $# -gt 0 ]; then WANT="$*"; else WANT=$DEFAULT_VOICES; fi
for v in $WANT; do
    if [ -f "$VOICES/$v.onnx" ] && [ -f "$VOICES/$v.onnx.json" ]; then
        echo "voice $v: already installed"
    else
        echo "voice $v: downloading..."
        "$VENV/bin/python" -I -m piper.download_voices --download-dir "$VOICES" "$v" \
            || echo "  could not download $v (check the name and the internet connection)"
    fi
    # Check the files against the checksums on file (install/voices.sha256).
    for f in "$v.onnx" "$v.onnx.json"; do
        [ -f "$VOICES/$f" ] || continue
        want=$(awk -v f="$f" '$2 == f {print $1}' "$SUMS")
        if [ -z "$want" ]; then
            echo "  $f: no checksum on file (not one of the default voices); not checked"
        elif [ "$(sha256sum "$VOICES/$f" | cut -d' ' -f1)" != "$want" ]; then
            echo "  $f: CHECKSUM DOES NOT MATCH; removed. (The voice may have changed upstream.)"
            rm -f "$VOICES/$f"
        fi
    done
done

chmod -R a+rX "$VENV" "$VOICES"
command -v restorecon >/dev/null 2>&1 && restorecon -R "$VENV" "$VOICES"

# --- Check it speaks.
echo
echo "Installed voices: $("$VENV/bin/python" "$APP/p833_tts.py" list "$VOICES")"
FIRST=$(echo "$WANT" | awk '{print $1; exit}')
if echo '{"voice": "'"$FIRST"'", "segments": [{"text": "Test."}]}' \
        | "$VENV/bin/python" "$APP/p833_tts.py" speak "$VOICES" | head -c 4 | grep -q RIFF; then
    echo "Voice lab ready. Refresh the admin page; it's the last box."
else
    echo "Piper installed but the test phrase failed; see the error above."
    exit 1
fi
