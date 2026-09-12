"""Checks the trailing-12-month and Piotroski math on made-up numbers. Run: ./venv/bin/python tests/test_sec_math.py"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout.sec import _piotroski, _quarters, annual, latest_avg, ttm  # noqa: E402


def q(y, m_end, d_end, val, months=3):
    end = date(y, m_end, d_end)
    sm = m_end - months + 1
    start = date(y if sm > 0 else y - 1, sm if sm > 0 else sm + 12, 1)
    return (start, end, val)


def test_calendar_year_company():
    # FY2025 = 400; Q1/Q2 2026 = 110/120; Q1/Q2 2025 = 95/100  ->  TTM = 400 + 230 - 195 = 435
    facts = sorted([
        (date(2024, 1, 1), date(2024, 12, 31), 350), (date(2025, 1, 1), date(2025, 12, 31), 400),
        q(2024, 3, 31, 85), q(2024, 6, 30, 88), q(2025, 3, 31, 95), q(2025, 6, 30, 100),
        q(2026, 3, 31, 110), q(2026, 6, 30, 120),
    ], key=lambda f: f[1])
    cur, prior, asof = ttm(facts)
    assert cur == 435, cur
    assert prior == 350 + 195 - 173, prior  # TTM to Jun 2025
    assert asof == date(2026, 6, 30)


def test_september_fiscal_year_company():
    # FY ends late Sep. TTM to Jun 2026 = FY(Sep25) + Dec25+Mar26+Jun26 - Dec24+Mar25+Jun25
    facts = sorted([
        (date(2023, 10, 1), date(2024, 9, 28), 1000), (date(2024, 9, 29), date(2025, 9, 27), 1200),
        (date(2023, 10, 1), date(2023, 12, 30), 240), (date(2023, 12, 31), date(2024, 3, 30), 250), (date(2024, 3, 31), date(2024, 6, 29), 255),
        (date(2024, 9, 29), date(2024, 12, 28), 280), (date(2024, 12, 29), date(2025, 3, 29), 290), (date(2025, 3, 30), date(2025, 6, 28), 300),
        (date(2025, 9, 28), date(2025, 12, 27), 330), (date(2025, 12, 28), date(2026, 3, 28), 340), (date(2026, 3, 29), date(2026, 6, 27), 350),
    ], key=lambda f: f[1])
    cur, prior, asof = ttm(facts)
    assert cur == 1200 + (330 + 340 + 350) - (280 + 290 + 300), cur
    assert prior == 1000 + (280 + 290 + 300) - (240 + 250 + 255), prior
    assert asof == date(2026, 6, 27)


def test_falls_back_to_annual_when_quarters_missing():
    facts = [(date(2024, 1, 1), date(2024, 12, 31), 350), (date(2025, 1, 1), date(2025, 12, 31), 400), q(2026, 6, 30, 120)]
    cur, prior, asof = ttm(facts)  # Q1 2026 missing -> can't build TTM to June, use FY2025
    assert (cur, prior, asof) == (400, 350, date(2025, 12, 31)), (cur, prior, asof)


def test_annual_and_shares():
    facts = [(date(2024, 1, 1), date(2024, 12, 31), 350), (date(2025, 1, 1), date(2025, 12, 31), 400)]
    last, prior = annual(facts)
    assert last[2] == 400 and prior[2] == 350
    shares = [q(2025, 6, 30, 100), q(2026, 6, 30, 95)]
    assert latest_avg(shares) == (95, 100)


def test_quarters_window():
    qs = _quarters(date(2026, 9, 13))
    assert qs[-1] == (2026, 2) and qs[0] == (2023, 3) and len(qs) == 12
    assert _quarters(date(2026, 2, 1))[-1] == (2025, 3)   # Q4 ended only 32 days ago: 10-Qs not in yet
    assert _quarters(date(2026, 2, 20))[-1] == (2025, 4)


def test_piotroski_perfect_and_bad():
    good = dict(net_income_annual=120, net_income_annual_prior=100, assets_fy=1000, assets_fy_prior=1000, cfo=150,
                lt_debt_fy=100, lt_debt_fy_prior=200, current_assets_fy=300, current_liabilities_fy=100,
                current_assets_fy_prior=250, current_liabilities_fy_prior=100, diluted_shares=95, diluted_shares_prior=100,
                gross_profit_annual=500, gross_profit_annual_prior=400, revenue_annual=1000, revenue_annual_prior=900)
    assert _piotroski(good) == 9
    bad = dict(good, net_income_annual=-50, cfo=-60, lt_debt_fy=400, current_assets_fy=150, diluted_shares=130,
               gross_profit_annual=300, revenue_annual=800)
    assert _piotroski(bad) == 0, _piotroski(bad)
    assert _piotroski({"net_income_annual": 1}) is None


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok ", t.__name__)
    print(f"{len(tests)} passed")
