#!/usr/bin/env bash
# Install the launchd job that runs the Daily Market Report at 6:00 AM weekdays.
# Idempotent — safe to run repeatedly.

set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"

PLIST_NAME="com.jack.daily-market-report.plist"
SOURCE_PLIST="$PROJECT_DIR/$PLIST_NAME"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$PLIST_NAME"
LABEL="com.jack.daily-market-report"

echo "==> Installing Daily Market Report scheduled job"
echo "    Project dir: $PROJECT_DIR"
echo "    Schedule:    6:00 AM, Monday through Friday"
echo

# 1) Make sure the project is set up first.
if [ ! -d "$PROJECT_DIR/venv" ]; then
  echo "==> First-time setup: creating virtualenv and installing deps…"
  ./setup.sh
  echo
fi

# 2) Make sure LaunchAgents exists.
mkdir -p "$TARGET_DIR"

# 3) If the job is already loaded, unload it first so we replace cleanly.
if launchctl list | grep -q "$LABEL"; then
  echo "==> Unloading existing job…"
  launchctl unload "$TARGET_PLIST" 2>/dev/null || true
fi

# 4) Copy the plist into LaunchAgents.
cp "$SOURCE_PLIST" "$TARGET_PLIST"
echo "==> Copied plist to: $TARGET_PLIST"

# 5) Load it.
launchctl load "$TARGET_PLIST"
echo "==> Loaded job: $LABEL"

echo
echo "Verify with:   launchctl list | grep $LABEL"
echo "Logs:          /tmp/daily-market-report.out.log"
echo "               /tmp/daily-market-report.err.log"
echo
echo "Test it right now (without waiting for 6am):"
echo "   launchctl start $LABEL"
echo
echo "Done. Tomorrow morning at 6:00 AM the report will generate and open in your browser."
