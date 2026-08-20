#!/bin/bash
# Installs the DocketWatch poll agent (every 3 hours) into launchd.
# Run this yourself when you want background polling on; nothing installs it for you.
set -euo pipefail

LABEL=com.parth.docketwatch-poll
SRC="$(cd "$(dirname "$0")" && pwd)/$LABEL.plist"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -x "$HOME/Developer/docketwatch/.venv/bin/docketwatch" ] || {
  echo "No docketwatch in .venv. Run: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
}

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.docketwatch"
cp "$SRC" "$DEST"

# bootstrap is the modern spelling; older launchctl only has load.
if launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null; then
  :
else
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null || launchctl load -w "$DEST"
fi

echo "Installed $DEST"
echo "  polls every 3h (RunAtLoad, so the first run is now)"
echo "  log:       ~/.docketwatch/launchd-poll.log"
echo "  status:    launchctl list | grep $LABEL"
echo "  run now:   launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "  uninstall: launchctl bootout gui/$(id -u)/$LABEL && rm $DEST"
echo
echo "Polling sample fixtures. Real dockets need COURTLISTENER_API_TOKEN + DOCKETWATCH_LIVE=1"
echo "added to EnvironmentVariables in $DEST (see spike_courtlistener_auth.py)."
