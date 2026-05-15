#!/usr/bin/env bash
# Install jp-form-outreach skill into ~/.openclaw/skills/
set -euo pipefail

SKILL_DIR="$HOME/.openclaw/skills/jp-form-outreach"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.openclaw/skills"

if [ -d "$SKILL_DIR" ] && [ "$SOURCE_DIR" != "$SKILL_DIR" ]; then
  echo "Skill already exists at $SKILL_DIR"
  read -p "Overwrite? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
  rm -rf "$SKILL_DIR"
fi

if [ "$SOURCE_DIR" != "$SKILL_DIR" ]; then
  cp -r "$SOURCE_DIR" "$SKILL_DIR"
fi
chmod +x "$SKILL_DIR/run.py"

echo "Installed to: $SKILL_DIR"
echo
echo "Next steps:"
echo "  1. pip install pyyaml --break-system-packages"
echo "  2. cd $SKILL_DIR"
echo "  3. cp config.example.yaml config.yaml      # edit if needed"
echo "  4. cp targets.example.yaml targets.yaml    # edit / extend"
echo "  5. openclaw browser --browser-profile openclaw start"
echo "  6. python run.py bootstrap"
echo "  7. python run.py enrich"
echo "  8. python run.py draft"
echo "  9. python run.py preview"
echo " 10. python run.py send --ids 1 --no-confirm  # try first one safely"
