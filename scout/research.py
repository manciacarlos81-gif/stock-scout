"""Deep dive on a single stock: 8 years of SEC history, recent filings, analyst views, insider trades, news."""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd
import yfinance as yf

from . import insiders, sec
from .config import DOCS_DIR, RESEARCH_DIR
from .report import DISCLAIMER, money, num, pct, score, _ok

HISTORY_TAGS = {
    "Revenue": (sec.DURATION_TAGS["revenue"] + ["SalesRevenueNet"], "USD"),
    "Operating profit": (sec.DURATION_TAGS["op_income"], "USD"),
    "Net profit": (sec.DURATION_TAGS["net_income"], "USD"),
    "Operating cash flow": (sec.ANNUAL_TAGS["cfo"], "USD"),
    "Capital spending": (sec.ANNUAL_TAGS["capex"], "USD"),
    "Dividends paid": (sec.ANNUAL_TAGS["dividends_paid"], "USD"),
    "Buybacks": (sec.ANNUAL_TAGS["buybacks"], "USD"),
    "Diluted shares": (sec.DURATION_TAGS["diluted_shares"], "shares"),
}
KEY_FORMS = {"10-K", "10-Q", "8-K", "DEF 14A", "S-1", "20-F", "6-K", "10-K/A", "SC 13D", "SC 13G"}


def _cagr(series: dict[int, float]):
    ys = [y for y, v in series.items() if v and v > 0]
    if len(ys) < 3:
        return None
    a, b = min(ys), max(ys)
    return (series[b] / series[a]) ** (1 / (b - a)) - 1


def _yahoo(t: str) -> tuple[dict, list, str]:
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    tk = yf.Ticker(t)
    try:
        info = tk.info or {}
    except Exception:
        info = {}
    news = []
    try:
        for n in (tk.news or [])[:8]:
            c = n.get("content", n)
            url = (c.get("canonicalUrl") or {}).get("url") or c.get("link") or ""
            when = (c.get("pubDate") or "")[:10]
            if c.get("title"):
                news.append(f"- {when} [{c['title']}]({url}) — {(c.get('provider') or {}).get('displayName', '')}")
    except Exception:
        pass
    earnings = ""
    try:
        cal = tk.calendar or {}
        dates = cal.get("Earnings Date") or []
        earnings = ", ".join(str(x) for x in dates)
    except Exception:
        pass
    return info, news, earnings


