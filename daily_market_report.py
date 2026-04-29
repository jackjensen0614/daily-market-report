#!/usr/bin/env python3
"""
Daily Market Report
===================
Pulls yesterday's stock and crypto data, synthesizes a daily briefing,
and opens an HTML dashboard in your browser.

Usage:
    python3 daily_market_report.py            # run & open the report
    python3 daily_market_report.py --no-open  # run but don't open the browser
    python3 daily_market_report.py --no-ai    # skip AI synthesis
    python3 daily_market_report.py --offline  # use the last cached data

Data sources:
    - yfinance / Yahoo Finance (stocks, news, earnings)
    - CoinGecko (crypto)
    - Nasdaq calendar API (earnings + economic events fallback)
    - Anthropic API (optional, for AI-synthesized analysis)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance is not installed. Run `./setup.sh` first.", file=sys.stderr)
    sys.exit(1)

try:
    import pandas as pd
except ImportError:
    print("ERROR: pandas is not installed. Run `./setup.sh` first.", file=sys.stderr)
    sys.exit(1)

# ------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
REPORT_PATH = SCRIPT_DIR / "report.html"
DATA_SNAPSHOT_PATH = CACHE_DIR / "last_snapshot.json"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

INDEX_TICKERS = {
    "^GSPC": "S&P 500",
    "^DJI": "Dow Jones",
    "^IXIC": "Nasdaq Composite",
    "^RUT": "Russell 2000",
    "^VIX": "VIX (Volatility)",
}

# Currencies, commodities, treasuries that contextualize the day
EXTRA_MACRO_TICKERS = {
    "DX-Y.NYB": "US Dollar Index",
    "^TNX":     "10Y Treasury Yield",
    "^TYX":     "30Y Treasury Yield",
    "GC=F":     "Gold",
    "CL=F":     "Crude Oil (WTI)",
    "SI=F":     "Silver",
    "NG=F":     "Natural Gas",
}

# Global equity indices
GLOBAL_INDICES = {
    "^GDAXI": "DAX (Germany)",
    "^FTSE":  "FTSE 100 (UK)",
    "^FCHI":  "CAC 40 (France)",
    "^N225":  "Nikkei 225 (Japan)",
    "^HSI":   "Hang Seng (HK)",
    "^AXJO":  "ASX 200 (Australia)",
    "^BSESN": "Sensex (India)",
}

CRYPTO_TOP_N = 20  # top coins by market cap on CoinGecko
MOVERS_COUNT = 10  # gainers/losers/active per category
NEWS_PER_TICKER = 3

# 11 SPDR sector ETFs — used for sector-rotation analysis.
SECTOR_ETFS: dict[str, str] = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Healthcare",
    "XLY":  "Consumer Discretionary",
    "XLP":  "Consumer Staples",
    "XLI":  "Industrials",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
    "XLC":  "Communication Services",
}

# Thresholds for surfacing technical setups in the "Signals" section.
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
NEAR_HIGH_PCT = 2.0   # within 2% of 52-week high → flagged as breakout candidate
NEAR_LOW_PCT  = 5.0   # within 5% of 52-week low  → flagged as bottoming candidate
VOL_ANOMALY_RATIO = 2.0  # last day's volume ≥ 2× 20-day average

# Fallback universe used when Yahoo's predefined screeners are rate-limited.
# Roughly the S&P 100 + Nasdaq 100 + popular high-volume retail names — enough
# liquidity to surface meaningful daily movers without flooding the API.
FALLBACK_UNIVERSE: list[str] = [
    # Mega-cap tech / S&P 100
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA", "AVGO", "ORCL",
    "BRK-B", "JPM", "V", "MA", "WMT", "JNJ", "PG", "HD", "COST", "ABBV",
    "BAC", "KO", "PEP", "TMO", "MRK", "CRM", "CVX", "AMD", "LIN", "ACN",
    "CSCO", "ADBE", "MCD", "WFC", "PFE", "ABT", "DHR", "TXN", "PM", "VZ",
    "DIS", "NEE", "COP", "QCOM", "INTC", "CMCSA", "INTU", "RTX", "BMY", "T",
    "NFLX", "AMGN", "UPS", "HON", "LOW", "SPGI", "ELV", "GS", "BA", "C",
    "BLK", "DE", "AMAT", "ETN", "ISRG", "PLD", "MS", "MDT", "BKNG", "SBUX",
    "TJX", "MDLZ", "AXP", "GILD", "ADI", "PANW", "VRTX", "REGN", "MU", "LMT",
    "SCHW", "LRCX", "CB", "CVS", "ZTS", "MMC", "PYPL", "NKE", "FI", "SO",
    "TMUS", "BSX", "DUK", "ITW", "EOG", "WM", "CCI", "EQIX", "APH", "USB",
    # Nasdaq 100 favorites not above
    "ASML", "ADP", "MELI", "PDD", "AZN", "MAR", "CDNS", "SNPS", "CRWD", "ADSK",
    "WDAY", "CHTR", "FTNT", "DDOG", "DXCM", "MRVL", "ABNB", "PCAR", "NXPI",
    "MNST", "PAYX", "ROST", "EXC", "AEP", "FAST", "BKR", "KDP", "VRSK", "CTSH",
    "CSX", "KHC", "GEHC", "BIIB", "DLTR", "ON", "CTAS", "ANSS", "ZS",
    "ALGN", "WBD", "TEAM", "LULU", "GFS", "SIRI", "ENPH", "DOCU", "EBAY", "MTCH",
    # Popular high-volume retail / meme / momentum names
    "PLTR", "SOFI", "F", "RIVN", "LCID", "NIO", "AMC", "GME", "BB", "CHWY",
    "RBLX", "DKNG", "COIN", "HOOD", "AFRM", "UPST", "DASH", "UBER", "LYFT",
    "SNAP", "PINS", "ROKU", "SHOP", "SQ", "ZM", "TWLO", "NET", "SNOW",
    "MARA", "RIOT", "MSTR", "DJT", "TLRY", "SMCI", "ARM", "CART", "RDDT",
    # Banks, insurers, energy, materials, pharma extras
    "TFC", "PNC", "AIG", "MET", "PRU", "TRV", "PSX", "VLO", "MPC",
    "OXY", "SLB", "FCX", "NEM", "DOW", "DD", "PPG", "SHW",
    "LLY", "NOW", "TTD",
    # Big ETFs (often dominate "most-active" by dollar volume)
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "EEM", "GLD", "SLV", "USO",
    "TLT", "HYG", "XLF", "XLE", "XLK", "XLV", "XLY", "XLP", "XLI", "XLB",
    "XLRE", "XLU", "XLC", "ARKK", "TQQQ", "SQQQ", "SOXL", "TNA",
]

# ------------------------------------------------------------------------
# Dataclasses
# ------------------------------------------------------------------------
@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    volume: int | None = None
    market_cap: float | None = None
    dollar_volume: float | None = None


@dataclass
class NewsItem:
    title: str
    publisher: str = ""
    link: str = ""
    published: str = ""


@dataclass
class MoverWithNews:
    quote: Quote
    news: list[NewsItem] = field(default_factory=list)
    ai_why: str = ""


@dataclass
class CalendarEvent:
    time: str
    symbol_or_event: str
    description: str
    extra: str = ""  # e.g. EPS estimate, prior value


@dataclass
class Snapshot:
    prior_session_date: str
    generated_at: str
    indices: list[Quote] = field(default_factory=list)
    macro: list[Quote] = field(default_factory=list)
    gainers: list[MoverWithNews] = field(default_factory=list)
    losers: list[MoverWithNews] = field(default_factory=list)
    most_active: list[MoverWithNews] = field(default_factory=list)
    crypto: list[MoverWithNews] = field(default_factory=list)
    crypto_gainers: list[MoverWithNews] = field(default_factory=list)
    crypto_losers: list[MoverWithNews] = field(default_factory=list)
    global_indices: list[Quote] = field(default_factory=list)
    earnings_today: list[CalendarEvent] = field(default_factory=list)
    econ_events_today: list[CalendarEvent] = field(default_factory=list)
    ai: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------
# Logging helpers
# ------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def warn(msg: str, snap: Snapshot | None = None) -> None:
    log(f"WARN: {msg}")
    if snap is not None:
        snap.warnings.append(msg)


# ------------------------------------------------------------------------
# Trading-day helpers
# ------------------------------------------------------------------------
def get_prior_trading_day() -> str:
    """Return ISO date of the most recent completed trading day, based on SPY data."""
    try:
        spy = yf.Ticker("SPY")
        hist = spy.history(period="7d", auto_adjust=False)
        if not hist.empty:
            return hist.index[-1].date().isoformat()
    except Exception as e:
        log(f"Could not determine trading day from SPY: {e}")
    # Fallback: if it's a weekday and past close, use today; otherwise last weekday
    now = datetime.now(ET)
    d = now.date()
    # If it's before market close (~4pm ET), use the prior day
    if now.hour < 16:
        d = d - timedelta(days=1)
    # Skip weekends
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d.isoformat()


# ------------------------------------------------------------------------
# Yahoo Finance helpers
# ------------------------------------------------------------------------
def _last_two(hist: pd.DataFrame) -> tuple[float, float, int] | None:
    if hist is None or hist.empty or len(hist) < 2:
        return None
    try:
        close = hist["Close"].dropna()
        vol = hist["Volume"].dropna() if "Volume" in hist else None
        if len(close) < 2:
            return None
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        volume = int(vol.iloc[-1]) if vol is not None and not vol.empty else 0
        return last, prev, volume
    except Exception:
        return None


def fetch_quotes(symbols_with_names: dict[str, str]) -> list[Quote]:
    """Fetch last-2-day close and compute change for a set of symbols."""
    if not symbols_with_names:
        return []
    out: list[Quote] = []
    symbols = list(symbols_with_names.keys())
    try:
        data = yf.download(
            symbols,
            period="7d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
    except Exception as e:
        log(f"Bulk download failed: {e}; falling back per-ticker")
        data = None

    for sym in symbols:
        name = symbols_with_names[sym]
        hist = None
        try:
            if data is not None and not data.empty:
                if len(symbols) == 1:
                    hist = data
                elif sym in data.columns.get_level_values(0):
                    hist = data[sym]
            if hist is None or hist.empty:
                hist = yf.Ticker(sym).history(period="7d", auto_adjust=False)
        except Exception as e:
            log(f"  {sym} history failed: {e}")
            continue
        pair = _last_two(hist)
        if pair is None:
            continue
        last, prev, vol = pair
        chg = last - prev
        pct = (chg / prev) * 100.0 if prev else 0.0
        out.append(
            Quote(
                symbol=sym,
                name=name,
                price=last,
                change=chg,
                change_pct=pct,
                volume=vol if vol else None,
                dollar_volume=(last * vol) if vol else None,
            )
        )
    return out


def fetch_screener(screener_id: str, count: int = 25) -> list[Quote]:
    """Use Yahoo's predefined screeners for gainers/losers/most_actives.

    Supported IDs: day_gainers, day_losers, most_actives
    """
    url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    params = {"scrIds": screener_id, "count": str(count)}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        # Prime cookies via main site first — Yahoo often requires them
        session = requests.Session()
        session.headers.update(headers)
        session.get("https://finance.yahoo.com", timeout=10)
        r = session.get(url, params=params, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log(f"Screener {screener_id} failed: {e}")
        return []

    try:
        quotes = payload["finance"]["result"][0]["quotes"]
    except (KeyError, IndexError, TypeError):
        return []

    out: list[Quote] = []
    for q in quotes:
        try:
            sym = q.get("symbol")
            if not sym:
                continue
            price = q.get("regularMarketPrice") or q.get("regularMarketPreviousClose") or 0
            prev = q.get("regularMarketPreviousClose") or price
            chg = q.get("regularMarketChange", price - prev)
            pct = q.get("regularMarketChangePercent", 0.0)
            volume = q.get("regularMarketVolume", 0) or 0
            mcap = q.get("marketCap")
            name = q.get("shortName") or q.get("longName") or sym
            out.append(
                Quote(
                    symbol=sym,
                    name=name,
                    price=float(price),
                    change=float(chg),
                    change_pct=float(pct),
                    volume=int(volume),
                    market_cap=float(mcap) if mcap else None,
                    dollar_volume=float(price) * float(volume) if volume else None,
                )
            )
        except Exception as e:
            log(f"  skipping bad screener row: {e}")
            continue
    return out


def fetch_movers_from_universe(
    count: int = MOVERS_COUNT,
) -> tuple[list[Quote], list[Quote], list[Quote]]:
    """Compute gainers / losers / most-active locally from FALLBACK_UNIVERSE.

    Used when Yahoo's predefined screeners are rate-limited or empty. Pulls
    last-2-day data for ~200 liquid tickers in one bulk yfinance call, then
    sorts client-side. Avoids the screener endpoint entirely.
    """
    log(f"Universe scan: pulling {len(FALLBACK_UNIVERSE)} liquid tickers in bulk…")
    universe = list(dict.fromkeys(FALLBACK_UNIVERSE))  # de-dupe, preserve order
    sym_to_name = {s: s for s in universe}
    quotes = fetch_quotes(sym_to_name)
    if not quotes:
        return [], [], []

    # Prefer real names where we have them via a lightweight per-ticker lookup
    # for just the top movers (avoids 500 .info calls).
    by_pct_desc = sorted(quotes, key=lambda q: q.change_pct, reverse=True)
    by_pct_asc = sorted(quotes, key=lambda q: q.change_pct)
    by_dvol = sorted(
        quotes, key=lambda q: (q.dollar_volume or 0.0), reverse=True
    )
    gainers = by_pct_desc[:count]
    losers = by_pct_asc[:count]
    active = by_dvol[:count]

    # Hydrate names just for the surfaced movers (cheap)
    seen = {q.symbol for q in (gainers + losers + active)}
    name_cache: dict[str, str] = {}
    for sym in seen:
        try:
            info = yf.Ticker(sym).info or {}
            name_cache[sym] = info.get("shortName") or info.get("longName") or sym
        except Exception:
            name_cache[sym] = sym
    for q in gainers + losers + active:
        q.name = name_cache.get(q.symbol, q.symbol)
    return gainers, losers, active


def fetch_ticker_news(ticker: str, limit: int = NEWS_PER_TICKER) -> list[NewsItem]:
    """Return recent news items for a ticker via yfinance."""
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as e:
        log(f"  news for {ticker} failed: {e}")
        return []

    items: list[NewsItem] = []
    for n in raw[:limit]:
        try:
            # yfinance has shipped two formats over time
            if "content" in n and isinstance(n["content"], dict):
                c = n["content"]
                title = c.get("title", "")
                publisher = (c.get("provider") or {}).get("displayName", "")
                link = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url", "")
                pub_dt = c.get("pubDate", "")
                items.append(NewsItem(title=title, publisher=publisher, link=link, published=pub_dt))
            else:
                title = n.get("title", "")
                publisher = n.get("publisher", "")
                link = n.get("link", "")
                pub_ts = n.get("providerPublishTime")
                pub_dt = ""
                if isinstance(pub_ts, (int, float)):
                    pub_dt = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
                items.append(NewsItem(title=title, publisher=publisher, link=link, published=pub_dt))
        except Exception as e:
            log(f"  skipping news row for {ticker}: {e}")
    return items


def attach_news(movers: list[Quote], concurrency: int = 6) -> list[MoverWithNews]:
    """Fetch news concurrently for a list of quotes."""
    out: dict[str, MoverWithNews] = {q.symbol: MoverWithNews(quote=q) for q in movers}
    if not movers:
        return []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_ticker_news, q.symbol): q.symbol for q in movers}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                out[sym].news = fut.result()
            except Exception as e:
                log(f"  news future for {sym} failed: {e}")
    return [out[q.symbol] for q in movers]


# ------------------------------------------------------------------------
# CoinGecko
# ------------------------------------------------------------------------
COINGECKO_BASE = "https://api.coingecko.com/api/v3"


def fetch_crypto_markets(n: int = CRYPTO_TOP_N) -> list[Quote]:
    """Top N coins by market cap, with 24h change."""
    url = f"{COINGECKO_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": str(n),
        "page": "1",
        "price_change_percentage": "24h",
    }
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        r.raise_for_status()
        coins = r.json()
    except Exception as e:
        log(f"CoinGecko markets failed: {e}")
        return []

    out: list[Quote] = []
    for c in coins:
        try:
            price = float(c.get("current_price") or 0)
            pct = float(c.get("price_change_percentage_24h") or 0)
            change = float(c.get("price_change_24h") or 0)
            vol = float(c.get("total_volume") or 0)
            mcap = float(c.get("market_cap") or 0)
            out.append(
                Quote(
                    symbol=(c.get("symbol") or "").upper(),
                    name=c.get("name", ""),
                    price=price,
                    change=change,
                    change_pct=pct,
                    volume=int(vol),
                    market_cap=mcap,
                    dollar_volume=vol,
                )
            )
        except Exception as e:
            log(f"  skipping crypto row: {e}")
    return out


def fetch_crypto_news_item(coin: Quote) -> list[NewsItem]:
    """Fetch news via yfinance for ticker form (e.g. BTC-USD)."""
    # Map symbol → yfinance ticker (most major coins: SYM-USD)
    yf_sym = f"{coin.symbol}-USD"
    return fetch_ticker_news(yf_sym, limit=NEWS_PER_TICKER)


def attach_crypto_news(coins: list[Quote]) -> list[MoverWithNews]:
    out: dict[str, MoverWithNews] = {c.symbol: MoverWithNews(quote=c) for c in coins}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(fetch_crypto_news_item, c): c.symbol for c in coins}
        for fut in as_completed(futs):
            sym = futs[fut]
            try:
                out[sym].news = fut.result()
            except Exception:
                pass
    return [out[c.symbol] for c in coins]


# ------------------------------------------------------------------------
# Calendar APIs
# ------------------------------------------------------------------------
def fetch_earnings_calendar(date_str: str) -> list[CalendarEvent]:
    """Earnings from Nasdaq's public calendar endpoint."""
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nasdaq.com/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"Nasdaq earnings calendar failed: {e}")
        return []

    rows = ((data.get("data") or {}).get("rows")) or []
    out: list[CalendarEvent] = []
    for row in rows:
        try:
            sym = row.get("symbol", "")
            name = row.get("name", "")
            time = row.get("time", "")
            eps_est = row.get("epsForecast", "") or row.get("eps_forecast", "")
            mcap = row.get("marketCap", "")
            extra_bits = []
            if eps_est:
                extra_bits.append(f"EPS est {eps_est}")
            if mcap:
                extra_bits.append(f"Mkt cap {mcap}")
            out.append(
                CalendarEvent(
                    time=time or "—",
                    symbol_or_event=sym,
                    description=name,
                    extra=" · ".join(extra_bits),
                )
            )
        except Exception as e:
            log(f"  skipping earnings row: {e}")
    return out


