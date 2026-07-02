#!/usr/bin/env bash
# Idempotent installer for the Doorman gateway watchdog (v14 §W5).
# Safe to run repeatedly: it regenerates the plist, reloads the launchd job,
# verifies registration, and best-effort asserts the gateway's KeepAlive.
set -euo pipefail

SKILLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# v32 FX5 — prefer the skill venv python (stable path, matches what the test
# suite runs on) over whatever python3 the installing shell resolves. The
# watchdog imports _outreach_core; drifting interpreters was how a syntax
# error killed it silently for a month.
PYTHON3="$SKILLS_DIR/jp-form-outreach/.venv/bin/python3"
if [[ ! -x "$PYTHON3" ]]; then
  PYTHON3="$(command -v python3)"
fi
LABEL="com.doorman.watchdog"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

echo "▶ Installing ${LABEL}"
echo "  skills dir: $SKILLS_DIR"
echo "  python3:    $PYTHON3"

mkdir -p "$SKILLS_DIR/data" "$HOME/Library/LaunchAgents"

# Build a PATH for the launchd job: the installing shell's PATH (where the
# operator confirmed `openclaw` resolves) plus the usual Homebrew/npm/user dirs.
# Without this, launchd's minimal default PATH hides `openclaw` and the health
# probe silently no-ops.
WD_PATH="$PATH:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/.npm-global/bin"
echo "  job PATH:   $WD_PATH"

# 1) Render the plist (idempotent: overwrite each run).
sed -e "s|{{SKILLS_DIR}}|$SKILLS_DIR|g" \
    -e "s|{{PYTHON3}}|$PYTHON3|g" \
    -e "s|{{PATH}}|$WD_PATH|g" \
    "$SKILLS_DIR/scripts/com.doorman.watchdog.plist.template" > "$PLIST_DST"

# 2) Reload (unload first so a re-run picks up template changes).
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

# 3) Verify registration.
if launchctl list | grep -q "$LABEL"; then
  echo "✅ watchdog registered with launchd ($LABEL)"
else
  echo "❌ watchdog NOT registered — check $SKILLS_DIR/data/watchdog.err" >&2
  exit 1
fi

# 4) Best-effort: assert the gateway is KeepAlive-managed by launchd.
GW_LABEL="$("$PYTHON3" -c 'from _outreach_core import gateway_config as g; print(g.label())' 2>/dev/null || echo "ai.openclaw.gateway")"
if launchctl print "gui/${UID_NUM}/${GW_LABEL}" >/dev/null 2>&1; then
  if launchctl print "gui/${UID_NUM}/${GW_LABEL}" 2>/dev/null | grep -qi "keepalive"; then
    echo "✅ gateway ($GW_LABEL) is launchd-managed (KeepAlive present)"
  else
    echo "⚠️ gateway ($GW_LABEL) is registered but KeepAlive not detected — watchdog §W1 will actively (re)start it"
  fi
else
  echo "⚠️ gateway ($GW_LABEL) not registered with launchd — watchdog §W1 will run start_cmd on first tick"
fi

# 5) Fire one tick now so a verifiable line lands in the log immediately.
( cd "$SKILLS_DIR" && PYTHONPATH="$SKILLS_DIR" "$PYTHON3" -m _outreach_core.helpers.watchdog tick >/dev/null 2>&1 ) || true

echo ""
echo "Done. Useful commands:"
echo "  launchctl list | grep doorman        # registration"
echo "  tail -f $SKILLS_DIR/data/watchdog.log  # ticks"
echo "  Override gateway label/commands in: $SKILLS_DIR/data/gateway.json"
