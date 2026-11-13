#!/bin/bash
# Remove the screenloop helper from this Mac.
set -uo pipefail
label=local.screenloop.helper
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$label.plist"
rm -rf "$HOME/Library/Application Support/screenloop/Screenloop Helper.app"
rmdir "$HOME/Library/Application Support/screenloop" 2>/dev/null
echo "screenloop helper removed (its log stays in ~/Library/Logs/screenloop-helper.log)."
echo "The Pi goes back to judging this Mac by pings after helper_memory_days (7),"
echo "or at once if you delete ~/.config/screenloop/helpers.json on the Pi and restart it."