def fetch_econ_events(date_str: str) -> list[CalendarEvent]:
    """Economic events via Nasdaq calendar."""
    url = f"https://api.nasdaq.com/api/calendar/economicevents?date={date_str}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nasdaq.com/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"Nasdaq econ events failed: {e}")
        return []

    rows = ((data.get("data") or {}).get("rows")) or []
    out: list[CalendarEvent] = []
    for row in rows:
        try:
            desc = row.get("eventName", "")
            country = row.get("gmt", "") or row.get("country", "")
            actual = row.get("actual", "")
            consensus = row.get("consensus", "")
            previous = row.get("previous", "")
            time = row.get("time", "")
            extra_bits = []
            if consensus:
                extra_bits.append(f"Cons. {consensus}")
            if previous:
                extra_bits.append(f"Prior {previous}")
            if actual:
                extra_bits.append(f"Actual {actual}")
            out.append(
                CalendarEvent(
                    time=time or "—",
                    symbol_or_event=country or "",
                    description=desc,
                    extra=" · ".join(extra_bits),
                )
            )
        except Exception as e:
            log(f"  skipping econ row: {e}")
    return out


# ------------------------------------------------------------------------
# AI synthesis (optional)
# ------------------------------------------------------------------------
def get_anthropic_client():
    """Return Anthropic client if API key is set, else None."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Try a .env file
        env_path = SCRIPT_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("ANTHROPIC_API_KEY"):
                    _, _, v = line.partition("=")
                    api_key = v.strip().strip('"').strip("'")
                    if api_key:
                        os.environ["ANTHROPIC_API_KEY"] = api_key
                        break
    if not api_key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        log("anthropic package not installed — skipping AI synthesis.")
        return None
    return Anthropic(api_key=api_key)


def build_ai_context(snap: Snapshot) -> dict:
    """Compact JSON payload to send to Claude."""
    def mw_brief(m: MoverWithNews) -> dict:
        return {
            "symbol": m.quote.symbol,
            "name": m.quote.name,
            "change_pct": round(m.quote.change_pct, 2),
            "price": round(m.quote.price, 4),
            "headlines": [h.title for h in m.news][:3],
        }

    return {
        "prior_session": snap.prior_session_date,
        "today": snap.generated_at[:10],
        "indices": [{"name": q.name, "change_pct": round(q.change_pct, 2)} for q in snap.indices],
        "macro": [{"name": q.name, "change_pct": round(q.change_pct, 2)} for q in snap.macro],
        "top_gainers": [mw_brief(m) for m in snap.gainers[:8]],
        "top_losers": [mw_brief(m) for m in snap.losers[:8]],
        "most_active": [mw_brief(m) for m in snap.most_active[:8]],
        "crypto_top": [mw_brief(m) for m in snap.crypto[:10]],
        "earnings_today": [
            {"sym": e.symbol_or_event, "name": e.description, "time": e.time, "extra": e.extra}
            for e in snap.earnings_today[:30]
        ],
        "econ_events_today": [
            {"event": e.description, "time": e.time, "extra": e.extra}
            for e in snap.econ_events_today[:20]
        ],
    }


AI_SYSTEM_PROMPT = """You are a professional, measured markets strategist writing a daily briefing
for a sophisticated individual investor. You are data-driven, cite specifics, avoid hype,
and never give personalized financial advice. You always contextualize moves (macro, sector,
company-specific) rather than just restating the numbers. Keep paragraphs tight.
Output strictly valid JSON with no markdown fences."""


AI_USER_PROMPT = """Given the compact market data below, return JSON with these exact keys:

