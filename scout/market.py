"""Stock list (Nasdaq's public screener) and prices (Yahoo Finance via yfinance). Free, no keys."""
from __future__ import annotations

import logging
import re
import time

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from .config import BROWSER_UA, DATA_DIR
from . import sec

NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
NOT_COMMON = re.compile(r"\b(?:warrants?|rights?|units?|preferred|notes due|debentures|"
                        r"depositary shares,? each representing a|subordinated)\b", re.I)
MARKET_TICKERS = ["SPY", "QQQ", "IWM", "^VIX"]
SECTOR_ETFS = {"XLK": "Technology", "XLF": "Financials", "XLV": "Health care", "XLE": "Energy",
               "XLI": "Industrials", "XLY": "Consumer discretionary", "XLP": "Consumer staples",
               "XLU": "Utilities", "XLB": "Materials", "XLRE": "Real estate", "XLC": "Communication"}


def universe() -> pd.DataFrame:
    """Every common stock on NYSE/Nasdaq/NYSE American, with sector and SEC CIK."""
    path = DATA_DIR / "universe.csv"
    try:
        r = requests.get(NASDAQ_URL, headers={"User-Agent": BROWSER_UA, "Accept": "application/json"}, timeout=60)
        r.raise_for_status()
        raw = pd.DataFrame(r.json()["data"]["rows"])
        df = pd.DataFrame({
            "ticker": raw["symbol"].str.strip().str.upper().str.replace("/", "-", regex=False),
            # Downstream (report rendering, sector labels) treats "name" as always a
            # string; a null company name from the screener must become "", not NaN.
            "name": raw["name"].fillna("").str.strip(),
            "sector": raw["sector"].replace("", "Other").fillna("Other"),
            "industry": raw["industry"].fillna(""),
            "country": raw["country"].fillna(""),
            "list_market_cap": pd.to_numeric(raw["marketCap"], errors="coerce"),
            "list_price": pd.to_numeric(raw["lastsale"].str.replace("$", "", regex=False), errors="coerce"),
            "list_volume": pd.to_numeric(raw["volume"], errors="coerce"),
        })
        DATA_DIR.mkdir(exist_ok=True)
        df.to_csv(path, index=False)
    except Exception as e:  # Nasdaq sometimes blocks cloud servers; the saved copy is fine for sectors
        print(f"  Nasdaq stock list unavailable ({e.__class__.__name__}); using saved copy")
        df = pd.read_csv(path)

    # na=False: a null ticker or name (the Nasdaq screener occasionally ships one)
    # must not match these masks, not crash them — `~` on a `str.contains` result
    # that contains a bare None/NaN (object-dtype column, no match to report)
    # raises TypeError, which would take down the whole scan over one bad row.
    df = df[~df["ticker"].str.contains(r"[\^\s]", regex=True, na=False)
            & ~df["name"].str.contains(NOT_COMMON, na=False)]
    df = df[df["industry"] != "Blank Checks"]
    df["name"] = df["name"].str.replace(
        r"\s+(?:Class [A-Z] )?(?:Common Stock|Ordinary Shares|Common Shares|Capital Stock|"
        r"American Depositary Shares)\b.*$", "", regex=True).str.strip(" ,")
    ciks = sec.ticker_map()[["ticker", "cik", "exchange"]]
    df = df.merge(ciks, on="ticker", how="inner")
    # one row per company: keep its most-traded share class
    df["_dv"] = df["list_price"] * df["list_volume"]
    df = df.sort_values("_dv", ascending=False).drop_duplicates("cik").drop(columns="_dv")
    return df.reset_index(drop=True)


def _field(df: pd.DataFrame, field: str, tickers: list[str]) -> pd.DataFrame:
    """One column per ticker for ``field``, whatever shape yfinance handed back.

    For a multi-ticker request the columns are a (field, ticker) MultiIndex and
    ``df[field]`` is already ticker-keyed. For a request of a single ticker the
    columns are flat, so ``df[field]`` is a Series named after the field — left
    alone it would enter the scan as a column literally called "Close" while the
    ticker itself vanished. Name it after the ticker instead.
    """
    part = df[field]
    if isinstance(part, pd.Series):
        return part.to_frame(name=tickers[0])
    return part


def download_prices(tickers: list[str], period="2y", chunk=200) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily adjusted closes and volumes, one column per ticker."""
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    closes, vols = [], []
    failed = 0
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, interval="1d", auto_adjust=True,
                                 progress=False, threads=True, group_by="column")
                break
            except Exception as e:
                print(f"  price download retry ({e.__class__.__name__})")
                time.sleep(20 * (attempt + 1))
        else:
            failed += len(part)
            continue
        if df.empty:
            failed += len(part)
            continue
        closes.append(_field(df, "Close", part))
        vols.append(_field(df, "Volume", part))
        print(f"  prices {min(i + chunk, len(tickers))}/{len(tickers)}", end="\r")
    print()
    if not closes:
        # Without prices every downstream metric is nan and the scan would write
        # an empty report over yesterday's good one. Stop here instead.
        raise RuntimeError(
            f"no price data for any of the {len(tickers)} tickers "
            "(Yahoo Finance unreachable or rate-limiting); scan aborted"
        )
    if failed:
        print(f"  warning: no prices for {failed} of {len(tickers)} tickers")
    close = pd.concat(closes, axis=1)
    vol = pd.concat(vols, axis=1)
    close = close.loc[:, ~close.columns.duplicated()].sort_index()
    vol = vol.loc[:, ~vol.columns.duplicated()].sort_index()
    return close, vol


def price_metrics(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    c = close.ffill(limit=5)
    last = c.iloc[-1]

    def back(n):
        return c.iloc[-1 - n] if len(c) > n else pd.Series(np.nan, index=c.columns)

    m = pd.DataFrame(index=c.columns)
    m["price"] = last
    m["ret_1m"] = last / back(21) - 1
    m["ret_3m"] = last / back(63) - 1
    m["ret_6m"] = last / back(126) - 1
    m["ret_12m"] = last / back(252) - 1
    m["mom_12_1"] = back(21) / back(252) - 1          # 12-month return skipping the last month
    tail = c.iloc[-200:]
    m["sma200"] = tail.mean().where(tail.count() >= 190)
    m["sma50"] = c.iloc[-50:].mean()
    m["vs_sma200"] = last / m["sma200"] - 1
    yr = c.iloc[-252:]
    m["high_52w"] = yr.max()
    m["low_52w"] = yr.min()
    m["pct_below_high"] = 1 - last / m["high_52w"]
    m["volatility"] = yr.pct_change().std() * np.sqrt(252)
    m["max_drawdown"] = (yr / yr.cummax() - 1).min()
    delta = c.diff().iloc[-150:]
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    m["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    m["dollar_volume"] = (c * volume.reindex_like(c)).iloc[-20:].mean()
    m.index.name = "ticker"
    return m
