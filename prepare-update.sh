#!/bin/bash
# Build first, then sign the DMG and produce the release's appcast asset.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
VERSION="$(/usr/bin/awk -F '"' '/^VERSION = / {print $2}' updater.py)"
GENERATOR="${SPARKLE_GENERATOR:-$SCRIPT_DIR/.venv/sparkle-2.10.0/bin/generate_appcast}"
if [ ! -f "dist/Ghostpin-$VERSION.dmg" ] || [ ! -x "$GENERATOR" ]; then
    echo "Run ./build.sh first (or set SPARKLE_GENERATOR)."; exit 1
fi
"$GENERATOR" --account ghostpin --maximum-deltas 0 \
    --download-url-prefix "https://github.com/Kron00/ghostpin/releases/download/v$VERSION/" \
    --link "https://github.com/Kron00/ghostpin/releases/latest" \
    "$SCRIPT_DIR/dist"
echo "Upload dist/appcast.xml and dist/Ghostpin-$VERSION.dmg to release v$VERSION together."
