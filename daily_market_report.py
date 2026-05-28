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
import time
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

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print("ERROR: jinja2 is not installed. Run `./run.command` to install dependencies.", file=sys.stderr)
    sys.exit(1)

# ------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
REPORT_PATH = SCRIPT_DIR / "report.html"
TEMPLATE_DIR = SCRIPT_DIR / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
)
DATA_SNAPSHOT_PATH = CACHE_DIR / "last_snapshot.json"
SCORECARD_HISTORY_PATH = SCRIPT_DIR / "scorecard_history.json"

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

PREMARKET_US     = {"ES=F": "S&P Fut", "NQ=F": "Nasdaq Fut", "YM=F": "Dow Fut", "RTY=F": "Russell Fut"}
PREMARKET_MACRO  = {"DX-Y.NYB": "DXY", "^TNX": "10Y Yield", "GC=F": "Gold", "CL=F": "WTI"}
PREMARKET_CRYPTO = {"BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "SOL-USD": "Solana", "XRP-USD": "XRP"}
OVERNIGHT_GLOBAL = {
    "^N225": "Nikkei", "^HSI": "Hang Seng", "^KS11": "KOSPI",
    "^FTSE": "FTSE 100", "^GDAXI": "DAX", "^STOXX50E": "STOXX 50",
}

CRYPTO_TOP_N = 20  # top coins by market cap on CoinGecko
MOVERS_COUNT = 10  # gainers/losers/active per category
NEWS_PER_TICKER = 3

# Default sidebar watchlist — used when WATCHLIST env var is not set
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META", "JPM"]

# Ticker → company-name lookup used by the watchlist autocomplete. Covers the
# largest, most-searched US-listed names. Anything missing here still works
# (the user can type the raw ticker), this just powers name-based search.
POPULAR_TICKERS: dict[str, str] = {
    # Mega-cap tech
    "AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation", "NVDA": "NVIDIA Corporation",
    "GOOGL": "Alphabet (Google) Class A", "GOOG": "Alphabet (Google) Class C",
    "AMZN": "Amazon.com", "META": "Meta Platforms (Facebook)", "TSLA": "Tesla",
    "AVGO": "Broadcom", "ORCL": "Oracle", "ADBE": "Adobe", "CRM": "Salesforce",
    "NFLX": "Netflix", "CSCO": "Cisco Systems", "AMD": "Advanced Micro Devices",
    "INTC": "Intel", "QCOM": "Qualcomm", "TXN": "Texas Instruments",
    "INTU": "Intuit", "IBM": "IBM", "NOW": "ServiceNow", "PANW": "Palo Alto Networks",
    "CRWD": "CrowdStrike", "SNOW": "Snowflake", "PLTR": "Palantir Technologies",
    "ARM": "Arm Holdings", "MU": "Micron Technology", "AMAT": "Applied Materials",
    "LRCX": "Lam Research", "ASML": "ASML Holding", "MRVL": "Marvell Technology",
    "ADI": "Analog Devices", "KLAC": "KLA Corporation", "NXPI": "NXP Semiconductors",
    "ON": "ON Semiconductor", "WDC": "Western Digital", "STX": "Seagate",
    "DDOG": "Datadog", "NET": "Cloudflare", "ZS": "Zscaler", "FTNT": "Fortinet",
    "WDAY": "Workday", "TEAM": "Atlassian", "ZM": "Zoom Video", "DOCU": "DocuSign",
    "TWLO": "Twilio", "SHOP": "Shopify", "SQ": "Block (Square)", "PYPL": "PayPal",
    "CDNS": "Cadence Design Systems", "SNPS": "Synopsys", "ANSS": "ANSYS",
    "ADSK": "Autodesk", "ROKU": "Roku", "PINS": "Pinterest", "SNAP": "Snap (Snapchat)",
    "MTCH": "Match Group (Tinder)", "SPOT": "Spotify", "EBAY": "eBay",
    "ABNB": "Airbnb", "UBER": "Uber", "LYFT": "Lyft", "DASH": "DoorDash",
    "AFRM": "Affirm Holdings", "UPST": "Upstart", "RBLX": "Roblox",
    "DKNG": "DraftKings", "COIN": "Coinbase", "HOOD": "Robinhood Markets",
    "RDDT": "Reddit", "DJT": "Trump Media & Technology Group",
    "SMCI": "Super Micro Computer", "GFS": "GlobalFoundries",

    # Financials
    "BRK-B": "Berkshire Hathaway Class B", "BRK-A": "Berkshire Hathaway Class A",
    "JPM": "JPMorgan Chase", "BAC": "Bank of America", "WFC": "Wells Fargo",
    "C": "Citigroup", "GS": "Goldman Sachs", "MS": "Morgan Stanley",
    "BLK": "BlackRock", "SCHW": "Charles Schwab", "USB": "U.S. Bancorp",
    "TFC": "Truist Financial", "PNC": "PNC Financial Services", "AXP": "American Express",
    "V": "Visa", "MA": "Mastercard", "FI": "Fiserv", "FIS": "Fidelity National Information Services",
    "CB": "Chubb", "MMC": "Marsh & McLennan", "AIG": "American International Group",
    "MET": "MetLife", "PRU": "Prudential Financial", "TRV": "Travelers Companies",
    "ALL": "Allstate", "AFL": "Aflac", "SPGI": "S&P Global", "MCO": "Moody's",
    "ICE": "Intercontinental Exchange", "CME": "CME Group", "NDAQ": "Nasdaq Inc.",

    # Healthcare / Pharma
    "JNJ": "Johnson & Johnson", "LLY": "Eli Lilly", "ABBV": "AbbVie",
    "MRK": "Merck", "PFE": "Pfizer", "TMO": "Thermo Fisher Scientific",
    "ABT": "Abbott Laboratories", "DHR": "Danaher", "BMY": "Bristol-Myers Squibb",
    "AMGN": "Amgen", "GILD": "Gilead Sciences", "REGN": "Regeneron",
    "VRTX": "Vertex Pharmaceuticals", "MDT": "Medtronic", "ISRG": "Intuitive Surgical",
    "BSX": "Boston Scientific", "ELV": "Elevance Health", "CVS": "CVS Health",
    "UNH": "UnitedHealth Group", "ZTS": "Zoetis", "SYK": "Stryker", "BIIB": "Biogen",
    "GEHC": "GE HealthCare", "DXCM": "DexCom", "MRNA": "Moderna",
    "NVO": "Novo Nordisk", "AZN": "AstraZeneca",

    # Consumer
    "WMT": "Walmart", "COST": "Costco", "HD": "Home Depot", "LOW": "Lowe's",
    "TGT": "Target", "TJX": "TJX Companies (TJ Maxx)", "ROST": "Ross Stores",
    "DLTR": "Dollar Tree", "DG": "Dollar General", "BBY": "Best Buy",
    "MCD": "McDonald's", "SBUX": "Starbucks", "CMG": "Chipotle Mexican Grill",
    "YUM": "Yum! Brands (KFC/Taco Bell)", "BKNG": "Booking Holdings",
    "MAR": "Marriott International", "HLT": "Hilton Worldwide",
    "NKE": "Nike", "LULU": "Lululemon Athletica", "F": "Ford Motor", "GM": "General Motors",
    "RIVN": "Rivian Automotive", "LCID": "Lucid Group", "NIO": "NIO Inc.",
    "PG": "Procter & Gamble", "KO": "Coca-Cola", "PEP": "PepsiCo",
    "MDLZ": "Mondelez International (Oreo/Cadbury)", "MNST": "Monster Beverage",
    "KHC": "Kraft Heinz", "KDP": "Keurig Dr Pepper", "PM": "Philip Morris",
    "MO": "Altria (Marlboro)", "STZ": "Constellation Brands", "TLRY": "Tilray Brands",
    "DIS": "Walt Disney Company", "CMCSA": "Comcast", "T": "AT&T",
    "VZ": "Verizon Communications", "TMUS": "T-Mobile US", "CHTR": "Charter Communications",
    "WBD": "Warner Bros. Discovery", "PARA": "Paramount Global", "SIRI": "Sirius XM",
    "AMC": "AMC Entertainment", "GME": "GameStop", "BB": "BlackBerry",
    "CHWY": "Chewy", "CART": "Instacart (Maplebear)", "ETSY": "Etsy",
    "PDD": "PDD Holdings (Temu/Pinduoduo)", "MELI": "MercadoLibre",
    "BABA": "Alibaba Group", "JD": "JD.com",

    # Industrials / Energy / Materials
    "BA": "Boeing", "CAT": "Caterpillar", "DE": "Deere & Company",
    "GE": "General Electric", "HON": "Honeywell", "RTX": "RTX (Raytheon)",
    "LMT": "Lockheed Martin", "NOC": "Northrop Grumman", "GD": "General Dynamics",
    "MMM": "3M", "ETN": "Eaton", "EMR": "Emerson Electric", "ITW": "Illinois Tool Works",
    "UPS": "United Parcel Service", "FDX": "FedEx", "CSX": "CSX", "UNP": "Union Pacific",
    "NSC": "Norfolk Southern", "DAL": "Delta Air Lines", "AAL": "American Airlines",
    "UAL": "United Airlines", "LUV": "Southwest Airlines", "WM": "Waste Management",
    "XOM": "ExxonMobil", "CVX": "Chevron", "COP": "ConocoPhillips",
    "EOG": "EOG Resources", "PSX": "Phillips 66", "VLO": "Valero Energy",
    "MPC": "Marathon Petroleum", "OXY": "Occidental Petroleum", "SLB": "Schlumberger",
    "BKR": "Baker Hughes", "HAL": "Halliburton", "FCX": "Freeport-McMoRan",
    "NEM": "Newmont Mining", "DOW": "Dow Inc.", "DD": "DuPont", "PPG": "PPG Industries",
    "SHW": "Sherwin-Williams", "LIN": "Linde", "APD": "Air Products",

    # Real Estate / Utilities
    "PLD": "Prologis", "AMT": "American Tower", "CCI": "Crown Castle",
    "EQIX": "Equinix", "PSA": "Public Storage", "SPG": "Simon Property Group",
    "O": "Realty Income", "WELL": "Welltower", "AVB": "AvalonBay Communities",
    "NEE": "NextEra Energy", "DUK": "Duke Energy", "SO": "Southern Company",
    "AEP": "American Electric Power", "EXC": "Exelon", "XEL": "Xcel Energy",
    "ED": "Consolidated Edison", "PEG": "Public Service Enterprise Group",

    # Crypto-equity / miners / fintech
    "MARA": "Marathon Digital Holdings", "RIOT": "Riot Platforms",
    "MSTR": "MicroStrategy", "WULF": "TeraWulf", "CLSK": "CleanSpark",
    "BITO": "ProShares Bitcoin Strategy ETF", "GBTC": "Grayscale Bitcoin Trust",
    "IBIT": "iShares Bitcoin Trust ETF", "FBTC": "Fidelity Wise Origin Bitcoin Fund",

    # Major ETFs
    "SPY": "SPDR S&P 500 ETF", "QQQ": "Invesco Nasdaq 100 ETF",
    "IWM": "iShares Russell 2000 ETF", "DIA": "SPDR Dow Jones ETF",
    "VOO": "Vanguard S&P 500 ETF", "VTI": "Vanguard Total Stock Market ETF",
    "EEM": "iShares MSCI Emerging Markets ETF", "EFA": "iShares MSCI EAFE ETF",
    "GLD": "SPDR Gold Trust", "SLV": "iShares Silver Trust",
    "USO": "United States Oil Fund", "UNG": "United States Natural Gas Fund",
    "TLT": "iShares 20+ Year Treasury Bond ETF", "HYG": "iShares High Yield Bond ETF",
    "ARKK": "ARK Innovation ETF", "TQQQ": "ProShares UltraPro QQQ (3x)",
    "SQQQ": "ProShares UltraPro Short QQQ (-3x)", "SOXL": "Direxion Daily Semiconductor Bull 3x",
    "TNA": "Direxion Daily Small Cap Bull 3x",
    "XLF": "Financials Select Sector SPDR", "XLE": "Energy Select Sector SPDR",
    "XLK": "Technology Select Sector SPDR", "XLV": "Health Care Select Sector SPDR",
    "XLY": "Consumer Discretionary Select Sector SPDR",
    "XLP": "Consumer Staples Select Sector SPDR",
    "XLI": "Industrials Select Sector SPDR", "XLB": "Materials Select Sector SPDR",
    "XLRE": "Real Estate Select Sector SPDR", "XLU": "Utilities Select Sector SPDR",
    "XLC": "Communication Services Select Sector SPDR",
    "VXX": "iPath Series B S&P 500 VIX Short-Term Futures ETN",

    # Other commonly searched
    "T": "AT&T", "VOO": "Vanguard S&P 500", "VUG": "Vanguard Growth ETF",
    "VTV": "Vanguard Value ETF", "SCHD": "Schwab US Dividend Equity ETF",
    "JEPI": "JPMorgan Equity Premium Income ETF",
    "ENB": "Enbridge", "BAM": "Brookfield Asset Management", "SU": "Suncor Energy",
    "TD": "Toronto-Dominion Bank", "RY": "Royal Bank of Canada",
    "TSM": "Taiwan Semiconductor Manufacturing", "SAP": "SAP SE",
    "NVS": "Novartis", "RHHBY": "Roche Holding",
}

# Macro-proxy tickers used to harvest broad economic news headlines
WORLD_NEWS_TICKERS = [
    "^GSPC", "^TNX", "GLD", "USO", "TLT", "^VIX",
    "DX-Y.NYB", "EEM", "FXI", "EFA", "SPY", "QQQ",
]

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
class SectorPerf:
    """1D / 1W / YTD performance for a single sector ETF."""
    symbol: str
    name: str
    pct_1d: float
    pct_1w: float
    pct_ytd: float


@dataclass
class ScorecardEntry:
    """Result of grading one predicted ticker against today's tape."""
    ticker: str
    rationale: str
    bias: str           # "bullish", "bearish", "neutral"
    actual_pct: float | None
    verdict: str        # "HIT", "MISS", "FLAT", "N/A" (legacy 3-tier)
    letter_grade: str = "—"   # "A", "B", "C", "D", "F", or "—"
    grade_reason: str = ""    # legacy single-register reason (kept for back-compat)
    grade_reason_standard: str = ""   # plain-English explanation
    grade_reason_advanced: str = ""   # technical / thesis-language explanation


@dataclass
class CalendarEvent:
    time: str
    symbol_or_event: str
    description: str
    extra: str = ""        # e.g. EPS estimate, prior value
    url: str = ""          # company website (earnings only)
    market_cap: float = 0.0  # numeric market cap for sorting (earnings only)


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
    world_news_raw: list[dict] = field(default_factory=list)
    premarket_us: list[Quote] = field(default_factory=list)
    premarket_macro: list[Quote] = field(default_factory=list)
    premarket_crypto: list[Quote] = field(default_factory=list)
    overnight_global: list[Quote] = field(default_factory=list)
    premarket_fetched_at: str = ""
    sectors: list[SectorPerf] = field(default_factory=list)
    scorecard: list[ScorecardEntry] = field(default_factory=list)
    sentiment: dict = field(default_factory=dict)
    watchlist: list[Quote] = field(default_factory=list)
    watchlist_news: list[MoverWithNews] = field(default_factory=list)
    earnings_reactions: list[MoverWithNews] = field(default_factory=list)
    earnings_results: dict = field(default_factory=dict)   # sym → {eps_est, eps_act, surprise_pct, verdict}


# ------------------------------------------------------------------------
# Logging helpers
# ------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------
# All-listed-tickers DB (NASDAQ + NYSE/AMEX/etc.)
# ------------------------------------------------------------------------
_TICKERS_CACHE_PATH = Path(__file__).parent / ".cache" / "all_tickers.json"
_TICKERS_CACHE_TTL_DAYS = 7

# Major cryptocurrencies seeded so autocomplete works even when the Yahoo
# remote-search endpoint is blocked by CORS. Symbol stored in Yahoo's
# "<TICKER>-USD" form so the existing watchlist pipeline accepts them.
CRYPTO_TICKERS: dict[str, str] = {
    "BTC-USD": "Bitcoin", "ETH-USD": "Ethereum", "USDT-USD": "Tether",
    "BNB-USD": "BNB", "XRP-USD": "XRP", "USDC-USD": "USD Coin",
    "SOL-USD": "Solana", "DOGE-USD": "Dogecoin", "ADA-USD": "Cardano",
    "TRX-USD": "TRON", "AVAX-USD": "Avalanche", "LINK-USD": "Chainlink",
    "DOT-USD": "Polkadot", "MATIC-USD": "Polygon", "LTC-USD": "Litecoin",
    "BCH-USD": "Bitcoin Cash", "SHIB-USD": "Shiba Inu", "UNI-USD": "Uniswap",
    "ATOM-USD": "Cosmos", "XLM-USD": "Stellar", "ETC-USD": "Ethereum Classic",
    "NEAR-USD": "NEAR Protocol", "APT-USD": "Aptos", "ARB-USD": "Arbitrum",
    "OP-USD": "Optimism", "FIL-USD": "Filecoin", "ICP-USD": "Internet Computer",
    "HBAR-USD": "Hedera", "VET-USD": "VeChain", "ALGO-USD": "Algorand",
    "AAVE-USD": "Aave", "MKR-USD": "Maker", "GRT-USD": "The Graph",
    "SAND-USD": "The Sandbox", "MANA-USD": "Decentraland", "AXS-USD": "Axie Infinity",
    "PEPE-USD": "Pepe", "WIF-USD": "dogwifhat", "BONK-USD": "Bonk",
    "INJ-USD": "Injective", "SUI-USD": "Sui", "SEI-USD": "Sei",
    "TIA-USD": "Celestia", "HYPE-USD": "Hyperliquid",
}

_ETF_NAME_HINTS = (
    "etf", "etn", "trust", "fund", "ishares", "spdr", "vanguard",
    "proshares", "invesco", "wisdomtree", "first trust", "schwab",
    "index fund", "portfolio", "treasury", "bond",
)

def _classify_ticker(symbol: str, name: str) -> str:
    """Best-effort asset-type tag for a (symbol, name) pair: Stock / ETF / Crypto / Index."""
    if not symbol:
        return "Stock"
    s = symbol.upper()
    if s.endswith("-USD") or s.endswith("USD=X"):
        return "Crypto"
    if s.startswith("^"):
        return "Index"
    nl = (name or "").lower()
    if any(h in nl for h in _ETF_NAME_HINTS):
        return "ETF"
    return "Stock"

def _clean_ticker_name(raw: str) -> str:
    """Strip the boilerplate suffixes Nasdaq Trader appends to security names."""
    if not raw:
        return raw
    n = raw.strip()
    # Strip boilerplate
    suffixes = [
        " - Common Stock", " Common Stock", " - Ordinary Shares", " Ordinary Shares",
        " - Class A Common Stock", " - Class B Common Stock", " - Class C Common Stock",
        " - Common Shares", " Common Shares", " - American Depositary Shares",
        " American Depositary Shares", " - Depositary Shares", " - Warrants", " Warrants",
        " - Rights", " Rights", " - Units", " Units",
    ]
    changed = True
    while changed:
        changed = False
        for s in suffixes:
            if n.lower().endswith(s.lower()):
                n = n[: -len(s)].rstrip(" ,;-")
                changed = True
    return n


def load_all_tickers() -> list[tuple[str, str]]:
    """Return [(symbol, name), ...] for every NASDAQ + NYSE/AMEX-listed security.

    Cached locally (refreshes weekly) so this is free at build time.
    Falls back to an empty list if the network is unreachable on first build.
    """
    # Try cache first
    try:
        if _TICKERS_CACHE_PATH.exists():
            age_days = (time.time() - _TICKERS_CACHE_PATH.stat().st_mtime) / 86400
            if age_days < _TICKERS_CACHE_TTL_DAYS:
                data = json.loads(_TICKERS_CACHE_PATH.read_text())
                return [(s, n) for s, n in data]
    except Exception as e:
        log(f"all-tickers cache read failed: {e}")

    # Fetch fresh
    try:
        urls = [
            "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
            "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
        ]
        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        for url in urls:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.raise_for_status()
            text = r.text
            lines = text.splitlines()
            if not lines:
                continue
            header = lines[0].split("|")
            sym_idx  = next((i for i, h in enumerate(header) if h.strip() in ("Symbol", "ACT Symbol")), 0)
            name_idx = next((i for i, h in enumerate(header) if h.strip() == "Security Name"), 1)
            test_idx = next((i for i, h in enumerate(header) if h.strip() == "Test Issue"), -1)
            for line in lines[1:]:
                if not line or line.startswith("File Creation Time"):
                    continue
                parts = line.split("|")
                if len(parts) <= max(sym_idx, name_idx):
                    continue
                sym = parts[sym_idx].strip()
                if not sym or "$" in sym or "." in sym and len(sym) > 6:
                    continue
                if test_idx >= 0 and len(parts) > test_idx and parts[test_idx].strip().upper() == "Y":
                    continue
                name = _clean_ticker_name(parts[name_idx])
                if not name or sym in seen:
                    continue
                seen.add(sym)
                pairs.append((sym, name))
        # Save to cache
        try:
            _TICKERS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _TICKERS_CACHE_PATH.write_text(json.dumps([[s, n] for s, n in pairs], ensure_ascii=False))
            log(f"all-tickers cache refreshed: {len(pairs)} symbols")
        except Exception as e:
            log(f"all-tickers cache write failed: {e}")
        return pairs
    except Exception as e:
        log(f"all-tickers fetch failed (search will use curated list only): {e}")
        # Try stale cache as last resort
        try:
            if _TICKERS_CACHE_PATH.exists():
                data = json.loads(_TICKERS_CACHE_PATH.read_text())
                return [(s, n) for s, n in data]
        except Exception:
            pass
        return []


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


def fetch_world_news(limit_per_ticker: int = 5) -> list[dict]:
    """Harvest recent macro/economic news headlines from market-proxy tickers."""
    seen: set[str] = set()
    items: list[dict] = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_ticker_news, t, limit_per_ticker): t
                   for t in WORLD_NEWS_TICKERS}
        for fut in as_completed(futures):
            try:
                for n in fut.result():
                    if not n.title:
                        continue
                    key = n.title.lower()[:80]
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append({
                        "headline": n.title,
                        "source": n.publisher,
                        "url": n.link,
                        "published": n.published,
                        "impact_summary": "",
                        "affected_tickers": [],
                        "affected_markets": [],
                        "direction": "mixed",
                    })
            except Exception as e:
                log(f"  world news fetch failed: {e}")

    items.sort(key=lambda x: x.get("published", ""), reverse=True)
    return items[:35]


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
            mcap_compact = fmt_mcap_compact(mcap)
            if mcap_compact:
                extra_bits.append(f"Mkt cap {mcap_compact}")
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

    def _get_info(sym: str) -> tuple[str, float]:
        try:
            info = yf.Ticker(sym).info
            website = info.get("website", "") or ""
            mcap = float(info.get("marketCap", 0) or 0)
            return website, mcap
        except Exception:
            return "", 0.0

    syms = [e.symbol_or_event for e in out if e.symbol_or_event]
    if syms:
        with ThreadPoolExecutor(max_workers=8) as pool:
            info_map = dict(zip(syms, pool.map(_get_info, syms)))
        for e in out:
            e.url, e.market_cap = info_map.get(e.symbol_or_event, ("", 0.0))

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


