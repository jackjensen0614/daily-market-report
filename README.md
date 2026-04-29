# Daily Market Report

A local macOS app that analyzes yesterday's stock and crypto action and previews today's catalysts. Generates a polished HTML dashboard you can check each morning as markets open.

## Quick start (3 commands)

```bash
cd ~/Documents/Claude/Projects/Daily\ Report
./setup.sh                # one-time: installs deps in ./venv
./install_schedule.sh     # one-time: schedules launchd job for 6:00 AM weekdays
```

That's it. Tomorrow at 6:00 AM the report generates and opens in your browser automatically. To run it manually any time, double-click `run.command` or run `./run.command`. To preview right now: `launchctl start com.jack.daily-market-report`.

## What it does

For the **prior trading day**:
- Major indices (S&P 500, Dow, Nasdaq, Russell 2000, VIX) plus macro context (USD index, 10Y yield, gold, WTI)
- Top 10 gainers, top 10 losers, and 10 most active stocks (by volume)
- Top 20 cryptocurrencies by market cap with 24h changes
- Recent news headlines linked to each mover, so you can see *why*
- AI-synthesized narrative explaining yesterday's session (optional — needs an Anthropic API key)

For the **day ahead**:
- Earnings calendar (companies reporting before/after open, with EPS estimates)
- Economic events and Fed data releases
- AI-generated "tickers to watch" with one-line rationales (optional)
- Crypto outlook and risk notes (optional)

## Setup (one-time)

1. Open Terminal and `cd` into this folder (`~/Documents/Claude/Projects/Daily Report`).
2. Run setup:
   ```bash
   ./setup.sh
   ```
   This creates a virtualenv in `venv/` and installs dependencies. It also copies `.env.example` → `.env`.
3. *(Optional but recommended)* Open `.env` and paste your Anthropic API key to enable AI synthesis:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
   Get a key at <https://console.anthropic.com/settings/keys>. Without it the report still works — you just get the headlines without the "why" summaries or the tickers-to-watch list.

If `./setup.sh` fails with "permission denied", run `chmod +x setup.sh run.command` first.

## Running it

**Easiest** — double-click `run.command` in Finder. A Terminal window will pop open, fetch the data, and launch the report in your browser.

**From Terminal**:
```bash
./run.command
```

**Flags**:
- `--no-open` — generate the report but don't open the browser
- `--no-ai` — skip AI synthesis (just headlines)
- `--offline` — re-render the last cached snapshot without hitting the network

Example:
```bash
source venv/bin/activate
python3 daily_market_report.py --no-ai
```

## Where the output goes

- `report.html` — the dashboard, opened automatically in your default browser
- `.cache/last_snapshot.json` — raw data from the last run, used by `--offline`

## Automating daily runs (6:00 AM, Mon–Fri)

The project ships with a launchd plist and a one-shot installer.

```bash
./install_schedule.sh
```

That's it. The script copies `com.jack.daily-market-report.plist` into `~/Library/LaunchAgents/`, registers it with `launchctl`, and runs `./setup.sh` first if the virtualenv hasn't been created yet.

What the schedule does at 6:00 AM weekdays:
1. Runs `daily_market_report.py` to fetch yesterday's data and today's catalysts.
2. Opens `report.html` in your default browser, so when you sit down it's already on screen.

**Test it without waiting until tomorrow:**
```bash
launchctl start com.jack.daily-market-report
```

**Check it's loaded:**
```bash
launchctl list | grep daily-market-report
```

**See logs:**
```bash
tail -f /tmp/daily-market-report.out.log
tail -f /tmp/daily-market-report.err.log
```

**Remove the schedule:**
```bash
./uninstall_schedule.sh
```

**Change the time** — open `com.jack.daily-market-report.plist`, edit the `<integer>` values under each `<key>Hour</key>` / `<key>Minute</key>`, then re-run `./install_schedule.sh`.

> **Note on sleep/closed-lid:** launchd only fires when the Mac is awake. If your laptop is asleep at 6 AM, the job runs as soon as you wake it. If you want it to run no matter what, set up a "Wake for network access" rule in System Settings → Battery → Schedule (or use `pmset` from the terminal).

## Data sources

- **Yahoo Finance** (via [yfinance](https://github.com/ranaroussi/yfinance)) — indices, gainers/losers/most-active screeners, individual quotes, and per-ticker news headlines.
- **CoinGecko** (public, no key) — crypto market data with 24h change.
- **Nasdaq Calendar API** (public, no key) — earnings and economic events for the current day.
- **Anthropic API** (optional) — market narrative, "why" per ticker, today's outlook, risk notes.

## Troubleshooting

**"No gainers returned" / empty tables** — Yahoo's free endpoints rate-limit aggressively. Wait a minute and run again, or try after US market hours when traffic is lower.

**Anthropic API errors** — check your key in `.env` and your account credit. You can always run `./run.command --no-ai` for headlines-only mode.

**yfinance / pandas version errors** — delete the `venv/` folder and re-run `./setup.sh`.

**Earnings / economic events missing** — Nasdaq's public API occasionally returns empty payloads on US holidays and very early in the morning (before ~6 a.m. ET). The rest of the report still works.

## Disclaimer

This is an informational tool. It is **not investment advice**. All data may be delayed. Always verify before trading.
