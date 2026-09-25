#!/usr/bin/env python3
"""Compares this run's report against the most recent prior dated report in reports/.

Every scan writes ``reports/YYYY-MM-DD.md`` alongside ``reports/latest.md``. This reads
the per-screen ticker tables out of two of those dated files, works out which tickers
entered or left which screens between them, and flags the ones whose status changed.

    python -m scout.audit                 compare the two most recent dated reports
    python -m scout.audit --verbose        same, with debug logging
    python -m scout.audit --output out.md  write the diff to a file instead of stdout
"""
from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import REPORTS_DIR

logger = logging.getLogger("scout.audit")

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_SCREEN_RE = re.compile(r"^### (.+?)\n", re.M)
_TICKER_RE = re.compile(r"^\|\s*\[([A-Z][A-Z0-9.\-]*)\]", re.M)


@dataclass
class Change:
    ticker: str
    status: str          # "new" | "dropped" | "changed"
    entered: list[str]   # screens it's newly in
    left: list[str]      # screens it's no longer in


def dated_reports(reports_dir: Path) -> list[tuple[date, Path]]:
    """Every dated report in ``reports_dir``, oldest first.

    ``latest.md`` is skipped: it's a copy of whichever dated file was written last, so
    including it would compare the newest run against itself.
    """
    out = []
    for p in reports_dir.glob("*.md"):
        m = _DATE_RE.match(p.name)
        if m:
            out.append((date.fromisoformat(m.group(1)), p))
    return sorted(out)


def parse_membership(path: Path) -> dict[str, set[str]]:
    """ticker -> set of screen titles it appears under in the report at ``path``."""
    text = path.read_text()
    parts = _SCREEN_RE.split(text)[1:]  # [title, body, title, body, ...]
    membership: dict[str, set[str]] = {}
    for title, body in zip(parts[0::2], parts[1::2]):
        for ticker in _TICKER_RE.findall(body):
            membership.setdefault(ticker, set()).add(title.strip())
    return membership


def diff_membership(prior: dict[str, set[str]], current: dict[str, set[str]]) -> list[Change]:
    """One entry per ticker whose screen membership changed, sorted by ticker."""
    changes = []
    for ticker in sorted(set(prior) | set(current)):
        before, after = prior.get(ticker, set()), current.get(ticker, set())
        if before == after:
            continue
        status = "new" if not before else ("dropped" if not after else "changed")
        changes.append(Change(ticker, status, sorted(after - before), sorted(before - after)))
    return changes


def format_changes(changes: list[Change], prior_date: date, current_date: date) -> str:
    if not changes:
        return f"No screen-membership changes between {prior_date} and {current_date}."
    lines = [f"# Screen changes: {prior_date} -> {current_date}", "",
             f"{len(changes)} ticker(s) changed status.", "",
             "| Ticker | Status | Entered | Left |", "|---|---|---|---|"]
    for c in changes:
        lines.append(f"| {c.ticker} | {c.status} | {', '.join(c.entered) or '–'} | {', '.join(c.left) or '–'} |")
    return "\n".join(lines)


def compare(reports_dir: Path = REPORTS_DIR,
           current: Path | None = None) -> tuple[date, date, list[Change]] | None:
    """Compare ``current`` (default: the most recent dated report) against the dated
    report immediately before it. Returns None when there's no prior run to compare."""
    reports = dated_reports(reports_dir)
    if not reports:
        raise FileNotFoundError(f"no dated reports found in {reports_dir}")

    if current is None:
        current_date, current_path = reports[-1]
        earlier = reports[:-1]
    else:
        m = _DATE_RE.match(current.name)
        if not m:
            raise ValueError(f"{current.name!r} isn't a dated report (expected YYYY-MM-DD.md)")
        current_date, current_path = date.fromisoformat(m.group(1)), current
        earlier = [r for r in reports if r[0] < current_date]

    if not earlier:
        logger.info("no prior report before %s; nothing to compare", current_date)
        return None

    prior_date, prior_path = earlier[-1]
    changes = diff_membership(parse_membership(prior_path), parse_membership(current_path))
    logger.info("%s -> %s: %d ticker(s) changed screen membership", prior_date, current_date, len(changes))
    return prior_date, current_date, changes


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reports-dir", type=Path, default=REPORTS_DIR, help="dated-reports directory (default: reports/)")
    p.add_argument("--current", type=Path, help="dated report to treat as the current run (default: most recent)")
    p.add_argument("--output", type=Path, help="write the diff as Markdown here instead of printing it")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s")

    try:
        result = compare(a.reports_dir, a.current)
    except (FileNotFoundError, ValueError) as e:
        logger.error("%s", e)
        raise SystemExit(1) from e

    if result is None:
        print("No prior run to compare against.")
        return
    prior_date, current_date, changes = result
    text = format_changes(changes, prior_date, current_date)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(text + "\n")
        logger.info("wrote %s", a.output)
    else:
        print(text)


if __name__ == "__main__":
    main()
