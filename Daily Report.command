#!/usr/bin/env bash
# Double-click this file in Finder to generate and open the daily report.

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "First run — installing dependencies…"
  ./setup.sh
fi

# shellcheck disable=SC1091
source venv/bin/activate

python3 daily_market_report.py "$@"

# Keep the Terminal window open so any errors stay visible
echo ""
echo "Done. Press any key to close this window."
read -n 1 -s -r
