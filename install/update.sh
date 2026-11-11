#!/bin/bash
# P833 Call Receiver - update to the newest version from GitHub, then install it.
# By Ethan Brady & Ryan Raikes
#
# Run from the folder you cloned:   sudo bash install/update.sh
# Needs internet. Your config, secrets, board list and call log are kept, and
# the installer backs up the old install first.

set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo: sudo bash install/update.sh"; exit 1; fi
if [ ! -d .git ]; then
    echo "This folder was not made with git clone, so it cannot update itself."
    echo "Download the new version, unzip it into a new folder, and run install/install.sh there."
    exit 1
fi
command -v git >/dev/null || dnf install -y git

# The folder may belong to another user; let root's git work in it.
git config --global --get-all safe.directory | grep -qx "$PWD" || git config --global --add safe.directory "$PWD"

OLD=$(git rev-parse --short HEAD)
if ! git pull --ff-only; then
    echo
    echo "Update stopped: files in this folder were changed by hand, so git will not overwrite them."
    echo "See what changed with: git status"
    echo "To throw those changes away and update: git checkout -- . && sudo bash install/update.sh"
    exit 1
fi
NEW=$(git rev-parse --short HEAD)
if [ "$OLD" = "$NEW" ]; then
    echo "Already the newest version ($NEW). Reinstalling anyway to be sure everything is in place."
else
    echo "Updated $OLD -> $NEW:"
    git log --oneline "$OLD..$NEW"
fi
echo
exec bash install/install.sh