{{
  "market_narrative": "3-4 sentences summarizing yesterday's session across equities, macro, and crypto",
  "why_gainers":  {{ "<TICKER>": "one-sentence cause" }},
  "why_losers":   {{ "<TICKER>": "one-sentence cause" }},
  "why_active":   {{ "<TICKER>": "one-sentence cause" }},
  "why_crypto":   {{ "<SYM>": "one-sentence cause" }},
  "today_outlook": "3-5 sentences on today's setup, referencing earnings and econ data",
  "tickers_to_watch": [ {{ "ticker": "XYZ", "rationale": "why to watch today in one line" }}, ... 5-8 items ],
  "crypto_outlook": "2-3 sentences on crypto for today",
  "risk_notes": "1-2 sentences highlighting key risks or things that would invalidate the setup"
}}

Ground every claim in the data/headlines provided. If headlines don't explain a move, say
"no clear catalyst in headlines" rather than speculating. Do not invent tickers or events.

DATA:
{data}
"""


BRIEFING_SYSTEM_PROMPT = """You are a professional markets strategist writing a concise morning briefing
for a sophisticated individual investor. Be data-driven, cite specific numbers, avoid hype,
never give personalized financial advice. Keep paragraphs tight — 3-5 sentences max each.
Output strictly valid JSON with no markdown fences."""

BRIEFING_USER_PROMPT = """Given the market data below, return JSON with EXACTLY these keys:

{{
  "exec_summary": ["one-line bullet 1", "one-line bullet 2", "one-line bullet 3", "one-line bullet 4", "one-line bullet 5"],
  "session_recap": "3-4 paragraphs. Lead with index moves and VIX, then sector/macro (cite crude, yields, gold), then 2-3 biggest individual stock moves tied to their specific news headline.",
  "crypto_recap": "1-2 paragraphs. BTC/ETH/XRP levels, top gainer and top loser in the top 20, notable volume or dominance shifts.",
  "today_setup": "Walk through tonight's/today's earnings (highlight highest-impact names with EPS estimates) and any economic events. For each name give one line on how it could shape the tape.",
  "tickers_to_watch": [{{"ticker": "XYZ", "rationale": "specific signal — e.g. RSI 28 oversold, earnings beat +8%, continuation from yesterday"}}, ...6-10 items],
  "crypto_outlook": "1-2 paragraphs on crypto positioning for the next 24 hours.",
  "risk_notes": ["concrete risk bullet 1", "concrete risk bullet 2", "concrete risk bullet 3"]
}}

Ground every claim in the data. Cite specific numbers. Do not invent tickers or events.

