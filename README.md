# 🔭 Stock Scout

Scans **every US-listed stock** (~3,000 after size/liquidity filters) every weekday using only **free public data** —
no API keys, no paid services — and ranks them on value, quality, growth, momentum and financial health.
Results land in three places:

| Where | What |
|---|---|
| `reports/latest.md` | Today's report: market backdrop, sector trends, 7 stock screens, track record of past picks |
| `stocks/TICKER.md` | A note for every stock that made a screen (Obsidian-ready frontmatter) |
| `docs/index.html` | Interactive dashboard (sort, filter, search, click for details) — free on GitHub Pages |

> **Not financial advice.** It's an automatic screen. Data can be wrong or late. Use it to find ideas worth researching.

## Data sources (all free)

| Data | Source |
|---|---|
| Income statement, balance sheet, cash flow for every US filer | SEC EDGAR XBRL "frames" API |
| 8-year history, recent filings (deep dive) | SEC EDGAR companyfacts + submissions |
| Daily prices & volume | Yahoo Finance via `yfinance` |
| Stock list, sector, industry, market value | Nasdaq public screener |
| Insider open-market purchases | OpenInsider (SEC Form 4), falls back to SEC's quarterly insider data set |
| Rates, inflation, unemployment, credit spreads, VIX | FRED (St. Louis Fed) |
| Analyst targets, short interest, news (deep dive) | Yahoo Finance |

## Run it

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
export SEC_USER_AGENT="Your Name your@email.com"   # SEC asks automated tools to identify themselves
./venv/bin/python scout.py scan                     # full scan, ~5–10 min
./venv/bin/python scout.py scan --limit 300         # quick test on the 300 most-traded stocks
./venv/bin/python scout.py stock AAPL NVDA          # deep dive -> research/AAPL.md
```

Dashboard locally: `cd docs && python3 -m http.server` then open http://localhost:8000.

## The screens

| Screen | Looks for |
|---|---|
| 🏆 Top overall | Best weighted blend of all five scores |
| 💎 Quality at a fair price | Quality ≥ 75, Value ≥ 60 |
| 🪙 Deep value | Value ≥ 85 in its sector, Piotroski F-score ≥ 6, profitable |
| 🚀 Growth + momentum | Growth ≥ 80, Momentum ≥ 80 |
| 🕵️ Insiders buying | 2+ insiders or $500k+ bought on the open market in 90 days |
| 💵 Dividends & buybacks | 4%+ returned to shareholders, covered by free cash flow |
| 🎯 Quality on sale | Quality ≥ 75, 25%+ below its 52-week high |

Scores are percentiles (0–100) vs. every other stock scanned; valuation is ranked within each sector so banks
aren't compared with software companies. Tune thresholds and weights in `scout/config.py` and `scout/scoring.py`.

Every pick is logged to `data/picks_history.csv`; once a pick is 30+ days old the report shows how each screen
has done against the S&P 500 — so you can see which screens actually work instead of trusting them blindly.

## Automatic daily runs (GitHub)

`.github/workflows/scan.yml` runs the scan at 22:30 UTC every weekday and commits the results.

1. Push this folder to a GitHub repo.
2. Settings → Secrets and variables → Actions → add `SEC_USER_AGENT` = `Your Name your@email.com` (optional but recommended).
3. Settings → Pages → Deploy from branch → `main` / `docs` → your dashboard is at `https://<user>.github.io/<repo>/`
   (free Pages needs a **public** repo; private repos still get the reports, just no hosted dashboard).
4. Actions tab → daily-scan → **Run workflow** to do the first run now.

## Use it in Obsidian

Open the repo folder as a vault (or clone it inside your vault). `reports/latest.md` links to every stock note;
each note has frontmatter (`score`, `value`, `quality`, `pe`, `screens`, …) so Dataview queries work, e.g.:

````markdown
```dataview
TABLE score, value, quality, pe, screens FROM "stocks" WHERE quality >= 80 SORT score DESC
```
````

Use the Obsidian Git plugin to pull the daily updates automatically.

## Known limits

- **Foreign companies filing 20-F** (e.g. TSM, ASML) use IFRS, not US GAAP, so they're skipped by the scan
  (`scout.py stock` still works for their prices/news).
- Cash-flow numbers come from the latest **annual** report (10-Qs only give year-to-date cash flow);
  income-statement numbers are **trailing 12 months** where quarterly data allows.
- SEC data only updates when companies file, so a company's numbers can be up to ~3 months old between reports.
- Yahoo and Nasdaq endpoints are unofficial and can change; the scan falls back to the saved stock list if Nasdaq fails.
