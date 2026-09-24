"""Report-diffing: parsing screen membership out of dated reports and flagging changes.
Run: ./venv/bin/python tests/test_audit.py"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scout import audit  # noqa: E402

REPORT_A = """# Stock Scout — 12 Sep 2026

## Screens

### Top overall
_blurb_

| Ticker | Company | Score |
|---|---|---:|
| [AAA](../stocks/AAA.md) | Alpha Co | 90 |
| [BBB](../stocks/BBB.md) | Beta Co | 80 |

### Deep value
_blurb_

| Ticker | Company | Score |
|---|---|---:|
| [CCC](../stocks/CCC.md) | Gamma Co | 70 |
"""

REPORT_B = """# Stock Scout — 13 Sep 2026

## Screens

### Top overall
_blurb_

| Ticker | Company | Score |
|---|---|---:|
| [AAA](../stocks/AAA.md) | Alpha Co | 88 |
| [DDD](../stocks/DDD.md) | Delta Co | 85 |

### Deep value
_blurb_

| Ticker | Company | Score |
|---|---|---:|
| [CCC](../stocks/CCC.md) | Gamma Co | 71 |
"""


def _write(dirpath: Path, name: str, text: str) -> Path:
    p = dirpath / name
    p.write_text(text)
    return p


def test_parse_membership():
    with tempfile.TemporaryDirectory() as tmp:
        p = _write(Path(tmp), "2026-09-12.md", REPORT_A)
        m = audit.parse_membership(p)
        assert m == {"AAA": {"Top overall"}, "BBB": {"Top overall"}, "CCC": {"Deep value"}}


def test_dated_reports_skips_latest():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "2026-09-12.md", REPORT_A)
        _write(d, "2026-09-13.md", REPORT_B)
        _write(d, "latest.md", REPORT_B)
        reports = audit.dated_reports(d)
        assert [name.name for _, name in reports] == ["2026-09-12.md", "2026-09-13.md"]


def test_diff_membership_flags_entered_left_and_unchanged():
    prior = {"AAA": {"Top overall"}, "BBB": {"Top overall"}, "CCC": {"Deep value"}}
    current = {"AAA": {"Top overall"}, "DDD": {"Top overall"}, "CCC": {"Top overall", "Deep value"}}
    changes = {c.ticker: c for c in audit.diff_membership(prior, current)}
    assert "AAA" not in changes  # unchanged, must not be flagged
    assert changes["BBB"].status == "dropped" and changes["BBB"].left == ["Top overall"]
    assert changes["DDD"].status == "new" and changes["DDD"].entered == ["Top overall"]
    assert changes["CCC"].status == "changed" and changes["CCC"].entered == ["Top overall"]


def test_compare_uses_most_recent_prior_report():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "2026-09-12.md", REPORT_A)
        _write(d, "2026-09-13.md", REPORT_B)
        prior_date, current_date, changes = audit.compare(d)
        assert str(prior_date) == "2026-09-12" and str(current_date) == "2026-09-13"
        tickers = {c.ticker for c in changes}
        assert tickers == {"BBB", "DDD"}


def test_compare_returns_none_without_a_prior_report():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "2026-09-13.md", REPORT_B)
        assert audit.compare(d) is None


def test_format_changes_reports_no_changes():
    from datetime import date
    text = audit.format_changes([], date(2026, 9, 12), date(2026, 9, 13))
    assert "No screen-membership changes" in text


if __name__ == "__main__":
    import inspect
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"  ok  {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL  {name}: {e}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
