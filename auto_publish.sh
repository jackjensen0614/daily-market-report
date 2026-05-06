#!/usr/bin/env bash
# auto_publish.sh — commit + push scorecard/briefing data so GitHub Pages
# reflects the latest grading. Called from launchd jobs after the report runs.
# Errors are logged but never fatal — a failed push retries next run.

set -uo pipefail

cd "$(dirname "$0")"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "auto_publish: not a git repo, skipping"; exit 0
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "main" ]; then
  echo "auto_publish: not on main (on $BRANCH), skipping"; exit 0
fi

git add scorecard_history.json 2>/dev/null || true
for f in briefing-*.json; do
  [ -e "$f" ] && git add "$f"
done

if git diff --cached --quiet; then
  echo "auto_publish: nothing to commit"; exit 0
fi

DATE=$(date +%Y-%m-%d)
if ! git commit -m "auto: scorecard + briefing data for $DATE"; then
  echo "auto_publish: commit failed"; exit 0
fi

if ! git push origin main; then
  echo "auto_publish: push failed (will retry next run)"; exit 0
fi

echo "auto_publish: published $DATE"
