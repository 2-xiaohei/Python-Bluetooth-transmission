#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] Creating Python virtual environment..."
python3 -m venv .venv
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] Installing Reliable Bluetooth..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ OK ] Installation complete. Run ./run_unix.sh to start."
