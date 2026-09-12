"""SEC EDGAR data (free, official, no key).

The scan uses XBRL "frames": one request returns a single line item (e.g. revenue) for every
US-listed company for one period, so the whole market's financials take a few hundred requests.
Deep dives use per-company "companyfacts" and "submissions".
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import pandas as pd
import requests

from .config import CACHE_DIR, SEC_UA

_session = requests.Session()
_session.headers.update({"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
_last_call = 0.0
YEAR = timedelta(days=365)


def get_json(url: str, max_age_hours: float = 20):
    """GET with an on-disk cache and SEC's 10-requests-per-second limit. Returns None on 404."""
    global _last_call
    path = CACHE_DIR / "sec" / url.split("://", 1)[1].replace("/", "_")
    if path.exists() and time.time() - path.stat().st_mtime < max_age_hours * 3600:
        return json.loads(path.read_text())
    for attempt in range(5):
        wait = 0.12 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = _session.get(url, timeout=60)
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (200, 404):
            break
        if r.status_code == 403 and "Undeclared" in r.text:
            raise RuntimeError("SEC rejected the User-Agent. Set SEC_USER_AGENT to 'Your Name your@email.com'.")
        time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"SEC request kept failing: {url}")
    data = r.json() if r.status_code == 200 else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def ticker_map() -> pd.DataFrame:
    d = get_json("https://www.sec.gov/files/company_tickers_exchange.json", max_age_hours=72)
    df = pd.DataFrame(d["data"], columns=d["fields"])
    df["ticker"] = df["ticker"].str.upper().str.replace(".", "-", regex=False)
    return df


# ---------------------------------------------------------------- frames (whole market)

