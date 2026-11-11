#!/bin/bash
# P833 Call Receiver - installer for AlmaLinux 8 or 9
# By Ethan Brady & Ryan Raikes
#
# Run from the unzipped folder:   sudo bash install/install.sh
# Safe to run again to update. It backs up the old install first, never
# replaces your config.json or secrets.env, and never turns SELinux off.

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
    for p in "$APP" "$ETC" "$UNIT" /var/log/p833-receiver /var/lib/p833-receiver; do
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
install -m 644 receiver/p833_receiver.py "$APP"/
install -m 755 tools/p833_test_sender.py "$APP"/
install -m 644 receiver/config.example.json "$APP"/

# --- Config: keep the existing one; drop the new example beside it.
install -d -m 755 "$ETC"
if [ -e "$ETC/config.json" ]; then
    install -m 644 receiver/config.example.json "$ETC/config.json.new"
    echo "Kept your $ETC/config.json (new example saved as config.json.new)"
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
chown root:root "$ETC/secrets.env"
chmod 600 "$ETC/secrets.env"

# --- Check the config loads before switching over.
"$PY" -c "import sys; sys.path.insert(0, '$APP'); import p833_receiver as r; r.load_config('$ETC/config.json')" \
    || { echo "config.json has an error; fix it and run again"; exit 1; }

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
echo "Done. Status page: http://$(hostname -I | awk '{print $1}'):${PORT}/"
