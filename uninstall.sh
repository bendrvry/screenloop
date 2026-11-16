#!/bin/bash
# Remove screenloop for the current user. Run on the Pi.
set -uo pipefail

systemctl --user disable --now screenloop.service 2>/dev/null
rm -f ~/.config/systemd/user/screenloop.service
systemctl --user daemon-reload
rm -rf ~/.local/share/screenloop
rm -rf ~/.cache/screenloop  # the dashboard's saved data and browser profile
echo "screenloop removed. Your settings are still in ~/.config/screenloop (delete if you like)."
echo
echo "To also undo the one-time system change:"
echo "  sudo rm /etc/udev/rules.d/71-screenloop-hdmi.rules"