DATA:
{data}
"""


def generate_briefing(snap: Snapshot) -> dict | None:
    """Generate the full morning briefing via Anthropic API."""
    client = get_anthropic_client()
    if client is None:
        return None

    ctx = build_ai_context(snap)
    try:
        resp = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=6000,
            system=BRIEFING_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": BRIEFING_USER_PROMPT.format(data=json.dumps(ctx, indent=2))}
            ],
        )
    except Exception as e:
        log(f"Briefing generation failed (modal will be skipped): {e}")
        return None

    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
        log("Briefing: unparseable JSON returned — modal will be skipped.")
        return None


def run_ai_synthesis(snap: Snapshot) -> dict:
    client = get_anthropic_client()
    if client is None:
        return {"_skipped": "ANTHROPIC_API_KEY not set; AI synthesis skipped. Headlines still shown."}

    ctx = build_ai_context(snap)
    try:
        resp = client.messages.create(
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=4000,
            system=AI_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": AI_USER_PROMPT.format(data=json.dumps(ctx, indent=2))}
            ],
        )
    except Exception as e:
        # Log only — do NOT surface in the report. Common cases (no credit
        # balance, network blip, expired key) are recoverable and shouldn't
        # ruin the rest of the briefing.
        log(f"Anthropic API call failed (AI sections will be skipped): {e}")
        return {"_error": str(e)}

    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text

    # Strip code fences if any, then parse
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n", "", text)
        text = re.sub(r"\n```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: find the first/last brace
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
        warn("AI returned unparseable JSON — showing raw text.", snap)
        return {"_raw": text}


# ------------------------------------------------------------------------
# HTML rendering
# ------------------------------------------------------------------------
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Daily Market Report · {prior_date}</title>
<meta name="viewport" content="width=device-width,initial-scale=1" />
<style>
:root {{
  --bg: #0b0d12;
  --bg-panel: #12151d;
  --bg-panel-2: #181c26;
  --border: #232836;
  --text: #e6e8ef;
  --text-dim: #8a92a6;
  --text-faint: #5a6278;
  --accent: #6ee7b7;
  --green: #22c55e;
  --green-dim: #0f3a22;
  --red: #ef4444;
  --red-dim: #3a1414;
  --yellow: #f59e0b;
  --blue: #60a5fa;
  --purple: #a78bfa;
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif;
  font-size: 14px; line-height: 1.45;
}}
.wrap {{
  max-width: 1400px; margin: 0 auto; padding: 28px 32px 72px;
}}
header {{
  display: flex; align-items: center; justify-content: space-between;
  border-bottom: 1px solid var(--border); padding-bottom: 18px; margin-bottom: 24px;
  flex-wrap: wrap; gap: 12px;
}}
h1 {{
  font-size: 22px; font-weight: 700; margin: 0; letter-spacing: -0.01em;
}}
h1 .subtle {{ color: var(--text-dim); font-weight: 400; margin-left: 10px; font-size: 15px; }}
h2 {{
  font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--text-dim); margin: 32px 0 12px;
}}
.meta {{ color: var(--text-faint); font-size: 12px; }}
.warn {{
  background: #2a1d08; border: 1px solid #5b3d12; color: #f0c77a;
  padding: 10px 14px; border-radius: 6px; margin: 12px 0; font-size: 13px;
}}

/* Index tiles */
.index-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px;
}}
.tile {{
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px;
}}
.tile .label {{ color: var(--text-dim); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
.tile .value {{ font-size: 22px; font-weight: 600; margin-top: 6px; letter-spacing: -0.01em; }}
.tile .delta {{ font-size: 13px; margin-top: 2px; }}

/* Colored numbers */
.up   {{ color: var(--green); }}
.down {{ color: var(--red); }}
.flat {{ color: var(--text-dim); }}
.num  {{ font-variant-numeric: tabular-nums; }}

/* Main 2-col grid for mover sections */
.cols {{
  display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 24px;
}}
@media (max-width: 980px) {{ .cols {{ grid-template-columns: 1fr; }} }}
.panel {{
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 0; overflow: hidden;
}}
.panel-head {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 16px; border-bottom: 1px solid var(--border);
}}
.panel-head h3 {{
  margin: 0; font-size: 14px; font-weight: 600; letter-spacing: 0.02em;
}}
.panel-head .sub {{ color: var(--text-faint); font-size: 12px; }}

/* Mover rows */
.mover {{
  display: grid; grid-template-columns: 64px 1fr auto; gap: 10px;
  padding: 12px 16px; border-bottom: 1px solid var(--border); align-items: start;
}}
.mover:last-child {{ border-bottom: none; }}
.mover .sym {{ font-weight: 700; letter-spacing: 0.02em; }}
.mover .name {{ color: var(--text-dim); font-size: 12px; margin-top: 2px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mover .why {{ color: var(--text); font-size: 12.5px; margin-top: 6px; padding-left: 10px; border-left: 2px solid var(--border); color: #cfd4e3; }}
.mover .news {{ margin-top: 6px; }}
.mover .news a {{ display: block; color: #b7c0d6; font-size: 12px; text-decoration: none; margin: 2px 0; }}
.mover .news a:hover {{ color: #dce3f5; text-decoration: underline; }}
.mover .news .pub {{ color: var(--text-faint); font-size: 11px; }}
.mover .right {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
.mover .right .pct {{ font-weight: 600; font-size: 15px; }}
.mover .right .px {{ color: var(--text-dim); font-size: 12px; margin-top: 2px; }}
.pill {{
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  background: var(--bg-panel-2); border: 1px solid var(--border);
  color: var(--text-dim); font-size: 11px; margin-left: 6px;
}}
.pill.up {{ background: #0e2a1a; border-color: #1a4c30; color: #6ee7b7; }}
.pill.down {{ background: #2a0e0e; border-color: #4c1a1a; color: #fca5a5; }}

/* Narrative cards */
.narr {{
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 20px; margin: 12px 0 8px;
}}
.narr p {{ margin: 8px 0; color: #dde2f0; }}
.narr .label {{
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-dim);
  margin-bottom: 6px;
}}
.narr.risk {{ border-color: #5b3d12; background: #1f1708; }}
.narr.risk p {{ color: #f0c77a; }}

/* Morning Briefing — FAB + modal */
.briefing-fab {{
  position: fixed; bottom: 28px; right: 28px; z-index: 100;
  background: var(--accent); color: #0b0d12;
  border: none; border-radius: 999px; padding: 11px 22px;
  font-size: 13px; font-weight: 700; cursor: pointer; letter-spacing: 0.02em;
  box-shadow: 0 4px 20px rgba(110,231,183,0.35);
  transition: transform 0.15s, box-shadow 0.15s;
}}
.briefing-fab:hover {{ transform: translateY(-2px); box-shadow: 0 6px 28px rgba(110,231,183,0.5); }}
.briefing-backdrop {{
  display: none; position: fixed; inset: 0; z-index: 200;
  background: rgba(0,0,0,0.72); backdrop-filter: blur(3px);
  align-items: flex-start; justify-content: center; padding: 40px 20px;
  overflow-y: auto;
}}
.briefing-backdrop.open {{ display: flex; }}
.briefing-modal {{
  background: var(--bg-panel); border: 1px solid var(--border); border-radius: 14px;
  width: 100%; max-width: 820px; flex-shrink: 0;
  overflow: hidden; margin: auto;
}}
.briefing-modal-head {{
  position: sticky; top: 0; z-index: 1;
  display: flex; align-items: center; gap: 10px;
  padding: 14px 20px; border-bottom: 1px solid var(--border);
  background: #111827;
}}
.briefing-modal-head h3 {{
  margin: 0; font-size: 15px; font-weight: 700; color: var(--accent);
}}
.briefing-modal-head .bdate {{ color: var(--text-faint); font-size: 12px; margin-left: auto; margin-right: 10px; }}
.briefing-close {{
  background: none; border: 1px solid var(--border); color: var(--text-dim);
  border-radius: 6px; padding: 3px 10px; cursor: pointer; font-size: 16px; line-height: 1.4;
  flex-shrink: 0;
}}
.briefing-close:hover {{ color: var(--text); border-color: var(--text-dim); }}
.exec-bar {{
  padding: 14px 20px; border-bottom: 1px solid var(--border);
  background: #0a1a10;
}}
.exec-bar .exec-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--accent); font-weight: 600; margin-bottom: 8px;
}}
.exec-bar ol {{ margin: 0; padding-left: 18px; }}
.exec-bar li {{ color: #b8e8cc; font-size: 13px; line-height: 1.55; margin: 5px 0; }}
.briefing-section {{ padding: 16px 20px; border-bottom: 1px solid var(--border); }}
.briefing-section:last-child {{ border-bottom: none; }}
.briefing-section .bs-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--text-dim); font-weight: 600; margin-bottom: 10px;
}}
.briefing-section p {{ margin: 0 0 10px; color: #dde2f0; font-size: 13.5px; line-height: 1.65; }}
.briefing-section p:last-child {{ margin-bottom: 0; }}
.briefing-section.crypto .bs-label {{ color: var(--purple); }}
.briefing-section.crypto p {{ color: #d4cbf8; }}
.briefing-section.setup .bs-label {{ color: var(--blue); }}
.briefing-section.setup p {{ color: #c9dbf5; }}
.briefing-section.risk {{ background: #1a1508; }}
.briefing-section.risk .bs-label {{ color: var(--yellow); }}
.briefing-section.risk ul {{ margin: 0; padding-left: 20px; list-style: disc; }}
.briefing-section.risk li {{ color: #f0c77a; font-size: 13px; line-height: 1.55; margin: 6px 0; }}
.briefing-watch {{ padding: 16px 20px; border-bottom: 1px solid var(--border); }}
.briefing-watch .bs-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--text-dim); font-weight: 600; margin-bottom: 10px;
}}
.b-watch-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 8px; }}
.b-watch-item {{
  background: var(--bg-panel-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px;
}}
.b-watch-item .sym {{ font-weight: 700; font-size: 13px; }}
.b-watch-item .why {{ color: var(--text-dim); font-size: 12px; margin-top: 4px; line-height: 1.45; }}

/* Watch list */
.watch-list {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; margin-top: 8px; }}
.watch-item {{
  background: var(--bg-panel-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px;
}}
.watch-item .sym {{ font-weight: 700; }}
.watch-item .why {{ color: var(--text-dim); font-size: 12.5px; margin-top: 4px; }}

/* Table for calendars */
table {{
  width: 100%; border-collapse: collapse; font-size: 13px;
}}
th, td {{
  text-align: left; padding: 10px 16px; border-bottom: 1px solid var(--border);
  vertical-align: top;
}}
th {{
  color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  font-weight: 600;
}}
.briefing-modal table th, .briefing-modal table td {{ padding: 5px 8px; }}
tr:last-child td {{ border-bottom: none; }}
td.sym {{ font-weight: 600; white-space: nowrap; }}
td.time {{ color: var(--text-dim); white-space: nowrap; }}

footer {{
  margin-top: 48px; padding-top: 20px; border-top: 1px solid var(--border);
  color: var(--text-faint); font-size: 12px; text-align: center;
}}
a {{ color: #8ab4f8; }}
.refresh-btn {{
  background: #1e2535; border: 1px solid #3d4560; color: #c8cfdf;
  border-radius: 6px; padding: 6px 14px; font-size: 13px; font-weight: 600;
  cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
  transition: background .15s, border-color .15s, color .15s; white-space: nowrap;
}}
.refresh-btn:hover {{ background: #252d42; border-color: var(--accent); color: var(--accent); }}
.refresh-btn .spin {{ font-size: 15px; line-height: 1; }}
.refresh-btn.spinning .spin {{ display: inline-block; animation: spin .6s linear infinite; }}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<div class="wrap">

<header>
  <div>
    <h1>Daily Market Report
      <span class="subtle">Prior session · {prior_date_human}</span>
    </h1>
    <div class="meta">Generated {generated_human} · Today is {today_human}</div>
  </div>
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <div class="meta">{warnings_html}</div>
    <button class="refresh-btn" id="refresh-btn" onclick="doRefresh()">
      <span class="spin">&#8635;</span> <span id="refresh-label">Refresh</span>
    </button>
    <span id="refresh-status" class="meta"></span>
  </div>
</header>

{briefing_block}

<h2>Indices & Macro</h2>
<div class="index-grid">
  {index_tiles}
</div>

{ai_narrative_block}

<h2>Stock Movers · {prior_date_human}</h2>
<div class="cols">
  <div class="panel">
    <div class="panel-head"><h3>Top Gainers</h3><div class="sub">Largest % moves up</div></div>
    {gainers_rows}
  </div>
  <div class="panel">
    <div class="panel-head"><h3>Top Losers</h3><div class="sub">Largest % moves down</div></div>
    {losers_rows}
  </div>
</div>

<div class="cols" style="margin-top: 18px;">
  <div class="panel">
    <div class="panel-head"><h3>Most Active</h3><div class="sub">Sorted by volume</div></div>
    {active_rows}
  </div>
  <div class="panel">
    <div class="panel-head"><h3>Crypto · Top {crypto_top_n}</h3><div class="sub">By market cap · 24h change</div></div>
    {crypto_rows}
  </div>
</div>

<h2>Today's Setup · {today_human}</h2>
{today_outlook_block}

<div class="cols">
  <div class="panel">
    <div class="panel-head"><h3>Earnings Today</h3><div class="sub">Reporting before/after open</div></div>
    {earnings_table}
  </div>
  <div class="panel">
    <div class="panel-head"><h3>Economic Events</h3><div class="sub">Data releases & Fed speakers</div></div>
    {econ_table}
  </div>
</div>

{tickers_to_watch_block}

{crypto_outlook_block}

{risk_block}

<footer>
  Data: Yahoo Finance (via yfinance), CoinGecko, Nasdaq Calendar · Analysis: Claude
  <br/>Not investment advice. Figures may be delayed. Always verify before trading.
</footer>

</div>
<script>
// If opened as a local file, go to the live hosted version instead
if (window.location.protocol === 'file:') {{
  window.location.replace('https://jackjensen0614.github.io/daily-market-report/');
}}
function doRefresh() {{
  var btn = document.getElementById('refresh-btn');
  if (btn) {{ btn.classList.add('spinning'); btn.disabled = true; }}
  location.reload();
}}
(function(){{
  function isMarketHours() {{
    var now = new Date();
    var et = new Date(now.toLocaleString('en-US', {{timeZone:'America/New_York'}}));
    var day = et.getDay();
    if (day === 0 || day === 6) return false;
    var h = et.getHours(), m = et.getMinutes();
    var mins = h * 60 + m;
    return mins >= 565 && mins <= 970; // 9:25–4:10 ET
  }}
  var statusEl = document.getElementById('refresh-status');
  var labelEl  = document.getElementById('refresh-label');
  if (isMarketHours()) {{
    var next = 60;
    function setStatus(s) {{ if (statusEl) statusEl.textContent = s; }}
    function setLabel(s)  {{ if (labelEl)  labelEl.textContent  = s; }}
    setStatus('Live · auto-refresh in ' + next + 's');
    var timer = setInterval(function() {{
      next--;
      setStatus('Live · auto-refresh in ' + next + 's');
      setLabel('Refresh (' + next + 's)');
      if (next <= 0) {{ clearInterval(timer); location.reload(); }}
    }}, 1000);
  }} else {{
    if (statusEl) statusEl.textContent = 'Market closed';
  }}
}})();
</script>
</body>
</html>
"""


