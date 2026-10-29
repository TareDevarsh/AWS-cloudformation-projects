#!/usr/bin/env bash
# Scaffold a new project from _template/.
# Usage: ./scripts/new-project.sh <NN> <slug> "<Title>"
# Example: ./scripts/new-project.sh 03 url-shortener "URL Shortener"
set -euo pipefail

[[ $# -eq 3 ]] || { echo "Usage: $0 <NN> <slug> \"<Title>\""; exit 1; }
NN="$1"; SLUG="$2"; TITLE="$3"

[[ "$NN" =~ ^[0-9]{2}$ ]] || { echo "NN must be two digits (e.g. 03)"; exit 1; }
[[ "$SLUG" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || { echo "slug must be lowercase-hyphenated"; exit 1; }
[[ "$TITLE" != *"|"* ]] || { echo "Title must not contain |"; exit 1; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${ROOT}/${NN}-${SLUG}"
[[ ! -e "$DEST" ]] || { echo "Already exists: $DEST"; exit 1; }
ls "${ROOT}" | grep -q "^${NN}-" && { echo "Number ${NN} already used"; exit 1; }

cp -R "${ROOT}/_template" "$DEST"

# Replace placeholders in every text file.
export NN SLUG TITLE
find "$DEST" -type f \( -name '*.md' -o -name '*.yaml' -o -name '*.py' -o -name '*.sh' \) -print0 |
  xargs -0 perl -pi -e 's/\{\{NN\}\}/$ENV{NN}/g; s/\{\{slug\}\}/$ENV{SLUG}/g; s/\{\{Title\}\}/$ENV{TITLE}/g'

chmod +x "$DEST"/scripts/*.sh

if grep -rq '{{' "$DEST" --include='*.md' --include='*.yaml' --include='*.py' --include='*.sh'; then
  echo "WARN: unreplaced placeholders remain:"; grep -rn '{{' "$DEST" | head
fi

echo "Created ${NN}-${SLUG}"
echo "Next:"
echo "  1. Edit infrastructure/template.yaml, src/, docs/diagram.py"
echo "  2. python3 ${NN}-${SLUG}/docs/diagram.py   # render architecture.png"
echo "  3. Add the project to the root README.md (table, gallery, map, mindmap, tree)"