def deep_dive(ticker: str) -> str:
    t = ticker.upper().replace(".", "-")
    tm = sec.ticker_map()
    hit = tm[tm["ticker"] == t]
    if hit.empty:
        return f"{t}: not found in SEC's list of US-listed companies"
    cik, name = int(hit["cik"].iloc[0]), hit["name"].iloc[0]
    facts, subs = sec.company_facts(cik), sec.submissions(cik)
    info, news, earnings = _yahoo(t)

    hist = {label: sec.annual_history(facts, tags, unit) for label, (tags, unit) in HISTORY_TAGS.items()}
    if hist["Operating cash flow"]:
        hist["Free cash flow"] = {y: v - hist["Capital spending"].get(y, 0) for y, v in hist["Operating cash flow"].items()}
    years = sorted(set().union(*[set(v) for v in hist.values()]))[-8:]
    hist_md = "| | " + " | ".join(f"FY{y}" for y in years) + " |\n|---|" + "---:|" * len(years) + "\n"
    for label, series in hist.items():
        if series:
            fmt = (lambda v: f"{v / 1e6:,.0f}M") if label == "Diluted shares" else money
            hist_md += f"| {label} | " + " | ".join(fmt(series[y]) if y in series else "–" for y in years) + " |\n"
    trends = []
    for label in ("Revenue", "Net profit", "Free cash flow"):
        g = _cagr({y: hist.get(label, {}).get(y) for y in years})
        if g is not None:
            trends.append(f"- {label} grew {g:+.1%} a year on average over the period shown.")
    sh = hist["Diluted shares"]
    if len(sh) >= 3:
        y0, y1 = min(sh), max(sh)
        ch = sh[y1] / sh[y0] - 1
        trends.append(f"- Share count {'fell' if ch < 0 else 'rose'} {abs(ch):.0%} since FY{y0} "
                      f"({'buybacks — good for owners' if ch < 0 else 'dilution — each share owns less'}).")

    recent = (subs or {}).get("filings", {}).get("recent", {})
    filings = []
    for form, fdate, acc, doc, desc in zip(recent.get("form", []), recent.get("filingDate", []),
                                           recent.get("accessionNumber", []), recent.get("primaryDocument", []),
                                           recent.get("primaryDocDescription", [])):
        if form in KEY_FORMS:
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"
            filings.append(f"- {fdate} **{form}** [{desc or 'document'}]({url})")
        if len(filings) >= 12:
            break

    try:
        trades = insiders.openinsider(365, ticker=t, buys=True, sells=True, pages=1)
    except Exception:
        trades = pd.DataFrame()
    if not trades.empty:
        trades = trades.head(15)
        ins_md = "| Date | Who | Role | Type | Shares | Price | Value |\n|---|---|---|---|---:|---:|---:|\n" + "\n".join(
            f"| {r.trade_date} | {r.insider} | {r.title} | {r.type} | {r.shares:,.0f} | ${r.price:,.2f} | {money(r.value)} |"
            for r in trades.itertuples())
        buys = trades.loc[trades["type"].str.startswith("P"), "value"].sum()
        sells = trades.loc[trades["type"].str.startswith("S"), "value"].abs().sum()
        ins_md = f"Last 12 months (latest 15 shown): bought **{money(buys)}**, sold **{money(sells)}**.\n\n" + ins_md
    else:
        ins_md = "_No insider buys or sells reported in the last 12 months (or data unavailable)._"

    scan_md = "_Not in the latest scan (run `python scout.py scan`, or it was filtered out as too small/illiquid)._"
    latest = DOCS_DIR / "data.json"   # committed to the repo, sorted by overall score
    if latest.exists():
        df = pd.DataFrame(json.loads(latest.read_text())["stocks"])
        row = df[df["ticker"] == t]
        if len(row):
            r = row.iloc[0]
            rank = int(row.index[0]) + 1
            scan_md = (f"Ranked **#{rank} of {len(df):,}** in the latest scan.\n\n"
                       "| Overall | Value | Quality | Growth | Momentum | Health |\n|---:|---:|---:|---:|---:|---:|\n"
                       f"| **{score(r['composite'])}** | {score(r['value'])} | {score(r['quality'])} | {score(r['growth'])} | "
                       f"{score(r['momentum'])} | {score(r['health'])} |")
            if isinstance(r.get("flag_text"), str) and r["flag_text"]:
                scan_md += "\n\n**Watch out:** " + r["flag_text"]

    g = info.get
    price = g("currentPrice") or g("regularMarketPrice")
    target = g("targetMeanPrice")
    upside = (target / price - 1) if target and price else None
    street = [
        ("Price", f"${price:,.2f}" if price else "–"), ("Market value", money(g("marketCap"))),
        ("P/E (last 12m / next 12m)", f"{num(g('trailingPE'))} / {num(g('forwardPE'))}"),
        ("Analyst target (low / avg / high)", f"${num(g('targetLowPrice'), 0)} / ${num(target, 0)} / ${num(g('targetHighPrice'), 0)}"),
        ("Implied upside to avg target", pct(upside, True)),
        ("Analyst consensus", f"{(g('recommendationKey') or '–').replace('_', ' ')} ({g('numberOfAnalystOpinions') or 0} analysts)"),
        ("Shares sold short", pct(g("shortPercentOfFloat"))),
        ("Beta (vs. market)", num(g("beta"), 2)), ("Dividend yield", pct((g("dividendYield") or 0) / 100)),
        ("Next earnings", earnings or "–"),
    ]
    md = "\n\n".join([
        f"---\nticker: {t}\ncompany: \"{name}\"\ncik: {cik}\nupdated: {datetime.now():%Y-%m-%d}\ntags: [stock, research]\n---",
        f"# {t} — {name} (deep dive)",
        f"{g('sector') or (subs or {}).get('sicDescription', '')} · {g('industry') or ''} · "
        f"{(g('city') or '')}{', ' + g('country') if g('country') else ''}",
        DISCLAIMER,
        "## What they do", (g("longBusinessSummary") or "_No description available._")[:1500],
        "## Scout scores", scan_md,
        "## Wall Street snapshot", "| | |\n|---|---:|\n" + "\n".join(f"| {k} | {v} |" for k, v in street),
        "## 8-year history (from SEC 10-K filings)", hist_md + ("\n" + "\n".join(trends) if trends else ""),
        "## Insider trades", ins_md,
        "## Recent SEC filings", "\n".join(filings) or "_None found._",
        "## Recent news", "\n".join(news) or "_None found._",
        "## Questions to answer before buying",
        "- Why is it priced where it is — what does the market think could go wrong?\n"
        "- Is growth coming from real demand, acquisitions, or one-offs? (Check the latest 10-K's MD&A section.)\n"
        "- Can it pay its debts if a recession hits?\n"
        "- What would make you sell? Write it down now.",
        f"_Sources: SEC EDGAR, Yahoo Finance, OpenInsider. Generated {datetime.now():%Y-%m-%d %H:%M}._",
    ]) + "\n"
    RESEARCH_DIR.mkdir(exist_ok=True)
    path = RESEARCH_DIR / f"{t}.md"
    path.write_text(md)
    return f"{t}: wrote {path.relative_to(RESEARCH_DIR.parent)}"