# Rationale used when the model omits a per-ticker explanation. Kept generic
# on purpose so it reads honestly when nothing else can be said.
_DEFAULT_RATIONALE = "Moved with broader sector flow; no single-stock catalyst."

# Number of crypto rows the AI is asked to explain. Capped so we don't burn
# tokens on stablecoins or quiet tickers; the remaining rows render without a
# rationale (the template hides empty .why divs cleanly).
_CRYPTO_AI_RATIONALE_TOP_N = 10


def _top_crypto_movers(snap: Snapshot, n: int = _CRYPTO_AI_RATIONALE_TOP_N) -> list[MoverWithNews]:
    """Return the n crypto rows with the largest absolute 24h price change.

    Independent of snap.crypto's display order (which has BTC/ETH pinned).
    """
    return sorted(
        snap.crypto or [],
        key=lambda m: abs(m.quote.change_pct),
        reverse=True,
    )[:n]


def _apply_crypto_display_order(snap: Snapshot) -> None:
    """Reorder snap.crypto for the main crypto panel.

    BTC and ETH come first when present; the rest are sorted by abs(24h %
    change) desc so the most interesting moves surface near the top and
    stablecoins fall to the bottom. Called right before render so it covers
    both fresh-fetch and --offline cached-snapshot paths.
    """
    if not snap.crypto:
        return
    pinned_syms = ("BTC", "ETH")
    pinned: list[MoverWithNews] = []
    for sym in pinned_syms:
        match = next((m for m in snap.crypto if m.quote.symbol.upper() == sym), None)
        if match is not None:
            pinned.append(match)
    others = [m for m in snap.crypto if m not in pinned]
    others.sort(key=lambda m: abs(m.quote.change_pct), reverse=True)
    snap.crypto = pinned + others


def _log_missing_rationales(snap: Snapshot, briefing: dict | None) -> None:
    """Walk rendered tickers and log any with missing AI rationale.

    Acts as a safety net AFTER Layer 1 fallback runs — if a ticker still has
    no rationale at render time, that's a regression worth surfacing in the
    console. Silent when everything is filled in.
    """
    ai = snap.ai or {}
    if "_skipped" in ai or "_error" in ai:
        return  # AI unavailable; nothing to validate.
    sources = [
        ("gainers",     snap.gainers,            ai.get("why_gainers") or {}),
        ("losers",      snap.losers,             ai.get("why_losers")  or {}),
        ("most_active", snap.most_active,        ai.get("why_active")  or {}),
        ("crypto",      _top_crypto_movers(snap),ai.get("why_crypto")  or {}),
    ]
    missing: list[str] = []
    for label, items, bucket in sources:
        for m in items or []:
            sym = m.quote.symbol
            if not (bucket.get(sym) or "").strip():
                missing.append(f"{label}:{sym}")

    if briefing:
        watch = briefing.get("watch") if isinstance(briefing, dict) else None
        for w in watch or []:
            if not isinstance(w, dict):
                continue
            ticker = str(w.get("ticker", "")).strip()
            r_adv   = str(w.get("rationale", "")).strip()
            r_plain = str(w.get("rationale_plain", "")).strip()
            if ticker and not r_adv and not r_plain:
                missing.append(f"briefing-watch:{ticker}")

    if missing:
        log(f"Missing AI rationale for {len(missing)} entry(ies): "
            f"{', '.join(missing)}")


def _fill_missing_briefing_rationales(briefing: dict | None) -> None:
    """Ensure every briefing 'watch' item carries a non-empty rationale.

    Mutates briefing['watch'] in place. Logs each fallback. No-op if briefing
    is None or has no watch list.
    """
    if not briefing:
        return
    watch = briefing.get("watch")
    if not isinstance(watch, list):
        return
    for w in watch:
        if not isinstance(w, dict):
            continue
        ticker = str(w.get("ticker", "")).strip()
        rationale_adv   = str(w.get("rationale", "")).strip()
        rationale_plain = str(w.get("rationale_plain", "")).strip()
        if not rationale_adv and not rationale_plain:
            w["rationale"] = _DEFAULT_RATIONALE
            w["rationale_plain"] = _DEFAULT_RATIONALE
            if ticker:
                log(f"  briefing rationale fallback: watch {ticker}")


def _fill_missing_mover_rationales(ai: dict, snap: Snapshot) -> None:
    """Ensure every rendered mover/crypto ticker has a non-empty rationale.

    Mutates ai in place. Logs each fallback so silent regressions are visible.
    Safe to call on any ai dict; no-op when ai is empty or signals
    failure/skip (those are handled by the renderers).
    """
    if not ai or "_skipped" in ai or "_error" in ai or "_raw" in ai:
        return
    # For crypto we only ever ask the AI about the top movers (see
    # _top_crypto_movers + build_ai_context), so fallback also targets only
    # that set. The other rows render without a rationale on purpose.
    sources = [
        ("why_gainers", snap.gainers, "gainer"),
        ("why_losers",  snap.losers,  "loser"),
        ("why_active",  snap.most_active, "most-active"),
        ("why_crypto",  _top_crypto_movers(snap), "crypto"),
    ]
    for key, items, label in sources:
        bucket = ai.get(key)
        if not isinstance(bucket, dict):
            bucket = {}
            ai[key] = bucket
        for m in items or []:
            sym = m.quote.symbol
            existing = (bucket.get(sym) or "").strip()
            if not existing:
                bucket[sym] = _DEFAULT_RATIONALE
                log(f"  AI rationale fallback: {label} {sym}")


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
        "crypto_top": [mw_brief(m) for m in _top_crypto_movers(snap)],
        "earnings_today": [
            {"sym": e.symbol_or_event, "name": e.description, "time": e.time, "extra": e.extra}
            for e in snap.earnings_today[:30]
        ],
        "econ_events_today": [
            {"event": e.description, "time": e.time, "extra": e.extra}
            for e in snap.econ_events_today[:20]
        ],
        "raw_world_news": [
            {"headline": n["headline"], "source": n["source"], "published": n["published"]}
            for n in snap.world_news_raw[:25]
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
  "session_recap_plain": "Same content as session_recap but rewritten for someone with NO finance background. Replace every piece of jargon with everyday words. Use short sentences (one idea each). Spell out acronyms on first use ('the Federal Reserve (the U.S. central bank that sets interest rates)', 'the VIX (a measure of how nervous traders are)'). Frame numbers in human terms when helpful ('the S&P 500 went up 1.2%, meaning a $1,000 investment would have gained about $12'). Friendly, conversational tone — like explaining to a curious friend. Keep all the same facts and numbers.",
  "crypto_recap": "1-2 paragraphs. BTC/ETH/XRP levels, top gainer and top loser in the top 20, notable volume or dominance shifts.",
  "crypto_recap_plain": "Plain-language version of crypto_recap with no jargon. 'Bitcoin' instead of 'BTC' on first mention, 'Ethereum' instead of 'ETH', etc. Conversational tone.",
  "today_setup": "Walk through tonight's/today's earnings (highlight highest-impact names with EPS estimates) and any economic events. For each name give one line on how it could shape the tape.",
  "today_setup_plain": "Plain-language version of today_setup. 'Earnings reports' = 'company quarterly results — when companies tell investors how much money they made'. 'EPS' = 'earnings per share — how much profit the company made for each share of stock'. Friendly, short sentences.",
  "tickers_to_watch": [
    {{
      "ticker": "XYZ",
      "bias": "bullish | bearish | neutral",
      "risk_level": "low | medium | high",
      "return_estimate": "+3-6% (swing) or -5-10% (short)",
      "rationale": "one-line signal — specific catalyst, level, or technical setup",
      "rationale_plain": "Same rationale but in plain language — no jargon ('catalyst' = 'reason', 'breakout' = 'big move up past a recent high', 'support level' = 'a price the stock has bounced off before'). Friendly tone.",
      "analysis": "2-3 sentences: trade thesis, key catalyst or level, primary risk to the thesis",
      "analysis_plain": "Same analysis in plain language. 2-3 short sentences. Explain WHY in human terms ('Nvidia jumped because the company told investors it expects to make more money than people thought'). Conversational tone."
    }},
    ... 6-10 items across all three risk tiers
  ],
  "crypto_outlook": "1-2 paragraphs on crypto positioning for the next 24 hours.",
  "crypto_outlook_plain": "Plain-language version of crypto_outlook. No jargon. Friendly tone.",
  "risk_notes": ["concrete risk bullet 1", "concrete risk bullet 2", "concrete risk bullet 3"],
  "risk_notes_plain": ["plain-language version of risk bullet 1 — what could go wrong, in everyday words", "plain version of bullet 2", "plain version of bullet 3"],
  "world_news": [
    {{
      "headline": "verbatim headline from raw_world_news",
      "source": "publisher name",
      "impact_summary": "One sentence: specific market consequence — what instrument moves, which direction, why",
      "impact_summary_plain": "Same impact in plain language — no jargon. Use everyday words: 'stocks' not 'equities', 'company size' not 'market cap', 'how much prices jump around' not 'volatility'. One short sentence.",
      "affected_tickers": ["TICK1", "TICK2"],
      "affected_markets": ["equities" | "bonds" | "crude oil" | "gold" | "crypto" | "forex" | "rates"],
      "direction": "bullish" | "bearish" | "mixed"
    }},
    ... select the 7-9 most market-moving items from raw_world_news; cover a range of themes (Fed/rates, geopolitical, sector-specific, commodity, crypto)
  ]
}}

Rules for world_news: only use headlines from raw_world_news. Prioritize macro-movers (Fed, war, tariffs,
inflation data, central bank decisions) over company-specific stories. For each, name the most directly
affected tickers or market (e.g. "XOM, CVX" for an oil story; "TLT, ^TNX" for a rates story).
Direction = bullish means good for risk assets overall or for the named tickers; bearish means the opposite.

Plain-language rules — the *_plain fields go to readers with no finance background:
- Replace jargon: "equities"→"stocks", "volatility"→"how much prices are jumping around",
  "bullish/bearish"→"optimistic/pessimistic" (or "looking up/looking down"), "market cap"→"company size",
  "P/E ratio"→"price compared to earnings", "yield"→"interest rate", "rally"→"big jump up",
  "correction"→"meaningful drop", "guidance"→"the company's forecast for future earnings",
  "consensus"→"what analysts expected".
- Explain WHY in human terms ("the Fed raised interest rates" → "the Federal Reserve (the U.S. central
  bank that sets interest rates) made borrowing money more expensive").
- Spell out acronyms on first use (FOMC, EPS, P/E, ETF, IPO, GDP, CPI, etc.) — define them once.
- Use short sentences with one idea each.
- Friendly, conversational tone — like explaining to a curious friend over coffee.
- Keep ALL the same facts and numbers — only translate the language around them.

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

    parsed: dict | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(text[start : end + 1])
            except Exception:
                parsed = None
    if parsed is None:
        log("Briefing: unparseable JSON returned — modal will be skipped.")
        return None
    _fill_missing_briefing_rationales(parsed)
    return parsed


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

    parsed: dict | None = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Best-effort: find the first/last brace
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1:
            try:
                parsed = json.loads(text[start : end + 1])
            except Exception:
                parsed = None
    if parsed is None:
        warn("AI returned unparseable JSON — showing raw text.", snap)
        return {"_raw": text}
    _fill_missing_mover_rationales(parsed, snap)
    return parsed


def fetch_premarket(snap: Snapshot) -> None:
    """Fetch live pre-market / overnight quotes and populate snap premarket fields."""
    snap.premarket_fetched_at = datetime.now(ET).isoformat(timespec="seconds")
    for attr, symbols in [
        ("premarket_us",     PREMARKET_US),
        ("premarket_macro",  PREMARKET_MACRO),
        ("premarket_crypto", PREMARKET_CRYPTO),
        ("overnight_global", OVERNIGHT_GLOBAL),
    ]:
        try:
            setattr(snap, attr, fetch_quotes(symbols))
        except Exception as e:
            warn(f"fetch_premarket {attr}: {e}", snap)


# ------------------------------------------------------------------------
# HTML rendering
# ------------------------------------------------------------------------
# HTML template moved to templates/base.html


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
    return _jinja_env.get_template("_tile.html").render(
        cls=cls, q=q, price=price, delta=delta
    )


