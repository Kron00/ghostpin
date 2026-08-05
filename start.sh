#!/bin/bash
# Developer mode: installs dependencies and starts the Flask app rootlessly.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
APP_PORT=8080

echo "=================================================="
echo "  Ghostpin (Dev Mode)"
echo "=================================================="
echo ""

# Prefer a Homebrew interpreter that supports pymobiledevice3 and pywebview.
PYTHON=""
for p in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 python3.13 python3.12 python3; do
    if command -v "$p" &>/dev/null; then
        PYTHON="$(command -v "$p")"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[!] Python 3.10+ is required."
    exit 1
fi

PY_MINOR="$($PYTHON -c 'import sys; print(sys.version_info.minor)')"
if [ "$PY_MINOR" -lt 10 ]; then
    echo "[!] Python 3.10+ is required; found $($PYTHON --version)."
    exit 1
fi
echo "[+] Using $($PYTHON --version) at $PYTHON"

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo "[*] Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

echo "[*] Installing current dependencies..."
"$VENV_DIR/bin/python3" -m pip install --upgrade -q -r requirements.txt
echo "[+] Dependencies ready"

# The app owns a no-root in-process userspace tunnel. DeviceManager starts the
# privileged tunneld compatibility path only if userspace RSD establishment fails.
lsof -ti :"$APP_PORT" 2>/dev/null | xargs kill 2>/dev/null || true

exec "$VENV_DIR/bin/python3" app.py
