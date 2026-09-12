"""Insider open-market purchases (Form 4 filings).

Primary: OpenInsider, which republishes SEC Form 4 data within minutes of filing.
Fallback: SEC's own quarterly insider-transaction data sets (official, but 1-3 months behind).
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, timedelta

import pandas as pd
import requests

from .config import BROWSER_UA, SEC_UA

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def _money(s: str) -> float:
    s = re.sub(r"[^\d.\-]", "", s)
    return float(s) if s not in ("", "-", ".") else 0.0


def _text(cell: str) -> str:
    links = re.findall(r">([^<>]*)</a>", cell)
    return (links[-1] if links else re.sub(r"<[^>]+>", "", cell)).replace("&amp;", "&").strip()


def openinsider(days: int, ticker: str = "", buys=True, sells=False, pages=5) -> pd.DataFrame:
    rows = []
    for page in range(1, pages + 1):
        params = {"s": ticker, "fd": days, "vl": 10 if not ticker else "", "cnt": 1000, "page": page,
                  "xp": 1 if buys else "", "xs": 1 if sells else ""}
        r = requests.get("http://openinsider.com/screener", params=params,
                         headers={"User-Agent": BROWSER_UA}, timeout=60)
        r.raise_for_status()
        m = re.search(r'<table[^>]*class="tinytable"[^>]*>(.*?)</table>', r.text, re.S)
        if not m:
            break
        # Column layout differs between the market-wide and single-ticker pages, so map by header name
        header = [re.sub(r"<[^>]+>", "", h).replace("&nbsp;", " ").replace("\xa0", " ").strip()
                  for h in re.findall(r"<th[^>]*>(.*?)</th>", m.group(1), re.S)]
        col = {name: i for i, name in enumerate(header)}
        need = ["Filing Date", "Trade Date", "Insider Name", "Title", "Trade Type", "Price", "Qty", "Value"]
        if any(n not in col for n in need):
            raise ValueError(f"OpenInsider layout changed: {header}")
        got = 0
        for tr in _ROW.findall(m.group(1)):
            c = _CELL.findall(tr)
            if len(c) < len(header):
                continue
            cell = lambda name: _text(c[col[name]])
            if "Ticker" in col:
                t = re.search(r'href="/([^"/?]+)"', c[col["Ticker"]])
                tick = t.group(1) if t else cell("Ticker")
            else:
                tick = ticker
            rows.append({"ticker": tick.upper().replace(".", "-"), "filed": cell("Filing Date")[:10],
                         "trade_date": cell("Trade Date"), "insider": cell("Insider Name"), "title": cell("Title"),
                         "type": cell("Trade Type"), "price": _money(cell("Price")), "shares": _money(cell("Qty")),
                         "value": _money(cell("Value"))})
            got += 1
        if got < 1000:
            break
    return pd.DataFrame(rows)


def sec_dataset(days: int) -> pd.DataFrame:
    """Open-market purchases from the SEC's latest quarterly Form 3/4/5 data set."""
    page = requests.get("https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets",
                        headers={"User-Agent": SEC_UA}, timeout=60).text
    links = re.findall(r'href="([^"]*\d{4}q\d_form345\.zip)"', page)
    frames = []
    for link in sorted(set(links), key=lambda s: re.search(r"(\d{4}q\d)", s).group(1), reverse=True)[:2]:
        z = zipfile.ZipFile(io.BytesIO(requests.get("https://www.sec.gov" + link if link.startswith("/") else link,
                                                    headers={"User-Agent": SEC_UA}, timeout=300).content))
        read = lambda n, cols: pd.read_csv(z.open(n), sep="\t", usecols=cols, dtype=str, on_bad_lines="skip")
        sub = read("SUBMISSION.tsv", ["ACCESSION_NUMBER", "ISSUERTRADINGSYMBOL"])
        own = read("REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERNAME", "RPTOWNER_TITLE",
                                          "RPTOWNER_RELATIONSHIP"]).drop_duplicates("ACCESSION_NUMBER")
        own["RPTOWNER_TITLE"] = own["RPTOWNER_TITLE"].fillna(
            own["RPTOWNER_RELATIONSHIP"].replace({"TenPercentOwner": "10%", "Director": "Dir"}))
        tr = read("NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE"])
        tr = tr[tr["TRANS_CODE"] == "P"].merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
        frames.append(pd.DataFrame({
            "ticker": tr["ISSUERTRADINGSYMBOL"].str.upper().str.replace(".", "-", regex=False),
            "trade_date": pd.to_datetime(tr["TRANS_DATE"], format="%d-%b-%Y", errors="coerce").dt.date.astype(str),
            "insider": tr["RPTOWNERNAME"], "title": tr["RPTOWNER_TITLE"].fillna(""), "type": "P - Purchase",
            "price": pd.to_numeric(tr["TRANS_PRICEPERSHARE"], errors="coerce"),
            "shares": pd.to_numeric(tr["TRANS_SHARES"], errors="coerce")}))
    df = pd.concat(frames, ignore_index=True)
    df["value"] = df["price"] * df["shares"]
    cutoff = str(date.today() - timedelta(days=days))
    return df[df["trade_date"] >= cutoff]


def recent_buys(days: int) -> tuple[pd.DataFrame, str]:
    """Per-ticker summary of insider purchases in the last `days` days, and which source was used."""
    try:
        df, source = openinsider(days), "OpenInsider (SEC Form 4)"
        if df.empty:
            raise ValueError("no rows")
    except Exception as e:
        print(f"  OpenInsider unavailable ({e.__class__.__name__}); using SEC quarterly data set")
        df, source = sec_dataset(days), "SEC insider-transaction data set"
    df = df[df["type"].str.startswith("P")]
    # Only people who run the company. Funds that merely own 10%+ are "insiders" legally but
    # their buys (often $100M+) would swamp the signal from executives and directors.
    df = df[df["title"].fillna("").str.replace(r"10%|,|\s", "", regex=True) != ""]
    g = df.groupby("ticker")
    out = pd.DataFrame({
        "insider_buy_value": g["value"].sum(),
        "insider_buyers": g["insider"].nunique(),
        "insider_last_buy": g["trade_date"].max(),
        "insider_who": g.apply(lambda x: "; ".join(
            f"{n} ({t})" if t else n for n, t in x.drop_duplicates("insider")[["insider", "title"]].head(3).values),
            include_groups=False),
    })
    return out, source
