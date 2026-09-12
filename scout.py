#!/usr/bin/env python3
"""Stock Scout — screens every US-listed stock using free public data.

    python scout.py scan              full market scan -> reports/, stocks/, data/, docs/
    python scout.py scan --limit 300  quick test on the 300 most-traded stocks
    python scout.py stock AAPL MSFT   deep-dive notes -> research/
"""
from __future__ import annotations

import argparse
import time
from datetime import date, timedelta

import pandas as pd

from scout import insiders, macro, market, report, scoring, sec
from scout.config import (DATA_DIR, INSIDER_LOOKBACK_DAYS, MAX_DATA_AGE_DAYS, MIN_DOLLAR_VOLUME,
                          MIN_MARKET_CAP, MIN_PRICE, WEIGHTS)

HISTORY = DATA_DIR / "picks_history.csv"


def market_summary(pm: pd.DataFrame) -> dict:
    idx = []
    for t, label in (("SPY", "S&P 500 (SPY)"), ("QQQ", "Nasdaq 100 (QQQ)"), ("IWM", "Small caps (IWM)")):
        if t in pm.index:
            r = pm.loc[t]
            idx.append({"ticker": t, "label": label, "price": r["price"], "ret_1m": r["ret_1m"],
                        "ret_12m": r["ret_12m"], "vs_sma200": r["vs_sma200"]})
    sectors = []
    for etf, name in market.SECTOR_ETFS.items():
        if etf in pm.index:
            r = pm.loc[etf]
            sectors.append({"etf": etf, "name": name, "ret_1m": r["ret_1m"], "ret_3m": r["ret_3m"],
                            "ret_6m": r["ret_6m"], "above_200": bool(r["vs_sma200"] > 0)})
    sectors.sort(key=lambda s: -(s["ret_3m"] if pd.notna(s["ret_3m"]) else -9))
    return {"indexes": idx, "sectors": sectors}


def track_record(screens: dict, close: pd.DataFrame, today: str) -> pd.DataFrame:
    """Log today's picks, then judge every pick that's 30+ days old against the S&P 500."""
    new = pd.concat([pd.DataFrame({"date": today, "screen": k, "ticker": h["ticker"], "price": h["price"].round(2)})
                     for k, h in screens.items()], ignore_index=True)
    hist = pd.read_csv(HISTORY) if HISTORY.exists() else new.iloc[0:0]
    hist = pd.concat([hist[hist["date"] != today], new], ignore_index=True)
    hist.to_csv(HISTORY, index=False)

    entries = hist.sort_values("date").drop_duplicates(["screen", "ticker"])  # first time each stock entered a screen
    entries = entries[pd.to_datetime(entries["date"]) <= pd.Timestamp(today) - pd.Timedelta(days=30)]
    if entries.empty or "SPY" not in close:
        return pd.DataFrame()
    c = close.ffill()

    def ret(t, d, fallback=None):
        if t in c and pd.Timestamp(d) >= c.index[0]:
            start = c[t].asof(pd.Timestamp(d))
            return c[t].iloc[-1] / start - 1 if pd.notna(start) and start else None
        return c[t].iloc[-1] / fallback - 1 if fallback and t in c else None

    entries = entries.assign(ret=[ret(t, d, p) for t, d, p in entries[["ticker", "date", "price"]].values],
                             spy_ret=[ret("SPY", d) for d in entries["date"]]).dropna(subset=["ret", "spy_ret"])
    if entries.empty:
        return pd.DataFrame()
    entries["beat"] = entries["ret"] > entries["spy_ret"]
    return entries.groupby("screen").agg(n=("ret", "size"), ret=("ret", "mean"), spy_ret=("spy_ret", "mean"),
                                         beat=("beat", "mean")).reset_index()


def scan(limit: int | None = None):
    t0 = time.time()
    today = date.today()
    print("1/6 Stock list")
    uni = market.universe()
    cap = uni["list_market_cap"]
    uni = uni[cap.isna() | (cap <= 0) | (cap >= MIN_MARKET_CAP * 0.7)]
    if limit:
        uni = uni.head(limit)
    print(f"  {len(uni)} candidates")

    print("2/6 SEC financials")
    fund = sec.fundamentals(today)

    print("3/6 Prices")
    close, vol = market.download_prices(uni["ticker"].tolist() + market.MARKET_TICKERS + list(market.SECTOR_ETFS))
    pm = market.price_metrics(close, vol)

    print("4/6 Insider buying")
    ins, insider_source = insiders.recent_buys(INSIDER_LOOKBACK_DAYS)

    print("5/6 Economy")
    macro_data = macro.fetch()

    print("6/6 Scoring")
    d = (uni.merge(fund, on="cik", how="inner")
            .merge(pm, left_on="ticker", right_index=True, how="inner")
            .merge(ins, left_on="ticker", right_index=True, how="left"))
    scaled = d["list_market_cap"] * d["price"] / d["list_price"]
    d["market_cap"] = scaled.where(scaled > 0, d["price"] * d["shares_outstanding"].fillna(d["diluted_shares"]))
    d["fin_asof"] = d["revenue_asof"].fillna(d["net_income_asof"])
    d["data_age_days"] = [(today - x).days if isinstance(x, date) else None for x in d["fin_asof"]]
    d = d[(d["market_cap"] >= MIN_MARKET_CAP) & (d["price"] >= MIN_PRICE)
          & (d["dollar_volume"] >= MIN_DOLLAR_VOLUME) & (d["data_age_days"] <= MAX_DATA_AGE_DAYS)]
    d = scoring.add_scores(scoring.add_ratios(d))
    d["flag_text"] = ["; ".join(scoring.flags(r)) for r in d.to_dict("records")]
    d = d.sort_values("composite", ascending=False).reset_index(drop=True)
    screens = scoring.run_screens(d)

    stamp = today.isoformat()
    perf = track_record(screens, close, stamp)
    mk = market_summary(pm)
    meta = {"date": stamp, "scanned": len(d), "insider_source": insider_source, "weights": WEIGHTS,
            "price_date": str(close.index[-1].date()), "runtime_s": round(time.time() - t0)}
    report.write_report(d, screens, macro_data, mk, perf, meta)
    report.write_stock_notes(d, screens, stamp)
    report.write_data(d, screens, macro_data, mk, perf, meta)
    print(f"Done in {meta['runtime_s']}s: {len(d)} stocks scored -> reports/latest.md, docs/data.json")
    return d, screens


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan the whole market")
    s.add_argument("--limit", type=int, help="only scan the N most-traded stocks (for testing)")
    k = sub.add_parser("stock", help="deep dive on one or more tickers")
    k.add_argument("tickers", nargs="+")
    a = p.parse_args()
    if a.cmd == "scan":
        scan(a.limit)
    else:
        from scout import research
        for t in a.tickers:
            print(research.deep_dive(t))


if __name__ == "__main__":
    main()
