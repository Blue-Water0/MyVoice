#!/usr/bin/env bash
# Launch MyVoice from the project venv.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if [ ! -d "$VENV" ]; then
    echo "Virtualenv not found. Run install.sh first." >&2
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
exec python -m myvoice.app "$@"
