#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="Ghostpin"
BUNDLE_ID="com.ghostpin.app"
VERSION="$(/usr/bin/awk -F '"' '/^VERSION = / {print $2}' updater.py)"
VENV_DIR="$SCRIPT_DIR/.venv"
DIST_DIR="$SCRIPT_DIR/dist"
APP_DIR="$DIST_DIR/$APP_NAME.app"
DMG_NAME="Ghostpin-${VERSION}"

echo "=================================================="
echo "  Building $APP_NAME v$VERSION"
echo "=================================================="
echo ""

# ── 1. Venv (requires Python 3.10+ for pymobiledevice3) ──
# Find best available Python: prefer 3.13 from Homebrew, then 3.12, 3.11, 3.10
PYTHON=""
for p in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$p" &>/dev/null; then
        PYVER=$("$p" -c "import sys; print(sys.version_info.minor)")
        if [ "$PYVER" -ge 10 ] 2>/dev/null; then
            PYTHON="$p"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[!] Python 3.10+ is required. Install via: brew install python@3.13"
    exit 1
fi
echo "    Using $($PYTHON --version) at $PYTHON"

if [ ! -d "$VENV_DIR" ]; then
    echo "[1/5] Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    # Verify existing venv is 3.10+
    VENV_VER=$("$VENV_DIR/bin/python3" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
    if [ "$VENV_VER" -lt 10 ] 2>/dev/null; then
        echo "[1/5] Recreating venv with Python 3.10+..."
        rm -rf "$VENV_DIR"
        "$PYTHON" -m venv "$VENV_DIR"
    else
        echo "[1/5] Using existing venv"
    fi
fi

VPYTHON="$VENV_DIR/bin/python3"
VPIP="$VENV_DIR/bin/pip"

# ── 2. Dependencies ─────────────────────────────────────
echo "[2/5] Installing dependencies..."
"$VPIP" install --no-cache-dir -q -r requirements.txt pyinstaller
echo "      Done"

# ── 3. Clean ─────────────────────────────────────────────
echo "[3/5] Cleaning previous builds..."
rm -rf "$DIST_DIR" build *.spec

# ── 4. PyInstaller ───────────────────────────────────────
echo "[4/5] Building app bundle..."
echo "      This takes 2-5 minutes..."

# Generate the macOS icon from Ghostpin's original 1024px artwork.
ICON_FLAG=""
if [ -f "$SCRIPT_DIR/icon.png" ]; then
    echo "      Generating app icon..."
    ICONSET="/tmp/Ghostpin.iconset"
    rm -rf "$ICONSET" && mkdir -p "$ICONSET"
    sips -z 16 16 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_16x16.png" &>/dev/null
    sips -z 32 32 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_16x16@2x.png" &>/dev/null
    sips -z 32 32 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_32x32.png" &>/dev/null
    sips -z 64 64 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_32x32@2x.png" &>/dev/null
    sips -z 128 128 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_128x128.png" &>/dev/null
    sips -z 256 256 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_128x128@2x.png" &>/dev/null
    sips -z 256 256 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_256x256.png" &>/dev/null
    sips -z 512 512 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_256x256@2x.png" &>/dev/null
    sips -z 512 512 "$SCRIPT_DIR/icon.png" --out "$ICONSET/icon_512x512.png" &>/dev/null
    cp "$SCRIPT_DIR/icon.png" "$ICONSET/icon_512x512@2x.png"
    iconutil -c icns "$ICONSET" -o "$SCRIPT_DIR/icon.icns" 2>/dev/null
    rm -rf "$ICONSET"
fi
if [ -f "$SCRIPT_DIR/icon.icns" ]; then
    ICON_FLAG="--icon=$SCRIPT_DIR/icon.icns"
fi

"$VPYTHON" -m PyInstaller \
    --name "$APP_NAME" \
    --windowed \
    --onedir \
    --noconfirm \
    --clean \
    --log-level WARN \
    --osx-bundle-identifier "$BUNDLE_ID" \
    $ICON_FLAG \
    --add-data "templates:templates" \
    --add-data "static:static" \
    --add-data "LICENSE:." \
    --add-data "NOTICE.md:." \
    --add-data "licenses:licenses" \
    --collect-all pymobiledevice3 \
    --collect-all webview \
    --copy-metadata apple-compress \
    --copy-metadata pyimg4 \
    --hidden-import pymobiledevice3.cli.remote \
    --hidden-import pymobiledevice3.remote.tunnel_service \
    --hidden-import pymobiledevice3.remote.userspace_tunnel \
    --hidden-import webview.platforms.cocoa \
    main_app.py

if [ ! -d "$APP_DIR" ]; then
    echo "[!] Build failed"
    exit 1
fi

# Write Info.plist with proper metadata
cat > "$APP_DIR/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>
    <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key>
    <string>$VERSION</string>
    <key>CFBundleShortVersionString</key>
    <string>$VERSION</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>$APP_NAME</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.15</string>
    <key>CFBundleIconFile</key>
    <string>icon.icns</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>NSAppTransportSecurity</key>
    <dict>
        <key>NSAllowsArbitraryLoads</key>
        <true/>
    </dict>
</dict>
</plist>
PLIST

# Embed the pinned Sparkle distribution. Only the public key belongs in the
# bundle; the release signing key stays in the maintainer's macOS Keychain.
SPARKLE_CACHE="$VENV_DIR/sparkle-2.10.0"
if [ -z "${SPARKLE_FRAMEWORK:-}" ]; then
    if [ ! -d "$SPARKLE_CACHE/Sparkle.framework" ]; then
        curl --fail --location --silent --show-error \
            https://github.com/sparkle-project/Sparkle/releases/download/2.10.0/Sparkle-2.10.0.tar.xz \
            -o "$VENV_DIR/sparkle.tar.xz"
        ACTUAL_SHA=$(shasum -a 256 "$VENV_DIR/sparkle.tar.xz" | cut -d ' ' -f 1)
        if [ "$ACTUAL_SHA" != "c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c" ]; then
            echo "[!] Sparkle archive checksum mismatch"; exit 1
        fi
        mkdir -p "$SPARKLE_CACHE"
        tar -xf "$VENV_DIR/sparkle.tar.xz" -C "$SPARKLE_CACHE"
    fi
    SPARKLE_FRAMEWORK="$SPARKLE_CACHE/Sparkle.framework"
fi
export SPARKLE_PUBLIC_KEY="${SPARKLE_PUBLIC_KEY:-Q3hQGG9JWAbkyl6IHWk8QWPXnNTse5Vs55Ur5VerQiM=}"
export SPARKLE_FEED_URL="${SPARKLE_FEED_URL:-https://github.com/Kron00/ghostpin/releases/latest/download/appcast.xml}"
case "$SPARKLE_FEED_URL" in https://*) ;; *) echo "Feed must use HTTPS"; exit 1 ;; esac
ditto "$SPARKLE_FRAMEWORK" "$APP_DIR/Contents/Frameworks/Sparkle.framework"
"$VPYTHON" - "$APP_DIR/Contents/Info.plist" <<'PYPLIST'
import os, plistlib, sys
with open(sys.argv[1], "rb") as handle:
    info = plistlib.load(handle)
info.update(SUFeedURL=os.environ["SPARKLE_FEED_URL"],
            SUPublicEDKey=os.environ["SPARKLE_PUBLIC_KEY"],
            SUEnableAutomaticChecks=True)
with open(sys.argv[1], "wb") as handle:
    plistlib.dump(info, handle)
PYPLIST

# PyInstaller signs before this script writes final metadata. Re-sign the
# completed bundle so macOS sees a valid, internally consistent app.
codesign --force --deep --sign - "$APP_DIR" >/dev/null 2>&1

echo "      App bundle created"

# ── 5. DMG ───────────────────────────────────────────────
echo "[5/5] Creating DMG..."

DMG_FINAL="$DIST_DIR/$DMG_NAME.dmg"
DMG_STAGING="$DIST_DIR/dmg_staging"

# Stage files
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
cp -R "$APP_DIR" "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"

# Create compressed DMG directly from staging directory
rm -f "$DMG_FINAL"
hdiutil create \
    -volname "$APP_NAME" \
    -srcfolder "$DMG_STAGING" \
    -ov \
    -format UDZO \
    "$DMG_FINAL" \
    -quiet

rm -rf "$DMG_STAGING"

APP_SIZE=$(du -sh "$APP_DIR" | cut -f1)
DMG_SIZE=$(du -sh "$DMG_FINAL" | cut -f1)

echo ""
echo "=================================================="
echo "  Build complete!"
echo "=================================================="
echo ""
echo "  App:  $APP_DIR ($APP_SIZE)"
echo "  DMG:  $DMG_FINAL ($DMG_SIZE)"
echo ""
echo "  Test:   open '$APP_DIR'"
echo "  Share:  Send the DMG file"
echo ""
