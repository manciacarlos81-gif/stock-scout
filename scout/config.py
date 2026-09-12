"""Settings. Edit the numbers here to change what the scanner looks for."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"          # downloaded raw data (gitignored)
DATA_DIR = ROOT / "data"            # pick history + saved stock list (committed); latest.csv full table (local only)
REPORTS_DIR = ROOT / "reports"      # daily markdown reports
STOCKS_DIR = ROOT / "stocks"        # one note per stock that made a screen
RESEARCH_DIR = ROOT / "research"    # deep-dive notes from `scout.py stock TICKER`
DOCS_DIR = ROOT / "docs"            # GitHub Pages dashboard

# SEC asks every automated client to identify itself. Set SEC_USER_AGENT to "your-name your@email".
SEC_UA = os.environ.get("SEC_USER_AGENT") or "stock-scout research contact@example.com"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Which stocks get scanned at all
MIN_MARKET_CAP = 300e6       # skip tiny companies (hard to trade, noisy data)
MIN_PRICE = 3.0              # skip penny stocks
MIN_DOLLAR_VOLUME = 2e6      # average daily $ traded over the last 20 days
MAX_DATA_AGE_DAYS = 470      # skip companies whose latest SEC financials are older than this

# How much each factor counts toward the overall score (must sum to 1)
WEIGHTS = {"value": 0.25, "quality": 0.25, "growth": 0.15, "momentum": 0.20, "health": 0.15}

INSIDER_LOOKBACK_DAYS = 90
TOP_N = 20                   # stocks listed per screen
