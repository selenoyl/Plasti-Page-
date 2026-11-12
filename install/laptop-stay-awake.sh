#!/bin/bash
# P833 Call Receiver - keep the laptop running with the lid shut.
# By Ethan Brady & Ryan Raikes
#
# Run once:    sudo bash install/laptop-stay-awake.sh
# To undo:     sudo rm /etc/systemd/logind.conf.d/p833-lid.conf
#              sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target
#              then reboot.

set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo"; exit 1; fi

install -d -m 755 /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/p833-lid.conf <<'EOF'
# P833: closing the lid must not stop the receiver. By Ethan Brady & Ryan Raikes
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
echo "Done. Reboot once so the lid setting takes effect."
echo "Closing the lid no longer locks the screen: log out (or lock the screen) before walking away,"
echo "so nobody can use the open session. The receiver keeps running after you log out."
