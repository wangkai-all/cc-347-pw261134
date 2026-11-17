#!/usr/bin/env bash
# Run the whole test suite in isolated Python mode (-I): no cwd or user
# site-packages on sys.path, so a planted json.py next to downloaded data
# can never be imported. The venv's .pth makes annarchive importable.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -I -m unittest discover -s tests -v
