#!/usr/bin/env bash
# Builds a Decky-installable zip at out/decky-xbox-wireless-helper.zip
# (same layout the Decky CLI produces: plugin folder at the root of the zip).
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="decky-xbox-wireless-helper"
STAGE="out/stage/$NAME"

[ -f dist/index.js ] || { echo "dist/index.js missing - run 'pnpm build' first" >&2; exit 1; }

rm -rf out
mkdir -p "$STAGE"

cp -r dist "$STAGE/"
rm -f "$STAGE"/dist/*.map
cp -r py_modules "$STAGE/"
find "$STAGE/py_modules" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE/py_modules" -name '.keep' -delete
cp -r assets "$STAGE/"
cp main.py plugin.json package.json README.md LICENSE "$STAGE/"

(cd out/stage && zip -qr "../$NAME.zip" "$NAME")
rm -rf out/stage
echo "Created out/$NAME.zip"
