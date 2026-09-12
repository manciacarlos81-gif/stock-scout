"""Writes the results as Markdown (reads well on GitHub and in Obsidian), CSV, and JSON for the dashboard."""
from __future__ import annotations

import json
import math
from datetime import date

import pandas as pd

from .config import DATA_DIR, DOCS_DIR, REPORTS_DIR, STOCKS_DIR
from .scoring import SCREENS

DISCLAIMER = ("> [!warning] Not financial advice\n"
              "> This is an automatic screen of public data. It can be wrong or out of date "
              "(SEC data lags filings; prices are end-of-day). Use it to find ideas worth researching, "
              "not as a buy list.")


def _ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def money(x):
    if not _ok(x):
        return "–"
    a = abs(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{'-' if x < 0 else ''}${a / div:.1f}{suf}"
    return f"${x:,.0f}"


def pct(x, signed=False):
    return f"{x:+.1%}" if signed and _ok(x) else (f"{x:.1%}" if _ok(x) else "–")


def num(x, d=1):
    return f"{x:,.{d}f}" if _ok(x) else "–"


def score(x):
    return f"{x:.0f}" if _ok(x) else "–"


def _table(df: pd.DataFrame, cols: list[tuple[str, str, callable]], link_prefix="../stocks/") -> str:
    head = "| Ticker | " + " | ".join(c[0] for c in cols) + " |"
    sep = "|---|" + "|".join("---:" if c[0] not in ("Company", "Sector", "Watch out", "Who bought") else "---"
                             for c in cols) + "|"
    lines = [head, sep]
    for _, r in df.iterrows():
        cells = [f"[{r['ticker']}]({link_prefix}{r['ticker']}.md)"]
        for _, key, f in cols:
            v = r.get(key)
            cells.append(str(f(v)).replace("|", "/"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


BASE_COLS = [("Company", "name", lambda v: (v or "")[:32]), ("Price", "price", lambda v: f"${v:,.2f}" if _ok(v) else "–"),
             ("Mkt cap", "market_cap", money), ("Score", "composite", score)]
FACTOR_COLS = [("Value", "value", score), ("Quality", "quality", score), ("Growth", "growth", score),
               ("Momentum", "momentum", score), ("Health", "health", score)]
SCREEN_COLS = {
    "top": BASE_COLS + FACTOR_COLS + [("Watch out", "flag_text", str)],
    "quality_value": BASE_COLS + [("P/E", "pe", num), ("FCF yield", "fcf_yield", pct), ("ROE", "roe", pct),
                                  ("Op margin", "op_margin", pct), ("Quality", "quality", score), ("Value", "value", score)],
    "deep_value": BASE_COLS + [("P/E", "pe", num), ("P/B", "pb", num), ("FCF yield", "fcf_yield", pct),
                               ("F-score", "piotroski", lambda v: f"{v:.0f}/9" if _ok(v) else "–"), ("Value", "value", score)],
    "growth_momentum": BASE_COLS + [("Sales growth", "rev_growth", lambda v: pct(v, True)),
                                    ("Profit growth", "ni_growth", lambda v: "+500%+" if _ok(v) and v >= 5 else pct(v, True)),
                                    ("12m return", "ret_12m", lambda v: pct(v, True)), ("P/S", "ps", num)],
    "insider_buying": BASE_COLS + [("Bought", "insider_buy_value", money), ("Buyers", "insider_buyers", lambda v: score(v)),
                                   ("Last buy", "insider_last_buy", lambda v: v if isinstance(v, str) else "–"),
                                   ("Who bought", "insider_who", lambda v: (v if isinstance(v, str) else "")[:60])],
    "income": BASE_COLS + [("Dividend yield", "dividend_yield", pct), ("Total yield", "shareholder_yield", pct),
                           ("FCF yield", "fcf_yield", pct), ("Debt/Equity", "debt_to_equity", lambda v: num(v, 2))],
    "pullback": BASE_COLS + [("Below 52w high", "pct_below_high", pct), ("RSI", "rsi14", lambda v: num(v, 0)),
                             ("ROE", "roe", pct), ("Quality", "quality", score), ("Watch out", "flag_text", str)],
}


def _index_table(market: dict) -> str:
    lines = ["| Index | Price | 1 month | 12 months | vs. 200-day avg |", "|---|---:|---:|---:|---:|"]
    for m in market.get("indexes", []):
        lines.append(f"| {m['label']} | ${m['price']:,.2f} | {pct(m['ret_1m'], True)} | {pct(m['ret_12m'], True)} | "
                     f"{pct(m['vs_sma200'], True)} |")
    return "\n".join(lines)


def _macro_table(macro: list[dict]) -> str:
    if not macro:
        return "_Economic data (FRED) unavailable this run._"
    lines = ["| Measure | Now | A year ago |", "|---|---:|---:|"]
    for m in macro:
        fmt = (lambda v: f"{v:.2f}%") if m["kind"] in ("pct", "yoy") else (lambda v: f"{v:.2f}")
        lines.append(f"| {m['label']} | {fmt(m['value'])} | {fmt(m['year_ago']) if m['year_ago'] is not None else '–'} |")
    return "\n".join(lines)


def _sector_table(market: dict) -> str:
    lines = ["| Sector | 1 month | 3 months | 6 months | Above 200-day avg? |", "|---|---:|---:|---:|:---:|"]
    for s in market.get("sectors", []):
        lines.append(f"| {s['name']} ({s['etf']}) | {pct(s['ret_1m'], True)} | {pct(s['ret_3m'], True)} | "
                     f"{pct(s['ret_6m'], True)} | {'✅' if s['above_200'] else '❌'} |")
    return "\n".join(lines)


def _track_record(perf: pd.DataFrame) -> str:
    if perf.empty:
        return "_Not enough history yet — picks are scored once they're at least 30 days old._"
    lines = ["| Screen | Picks judged | Avg return | S&P 500 same period | Beat the S&P |", "|---|---:|---:|---:|---:|"]
    titles = {s["key"]: s["title"] for s in SCREENS}
    for _, r in perf.iterrows():
        lines.append(f"| {titles.get(r['screen'], r['screen'])} | {r['n']:.0f} | {pct(r['ret'], True)} | "
                     f"{pct(r['spy_ret'], True)} | {pct(r['beat'])} |")
    return "\n".join(lines)


def write_report(d, screens, macro, market, perf, meta) -> str:
    today = meta["date"]
    parts = [
        f"---\ndate: {today}\ntype: stock-scan\nstocks_scanned: {meta['scanned']}\n---",
        f"# Stock Scout — {date.fromisoformat(today):%d %b %Y}",
        DISCLAIMER,
        f"Scanned **{meta['scanned']:,}** US-listed companies (market value ≥ $300M, actively traded) using "
        f"SEC filings, daily prices, insider trades and economic data. Every score is 0–100: "
        f"**how this stock ranks against all the others** (value is ranked within its own sector).",
        "## Market backdrop", _index_table(market), "### Economy", _macro_table(macro),
        "### Sector trends", _sector_table(market),
        "## Screens",
    ]
    for s in SCREENS:
        hit = screens[s["key"]]
        parts.append(f"### {s['title']}\n_{s['why']}_\n")
        parts.append(_table(hit, SCREEN_COLS[s["key"]]) if len(hit) else "_Nothing passes this screen today._")
    parts += [
        "## How past picks did", _track_record(perf),
        "## What the scores mean",
        "- **Value** — how cheap vs. sector peers: profit, free cash flow, operating profit vs. company value, sales and book value.\n"
        "- **Quality** — return on equity, gross profit vs. assets, margins, free-cash-flow margin, Piotroski F-score, "
        "and whether profits are backed by cash.\n"
        "- **Growth** — sales and profit growth over the last 12 months vs. the 12 months before, minus share dilution.\n"
        "- **Momentum** — 12-month return (skipping the latest month), 6-month return, and position vs. the 200-day average.\n"
        "- **Health** — debt, ability to pay interest, short-term cash cushion, bankruptcy risk (Altman Z), and how jumpy the price is.\n"
        f"- **Score** — weighted mix: {', '.join(f'{k} {v:.0%}' for k, v in meta['weights'].items())}.",
        "## Data sources",
        "- Financial statements: SEC EDGAR XBRL (company filings, trailing-12-months where quarterly data exists)\n"
        "- Prices & volume: Yahoo Finance (via `yfinance`)\n"
        "- Stock list, sectors, market value: Nasdaq public screener\n"
        f"- Insider purchases: {meta['insider_source']}\n"
        "- Economy: FRED (Federal Reserve Bank of St. Louis)",
    ]
    md = "\n\n".join(parts) + "\n"
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / f"{today}.md").write_text(md)
    (REPORTS_DIR / "latest.md").write_text(md.replace("](../stocks/", "](../stocks/"))
    return md


def _plain_summary(r) -> list[str]:
    s = []
    sec = r.get("sector") or "its sector"
    if _ok(r.get("value")):
        s.append(f"Cheaper than about {r['value']:.0f}% of stocks in {sec} (value score)." if r["value"] >= 50
                 else f"More expensive than about {100 - r['value']:.0f}% of {sec} peers (value score).")
    if _ok(r.get("quality")):
        s.append(f"Business quality ranks above {r['quality']:.0f}% of all stocks scanned.")
    if _ok(r.get("rev_growth")):
        s.append(f"Sales {'grew' if r['rev_growth'] >= 0 else 'fell'} {abs(r['rev_growth']):.0%} over the last 12 months.")
    if _ok(r.get("ret_12m")):
        s.append(f"Share price {'up' if r['ret_12m'] >= 0 else 'down'} {abs(r['ret_12m']):.0%} over 12 months "
                 f"({r.get('pct_below_high', 0):.0%} below its 52-week high).")
    if _ok(r.get("insider_buy_value")) and r["insider_buy_value"] > 0:
        s.append(f"Insiders bought {money(r['insider_buy_value'])} of shares recently ({r.get('insider_who')}).")
    return s


def write_stock_notes(d: pd.DataFrame, screens: dict, today: str):
    STOCKS_DIR.mkdir(exist_ok=True)
    member: dict[str, list[str]] = {}
    titles = {s["key"]: s["title"] for s in SCREENS}
    for key, hit in screens.items():
        for t in hit["ticker"]:
            member.setdefault(t, []).append(titles[key])
    rows = d[d["ticker"].isin(member)]
    for old in STOCKS_DIR.glob("*.md"):  # a stock that left every screen shouldn't keep a stale note
        if old.stem not in member:
            old.unlink()
    for _, r in rows.iterrows():
        t = r["ticker"]
        fm = {"ticker": t, "company": r["name"], "sector": r["sector"], "industry": r["industry"],
              "price": round(r["price"], 2), "market_cap": money(r["market_cap"]),
              "score": round(r["composite"]) if _ok(r["composite"]) else None,
              **{k: round(r[k]) if _ok(r[k]) else None for k in ("value", "quality", "growth", "momentum", "health")},
              "pe": round(r["pe"], 1) if _ok(r["pe"]) else None,
              "piotroski": int(r["piotroski"]) if _ok(r["piotroski"]) else None, "updated": today}
        q = lambda v: json.dumps(v, ensure_ascii=False)
        front = "---\n" + "\n".join(f"{k}: {q(v) if isinstance(v, str) else ('null' if v is None else v)}"
                                    for k, v in fm.items())
        front += "\nscreens:\n" + "\n".join(f"  - {q(x)}" for x in member[t]) + "\ntags: [stock]\n---"
        kv = [
            ("Price", f"${r['price']:,.2f}"), ("Market value", money(r["market_cap"])),
            ("P/E", num(r["pe"])), ("Price/Sales", num(r["ps"], 2)), ("Price/Book", num(r["pb"], 2)),
            ("Free-cash-flow yield", pct(r["fcf_yield"])), ("Dividend yield", pct(r["dividend_yield"])),
            ("Sales (12m)", money(r["revenue"])), ("Sales growth", pct(r["rev_growth"], True)),
            ("Net profit (12m)", money(r["net_income"])),
            ("Profit growth", "+500%+" if _ok(r["ni_growth"]) and r["ni_growth"] >= 5 else pct(r["ni_growth"], True)),
            ("Gross margin", pct(r["gross_margin"])), ("Operating margin", pct(r["op_margin"])),
            ("Return on equity", pct(r["roe"])), ("Debt / equity", num(r["debt_to_equity"], 2)),
            ("Current ratio", num(r["current_ratio"], 2)), ("Piotroski F-score", f"{r['piotroski']:.0f}/9" if _ok(r["piotroski"]) else "–"),
            ("Altman Z", num(r["altman_z"], 2)), ("Share count change (1y)", pct(r["dilution"], True)),
            ("Return 1m / 6m / 12m", f"{pct(r['ret_1m'], True)} / {pct(r['ret_6m'], True)} / {pct(r['ret_12m'], True)}"),
            ("vs. 200-day average", pct(r["vs_sma200"], True)), ("RSI (14d)", num(r["rsi14"], 0)),
            ("Volatility (1y)", pct(r["volatility"])), ("Financials as of", str(r.get("fin_asof") or "–")),
        ]
        body = [front, f"# {t} — {r['name']}", f"{r['sector']} · {r['industry']}",
                "**In screens today:** " + ", ".join(member[t]),
                "## In plain English", "\n".join(f"- {x}" for x in _plain_summary(r)),
                "## Scores (0–100, higher is better)",
                "| Overall | Value | Quality | Growth | Momentum | Health |\n|---:|---:|---:|---:|---:|---:|\n"
                f"| **{score(r['composite'])}** | {score(r['value'])} | {score(r['quality'])} | {score(r['growth'])} | "
                f"{score(r['momentum'])} | {score(r['health'])} |",
                "## Key numbers", "| | |\n|---|---:|\n" + "\n".join(f"| {k} | {v} |" for k, v in kv)]
        if r["flag_text"]:
            body += ["## Watch out", "\n".join(f"- {x}" for x in r["flag_text"].split("; "))]
        body += ["## Dig deeper",
                 f"- SEC filings: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(r['cik'])}&type=&dateb=&owner=include&count=40\n"
                 f"- Yahoo Finance: https://finance.yahoo.com/quote/{t}\n"
                 f"- Insider trades: http://openinsider.com/{t}\n"
                 f"- Full deep dive: run `python scout.py stock {t}` → `research/{t}.md`",
                 f"_Updated {today} · [latest report](../reports/latest.md)_"]
        (STOCKS_DIR / f"{t}.md").write_text("\n\n".join(body) + "\n")


DASH_COLS = ["ticker", "name", "sector", "industry", "cik", "price", "market_cap", "composite", "value", "quality",
             "growth", "momentum", "health", "pe", "ps", "pb", "fcf_yield", "dividend_yield", "shareholder_yield",
             "roe", "gross_margin", "op_margin", "rev_growth", "ni_growth", "debt_to_equity", "piotroski", "altman_z",
             "ret_1m", "ret_6m", "ret_12m", "pct_below_high", "vs_sma200", "rsi14", "volatility",
             "insider_buy_value", "insider_buyers", "flag_text"]


def write_alert_feed(d: pd.DataFrame, screens: dict, meta):
    """Small file the Cloudflare alert worker reads: today's top stocks, newcomers, fresh insider buying."""
    path = DOCS_DIR / "alerts.json"
    prev = json.loads(path.read_text()) if path.exists() else {}
    prev_top = {s["ticker"] for s in prev.get("top", [])} if prev.get("date") != meta["date"] else \
               set(prev.get("prev_top", []))
    top = screens["top"].head(10)
    recent = str((pd.Timestamp(meta["date"]) - pd.Timedelta(days=5)).date())
    ins = screens["insider_buying"]
    ins = ins[ins["insider_last_buy"].fillna("") >= recent]
    feed = {
        "date": meta["date"], "scanned": meta["scanned"],
        "top": [{"rank": i + 1, "ticker": r["ticker"], "name": r["name"], "score": round(r["composite"]),
                 "price": round(r["price"], 2), "flags": r["flag_text"] if isinstance(r["flag_text"], str) else ""}
                for i, (_, r) in enumerate(top.iterrows())],
        "new_top": [t for t in top["ticker"] if prev_top and t not in prev_top],
        "prev_top": sorted(prev_top),
        "insider": [{"ticker": r["ticker"], "name": r["name"], "value": round(r["insider_buy_value"]),
                     "buyers": int(r["insider_buyers"]), "last": r["insider_last_buy"]} for _, r in ins.iterrows()],
    }
    path.write_text(json.dumps(feed, ensure_ascii=False, indent=1))


def write_data(d: pd.DataFrame, screens: dict, macro, market, perf, meta):
    DATA_DIR.mkdir(exist_ok=True)
    d.to_csv(DATA_DIR / "latest.csv", index=False)
    rows = d[DASH_COLS].copy()
    for c in rows.select_dtypes("number").columns:
        rows[c] = rows[c].round(4)
    records = json.loads(rows.to_json(orient="records"))
    payload = {"meta": meta, "macro": macro, "market": market,
               "screens": [{"key": s["key"], "title": s["title"], "why": s["why"],
                            "tickers": screens[s["key"]]["ticker"].tolist()} for s in SCREENS],
               "track_record": json.loads(perf.to_json(orient="records")) if not perf.empty else [],
               "stocks": records}
    DOCS_DIR.mkdir(exist_ok=True)
    write_alert_feed(d, screens, meta)
    # One stock per line so the daily git commit stores only changed lines, not a new 2 MB blob
    stocks = payload.pop("stocks")
    head = json.dumps(payload, separators=(",", ":"))[:-1]
    body = ",\n".join(json.dumps(s, separators=(",", ":")) for s in stocks)
    (DOCS_DIR / "data.json").write_text(f'{head},"stocks":[\n{body}\n]}}\n')
