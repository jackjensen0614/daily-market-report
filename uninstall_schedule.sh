#!/usr/bin/env bash
# Remove the launchd job for the Daily Market Report.

set -euo pipefail

PLIST_NAME="com.jack.daily-market-report.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LABEL="com.jack.daily-market-report"

if [ -f "$TARGET_PLIST" ]; then
  echo "==> Unloading $LABEL…"
  launchctl unload "$TARGET_PLIST" 2>/dev/null || true
  rm "$TARGET_PLIST"
  echo "==> Removed $TARGET_PLIST"
else
  echo "Job not installed (no plist at $TARGET_PLIST). Nothing to do."
fi

# Belt-and-suspenders: kill any leftover registration.
launchctl remove "$LABEL" 2>/dev/null || true

echo "Done."
