"""Economy backdrop from FRED (St. Louis Fed). Uses the public CSV download, so no API key."""
from __future__ import annotations

import io

import pandas as pd
import requests

from .config import SEC_UA

SERIES = [  # id, label, how to show it
    ("DGS10", "10-year Treasury yield", "pct"),
    ("DGS2", "2-year Treasury yield", "pct"),
    ("T10Y2Y", "10y minus 2y (negative = recession warning)", "pts"),
    ("FEDFUNDS", "Fed funds rate", "pct"),
    ("UNRATE", "Unemployment rate", "pct"),
    ("CPIAUCSL", "Inflation (CPI, past 12 months)", "yoy"),
    ("BAMLH0A0HYM2", "Junk-bond spread (higher = more stress)", "pct"),
    ("VIXCLS", "VIX fear gauge (20+ = nervous market)", "num"),
]


def fetch() -> list[dict]:
    out = []
    for sid, label, kind in SERIES:
        try:
            # FRED stalls browser-like and bare tool User-Agents (bot check) but serves ones with contact info
            r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}",
                             headers={"User-Agent": SEC_UA}, timeout=30)
            r.raise_for_status()
            s = pd.read_csv(io.StringIO(r.text), index_col=0, parse_dates=True).iloc[:, 0]
            s = pd.to_numeric(s, errors="coerce").dropna()
            if kind == "yoy":
                s = (s / s.shift(12) - 1) * 100
                s = s.dropna()
            last_date = s.index[-1]
            year_ago = s[s.index <= last_date - pd.Timedelta(days=365)]
            out.append({"id": sid, "label": label, "kind": kind, "value": float(s.iloc[-1]),
                        "year_ago": float(year_ago.iloc[-1]) if len(year_ago) else None,
                        "date": last_date.date().isoformat()})
        except Exception as e:
            print(f"  FRED {sid} unavailable ({e.__class__.__name__})")
    return out
