#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ ! -x ".venv/bin/python" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [FAIL] Not installed. Run ./setup_unix.sh first."
    exit 1
fi

exec .venv/bin/python -m reliable_bluetooth peer "$@"
