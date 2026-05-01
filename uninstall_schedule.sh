#!/usr/bin/env bash
# Remove the launchd jobs for the Daily Market Report (morning + EOD).

set -euo pipefail

TARGET_DIR="$HOME/Library/LaunchAgents"

MORNING_LABEL="com.jack.daily-market-report"
MORNING_PLIST="com.jack.daily-market-report.plist"
EOD_LABEL="com.jack.daily-market-report-eod"
EOD_PLIST="com.jack.daily-market-report-eod.plist"

remove_job() {
  local LABEL="$1"
  local PLIST_NAME="$2"
  local TARGET_PLIST="$TARGET_DIR/$PLIST_NAME"

  if [ -f "$TARGET_PLIST" ]; then
    echo "==> Unloading $LABEL…"
    launchctl unload "$TARGET_PLIST" 2>/dev/null || true
    rm "$TARGET_PLIST"
    echo "==> Removed $TARGET_PLIST"
  else
    echo "Job not installed (no plist at $TARGET_PLIST). Skipping."
  fi

  launchctl remove "$LABEL" 2>/dev/null || true
}

remove_job "$MORNING_LABEL" "$MORNING_PLIST"
remove_job "$EOD_LABEL"     "$EOD_PLIST"

echo "Done."
