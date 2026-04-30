You are working in ~/Documents/Claude/Projects/Daily Report/.

The main script is daily_market_report.py. It generates report.html, an HTML
dashboard checked each morning as US markets open. Existing sections:
- Indices & Macro tiles
- Stock movers (gainers/losers/most-active) with news
- Crypto top 20 with news
- Today's Setup (earnings + econ events)
- A "Morning Briefing" modal (FAB button) populated by AI synthesis or a
  data-only fallback (_build_data_briefing / render_briefing_block)

Run mode is `./run.command` (uses ./venv). Daily snapshot caches to
.cache/last_snapshot.json. Reports overwrite report.html.

Goal: extend the report to be more useful at the moment of US market open,
add a learning loop, and improve flow. Implement the tasks below in order.
Commit after each task with a clear message. Run `./run.command --offline`
between tasks to confirm the report still renders, and `./run.command` once
at the end to confirm fresh fetches still work. Keep all new code consistent
with existing style (typed dataclasses, log()/warn() helpers, no new heavy
deps if avoidable — yfinance + requests is fine).

TASK 1 — Color-code indices inside the Morning Briefing
TASK 2 — Pre-Market & Overnight bar (top of report)
TASK 3 — Sector Heatmap
TASK 4 — Prediction Scorecard
TASK 5 — Sentiment & crypto regime row
TASK 6 — Sticky top nav + watchlist
TASK 7 — Earnings reactions (last night's prints)