def escape_html(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def mode_pair(std: str, adv: str) -> str:
    """Emit paired-mode HTML — Standard text shows by default, Advanced when the toggle is on.

    Pass already-escaped or trusted HTML; this wrapper does not call escape_html.
    """
    return f'<span class="std-only">{std}</span><span class="adv-only">{adv}</span>'


def mode_pair_text(std: str, adv: str) -> str:
    """Like mode_pair() but escapes both inputs first (use for plain text)."""
    return f'<span class="std-only">{escape_html(std)}</span><span class="adv-only">{escape_html(adv)}</span>'


# Plain-language labels for the directional bias classifications used throughout
# the report. The first element is the std-view label; the second is the adv-view.
_BIAS_LABELS = {
    "bullish": ("Looking Up",   "Bullish"),
    "bearish": ("Looking Down", "Bearish"),
    "neutral": ("Mixed",        "Neutral"),
}


def humanize_bias_label(bias: str) -> tuple[str, str]:
    """Return (plain, advanced) labels for a bias key. Falls back to title-cased input."""
    return _BIAS_LABELS.get(bias, (bias.title(), bias.title()))


def prose_block_pair(plain: str, advanced: str) -> str:
    """Render two paragraph blocks — one for std view, one for adv view.

    Each input is split on double-newlines into <p> tags. Both inputs are
    escape_html'd. If either side is empty the other is shown to both modes.
    """
    plain    = (plain or "").strip()
    advanced = (advanced or "").strip()
    if not plain and not advanced:
        return ""
    if not plain:
        plain = advanced
    if not advanced:
        advanced = plain

    def _to_paras(text: str) -> str:
        return "".join(
            f"<p>{escape_html(p.strip())}</p>"
            for p in text.split("\n\n") if p.strip()
        )

    return (
        f'<div class="std-only">{_to_paras(plain)}</div>'
        f'<div class="adv-only">{_to_paras(advanced)}</div>'
    )


def text_pair(plain: str, advanced: str) -> str:
    """Inline text variant — wraps each in a span and escapes."""
    plain    = (plain or "").strip()
    advanced = (advanced or "").strip()
    if not plain:
        plain = advanced
    if not advanced:
        advanced = plain
    return (
        f'<span class="std-only">{escape_html(plain)}</span>'
        f'<span class="adv-only">{escape_html(advanced)}</span>'
    )


def fmt_mcap_compact(value) -> str:
    """Format a market cap value as $X.XT / $X.XB / $XM.

    Accepts ints, floats, or strings like '$643,056,603,701' / '643056603701'.
    Returns '' for falsy / unparseable input.
    """
    if value in (None, "", 0):
        return ""
    try:
        n = float(re.sub(r"[^\d.\-]", "", str(value)))
    except (TypeError, ValueError):
        return str(value)
    if n == 0:
        return ""
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1e12: return f"{sign}${n/1e12:.2f}T"
    if n >= 1e9:  return f"{sign}${n/1e9:.1f}B"
    if n >= 1e6:  return f"{sign}${n/1e6:.0f}M"
    return f"{sign}${n:,.0f}"


def time_badge(t: str) -> str:
    """Render an earnings 'time' field as a small styled badge.

    Handles raw API tokens (time-pre-market, time-not-supplied, etc.) and
    falls back to a generic badge for anything unrecognized.
    """
    raw = (t or "").strip().lower()
    if raw in ("", "—", "-") or "not-supplied" in raw or "not_supplied" in raw \
       or "tbd" in raw or "high-potential" in raw:
        return f'<span class="time-badge time-tbd">{mode_pair("Time TBD", "TBD")}</span>'
    if any(x in raw for x in ("pre-market", "premarket", "before", "bmo", "pre")):
        return f'<span class="time-badge time-bmo">{mode_pair("Before Market Opens", "Pre-Market")}</span>'
    if any(x in raw for x in ("after-hours", "afterhours", "after", "amc", "post")):
        return f'<span class="time-badge time-amc">{mode_pair("After Market Closes", "After Hours")}</span>'
    return f'<span class="time-badge">{escape_html(t)}</span>'


def humanize_time_token(t: str) -> str:
    """Convert raw API time tokens like 'time-pre-market' into a readable phrase."""
    raw = (t or "").strip().lower()
    if not raw or raw in ("—", "-") or "not-supplied" in raw or "not_supplied" in raw or "tbd" in raw:
        return "at a time not yet announced"
    if any(x in raw for x in ("pre-market", "premarket", "before", "bmo", "pre")):
        return "before the market opens"
    if any(x in raw for x in ("after-hours", "afterhours", "after", "amc", "post")):
        return "after the market closes"
    return t


def render_mover_row(m: MoverWithNews, ai_why: dict[str, str] | None = None) -> str:
    q = m.quote
    cls = cls_for(q.change_pct)
    why = ""
    why_text = ((ai_why or {}).get(q.symbol) or "").strip()
    if why_text:
        why = f'<div class="why">{escape_html(why_text)}</div>'

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

    return _jinja_env.get_template("_mover.html").render(
        symbol=escape_html(q.symbol),
        name=escape_html(q.name),
        why=why,
        news_html=news_html,
        cls=cls,
        pct=fmt_pct(q.change_pct),
        price=fmt_usd(q.price),
    )


def render_movers_block(movers: list[MoverWithNews], ai_why: dict[str, str] | None, empty_msg: str) -> str:
    if not movers:
        return f'<div style="padding: 16px; color: var(--text-faint);">{empty_msg}</div>'
    return "".join(render_mover_row(m, ai_why) for m in movers)


def render_calendar_table(events: list[CalendarEvent], empty_msg: str) -> str:
    if not events:
        return f'<div style="padding: 16px; color: var(--text-faint);">{empty_msg}</div>'
    row_tpl = _jinja_env.get_template("_calendar_row.html")
    rows = []
    for e in events:
        name = escape_html(e.description)
        if e.url:
            name = f'<a href="{escape_html(e.url)}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;text-underline-offset:3px;">{name}</a>'
        rows.append(row_tpl.render(
            time=time_badge(e.time),
            symbol_or_event=escape_html(e.symbol_or_event),
            name=name,
            extra=escape_html(e.extra),
        ))
    return f"""
    <table>
      <thead><tr><th>Time</th><th>Symbol / Region</th><th>Event</th><th>Details</th></tr></thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    """


def render_earnings_section(snap: Snapshot) -> str:
    """
    Earnings & Events section:
    - Top 5-6 companies by market cap as always-visible featured cards
    - Remaining earnings + all economic events inside a <details> expander
    """
    earnings = sorted(snap.earnings_today, key=lambda e: e.market_cap, reverse=True)
    featured = earnings[:6]
    rest     = earnings[6:]

    def _time_class(t: str) -> str:
        t = (t or "").lower()
        if any(x in t for x in ("before", "bmo", "pre")):
            return "bmo"
        if any(x in t for x in ("after", "amc", "post")):
            return "amc"
        return ""

    def _time_label(t: str) -> str:
        t = (t or "").lower()
        if any(x in t for x in ("before", "bmo", "pre")):
            return "Before Open"
        if any(x in t for x in ("after", "amc", "post")):
            return "After Close"
        if not t or t in ("—", "-") or "not-supplied" in t or "not_supplied" in t \
           or "tbd" in t or "high-potential" in t:
            return "TBD"
        return t.title()

    # Build lookup maps from reactions and results for fast access
    reaction_map: dict[str, MoverWithNews] = {
        mw.quote.symbol: mw for mw in snap.earnings_reactions
    }

    cards = []
    for e in featured:
        sym = e.symbol_or_event
        tc = _time_class(e.time)
        tl = _time_label(e.time)
        eps_est_str = ""
        for part in e.extra.split("·"):
            if "EPS" in part or "eps" in part:
                eps_est_str = part.strip()
                break

        name_html = (
            f'<a href="{escape_html(e.url)}" target="_blank" rel="noopener">'
            f'{escape_html(e.description)}</a>'
            if e.url else escape_html(e.description)
        )

        result = snap.earnings_results.get(sym)
        reaction = reaction_map.get(sym)

        if result:
            # Company has already reported — show enriched result card
            verdict = result["verdict"]
            card_cls = {"BEAT": "beat", "MISS": "miss", "IN-LINE": "inline"}.get(verdict, tc)
            eps_line = (
                f'EPS: ${result["eps_act"]:.2f} vs ${result["eps_est"]:.2f} est '
                f'({"+":}{result["surprise_pct"]:.1f}%)'
                if result["surprise_pct"] >= 0
                else f'EPS: ${result["eps_act"]:.2f} vs ${result["eps_est"]:.2f} est '
                     f'({result["surprise_pct"]:.1f}%)'
            )
            move_html = ""
            if reaction:
                pct = reaction.quote.change_pct
                sign = "+" if pct >= 0 else ""
                move_cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
                move_html = f'<span class="ef-move {move_cls}">{sign}{pct:.2f}%</span>'

            # News summary: top headline from reactions or world news
            summary = ""
            if reaction and reaction.news:
                summary = reaction.news[0].title
            elif not summary:
                for item in snap.world_news_raw:
                    if sym.upper() in item.get("headline", "").upper():
                        summary = item.get("headline", "")
                        break

            summary_html = (
                f'<div class="ef-summary">{escape_html(summary)}</div>' if summary else ""
            )

            cards.append(
                f'<div class="ef-card {card_cls}">'
                f'  <div class="ef-sym-row">'
                f'    <span class="ef-sym">{escape_html(sym)}</span>'
                f'    <span class="ef-verdict {verdict}">{verdict}</span>'
                f'  </div>'
                f'  <div class="ef-name">{name_html}</div>'
                f'  <div class="ef-result">'
                f'    <span class="ef-eps">{escape_html(eps_line)}</span>'
                f'    {move_html}'
                f'  </div>'
                f'  {summary_html}'
                f'</div>'
            )
        else:
            # Not yet reported — show upcoming card with estimate
            cards.append(
                f'<div class="ef-card {tc}">'
                f'  <div class="ef-sym-row"><span class="ef-sym">{escape_html(sym)}</span></div>'
                f'  <div class="ef-name">{name_html}</div>'
                f'  <div class="ef-meta">'
                f'    <span class="ef-badge {tc}">{escape_html(tl)}</span>'
                + (f'    <span class="ef-badge">{escape_html(eps_est_str)}</span>' if eps_est_str else '')
                + f'  </div>'
                f'</div>'
            )

    featured_html = (
        f'<div class="earnings-featured">{"".join(cards)}</div>'
        if cards else ""
    )

    rest_table  = render_calendar_table(rest, "") if rest else ""
    econ_block  = render_econ_news_block(snap)

    rest_label = f"All earnings ({len(earnings)} companies)" if rest else "Economic events"
    extra_html = (
        f'<details class="earnings-extra">'
        f'<summary>{rest_label} &amp; economic events</summary>'
        f'<div class="cols">'
        + (f'<div class="panel"><div class="panel-head"><h3><span class="std-only">Companies Sharing Their Profits Today</span><span class="adv-only">All Earnings</span></h3>'
           f'<div class="sub">Full list</div></div>{rest_table}</div>' if rest else "")
        + f'<div class="panel"><div class="panel-head"><h3><span class="std-only">Important Economic News &amp; Reports</span><span class="adv-only">Economic Events &amp; News</span></h3>'
          f'<div class="sub">Scheduled releases &amp; macro headlines</div></div>{econ_block}</div>'
        f'</div></details>'
    )

    if not earnings and not snap.econ_events_today and not snap.world_news_raw:
        return ""

    return (
        f'<div class="earnings-section" id="earnings-cal">'
        f'<div class="earnings-section-label">'
        f'{mode_pair("Companies Sharing Profits &amp; Government Reports", "Earnings &amp; Events")} · '
        f'{datetime.fromisoformat(snap.generated_at[:10]).strftime("%B %-d, %Y")}'
        f'</div>'
        + featured_html
        + extra_html
        + '</div>'
    )


# Keyword sets for matching world news to economic event categories
_ECON_TOPIC_KEYS: list[tuple[str, list[str]]] = [
    ("fed",       ["fed", "fomc", "federal reserve", "powell", "rate decision", "rate cut",
                   "rate hike", "monetary policy", "basis point", "bps", "dovish", "hawkish"]),
    ("inflation", ["cpi", "inflation", "consumer price", "pce", "ppi", "producer price",
                   "core inflation", "price index"]),
    ("jobs",      ["jobs", "employment", "payroll", "nfp", "unemployment", "jobless",
                   "labor market", "job growth", "claims"]),
    ("gdp",       ["gdp", "gross domestic", "recession", "economic growth", "output"]),
    ("trade",     ["tariff", "trade war", "trade deal", "import", "export", "deficit",
                   "sanctions", "trade policy"]),
    ("housing",   ["housing", "home sales", "mortgage", "real estate", "construction"]),
    ("consumer",  ["retail sales", "consumer confidence", "consumer spending", "sentiment"]),
    ("earnings_macro", ["earnings season", "corporate earnings", "profit", "guidance"]),
]

def _topics_for_event(desc: str) -> list[str]:
    """Return topic keys that match an event description string."""
    d = desc.lower()
    return [key for key, words in _ECON_TOPIC_KEYS if any(w in d for w in words)]

def _topics_for_headline(headline: str) -> list[str]:
    """Return topic keys that a news headline belongs to."""
    h = headline.lower()
    return [key for key, words in _ECON_TOPIC_KEYS if any(w in h for w in words)]

def _impact_level(desc: str) -> str:
    """Classify an economic event as high / med / low impact."""
    d = desc.lower()
    if any(w in d for w in ["fomc", "fed", "cpi", "nfp", "payroll", "gdp", "pce"]):
        return "high-impact"
    if any(w in d for w in ["ppi", "retail", "housing", "claims", "ism"]):
        return "med-impact"
    return "low-impact"


def render_econ_news_block(snap: Snapshot) -> str:
    """
    Rich economic-events panel: each event card shows its scheduled time/details
    plus matched news headlines from world_news_raw. Falls back to theme-grouped
    macro news when the calendar is sparse or empty.
    """
    news_items = snap.world_news_raw  # already sorted recent-first

    # Build topic → list[news] index from world_news_raw
    topic_news: dict[str, list[dict]] = {key: [] for key, _ in _ECON_TOPIC_KEYS}
    for item in news_items:
        for t in _topics_for_headline(item.get("headline", "")):
            if len(topic_news[t]) < 4:
                topic_news[t].append(item)

    def _news_html(matched: list[dict]) -> str:
        if not matched:
            return ""
        items_html = ""
        for n in matched[:3]:
            src  = escape_html(n.get("source", ""))
            hl   = escape_html(n.get("headline", ""))
            url  = n.get("url", "")
            link = (f'<a href="{escape_html(url)}" target="_blank" rel="noopener">{hl}</a>'
                    if url else hl)
            src_span = f' <span class="econ-news-src">— {src}</span>' if src else ""
            items_html += f'<div class="econ-news-item">{link}{src_span}</div>'
        return f'<div class="econ-ev-news">{items_html}</div>'

    cards_html = ""

    # Calendar-driven cards
    for ev in snap.econ_events_today:
        topics  = _topics_for_event(ev.description)
        matched = []
        seen_hl = set()
        for t in topics:
            for n in topic_news.get(t, []):
                k = n.get("headline", "")[:60]
                if k not in seen_hl:
                    seen_hl.add(k)
                    matched.append(n)
        matched = matched[:3]

        lvl = _impact_level(ev.description)
        cards_html += (
            f'<div class="econ-event-card {lvl}">'
            f'  <div class="econ-ev-top">'
            f'    <span class="econ-ev-time">{escape_html(ev.time)}</span>'
            f'    <span class="econ-ev-name">{escape_html(ev.description)}</span>'
            f'  </div>'
            + (f'<div class="econ-ev-extra">{escape_html(ev.extra)}</div>' if ev.extra else "")
            + _news_html(matched)
            + '</div>'
        )

    # If calendar is sparse, add theme cards for topics with news but no matching event
    covered_topics = set()
    for ev in snap.econ_events_today:
        covered_topics.update(_topics_for_event(ev.description))

    theme_labels = {
        "fed": "Federal Reserve & Rates",
        "inflation": "Inflation",
        "trade": "Trade & Tariffs",
        "jobs": "Labor Market",
        "gdp": "Economic Growth",
        "housing": "Housing",
        "consumer": "Consumer",
    }
    for key, label in theme_labels.items():
        if key in covered_topics:
            continue
        items = topic_news.get(key, [])
        if not items:
            continue
        cards_html += (
            f'<div class="econ-event-card low-impact">'
            f'  <div class="econ-ev-top">'
            f'    <span class="econ-ev-name">{escape_html(label)}</span>'
            f'  </div>'
            + _news_html(items)
            + '</div>'
        )

    if not cards_html:
        return '<div style="color:var(--text-faint);font-size:13px;padding:8px 0">No economic events or macro news available.</div>'

    return f'<div class="econ-section">{cards_html}</div>'


def render_narrative(ai: dict) -> str:
    # When AI is disabled, skipped, or errored, render NOTHING. The headline
    # data already tells the story; we don't want to pollute the report with
    # billing/error banners.
    if not ai or "_skipped" in ai or "_error" in ai or "_raw" in ai:
        return ""
    text = ai.get("market_narrative", "")
    if not text:
        return ""
    return _jinja_env.get_template("_narr.html").render(
        extra="",
        label="Market Narrative · Yesterday",
        body=f"<p>{escape_html(text)}</p>",
    )


def render_today_outlook(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    text = ai.get("today_outlook", "")
    if not text:
        return ""
    return _jinja_env.get_template("_narr.html").render(
        extra="",
        label=mode_pair("What might happen today", "Today's Outlook"),
        body=f"<p>{escape_html(text)}</p>",
    )


def render_crypto_outlook(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    text       = ai.get("crypto_outlook", "")
    text_plain = ai.get("crypto_outlook_plain", "") or text
    if not text:
        return ""
    return _jinja_env.get_template("_narr.html").render(
        extra="",
        label=mode_pair("What might happen with cryptocurrency today", "Crypto Outlook"),
        body=prose_block_pair(text_plain, text),
    )


def render_risk_block(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    raw       = ai.get("risk_notes", "")
    raw_plain = ai.get("risk_notes_plain", "") or raw
    text       = "\n\n".join(raw)       if isinstance(raw, list)       else (raw or "")
    text_plain = "\n\n".join(raw_plain) if isinstance(raw_plain, list) else (raw_plain or text)
    if not text:
        return ""
    return _jinja_env.get_template("_narr.html").render(
        extra=" risk",
        label=mode_pair("What could cause problems today", "Risk Notes"),
        body=prose_block_pair(text_plain, text),
    )


def _ticker_cards_html(picks: list[dict]) -> str:
    """Render a risk-tiered grid of ticker prediction cards."""
    tiers = [
        ("low",    mode_pair("Safer Bets",      "Low Risk")),
        ("medium", mode_pair("Medium-Risk Bets", "Medium Risk")),
        ("high",   mode_pair("Riskier Bets",    "High Risk")),
    ]
    bias_arrow = {"bullish": "▲", "bearish": "▼", "neutral": "—"}
    out = []
    for tier_key, tier_label in tiers:
        tier_picks = [p for p in picks if p.get("risk_level", "medium") == tier_key]
        if not tier_picks:
            continue
        cards = []
        for p in tier_picks:
            bias = p.get("bias", "neutral")
            arrow = bias_arrow.get(bias, "—")
            ret   = escape_html(p.get("return_estimate", ""))
            rat_adv   = p.get("rationale", "")
            rat_plain = p.get("rationale_plain") or rat_adv
            ana_adv   = p.get("analysis", "")
            ana_plain = p.get("analysis_plain") or ana_adv
            sym   = escape_html(str(p.get("ticker", "")))
            bias_plain, bias_adv_label = humanize_bias_label(bias)
            # Strip jargon like "(gap risk)" for the std variant
            ret_plain = ret.replace("(gap risk)", "(could swing either way)").replace("(short)", "(betting it goes down)").replace("(swing)", "(short-term trade)")
            if ret:
                ret_html = (
                    f'<div class="tc-return">'
                    f'<span class="std-only">What we think it could move: {ret_plain}</span>'
                    f'<span class="adv-only">Est. return: {ret}</span>'
                    f'</div>'
                )
            else:
                ret_html = ""

            rat_html = (
                f'<div class="tc-rationale">'
                f'<span class="std-only">{escape_html(rat_plain)}</span>'
                f'<span class="adv-only">{escape_html(rat_adv)}</span>'
                f'</div>'
            )
            if ana_adv or ana_plain:
                ana_html = (
                    f'<div class="tc-analysis">'
                    f'<span class="std-only">{escape_html(ana_plain)}</span>'
                    f'<span class="adv-only">{escape_html(ana_adv)}</span>'
                    f'</div>'
                )
            else:
                ana_html = ""

            cards.append(
                f'<div class="ticker-card {tier_key}">'
                f'  <div class="tc-top">'
                f'    <span class="tc-symbol">{sym}</span>'
                f'    <span class="tc-bias {bias}">{arrow} '
                f'<span class="std-only">{escape_html(bias_plain)}</span>'
                f'<span class="adv-only">{escape_html(bias_adv_label)}</span>'
                f'</span>'
                f'  </div>'
                f'  {ret_html}'
                f'  {rat_html}'
                f'  {ana_html}'
                f'</div>'
            )
        out.append(
            f'<div class="risk-tier">'
            f'<div class="risk-tier-header {tier_key}">'
            f'<span class="risk-dot {tier_key}"></span>{tier_label}'
            f'</div>'
            f'<div class="ticker-cards">{"".join(cards)}</div>'
            f'</div>'
        )
    return "".join(out)


def render_tickers_to_watch(ai: dict) -> str:
    if not ai or "_skipped" in ai or "_error" in ai:
        return ""
    watch = ai.get("tickers_to_watch") or []
    if not watch:
        return ""
    cards_html = _ticker_cards_html(watch)
    return (
        '<h2><span class="std-only">Stocks We Think Are Worth Watching</span><span class="adv-only">Tickers to Watch &amp; Predictions</span></h2>'
        + cards_html
    )


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


def _index_chip(label: str, pct: float, price: float | None = None) -> str:
    """Colored pill showing label, direction arrow, and % change."""
    cls = "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")
    arrow = "▲" if pct > 0.05 else ("▼" if pct < -0.05 else "—")
    price_str = f" · {fmt_num(price)}" if price is not None else ""
    return f'<span class="b-chip {cls}">{escape_html(label)} {arrow}{abs(pct):.2f}%{escape_html(price_str)}</span>'


def _b_exec_summary(snap: Snapshot) -> list[str]:
    bullets: list[str] = []
    idx = {q.symbol: q for q in snap.indices}
    sp, dji, ixic, vix = idx.get("^GSPC"), idx.get("^DJI"), idx.get("^IXIC"), idx.get("^VIX")
    rut = idx.get("^RUT")

    # Plain-text index summary (chips are rendered separately above the bullet list)
    idx_parts = []
    for q, label in [(sp, "S&P 500"), (dji, "Dow"), (ixic, "Nasdaq"), (rut, "Russell 2K")]:
        if q:
            sign = "+" if q.change_pct >= 0 else ""
            idx_parts.append(f"{label} {sign}{q.change_pct:.2f}%")
    if vix:
        sign = "+" if vix.change_pct >= 0 else ""
        idx_parts.append(f"VIX {sign}{vix.change_pct:.2f}% to {vix.price:.2f}")
    if idx_parts:
        bullets.append(" · ".join(idx_parts))

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
    index_chips_html = ""
    if snap.indices:
        chips = "".join(_index_chip(q.name.split("(")[0].strip(), q.change_pct, q.price) for q in snap.indices)
        index_chips_html = f'<div class="b-index-row">{chips}</div>'
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
        '<div class="bs-label"><span class="std-only">How U.S. stocks did yesterday</span><span class="adv-only">US Markets · Yesterday\'s Session</span></div>'
        f'{index_chips_html}{idx_table}{movers_2col}'
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
        '<div class="bs-label"><span class="std-only">How markets did around the world</span><span class="adv-only">Global Markets</span></div>'
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
    # The label here says "Top 10 by Market Cap" — snap.crypto's display order
    # has BTC/ETH pinned and the rest by move size, so re-sort locally to keep
    # this section honest.
    by_mcap = sorted(snap.crypto, key=lambda m: m.quote.market_cap or 0, reverse=True)
    rows = "".join(
        f'<tr><td style="font-weight:700">{escape_html(m.quote.symbol)}</td>'
        f'<td style="color:#8a92a6;font-size:11px">{escape_html(m.quote.name)}</td>'
        f'<td class="num" style="text-align:right">{fmt_usd(m.quote.price)}</td>'
        f'<td class="num" style="text-align:right">{_pct_span(m.quote.change_pct)}</td>'
        f'<td class="num" style="text-align:right;color:#8a92a6">{fmt_usd(m.quote.dollar_volume) if m.quote.dollar_volume else "—"}</td></tr>'
        for m in by_mcap[:10]
    )
    return (
        '<div class="briefing-section crypto">'
        '<div class="bs-label"><span class="std-only">The 10 biggest cryptocurrencies</span><span class="adv-only">Crypto Markets · Top 10 by Market Cap</span></div>'
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
            f'<tr><td class="time">{time_badge(e.time)}</td>'
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
            f'<tr><td class="time">{time_badge(e.time)}</td>'
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
        '<div class="bs-label"><span class="std-only">What to watch for today</span><span class="adv-only">Today\'s Setup — What to Watch</span></div>'
        + "".join(parts) +
        '</div>'
    )


def _b_risks(snap: Snapshot) -> str:
    risks_adv:   list[str] = []
    risks_plain: list[str] = []
    big_names = {"AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA", "JPM", "BAC", "NFLX"}
    big_earnings = [e for e in snap.earnings_today if e.symbol_or_event in big_names]
    if big_earnings:
        tickers = ", ".join(e.symbol_or_event for e in big_earnings[:5])
        risks_adv.append(f"High-impact earnings today ({tickers}) — misses or cautious guidance can gap indices at open.")
        risks_plain.append(
            f"Big companies report their quarterly results today ({tickers}). If they earned less than expected — or warn that future results will be weaker — the whole market can drop right at the open."
        )

    vix = next((q for q in snap.indices if q.symbol == "^VIX"), None)
    if vix and vix.price > 20:
        risks_adv.append(f"VIX elevated at {vix.price:.2f} — options market pricing above-average volatility.")
        risks_plain.append(
            f"The fear gauge (called the VIX) is high at {vix.price:.2f}. That means traders are paying more to protect their bets — they expect bigger price swings than usual."
        )

    crude = next((q for q in snap.macro if "Crude" in q.name), None)
    if crude and abs(crude.change_pct) > 3:
        dir_ = "surge" if crude.change_pct > 0 else "drop"
        risks_adv.append(f"WTI crude {dir_} {crude.change_pct:+.1f}% to {fmt_usd(crude.price)} — watch macro read-through to consumer and transport names.")
        word = "jumped" if crude.change_pct > 0 else "dropped"
        risks_plain.append(
            f"Oil prices {word} {abs(crude.change_pct):.1f}% to {fmt_usd(crude.price)} a barrel. That ripples into gas prices, airline costs, and what shoppers spend on everything else."
        )

    tnx = next((q for q in snap.macro if "10Y" in q.name), None)
    if tnx and tnx.price > 4.5:
        risks_adv.append(f"10Y yield at {tnx.price:.2f}% — elevated rates a headwind for growth and rate-sensitive equities.")
        risks_plain.append(
            f"Long-term U.S. government interest rates are high ({tnx.price:.2f}%). When rates are this high, borrowing is expensive, and that hurts fast-growing companies and anything sensitive to interest rates."
        )

    fed_evts = [e for e in snap.econ_events_today if any(
        kw in (e.description or "").upper() for kw in ["FOMC", "FEDERAL RESERVE", "POWELL", "RATE DECISION"]
    )]
    if fed_evts:
        risks_adv.append("FOMC/Fed event today — any surprise on rates or tone could trigger outsized moves across asset classes.")
        risks_plain.append(
            "The Federal Reserve (the U.S. central bank that sets interest rates) is making news today. Any surprise about interest rates — or even how they word things — can cause big swings in stocks and bonds."
        )

    if snap.global_indices:
        weak = [q for q in snap.global_indices if q.change_pct < -1.5]
        if weak:
            names = ", ".join(q.name.split("(")[0].strip() for q in weak[:3])
            risks_adv.append(f"Global market weakness ({names}) may weigh on pre-market sentiment.")
            risks_plain.append(
                f"Markets in other countries had a rough day ({names}). That negative mood often carries into the U.S. opening."
            )

    if not risks_adv:
        risks_adv.append("No major elevated risk signals detected in today's data.")
        risks_plain.append("Nothing scary stands out in today's data — looks like a normal trading day.")

    lis_adv = "".join(f"<li>{escape_html(r)}</li>" for r in risks_adv[:4])
    lis_std = "".join(f"<li>{escape_html(r)}</li>" for r in risks_plain[:4])
    return (
        '<div class="briefing-section risk">'
        '<div class="bs-label"><span class="std-only">What could cause problems today</span><span class="adv-only">Risk Notes</span></div>'
        f'<ul class="std-only">{lis_std}</ul>'
        f'<ul class="adv-only">{lis_adv}</ul>'
        '</div>'
    )


def _b_session_narrative(snap: Snapshot) -> str:
    """2-3 sentence plain-English summary of yesterday's session."""
    idx = {q.symbol: q for q in snap.indices}
    sp  = idx.get("^GSPC")
    dji = idx.get("^DJI")
    ixic = idx.get("^IXIC")
    vix = idx.get("^VIX")
    adv_sentences:   list[str] = []
    plain_sentences: list[str] = []
    if sp:
        dir_ = "gained" if sp.change_pct > 0 else ("lost" if sp.change_pct < 0 else "closed flat at")
        if sp.change_pct != 0:
            adv_sentences.append(
                f"The S&P 500 {dir_} {abs(sp.change_pct):.2f}% to {sp.price:,.2f}"
                + (f", while the Nasdaq {'+' if (ixic and ixic.change_pct >= 0) else ''}{ixic.change_pct:.2f}% and Dow {'+' if (dji and dji.change_pct >= 0) else ''}{dji.change_pct:.2f}%." if ixic and dji else ".")
            )
            move_word = "went up" if sp.change_pct > 0 else "went down"
            example_dollars = abs(sp.change_pct) * 10
            plain_sentences.append(
                f"The S&P 500 (the index that tracks 500 of the biggest U.S. companies) {move_word} {abs(sp.change_pct):.2f}% — meaning if you had $1,000 invested, you'd have about ${example_dollars:.2f} {'more' if sp.change_pct > 0 else 'less'}."
            )
            if ixic and dji:
                plain_sentences.append(
                    f"The Nasdaq (more tech-heavy) {'rose' if ixic.change_pct >= 0 else 'fell'} {abs(ixic.change_pct):.2f}% and the Dow Jones (30 large blue-chip companies) {'rose' if dji.change_pct >= 0 else 'fell'} {abs(dji.change_pct):.2f}%."
                )
    if snap.gainers and snap.losers:
        g, l = snap.gainers[0].quote, snap.losers[0].quote
        news_g = snap.gainers[0].news[0].title[:60] if snap.gainers[0].news else ""
        adv_sentences.append(
            f"Top mover: {g.symbol} +{g.change_pct:.1f}% to {fmt_usd(g.price)}"
            + (f" ({news_g})" if news_g else "")
            + f". Largest decline: {l.symbol} {l.change_pct:.1f}% to {fmt_usd(l.price)}."
        )
        plain_sentences.append(
            f"The biggest winner was {g.symbol}, whose stock jumped {g.change_pct:.1f}% to {fmt_usd(g.price)}"
            + (f" ({news_g.lower() if news_g else ''})" if news_g else "")
            + f". The biggest loser was {l.symbol}, down {abs(l.change_pct):.1f}% to {fmt_usd(l.price)}."
        )
    crude = next((q for q in snap.macro if "Crude" in q.name), None)
    tnx   = next((q for q in snap.macro if "10Y"   in q.name), None)
    if crude or tnx:
        parts = []
        if crude: parts.append(f"WTI crude {'+' if crude.change_pct >= 0 else ''}{crude.change_pct:.2f}% to {fmt_usd(crude.price)}")
        if tnx:   parts.append(f"10Y yield {tnx.price:.2f}%")
        if vix:   parts.append(f"VIX {'+' if vix.change_pct >= 0 else ''}{vix.change_pct:.2f}% to {vix.price:.2f}")
        adv_sentences.append(" · ".join(parts) + ".")

        plain_parts = []
        if crude:
            plain_parts.append(f"oil prices {'rose' if crude.change_pct >= 0 else 'fell'} {abs(crude.change_pct):.2f}% to {fmt_usd(crude.price)} a barrel")
        if tnx:
            plain_parts.append(f"the 10-year Treasury rate (a key long-term U.S. interest rate) sat at {tnx.price:.2f}%")
        if vix:
            plain_parts.append(f"the VIX 'fear gauge' was at {vix.price:.2f}")
        if plain_parts:
            plain_sentences.append("Elsewhere: " + ", and ".join(plain_parts) + ".")
    if not adv_sentences:
        return ""
    return (
        '<div class="briefing-section">'
        '<div class="bs-label"><span class="std-only">What happened in the market yesterday</span><span class="adv-only">Yesterday\'s Session</span></div>'
        + prose_block_pair(" ".join(plain_sentences), " ".join(adv_sentences)) +
        '</div>'
    )


def _b_tickers_prediction(snap: Snapshot) -> list[dict]:
    """Data-driven tickers to watch. Returns list of pick dicts for risk-tiered rendering."""
    picks: list[dict] = []
    seen: set[str] = set()

    def add(ticker: str, bias: str, risk: str, ret: str,
            rationale: str, analysis: str,
            rationale_plain: str = "", analysis_plain: str = "") -> None:
        if ticker and ticker not in seen and len(picks) < 10:
            seen.add(ticker)
            picks.append({
                "ticker": ticker, "bias": bias, "risk_level": risk,
                "return_estimate": ret,
                "rationale": rationale, "analysis": analysis,
                "rationale_plain": rationale_plain or rationale,
                "analysis_plain":  analysis_plain  or analysis,
            })

    # 1. Earnings reporters — binary gap risk = HIGH
    for e in snap.earnings_today[:3]:
        sym = e.symbol_or_event
        if sym and sym.isalpha() and len(sym) <= 5:
            detail = f" ({e.extra})" if e.extra else ""
            human_time = humanize_time_token(e.time) if e.time else "today"
            add(sym, "neutral", "high", "±5-15% (gap risk)",
                f"Reporting {e.time or 'today'}{detail}.",
                f"Earnings prints create binary gap risk — a beat typically gaps +5-15% at open while a miss or guidance cut can produce the reverse. "
                f"Enter only with defined risk via options or tight stops. "
                f"Monitor pre-market tape for whisper numbers and institutional flow before committing size.",
                rationale_plain=f"Sharing quarterly results {human_time}{detail}.",
                analysis_plain=(
                    f"When a company tells investors how much it earned, the stock often jumps or drops sharply — sometimes 5-15% — depending on whether the numbers beat or missed expectations. "
                    f"This is risky because the move happens fast. Watch carefully before placing any trade."
                ))

    # 2. Biggest gainer — continuation
    if snap.gainers:
        g = snap.gainers[0].quote
        if abs(g.change_pct) > 4:
            mag = g.change_pct
            ret_lo = round(mag * 0.2, 1)
            ret_hi = round(mag * 0.5, 1)
            add(g.symbol, "bullish", "medium", f"+{ret_lo}-{ret_hi}%",
                f"Led gainers at +{mag:.1f}% to {fmt_usd(g.price)} yesterday.",
                f"Large single-session moves in high-volume names often see partial continuation into the following session as momentum traders add and short-sellers cover. "
                f"The primary risk is a mean-reversion fade if yesterday's move was news-driven without a fundamental repricing. "
                f"Watch for volume confirmation in the first 30 minutes — low open volume is an early fade signal.",
                rationale_plain=f"Was yesterday's biggest winner — up {mag:.1f}% to {fmt_usd(g.price)}.",
                analysis_plain=(
                    f"When a stock has a really strong day, it often keeps going up the next day too — other traders see the gain and pile in. "
                    f"The risk: if yesterday's jump was just from one piece of news, the stock could give some of those gains back. "
                    f"Watch the first 30 minutes of trading — if not many people are buying, it might fade."
                ))

    # 3. Biggest loser — bounce or continuation
    if snap.losers:
        l = snap.losers[0].quote
        mag = abs(l.change_pct)
        if mag > 4:
            ret_lo = round(mag * 0.15, 1)
            ret_hi = round(mag * 0.35, 1)
            add(l.symbol, "bearish", "medium", f"-{ret_lo}-{ret_hi}%",
                f"Led losers at {l.change_pct:.1f}% to {fmt_usd(l.price)} yesterday.",
                f"High-volume declines frequently see follow-through selling as institutional holders reposition and stop-losses trigger below the prior close. "
                f"A dead-cat bounce is possible intraday but the path of least resistance is lower until a fundamental catalyst appears. "
                f"Short thesis is best expressed intraday given elevated borrow costs after large single-day drops.",
                rationale_plain=f"Was yesterday's biggest loser — down {abs(l.change_pct):.1f}% to {fmt_usd(l.price)}.",
                analysis_plain=(
                    f"When a stock falls hard, it often keeps falling the next day — big investors keep selling, and automatic 'sell' orders kick in. "
                    f"It might bounce briefly during the day, but the overall direction is usually down until something good happens. "
                    f"Betting on further declines gets expensive after a big drop, so this is best played carefully and quickly."
                ))

    # 4. Crypto equity proxy — HIGH risk
    btc = next((m.quote for m in snap.crypto if m.quote.symbol.upper() == "BTC"), None)
    if btc and abs(btc.change_pct) > 3:
        bias = "bullish" if btc.change_pct > 0 else "bearish"
        proxy = next((m for m in snap.most_active
                      if m.quote.symbol in ("COIN", "MSTR", "MARA", "RIOT", "HOOD")), None)
        if proxy:
            mult = 2.5
            est = round(abs(btc.change_pct) * mult, 1)
            add(proxy.quote.symbol, bias, "high", f"{'+'if bias=='bullish' else '-'}{est//2:.0f}-{est:.0f}%",
                f"BTC {'+' if btc.change_pct >= 0 else ''}{btc.change_pct:.1f}% — crypto equity proxy.",
                f"Crypto equities trade at a 2-3× beta to spot Bitcoin moves, amplifying both upside and downside. "
                f"{proxy.quote.symbol} is currently the highest-volume proxy, making it the fastest vehicle for this directional thesis. "
                f"Risk is elevated: crypto equities are subject to equity-market correlation during risk-off sessions that may override the spot BTC signal.",
                rationale_plain=f"Bitcoin moved {btc.change_pct:+.1f}% — this stock tends to move with it.",
                analysis_plain=(
                    f"Stocks tied to crypto, like {proxy.quote.symbol}, tend to swing 2-3× harder than Bitcoin itself — bigger gains when Bitcoin's up, bigger losses when it's down. "
                    f"This is a fast way to bet on Bitcoin's direction without buying Bitcoin directly. "
                    f"It's risky: if the broader stock market has a bad day, this could fall too — even if Bitcoin holds up."
                ))

    # 5. Most-active fill — MEDIUM risk
    for m in snap.most_active:
        q = m.quote
        bias = "bullish" if q.change_pct > 0.5 else ("bearish" if q.change_pct < -0.5 else "neutral")
        vol_str = fmt_usd(q.dollar_volume) if q.dollar_volume else "high"
        mag = abs(q.change_pct)
        ret_est = f"+{mag*0.2:.1f}-{mag*0.4:.1f}%" if mag > 1 else "+1-3%"
        add(q.symbol, bias, "medium", ret_est,
            f"Most active at {vol_str} dollar volume — elevated institutional flow.",
            f"High-dollar-volume sessions signal institutional participation that often sustains directional moves into the next open. "
            f"The elevated activity makes this name more sensitive to broad market direction — a weak tape will weigh on even fundamentally sound names. "
            f"Set alerts at yesterday's high and low as breakout/breakdown triggers.",
            rationale_plain=f"One of the most heavily traded stocks ({vol_str} changed hands) — big investors are paying attention.",
            analysis_plain=(
                f"When huge amounts of money trade in a stock in one day, it usually means big institutional investors are taking positions. "
                f"That tends to keep the stock moving the same direction the next day. "
                f"The downside: stocks like this swing harder when the overall market has a good or bad day."
            ))

    return picks


def _b_coming_day(snap: Snapshot) -> str:
    """Brief synopsis of what to watch in the coming trading session."""
    adv_lines:   list[str] = []
    plain_lines: list[str] = []

    sp_fut = next((q for q in snap.premarket_us if "S&P" in q.name or "Fut" in q.name), None)
    if sp_fut:
        dir_ = "pointing higher" if sp_fut.change_pct > 0.1 else ("pointing lower" if sp_fut.change_pct < -0.1 else "flat")
        adv_lines.append(f"S&P futures are {dir_} pre-market ({sp_fut.change_pct:+.2f}%), setting the early directional bias.")
        plain_word = "higher" if sp_fut.change_pct > 0.1 else ("lower" if sp_fut.change_pct < -0.1 else "about even")
        plain_lines.append(
            f"Before the U.S. stock market opens, early bets on the S&P 500 are pointing {plain_word} ({sp_fut.change_pct:+.2f}%). That gives an early hint at how the day might start."
        )

    if snap.earnings_today:
        tickers = [e.symbol_or_event for e in snap.earnings_today[:6] if e.symbol_or_event]
        n = len(snap.earnings_today)
        adv_lines.append(f"{n} companies report today — key names: {', '.join(tickers[:5])}{'…' if n > 5 else ''}. Expect elevated volatility around open and post-market.")
        plain_lines.append(
            f"{n} companies share their quarterly results today — including {', '.join(tickers[:5])}{', and others' if n > 5 else ''}. Expect bigger price swings around the open and after the market closes."
        )

    if snap.econ_events_today:
        evts = [e.description for e in snap.econ_events_today[:3] if e.description]
        if evts:
            adv_lines.append(f"Economic events to watch: {'; '.join(evts)}.")
            plain_lines.append(f"Government economic reports out today: {'; '.join(evts)}.")

    btc = next((m.quote for m in snap.crypto if m.quote.symbol.upper() == "BTC"), None)
    if btc and abs(btc.change_pct) > 2:
        adv_lines.append(f"Crypto: BTC {'+' if btc.change_pct >= 0 else ''}{btc.change_pct:.2f}% — monitor for crypto-equity spillover into COIN, MSTR, and related names.")
        word = "up" if btc.change_pct > 0 else "down"
        plain_lines.append(
            f"Bitcoin is {word} {abs(btc.change_pct):.2f}%. Big bitcoin moves often spill into stocks tied to crypto — like Coinbase (COIN) and MicroStrategy (MSTR)."
        )

    if not adv_lines:
        adv_lines.append("No major pre-market catalysts identified. Monitor the open for directional clues and watch for news flow around major sector movers.")
        plain_lines.append("No major news before the market opens. Watch the first hour to see which way things are heading, and keep an eye on news about the day's biggest movers.")

    return (
        '<div class="briefing-section setup">'
        '<div class="bs-label"><span class="std-only">When the market is open today</span><span class="adv-only">Coming Trading Day</span></div>'
        + prose_block_pair(" ".join(plain_lines), " ".join(adv_lines)) +
        '</div>'
    )


def _build_data_briefing(snap: Snapshot) -> str:
    """Build complete briefing expander content from snapshot data — no AI API needed."""
    exec_bullets = _b_exec_summary(snap)
    exec_html = ""
    if exec_bullets:
        lis = "".join(f"<li>{escape_html(b)}</li>" for b in exec_bullets)
        exec_html = (
            '<div class="exec-bar">'
            f'<div class="exec-label">{mode_pair("The Big Picture in 5 Bullets", "Market Summary")}</div>'
            f'<ol>{lis}</ol>'
            '</div>'
        )
    return (
        exec_html
        + _b_session_narrative(snap)
        + _b_us_markets(snap)
        + _b_global_markets(snap)
        + _b_crypto(snap)
        + _ticker_cards_html(_b_tickers_prediction(snap))
        + _b_coming_day(snap)
        + _b_setup(snap)
        + _b_risks(snap)
    )


# --------------------------------------------------------------------------
# Standalone analysis & outlook blocks (always visible, no AI required)
# --------------------------------------------------------------------------

_MACRO_KEYWORDS = {
    "tariff", "trade war", "trade deal", "sanction", "fed ", "federal reserve",
    "fomc", "rate hike", "rate cut", "interest rate", "inflation", "cpi", "pce",
    "gdp", "recession", "war", "conflict", "geopolit", "china", "russia",
    "ukraine", "iran", "opec", "oil supply", "oil price", "congress", "debt ceiling",
    "treasury", "powell", "yellen", "jobs report", "nonfarm", "unemployment",
    "election", "earnings miss", "guidance cut", "layoff", "bankruptcy",
}


def _build_session_text_plain(snap: Snapshot) -> str:
    """Plain-language session narrative for std view."""
    idx = {q.symbol: q for q in snap.indices}
    sp   = idx.get("^GSPC")
    dji  = idx.get("^DJI")
    ixic = idx.get("^IXIC")
    rut  = idx.get("^RUT")
    vix  = idx.get("^VIX")
    if not sp:
        return ""

    sentences: list[str] = []

    # Overall market tone — friendly summary
    if sp.change_pct > 1.5:
        tone = "had a strong day"
    elif sp.change_pct > 0.5:
        tone = "had a decent day"
    elif sp.change_pct > 0:
        tone = "had a mildly positive day"
    elif sp.change_pct > -0.5:
        tone = "had a mildly negative day"
    elif sp.change_pct > -1.5:
        tone = "had a rough day"
    else:
        tone = "had a really rough day"

    move_word = "rose" if sp.change_pct > 0 else "fell"
    example = abs(sp.change_pct) * 10
    sentence = (
        f"Stocks {tone} yesterday. The S&P 500 (the index that tracks 500 of the biggest U.S. companies) "
        f"{move_word} {abs(sp.change_pct):.2f}% to {sp.price:,.2f} — meaning a $1,000 investment would have "
        f"{'gained' if sp.change_pct > 0 else 'lost'} about ${example:.2f}."
    )
    pieces = []
    if dji:  pieces.append(f"the Dow Jones (30 large companies) {'rose' if dji.change_pct >= 0 else 'fell'} {abs(dji.change_pct):.2f}%")
    if ixic: pieces.append(f"the Nasdaq (more tech-heavy) {'rose' if ixic.change_pct >= 0 else 'fell'} {abs(ixic.change_pct):.2f}%")
    if rut:  pieces.append(f"the Russell 2000 (smaller companies) {'rose' if rut.change_pct >= 0 else 'fell'} {abs(rut.change_pct):.2f}%")
    if pieces:
        sentence += f" For comparison, {'; '.join(pieces)}."
    sentences.append(sentence)

    # VIX
    if vix:
        if vix.change_pct > 5:
            sentences.append(
                f"The 'fear gauge' (called the VIX) jumped {vix.change_pct:.1f}% to {vix.price:.2f}. "
                f"That means traders are nervous and paying more to protect their bets. "
                f"When this number goes above 20, it usually means people are worried about something bad happening soon."
            )
        elif vix.change_pct < -5:
            sentences.append(
                f"The 'fear gauge' (the VIX) dropped {abs(vix.change_pct):.1f}% to {vix.price:.2f}. "
                f"That means traders are more relaxed than yesterday — they're not paying as much to protect their bets. "
                f"That's typically a good sign for the stock market."
            )

    # Sector leadership
    if snap.sectors:
        leader = snap.sectors[0]
        lagger = snap.sectors[-1]
        if leader.name in ("Technology", "Consumer Discretionary", "Communication Services"):
            mood = "Investors are feeling optimistic and willing to take risks for bigger potential rewards."
        elif leader.name in ("Utilities", "Consumer Staples", "Real Estate"):
            mood = "Investors are playing it safe — buying boring, steady companies that pay reliable dividends."
        else:
            mood = "Specific company news is driving things, rather than a big shift in mood."
        sentences.append(
            f"The strongest part of the economy was {leader.name} (up {abs(leader.pct_1d):.2f}%), "
            f"while {lagger.name} stocks did the worst ({lagger.pct_1d:+.2f}%). {mood}"
        )

    return " ".join(sentences)


def _build_session_text(snap: Snapshot) -> str:
    """Multi-sentence session narrative built from index + sector + mover data."""
    idx = {q.symbol: q for q in snap.indices}
    sp   = idx.get("^GSPC")
    dji  = idx.get("^DJI")
    ixic = idx.get("^IXIC")
    rut  = idx.get("^RUT")
    vix  = idx.get("^VIX")
    if not sp:
        return ""

    sentences: list[str] = []

    # Overall market tone
    dir_  = "advanced" if sp.change_pct > 0 else "declined"
    adv   = "sharply" if abs(sp.change_pct) > 1.5 else ("modestly" if abs(sp.change_pct) > 0.5 else "marginally")
    s = f"Equities {adv} {dir_} in the prior session."
    pieces = []
    if sp:   pieces.append(f"S&P 500 {'gained' if sp.change_pct > 0 else 'lost'} {abs(sp.change_pct):.2f}% to {sp.price:,.2f}")
    if dji:  pieces.append(f"Dow {'+' if dji.change_pct >= 0 else ''}{dji.change_pct:.2f}%")
    if ixic: pieces.append(f"Nasdaq {'+'  if ixic.change_pct >= 0 else ''}{ixic.change_pct:.2f}%")
    if rut:  pieces.append(f"Russell 2000 {'+' if rut.change_pct >= 0 else ''}{rut.change_pct:.2f}%")
    if pieces:
        s += f" {', '.join(pieces)}."
    sentences.append(s)

    # VIX
    if vix:
        if vix.change_pct > 5:
            sentences.append(
                f"The VIX surged {vix.change_pct:.1f}% to {vix.price:.2f}, signaling elevated anxiety in options markets "
                f"and increased hedging demand — a level above 20 typically indicates investor unease about near-term tail risk."
            )
        elif vix.change_pct < -5:
            sentences.append(
                f"The VIX compressed {abs(vix.change_pct):.1f}% to {vix.price:.2f}, reflecting growing calm and reduced demand "
                f"for protective options — a constructive backdrop for risk assets."
            )

    # Sector leadership
    if snap.sectors:
        leader = snap.sectors[0]
        lagger = snap.sectors[-1]
        sentences.append(
            f"Sector leadership was concentrated in {leader.name} ({fmt_pct(leader.pct_1d)}), "
            f"while {lagger.name} lagged at {fmt_pct(lagger.pct_1d)}. "
            f"This rotation "
            + ("signals risk-on appetite and growth preference." if leader.name in ("Technology","Consumer Discretionary","Communication Services") else
               "reflects defensive positioning." if leader.name in ("Utilities","Consumer Staples","Real Estate") else
               "reflects sector-specific catalysts rather than a broad macro theme.")
        )

    return " ".join(sentences)


def _build_movers_reasoning(snap: Snapshot) -> str:
    """Paragraph explaining why the biggest gainers and losers moved, using news headlines."""
    parts: list[str] = []

    # Gainers
    gain_parts: list[str] = []
    for m in snap.gainers[:3]:
        q = m.quote
        headline = m.news[0].title if m.news else None
        s = f"{q.symbol} surged {q.change_pct:+.1f}% to {fmt_usd(q.price)}"
        s += f" — {headline}" if headline else " on heavy volume"
        gain_parts.append(s + ".")
    if gain_parts:
        parts.append("Top gainers: " + " ".join(gain_parts))

    # Losers
    loss_parts: list[str] = []
    for m in snap.losers[:3]:
        q = m.quote
        headline = m.news[0].title if m.news else None
        s = f"{q.symbol} fell {q.change_pct:.1f}% to {fmt_usd(q.price)}"
        s += f" — {headline}" if headline else " on elevated volume"
        loss_parts.append(s + ".")
    if loss_parts:
        parts.append("Key declines: " + " ".join(loss_parts))

    # Most active (where move > 2%)
    active_notable = [m for m in snap.most_active if abs(m.quote.change_pct) > 2][:2]
    if active_notable:
        act_parts = []
        for m in active_notable:
            q = m.quote
            headline = m.news[0].title if m.news else None
            s = f"{q.symbol} was among the most active ({fmt_usd(q.dollar_volume) if q.dollar_volume else 'high vol'}) " \
                f"with a {q.change_pct:+.1f}% move"
            s += f" — {headline}" if headline else ""
            act_parts.append(s + ".")
        parts.append(" ".join(act_parts))

    return "\n\n".join(parts)


def _build_movers_reasoning_plain(snap: Snapshot) -> str:
    """Plain-language version of mover reasoning for the std view."""
    parts: list[str] = []

    # Gainers
    gain_parts: list[str] = []
    for m in snap.gainers[:3]:
        q = m.quote
        headline = m.news[0].title if m.news else None
        s = f"{q.symbol}'s stock jumped {q.change_pct:+.1f}% to {fmt_usd(q.price)}"
        s += f" — the news: \"{headline}\"" if headline else " on heavy trading"
        gain_parts.append(s + ".")
    if gain_parts:
        parts.append("Biggest winners: " + " ".join(gain_parts))

    # Losers
    loss_parts: list[str] = []
    for m in snap.losers[:3]:
        q = m.quote
        headline = m.news[0].title if m.news else None
        s = f"{q.symbol}'s stock dropped {abs(q.change_pct):.1f}% to {fmt_usd(q.price)}"
        s += f" — the news: \"{headline}\"" if headline else " on heavy trading"
        loss_parts.append(s + ".")
    if loss_parts:
        parts.append("Biggest losers: " + " ".join(loss_parts))

    # Most active (where move > 2%)
    active_notable = [m for m in snap.most_active if abs(m.quote.change_pct) > 2][:2]
    if active_notable:
        act_parts = []
        for m in active_notable:
            q = m.quote
            headline = m.news[0].title if m.news else None
            vol = fmt_usd(q.dollar_volume) if q.dollar_volume else "a lot of money"
            direction = "up" if q.change_pct > 0 else "down"
            s = (f"{q.symbol} had heavy trading ({vol} changed hands) and moved {direction} "
                 f"{abs(q.change_pct):.1f}%")
            s += f" — the news: \"{headline}\"" if headline else ""
            act_parts.append(s + ".")
        parts.append("Stocks people were really paying attention to: " + " ".join(act_parts))

    return "\n\n".join(parts)


def _build_macro_world_text(snap: Snapshot) -> str:
    """Paragraph on macro moves and world news inferred from data and news headlines."""
    sentences: list[str] = []

    crude  = next((q for q in snap.macro if "Crude"   in q.name), None)
    gold   = next((q for q in snap.macro if "Gold"    in q.name), None)
    tnx    = next((q for q in snap.macro if "10Y"     in q.name), None)
    dxy    = next((q for q in snap.macro if "Dollar"  in q.name), None)
    silver = next((q for q in snap.macro if "Silver"  in q.name), None)
    ng     = next((q for q in snap.macro if "Natural" in q.name), None)

    if crude:
        if abs(crude.change_pct) > 3:
            context = ("suggesting significant geopolitical disruption, supply concerns, or OPEC action"
                       if crude.change_pct > 0 else
                       "reflecting demand concerns, a supply glut, or easing geopolitical risk")
            sentences.append(
                f"WTI crude oil moved sharply {crude.change_pct:+.2f}% to {fmt_usd(crude.price)}, {context}. "
                f"A move of this magnitude in oil typically has read-through effects on energy companies, "
                f"airlines, consumer staples, and the broader inflation narrative."
            )
        elif abs(crude.change_pct) > 1:
            sentences.append(f"Oil prices shifted {crude.change_pct:+.2f}% to {fmt_usd(crude.price)}.")

    if gold and abs(gold.change_pct) > 0.5:
        if gold.change_pct > 1.5:
            sentences.append(
                f"Gold rose {gold.change_pct:+.2f}% to {fmt_usd(gold.price)}, a classic safe-haven signal indicating "
                f"investors are seeking protection from geopolitical risk, inflation, or currency instability. "
                + ("The simultaneous crude surge reinforces a geopolitical read." if crude and crude.change_pct > 3 else "")
            )
        elif gold.change_pct < -1:
            sentences.append(
                f"Gold fell {gold.change_pct:.2f}% to {fmt_usd(gold.price)}, suggesting a risk-on shift "
                f"with capital rotating away from defensive stores of value."
            )
        else:
            sentences.append(f"Gold moved {gold.change_pct:+.2f}% to {fmt_usd(gold.price)}.")

    if tnx:
        if tnx.price > 4.5:
            sentences.append(
                f"The 10-year Treasury yield sits at {tnx.price:.2f}% — an elevated level that continues to compress "
                f"valuations on growth stocks, widen mortgage spreads, and squeeze bank net interest margins. "
                f"Any Fed commentary today on the rate path will be closely watched."
            )
        elif abs(tnx.change_pct) > 2:
            dir_ = "rose" if tnx.change_pct > 0 else "fell"
            implication = ("adding pressure to rate-sensitive equities such as REITs, utilities, and long-duration tech"
                           if tnx.change_pct > 0 else
                           "offering some relief to rate-sensitive names and growth stocks")
            sentences.append(f"The 10-year yield {dir_} {tnx.change_pct:+.2f}% to {tnx.price:.2f}%, {implication}.")

    if dxy and abs(dxy.change_pct) > 0.5:
        dir_ = "strengthened" if dxy.change_pct > 0 else "weakened"
        sentences.append(
            f"The US Dollar Index {dir_} {dxy.change_pct:+.2f}% to {dxy.price:.2f} — "
            + ("a stronger dollar puts pressure on multinationals' overseas earnings and commodities priced in USD."
               if dxy.change_pct > 0 else
               "a weaker dollar typically boosts commodity prices and benefits large-cap exporters.")
        )

    # Fed / FOMC events
    fed_evts = [e for e in snap.econ_events_today if any(
        kw in (e.description or "").upper()
        for kw in ["FOMC", "FEDERAL RESERVE", "POWELL", "RATE DECISION", "FED MEETING"]
    )]
    if fed_evts:
        sentences.append(
            f"The Federal Reserve is on today's calendar ({fed_evts[0].description}). "
            f"FOMC decisions and Powell press conferences are among the highest-volatility macro events — "
            f"watch for language on the pace of rate cuts, inflation tolerance, and any mentions of employment conditions."
        )

    # Global weakness / strength as geopolitical proxy
    if snap.global_indices:
        weak   = [q for q in snap.global_indices if q.change_pct < -1.5]
        strong = [q for q in snap.global_indices if q.change_pct >  1.5]
        if weak:
            names = ", ".join(q.name.split("(")[0].strip() for q in weak[:3])
            sentences.append(
                f"Notable weakness in global markets ({names}) may reflect risk-off sentiment, "
                f"regional geopolitical concerns, or spillover from currency moves. "
                f"Broad international declines often set a cautious tone for US pre-market futures."
            )
        elif strong:
            names = ", ".join(q.name.split("(")[0].strip() for q in strong[:2])
            sentences.append(
                f"International markets posted gains ({names}), supporting a constructive global backdrop "
                f"heading into the US session."
            )

    # World news extracted from mover headlines
    world_headlines: list[str] = []
    seen_keys: set[str] = set()
    for mover_list in [snap.gainers, snap.losers, snap.most_active]:
        for m in mover_list[:6]:
            for n in m.news:
                tl = n.title.lower()
                if any(kw in tl for kw in _MACRO_KEYWORDS):
                    key = tl[:45]
                    if key not in seen_keys:
                        seen_keys.add(key)
                        world_headlines.append(n.title)
    if world_headlines:
        sentences.append(
            "Market-relevant headlines: "
            + " | ".join(f'"{h}"' for h in world_headlines[:3])
            + "."
        )

    return "\n\n".join(sentences)


def _build_macro_world_text_plain(snap: Snapshot) -> str:
    """Plain-language version of macro/world commentary for the std view."""
    sentences: list[str] = []

    crude  = next((q for q in snap.macro if "Crude"   in q.name), None)
    gold   = next((q for q in snap.macro if "Gold"    in q.name), None)
    tnx    = next((q for q in snap.macro if "10Y"     in q.name), None)
    dxy    = next((q for q in snap.macro if "Dollar"  in q.name), None)

    if crude:
        if abs(crude.change_pct) > 3:
            why = ("possibly because of fighting somewhere in the world, supply disruptions, or oil-producing countries cutting production"
                   if crude.change_pct > 0 else
                   "possibly because of weaker demand, more oil being produced than needed, or fewer geopolitical worries")
            word = "jumped" if crude.change_pct > 0 else "dropped"
            sentences.append(
                f"Oil prices {word} sharply {abs(crude.change_pct):.2f}% to {fmt_usd(crude.price)} a barrel — {why}. "
                f"Big oil moves affect a lot of things: energy companies, airlines, what shoppers pay for everyday goods, and how worried people are about prices going up overall."
            )
        elif abs(crude.change_pct) > 1:
            word = "rose" if crude.change_pct > 0 else "fell"
            sentences.append(f"Oil prices {word} {abs(crude.change_pct):.2f}% to {fmt_usd(crude.price)} a barrel.")

    if gold and abs(gold.change_pct) > 0.5:
        if gold.change_pct > 1.5:
            extra = " The matching jump in oil prices suggests people are worried about something happening in the world." if crude and crude.change_pct > 3 else ""
            sentences.append(
                f"Gold rose {gold.change_pct:+.2f}% to {fmt_usd(gold.price)}. "
                f"Gold is what investors usually buy when they're worried — about war, rising prices, or the dollar losing value. "
                f"A move up means people are looking for a safer place to put their money.{extra}"
            )
        elif gold.change_pct < -1:
            sentences.append(
                f"Gold fell {abs(gold.change_pct):.2f}% to {fmt_usd(gold.price)}. "
                f"That usually means investors are feeling braver — they're moving money out of 'safe' bets like gold and into riskier things like stocks."
            )
        else:
            sentences.append(f"Gold moved {gold.change_pct:+.2f}% to {fmt_usd(gold.price)}.")

    if tnx:
        if tnx.price > 4.5:
            sentences.append(
                f"The 10-year Treasury rate (a key long-term U.S. interest rate) sits at {tnx.price:.2f}% — that's high. "
                f"When this rate is high, fast-growing companies (especially tech) are worth less to investors, mortgages get more expensive, and banks have a harder time making money. "
                f"Anything the Federal Reserve says today about future rates will be watched closely."
            )
        elif abs(tnx.change_pct) > 2:
            dir_ = "rose" if tnx.change_pct > 0 else "fell"
            implication = ("which puts pressure on stocks that are sensitive to interest rates, like real estate companies, utilities, and high-growth tech"
                           if tnx.change_pct > 0 else
                           "which gives a bit of relief to interest-rate-sensitive stocks and growth companies")
            sentences.append(f"The 10-year U.S. Treasury rate {dir_} to {tnx.price:.2f}%, {implication}.")

    if dxy and abs(dxy.change_pct) > 0.5:
        dir_ = "got stronger" if dxy.change_pct > 0 else "got weaker"
        explain = ("A stronger dollar makes American exports more expensive abroad and hurts U.S. companies that earn money overseas."
                   if dxy.change_pct > 0 else
                   "A weaker dollar usually pushes up the price of things like oil and gold, and helps big U.S. companies that sell abroad.")
        sentences.append(
            f"The U.S. dollar {dir_} compared to other currencies ({dxy.change_pct:+.2f}%). {explain}"
        )

    fed_evts = [e for e in snap.econ_events_today if any(
        kw in (e.description or "").upper()
        for kw in ["FOMC", "FEDERAL RESERVE", "POWELL", "RATE DECISION", "FED MEETING"]
    )]
    if fed_evts:
        sentences.append(
            f"The Federal Reserve (the U.S. central bank that sets interest rates) is making news today: {fed_evts[0].description}. "
            f"These meetings cause some of the biggest market swings — every word about future rate cuts, prices, or jobs gets analyzed."
        )

    if snap.global_indices:
        weak   = [q for q in snap.global_indices if q.change_pct < -1.5]
        strong = [q for q in snap.global_indices if q.change_pct >  1.5]
        if weak:
            names = ", ".join(q.name.split("(")[0].strip() for q in weak[:3])
            sentences.append(
                f"Stock markets in other countries had a rough day ({names}). "
                f"That can mean investors are nervous globally, there's regional trouble, or currency moves are hurting investors. "
                f"When other countries' markets fall hard, U.S. stocks often open lower too."
            )
        elif strong:
            names = ", ".join(q.name.split("(")[0].strip() for q in strong[:2])
            sentences.append(
                f"Stock markets in other countries had a strong day ({names}), which gives U.S. stocks a positive backdrop heading into trading."
            )

    world_headlines: list[str] = []
    seen_keys: set[str] = set()
    for mover_list in [snap.gainers, snap.losers, snap.most_active]:
        for m in mover_list[:6]:
            for n in m.news:
                tl = n.title.lower()
                if any(kw in tl for kw in _MACRO_KEYWORDS):
                    key = tl[:45]
                    if key not in seen_keys:
                        seen_keys.add(key)
                        world_headlines.append(n.title)
    if world_headlines:
        sentences.append(
            "Headlines that matter for markets: "
            + " | ".join(f'"{h}"' for h in world_headlines[:3])
            + "."
        )

    return "\n\n".join(sentences)


def _build_outlook_text(snap: Snapshot) -> str:
    """Data-driven predictions paragraph for the coming session (advanced view)."""
    parts: list[str] = []

    sp_fut = next((q for q in snap.premarket_us if "S&P" in q.name), None)
    if sp_fut:
        dir_  = "higher" if sp_fut.change_pct > 0.1 else ("lower" if sp_fut.change_pct < -0.1 else "flat")
        conf  = ("signaling early buying interest from institutional participants"
                 if sp_fut.change_pct > 0.4 else
                 "suggesting caution ahead of the open" if sp_fut.change_pct < -0.4 else
                 "offering no clear directional bias — watch the first 30 minutes for tone-setting")
        parts.append(f"S&P 500 futures point {dir_} pre-market ({sp_fut.change_pct:+.2f}%), {conf}.")

    if snap.earnings_today:
        mega = [e for e in snap.earnings_today
                if e.symbol_or_event in {"AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","JPM","NFLX","AMD"}]
        if mega:
            names = ", ".join(e.symbol_or_event for e in mega[:5])
            parts.append(
                f"Mega-cap earnings from {names} will dominate today's tape. "
                f"At the current market cap concentration, a collective miss or cautious guidance "
                f"from these names can gap the S&P 500 down 1–2% at the open; a beat could do the opposite. "
                f"Pay particular attention to forward guidance and AI capital expenditure commentary."
            )
        else:
            n = len(snap.earnings_today)
            tks = ", ".join(e.symbol_or_event for e in snap.earnings_today[:5] if e.symbol_or_event)
            suffix = f" (+{n-5} more)" if n > 5 else ""
            parts.append(
                f"{n} companies report today — notable names: {tks}{suffix}. "
                f"In the current market, guidance language and forward outlooks are moving stocks more "
                f"than headline EPS beats or misses."
            )

    # Sector momentum prediction
    if snap.sectors and len(snap.sectors) >= 2:
        leader = snap.sectors[0]
        lagger = snap.sectors[-1]
        if abs(leader.pct_1d) > 0.8:
            parts.append(
                f"Sector momentum favors {leader.name} to continue leading "
                f"(1D: {fmt_pct(leader.pct_1d)}, 1W: {fmt_pct(leader.pct_1w)}). "
                f"Watch for potential rotation out of {lagger.name} ({fmt_pct(lagger.pct_1d)}) "
                f"if risk sentiment improves intraday."
            )

    # Crypto read-through
    btc = next((m.quote for m in snap.crypto if m.quote.symbol.upper() == "BTC"), None)
    if btc:
        if btc.change_pct > 3:
            parts.append(
                f"Bitcoin is up {btc.change_pct:.1f}%, adding a tailwind for crypto-adjacent equities "
                f"(COIN, MSTR, MARA, RIOT) at today's open."
            )
        elif btc.change_pct < -3:
            parts.append(
                f"Bitcoin is down {abs(btc.change_pct):.1f}%, which typically carries headwinds for crypto equities. "
                f"Watch COIN and MARA for gap-down risk at the open."
            )

    # Key risk to watch
    vix = next((q for q in snap.indices if q.symbol == "^VIX"), None)
    if vix and vix.price > 20:
        parts.append(
            f"With VIX at {vix.price:.2f}, options markets are pricing above-average volatility — "
            f"keep position sizing in check and watch for intraday reversals."
        )

    return "\n\n".join(parts)


def _build_outlook_text_plain(snap: Snapshot) -> str:
    """Plain-language version of the data-driven outlook for the std view."""
    parts: list[str] = []

    sp_fut = next((q for q in snap.premarket_us if "S&P" in q.name), None)
    if sp_fut:
        word = "higher" if sp_fut.change_pct > 0.1 else ("lower" if sp_fut.change_pct < -0.1 else "about even")
        if sp_fut.change_pct > 0.4:
            why = "Big investors look like they're buying early — a positive sign for the open."
        elif sp_fut.change_pct < -0.4:
            why = "Traders look cautious before the market opens."
        else:
            why = "There's no clear direction yet — watch how the first 30 minutes go."
        parts.append(
            f"Before the U.S. market opens, early bets on the S&P 500 are pointing {word} ({sp_fut.change_pct:+.2f}%). {why}"
        )

    if snap.earnings_today:
        mega = [e for e in snap.earnings_today
                if e.symbol_or_event in {"AAPL","MSFT","GOOGL","GOOG","AMZN","META","NVDA","TSLA","JPM","NFLX","AMD"}]
        if mega:
            names = ", ".join(e.symbol_or_event for e in mega[:5])
            parts.append(
                f"Big-name companies — {names} — share their quarterly results today. "
                f"Because these companies are so huge, if they all disappoint, the whole market can drop 1-2% right at the open. If they do well, the opposite happens. "
                f"Pay close attention to what they say about future months — that often matters more than the current results."
            )
        else:
            n = len(snap.earnings_today)
            tks = ", ".join(e.symbol_or_event for e in snap.earnings_today[:5] if e.symbol_or_event)
            suffix = f", and {n-5} more" if n > 5 else ""
            parts.append(
                f"{n} companies share their quarterly results today — including {tks}{suffix}. "
                f"In today's market, what companies say about their future is moving stocks more than the actual numbers they just reported."
            )

    # Sector momentum prediction
    if snap.sectors and len(snap.sectors) >= 2:
        leader = snap.sectors[0]
        lagger = snap.sectors[-1]
        if abs(leader.pct_1d) > 0.8:
            parts.append(
                f"The {leader.name} part of the economy was the strongest yesterday "
                f"(up {abs(leader.pct_1d):.2f}% in one day, {abs(leader.pct_1w):.2f}% over the week) and will likely keep leading. "
                f"Meanwhile, {lagger.name} stocks lagged ({lagger.pct_1d:+.2f}%) — money may flow back there if traders feel braver later."
            )

    # Crypto read-through
    btc = next((m.quote for m in snap.crypto if m.quote.symbol.upper() == "BTC"), None)
    if btc:
        if btc.change_pct > 3:
            parts.append(
                f"Bitcoin is up {btc.change_pct:.1f}%. That's good news for stocks tied to crypto — like Coinbase (COIN), MicroStrategy (MSTR), and crypto miners like MARA and RIOT — when the market opens."
            )
        elif btc.change_pct < -3:
            parts.append(
                f"Bitcoin is down {abs(btc.change_pct):.1f}%. Stocks tied to crypto, like Coinbase (COIN) and MARA, may open lower as a result."
            )

    # Key risk to watch
    vix = next((q for q in snap.indices if q.symbol == "^VIX"), None)
    if vix and vix.price > 20:
        parts.append(
            f"The fear gauge (VIX) is high at {vix.price:.2f}. Traders are bracing for bigger price swings than normal — "
            f"so keep your bet sizes smaller and watch for sudden reversals during the day."
        )

    return "\n\n".join(parts)


def render_world_news_block(snap: Snapshot, briefing: dict | None = None) -> str:
    """
    Collapsible section: economically relevant world news with per-item market impact.
    Prefers AI-annotated items from briefing["world_news"]; falls back to raw headlines.
    """
    items: list[dict] = []

    if briefing and isinstance(briefing.get("world_news"), list):
        items = briefing["world_news"]

    # Data-driven fallback: use raw headlines with keyword-based direction
    if not items and snap.world_news_raw:
        _bull = {"rally", "surge", "jump", "rise", "gain", "beat", "strong", "record",
                 "growth", "approved", "deal", "ceasefire", "peace", "cut rates", "stimulus"}
        _bear = {"fall", "drop", "plunge", "decline", "miss", "weak", "recession", "tariff",
                 "sanction", "war", "conflict", "default", "layoff", "cut", "hawkish", "hike"}
        for n in snap.world_news_raw[:10]:
            low = n["headline"].lower()
            if any(w in low for w in _bull):
                direction = "bullish"
            elif any(w in low for w in _bear):
                direction = "bearish"
            else:
                direction = "mixed"
            items.append({
                "headline": n["headline"],
                "source": n["source"],
                "url": n.get("url", ""),
                "published": n.get("published", ""),
                "impact_summary": "",
                "affected_tickers": [],
                "affected_markets": [],
                "direction": direction,
            })

    if not items:
        return ""

    def _age(pub: str) -> str:
        if not pub:
            return ""
        try:
            from datetime import timezone as _tz
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            delta = datetime.now(_tz.utc) - dt.astimezone(_tz.utc)
            mins = int(delta.total_seconds() / 60)
            if mins < 60:
                return f"{mins}m ago"
            elif mins < 1440:
                return f"{mins // 60}h ago"
            else:
                return f"{mins // 1440}d ago"
        except Exception:
            return ""

    cards = []
    for item in items[:10]:
        direction = item.get("direction", "mixed")
        dir_class = {"bullish": "up", "bearish": "down"}.get(direction, "flat")
        dir_arrow = {"bullish": "↑", "bearish": "↓"}.get(direction, "↔")
        wn_class  = {"bullish": "wn-bullish", "bearish": "wn-bearish"}.get(direction, "wn-mixed")

        headline = escape_html(item.get("headline", ""))
        url = item.get("url", "")
        hl_html = (f'<a href="{escape_html(url)}" target="_blank" rel="noopener">{headline}</a>'
                   if url else headline)

        source = escape_html(item.get("source", ""))
        age    = _age(item.get("published", ""))
        meta   = " · ".join(filter(None, [source, age]))

        impact_adv   = item.get("impact_summary", "") or ""
        impact_plain = item.get("impact_summary_plain", "") or impact_adv
        if impact_adv or impact_plain:
            impact_html = (
                f'<div class="wn-impact">'
                f'<span class="std-only">{escape_html(impact_plain)}</span>'
                f'<span class="adv-only">{escape_html(impact_adv)}</span>'
                f'</div>'
            )
        else:
            impact_html = ""

        ticker_chips = "".join(
            f'<span class="wn-chip">{escape_html(t)}</span>'
            for t in item.get("affected_tickers", [])[:5]
        )
        market_chips = "".join(
            f'<span class="wn-chip market">{escape_html(m)}</span>'
            for m in item.get("affected_markets", [])[:4]
        )
        chips_html = (f'<div class="wn-chips">{ticker_chips}{market_chips}</div>'
                      if ticker_chips or market_chips else "")

        cards.append(
            f'<div class="wn-item {wn_class}">'
            f'  <div class="wn-dir {dir_class}">{dir_arrow}</div>'
            f'  <div class="wn-body">'
            f'    <div class="wn-headline">{hl_html}</div>'
            f'    <div class="wn-meta">{meta}</div>'
            f'    {impact_html}'
            f'    {chips_html}'
            f'  </div>'
            f'</div>'
        )

    grid = '<div class="wn-grid">' + "\n".join(cards) + '</div>'
    legend = (
        '<div class="wn-legend">'
        f'<span class="wn-legend-label">{mode_pair("Direction:", "Sentiment:")}</span>'
        f'<span class="wn-legend-item"><span class="wn-dir up">↑</span> {mode_pair("Good for stocks", "Bullish")}</span>'
        f'<span class="wn-legend-item"><span class="wn-dir down">↓</span> {mode_pair("Bad for stocks", "Bearish")}</span>'
        f'<span class="wn-legend-item"><span class="wn-dir flat">↔</span> {mode_pair("Could go either way", "Mixed / Neutral")}</span>'
        '</div>'
    )
    return (
        '<details class="world-news-details" id="world-news">'
        f'<summary>{mode_pair("World News &amp; What It Means for Markets", "Global News &amp; Market Impact")}'
        '<span class="expand-hint"> — click to expand</span>'
        '</summary>'
        + legend + grid +
        '</details>'
    )


def render_analysis_block(snap: Snapshot, briefing: dict | None = None) -> str:
    """
    Always-visible section with 3 narrative cards:
    1. Session recap (AI text if available, otherwise data-driven)
    2. Key movers + reasoning from news headlines
    3. Macro, world news & geopolitical context
    """
    cards: list[tuple[str, str, str]] = []  # (title_pair_html, text_plain, text_adv)

    # Card 1 — Session recap
    session_text       = (briefing or {}).get("session_recap")       or _build_session_text(snap)
    session_text_plain = (briefing or {}).get("session_recap_plain") or _build_session_text_plain(snap) or session_text
    if session_text:
        title = mode_pair_text(
            "Yesterday's Market — What Happened, Explained Simply",
            "Yesterday's Session — What Happened & Why",
        )
        cards.append((title, session_text_plain, session_text))

    # Card 2 — Mover reasoning (always data-driven for freshness)
    movers_text       = _build_movers_reasoning(snap)
    movers_text_plain = _build_movers_reasoning_plain(snap) or movers_text
    if movers_text:
        title = mode_pair_text(
            "Why Certain Stocks Moved — The Stories Behind the Numbers",
            "Key Movers — Gainers, Losers & the Reasons Behind the Moves",
        )
        cards.append((title, movers_text_plain, movers_text))

    # Card 3 — Macro & world news
    macro_text       = _build_macro_world_text(snap)
    macro_text_plain = _build_macro_world_text_plain(snap) or macro_text
    if macro_text:
        title = mode_pair_text(
            "World Events That Are Affecting the Market",
            "Macro & Global Context — World Events Affecting Markets",
        )
        cards.append((title, macro_text_plain, macro_text))

    if not cards:
        return ""

    html = '<h2 id="analysis"><span class="std-only">What\'s Happening in the Market</span><span class="adv-only">Market Analysis</span></h2>'
    for title, text_plain, text_adv in cards:
        body = prose_block_pair(text_plain, text_adv)
        html += f'<div class="narr"><div class="label">{title}</div>{body}</div>'
    return html


def render_outlook_block(snap: Snapshot, briefing: dict | None = None) -> str:
    """
    Always-visible predictions section: today's setup + directional predictions.
    Uses AI today_setup text when available; falls back to data-driven.
    """
    # Main outlook text
    outlook_text       = (briefing or {}).get("today_setup")       or _build_outlook_text(snap)
    outlook_text_plain = (briefing or {}).get("today_setup_plain") or _build_outlook_text_plain(snap) or outlook_text

    # Risk notes (AI if available, otherwise data-driven)
    risk_items       = (briefing or {}).get("risk_notes", [])
    risk_items_plain = (briefing or {}).get("risk_notes_plain", []) or risk_items

    def _to_text(items) -> str:
        if isinstance(items, list):
            return "\n\n".join(items)
        return str(items) if items else ""

    risk_text       = _to_text(risk_items)
    risk_text_plain = _to_text(risk_items_plain) or risk_text

    html = ""
    if outlook_text:
        label = mode_pair(
            "What Today Could Bring — Our Best Guesses",
            "Today's Outlook &amp; Predictions",
        )
        body = prose_block_pair(outlook_text_plain, outlook_text)
        html += f'<div class="narr"><div class="label">{label}</div>{body}</div>'

    if risk_text:
        label = mode_pair(
            "Things That Could Go Wrong",
            "Key Risks to Watch",
        )
        body = prose_block_pair(risk_text_plain, risk_text)
        html += f'<div class="narr risk"><div class="label">{label}</div>{body}</div>'

    return html


# --------------------------------------------------------------------------

def _what_to_watch_html(snap: Snapshot | None, briefing: dict | None) -> str:
    """Build a 3-bullet 'What to Watch Today' checklist from snapshot + briefing data."""
    items_adv:   list[str] = []
    items_plain: list[str] = []

    # 1) Top earnings reporters today (by market cap)
    if snap and snap.earnings_today:
        big = sorted(snap.earnings_today, key=lambda e: e.market_cap, reverse=True)
        names = [e.symbol_or_event for e in big[:3] if e.symbol_or_event]
        if names:
            items_adv.append(f"Earnings on deck: {', '.join(names)}")
            items_plain.append(f"Quarterly results out today: {', '.join(names)}")

    # 2) Biggest pre-market mover (futures or single-name pre-market)
    if snap and snap.premarket_us:
        biggest = max(snap.premarket_us, key=lambda q: abs(q.change_pct or 0.0), default=None)
        if biggest and abs(biggest.change_pct or 0.0) >= 0.05:
            sign = "+" if (biggest.change_pct or 0.0) >= 0 else ""
            items_adv.append(f"Pre-market: {biggest.name} {sign}{biggest.change_pct:.2f}%")
            items_plain.append(f"Before the market opens: {biggest.name} is {sign}{biggest.change_pct:.2f}%")

    # 3) Risk note from AI briefing, or first economic event of the day
    if briefing:
        risks       = briefing.get("risk_notes")
        risks_plain = briefing.get("risk_notes_plain") or risks
        if isinstance(risks, list) and risks:
            first = str(risks[0]).split(".")[0].strip()
            if first:
                items_adv.append(f"Watch: {first[:140]}")
        if isinstance(risks_plain, list) and risks_plain:
            first = str(risks_plain[0]).split(".")[0].strip()
            if first:
                items_plain.append(f"Watch: {first[:140]}")
    if len(items_adv) < 3 and snap and snap.econ_events_today:
        ev = snap.econ_events_today[0]
        items_adv.append(f"Macro: {ev.description} ({ev.time or 'today'})")
        items_plain.append(f"Economic report: {ev.description} ({ev.time or 'today'})")

    if not items_adv:
        return ""
    # Pad plain to match adv length
    while len(items_plain) < len(items_adv):
        items_plain.append(items_adv[len(items_plain)])
    bullets_adv = "".join(f"<li>{escape_html(b)}</li>" for b in items_adv[:3])
    bullets_std = "".join(f"<li>{escape_html(b)}</li>" for b in items_plain[:3])
    return (
        '<div class="b-watchlist">'
        f'<div class="bw-label">{mode_pair("Things to Watch Today", "What to Watch Today")}</div>'
        f'<ul class="bw-list std-only">{bullets_std}</ul>'
        f'<ul class="bw-list adv-only">{bullets_adv}</ul>'
        '</div>'
    )


def render_briefing_block(briefing: dict | None, snap: Snapshot | None = None,
                          analysis_html: str = "") -> str:
    """Render the Morning Briefing as an inline card at the top of the page.

    Uses AI-generated JSON if available; otherwise builds from snapshot data.
    Executive summary + index chips always visible; full text in a <details> expander.
    """
    if not briefing and not snap:
        return ""

    gen_date = datetime.now(ET).strftime("%b %-d, %Y · %-I:%M %p %Z")
    watch_html = _what_to_watch_html(snap, briefing)

    # Index chips row — always visible
    index_row_html = ""
    if snap and snap.indices:
        chips = "".join(
            _index_chip(q.name.split("(")[0].strip(), q.change_pct, q.price)
            for q in snap.indices
        )
        index_row_html = f'<div class="b-index-row" style="padding:12px 20px 4px">{chips}</div>'

    # Executive summary — always visible
    exec_html = ""
    if briefing and briefing.get("exec_summary"):
        lis = "".join(f"<li>{escape_html(b)}</li>" for b in briefing["exec_summary"])
        exec_html = (
            '<div class="exec-bar">'
            f'<div class="exec-label">{mode_pair("The Big Picture in 5 Bullets", "Executive Summary")}</div>'
            f'<ol>{lis}</ol></div>'
        )
    elif snap:
        bullets = _b_exec_summary(snap)
        if bullets:
            lis = "".join(f"<li>{escape_html(b)}</li>" for b in bullets)
            exec_html = (
                '<div class="exec-bar">'
                f'<div class="exec-label">{mode_pair("The Big Picture in 5 Bullets", "Market Summary")}</div>'
                f'<ol>{lis}</ol></div>'
            )

    # Detailed sections — inside <details> expander
    if briefing:
        source = "Claude AI"
        session_text       = briefing.get("session_recap", "")
        session_text_plain = briefing.get("session_recap_plain", "") or session_text
        session_html = (
            '<div class="briefing-section">'
            '<div class="bs-label"><span class="std-only">What happened in the market yesterday</span><span class="adv-only">Yesterday\'s Session</span></div>'
            + prose_block_pair(session_text_plain, session_text) +
            '</div>'
        ) if session_text else ""

        crypto_recap       = briefing.get("crypto_recap", "")
        crypto_recap_plain = briefing.get("crypto_recap_plain", "") or crypto_recap
        crypto_recap_html = (
            '<div class="briefing-section crypto">'
            '<div class="bs-label"><span class="std-only">What happened with cryptocurrency</span><span class="adv-only">Crypto Recap</span></div>'
            + prose_block_pair(crypto_recap_plain, crypto_recap) +
            '</div>'
        ) if crypto_recap else ""

        setup_text       = briefing.get("today_setup", "")
        setup_text_plain = briefing.get("today_setup_plain", "") or setup_text
        setup_html = (
            '<div class="briefing-section setup">'
            '<div class="bs-label"><span class="std-only">What to watch for today</span><span class="adv-only">Today\'s Setup</span></div>'
            + prose_block_pair(setup_text_plain, setup_text) +
            '</div>'
        ) if setup_text else ""

        watch = briefing.get("tickers_to_watch", [])
        watch_html = ""
        if watch:
            card_parts = []
            for w in watch:
                ticker = str(w.get("ticker", "")).strip()
                if not ticker:
                    continue
                rationale_adv   = str(w.get("rationale", "")).strip()
                rationale_plain = (str(w.get("rationale_plain", "")).strip()
                                   or rationale_adv)
                if not rationale_adv and not rationale_plain:
                    log(f"  briefing watch item '{ticker}' has no rationale; "
                        f"dropping from cards.")
                    continue
                card_parts.append(
                    f'<div class="b-watch-item">'
                    f'<div class="sym">{escape_html(ticker)}</div>'
                    f'<div class="why">'
                    f'<span class="std-only">{escape_html(rationale_plain)}</span>'
                    f'<span class="adv-only">{escape_html(rationale_adv)}</span>'
                    f'</div>'
                    f'</div>'
                )
            if card_parts:
                watch_html = (
                    '<div class="briefing-watch">'
                    '<div class="bs-label"><span class="std-only">Stocks we\'re watching today</span><span class="adv-only">Tickers to Watch Today</span></div>'
                    f'<div class="b-watch-grid">{"".join(card_parts)}</div>'
                    '</div>'
                )

        crypto_out       = briefing.get("crypto_outlook", "")
        crypto_out_plain = briefing.get("crypto_outlook_plain", "") or crypto_out
        crypto_out_html = (
            '<div class="briefing-section crypto">'
            '<div class="bs-label"><span class="std-only">What to watch for in crypto</span><span class="adv-only">Crypto Outlook</span></div>'
            + prose_block_pair(crypto_out_plain, crypto_out) +
            '</div>'
        ) if crypto_out else ""

        risk_items       = briefing.get("risk_notes", [])
        risk_items_plain = briefing.get("risk_notes_plain", []) or risk_items
        risk_html = ""
        if risk_items:
            if isinstance(risk_items, list):
                lis_adv = "".join(f"<li>{escape_html(r)}</li>" for r in risk_items)
                if isinstance(risk_items_plain, list) and risk_items_plain:
                    lis_std = "".join(f"<li>{escape_html(r)}</li>" for r in risk_items_plain)
                else:
                    lis_std = lis_adv
                risk_html = (
                    '<div class="briefing-section risk">'
                    '<div class="bs-label"><span class="std-only">What could cause problems today</span><span class="adv-only">Risk Notes</span></div>'
                    f'<ul class="std-only">{lis_std}</ul>'
                    f'<ul class="adv-only">{lis_adv}</ul></div>'
                )
            else:
                plain_text = str(risk_items_plain) if risk_items_plain else str(risk_items)
                risk_html = (
                    '<div class="briefing-section risk">'
                    '<div class="bs-label"><span class="std-only">What could cause problems today</span><span class="adv-only">Risk Notes</span></div>'
                    + prose_block_pair(plain_text, str(risk_items)) +
                    '</div>'
                )

        global_html = _b_global_markets(snap) if snap else ""
        detailed_html = session_html + crypto_recap_html + global_html + setup_html + watch_html + crypto_out_html + risk_html
    else:
        source = "Live Data"
        detailed_html = (
            _b_us_markets(snap) + _b_global_markets(snap) +
            _b_crypto(snap) + _b_setup(snap) + _b_risks(snap)
        )

    # Wrap the analysis block (if any) so it lives inside the briefing card.
    # We pull it out of the inner <h2> shell and just include the .narr cards.
    analysis_inner = ""
    if analysis_html:
        # Drop the leading <h2>...</h2> from render_analysis_block — replace with our own label
        import re as _re
        body_only = _re.sub(r'^<h2[^>]*>.*?</h2>', '', analysis_html, count=1)
        analysis_inner = (
            '<div class="briefing-section">'
            '<div class="bs-label">'
            f'{mode_pair("What\'s Happening in the Market", "Market Analysis")}'
            '</div>'
            f'{body_only}'
            '</div>'
        )

    details_block = ""
    if detailed_html.strip() or analysis_inner:
        details_block = (
            f'<details class="briefing-details" open>'
            f'<summary>{mode_pair("See the Full Summary", "Full Briefing")}</summary>'
            f'{analysis_inner}'
            f'{detailed_html}'
            f'</details>'
        )

    # Order: lead with what to watch + the prose summary, push the dense
    # index/exec numbers below. The full briefing (with the analysis cards)
    # comes last and is expanded by default.
    return (
        f'<div class="briefing-inline" id="briefing">'
        f'<div class="briefing-inline-head">'
        f'<span class="bi-title">{mode_pair("Today\'s Market Summary", "Morning Briefing")}</span>'
        f'<span class="bi-source">{source} · {gen_date}</span>'
        f'</div>'
        f'{watch_html}'
        f'{exec_html}'
        f'{index_row_html}'
        f'{details_block}'
        f'</div>'
    )


def _bell_countdown(now_et: datetime) -> str:
    """Return human string: 'Bell in Xh Ym', 'Market open', or 'After hours'."""
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et < open_t:
        delta = open_t - now_et
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"Bell in {h}h {m}m" if h else f"Bell in {m}m"
    if now_et < close_t:
        return "Market open"
    return "After hours"


def _pm_tile(q: Quote) -> str:
    cls = cls_for(q.change_pct)
    val = fmt_num(q.price) if q.price and q.price >= 10 else fmt_usd(q.price)
    return (
        f'<div class="tile compact">'
        f'<div class="label">{escape_html(q.name)}</div>'
        f'<div class="value num">{val}</div>'
        f'<div class="delta num {cls}">{fmt_pct(q.change_pct)}</div>'
        f'</div>'
    )


def render_premarket_strips(snap: Snapshot) -> str:
    """Render the pre-market futures + overnight global strips."""
    if not (snap.premarket_us or snap.overnight_global):
        return ""
    now_et = datetime.now(ET)
    bell = _bell_countdown(now_et)
    ts = (datetime.fromisoformat(snap.premarket_fetched_at).strftime("%I:%M %p ET")
          if snap.premarket_fetched_at else "—")

    def group(std_label: str, adv_label: str, quotes: list[Quote]) -> str:
        if not quotes:
            return ""
        return (f'<div class="pm-section-label">{mode_pair(std_label, adv_label)}</div>'
                f'<div class="pm-grid">{"".join(_pm_tile(q) for q in quotes)}</div>')

    strip_a = (
        f'<div class="premarket-bar" id="premarket">'
        f'<h2>{mode_pair("Before the Market Opens", "Pre-Market")}'
        f'<span style="font-weight:400;text-transform:none;font-size:11px;color:var(--text-faint);margin-left:10px">'
        f'{ts} · {bell}</span></h2>'
        + group("Early bets on U.S. stock indexes", "US Futures",     snap.premarket_us)
        + group("Oil, gold, interest rates",         "Macro",          snap.premarket_macro)
        + group("Cryptocurrency",                    "Crypto",         snap.premarket_crypto)
        + '</div>'
    )
    return strip_a


def render_data_tickers_block(snap: Snapshot) -> str:
    """Standalone tickers-to-watch section for the main page (data-driven, no AI needed)."""
    picks = _b_tickers_prediction(snap)
    if not picks:
        return ""
    cards_html = _ticker_cards_html(picks)
    coming = _b_coming_day(snap)
    return (
        '<h2><span class="std-only">Stocks We Think Are Worth Watching</span><span class="adv-only">Tickers to Watch &amp; Predictions</span></h2>'
        + coming
        + cards_html
    )


def render_global_block(snap: Snapshot) -> str:
    """Render global equity indices as a compact tile grid for the Global Markets section.

    Prefers overnight_global (fetched at pre-market time, most recent) and falls back
    to global_indices (prior-session close).
    """
    quotes = snap.overnight_global or snap.global_indices
    if not quotes:
        return '<p class="meta" style="color:var(--text-faint);padding:8px 0">No global market data available.</p>'
    ts = ""
    if snap.premarket_fetched_at:
        ts = datetime.fromisoformat(snap.premarket_fetched_at).strftime("%-I:%M %p ET")
    sub = f'<div class="pm-section-label" style="margin-top:0">Asia &amp; Europe' + (f' · {ts}' if ts else '') + '</div>'
    tiles = "".join(_pm_tile(q) for q in quotes)
    return f'{sub}<div class="pm-grid">{tiles}</div>'


def render_report(snap: Snapshot, briefing: dict | None = None,
                  eod: bool = False, history: dict | None = None) -> str:
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

    gen_dt_et = datetime.fromisoformat(snap.generated_at).astimezone(ET)
    last_updated = gen_dt_et.strftime("%-I:%M %p ET")

    # Build the autocomplete ticker DB: curated mega-caps first (so they
    # surface for partial queries), then every NASDAQ + NYSE-listed security,
    # then any snapshot symbols not already covered. Insertion order is
    # preserved into the JS, so the JS pickup-by-prefix returns the largest
    # companies first.
    ticker_pairs: dict[str, str] = dict(POPULAR_TICKERS)
    for sym, name in load_all_tickers():
        if sym and name and sym not in ticker_pairs:
            ticker_pairs[sym] = name
    for src in (snap.gainers, snap.losers, snap.most_active, snap.watchlist_news):
        for m in src or []:
            q = m.quote if hasattr(m, "quote") else m
            if q.symbol and q.name and q.symbol not in ticker_pairs:
                ticker_pairs[q.symbol] = q.name
    for q in snap.watchlist or []:
        if q.symbol and q.name and q.symbol not in ticker_pairs:
            ticker_pairs[q.symbol] = q.name
    # Seed crypto so autocomplete works without a remote-search round-trip
    crypto_overrides: dict[str, str] = {}
    for sym, name in CRYPTO_TICKERS.items():
        if sym not in ticker_pairs:
            ticker_pairs[sym] = name
        crypto_overrides[sym] = "Crypto"
    for m in snap.crypto or []:
        q = m.quote
        if q.symbol and q.name and q.symbol not in ticker_pairs:
            ticker_pairs[q.symbol] = q.name
        if q.symbol:
            crypto_overrides[q.symbol] = "Crypto"
    ticker_db_json = json.dumps(
        [[s, n, crypto_overrides.get(s) or _classify_ticker(s, n)] for s, n in ticker_pairs.items()],
        ensure_ascii=False,
    )

    build_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = _jinja_env.get_template("base.html").render(
        build_id=build_id,
        ticker_db_json=ticker_db_json,
        prior_date=prior_date,
        prior_date_human=prior_dt.strftime("%A, %B %-d, %Y"),
        generated_human=gen_dt_et.strftime("%Y-%m-%d %H:%M %Z"),
        last_updated=last_updated,
        today_human=today_dt.strftime("%A, %B %-d, %Y"),
        warnings_html=warnings_html,
        index_tiles=index_tiles,
        briefing_block=render_briefing_block(briefing, snap, analysis_html=render_analysis_block(snap, briefing)),
        premarket_block=render_premarket_strips(snap),
        watchlist_block=render_watchlist(snap),
        sector_heatmap_block=render_sector_heatmap(snap),
        sentiment_block=render_sentiment_strip(snap),
        scorecard_block=render_scorecard(snap, briefing, eod=eod, history=history),
        earnings_reactions_block=render_earnings_reactions(snap),
        gainers_rows=render_movers_block(snap.gainers, why_g, "No gainer data."),
        losers_rows=render_movers_block(snap.losers, why_l, "No loser data."),
        active_rows=render_movers_block(snap.most_active, why_a, "No active data."),
        crypto_rows=render_movers_block(crypto_list, why_c, "No crypto data."),
        crypto_top_n=CRYPTO_TOP_N,
        outlook_block=render_outlook_block(snap, briefing),
        earnings_section_block=render_earnings_section(snap),
        crypto_outlook_block=render_crypto_outlook(ai),
        risk_block=render_risk_block(ai),
        global_block=render_global_block(snap),
        world_news_block=render_world_news_block(snap, briefing),
        sidebar_block=render_sidebar_block(snap),
    )
    return html


# ------------------------------------------------------------------------
# Sector heatmap
# ------------------------------------------------------------------------
def fetch_sectors() -> list[SectorPerf]:
    """Fetch 1D/1W/YTD % change for all 11 SPDR sector ETFs."""
    out: list[SectorPerf] = []
    for sym, name in SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(sym).history(period="ytd", interval="1d", auto_adjust=False)
            if hist is None or hist.empty or len(hist) < 2:
                continue
            close = hist["Close"].dropna()
            if len(close) < 2:
                continue
            last  = float(close.iloc[-1])
            prev1 = float(close.iloc[-2])
            prev5 = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
            first = float(close.iloc[0])
            def _p(a: float, b: float) -> float:
                return (a - b) / b * 100.0 if b else 0.0
            out.append(SectorPerf(symbol=sym, name=name,
                                  pct_1d=_p(last, prev1),
                                  pct_1w=_p(last, prev5),
                                  pct_ytd=_p(last, first)))
        except Exception as e:
            log(f"  sector {sym} failed: {e}")
    out.sort(key=lambda s: s.pct_1d, reverse=True)
    return out


def render_sector_heatmap(snap: Snapshot) -> str:
    """Render sector heatmap with horizontal bar chart + detail card grid."""
    if not snap.sectors:
        return ""
    sectors_sorted = sorted(snap.sectors, key=lambda s: s.pct_1d, reverse=True)
    max_abs = max((abs(s.pct_1d) for s in sectors_sorted), default=0.5) or 0.5

    bar_rows = []
    for s in sectors_sorted:
        cls = cls_for(s.pct_1d)
        width_pct = min(100.0, abs(s.pct_1d) / max_abs * 50.0)
        side = "right" if s.pct_1d >= 0 else "left"
        bar_rows.append(
            f'<div class="sb-row">'
            f'<div class="sb-name">{escape_html(s.name)}</div>'
            f'<div class="sb-track" role="img" aria-label="{escape_html(s.name)} 1-day {fmt_pct(s.pct_1d)}">'
            f'<div class="sb-axis"></div>'
            f'<div class="sb-fill sb-{side} {cls}" style="width:{width_pct:.2f}%"></div>'
            f'</div>'
            f'<div class="sb-pct num {cls}">{fmt_pct(s.pct_1d)}</div>'
            f'</div>'
        )
    bars_html = (
        '<div class="sector-bars">'
        '<div class="sb-caption">1-day performance · scaled to range</div>'
        + "".join(bar_rows) +
        '</div>'
    )

    card_tpl = _jinja_env.get_template("_sector_tile.html")
    cards = []
    for s in sectors_sorted:
        cards.append(card_tpl.render(
            name=escape_html(s.name),
            cls=cls_for(s.pct_1d),
            pct_1d=fmt_pct(s.pct_1d),
            cls_1w=cls_for(s.pct_1w),
            pct_1w=fmt_pct(s.pct_1w),
            cls_ytd=cls_for(s.pct_ytd),
            pct_ytd=fmt_pct(s.pct_ytd),
        ))
    return (
        f'<h2 id="sectors"><span class="std-only">How Each Part of the Economy Is Doing</span><span class="adv-only">Sector Performance</span></h2>'
        f'{bars_html}'
        f'<div class="sector-grid">{"".join(cards)}</div>'
    )


# ============================================================================
# SCORECARD — daily prediction grading, multi-day history, self-calibration
# ============================================================================
# Flow:
#   morning briefing  →  briefing-{date}.json (predictions for the session)
#   end of session    →  grade those predictions vs that day's close
#   persisted to      →  scorecard_history.json (durable across runs)
#   render            →  recent N days + rolling calibration metrics
# ----------------------------------------------------------------------------

_BULL_KW = {"beat","momentum","upside","breakout","continuation","rally",
            "strength","oversold","recovery","rebound","acceleration"}
_BEAR_KW = {"miss","cut","downside","breakdown","weakness","overbought",
            "slump","pressure","decline","guidance cut"}
_GRADE_PTS = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}


def _prior_trading_day_before(date_str: str) -> str:
    """Return the trading day immediately before the given ISO date."""
    d = datetime.fromisoformat(date_str).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def _next_trading_day_at_or_after(date_str: str) -> str:
    """Return the next trading day at-or-after the given ISO date (weekend-aware)."""
    d = datetime.fromisoformat(date_str).date()
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def _infer_bias(rationale: str) -> str:
    low = (rationale or "").lower()
    b = sum(1 for w in _BULL_KW if w in low)
    r = sum(1 for w in _BEAR_KW if w in low)
    return "bullish" if b > r else "bearish" if r > b else "neutral"


def _gpa_letter(g: float | None) -> str:
    if g is None: return "—"
    if g >= 3.5:  return "A"
    if g >= 2.5:  return "B"
    if g >= 1.5:  return "C"
    if g >= 0.5:  return "D"
    return "F"


def _grade_prediction(bias: str, pct: float | None) -> tuple[str, str, str, str]:
    """Return (verdict, letter_grade, reason_standard, reason_advanced)."""
    if pct is None:
        return ("N/A", "—",
                "We couldn't check this stock — no price was available.",
                "No price data available — ticker may be delisted or untracked.")
    p = float(pct); a = abs(p); sign = "+" if p >= 0 else ""

    if bias == "neutral":
        if a >= 3.0:
            return ("HIT", "A",
                    f"The stock moved a lot today ({sign}{p:.2f}%). It was a good one to watch.",
                    f"Watchlist call vindicated: {sign}{p:.2f}% — high realized volatility.")
        if a >= 1.5:
            return ("HIT", "B",
                    f"The stock moved a fair amount ({sign}{p:.2f}%). Worth watching.",
                    f"Notable mover: {sign}{p:.2f}% — meaningful tape action validated the watch.")
        if a >= 0.5:
            return ("FLAT", "C",
                    f"The stock moved a little ({sign}{p:.2f}%). Less than we hoped.",
                    f"Modest mover: {sign}{p:.2f}% — within ordinary range, soft signal.")
        if a >= 0.2:
            return ("FLAT", "D",
                    f"The stock barely moved ({sign}{p:.2f}%).",
                    f"Quiet day: {sign}{p:.2f}% — minimal movement despite watchlist flag.")
        return     ("FLAT", "F",
                    f"The stock barely moved ({sign}{p:.2f}%). Watching it didn't help.",
                    f"Flat tape: {sign}{p:.2f}% — watchlist call added no signal.")

    # directional bias: signed_p > 0 means the call was right
    signed_p = p if bias == "bullish" else -p
    label_adv = bias.title()
    expected_word = "go up" if bias == "bullish" else "go down"
    actual_dir = "up" if p >= 0 else "down"
    verdict = "FLAT" if a < 1.0 else ("HIT" if signed_p >= 1.0 else "MISS")

    if signed_p >= 3.0:
        return (verdict, "A",
                f"We said this stock would {expected_word}, and it went {actual_dir} a lot ({sign}{p:.2f}%). Great call.",
                f"{label_adv} thesis confirmed with conviction: {sign}{p:.2f}% — strong follow-through.")
    if signed_p >= 1.5:
        return (verdict, "B",
                f"We said this stock would {expected_word}, and it did ({sign}{p:.2f}%). Good call.",
                f"{label_adv} thesis worked: {sign}{p:.2f}% — direction right, solid magnitude.")
    if signed_p >= 0:
        return (verdict, "C",
                f"We said this stock would {expected_word}. It barely moved the right way ({sign}{p:.2f}%). Right idea, but weak.",
                f"{label_adv} call closed mildly correct: {sign}{p:.2f}% — direction right, conviction lacking.")
    if signed_p > -1.5:
        return (verdict, "D",
                f"We said this stock would {expected_word}, but it went the other way a little ({sign}{p:.2f}%). Wrong, but only by a little.",
                f"{label_adv} thesis underwhelmed: {sign}{p:.2f}% — wrong direction, contained.")
    return     (verdict, "F",
                f"We said this stock would {expected_word}, but it went the other way a lot ({sign}{p:.2f}%). Bad call.",
                f"{label_adv} thesis broke: {sign}{p:.2f}% — moved sharply against the call.")


def _entry_from_pred(ticker: str, rationale: str, pct: float | None) -> ScorecardEntry:
    bias = _infer_bias(rationale)
    verdict, letter, reason_std, reason_adv = _grade_prediction(bias, pct)
    return ScorecardEntry(
        ticker=ticker, rationale=rationale, bias=bias,
        actual_pct=pct, verdict=verdict,
        letter_grade=letter,
        grade_reason=reason_adv,  # legacy field retained for back-compat
        grade_reason_standard=reason_std,
        grade_reason_advanced=reason_adv,
    )


def fetch_eod_change_pct(ticker: str, date_iso: str) -> float | None:
    """Day-over-day % close change for `ticker` on the given trading date.

    Used for backfilling historical grades. Returns None on any failure.
    """
    try:
        d = datetime.fromisoformat(date_iso).date()
        start = (d - timedelta(days=10)).isoformat()
        end   = (d + timedelta(days=1)).isoformat()
        h = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if h is None or h.empty:
            return None
        closes = [(idx.date().isoformat(), float(h.loc[idx, "Close"]))
                  for idx in h.index if not pd.isna(h.loc[idx, "Close"])]
        target_idx = next((i for i, (di, _) in enumerate(closes) if di == date_iso), None)
        if target_idx is None or target_idx == 0:
            return None
        prior_close = closes[target_idx - 1][1]
        target_close = closes[target_idx][1]
        if prior_close <= 0:
            return None
        return (target_close - prior_close) / prior_close * 100.0
    except Exception:
        return None


def score_predictions(prior_briefing: dict, snap: Snapshot) -> list[ScorecardEntry]:
    """Grade a briefing's tickers_to_watch using prices already in the snapshot.

    Falls back to live quotes for tickers not present in snapshot movers.
    """
    watches = prior_briefing.get("tickers_to_watch", [])
    if not watches:
        return []
    price_map: dict[str, float] = {}
    for mw in snap.gainers + snap.losers + snap.most_active:
        price_map[mw.quote.symbol] = mw.quote.change_pct
    for q in snap.premarket_us + snap.premarket_crypto:
        price_map.setdefault(q.symbol, q.change_pct)
    missing = [w.get("ticker", "") for w in watches
               if w.get("ticker", "") and w["ticker"] not in price_map]
    if missing:
        try:
            for q in fetch_quotes({s: s for s in missing if s}):
                price_map[q.symbol] = q.change_pct
        except Exception:
            pass
    out: list[ScorecardEntry] = []
    for w in watches:
        t = (w.get("ticker") or "").strip().upper()
        if not t: continue
        out.append(_entry_from_pred(t, w.get("rationale", ""), price_map.get(t)))
    return out


# ── History persistence ────────────────────────────────────────────────────
def load_scorecard_history() -> dict:
    if SCORECARD_HISTORY_PATH.exists():
        try:
            return json.loads(SCORECARD_HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  scorecard history unreadable: {e}")
    return {"days": []}


def save_scorecard_history(hist: dict) -> None:
    try:
        SCORECARD_HISTORY_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception as e:
        warn(f"  could not persist scorecard history: {e}")


def upsert_scorecard_day(hist: dict, date_iso: str, entries: list[ScorecardEntry]) -> dict:
    """Insert or replace a day's scorecard entries in history (in place)."""
    days = [d for d in hist.get("days", []) if d.get("date") != date_iso]
    days.append({
        "date": date_iso,
        "graded_at": datetime.now(ET).isoformat(timespec="seconds"),
        "entries": [asdict(e) for e in entries],
    })
    days.sort(key=lambda d: d.get("date", ""))
    hist["days"] = days
    return hist


def backfill_scorecard_history(force_dates: list[str] | None = None) -> dict:
    """Process every briefing-YYYY-MM-DD.json and grade it using yfinance close data.

    Skips dates already present in history unless `force_dates` includes them.
    Returns the updated history dict.
    """
    hist = load_scorecard_history()
    have = {d["date"] for d in hist.get("days", [])}
    force = set(force_dates or [])
    pat = re.compile(r"briefing-(\d{4}-\d{2}-\d{2})\.json$")

    for path in sorted(SCRIPT_DIR.glob("briefing-*.json")):
        m = pat.search(path.name)
        if not m: continue
        date = m.group(1)
        if date in have and date not in force:
            continue
        try:
            b = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  briefing {path.name} unreadable: {e}")
            continue
        watches = b.get("tickers_to_watch") or []
        if not watches: continue
        log(f"  backfilling scorecard for {date} ({len(watches)} picks)")
        entries: list[ScorecardEntry] = []
        for w in watches:
            t = (w.get("ticker") or "").strip().upper()
            if not t: continue
            pct = fetch_eod_change_pct(t, date)
            entries.append(_entry_from_pred(t, w.get("rationale", ""), pct))
        # Skip non-trading days: if every pick returned None, the date had no
        # market data (weekend/holiday or session not yet closed).
        if entries and any(e.actual_pct is not None for e in entries):
            upsert_scorecard_day(hist, date, entries)
        else:
            log(f"  skipping {date}: no price data (likely non-trading day)")

    save_scorecard_history(hist)
    return hist


# ── Calibration metrics (the "self-learning" feedback loop) ────────────────
def compute_calibration(hist: dict, window: int = 5) -> dict:
    """Compute rolling stats from the most recent `window` days of history.

    Provides the AI with empirical feedback on its own track record so future
    predictions can be calibrated against measured performance.
    """
    days = sorted(hist.get("days", []), key=lambda d: d.get("date", ""), reverse=True)[:window]
    all_entries = [e for d in days for e in d.get("entries", [])]

    def _gpa(entries: list[dict]) -> float | None:
        letters = [e.get("letter_grade") for e in entries if e.get("letter_grade") in _GRADE_PTS]
        return (sum(_GRADE_PTS[l] for l in letters) / len(letters)) if letters else None

    def _hit_rate(entries: list[dict], bias: str) -> tuple[float | None, int]:
        same = [e for e in entries if e.get("bias") == bias and e.get("verdict") in ("HIT", "MISS", "FLAT")]
        if not same: return (None, 0)
        hits = sum(1 for e in same if e.get("verdict") == "HIT")
        return (hits / len(same), len(same))

    bullish_hr, bullish_n = _hit_rate(all_entries, "bullish")
    bearish_hr, bearish_n = _hit_rate(all_entries, "bearish")
    neutral_hr, neutral_n = _hit_rate(all_entries, "neutral")

    return {
        "window_days": len(days),
        "total_graded": len(all_entries),
        "rolling_gpa": _gpa(all_entries),
        "rolling_letter": _gpa_letter(_gpa(all_entries)),
        "hit_rate": {
            "bullish": {"rate": bullish_hr, "n": bullish_n},
            "bearish": {"rate": bearish_hr, "n": bearish_n},
            "neutral": {"rate": neutral_hr, "n": neutral_n},
        },
        "best_day": (max(days, key=lambda d: _gpa(d.get("entries", [])) or -1).get("date")
                     if days else None),
        "worst_day": (min(days, key=lambda d: _gpa(d.get("entries", [])) or 99).get("date")
                      if days else None),
        "last_updated": datetime.now(ET).isoformat(timespec="seconds"),
    }


# ── Render ─────────────────────────────────────────────────────────────────
def _grade_card_html(e: dict) -> str:
    pct = e.get("actual_pct")
    pct_str = fmt_pct(pct) if pct is not None else "—"
    pcls = cls_for(pct or 0.0)
    grade = e.get("letter_grade") or "—"
    gcls = grade if grade in {"A","B","C","D","F"} else "NA"
    bias = e.get("bias", "neutral")
    bias_label = mode_pair_text(
        {"bullish": "predicted up", "bearish": "predicted down", "neutral": "just watching"}.get(bias, bias),
        bias,
    )

    # Paired grade reasons; recompute from bias+pct if not stored (legacy entries).
    reason_std = e.get("grade_reason_standard")
    reason_adv = e.get("grade_reason_advanced") or e.get("grade_reason")
    if not reason_std or not reason_adv:
        _, _, recomp_std, recomp_adv = _grade_prediction(bias, pct)
        reason_std = reason_std or recomp_std
        reason_adv = reason_adv or recomp_adv
    result_label = mode_pair("How it turned out", "Result")
    thesis_label = mode_pair("Why we picked it", "Thesis")
    return (
        f'<div class="grade-card">'
        f'<div class="gc-top">'
        f'<span class="gc-grade gc-grade-{gcls}">{escape_html(grade)}</span>'
        f'<span class="gc-ticker">{escape_html(e.get("ticker",""))}</span>'
        f'<span class="gc-pct num {pcls}">{escape_html(pct_str)}</span>'
        f'<span class="gc-bias gc-bias-{escape_html(bias)}">{bias_label}</span>'
        f'</div>'
        f'<div class="gc-thesis"><span class="gc-label">{thesis_label}</span>{escape_html(e.get("rationale",""))}</div>'
        f'<div class="gc-reasoning"><span class="gc-label">{result_label}</span>'
        f'{mode_pair_text(reason_std, reason_adv)}</div>'
        f'</div>'
    )


def _day_section_html(day: dict, open_default: bool = False) -> str:
    """One collapsible day-of-grades section."""
    entries = day.get("entries", [])
    letters = [e.get("letter_grade") for e in entries if e.get("letter_grade") in _GRADE_PTS]
    gpa = (sum(_GRADE_PTS[l] for l in letters) / len(letters)) if letters else None
    gpa_str = f"{gpa:.2f}" if gpa is not None else "—"
    gpa_l = _gpa_letter(gpa)
    counts = {g: sum(1 for l in letters if l == g) for g in "ABCDF"}
    dist = "".join(f'<span class="gd-pill gd-{g}">{counts[g]}{g}</span>'
                   for g in "ABCDF" if counts[g])

    try:
        date_human = datetime.fromisoformat(day["date"]).strftime("%a, %b %-d")
    except Exception:
        date_human = day.get("date", "—")

    cards = "".join(_grade_card_html(e) for e in entries)
    open_attr = " open" if open_default else ""
    gpa_pill_inner = mode_pair(
        f"Grade: {gpa_l} ({gpa_str} out of 4)",
        f"GPA {gpa_str} · {gpa_l}",
    )
    picks_count = mode_pair(
        f"{len(entries)} stocks graded",
        f"{len(entries)} picks",
    )
    return (
        f'<details class="sc-day"{open_attr}>'
        f'<summary>'
        f'<span class="sc-day-date">{escape_html(date_human)}</span>'
        f'<span class="sc-day-meta">'
        f'<span class="gpa-pill gpa-{gpa_l}">{gpa_pill_inner}</span>'
        f'{dist}<span style="color:var(--text-faint)">{picks_count}</span>'
        f'</span>'
        f'</summary>'
        f'<div class="grade-cards">{cards}</div>'
        f'</details>'
    )


def _calibration_html(cal: dict) -> str:
    """Header strip showing self-learning metrics across the rolling window."""
    if not cal or cal.get("total_graded", 0) == 0:
        return ""

    def _hr(b: dict) -> str:
        if b["rate"] is None: return f'<span class="cal-na">{b["n"]} picks</span>'
        return f'<span class="cal-rate">{b["rate"]*100:.0f}%</span> <span class="cal-n">({b["n"]})</span>'

    gpa = cal.get("rolling_gpa")
    gpa_str = f"{gpa:.2f}" if gpa is not None else "—"
    gpa_l = cal.get("rolling_letter") or "—"
    hr = cal.get("hit_rate", {})

    window_d = cal.get("window_days", 0)
    cal_label = mode_pair(
        f"How accurate we've been · last {window_d} days",
        f"Self-Calibration · Rolling {window_d}d",
    )
    bull_label = mode_pair("How often we were right when we said \"up\"", "Bullish hit-rate")
    bear_label = mode_pair("How often we were right when we said \"down\"", "Bearish hit-rate")
    neut_label = mode_pair("How often \"watch\" stocks actually moved", "Neutral hit-rate")
    return (
        '<div class="sc-calibration">'
        f'<div class="cal-label">{cal_label}</div>'
        '<div class="cal-grid">'
        f'<div class="cal-tile"><div class="cal-key">GPA</div>'
        f'<div class="cal-val gpa-{gpa_l}">{gpa_str}</div>'
        f'<div class="cal-sub">{cal.get("total_graded",0)} graded</div></div>'
        f'<div class="cal-tile"><div class="cal-key">{bull_label}</div>'
        f'<div class="cal-val">{_hr(hr.get("bullish", {"rate":None,"n":0}))}</div></div>'
        f'<div class="cal-tile"><div class="cal-key">{bear_label}</div>'
        f'<div class="cal-val">{_hr(hr.get("bearish", {"rate":None,"n":0}))}</div></div>'
        f'<div class="cal-tile"><div class="cal-key">{neut_label}</div>'
        f'<div class="cal-val">{_hr(hr.get("neutral", {"rate":None,"n":0}))}</div></div>'
        '</div></div>'
    )


def render_scorecard(snap: Snapshot, briefing: dict | None = None,
                     eod: bool = False, history: dict | None = None) -> str:
    """Multi-day scorecard with self-calibration panel + today's predictions."""
    history = history if history is not None else load_scorecard_history()
    days = sorted(history.get("days", []), key=lambda d: d.get("date",""), reverse=True)
    cal = compute_calibration(history, window=5)

    # ── Today's predictions (forward-looking) ─────────────────────────────
    if eod:
        eod_title = mode_pair("The market is closed for today", "End-of-Day · Session Closed")
        eod_sub = mode_pair(
            "Tomorrow's stock picks will appear in the morning summary",
            "Tomorrow's predictions will appear in the morning briefing",
        )
        picks_section = (
            '<div class="sc-section-head">'
            f'<span class="sc-section-title" id="sc-preds-label">{eod_title}</span>'
            f'<span class="sc-section-sub" id="sc-preds-sub">{eod_sub}</span>'
            '</div>'
        )
    else:
        ai = briefing or snap.ai or {}
        watch_picks = ai.get("tickers_to_watch") or _b_tickers_prediction(snap) or []
        picks_html = _ticker_cards_html(watch_picks) if watch_picks else ""
        preds_title = mode_pair("Today's stock picks", "Today's Predictions")
        preds_sub = mode_pair("Stocks we think are worth watching today", "Next session watchlist")
        picks_section = (
            '<div class="sc-section-head">'
            f'<span class="sc-section-title" id="sc-preds-label">{preds_title}</span>'
            f'<span class="sc-section-sub" id="sc-preds-sub">{preds_sub}</span>'
            '</div>'
            f'<div class="sc-picks-wrap">{picks_html}</div>'
        )

    # ── Calibration + per-day history ─────────────────────────────────────
    history_title = mode_pair("How our past picks did", "Graded Calls — Recent Sessions")
    if days:
        # Most recent day expanded by default; older days collapsed.
        day_blocks = "".join(
            _day_section_html(d, open_default=(i == 0))
            for i, d in enumerate(days[:10])
        )
        history_sub = mode_pair(
            f"{len(days)} day(s) saved · click any day to see details",
            f"{len(days)} day(s) of history · expandable",
        )
        history_section = (
            '<div class="sc-section-head" style="border-top:1px solid var(--border)">'
            f'<span class="sc-section-title">{history_title}</span>'
            f'<span class="sc-section-sub">{history_sub}</span>'
            '</div>'
            f'{_calibration_html(cal)}'
            f'<div class="sc-day-stack">{day_blocks}</div>'
        )
    else:
        empty_sub = mode_pair(
            "We'll start grading picks after the first full trading day",
            "Populates after the first full trading day with briefing data",
        )
        history_section = (
            '<div class="sc-section-head" style="border-top:1px solid var(--border)">'
            f'<span class="sc-section-title">{history_title}</span>'
            f'<span class="sc-section-sub" style="color:var(--text-faint)">{empty_sub}</span>'
            '</div>'
        )

    # ── Summary badge (collapsed-state header) ────────────────────────────
    if days:
        gpa = cal.get("rolling_gpa")
        gpa_str = f"{gpa:.2f}" if gpa is not None else "—"
        gpa_l = cal.get("rolling_letter") or "—"
        graded_n = cal.get("total_graded", 0)
        sessions_n = len(days)
        gpa_pill = mode_pair(
            f"Overall grade: {gpa_l} ({gpa_str} out of 4)",
            f"GPA {gpa_str} · {gpa_l}",
        )
        meta_text = mode_pair(
            f"{graded_n} picks graded across {sessions_n} day(s)",
            f"{graded_n} graded · {sessions_n} session(s)",
        )
        stats_html = (
            '<span class="sc-summary-stats">'
            f'<span class="gpa-pill gpa-{gpa_l}">{gpa_pill}</span>'
            f'<span style="color:var(--text-faint)">{meta_text}</span>'
            '</span>'
        )
    else:
        n_picks = 0
        if not eod:
            ai = briefing or snap.ai or {}
            n_picks = len(ai.get("tickers_to_watch") or _b_tickers_prediction(snap) or [])
        empty_meta = mode_pair(
            f"{n_picks} picks · we haven't graded any yet",
            f"{n_picks} picks · no prior grades yet",
        )
        stats_html = (
            f'<span class="sc-summary-stats">'
            f'<span style="color:var(--text-faint)">{empty_meta}</span>'
            f'</span>'
        )

    title_html = mode_pair("Our Track Record", "Scorecard")
    return (
        '<details class="scorecard-details" id="scorecard" open>'
        '<summary>'
        '<span class="sc-summary-left">'
        '<span class="sc-summary-arrow">▶</span>'
        f'<span class="sc-summary-title">{title_html}</span>'
        '</span>'
        f'{stats_html}'
        '</summary>'
        '<div class="scorecard-body">'
        f'{picks_section}'
        f'{history_section}'
        '</div>'
        '</details>'
    )


# ------------------------------------------------------------------------
# Sentiment strip
# ------------------------------------------------------------------------
def fetch_sentiment(snap: Snapshot) -> None:
    """Fetch CNN Fear & Greed, Crypto F&G, Put/Call ratio, BTC dominance."""
    result: dict = {}
    try:
        r = requests.get(
            "https://production.fear-and-greed.cnn.com/data/fear-and-greed",
            headers={"User-Agent": USER_AGENT}, timeout=10,
        )
        r.raise_for_status()
        fg = r.json().get("fear_and_greed", {})
        result["cnn_fg_score"] = fg.get("score")
        result["cnn_fg_rating"] = fg.get("rating", "")
    except Exception as e:
        log(f"CNN Fear & Greed: {e}")
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1",
                         headers={"User-Agent": USER_AGENT}, timeout=10)
        r.raise_for_status()
        row = r.json().get("data", [{}])[0]
        result["crypto_fg_score"] = int(row.get("value", 0))
        result["crypto_fg_rating"] = row.get("value_classification", "")
    except Exception as e:
        log(f"Crypto F&G: {e}")
    # ^PCALL is the CBOE total put/call ratio on some feeds; try gracefully
    for pc_sym in ("^PCALL", "^PCRATIO"):
        try:
            pc = fetch_quotes({pc_sym: "Put/Call Ratio"})
            if pc and pc[0].price > 0:
                result["put_call"] = round(pc[0].price, 3)
                break
        except Exception:
            pass
    vix = next((q for q in snap.indices if q.symbol == "^VIX"), None)
    if vix:
        result["vix"] = round(vix.price, 2)
        result["vix_pct"] = round(vix.change_pct, 2)
    try:
        r = requests.get(f"{COINGECKO_BASE}/global",
                         headers={"User-Agent": USER_AGENT}, timeout=10)
        r.raise_for_status()
        dom = r.json().get("data", {}).get("market_cap_percentage", {})
        result["btc_dominance"] = round(dom.get("btc", 0), 1)
        result["eth_dominance"] = round(dom.get("eth", 0), 1)
    except Exception as e:
        log(f"CoinGecko global: {e}")
    btc_q = next((m.quote for m in snap.crypto if m.quote.symbol == "BTC"), None)
    eth_q = next((m.quote for m in snap.crypto if m.quote.symbol == "ETH"), None)
    if btc_q and eth_q and btc_q.price:
        result["eth_btc"] = round(eth_q.price / btc_q.price, 5)
    snap.sentiment = result


def render_sentiment_strip(snap: Snapshot) -> str:
    """Render the sentiment indicator tile row."""
    s = snap.sentiment
    if not s:
        return ""

    def tile(label: str, val: str, sub: str = "", cls: str = "") -> str:
        cls_str = f" {cls}" if cls else ""
        sub_html = f'<div class="st-sub">{escape_html(sub)}</div>' if sub else ""
        return (f'<div class="st-tile">'
                f'<div class="st-label">{escape_html(label)}</div>'
                f'<div class="st-val{cls_str}">{escape_html(val)}</div>'
                f'{sub_html}</div>')

    tiles: list[str] = []
    if "vix" in s:
        vcls = "up" if s.get("vix_pct", 0) > 2 else ("down" if s.get("vix_pct", 0) < -2 else "flat")
        tiles.append(tile("VIX", f'{s["vix"]:.2f}', fmt_pct(s.get("vix_pct", 0)), vcls))
    if s.get("cnn_fg_score") is not None:
        sc = int(s["cnn_fg_score"])
        tiles.append(tile("Fear & Greed", str(sc), s.get("cnn_fg_rating", "").title(),
                          "up" if sc >= 60 else ("down" if sc <= 40 else "flat")))
    if "crypto_fg_score" in s:
        sc = s["crypto_fg_score"]
        tiles.append(tile("Crypto F&G", str(sc), s.get("crypto_fg_rating", "").title(),
                          "up" if sc >= 60 else ("down" if sc <= 40 else "flat")))
    if "put_call" in s:
        pc = s["put_call"]
        tiles.append(tile("Put/Call", f'{pc:.2f}', ">1.0 = bearish",
                          "down" if pc > 1.0 else ("up" if pc < 0.7 else "flat")))
    if "btc_dominance" in s:
        tiles.append(tile("BTC Dom", f'{s["btc_dominance"]:.1f}%',
                          f'ETH {s.get("eth_dominance", 0):.1f}%'))
    if "eth_btc" in s:
        tiles.append(tile("ETH/BTC", f'{s["eth_btc"]:.5f}'))
    if not tiles:
        return ""
    return (f'<h2 id="sentiment"><span class="std-only">How Investors Are Feeling</span><span class="adv-only">Sentiment</span></h2>'
            f'<div class="sentiment-strip">{"".join(tiles)}</div>')


# ------------------------------------------------------------------------
# Watchlist
# ------------------------------------------------------------------------
def fetch_watchlist_quotes() -> list[Quote]:
    """Read WATCHLIST env var (comma-separated tickers) and fetch their quotes."""
    raw_wl = os.environ.get("WATCHLIST", "").strip()
    if not raw_wl:
        env_path = SCRIPT_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("WATCHLIST"):
                    _, _, v = line.partition("=")
                    raw_wl = v.strip().strip('"').strip("'")
                    break
    tickers = [t.strip().upper() for t in raw_wl.split(",") if t.strip()] if raw_wl else DEFAULT_WATCHLIST
    return fetch_quotes({t: t for t in tickers}) if tickers else []


def render_watchlist(snap: Snapshot) -> str:
    """Compact tile row — kept for any legacy callers."""
    if not snap.watchlist:
        return ""
    tpl = _jinja_env.get_template("_watch_item.html")
    tiles = []
    for q in snap.watchlist:
        cls = cls_for(q.change_pct)
        tiles.append(tpl.render(
            symbol=escape_html(q.symbol),
            price=fmt_usd(q.price),
            cls=cls,
            pct=fmt_pct(q.change_pct),
        ))
    return f'<div class="wl-row" id="watchlist">{"".join(tiles)}</div>'


def render_sidebar_block(snap: Snapshot) -> str:
    """Rich watchlist sidebar cards — server-rendered with price, prediction, news, earnings."""
    source = snap.watchlist_news or [MoverWithNews(quote=q) for q in snap.watchlist]
    if not source:
        return '<p style="color:var(--text-faint);font-size:12px;padding:8px 16px">No watchlist configured. Add tickers above or set the WATCHLIST environment variable.</p>'

    earnings_syms = {e.symbol_or_event for e in snap.earnings_today}

    def _prediction(q: Quote) -> tuple[str, str, str, str, str]:
        """Returns (bias_class, label_adv, label_std, analysis_adv, analysis_std)."""
        pct = q.change_pct
        if pct > 3:
            return ("bull",
                f"▲ Strong Momentum +{pct:.1f}%",
                f"▲ Big jump +{pct:.1f}%",
                "Significant prior-session gain. Momentum plays often see continuation in the opening hour — watch for volume confirmation above yesterday's close.",
                "Had a really strong day. When stocks jump this much, they often keep going up the next morning — but watch closely: if not many people are buying, it could fade.")
        if pct > 1:
            prior = fmt_usd(q.price / (1 + pct/100))
            return ("bull",
                f"▲ Bullish +{pct:.1f}%",
                f"▲ Up +{pct:.1f}%",
                f"Mild upside last session. Near-term bias positive while price holds above prior close of {prior}.",
                f"Rose nicely. Likely to keep edging up as long as the price stays above {prior} (where it closed the day before).")
        if pct < -3:
            return ("bear",
                f"▼ Selling Pressure {pct:.1f}%",
                f"▼ Big drop {pct:.1f}%",
                "Sharp prior-session decline. Path of least resistance lower until a catalyst or support level holds. Risk elevated — position sizing matters.",
                "Took a hard hit yesterday. The path of least resistance is more downside until something positive comes along — be careful with how much you put in.")
        if pct < -1:
            bounce = fmt_usd(q.price * 0.98)
            return ("bear",
                f"▼ Bearish {pct:.1f}%",
                f"▼ Down {pct:.1f}%",
                f"Modest prior-session loss. Watch for bounce off {fmt_usd(q.price * 0.98)} or continuation below prior low.",
                f"Slipped a bit. Watch to see if it bounces back from around {bounce} — or keeps falling.")
        return ("flat",
            "— Neutral",
            "— Quiet day",
            "Tight prior-session range. Likely to follow the broader tape direction today. Catalyst-driven — monitor news flow.",
            "Didn't move much yesterday. Will likely follow whatever the broader market does today — keep an eye on the news for surprises.")

    cards_html = ""
    for mw in source:
        q   = mw.quote
        cls = cls_for(q.change_pct)
        pct_sign = "+" if q.change_pct >= 0 else ""
        pct_color = "var(--up)" if q.change_pct > 0 else ("var(--down)" if q.change_pct < 0 else "var(--text-faint)")

        bias_cls, bias_label_adv, bias_label_std, analysis_adv, analysis_std = _prediction(q)
        name = escape_html(q.name or q.symbol)

        # Earnings badge
        earn_badge = (
            '<span class="sb-badge earnings">⚡ Earnings Today</span>'
            if q.symbol in earnings_syms else ""
        )
        bias_badge = (
            f'<span class="sb-badge {bias_cls}">'
            f'<span class="std-only">{escape_html(bias_label_std)}</span>'
            f'<span class="adv-only">{escape_html(bias_label_adv)}</span>'
            f'</span>'
        )

        # Top news headline
        news_html = ""
        if mw.news:
            n   = mw.news[0]
            url = n.link or ""
            hl  = escape_html(n.title)
            news_html = (
                f'<div class="sb-news">'
                + (f'<a href="{escape_html(url)}" target="_blank" rel="noopener">{hl}</a>' if url else hl)
                + '</div>'
            )

        sym_js = escape_html(q.symbol).replace("'", "\\'")
        cards_html += (
            f'<div class="sb-card {cls}" data-symbol="{escape_html(q.symbol)}">'
            f'  <div class="sb-card-top">'
            f'    <span class="sb-sym">{escape_html(q.symbol)}</span>'
            f'    <span class="sb-pct {cls}">{pct_sign}{q.change_pct:.2f}%</span>'
            f'    <button class="sb-user-rm" title="Hide" onclick="hideServerCard(\'{sym_js}\')">✕</button>'
            f'  </div>'
            f'  <div class="sb-name">{name}</div>'
            f'  <div class="sb-price">{fmt_usd(q.price)}</div>'
            f'  <div class="sb-badges">{earn_badge}{bias_badge}</div>'
            f'  <div class="sb-pred">'
            f'    <span class="std-only">{escape_html(analysis_std)}</span>'
            f'    <span class="adv-only">{escape_html(analysis_adv)}</span>'
            f'  </div>'
            f'  {news_html}'
            f'</div>'
        )

    return cards_html


# ------------------------------------------------------------------------
# Earnings reactions
# ------------------------------------------------------------------------
def fetch_eps_results(symbols: list[str]) -> dict[str, dict]:
    """Fetch most-recent actual EPS vs estimate for a list of symbols via yfinance."""
    results: dict[str, dict] = {}

    def _get(sym: str) -> None:
        try:
            df = yf.Ticker(sym).earnings_dates
            if df is None or df.empty:
                return
            df = df.dropna(subset=["EPS Estimate", "Reported EPS"])
            if df.empty:
                return
            row = df.iloc[0]
            est = float(row["EPS Estimate"])
            act = float(row["Reported EPS"])
            surprise_pct = ((act - est) / abs(est) * 100) if est != 0 else 0.0
            if surprise_pct > 3:
                verdict = "BEAT"
            elif surprise_pct < -3:
                verdict = "MISS"
            else:
                verdict = "IN-LINE"
            results[sym] = {
                "eps_est": round(est, 2),
                "eps_act": round(act, 2),
                "surprise_pct": round(surprise_pct, 1),
                "verdict": verdict,
            }
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(_get, symbols))
    return results


def fetch_earnings_reactions(prior_earnings: list[CalendarEvent]) -> list[MoverWithNews]:
    """Fetch today's % change for tickers that reported in the prior session."""
    symbols = [e.symbol_or_event for e in prior_earnings
               if e.symbol_or_event and e.symbol_or_event.isalpha()
               and len(e.symbol_or_event) <= 5][:50]  # cap to avoid timeout
    if not symbols:
        return []
    log(f"Fetching earnings reactions for {len(symbols)} symbols…")
    name_map = {e.symbol_or_event: (e.description or e.symbol_or_event)
                for e in prior_earnings if e.symbol_or_event}
    quotes = fetch_quotes({s: name_map.get(s, s) for s in symbols})
    if not quotes:
        return []
    quotes.sort(key=lambda q: abs(q.change_pct), reverse=True)
    return attach_news(quotes[:15])


def render_earnings_reactions(snap: Snapshot) -> str:
    """Render enriched panel of prior session earnings reactions with EPS beat/miss."""
    if not snap.earnings_reactions:
        return ""

    cards = []
    for mw in snap.earnings_reactions:
        q = mw.quote
        sym = q.symbol
        result = snap.earnings_results.get(sym)
        verdict = result["verdict"] if result else None
        card_cls = {"BEAT": "beat", "MISS": "miss", "IN-LINE": "inline"}.get(verdict or "", "")
        verdict_html = (
            f'<span class="ef-verdict {verdict}">{verdict}</span>' if verdict else ""
        )

        pct = q.change_pct
        sign = "+" if pct >= 0 else ""
        move_cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")

        eps_html = ""
        if result:
            sup = result["surprise_pct"]
            sup_sign = "+" if sup >= 0 else ""
            eps_html = (
                f'<div class="ef-result">'
                f'<span class="ef-eps">EPS: ${result["eps_act"]:.2f} vs '
                f'${result["eps_est"]:.2f} est ({sup_sign}{sup:.1f}%)</span>'
                f'<span class="ef-move {move_cls}">{sign}{pct:.2f}%</span>'
                f'</div>'
            )
        else:
            eps_html = (
                f'<div class="ef-result">'
                f'<span class="ef-eps" style="color:var(--text-faint)">EPS data unavailable</span>'
                f'<span class="ef-move {move_cls}">{sign}{pct:.2f}%</span>'
                f'</div>'
            )

        summary = ""
        if mw.news:
            summary = mw.news[0].title
            if len(mw.news) > 1 and len(summary) < 60:
                summary = mw.news[0].title + " · " + mw.news[1].title
        summary_html = (
            f'<div class="ef-summary">{escape_html(summary[:220])}</div>' if summary else ""
        )

        price_str = fmt_usd(q.price) if q.price else "—"
        cards.append(
            f'<div class="ef-card {card_cls}" style="margin-bottom:8px">'
            f'  <div class="ef-sym-row">'
            f'    <span class="ef-sym">{escape_html(sym)}</span>'
            f'    {verdict_html}'
            f'  </div>'
            f'  <div class="ef-name" style="margin-bottom:4px">'
            f'    {escape_html(q.name or sym)} &nbsp;'
            f'    <span style="color:var(--text-faint);font-size:11px">{price_str}</span>'
            f'  </div>'
            f'  {eps_html}'
            f'  {summary_html}'
            f'</div>'
        )

    grid = f'<div class="earnings-featured" style="grid-template-columns:repeat(auto-fill,minmax(240px,1fr))">{"".join(cards)}</div>'
    return (
        f'<div id="earnings-reactions" style="margin-bottom:18px">'
        f'<div class="earnings-section-label" style="margin-top:0">'
        f'Prior Session Earnings Results · sorted by absolute move</div>'
        f'{grid}'
        f'</div>'
    )


# ------------------------------------------------------------------------
# Main orchestration
# ------------------------------------------------------------------------
def build_snapshot(no_ai: bool = False, no_premarket: bool = False) -> Snapshot:
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

    log("Fetching sector performance (YTD)…")
    try:
        snap.sectors = fetch_sectors()
    except Exception as e:
        warn(f"sector fetch failed: {e}", snap)

    log("Fetching watchlist…")
    try:
        snap.watchlist = fetch_watchlist_quotes()
        snap.watchlist_news = attach_news(snap.watchlist[:12])
    except Exception as e:
        warn(f"watchlist fetch failed: {e}", snap)

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

    log("Fetching world / macro news headlines…")
    try:
        snap.world_news_raw = fetch_world_news()
    except Exception as e:
        warn(f"world news fetch failed: {e}", snap)

    log("Fetching crypto markets from CoinGecko…")
    crypto_q = fetch_crypto_markets(CRYPTO_TOP_N)
    if not crypto_q:
        warn("No crypto data — CoinGecko may be rate-limited.", snap)
    snap.crypto = attach_crypto_news(crypto_q)
    # sort for gainer/loser subsets (display ordering is applied later at
    # render time, so it also covers the --offline cached-snapshot path)
    sorted_c = sorted(crypto_q, key=lambda q: q.change_pct, reverse=True)
    snap.crypto_gainers = [MoverWithNews(quote=q) for q in sorted_c[:5]]
    snap.crypto_losers = [MoverWithNews(quote=q) for q in sorted_c[-5:][::-1]]

    log("Fetching sentiment indicators…")
    try:
        fetch_sentiment(snap)
    except Exception as e:
        warn(f"sentiment fetch failed: {e}", snap)

    if not no_premarket:
        log("Fetching pre-market & overnight data…")
        try:
            fetch_premarket(snap)
        except Exception as e:
            warn(f"pre-market fetch failed: {e}", snap)

    today_iso = datetime.now(ET).date().isoformat()
    log(f"Fetching earnings calendar for {today_iso}…")
    snap.earnings_today = fetch_earnings_calendar(today_iso)
    log(f"Fetching economic events for {today_iso}…")
    snap.econ_events_today = fetch_econ_events(today_iso)

    log(f"Fetching earnings reactions ({snap.prior_session_date})…")
    try:
        prior_earnings_cal = fetch_earnings_calendar(snap.prior_session_date)
        snap.earnings_reactions = fetch_earnings_reactions(prior_earnings_cal)
    except Exception as e:
        warn(f"earnings reactions failed: {e}", snap)

    # Fetch actual EPS vs estimate for all reaction symbols + today's reporters
    reaction_syms = [mw.quote.symbol for mw in snap.earnings_reactions]
    today_syms = [e.symbol_or_event for e in snap.earnings_today
                  if e.symbol_or_event and e.symbol_or_event.isalpha() and len(e.symbol_or_event) <= 5]
    all_eps_syms = list(dict.fromkeys(reaction_syms + today_syms))  # deduplicated, order preserved
    if all_eps_syms:
        log(f"Fetching EPS results for {len(all_eps_syms)} symbols…")
        try:
            snap.earnings_results = fetch_eps_results(all_eps_syms)
            log(f"EPS results: {len(snap.earnings_results)} symbols with data")
        except Exception as e:
            warn(f"EPS results fetch failed: {e}", snap)

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
        def ev_from(d): return CalendarEvent(
            time=d.get("time",""), symbol_or_event=d.get("symbol_or_event",""),
            description=d.get("description",""), extra=d.get("extra",""),
            url=d.get("url",""), market_cap=float(d.get("market_cap",0) or 0),
        )
        def sp_from(d): return SectorPerf(**d)
        def sc_from(d): return ScorecardEntry(
            ticker=d.get("ticker", ""), rationale=d.get("rationale", ""),
            bias=d.get("bias", "neutral"), actual_pct=d.get("actual_pct"),
            verdict=d.get("verdict", "N/A"),
            letter_grade=d.get("letter_grade", "—"),
            grade_reason=d.get("grade_reason", ""),
            grade_reason_standard=d.get("grade_reason_standard", ""),
            grade_reason_advanced=d.get("grade_reason_advanced", ""),
        )

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
            premarket_us=[q_from(x) for x in raw.get("premarket_us", [])],
            premarket_macro=[q_from(x) for x in raw.get("premarket_macro", [])],
            premarket_crypto=[q_from(x) for x in raw.get("premarket_crypto", [])],
            overnight_global=[q_from(x) for x in raw.get("overnight_global", [])],
            premarket_fetched_at=raw.get("premarket_fetched_at", ""),
            sectors=[sp_from(x) for x in raw.get("sectors", [])],
            scorecard=[sc_from(x) for x in raw.get("scorecard", [])],
            sentiment=raw.get("sentiment", {}),
            watchlist=[q_from(x) for x in raw.get("watchlist", [])],
            watchlist_news=[mw_from(x) for x in raw.get("watchlist_news", [])],
            earnings_reactions=[mw_from(x) for x in raw.get("earnings_reactions", [])],
            earnings_results=raw.get("earnings_results", {}),
            world_news_raw=raw.get("world_news_raw", []),
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
    p.add_argument("--no-premarket", action="store_true", help="Skip pre-market / overnight fetch.")
    p.add_argument(
        "--eod", action="store_true",
        help="End-of-day mode: grade today's predictions against today's close (runs at 4:15 PM ET).",
    )
    return p.parse_args()


_PAGES_BASE = "https://jackjensen0614.github.io/daily-market-report"

def load_briefing_json(path: str | None, snap_date: str | None = None) -> dict | None:
    if not path and snap_date:
        # Auto-detect briefing-YYYY-MM-DD.json next to the script
        candidate = Path(__file__).parent / f"briefing-{snap_date}.json"
        if candidate.exists():
            path = str(candidate)
    if path:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as e:
            log(f"Could not load briefing JSON ({path}): {e}")
            return None
    # Fallback: fetch from live GitHub Pages (works in GitHub Actions where local file was never committed)
    if snap_date:
        url = f"{_PAGES_BASE}/briefing-{snap_date}.json"
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                log(f"Loaded briefing from Pages: {url}")
                return r.json()
        except Exception as e:
            log(f"Could not fetch briefing from Pages ({url}): {e}")
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
        snap = build_snapshot(no_ai=args.no_ai, no_premarket=args.no_premarket)
        save_cache(snap)

    # Target the next upcoming trading session (today if a weekday, else next Monday).
    # This makes weekend rollover runs generate Monday's briefing automatically.
    today_iso = datetime.now(ET).date().isoformat()
    target_session = _next_trading_day_at_or_after(today_iso)
    briefing = load_briefing_json(args.briefing_json, snap_date=target_session)

    if not args.eod:
        if briefing is None and not args.no_ai:
            log(f"Generating briefing for {target_session} via Anthropic…")
            briefing = generate_briefing(snap)

        # Persist briefing under the target trading session's date so the
        # following EOD run finds it for grading.
        if briefing:
            bp = SCRIPT_DIR / f"briefing-{target_session}.json"
            if not bp.exists():
                try:
                    bp.write_text(json.dumps(briefing, indent=2), encoding="utf-8")
                    log(f"Briefing persisted to {bp.name}")
                except Exception as e:
                    warn(f"Could not persist briefing: {e}")
            out_briefing = Path(args.out).parent / f"briefing-{target_session}.json"
            if out_briefing != bp:
                try:
                    out_briefing.write_text(json.dumps(briefing, indent=2), encoding="utf-8")
                    log(f"Briefing also saved to {out_briefing}")
                except Exception as e:
                    log(f"Could not save briefing to output dir: {e}")

    # Score predictions for the relevant day, then upsert into history
    history = load_scorecard_history()
    if args.eod:
        # EOD mode: grade TODAY's morning predictions against today's closing prices
        today_iso = datetime.now(ET).date().isoformat()
        eod_briefing = load_briefing_json(None, snap_date=today_iso)
        graded_date = today_iso
        if eod_briefing is None:
            eod_briefing = load_briefing_json(None, snap_date=snap.prior_session_date)
            graded_date = snap.prior_session_date
        if eod_briefing:
            log("EOD: Scoring today's predictions against today's close…")
            snap.scorecard = score_predictions(eod_briefing, snap)
            if snap.scorecard:
                upsert_scorecard_day(history, graded_date, snap.scorecard)
        else:
            log("EOD: No today's briefing found — scorecard will be empty.")
    else:
        prior_date_str = _prior_trading_day_before(snap.prior_session_date)
        prior_briefing = load_briefing_json(None, snap_date=prior_date_str)
        if prior_briefing:
            log("Scoring prior day's predictions…")
            snap.scorecard = score_predictions(prior_briefing, snap)
            if snap.scorecard:
                upsert_scorecard_day(history, prior_date_str, snap.scorecard)

    # Backfill any other briefings we have on disk but no grades for yet
    log("Backfilling scorecard history from briefing files…")
    history = backfill_scorecard_history()

    _apply_crypto_display_order(snap)
    _log_missing_rationales(snap, briefing)
    log("Rendering HTML…")
    html = render_report(snap, briefing=briefing, eod=args.eod, history=history)
    out = Path(args.out)
    out.write_text(html, encoding="utf-8")
    log(f"Report written to {out}")

    if not args.no_open:
        try:
            import webbrowser
            # Append a timestamp so the browser can't serve a stale cached copy
            cache_bust = f"?v={int(time.time())}"
            webbrowser.open(out.as_uri() + cache_bust)
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
