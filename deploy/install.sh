#!/bin/sh
# Install Asta as always-running background services (macOS launchd).
#
# What this gives you:
#   - Asta + WhatsApp bridge start at login and restart automatically if they crash.
#   - Screen off / locked: keeps running (normal for background processes).
#   - Lid open on AC power: caffeinate -si prevents system sleep while Asta runs.
#   - Lid CLOSED on battery: macOS force-sleeps regardless — no software can stop
#     that. Keep the Mac plugged in (or add an external display) for lid-closed use.
#     Optional, plugged-in only: System Settings -> Battery -> Options ->
#     "Prevent automatic sleeping on power adapter when the display is off".
#
# Run once:   sh deploy/install.sh
# Uninstall:  sh deploy/install.sh remove
set -e
cd "$(dirname "$0")/.."
mkdir -p data/logs ~/Library/LaunchAgents

UID_N=$(id -u)

if [ "$1" = "remove" ]; then
  for svc in com.asta.server com.asta.whatsapp; do
    launchctl bootout "gui/$UID_N/$svc" 2>/dev/null || true
    rm -f ~/Library/LaunchAgents/$svc.plist
  done
  echo "Asta services removed."
  exit 0
fi

echo "NOTE: stop any dev instances first (ports 8321/8323 must be free)."
for svc in com.asta.server com.asta.whatsapp; do
  cp "deploy/$svc.plist" ~/Library/LaunchAgents/
  launchctl bootout "gui/$UID_N/$svc" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_N" ~/Library/LaunchAgents/$svc.plist
  echo "loaded $svc"
done

echo
echo "Asta is now a background service:"
echo "  UI:      http://127.0.0.1:8321"
echo "  logs:    tail -f data/logs/server.log data/logs/whatsapp.log"
echo "  status:  launchctl print gui/$UID_N/com.asta.server | head -20"
echo
echo "Remember: for Teams/Outlook watching, grant Full Disk Access to"
echo "/Users/arun.k.k/help/asta/.venv/bin/python (System Settings -> Privacy)."
