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
#
# Optional: let only some networks reach the receiver's port (firewalld), e.g. the
# IoT network the boards are on and the PC you use for the admin page:
#   sudo P833_ALLOW_FROM="BOARDS_NETWORK ADMIN_PC" bash install/install.sh
# with real addresses in place of the two words (for example 10.180.0.0/24 for a network).
# The choice is saved in /etc/p833-receiver/allow_from and used by later updates.
# P833_ALLOW_FROM=any opens the port to every network again.

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

# --- Check P833_ALLOW_FROM now, before anything changes (a typo must not lock the boards out).
if [ -n "${P833_ALLOW_FROM:-}" ] && [ "$P833_ALLOW_FROM" != any ]; then
    for src in $P833_ALLOW_FROM; do
        if ! "$PY" -I -c 'import ipaddress, sys; ipaddress.ip_network(sys.argv[1], strict=False)' "$src" 2>/dev/null; then
            echo "STOPPED: P833_ALLOW_FROM has '$src', which isn't an address or network (like 10.180.0.0/24"
            echo "or 10.180.0.25). Nothing was changed."
            exit 1
        fi
    done
fi

# --- Back up anything already installed into a new, empty folder.
if [ -e "$APP" ] || [ -e "$ETC" ] || [ -e "$UNIT" ]; then
    BK=$(mktemp -d "/root/p833-backup-$(date +%Y%m%d-%H%M%S)-XXXX")   # always a new, empty folder
    # The program and settings; not the recordings, Piper or its voices (large, and
    # not changed by this installer).
    if [ -e "$APP" ]; then
        tar -C / --exclude="${APP#/}/audio" --exclude="${APP#/}/tts" --exclude="${APP#/}/voices" \
            -cf - "${APP#/}" | tar -C "$BK" -xf -
    fi
    for p in "$ETC" "$UNIT" /var/log/p833-receiver; do
        [ -e "$p" ] && cp -a --parents "$p" "$BK"/
    done
    # The board list, settings and so on; not the kept page audio (it is made again as needed).
    if [ -e /var/lib/p833-receiver ]; then
        tar -C / --exclude=var/lib/p833-receiver/voice_cache -cf - var/lib/p833-receiver | tar -C "$BK" -xf -
    fi
    chmod 700 "$BK"
    echo "Backed up the old install to $BK"
fi

# --- Service user (no login, no home).
if ! id p833 >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /sbin/nologin p833
    echo "Created user p833"
fi

