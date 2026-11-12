#!/bin/sh
# Install Asta as always-running background services (macOS launchd).
#
# What this gives you:
#   - Asta + WhatsApp bridge start at login and restart automatically if they crash.
#   - Screen off / locked: keeps running (normal for background processes).
#   - Lid open on AC power: a separate com.asta.caffeinate agent runs `caffeinate
#     -si` to prevent system sleep. It is deliberately NOT wrapped around the
#     server: macOS attributes file-access permissions to the process that did
#     the launching, and wrapping the server in caffeinate made caffeinate — an
#     Apple binary you cannot grant Full Disk Access to — the responsible process
#     for reading the Teams notification database. Split, the server is launched
#     directly by launchd, so Python is the responsible process and the FDA grant
#     on Python actually takes effect.
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

# The WhatsApp bridge is its own launchd job, not a child of the server: launchd
# kills a job's whole process group when it exits, so a child bridge died with a
# crashed server and its "server down" alert could never be sent. The server
# sees ~/Library/LaunchAgents/com.asta.whatsapp.plist and stands down its own
# supervisor (app/wa_bridge.py), so there is never a second bridge on port 8323.
# Order matters: the bridge plist is copied before anything is reloaded, so the
# new server never spawns a child; the server is reloaded before the bridge job,
# so the old server's child (stopped with it) has freed the port first.
SERVICES="com.asta.server com.asta.whatsapp com.asta.caffeinate"

if [ "$1" = "remove" ]; then
  for svc in $SERVICES; do
    launchctl bootout "gui/$UID_N/$svc" 2>/dev/null || true
    rm -f ~/Library/LaunchAgents/$svc.plist
  done
  echo "Asta services removed."
  exit 0
fi

echo "NOTE: stop any dev instances first (ports 8321/8323 must be free)."
cp deploy/com.asta.whatsapp.plist ~/Library/LaunchAgents/
for svc in $SERVICES; do
  cp "deploy/$svc.plist" ~/Library/LaunchAgents/
  # bootout is ASYNCHRONOUS: launchd tears the old job down in the background,
  # and bootstrapping the new plist before that finishes fails with
  # "Bootstrap failed: 5: Input/output error" — which leaves the service DOWN
  # instead of reloaded. Wait (bounded) for the label to actually disappear
  # before loading the new plist.
  launchctl bootout "gui/$UID_N/$svc" 2>/dev/null || true
  n=0
  while launchctl print "gui/$UID_N/$svc" >/dev/null 2>&1 && [ "$n" -lt 10 ]; do
    sleep 1
    n=$((n + 1))
  done
  launchctl bootstrap "gui/$UID_N" ~/Library/LaunchAgents/$svc.plist
  echo "loaded $svc"
done

echo
echo "Asta is now a background service:"
echo "  UI:      http://127.0.0.1:8321"
echo "  logs:    tail -f data/logs/server.log data/logs/whatsapp.log"
echo "  status:  launchctl print gui/$UID_N/com.asta.server | head -20"
echo
echo "Teams/Outlook NOTIFICATION watching (the instant 'you were pinged' trigger)"
echo "needs Full Disk Access on the Python that launchd now runs:"
echo "  /opt/homebrew/opt/python@3.13/Frameworks/Python.framework/Versions/3.13/Resources/Python.app"
echo "System Settings -> Privacy & Security -> Full Disk Access -> + (Cmd-Shift-G to paste)."
echo "This only works because launchd — not your Terminal — is now the launcher;"
echo "a shell-run server is attributed to Terminal.app, so granting Python did nothing."
echo "Verify:  ./.venv/bin/python -c \"open('$HOME/Library/Group Containers/group.com.apple.usernoted/db2/db','rb').read(1); print('FDA OK')\""
