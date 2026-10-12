#!/bin/bash
set -euo pipefail

MANIFEST_URL="https://cciteheck-api.onrender.com/manifest.xml"
# 备用源:https://raw.githubusercontent.com/Ran-jiang/check-7.16/main/apps/word_addin/manifest.render.xml
INSTALL_DIR="$HOME/Library/Containers/com.microsoft.Word/Data/Documents/wef"
MANIFEST_PATH="$INSTALL_DIR/manifest.render.xml"
EXPECTED_ID="b01e1d79-81a3-4162-9c0a-c80fa9b1203b"
EXPECTED_HOST="https://cciteheck-api.onrender.com"

echo "CCitecheck Word Add-in installer"
echo "================================"

if pgrep -x "Microsoft Word" >/dev/null 2>&1; then
  echo "Please close Microsoft Word completely, then run this installer again."
  read -r -p "Press Enter to close..."
  exit 1
fi

mkdir -p "$INSTALL_DIR"
echo "Downloading the public manifest..."
curl --fail --location --retry 3 --output "$MANIFEST_PATH" "$MANIFEST_URL"

if ! grep -q "$EXPECTED_ID" "$MANIFEST_PATH" || ! grep -q "$EXPECTED_HOST" "$MANIFEST_PATH"; then
  rm -f "$MANIFEST_PATH"
  echo "The downloaded manifest failed validation. Installation was stopped."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Checking the public service..."
curl --fail --silent --show-error --retry 3 "$EXPECTED_HOST/api/health" >/dev/null

echo ""
echo "Installation completed successfully."
echo "Open Word, then choose Home > Add-ins > CCitecheck."
read -r -p "Press Enter to close..."
