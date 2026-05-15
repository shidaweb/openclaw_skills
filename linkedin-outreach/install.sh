#!/usr/bin/env bash
# Install linkedin-outreach skill into ~/.openclaw/skills/
set -euo pipefail

SKILL_DIR="$HOME/.openclaw/skills/linkedin-outreach"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.openclaw/skills"

if [ -d "$SKILL_DIR" ]; then
  echo "Skill already exists at $SKILL_DIR"
  read -p "Overwrite? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
  rm -rf "$SKILL_DIR"
fi

cp -r "$SOURCE_DIR" "$SKILL_DIR"
chmod +x "$SKILL_DIR/run.py"

echo "Installed to: $SKILL_DIR"
echo
echo "Next steps:"
echo "  1. pip install pyyaml --break-system-packages"
echo "  2. cd $SKILL_DIR"
echo "  3. cp config.example.yaml config.yaml  # then edit"
echo "  4. openclaw browser --browser-profile openclaw start"
echo "  5. python run.py fetch-leads --search-url '<sales nav URL>' --limit 3"
