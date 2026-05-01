#!/usr/bin/env bash
# Install the launchd jobs for the Daily Market Report.
#   Morning job  -- 6:00 AM ET, Mon-Fri  (generates briefing + morning report)
#   EOD job      -- 4:15 PM ET, Mon-Fri  (grades today's predictions at close)
# Idempotent -- safe to run repeatedly.

set -euo pipefail

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"

echo "==> Installing Daily Market Report scheduled jobs"
echo "    Project dir: $PROJECT_DIR"
echo "    Morning job: 6:00 AM, Monday through Friday"
echo "    EOD job:     4:15 PM, Monday through Friday"
echo

# 1) Make sure the project is set up first.
if [ ! -d "$PROJECT_DIR/venv" ]; then
  echo "==> First-time setup: creating virtualenv and installing deps..."
  ./setup.sh
  echo
fi

# 2) Make sure LaunchAgents directory exists.
mkdir -p "$TARGET_DIR"

# ---- Morning job ----
MORNING_LABEL="com.jack.daily-market-report"
MORNING_PLIST="com.jack.daily-market-report.plist"
MORNING_TARGET="$TARGET_DIR/$MORNING_PLIST"

if launchctl list | grep -q "$MORNING_LABEL"; then
  echo "==> Unloading existing morning job..."
  launchctl unload "$MORNING_TARGET" 2>/dev/null || true
fi
cp "$PROJECT_DIR/$MORNING_PLIST" "$MORNING_TARGET"
echo "==> Copied morning plist to: $MORNING_TARGET"
launchctl load "$MORNING_TARGET"
echo "==> Loaded: $MORNING_LABEL"

# ---- EOD job ----
EOD_LABEL="com.jack.daily-market-report-eod"
EOD_PLIST="com.jack.daily-market-report-eod.plist"
EOD_TARGET="$TARGET_DIR/$EOD_PLIST"

if launchctl list | grep -q "$EOD_LABEL"; then
  echo "==> Unloading existing EOD job..."
  launchctl unload "$EOD_TARGET" 2>/dev/null || true
fi
cp "$PROJECT_DIR/$EOD_PLIST" "$EOD_TARGET"
echo "==> Copied EOD plist to: $EOD_TARGET"
launchctl load "$EOD_TARGET"
echo "==> Loaded: $EOD_LABEL"

echo
echo "Verify with:  launchctl list | grep com.jack.daily-market-report"
echo
echo "Logs:"
echo "   Morning: /tmp/daily-market-report.out.log"
echo "            /tmp/daily-market-report.err.log"
echo "   EOD:     /tmp/daily-market-report-eod.out.log"
echo "            /tmp/daily-market-report-eod.err.log"
echo
echo "Test morning job now:  launchctl start $MORNING_LABEL"
echo "Test EOD job now:      launchctl start $EOD_LABEL"
echo
echo "Done."
