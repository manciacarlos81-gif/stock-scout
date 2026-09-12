"""Turns raw numbers into ratios, 0-100 factor scores, red flags and screens."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import TOP_N, WEIGHTS

FINANCIAL_SECTORS = {"Finance", "Real Estate"}


def _div(a, b):
    a, b = pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")
    return (a / b.where(b != 0)).replace([np.inf, -np.inf], np.nan)


def add_ratios(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    fin = d["sector"].isin(FINANCIAL_SECTORS)
    # Missing debt tags usually mean "tagged some other way", not "no debt" -> leave ratios blank, don't reward it
    debt_known = d["lt_debt"].notna() | d["st_debt"].notna()
    d["total_debt"] = d[["lt_debt", "st_debt"]].sum(axis=1, min_count=1)
    d["ev"] = d["market_cap"] + d["total_debt"].fillna(0) - d["cash"].fillna(0)
    # Banks/insurers/brokers: operating cash flow swings with loans and deposits, so "free cash flow" means nothing
    d["fcf"] = (d["cfo"] - d["capex"].fillna(0)).where(d["sector"] != "Finance")

    d["pe"] = _div(d["market_cap"], d["net_income"]).where(d["net_income"] > 0)
    d["ps"] = _div(d["market_cap"], d["revenue"])
    d["pb"] = _div(d["market_cap"], d["equity"]).where(d["equity"] > 0)
    d["earnings_yield"] = _div(d["net_income"], d["market_cap"])
    d["fcf_yield"] = _div(d["fcf"], d["market_cap"])
    d["sales_yield"] = _div(d["revenue"], d["market_cap"])
    d["book_yield"] = _div(d["equity"], d["market_cap"])
    d["ebit_ev"] = _div(d["op_income"], d["ev"]).where(d["ev"] > 0)

    d["roe"] = _div(d["net_income"], d["equity"]).where(d["equity"] > 0)
    d["roa"] = _div(d["net_income"], d["assets"])
    d["gross_profitability"] = _div(d["gross_profit"], d["assets"])
    d["gross_margin"] = _div(d["gross_profit"], d["revenue"])
    d["op_margin"] = _div(d["op_income"], d["revenue"])
    d["net_margin"] = _div(d["net_income"], d["revenue"])
    d["fcf_margin"] = _div(d["fcf"], d["revenue_annual"])
    d["accruals"] = _div(d["net_income_annual"] - d["cfo"], d["assets"]).where(d["sector"] != "Finance")  # profit not backed by cash

    d["rev_growth"] = (_div(d["revenue"], d["revenue_prior"]) - 1).where(d["revenue_prior"] > 0)
    d["ni_growth"] = _div(d["net_income"] - d["net_income_prior"], d["net_income_prior"].abs()).clip(-3, 5)
    d["op_income_growth"] = _div(d["op_income"] - d["op_income_prior"], d["op_income_prior"].abs()).clip(-3, 5)
    d["dilution"] = _div(d["diluted_shares"], d["diluted_shares_prior"]) - 1

    d["debt_to_equity"] = _div(d["total_debt"], d["equity"]).where(d["equity"] > 0)
    d["current_ratio"] = _div(d["current_assets"], d["current_liabilities"])
    d["interest_coverage"] = _div(d["op_income"], d["interest_expense"]).where(d["interest_expense"] > 0).clip(-20, 50)
    d.loc[d["interest_expense"].fillna(0).le(0) & d["op_income"].gt(0) & debt_known
          & d["total_debt"].eq(0), "interest_coverage"] = 50
    d["equity_to_assets"] = _div(d["equity"], d["assets"])
    d["dividend_yield"] = _div(d["dividends_paid"].fillna(0), d["market_cap"])
    d["shareholder_yield"] = _div(d["dividends_paid"].fillna(0) + d["buybacks"].fillna(0), d["market_cap"])
    d["insider_buy_pct"] = _div(d["insider_buy_value"].fillna(0), d["market_cap"])

    # Altman Z-score: bankruptcy-risk model for non-financial companies (< 1.8 = distress zone)
    ta = d["assets"]
    z = (1.2 * _div(d["current_assets"] - d["current_liabilities"], ta)
         + 1.4 * _div(d["retained_earnings"], ta)
         + 3.3 * _div(d["op_income"], ta)
         + 0.6 * _div(d["market_cap"], d["liabilities"])
         + 1.0 * _div(d["revenue"], ta))
    d["altman_z"] = z.where(~fin).clip(-10, 30)
    return d


def _pct(s: pd.Series, higher_is_better=True) -> pd.Series:
    return s.rank(pct=True, ascending=higher_is_better) * 100


def _sector_pct(d: pd.DataFrame, col: str, higher_is_better=True, min_peers=15) -> pd.Series:
    """Percentile vs. sector peers (fair for valuation: banks and software shouldn't be compared directly)."""
    overall = _pct(d[col], higher_is_better)
    by_sector = d.groupby("sector")[col].rank(pct=True, ascending=higher_is_better) * 100
    peers = d.groupby("sector")[col].transform("count")
    return by_sector.where(peers >= min_peers, overall)


def _combine(parts: dict[str, tuple[pd.Series, float]]) -> pd.Series:
    vals = pd.DataFrame({k: s for k, (s, _) in parts.items()})
    w = pd.Series({k: wt for k, (_, wt) in parts.items()})
    have = vals.notna()
    score = (vals.fillna(0) * w).sum(axis=1) / (have * w).sum(axis=1)
    return score.where(have.sum(axis=1) >= max(1, len(parts) // 2))


def add_scores(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    fin = d["sector"].isin(FINANCIAL_SECTORS)
    d["value"] = _combine({
        "ey": (_sector_pct(d, "earnings_yield"), 1.0),
        "fcf": (_sector_pct(d, "fcf_yield"), 1.0),
        "ebit": (_sector_pct(d, "ebit_ev"), 1.0),
        "sales": (_sector_pct(d, "sales_yield"), 0.5),
        "book": (_sector_pct(d, "book_yield"), 0.5),
    })
    d["quality"] = _combine({
        "roe": (_pct(d["roe"].clip(upper=1.0)), 1.0),
        "gp": (_pct(d["gross_profitability"]).where(~fin), 1.0),
        "opm": (_sector_pct(d, "op_margin"), 1.0),
        "fcfm": (_sector_pct(d, "fcf_margin"), 1.0),
        "f": (_pct(d["piotroski"]), 1.0),
        "accr": (_pct(d["accruals"], higher_is_better=False), 0.5),
    })
    d["growth"] = _combine({
        "rev": (_pct(d["rev_growth"]), 1.0),
        "ni": (_pct(d["ni_growth"]), 0.75),
        "op": (_pct(d["op_income_growth"]), 0.75),
        "dil": (_pct(d["dilution"], higher_is_better=False), 0.25),
    })
    d["momentum"] = _combine({
        "m12": (_pct(d["mom_12_1"]), 1.0),
        "m6": (_pct(d["ret_6m"]), 1.0),
        "trend": (_pct(d["vs_sma200"]), 0.5),
    })
    nonfin_health = _combine({
        "de": (_pct(d["debt_to_equity"], higher_is_better=False), 1.0),
        "cr": (_pct(d["current_ratio"].clip(upper=5)), 0.5),
        "ic": (_pct(d["interest_coverage"]), 1.0),
        "z": (_pct(d["altman_z"]), 1.0),
        "vol": (_pct(d["volatility"], higher_is_better=False), 0.5),
    })
    fin_health = _combine({
        "eqa": (_sector_pct(d, "equity_to_assets"), 1.0),
        "vol": (_pct(d["volatility"], higher_is_better=False), 0.5),
    })
    d["health"] = nonfin_health.where(~fin, fin_health)
    # A factor we can't measure counts as average (50) — otherwise thin data would rank above full data
    d["composite"] = sum(d[k].fillna(50) * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values())
    d.loc[d[list(WEIGHTS)].notna().sum(axis=1) < 3, "composite"] = np.nan
    return d


def flags(r) -> list[str]:
    f = []
    def v(k):
        x = r.get(k)
        return None if x is None or pd.isna(x) else x
    if (v("net_income") or 0) < 0: f.append("losing money")
    if v("fcf") is not None and v("fcf") < 0: f.append("burning cash")
    if (v("dilution") or 0) > 0.08: f.append(f"shares up {v('dilution'):.0%} in a year")
    if v("altman_z") is not None and v("altman_z") < 1.8: f.append("bankruptcy-risk zone (Altman Z)")
    if (v("debt_to_equity") or 0) > 2.5: f.append("heavy debt")
    if v("equity") is not None and v("equity") < 0: f.append("negative equity")
    if (v("accruals") or 0) > 0.10: f.append("profits not backed by cash")
    if (v("rev_growth") or 0) < -0.15: f.append("sales shrinking fast")
    if (v("data_age_days") or 0) > 200: f.append("financials over 6 months old")
    if (v("vs_sma200") or 0) < -0.2: f.append("well below 200-day average")
    return f


SCREENS = [
    {"key": "top", "title": "🏆 Top overall",
     "why": "Best all-round blend: cheap, profitable, growing, rising and financially healthy.",
     "filter": lambda d: d["composite"].notna() & d["quality"].notna() & d["growth"].notna()
                         & (d["altman_z"].fillna(3) >= 1.8),
     "sort": lambda d: d["composite"], "n": 25},
    {"key": "quality_value", "title": "💎 Quality at a fair price",
     "why": "Highly profitable, well-run businesses that aren't expensive vs. their sector.",
     "filter": lambda d: (d["quality"] >= 75) & (d["value"] >= 60) & (d["health"] >= 40),
     "sort": lambda d: d["quality"] + d["value"]},
    {"key": "deep_value", "title": "🪙 Deep value",
     "why": "Among the cheapest in their sector but with improving finances (Piotroski ≥ 6), "
            "so less likely to be cheap for a bad reason.",
     "filter": lambda d: (d["value"] >= 85) & (d["piotroski"] >= 6) & (d["health"] >= 40) & (d["net_income"] > 0),
     "sort": lambda d: d["value"]},
    {"key": "growth_momentum", "title": "🚀 Growth + momentum",
     "why": "Fast-growing sales and profits with a rising share price. Riskier — often not cheap.",
     "filter": lambda d: (d["growth"] >= 80) & (d["momentum"] >= 80) & (d["quality"] >= 40),
     "sort": lambda d: d["growth"] + d["momentum"]},
    {"key": "insider_buying", "title": "🕵️ Insiders buying",
     "why": "Two or more executives/directors (or $500k+ total) bought shares with their own money "
            "on the open market recently. Insiders sell for many reasons, but buy for one.",
     "filter": lambda d: ((d["insider_buyers"] >= 2) | (d["insider_buy_value"] >= 5e5)) & (d["composite"] >= 40),
     "sort": lambda d: d["insider_buy_pct"]},
    {"key": "income", "title": "💵 Dividends & buybacks",
     "why": "Returns 4%+ of its market value to shareholders a year, paid for by real free cash flow.",
     "filter": lambda d: (d["shareholder_yield"] >= 0.04)
                         & (d["fcf"].fillna(d["net_income"]) >= d["dividends_paid"].fillna(0))
                         & (d["health"] >= 50) & (d["quality"] >= 40),
     "sort": lambda d: d["shareholder_yield"]},
    {"key": "pullback", "title": "🎯 Quality on sale",
     "why": "High-quality companies 25%+ below their 52-week high. Could be a bargain or a falling "
            "knife — find out why it dropped.",
     "filter": lambda d: (d["quality"] >= 75) & (d["pct_below_high"] >= 0.25) & (d["health"] >= 50),
     "sort": lambda d: d["quality"]},
]


def run_screens(d: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    for s in SCREENS:
        hit = d[s["filter"](d).fillna(False)]
        out[s["key"]] = hit.assign(_sort=s["sort"](hit)).sort_values("_sort", ascending=False) \
                           .head(s.get("n", TOP_N)).drop(columns="_sort")
    return out