# --- Program files: staged first, and only put in place once they load (below).
# From a git clone, only the files git tracks are installed (no stray .py files).
if git -c safe.directory="$PWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    MODULES=$(git -c safe.directory="$PWD" ls-files -- 'receiver/*.py')
else
    MODULES=$(ls receiver/*.py)
fi
STAGE=$(mktemp -d /root/p833-stage.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT
# shellcheck disable=SC2086
install -m 644 $MODULES "$STAGE"/
install -m 755 tools/p833_test_sender.py "$STAGE"/
install -m 644 receiver/config.example.json "$STAGE"/
install -d -m 755 "$APP"
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
    "$PY" -I install/migrate_config.py "$ETC/config.json" receiver/config.example.json
    chmod 644 "$ETC/config.json"
else
    install -m 644 receiver/config.example.json "$ETC/config.json"
    echo "Installed $ETC/config.json (dry run is ON)"
fi

# --- Secrets: root-only. A missing or EMPTY value is filled in (the receiver refuses
# calls without a board key, and keeps the admin page off without a password).
if [ ! -e "$ETC/secrets.env" ]; then
    ( umask 077; printf '# P833 receiver secrets. Root-only. By Ethan Brady & Ryan Raikes\n' > "$ETC/secrets.env" )
fi
chown root:root "$ETC/secrets.env"
chmod 600 "$ETC/secrets.env"
if ! grep -Eq '^P833_BOARD_KEY=.+' "$ETC/secrets.env"; then
    sed -i '/^P833_BOARD_KEY=/d' "$ETC/secrets.env"
    KEY=$("$PY" -I -c 'import secrets; print(secrets.token_hex(16))')
    printf 'P833_BOARD_KEY=%s\n' "$KEY" >> "$ETC/secrets.env"
    echo "Made a new board key in $ETC/secrets.env (see it with: sudo cat $ETC/secrets.env)."
    echo "  Every board needs this key: set it on each board's serial console ('key ...')."
fi
if ! grep -Eq '^P833_ADMIN_PASSWORD=.+' "$ETC/secrets.env"; then
    sed -i '/^P833_ADMIN_PASSWORD=/d' "$ETC/secrets.env"
    PW=$("$PY" -I -c 'import secrets; print(secrets.token_urlsafe(12))')
    printf 'P833_ADMIN_PASSWORD=%s\n' "$PW" >> "$ETC/secrets.env"
    echo
    echo "  Admin page password: $PW"
    echo "  (user name: anything. See it again with: sudo grep ADMIN $ETC/secrets.env)"
    echo
fi

# --- HTTPS certificate for the admin page (self-signed, made once, 10 years).
# Boards keep using plain HTTP on the same port; only the admin page moves to https://.
TLS=$ETC/tls
install -d -m 750 -o root -g p833 "$TLS"
if [ ! -s "$TLS/cert.pem" ] || [ ! -s "$TLS/key.pem" ]; then
    command -v openssl >/dev/null || dnf install -y openssl
    SAN="DNS:$(hostname)"
    for ip in $(hostname -I 2>/dev/null); do
        case "$ip" in *:*) ;; *) SAN="$SAN,IP:$ip" ;; esac
    done
    ( umask 077
      openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=p833-receiver" \
          -addext "subjectAltName=$SAN" -keyout "$TLS/key.pem" -out "$TLS/cert.pem" 2>/dev/null \
      || openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=p833-receiver" \
          -keyout "$TLS/key.pem" -out "$TLS/cert.pem" 2>/dev/null )
    echo "Made the admin page's HTTPS certificate in $TLS"
fi
chown root:p833 "$TLS/key.pem" "$TLS/cert.pem"
chmod 640 "$TLS/key.pem"
chmod 644 "$TLS/cert.pem"

# --- Check the NEW program loads and the config is valid, before anything is replaced.
# The Python error is shown as it is, so a missing file isn't mistaken for a bad config.
if ! "$PY" -I -B -c "import sys; sys.path.insert(0, '$STAGE'); import p833_receiver as r; r.load_config('$ETC/config.json')"; then
    echo
    echo "STOPPED: the new receiver program or $ETC/config.json did not load (error above)."
    echo "Nothing was replaced and the service was not restarted: the version running keeps running."
    exit 1
fi
# Now put the program in place: copy the new files over, then remove old modules that
# are no longer part of it.
cp -p "$STAGE"/*.py "$STAGE"/config.example.json "$APP"/
for f in "$APP"/*.py; do
    [ -e "$STAGE/$(basename "$f")" ] || rm -f "$f"
done
rm -rf "$APP/__pycache__"
chown root:root "$APP"/*.py "$APP"/config.example.json

# --- State and logs: readable only by the service (and root). They hold staff phone
# numbers, the voip.ms account, the text log and line recordings.
for d in /var/lib/p833-receiver /var/log/p833-receiver; do
    if [ -d "$d" ]; then
        chmod 750 "$d"
        find "$d" -type f -exec chmod o-rwx,g-w {} +
    fi
done

# --- systemd service.
sed "s#@PYTHON@#$PY#" install/p833-receiver.service > "$UNIT"
chmod 644 "$UNIT"

# --- SELinux: keep it on; just give the new files their normal labels.
if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$APP" "$ETC" "$UNIT"
fi
echo "SELinux: $(getenforce 2>/dev/null || echo unknown) (left as it is)"

# --- firewalld: open only the receiver's port; to every network, or only to the
# networks in P833_ALLOW_FROM (saved for later updates).
if [ -n "${P833_ALLOW_FROM:-}" ]; then
    echo "$P833_ALLOW_FROM" > "$ETC/allow_from"
    chmod 644 "$ETC/allow_from"
fi
ALLOW=$(cat "$ETC/allow_from" 2>/dev/null || echo any)
if systemctl is-active --quiet firewalld; then
    # Remove any earlier rules for the port, then add the ones wanted now.
    firewall-cmd --quiet --permanent --remove-port=${PORT}/tcp 2>/dev/null || true
    { firewall-cmd --permanent --list-rich-rules | grep "port=\"${PORT}\"" || true; } | while read -r rule; do
        firewall-cmd --quiet --permanent --remove-rich-rule="$rule"
    done
    if [ "$ALLOW" = "any" ]; then
        firewall-cmd --quiet --permanent --add-port=${PORT}/tcp
        echo "firewalld: opened ${PORT}/tcp to EVERY network."
        echo "  WARNING: any PC on the network can reach the receiver. Once the boards' network and your"
        echo "  admin PC are known, limit it: sudo P833_ALLOW_FROM=\"<boards' network> <admin PC>\" bash install/install.sh"
    else
        for src in $ALLOW; do
            fam=ipv4; case "$src" in *:*) fam=ipv6 ;; esac
            firewall-cmd --quiet --permanent --add-rich-rule="rule family=\"$fam\" source address=\"$src\" port port=\"${PORT}\" protocol=\"tcp\" accept"
        done
        echo "firewalld: opened ${PORT}/tcp only to: $ALLOW"
    fi
    firewall-cmd --quiet --reload
else
    echo "WARNING: firewalld is not running, so port ${PORT} is open to every host."
    echo "  Turn it on and run this installer again: sudo systemctl enable --now firewalld"
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
echo "  Admin page:  https://${IP}:${PORT}/admin"
echo "    The browser warns once about the certificate (it is made by this laptop, not bought)."
echo "    Check it matches this fingerprint, then choose Advanced > Proceed:"
echo "    $(openssl x509 -in "$TLS/cert.pem" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)"
