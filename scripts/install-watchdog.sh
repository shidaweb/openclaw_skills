#!/usr/bin/env bash
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON3="$(command -v python3)"
PLIST_DST="$HOME/Library/LaunchAgents/com.doorman.watchdog.plist"

sed -e "s|{{SKILLS_DIR}}|$SKILLS_DIR|g" \
    -e "s|{{PYTHON3}}|$PYTHON3|g" \
    "$SKILLS_DIR/scripts/com.doorman.watchdog.plist.template" > "$PLIST_DST"

launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "✅ watchdog installed. Check: launchctl list | grep doorman"
echo "   Log: tail -f $SKILLS_DIR/data/watchdog.log"
