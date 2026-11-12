#!/bin/bash
# P833 Call Receiver - update to the newest version from GitHub, then install it.
# By Ethan Brady & Ryan Raikes
#
# Run from the folder you cloned:   sudo bash install/update.sh
#   (add -y to skip the "install these changes?" question)
# Needs internet. Your config, secrets, board list and call log are kept, and
# the installer backs up the old install first.
#
# Safety: this runs as root, so it shows what changed and asks before installing.
# git runs with hooks off, and the folder is trusted for this run only (nothing is
# added to root's git settings).

set -euo pipefail
cd "$(dirname "$0")/.."

YES=no
[ "${1:-}" = "-y" ] && YES=yes

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash install/update.sh"; exit 1; fi
if [ ! -d .git ]; then
    echo "This folder was not made with git clone, so it cannot update itself."
    echo "Download the new version, unzip it into a new folder, and run install/install.sh there."
    exit 1
fi
command -v git >/dev/null || dnf install -y git

# git as root in a folder that may belong to another user: trust it for these commands
# only, with no hooks and no pager.
g() { git -c safe.directory="$PWD" -c core.hooksPath=/dev/null --no-pager "$@"; }

# Older versions of this script added the folder to root's git settings; take it out.
if git config --global --get-all safe.directory 2>/dev/null | grep -qxF "$PWD"; then
    git config --global --unset-all safe.directory "^$(printf '%s' "$PWD" | sed 's/[][\.*^$/]/\\&/g')\$" || true
fi

OWNER=$(stat -c %U .)
if [ "$OWNER" != root ]; then
    echo "Note: this folder belongs to '$OWNER'. Anyone who can log in as '$OWNER' can change what"
    echo "root installs from it. For the most safety, keep the clone in a folder only root can change."
    echo
fi

OLD=$(g rev-parse --short HEAD)
BRANCH=$(g rev-parse --abbrev-ref HEAD)
g fetch --quiet origin "$BRANCH"
if ! g merge-base --is-ancestor HEAD "origin/$BRANCH"; then
    echo "Update stopped: this folder has commits GitHub doesn't, so it can't simply move forward."
    exit 1
fi
NEW=$(g rev-parse --short "origin/$BRANCH")
if [ "$OLD" = "$NEW" ]; then
    echo "Already the newest version ($NEW). Reinstalling anyway to be sure everything is in place."
else
    echo "Changes from $OLD to $NEW:"
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
        echo "Update stopped: files in this folder were changed by hand, so git will not overwrite them."
        echo "See what changed with: git status"
        echo "To throw those changes away and update: git checkout -- . && sudo bash install/update.sh"
        exit 1
    fi
fi
echo
exec bash install/install.sh
