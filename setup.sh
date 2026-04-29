#!/usr/bin/env bash
# One-time setup: create a virtualenv and install dependencies.
# Run once:   ./setup.sh

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3 from https://www.python.org/downloads/ or run: brew install python"
  exit 1
fi

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Python version: $PYVER"

if [ ! -d "venv" ]; then
  echo "Creating virtualenv in ./venv…"
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Created .env. Open it and paste your Anthropic API key to unlock AI synthesis."
  echo "  open .env"
fi

echo ""
echo "✅ Setup complete."
echo "   Run the report with:   ./run.command"
echo "   Or from Terminal:      source venv/bin/activate && python3 daily_market_report.py"
