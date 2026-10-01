#!/bin/zsh
# Build Asta Voice (the menu-bar helper for voice mode) and start it at login.
#   deploy/voice/install.sh            build + install + (re)start
#   deploy/voice/install.sh --build    build only (what the tests check)
set -euo pipefail
HERE="${0:A:h}"
ROOT="${HERE:h:h}"
APP="${ASTA_VOICE_APP:-$HOME/Applications/AstaVoice.app}"
LABEL="com.asta.voice"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$APP/Contents/MacOS"
swiftc -O "$HERE/AstaVoice.swift" -o "$APP/Contents/MacOS/AstaVoice" \
  -framework AppKit -framework AVFoundation -framework Carbon -framework CoreAudio
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>Asta Voice</string>
  <key>CFBundleExecutable</key><string>AstaVoice</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Asta listens only while you have its mic switched on (⌃⌥M).</string>
</dict></plist>
PL
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
echo "built $APP"
[[ "${1:-}" == "--build" ]] && exit 0

cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$APP/Contents/MacOS/AstaVoice</string>
    <string>$ROOT/.env</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/voice.log</string>
  <key>StandardOutPath</key><string>$ROOT/data/logs/voice.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "started $LABEL — look for 🔇· in the menu bar"
