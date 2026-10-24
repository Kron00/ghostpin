#!/bin/bash
# Developer mode: installs dependencies and starts the Flask app rootlessly.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"

echo "=================================================="
echo "  Ghostpin (Dev Mode)"
echo "=================================================="
echo ""

# Prefer a Homebrew interpreter that supports pymobiledevice3 and pywebview.
PYTHON=""
for p in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 python3.13 python3.12 python3; do
    if command -v "$p" &>/dev/null; then
        CANDIDATE="$(command -v "$p")"
        if "$CANDIDATE" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' \
                &>/dev/null; then
            PYTHON="$CANDIDATE"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[!] Python 3.10+ is required."
    exit 1
fi
echo "[+] Using $($PYTHON --version) at $PYTHON"

VENV_PYTHON="$VENV_DIR/bin/python3"
if "$VENV_PYTHON" -c \
        'import sys; raise SystemExit(sys.version_info < (3, 10))' \
        &>/dev/null; then
    echo "[*] Using existing virtual environment"
else
    if [ -e "$VENV_DIR" ] || [ -L "$VENV_DIR" ]; then
        TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
        BROKEN_VENV="$SCRIPT_DIR/.venv.broken-$TIMESTAMP"
        SUFFIX=1
        while [ -e "$BROKEN_VENV" ] || [ -L "$BROKEN_VENV" ]; do
            BROKEN_VENV="$SCRIPT_DIR/.venv.broken-$TIMESTAMP-$SUFFIX"
            SUFFIX=$((SUFFIX + 1))
        done
        echo "[*] Moving unusable virtual environment to $(basename "$BROKEN_VENV")"
        mv "$VENV_DIR" "$BROKEN_VENV"
    fi
    echo "[*] Creating virtual environment..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

echo "[*] Installing current dependencies..."
"$VENV_DIR/bin/python3" -m pip install --upgrade -q -r requirements.txt
echo "[+] Dependencies ready"

# The app owns a no-root in-process userspace tunnel. DeviceManager starts the
# privileged tunneld compatibility path only if userspace RSD establishment fails.

exec "$VENV_DIR/bin/python3" app.py
