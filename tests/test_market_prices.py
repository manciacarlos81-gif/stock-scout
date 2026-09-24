"""Price download shaping and the metrics derived from it.

No network: yf.download is replaced with a stub that returns the two frame
shapes yfinance actually produces (MultiIndex for many tickers, flat columns
for one).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout import market  # noqa: E402

DAYS = pd.bdate_range("2024-01-01", periods=400)


def _series(seed, start=100.0, n=len(DAYS)):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1 + rng.normal(0.0004, 0.01, n))


def _multi(tickers):
    """What yf.download returns for two or more tickers: (field, ticker) columns."""
    cols = pd.MultiIndex.from_product([["Close", "Volume"], tickers])
    data = {}
    for i, t in enumerate(tickers):
        data[("Close", t)] = _series(i)
        data[("Volume", t)] = np.full(len(DAYS), 1_000_000.0)
    return pd.DataFrame(data, index=DAYS, columns=cols)


def _flat():
    """What yf.download returns for exactly one ticker: flat field columns."""
    return pd.DataFrame(
        {"Close": _series(7), "Volume": np.full(len(DAYS), 1_000_000.0)}, index=DAYS
    )


def test_a_single_ticker_chunk_is_keyed_by_ticker_not_by_field(monkeypatch):
    """A trailing chunk of one used to arrive as a column called "Close"."""
    monkeypatch.setattr(market.yf, "download", lambda part, **kw: _flat())
    close, vol = market.download_prices(["ZZZ"], chunk=200)
    assert list(close.columns) == ["ZZZ"]
    assert list(vol.columns) == ["ZZZ"]


def test_chunks_of_both_shapes_combine(monkeypatch):
    calls = []

    def fake(part, **kw):
        calls.append(list(part))
        return _flat() if len(part) == 1 else _multi(list(part))

    monkeypatch.setattr(market.yf, "download", fake)
    tickers = ["AAA", "BBB", "CCC"]
    close, _ = market.download_prices(tickers, chunk=2)
    assert calls == [["AAA", "BBB"], ["CCC"]]
    assert set(close.columns) == set(tickers)
    assert "Close" not in close.columns


def test_a_total_price_outage_raises_instead_of_producing_an_empty_scan(monkeypatch):
    monkeypatch.setattr(market.JitterSchedule, "wait", lambda self: 0.0)

    def boom(part, **kw):
        raise ConnectionError("yahoo is down")

    monkeypatch.setattr(market.yf, "download", boom)
    with pytest.raises(RuntimeError, match="no price data"):
        market.download_prices(["AAA", "BBB"])


def test_empty_frames_are_skipped_but_good_chunks_survive(monkeypatch):
    def fake(part, **kw):
        return pd.DataFrame() if "BBB" in part else _multi(list(part))

    monkeypatch.setattr(market.yf, "download", fake)
    close, _ = market.download_prices(["AAA", "XXX", "BBB", "YYY"], chunk=2)
    assert set(close.columns) == {"AAA", "XXX"}


def test_price_metrics_are_internally_consistent():
    close = pd.DataFrame({"AAA": _series(1), "BBB": _series(2, start=40)}, index=DAYS)
    vol = pd.DataFrame(1_000_000.0, index=DAYS, columns=["AAA", "BBB"])
    m = market.price_metrics(close, vol)

    assert list(m.index) == ["AAA", "BBB"]
    assert m.index.name == "ticker"
    for t in ("AAA", "BBB"):
        assert m.loc[t, "price"] == pytest.approx(close[t].iloc[-1])
        assert m.loc[t, "ret_12m"] == pytest.approx(close[t].iloc[-1] / close[t].iloc[-253] - 1)
        assert m.loc[t, "high_52w"] >= m.loc[t, "price"] >= m.loc[t, "low_52w"] * 0.999
        assert 0 <= m.loc[t, "pct_below_high"] <= 1
        assert 0 <= m.loc[t, "rsi14"] <= 100
        assert m.loc[t, "max_drawdown"] <= 0
        assert m.loc[t, "volatility"] > 0


def test_price_metrics_survive_a_short_history():
    """A recent IPO has no 12-month return — nan, not an IndexError."""
    short = pd.bdate_range("2026-06-01", periods=30)
    close = pd.DataFrame({"NEW": _series(3, n=30)}, index=short)
    vol = pd.DataFrame(500_000.0, index=short, columns=["NEW"])
    m = market.price_metrics(close, vol)
    assert np.isnan(m.loc["NEW", "ret_12m"])
    assert np.isnan(m.loc["NEW", "sma200"])
    assert m.loc["NEW", "price"] == pytest.approx(close["NEW"].iloc[-1])


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_universe_survives_a_null_company_name(monkeypatch, tmp_path):
    """A blank/null name from the Nasdaq screener used to crash the whole scan.

    ``~series.str.contains(...)`` raises TypeError the moment the underlying
    object-dtype column holds a bare None/NaN for a row that pattern never
    matched, because there is nothing to negate. One bad row on a public,
    uncontrolled feed took the entire scan down with it.
    """
    monkeypatch.setattr(market, "DATA_DIR", tmp_path)  # never touch the committed data/ copy
    rows = [
        {"symbol": "AAA", "name": None, "sector": "Tech", "industry": "Software",
         "country": "US", "marketCap": "1000000000", "lastsale": "$10.00", "volume": "100000"},
        {"symbol": "BBB", "name": "Beta Inc Common Stock", "sector": "Tech", "industry": "Software",
         "country": "US", "marketCap": "2000000000", "lastsale": "$5.00", "volume": "200000"},
        {"symbol": "WWW", "name": "Gamma Warrants", "sector": "Tech", "industry": "Software",
         "country": "US", "marketCap": "500000000", "lastsale": "$1.00", "volume": "50000"},
    ]
    monkeypatch.setattr(
        market.requests, "get",
        lambda *a, **kw: _FakeResponse({"data": {"rows": rows}}),
    )
    monkeypatch.setattr(
        market.sec, "ticker_map",
        lambda: pd.DataFrame({"ticker": ["AAA", "BBB", "WWW"],
                              "cik": [1, 2, 3], "exchange": ["Q", "Q", "Q"]}),
    )
    df = market.universe()  # must not raise
    assert set(df["ticker"]) == {"AAA", "BBB"}  # the warrants row is excluded, the null-name row survives
    assert df.loc[df["ticker"] == "AAA", "name"].iloc[0] == ""