DURATION_TAGS = {  # line item -> XBRL tags, most preferred first
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "cost_of_revenue": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "gross_profit": ["GrossProfit"],
    "op_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}
# Cash-flow statements in 10-Qs are year-to-date only, so these come from annual reports.
ANNUAL_TAGS = {
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "dividends_paid": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating"],
}
INSTANT_TAGS = {
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt", "LongTermDebtAndCapitalLeaseObligations",
                "SeniorNotes", "ConvertibleNotesPayable"],
    "st_debt": ["LongTermDebtCurrent", "DebtCurrent"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
}


def _quarters(today: date, n: int = 12) -> list[tuple[int, int]]:
    """The last n calendar quarters that ended at least 45 days ago (enough time for 10-Qs), oldest first."""
    d = today - timedelta(days=45)
    y, q = d.year, (d.month - 1) // 3
    if q == 0:
        y, q = y - 1, 4
    out = []
    for _ in range(n):
        out.append((y, q))
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    return out[::-1]


def _d(s):
    return date.fromisoformat(s) if s else None


def _collect(tags, unit, periods, taxonomy="us-gaap") -> dict[int, list[tuple]]:
    """cik -> sorted [(start, end, value)]. Per company, the tag with the newest data leads and the
    other tags fill in periods it lacks (companies often tag quarters and years differently)."""
    per_tag = []
    for tag in tags:
        seen: dict[int, dict] = {}
        for p in periods:
            d = get_json(f"https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{p}.json")
            for f in (d or {}).get("data", []):
                seen.setdefault(f["cik"], {})[(f.get("start"), f["end"])] = f["val"]
        per_tag.append(seen)
    out = {}
    for cik in set().union(*per_tag):
        have = [s[cik] for s in per_tag if cik in s]  # priority order
        have.sort(key=lambda facts: max(e for _, e in facts), reverse=True)  # stable: ties keep priority
        merged: dict = {}
        for facts in have:
            for k, v in facts.items():
                merged.setdefault(k, v)
        out[cik] = sorted(((_d(s), _d(e), v) for (s, e), v in merged.items()), key=lambda x: x[1])
    return out


def _days(f):
    return (f[1] - f[0]).days if f[0] else None


def _is_q(f):
    d = _days(f)
    return d is not None and 80 <= d <= 100


def _is_a(f):
    d = _days(f)
    return d is not None and 350 <= d <= 380


def _near(a, b, tol=20):
    return abs((a - b).days) <= tol


def _ttm_at(facts, end):
    """Trailing 12 months ending near `end`: last annual + later quarters - the same quarters a year earlier."""
    annuals = [f for f in facts if _is_a(f) and f[1] <= end + timedelta(days=20)]
    if not annuals:
        return None
    base = annuals[-1]
    quarters = [f for f in facts if _is_q(f)]
    post = [q for q in quarters if base[1] + timedelta(days=20) < q[1] <= end + timedelta(days=20)]
    n = round((end - base[1]).days / 91.3)
    if n > 3 or len(post) != n:
        return None
    total = base[2]
    for q in post:
        prior = [p for p in quarters if _near(p[1], q[1] - YEAR)]
        if not prior:
            return None
        total += q[2] - prior[0][2]
    return total


def ttm(facts):
    """(ttm value, ttm value one year earlier, as-of date)."""
    ends = sorted({f[1] for f in facts if _is_q(f) or _is_a(f)}, reverse=True)
    for end in ends[:4]:
        cur = _ttm_at(facts, end)
        if cur is not None:
            return cur, _ttm_at(facts, end - YEAR), end
    return None, None, None


def annual(facts):
    """(latest annual fact, the annual fact before it) — facts are (start, end, value)."""
    a = [f for f in facts if _is_a(f)]
    if not a:
        return None, None
    prior = next((f for f in reversed(a[:-1]) if _near(f[1], a[-1][1] - YEAR, 30)), None)
    return a[-1], prior


def latest_avg(facts):
    """Latest period average (e.g. share count) and the value a year before it."""
    f = [x for x in facts if _is_q(x) or _is_a(x)]
    if not f:
        return None, None
    last = f[-1]
    prior = next((x for x in reversed(f) if _near(x[1], last[1] - YEAR, 30)), None)
    return last[2], prior[2] if prior else None


def instant_at(facts, when, tol=20):
    hits = [f for f in facts if _near(f[1], when, tol)]
    return hits[-1][2] if hits else None


def fundamentals(today: date | None = None, verbose=True) -> pd.DataFrame:
    """One row per company (CIK) with trailing-12-month, annual and latest balance-sheet figures."""
    today = today or date.today()
    qs = _quarters(today)
    years = sorted({y for y, _ in qs})[-4:]
    dur_periods = [f"CY{y}Q{q}" for y, q in qs] + [f"CY{y}" for y in years]
    inst_periods = [f"CY{y}Q{q}I" for y, q in qs]
    y, q = qs[-1]
    nxt = (y + 1, 1) if q == 4 else (y, q + 1)
    share_periods = inst_periods[-4:] + [f"CY{nxt[0]}Q{nxt[1]}I"]

    log = print if verbose else (lambda *a, **k: None)
    log(f"  SEC frames: quarters {dur_periods[0]}..{dur_periods[len(qs) - 1]}, years {years}")
    dur = {k: _collect(t, "shares" if k == "diluted_shares" else "USD", dur_periods) for k, t in DURATION_TAGS.items()}
    ann = {k: _collect(t, "USD", [f"CY{y}" for y in years]) for k, t in ANNUAL_TAGS.items()}
    inst = {k: _collect(t, "USD", inst_periods) for k, t in INSTANT_TAGS.items()}
    shares_out = _collect(["EntityCommonStockSharesOutstanding"], "shares", share_periods, taxonomy="dei")

    ciks = set().union(*dur.values(), *inst.values())
    rows = []
    for cik in ciks:
        r = {"cik": cik}
        for k in ("revenue", "cost_of_revenue", "gross_profit", "op_income", "net_income"):
            facts = dur[k].get(cik, [])
            r[k], r[f"{k}_prior"], asof = ttm(facts)
            if k in ("revenue", "net_income") and asof:
                r[f"{k}_asof"] = asof
            last, prior = annual(facts)
            r[f"{k}_annual"] = last[2] if last else None
            r[f"{k}_annual_prior"] = prior[2] if prior else None
            if k == "net_income" and last:
                r["annual_end"] = last[1]
                r["annual_prior_end"] = prior[1] if prior else None
        if r["gross_profit"] is None and r["revenue"] is not None and r["cost_of_revenue"] is not None:
            r["gross_profit"] = r["revenue"] - r["cost_of_revenue"]
            if r["revenue_prior"] is not None and r["cost_of_revenue_prior"] is not None:
                r["gross_profit_prior"] = r["revenue_prior"] - r["cost_of_revenue_prior"]
        for k in ("gross_profit",):
            if r[f"{k}_annual"] is None and r["revenue_annual"] is not None and r["cost_of_revenue_annual"] is not None:
                r[f"{k}_annual"] = r["revenue_annual"] - r["cost_of_revenue_annual"]
                if r["revenue_annual_prior"] is not None and r["cost_of_revenue_annual_prior"] is not None:
                    r[f"{k}_annual_prior"] = r["revenue_annual_prior"] - r["cost_of_revenue_annual_prior"]
        r["diluted_shares"], r["diluted_shares_prior"] = latest_avg(dur["diluted_shares"].get(cik, []))

        for k in ANNUAL_TAGS:
            last, prior = annual(ann[k].get(cik, []))
            r[k] = last[2] if last else None
            r[f"{k}_prior"] = prior[2] if prior else None
        for k in INSTANT_TAGS:
            facts = inst[k].get(cik, [])
            r[k] = facts[-1][2] if facts else None
            if k == "assets" and facts:
                r["balance_sheet_asof"] = facts[-1][1]
            # values at the last two fiscal year-ends, for year-over-year checks (Piotroski)
            if r.get("annual_end"):
                r[f"{k}_fy"] = instant_at(facts, r["annual_end"])
                if r.get("annual_prior_end"):
                    r[f"{k}_fy_prior"] = instant_at(facts, r["annual_prior_end"])
        s = shares_out.get(cik, [])
        r["shares_outstanding"] = s[-1][2] if s else None
        rows.append(r)
    df = pd.DataFrame(rows)
    df["piotroski"] = df.apply(_piotroski, axis=1)
    return df


def _piotroski(r):
    """Piotroski F-score (0-9): nine yes/no checks that a company's finances are improving.
    Scaled to 9 when a few inputs are missing; None when fewer than 6 checks can be run."""
    def v(k):
        x = r.get(k)
        return None if x is None or pd.isna(x) else x

    ni, ni_p = v("net_income_annual"), v("net_income_annual_prior")
    a, a_p = v("assets_fy"), v("assets_fy_prior")
    cfo = v("cfo")
    checks = []

    def add(ok):
        if ok is not None:
            checks.append(bool(ok))

    roa = ni / a if ni is not None and a else None
    roa_p = ni_p / a_p if ni_p is not None and a_p else None
    add(roa > 0 if roa is not None else None)
    add(cfo > 0 if cfo is not None else None)
    add(roa > roa_p if roa is not None and roa_p is not None else None)
    add(cfo > ni if cfo is not None and ni is not None else None)
    ltd, ltd_p = v("lt_debt_fy"), v("lt_debt_fy_prior")
    if a and a_p:
        add((ltd or 0) / a <= (ltd_p or 0) / a_p)
    ca, cl, ca_p, cl_p = v("current_assets_fy"), v("current_liabilities_fy"), v("current_assets_fy_prior"), v("current_liabilities_fy_prior")
    if ca and cl and ca_p and cl_p:
        add(ca / cl > ca_p / cl_p)
    sh, sh_p = v("diluted_shares"), v("diluted_shares_prior")
    add(sh <= sh_p * 1.005 if sh and sh_p else None)
    gp, gp_p, rev, rev_p = v("gross_profit_annual"), v("gross_profit_annual_prior"), v("revenue_annual"), v("revenue_annual_prior")
    if gp is not None and gp_p is not None and rev and rev_p:
        add(gp / rev > gp_p / rev_p)
    if rev and rev_p and a and a_p:
        add(rev / a > rev_p / a_p)
    if len(checks) < 6:
        return None
    return round(sum(checks) / len(checks) * 9)


# ---------------------------------------------------------------- per company (deep dive)

def company_facts(cik: int):
    return get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", max_age_hours=24)


def submissions(cik: int):
    return get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", max_age_hours=12)


def annual_history(facts_json, tags, unit="USD", years=8) -> dict[int, float]:
    """Fiscal year -> value from 10-K filings. The tag with the newest data wins; older tags fill gaps."""
    gaap = (facts_json or {}).get("facts", {}).get("us-gaap", {})
    per_tag = []
    for tag in tags:
        found = {}
        for f in gaap.get(tag, {}).get("units", {}).get(unit, []):
            if f.get("form") not in ("10-K", "10-K/A") or not f.get("start"):
                continue
            s, e = _d(f["start"]), _d(f["end"])
            if 350 <= (e - s).days <= 380:
                found[e.year if e.month > 3 else e.year - 1] = f["val"]  # fiscal-year label
        if found:
            per_tag.append(found)
    merged: dict[int, float] = {}
    for found in sorted(per_tag, key=lambda f: (max(f), len(f)), reverse=True):
        for y, v in found.items():
            merged.setdefault(y, v)
    return dict(sorted(merged.items())[-years:])
