#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git diff --check
fi

if grep -R -n -E '/Users/|/home/[^/ ]+|/www/|ardilla|cuy\.cl|dps\.live' \
    --exclude-dir=.git --exclude-dir=.venv --exclude-dir=.cache \
    --exclude-dir=eigen \
    --exclude-dir=__pycache__ --exclude-dir=build --exclude-dir=work --exclude-dir=outputs \
    --exclude='*.pyc' --exclude=verify-repository.sh .; then
    echo "Potential machine-specific value found; review before publishing." >&2
    exit 1
fi

echo "Repository verification passed."
