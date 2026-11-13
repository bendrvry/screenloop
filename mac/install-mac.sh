#!/bin/bash
# Install the screenloop helper on a Mac that shares the monitor with the Pi.
# It tells the Pi when this Mac's screen turns on or off.
# Run as yourself, no sudo:  bash mac/install-mac.sh [http://raspberrypi.local:8080]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
pi="${1:-http://raspberrypi.local:8080}"
label=local.screenloop.helper
app="$HOME/Library/Application Support/screenloop/Screenloop Helper.app"
plist="$HOME/Library/LaunchAgents/$label.plist"
logfile="$HOME/Library/Logs/screenloop-helper.log"

command -v swiftc >/dev/null || { echo "Needs Apple's command line tools first: xcode-select --install"; exit 1; }

# A small app bundle, so macOS can ask once for Local Network access.
mkdir -p "$app/Contents/MacOS" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
swiftc -O -o "$app/Contents/MacOS/screenloop-helper" "$here/screenloop-helper.swift"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>local.screenloop.helper</string>
  <key>CFBundleName</key><string>Screenloop Helper</string>
  <key>CFBundleExecutable</key><string>screenloop-helper</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSLocalNetworkUsageDescription</key>
  <string>Tells the Raspberry Pi whether this Mac's screen is on, so its video steps aside.</string>
</dict>
</plist>
PLIST
codesign --force --sign - "$app" 2>/dev/null

cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$app/Contents/MacOS/screenloop-helper</string>
    <string>$pi</string>
  </array>
  <key>AssociatedBundleIdentifiers</key><string>local.screenloop.helper</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$logfile</string>
  <key>StandardErrorPath</key><string>$logfile</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "screenloop helper is running and starts at login."
echo "If macOS asks to allow Screenloop Helper on your local network, click Allow."
echo "Log: $logfile"
sleep 3
tail -n 3 "$logfile" 2>/dev/null || true
