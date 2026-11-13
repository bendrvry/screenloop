#!/bin/bash
# Install screenloop for the current user (no sudo needed). Run on the Pi.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

for tool in mpv ffprobe wlr-randr; do
    command -v "$tool" >/dev/null || echo "Missing $tool. Install it with:  sudo apt install mpv ffmpeg wlr-randr"
done

install -Dm755 "$here/screenloop.py" ~/.local/share/screenloop/screenloop.py
if [[ -e ~/.config/screenloop/screenloop.ini ]]; then
    echo "Keeping your existing ~/.config/screenloop/screenloop.ini"
else
    install -Dm644 "$here/screenloop.ini" ~/.config/screenloop/screenloop.ini
fi
install -Dm644 "$here/screenloop.service" ~/.config/systemd/user/screenloop.service

systemctl --user daemon-reload
systemctl --user enable screenloop.service
systemctl --user restart screenloop.service
echo "screenloop is running. Control page: http://$(hostname).local:8080"

if [[ ! -e /etc/udev/rules.d/71-screenloop-hdmi.rules ]]; then
    echo "Next: run the one-time system step:  sudo bash $here/install-root.sh"
fi