def fmt_pct(x: float) -> str:
    sign = "+" if x > 0 else ""
    return f"{sign}{x:.2f}%"


def fmt_usd(x: float) -> str:
    if x is None:
        return "—"
    if abs(x) >= 1e12:
        return f"${x / 1e12:.2f}T"
    if abs(x) >= 1e9:
        return f"${x / 1e9:.2f}B"
    if abs(x) >= 1e6:
        return f"${x / 1e6:.2f}M"
    if abs(x) >= 1000:
        return f"${x:,.2f}"
    if abs(x) >= 1:
        return f"${x:,.2f}"
    return f"${x:.4f}"


def fmt_num(x: float) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}"


def cls_for(pct: float) -> str:
    if pct > 0.01: return "up"
    if pct < -0.01: return "down"
    return "flat"


def render_index_tile(q: Quote) -> str:
    cls = cls_for(q.change_pct)
    price = fmt_num(q.price)
    delta = f"{'+' if q.change >= 0 else ''}{q.change:,.2f} ({fmt_pct(q.change_pct)})"
    return f"""
    <div class="tile">
      <div class="label">{q.name}</div>
      <div class="value num">{price}</div>
      <div class="delta num {cls}">{delta}</div>
    </div>
    """


def escape_html(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def render_mover_row(m: MoverWithNews, ai_why: dict[str, str] | None = None) -> str:
    q = m.quote
    cls = cls_for(q.change_pct)
    why = ""
    if ai_why and q.symbol in ai_why:
        why = f'<div class="why">{escape_html(ai_why[q.symbol])}</div>'

    news_html = ""
    if m.news:
        items = []
        for n in m.news[:NEWS_PER_TICKER]:
            title = escape_html(n.title or "(untitled)")
            pub = escape_html(n.publisher or "")
            link = n.link or "#"
            pub_html = f' <span class="pub">· {pub}</span>' if pub else ""
            items.append(f'<a href="{escape_html(link)}" target="_blank" rel="noopener">{title}{pub_html}</a>')
        news_html = '<div class="news">' + "".join(items) + '</div>'

    return f"""
    <div class="mover">
      <div>
        <div class="sym">{escape_html(q.symbol)}</div>
      </div>
      <div>
        <div class="name">{escape_html(q.name)}</div>
        {why}
        {news_html}
      </div>
      <div class="right">
        <div class="pct num {cls}">{fmt_pct(q.change_pct)}</div>
        <div class="px num">{fmt_usd(q.price)}</div>
      </div>
    </div>
    """


def render_movers_block(movers: list[MoverWithNews], ai_why: dict[str, str] | None, empty_msg: str) -> str:
    if not movers:
        return f'<div style="padding: 16px; color: var(--text-faint);">{empty_msg}</div>'
    return "".join(render_mover_row(m, ai_why) for m in movers)


def render_calendar_table(events: list[CalendarEvent], empty_msg: str) -> str:
    if not events:
        return f'<div style="padding: 16px; color: var(--text-faint);">{empty_msg}</div>'
    rows = []
    for e in events:
        rows.append(f"""
        <tr>
          <td class="time">{escape_html(e.time)}</td>
          <td class="sym">{escape_html(e.symbol_or_event)}</td>
          <td>{escape_html(e.description)}</td>
          <td style="color: var(--text-dim)">{escape_html(e.extra)}</td>
        </tr>
        """)
    return f"""
    <table>
      <thead><tr><th>Time</th><th>Symbol / Region</th><th>Event</th><th>Details</th></tr></thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def render_narrative(ai: dict) -> str:
    # When AI is disabled, skipped, or errored, render NOTHING. The headline
    # data already tells the story; we don't want to pollute the report with
    # billing/error banners.
    if not ai or "_skipped" in ai or "_error" in ai or "_raw" in ai:
        return ""
    text = ai.get("market_narrative", "")
    if not text:
        return ""
    return f"""
    <div class="narr">
      <div class="label">Market Narrative · Yesterday</div>
      <p>{escape_html(text)}</p>
    </div>
    """


def render_today_outlook(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    text = ai.get("today_outlook", "")
    if not text:
        return ""
    return f"""
    <div class="narr">
      <div class="label">Today's Outlook</div>
      <p>{escape_html(text)}</p>
    </div>
    """


def render_crypto_outlook(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    text = ai.get("crypto_outlook", "")
    if not text:
        return ""
    return f"""
    <div class="narr">
      <div class="label">Crypto Outlook</div>
      <p>{escape_html(text)}</p>
    </div>
    """


def render_risk_block(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    text = ai.get("risk_notes", "")
    if not text:
        return ""
    return f"""
    <div class="narr risk">
      <div class="label">Risk Notes</div>
      <p>{escape_html(text)}</p>
    </div>
    """


def render_tickers_to_watch(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    watch = ai.get("tickers_to_watch") or []
    if not watch:
        return ""
    items = []
    for w in watch:
        sym = escape_html(str(w.get("ticker", "")))
        why = escape_html(str(w.get("rationale", "")))
        items.append(f'<div class="watch-item"><div class="sym">{sym}</div><div class="why">{why}</div></div>')
    return f"""
    <h2>Tickers to Watch Today</h2>
    <div class="watch-list">
      {''.join(items)}
    </div>
    """


def _paras(text: str) -> str:
    return "".join(
        f"<p>{escape_html(p.strip())}</p>"
        for p in text.split("\n\n") if p.strip()
    )


# ---- data-driven briefing helpers ----------------------------------------

def _pct_span(pct: float) -> str:
    cls = "up" if pct > 0.01 else ("down" if pct < -0.01 else "flat")
    sign = "+" if pct > 0 else ""
    return f'<span class="{cls} num">{sign}{pct:.2f}%</span>'


def _b_exec_summary(snap: Snapshot) -> list[str]:
    bullets: list[str] = []
    idx = {q.symbol: q for q in snap.indices}
    sp, dji, ixic, vix = idx.get("^GSPC"), idx.get("^DJI"), idx.get("^IXIC"), idx.get("^VIX")
    parts = []
    for q, label in [(sp, "S&P 500"), (dji, "Dow"), (ixic, "Nasdaq")]:
        if q:
            arrow = "▲" if q.change_pct > 0 else ("▼" if q.change_pct < 0 else "—")
            parts.append(f"{label} {arrow}{abs(q.change_pct):.2f}%")
    if vix:
        parts.append(f"VIX {'+' if vix.change_pct >= 0 else ''}{vix.change_pct:.1f}% to {vix.price:.2f}")
    if parts:
        bullets.append(" · ".join(parts))

    if snap.gainers and snap.losers:
        g, l = snap.gainers[0].quote, snap.losers[0].quote
        bullets.append(
            f"Top gainer: {g.symbol} +{g.change_pct:.1f}% to {fmt_usd(g.price)} · "
            f"Top loser: {l.symbol} {l.change_pct:.1f}% to {fmt_usd(l.price)}"
        )

    crude = next((q for q in snap.macro if "Crude" in q.name), None)
    gold  = next((q for q in snap.macro if "Gold"  in q.name), None)
    tnx   = next((q for q in snap.macro if "10Y"   in q.name), None)
    macro_parts: list[str] = []
    if crude: macro_parts.append(f"WTI {fmt_pct(crude.change_pct)} to {fmt_usd(crude.price)}")
    if gold:  macro_parts.append(f"Gold {fmt_pct(gold.change_pct)} to {fmt_usd(gold.price)}")
    if tnx:   macro_parts.append(f"10Y yield {tnx.price:.2f}%")
    if macro_parts:
        bullets.append(" · ".join(macro_parts))

    btc = next((m.quote for m in snap.crypto if m.quote.symbol.upper() == "BTC"), None)
    eth = next((m.quote for m in snap.crypto if m.quote.symbol.upper() == "ETH"), None)
    cparts: list[str] = []
    if btc: cparts.append(f"BTC {fmt_pct(btc.change_pct)} to {fmt_usd(btc.price)}")
    if eth: cparts.append(f"ETH {fmt_pct(eth.change_pct)} to {fmt_usd(eth.price)}")
    if snap.crypto_gainers:
        cg = snap.crypto_gainers[0].quote
        cparts.append(f"Top crypto: {cg.symbol} +{cg.change_pct:.1f}%")
    if cparts:
        bullets.append(" · ".join(cparts))

    if snap.earnings_today:
        tks = [e.symbol_or_event for e in snap.earnings_today[:6] if e.symbol_or_event]
        n = len(snap.earnings_today)
        suffix = f" +{n - 6} more" if n > 6 else ""
        bullets.append(f"Earnings today ({n}): {', '.join(tks)}{suffix}")
    elif snap.econ_events_today:
        evts = [e.description for e in snap.econ_events_today[:3] if e.description]
        bullets.append(f"Econ events today: {', '.join(evts)}")
    else:
        bullets.append("No major earnings or economic events scheduled today.")

    return bullets[:5]


def _b_us_markets(snap: Snapshot) -> str:
    idx_rows = "".join(
        f'<tr><td style="font-weight:600">{escape_html(q.name)}</td>'
        f'<td class="num" style="text-align:right">{fmt_num(q.price)}</td>'
        f'<td class="num" style="text-align:right">{("+" if q.change >= 0 else "")}{q.change:,.2f}</td>'
        f'<td class="num" style="text-align:right">{_pct_span(q.change_pct)}</td></tr>'
        for q in snap.indices
    )
    macro_rows = "".join(
        f'<tr><td style="color:#8a92a6">{escape_html(q.name)}</td>'
        f'<td class="num" style="text-align:right;color:#8a92a6">{fmt_num(q.price)}</td>'
        f'<td class="num" style="text-align:right;color:#8a92a6">{("+" if q.change >= 0 else "")}{q.change:,.2f}</td>'
        f'<td class="num" style="text-align:right">{_pct_span(q.change_pct)}</td></tr>'
        for q in snap.macro
    )
    idx_table = (
        '<table><thead><tr>'
        '<th style="text-align:left">Index / Macro</th>'
        '<th style="text-align:right">Price</th>'
        '<th style="text-align:right">Change</th>'
        '<th style="text-align:right">%</th>'
        f'</tr></thead><tbody>{idx_rows}{macro_rows}</tbody></table>'
    )

    def mover_rows(movers: list) -> str:
        out = ""
        for m in movers[:5]:
            q = m.quote
            headline = (m.news[0].title[:65] + "…") if m.news and m.news[0].title else ""
            out += (
                f'<tr><td style="font-weight:700">{escape_html(q.symbol)}</td>'
                f'<td style="color:#8a92a6;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px">{escape_html(q.name)}</td>'
                f'<td class="num" style="text-align:right">{_pct_span(q.change_pct)}</td>'
                f'<td class="num" style="text-align:right;color:#8a92a6">{fmt_usd(q.price)}</td></tr>'
            )
            if headline:
                out += f'<tr><td colspan="4" style="color:#6b7280;font-size:11px;padding-top:0;line-height:1.3">{escape_html(headline)}</td></tr>'
        return out

    movers_2col = (
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-top:12px">'
        '<div>'
        '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#22c55e;font-weight:600;margin-bottom:4px">Top Gainers</div>'
        f'<table><tbody>{mover_rows(snap.gainers)}</tbody></table>'
        '</div>'
        '<div>'
        '<div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:#ef4444;font-weight:600;margin-bottom:4px">Top Losers</div>'
        f'<table><tbody>{mover_rows(snap.losers)}</tbody></table>'
        '</div>'
        '</div>'
    )

    return (
        '<div class="briefing-section">'
        '<div class="bs-label">US Markets · Yesterday\'s Session</div>'
        f'{idx_table}{movers_2col}'
        '</div>'
    )


def _b_global_markets(snap: Snapshot) -> str:
    if not snap.global_indices:
        return ""
    rows = "".join(
        f'<tr><td>{escape_html(q.name)}</td>'
        f'<td class="num" style="text-align:right">{fmt_num(q.price)}</td>'
        f'<td class="num" style="text-align:right;color:#8a92a6">{("+" if q.change >= 0 else "")}{q.change:,.2f}</td>'
        f'<td class="num" style="text-align:right">{_pct_span(q.change_pct)}</td></tr>'
        for q in snap.global_indices
    )
    return (
        '<div class="briefing-section">'
        '<div class="bs-label">Global Markets</div>'
        '<table><thead><tr>'
        '<th style="text-align:left">Market</th>'
        '<th style="text-align:right">Price</th>'
        '<th style="text-align:right">Change</th>'
        '<th style="text-align:right">%</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        '</div>'
    )


def _b_crypto(snap: Snapshot) -> str:
    if not snap.crypto:
        return ""
    rows = "".join(
        f'<tr><td style="font-weight:700">{escape_html(m.quote.symbol)}</td>'
        f'<td style="color:#8a92a6;font-size:11px">{escape_html(m.quote.name)}</td>'
        f'<td class="num" style="text-align:right">{fmt_usd(m.quote.price)}</td>'
        f'<td class="num" style="text-align:right">{_pct_span(m.quote.change_pct)}</td>'
        f'<td class="num" style="text-align:right;color:#8a92a6">{fmt_usd(m.quote.dollar_volume) if m.quote.dollar_volume else "—"}</td></tr>'
        for m in snap.crypto[:10]
    )
    return (
        '<div class="briefing-section crypto">'
        '<div class="bs-label">Crypto Markets · Top 10 by Market Cap</div>'
        '<table><thead><tr>'
        '<th style="text-align:left">Symbol</th><th style="text-align:left">Name</th>'
        '<th style="text-align:right">Price</th><th style="text-align:right">24h %</th>'
        '<th style="text-align:right">Volume</th>'
        f'</tr></thead><tbody>{rows}</tbody></table>'
        '</div>'
    )


def _b_setup(snap: Snapshot) -> str:
    parts: list[str] = []
    if snap.earnings_today:
        rows = "".join(
            f'<tr><td class="time">{escape_html(e.time)}</td>'
            f'<td style="font-weight:700">{escape_html(e.symbol_or_event)}</td>'
            f'<td>{escape_html(e.description)}</td>'
            f'<td style="color:var(--text-dim)">{escape_html(e.extra)}</td></tr>'
            for e in snap.earnings_today[:20]
        )
        parts.append(
            '<div style="margin-bottom:14px">'
            '<div style="font-size:12px;font-weight:600;color:#60a5fa;margin-bottom:6px">Earnings Today</div>'
            '<table><thead><tr><th>Time</th><th>Ticker</th><th>Company</th><th>Details</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )
    else:
        parts.append('<p style="color:var(--text-faint)">No earnings reporting today.</p>')

    if snap.econ_events_today:
        rows = "".join(
            f'<tr><td class="time">{escape_html(e.time)}</td>'
            f'<td style="color:#8a92a6">{escape_html(e.symbol_or_event)}</td>'
            f'<td>{escape_html(e.description)}</td>'
            f'<td style="color:var(--text-dim)">{escape_html(e.extra)}</td></tr>'
            for e in snap.econ_events_today[:15]
        )
        parts.append(
            '<div>'
            '<div style="font-size:12px;font-weight:600;color:#60a5fa;margin-bottom:6px">Economic Events</div>'
            '<table><thead><tr><th>Time</th><th>Region</th><th>Event</th><th>Details</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )
    else:
        parts.append('<p style="color:var(--text-faint)">No major economic events today.</p>')

    return (
        '<div class="briefing-section setup">'
        '<div class="bs-label">Today\'s Setup — What to Watch</div>'
        + "".join(parts) +
        '</div>'
    )


def _b_risks(snap: Snapshot) -> str:
    risks: list[str] = []
    big_names = {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "JPM", "BAC", "NFLX"}
    big_earnings = [e for e in snap.earnings_today if e.symbol_or_event in big_names]
    if big_earnings:
        tickers = ", ".join(e.symbol_or_event for e in big_earnings[:5])
        risks.append(f"High-impact earnings today ({tickers}) — misses or cautious guidance can gap indices at open.")

    vix = next((q for q in snap.indices if q.symbol == "^VIX"), None)
    if vix and vix.price > 20:
        risks.append(f"VIX elevated at {vix.price:.2f} — options market pricing above-average volatility.")

    crude = next((q for q in snap.macro if "Crude" in q.name), None)
    if crude and abs(crude.change_pct) > 3:
        dir_ = "surge" if crude.change_pct > 0 else "drop"
        risks.append(f"WTI crude {dir_} {crude.change_pct:+.1f}% to {fmt_usd(crude.price)} — watch macro read-through to consumer and transport names.")

    tnx = next((q for q in snap.macro if "10Y" in q.name), None)
    if tnx and tnx.price > 4.5:
        risks.append(f"10Y yield at {tnx.price:.2f}% — elevated rates a headwind for growth and rate-sensitive equities.")

    fed_evts = [e for e in snap.econ_events_today if any(
        kw in (e.description or "").upper() for kw in ["FOMC", "FEDERAL RESERVE", "POWELL", "RATE DECISION"]
    )]
    if fed_evts:
        risks.append("FOMC/Fed event today — any surprise on rates or tone could trigger outsized moves across asset classes.")

    if snap.global_indices:
        weak = [q for q in snap.global_indices if q.change_pct < -1.5]
        if weak:
            names = ", ".join(q.name.split("(")[0].strip() for q in weak[:3])
            risks.append(f"Global market weakness ({names}) may weigh on pre-market sentiment.")

    if not risks:
        risks.append("No major elevated risk signals detected in today's data.")

    lis = "".join(f"<li>{escape_html(r)}</li>" for r in risks[:4])
    return (
        '<div class="briefing-section risk">'
        '<div class="bs-label">Risk Notes</div>'
        f'<ul>{lis}</ul>'
        '</div>'
    )


def _build_data_briefing(snap: Snapshot) -> str:
    """Build complete briefing modal content from snapshot data — no AI API needed."""
    exec_bullets = _b_exec_summary(snap)
    exec_html = ""
    if exec_bullets:
        lis = "".join(f"<li>{escape_html(b)}</li>" for b in exec_bullets)
        exec_html = (
            '<div class="exec-bar">'
            '<div class="exec-label">Market Summary</div>'
            f'<ol>{lis}</ol>'
            '</div>'
        )
    return exec_html + _b_us_markets(snap) + _b_global_markets(snap) + _b_crypto(snap) + _b_setup(snap) + _b_risks(snap)


# --------------------------------------------------------------------------

def render_briefing_block(briefing: dict | None, snap: Snapshot | None = None) -> str:
    """Render the Morning Briefing FAB + modal.

    Uses AI-generated JSON if available; otherwise builds from snapshot data.
    Always renders the modal as long as we have either source.
    """
    if briefing:
        # --- AI-generated briefing ---
        exec_bullets = briefing.get("exec_summary", [])
        exec_html = ""
        if exec_bullets:
            lis = "".join(f"<li>{escape_html(b)}</li>" for b in exec_bullets)
            exec_html = (
                '<div class="exec-bar">'
                '<div class="exec-label">Executive Summary</div>'
                f'<ol>{lis}</ol>'
                '</div>'
            )

        session_text = briefing.get("session_recap", "")
        session_html = (
            '<div class="briefing-section">'
            '<div class="bs-label">Yesterday\'s Session</div>'
            f'{_paras(session_text)}</div>'
        ) if session_text else ""

        crypto_recap = briefing.get("crypto_recap", "")
        crypto_recap_html = (
            '<div class="briefing-section crypto">'
            '<div class="bs-label">Crypto Recap</div>'
            f'{_paras(crypto_recap)}</div>'
        ) if crypto_recap else ""

        setup_text = briefing.get("today_setup", "")
        setup_html = (
            '<div class="briefing-section setup">'
            '<div class="bs-label">Today\'s Setup</div>'
            f'{_paras(setup_text)}</div>'
        ) if setup_text else ""

        watch = briefing.get("tickers_to_watch", [])
        watch_html = ""
        if watch:
            cards = "".join(
                f'<div class="b-watch-item">'
                f'<div class="sym">{escape_html(str(w.get("ticker", "")))}</div>'
                f'<div class="why">{escape_html(str(w.get("rationale", "")))}</div>'
                f'</div>'
                for w in watch
            )
            watch_html = (
                '<div class="briefing-watch">'
                '<div class="bs-label">Tickers to Watch Today</div>'
                f'<div class="b-watch-grid">{cards}</div>'
                '</div>'
            )

        crypto_out = briefing.get("crypto_outlook", "")
        crypto_out_html = (
            '<div class="briefing-section crypto">'
            '<div class="bs-label">Crypto Outlook</div>'
            f'{_paras(crypto_out)}</div>'
        ) if crypto_out else ""

        risk_items = briefing.get("risk_notes", [])
        risk_html = ""
        if risk_items:
            if isinstance(risk_items, list):
                lis = "".join(f"<li>{escape_html(r)}</li>" for r in risk_items)
                risk_html = (
                    '<div class="briefing-section risk">'
                    '<div class="bs-label">Risk Notes</div>'
                    f'<ul>{lis}</ul></div>'
                )
            else:
                risk_html = (
                    '<div class="briefing-section risk">'
                    '<div class="bs-label">Risk Notes</div>'
                    f'{_paras(str(risk_items))}</div>'
                )

        # Append global markets and live data tables from snap if available
        global_html = _b_global_markets(snap) if snap else ""
        inner = exec_html + session_html + crypto_recap_html + global_html + setup_html + watch_html + crypto_out_html + risk_html
        source = "Claude AI"

    elif snap:
        # --- Data-driven fallback (no AI API needed) ---
        inner = _build_data_briefing(snap)
        source = "Live Data"
    else:
        return ""

    if not inner.strip():
        return ""

    gen_date = datetime.now(ET).strftime("%B %-d, %Y · %I:%M %p %Z")
    return f"""
<button class="briefing-fab" id="briefing-fab">Morning Briefing</button>
<div class="briefing-backdrop" id="briefing-backdrop">
  <div class="briefing-modal">
    <div class="briefing-modal-head">
      <h3>Morning Briefing</h3>
      <span class="bdate">{source} · {gen_date}</span>
      <button class="briefing-close" id="briefing-close">&#x2715;</button>
    </div>
    {inner}
  </div>
</div>
<script>
(function(){{
  var fab=document.getElementById('briefing-fab');
  var bd=document.getElementById('briefing-backdrop');
  var cl=document.getElementById('briefing-close');
  if(!fab)return;
  function open(){{ bd.classList.add('open'); document.body.style.overflow='hidden'; }}
  function close(){{ bd.classList.remove('open'); document.body.style.overflow=''; }}
  fab.addEventListener('click',open);
  cl.addEventListener('click',close);
  bd.addEventListener('click',function(e){{ if(e.target===bd)close(); }});
  document.addEventListener('keydown',function(e){{ if(e.key==='Escape')close(); }});
}})();
</script>"""


def render_report(snap: Snapshot, briefing: dict | None = None) -> str:
    prior_date = snap.prior_session_date
    prior_dt = datetime.fromisoformat(snap.prior_session_date)
    today_dt = datetime.fromisoformat(snap.generated_at[:10])

    warnings_html = ""
    if snap.warnings:
        warnings_html = "".join(
            f'<div class="warn">{escape_html(w)}</div>' for w in snap.warnings
        )

    index_tiles = "".join(render_index_tile(q) for q in (snap.indices + snap.macro))
    if not index_tiles:
        index_tiles = '<div class="tile"><div class="label">No index data available</div></div>'

    ai = snap.ai or {}
    why_g = ai.get("why_gainers") or {}
    why_l = ai.get("why_losers") or {}
    why_a = ai.get("why_active") or {}
    why_c = ai.get("why_crypto") or {}

    # Prefer crypto_gainers/losers for the crypto panel if available; else full list
    crypto_list = snap.crypto

    html = HTML_TEMPLATE.format(
        prior_date=prior_date,
        prior_date_human=prior_dt.strftime("%A, %B %-d, %Y"),
        generated_human=datetime.fromisoformat(snap.generated_at).strftime("%Y-%m-%d %H:%M %Z"),
        today_human=today_dt.strftime("%A, %B %-d, %Y"),
        warnings_html=warnings_html,
        index_tiles=index_tiles,
        briefing_block=render_briefing_block(briefing, snap),
        ai_narrative_block=render_narrative(ai),
        gainers_rows=render_movers_block(snap.gainers, why_g, "No gainer data."),
        losers_rows=render_movers_block(snap.losers, why_l, "No loser data."),
        active_rows=render_movers_block(snap.most_active, why_a, "No active data."),
        crypto_rows=render_movers_block(crypto_list, why_c, "No crypto data."),
        crypto_top_n=CRYPTO_TOP_N,
        today_outlook_block=render_today_outlook(ai),
        earnings_table=render_calendar_table(snap.earnings_today, "No earnings reporting today."),
        econ_table=render_calendar_table(snap.econ_events_today, "No major economic events today."),
        tickers_to_watch_block=render_tickers_to_watch(ai),
        crypto_outlook_block=render_crypto_outlook(ai),
        risk_block=render_risk_block(ai),
    )
    return html


# ------------------------------------------------------------------------
# Main orchestration
# ------------------------------------------------------------------------
def build_snapshot(no_ai: bool = False) -> Snapshot:
    snap = Snapshot(
        prior_session_date=get_prior_trading_day(),
        generated_at=datetime.now(ET).isoformat(timespec="seconds"),
    )
    log(f"Prior trading session: {snap.prior_session_date}")

    log("Fetching index quotes…")
    try:
        snap.indices = fetch_quotes(INDEX_TICKERS)
    except Exception as e:
        warn(f"indices fetch failed: {e}", snap)

    log("Fetching macro quotes…")
    try:
        snap.macro = fetch_quotes(EXTRA_MACRO_TICKERS)
    except Exception as e:
        warn(f"macro fetch failed: {e}", snap)

    log("Fetching global indices…")
    try:
        snap.global_indices = fetch_quotes(GLOBAL_INDICES)
    except Exception as e:
        warn(f"global indices fetch failed: {e}", snap)

    log("Fetching gainers / losers / most active (screener)…")
    gainers_q = fetch_screener("day_gainers", count=MOVERS_COUNT)
    losers_q = fetch_screener("day_losers", count=MOVERS_COUNT)
    active_q = fetch_screener("most_actives", count=MOVERS_COUNT)

    # Yahoo's predefined screener is rate-limited and frequently returns empty.
    # Fall back to a local universe scan, which uses the much more reliable
    # bulk quote endpoint and computes movers client-side.
    if not gainers_q or not losers_q or not active_q:
        log("Screener returned partial/empty result — using S&P universe fallback.")
        g_fb, l_fb, a_fb = fetch_movers_from_universe(MOVERS_COUNT)
        if not gainers_q and g_fb:
            gainers_q = g_fb
        if not losers_q and l_fb:
            losers_q = l_fb
        if not active_q and a_fb:
            active_q = a_fb

    # If after both attempts we still have nothing, that's a real problem worth
    # surfacing — but not the routine screener-empty case.
    if not gainers_q and not losers_q and not active_q:
        warn(
            "Could not retrieve any market movers. "
            "Check your network connection or try again later.",
            snap,
        )

    log("Fetching news for movers…")
    snap.gainers = attach_news(gainers_q)
    snap.losers = attach_news(losers_q)
    snap.most_active = attach_news(active_q)

    log("Fetching crypto markets from CoinGecko…")
    crypto_q = fetch_crypto_markets(CRYPTO_TOP_N)
    if not crypto_q:
        warn("No crypto data — CoinGecko may be rate-limited.", snap)
    snap.crypto = attach_crypto_news(crypto_q)
    # sort for gainer/loser subsets
    sorted_c = sorted(crypto_q, key=lambda q: q.change_pct, reverse=True)
    snap.crypto_gainers = [MoverWithNews(quote=q) for q in sorted_c[:5]]
    snap.crypto_losers = [MoverWithNews(quote=q) for q in sorted_c[-5:][::-1]]

    today_iso = datetime.now(ET).date().isoformat()
    log(f"Fetching earnings calendar for {today_iso}…")
    snap.earnings_today = fetch_earnings_calendar(today_iso)
    log(f"Fetching economic events for {today_iso}…")
    snap.econ_events_today = fetch_econ_events(today_iso)

    if not no_ai:
        log("Running AI synthesis via Anthropic…")
        snap.ai = run_ai_synthesis(snap)
    else:
        snap.ai = {"_skipped": "--no-ai flag used; headlines only."}

    return snap


def save_cache(snap: Snapshot) -> None:
    try:
        # Dataclass → dict recursively
        def to_plain(o):
            if hasattr(o, "__dataclass_fields__"):
                return {k: to_plain(v) for k, v in asdict(o).items()}
            if isinstance(o, list):
                return [to_plain(x) for x in o]
            if isinstance(o, dict):
                return {k: to_plain(v) for k, v in o.items()}
            return o

        DATA_SNAPSHOT_PATH.write_text(json.dumps(to_plain(snap), indent=2, default=str))
    except Exception as e:
        log(f"Could not write cache: {e}")


def load_cache() -> Snapshot | None:
    if not DATA_SNAPSHOT_PATH.exists():
        return None
    try:
        raw = json.loads(DATA_SNAPSHOT_PATH.read_text())

        def q_from(d): return Quote(**d)
        def n_from(d): return NewsItem(**d)
        def mw_from(d):
            return MoverWithNews(
                quote=q_from(d["quote"]),
                news=[n_from(x) for x in d.get("news", [])],
                ai_why=d.get("ai_why", ""),
            )
        def ev_from(d): return CalendarEvent(**d)

        snap = Snapshot(
            prior_session_date=raw["prior_session_date"],
            generated_at=raw["generated_at"],
            indices=[q_from(x) for x in raw.get("indices", [])],
            macro=[q_from(x) for x in raw.get("macro", [])],
            global_indices=[q_from(x) for x in raw.get("global_indices", [])],
            gainers=[mw_from(x) for x in raw.get("gainers", [])],
            losers=[mw_from(x) for x in raw.get("losers", [])],
            most_active=[mw_from(x) for x in raw.get("most_active", [])],
            crypto=[mw_from(x) for x in raw.get("crypto", [])],
            crypto_gainers=[mw_from(x) for x in raw.get("crypto_gainers", [])],
            crypto_losers=[mw_from(x) for x in raw.get("crypto_losers", [])],
            earnings_today=[ev_from(x) for x in raw.get("earnings_today", [])],
            econ_events_today=[ev_from(x) for x in raw.get("econ_events_today", [])],
            ai=raw.get("ai", {}),
            warnings=raw.get("warnings", []),
        )
        return snap
    except Exception as e:
        log(f"Could not read cache: {e}")
        return None


def parse_args():
    p = argparse.ArgumentParser(description="Generate a local daily market & crypto report.")
    p.add_argument("--no-open", action="store_true", help="Do not open the browser.")
    p.add_argument("--no-ai", action="store_true", help="Skip Anthropic AI synthesis.")
    p.add_argument("--offline", action="store_true", help="Use last cached data; no network.")
    p.add_argument("--out", type=str, default=str(REPORT_PATH), help="Output HTML path.")
    p.add_argument(
        "--briefing-json", type=str, default=None, metavar="PATH",
        help="Path to a briefing JSON file to embed in the report.",
    )
    return p.parse_args()


def load_briefing_json(path: str | None, snap_date: str | None = None) -> dict | None:
    if not path and snap_date:
        # Auto-detect briefing-YYYY-MM-DD.json next to the script
        candidate = Path(__file__).parent / f"briefing-{snap_date}.json"
        if candidate.exists():
            path = str(candidate)
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Could not load briefing JSON ({path}): {e}")
        return None


def main():
    args = parse_args()

    if args.offline:
        log("Offline mode: using last cached snapshot.")
        snap = load_cache()
        if snap is None:
            print("No cached snapshot found. Run without --offline first.", file=sys.stderr)
            sys.exit(2)
    else:
        snap = build_snapshot(no_ai=args.no_ai)
        save_cache(snap)

    briefing = load_briefing_json(args.briefing_json, snap_date=snap.prior_session_date)

    if briefing is None and not args.no_ai:
        log("Generating morning briefing via Anthropic…")
        briefing = generate_briefing(snap)

    log("Rendering HTML…")
    html = render_report(snap, briefing=briefing)
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    log(f"Report written to {out}")

    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(out.as_uri())
        except Exception as e:
            log(f"Could not open browser automatically: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
