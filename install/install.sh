#!/bin/bash
# P833 Call Receiver - installer for AlmaLinux 8 or 9
# By Ethan Brady & Ryan Raikes
#
# First setup or update, from the downloaded folder:   sudo bash install/install.sh
# To fetch the newest version from GitHub and install it: sudo bash install/update.sh
#
# Safe to run again any time. It backs up the old install first, never
# replaces your config.json, keeps your secrets.env (only adding anything new
# it needs), keeps the board list and call log, and never turns SELinux off.

set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8833
APP=/opt/p833-receiver
ETC=/etc/p833-receiver
UNIT=/etc/systemd/system/p833-receiver.service

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash install/install.sh"; exit 1
fi

# --- Python: AlmaLinux 9 has /usr/bin/python3; AlmaLinux 8 may only have platform-python.
if [ -x /usr/bin/python3 ]; then
    PY=/usr/bin/python3
elif [ -x /usr/libexec/platform-python ]; then
    PY=/usr/libexec/platform-python
else
    echo "No Python 3 found. Install it: sudo dnf install python3"; exit 1
fi
echo "Python: $PY ($($PY --version 2>&1))"

# --- Back up anything already installed into a new, empty folder.
if [ -e "$APP" ] || [ -e "$ETC" ] || [ -e "$UNIT" ]; then
    BK=/root/p833-backup-$(date +%Y%m%d-%H%M%S)
    mkdir "$BK"          # fails if it already exists, so it is always new and empty
    # The program and settings; not the recordings, Piper or its voices (large, and
    # not changed by this installer).
    if [ -e "$APP" ]; then
        tar -C / --exclude="${APP#/}/audio" --exclude="${APP#/}/tts" --exclude="${APP#/}/voices" \
            -cf - "${APP#/}" | tar -C "$BK" -xf -
    fi
    for p in "$ETC" "$UNIT" /var/log/p833-receiver /var/lib/p833-receiver; do
        [ -e "$p" ] && cp -a --parents "$p" "$BK"/
    done
    chmod 700 "$BK"
    echo "Backed up the old install to $BK"
fi

# --- Service user (no login, no home).
if ! id p833 >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /sbin/nologin p833
    echo "Created user p833"
fi

# --- Program files.
install -d -m 755 "$APP"
# Every receiver module (p833_receiver.py, p833_audio.py, p833_sip.py, ...).
install -m 644 receiver/*.py "$APP"/
install -m 755 tools/p833_test_sender.py "$APP"/
install -m 644 receiver/config.example.json "$APP"/
# Recorded page audio (made by tools/make_audio.py). Replaced as a whole.
if [ -f audio/manifest.json ]; then
    rm -rf "$APP/audio.new"
    cp -r audio "$APP/audio.new"
    find "$APP/audio.new" -type d -exec chmod 755 {} +
    find "$APP/audio.new" -type f -exec chmod 644 {} +
    rm -rf "$APP/audio"
    mv "$APP/audio.new" "$APP/audio"
    echo "Audio: $(find "$APP/audio" -name '*.wav' | wc -l) clips installed"
else
    echo "Audio: no recorded clips in this folder; pages will have no sound until they are added"
fi

# --- Config: keep the existing one; drop the new example beside it.
install -d -m 755 "$ETC"
if [ -e "$ETC/config.json" ]; then
    install -m 644 receiver/config.example.json "$ETC/config.json.new"
    echo "Kept your $ETC/config.json (new example saved as config.json.new)"
    "$PY" install/migrate_config.py "$ETC/config.json" receiver/config.example.json
    chmod 644 "$ETC/config.json"
else
    install -m 644 receiver/config.example.json "$ETC/config.json"
    echo "Installed $ETC/config.json (dry run is ON)"
fi

# --- Secrets: root-only. Make a random board key the first time.
if [ ! -e "$ETC/secrets.env" ]; then
    KEY=$($PY -c 'import secrets; print(secrets.token_hex(16))')
    ( umask 077; printf '# P833 receiver secrets. Root-only. By Ethan Brady & Ryan Raikes\nP833_BOARD_KEY=%s\n' "$KEY" > "$ETC/secrets.env" )
    echo "Made a new board key in $ETC/secrets.env (see it with: sudo cat $ETC/secrets.env)"
fi
# Admin page password (added on update if an older install has none).
if ! grep -q '^P833_ADMIN_PASSWORD=' "$ETC/secrets.env"; then
    PW=$($PY -c 'import secrets; print(secrets.token_urlsafe(9))')
    printf 'P833_ADMIN_PASSWORD=%s\n' "$PW" >> "$ETC/secrets.env"
    echo
    echo "  Admin page password: $PW"
    echo "  (user name: anything. See it again with: sudo grep ADMIN $ETC/secrets.env)"
    echo
fi
chown root:root "$ETC/secrets.env"
chmod 600 "$ETC/secrets.env"

# --- Check the config loads before switching over.
# Check the program loads and the config is valid before switching over. The
# Python error is shown as it is, so a missing file isn't mistaken for a bad config.
if ! "$PY" -c "import sys; sys.path.insert(0, '$APP'); import p833_receiver as r; r.load_config('$ETC/config.json')"; then
    echo
    echo "STOPPED: the receiver program or $ETC/config.json did not load (error above)."
    echo "The service was not restarted, so the version already running keeps running."
    exit 1
fi

# --- systemd service.
sed "s#@PYTHON@#$PY#" install/p833-receiver.service > "$UNIT"
chmod 644 "$UNIT"

# --- SELinux: keep it on; just give the new files their normal labels.
if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$APP" "$ETC" "$UNIT"
fi
echo "SELinux: $(getenforce 2>/dev/null || echo unknown) (left as it is)"

# --- firewalld: open only the receiver's port.
if systemctl is-active --quiet firewalld; then
    firewall-cmd --quiet --permanent --add-port=${PORT}/tcp
    firewall-cmd --quiet --reload
    echo "firewalld: opened ${PORT}/tcp"
else
    echo "firewalld is not running; nothing opened"
fi

systemctl daemon-reload
systemctl enable p833-receiver >/dev/null 2>&1
systemctl restart p833-receiver
sleep 2
systemctl --no-pager --lines=5 status p833-receiver || true
echo
IP=$(hostname -I | awk '{print $1}')
echo "Done. Version $(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$APP/p833_receiver.py") installed."
echo "  Status page: http://${IP}:${PORT}/"
echo "  Admin page:  http://${IP}:${PORT}/admin"
