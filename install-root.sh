#!/bin/bash
# One-time system change for screenloop. Run on the Pi:  sudo bash install-root.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Please run with sudo."; exit 1; }

# Let the video group force the Pi's HDMI port "unplugged" and back (a software
# replug). That's how screenloop gives the monitor no signal. Switching the output
# off in the desktop instead gets stuck when the monitor wakes and re-announces
# itself while the output is off.
cat > /etc/udev/rules.d/71-screenloop-hdmi.rules <<'RULE'
# screenloop: allow forcing the Pi's HDMI connector off/on via its sysfs "status" file
ACTION=="add|change", SUBSYSTEM=="drm", KERNEL=="card*-HDMI-A-*", RUN+="/bin/chgrp video /sys%p/status", RUN+="/bin/chmod 0664 /sys%p/status"
RULE
udevadm control --reload-rules
udevadm trigger --subsystem-match=drm --action=change

echo "Done. The rule is active now and after every reboot."
