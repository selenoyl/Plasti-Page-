#!/bin/bash
# P833 Call Receiver - update to the newest version from GitHub, then install it.
# By Ethan Brady & Ryan Raikes
#
# By hand, from the folder you cloned:   sudo bash install/update.sh
#   (add -y to skip the "install these changes?" question)
# Weekly, by itself: the p833-update timer runs a copy of this script (installed by
# install.sh as /opt/p833-receiver/p833-update.sh) with --auto --repo <clone folder>.
#
# Needs internet. Your config, secrets, board list and call log are kept, and the
# installer backs up the old install and checks the new version starts before it
# switches over.
#
# Safety: git runs as the folder's owner (not root), with hooks off. With --auto it
# installs only when GitHub has something new AND the folder is exactly as GitHub
# has it (no file changed by hand, the same GitHub address as when it was set up),
# and it waits until no page is going out before the receiver restarts.

set -euo pipefail

YES=no AUTO=no REPO=""
while [ $# -gt 0 ]; do
    case "$1" in
        -y) YES=yes ;;
        --auto) AUTO=yes; YES=yes ;;
        --repo) REPO="$2"; shift ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
    shift
done
cd "${REPO:-$(dirname "$0")/..}"

say() { echo "$(date '+%Y-%m-%d %H:%M') $*"; }

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash install/update.sh"; exit 1; fi
if [ ! -d .git ]; then
    echo "This folder was not made with git clone, so it cannot update itself."
    echo "Download the new version, unzip it into a new folder, and run install/install.sh there."
    exit 1
fi
command -v git >/dev/null || dnf install -y git

# Older versions of this script added the folder to root's git settings; take it out.
if git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$PWD"; then
    git config --global --unset-all safe.directory "^$(printf '%s' "$PWD" | sed 's/[][\.*^$/]/\\&/g')\$" || true
fi

# git runs as the folder's owner, so nothing in it ends up owned by root (older versions
# ran it as root: those files are given back), and no hooks run.
OWNER=$(stat -c %U .)
if [ "$OWNER" = root ]; then
    g() { git -c core.hooksPath=/dev/null -c core.fsmonitor= --no-pager "$@"; }
else
    find .git ! -user "$OWNER" -exec chown "$OWNER": {} + 2>/dev/null || true
    OWNER_HOME=$(getent passwd "$OWNER" | cut -d: -f6)
    g() { runuser -u "$OWNER" -- env HOME="$OWNER_HOME" git -c core.hooksPath=/dev/null -c core.fsmonitor= \
              --no-pager "$@"; }
fi

BRANCH=$(g rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = HEAD ]; then
    say "Update stopped: this folder isn't on a branch (git checkout main, then try again)."
    exit 1
fi

if [ "$AUTO" = yes ]; then
    # The same GitHub address as when weekly updates were set up (install.sh notes it).
    WANT=$(sed -n 's/^origin=//p' /etc/p833-receiver/update.conf 2>/dev/null || true)
    HAVE=$(g remote get-url origin)
    if [ -n "$WANT" ] && [ "$HAVE" != "$WANT" ]; then
        say "Update stopped: this folder now gets updates from $HAVE, not $WANT as set up."
        exit 1
    fi
    if [ -n "$(g status --porcelain --untracked-files=no)" ]; then
        say "Update stopped: files in $PWD were changed by hand, so it isn't exactly what GitHub has."
        say "See them with: git status. To put them back: git checkout -- .  (then it updates next week)"
        exit 1
    fi
elif [ "$OWNER" != root ]; then
    echo "Note: this folder belongs to '$OWNER'. Anyone who can log in as '$OWNER' can change what"
    echo "root installs from it. For the most safety, keep the clone in a folder only root can change."
    echo
fi

OLD=$(g rev-parse --short HEAD)
if ! g fetch --quiet origin "$BRANCH"; then
    say "Update stopped: couldn't reach GitHub (no internet?). Nothing changed."
    exit 1
fi
if ! g merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    say "Update stopped: this folder has commits GitHub doesn't, so it can't simply move forward."
    exit 1
fi
NEW=$(g rev-parse --short "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
    if [ "$AUTO" = yes ]; then
        say "Checked GitHub: already the newest version ($NEW). Nothing to do."
        exit 0
    fi
    echo "Already the newest version ($NEW). Reinstalling anyway to be sure everything is in place."
else
    say "Changes from $OLD to $NEW:"
    g log --oneline "$OLD..origin/$BRANCH"
    echo
    echo "Files changed:"
    g diff --stat "$OLD" "origin/$BRANCH" | tail -n 25
    echo
    if [ "$YES" != yes ]; then
        read -r -p "Install these changes? [y/N] " ans
        case "$ans" in y|Y|yes|YES) ;; *) echo "Nothing changed."; exit 0 ;; esac
    fi
    if ! g merge --ff-only --quiet "origin/$BRANCH"; then
        echo
        say "Update stopped: files in this folder were changed by hand, so git will not overwrite them."
        echo "See what changed with: git status"
        echo "To throw those changes away and update: git checkout -- . && sudo bash install/update.sh"
        exit 1
    fi
    if [ "$(g rev-parse HEAD)" != "$(g rev-parse "origin/$BRANCH")" ]; then
        say "Update stopped: after updating, this folder doesn't match GitHub."
        exit 1
    fi
fi

if [ "$AUTO" = yes ]; then
    # Don't restart the receiver in the middle of a page: wait (up to 15 minutes) until no
    # page is waiting or going out. Boards resend their calls anyway if it's busy restarting.
    PORT=$(python3 -I -c 'import json; print(json.load(open("/etc/p833-receiver/config.json")).get("port", 8833))' \
           2>/dev/null || echo 8833)
    for _ in $(seq 1 180); do
        BUSY=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/status.json" | python3 -I -c \
               'import json, sys; s = json.load(sys.stdin); print(int(bool(s.get("busy")) or s.get("pages_waiting", 0) > 0))' \
               2>/dev/null || echo 0)
        [ "$BUSY" = 0 ] && break
        sleep 5
    done
    say "Installing $NEW."
fi
echo
exec bash install/install.sh
