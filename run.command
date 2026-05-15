#!/usr/bin/env bash
# Double-click in Finder to generate the daily market report.
# Handles first-time setup automatically; later runs skip what is already in place.

set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found."
  echo "Install Python 3 from https://www.python.org/downloads/"
  echo "or run: brew install python"
  exit 1
fi

if [ ! -d "venv" ]; then
  echo "Creating virtualenv in ./venv..."
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

REQ_HASH_FILE="venv/.requirements.hash"
CURRENT_HASH="$(shasum -a 256 requirements.txt | awk '{print $1}')"
STORED_HASH=""
[ -f "$REQ_HASH_FILE" ] && STORED_HASH="$(cat "$REQ_HASH_FILE")"
if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
  echo "Installing dependencies..."
  pip install --upgrade pip >/dev/null
  pip install -r requirements.txt >/dev/null
  echo "$CURRENT_HASH" > "$REQ_HASH_FILE"
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Created .env. Add your Anthropic API key to enable AI synthesis."
  echo "Opening .env in your default editor..."
  open -t .env || true
fi

echo ""
echo "Generating report..."
python3 daily_market_report.py --no-open "$@"

if [ -f "report.html" ]; then
  open report.html
fi

echo ""
echo "Done. Press any key to close this window."
read -n 1 -s -r
