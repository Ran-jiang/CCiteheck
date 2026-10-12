#!/bin/bash
set -euo pipefail

MANIFEST_PATH="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef/manifest.render.xml"

if pgrep -x "Microsoft Word" >/dev/null 2>&1; then
  echo "Please close Microsoft Word completely, then run this uninstaller again."
  read -r -p "Press Enter to close..."
  exit 1
fi

rm -f "$MANIFEST_PATH"
echo "CCitecheck Word Add-in manifest was removed."
read -r -p "Press Enter to close..."
