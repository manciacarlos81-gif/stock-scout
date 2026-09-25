#!/usr/bin/env python3
"""QA log aggregator: reads a timestamped results log, rolls up per-identifier
pass/fail status into a summary report, and archives both the summary and the
source log in a dated output folder for later QA review.

Supported log formats (auto-detected by extension, override with --format):
  - csv    : header row with columns timestamp,identifier,status (extra columns ignored)
  - jsonl  : one JSON object per line with keys "timestamp", "identifier", "status"
  - text   : whitespace-separated "<timestamp> <identifier> <status>" per line

Usage:
  python tools/qa_log_aggregator.py --log results/2026-09-24T101500.csv
  python tools/qa_log_aggregator.py --log results/ --output-dir qa_reports --verbose
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("qa_log_aggregator")

DEFAULT_PASS_VALUES = {"pass", "passed", "ok", "success", "true", "1"}
DEFAULT_FAIL_VALUES = {"fail", "failed", "error", "false", "0"}

TIMESTAMP_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}[T_ ]\d{2}[:.-]?\d{2}[:.-]?\d{2}"),
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\d{8}[T_-]?\d{6}"),
)


class LogParseError(RuntimeError):
    """Raised when a log file cannot be parsed at all (no usable records)."""


@dataclass
class Record:
    timestamp: str
    identifier: str
    status: str
    line_no: int


@dataclass
class IdentifierSummary:
    identifier: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    unknown: int = 0
    last_status: str = ""
    last_timestamp: str = ""
    statuses_seen: list = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    @property
    def overall_status(self) -> str:
        if self.failed > 0:
            return "FAIL"
        if self.passed > 0:
            return "PASS"
        return "UNKNOWN"


def normalize_status(raw: str, pass_values: set, fail_values: set) -> str:
    key = (raw or "").strip().lower()
    if key in pass_values:
        return "PASS"
    if key in fail_values:
        return "FAIL"
    return "UNKNOWN"


def extract_timestamp_from_name(path: Path) -> str | None:
    name = path.stem
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(name)
        if match:
            return match.group(0)
    return None


def find_latest_log(directory: Path, extensions: tuple[str, ...] = (".csv", ".jsonl", ".log", ".txt")) -> Path:
    candidates = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in extensions]
    if not candidates:
        raise FileNotFoundError(f"No log files ({', '.join(extensions)}) found in {directory}")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in (".jsonl", ".ndjson"):
        return "jsonl"
    return "text"


def parse_csv(path: Path) -> tuple[list[Record], int]:
    records: list[Record] = []
    skipped = 0
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise LogParseError(f"{path} is empty or has no header row")
        fields = {f.lower(): f for f in reader.fieldnames}
        required = ("timestamp", "identifier", "status")
        missing = [f for f in required if f not in fields]
        if missing:
            raise LogParseError(f"{path} is missing required column(s): {', '.join(missing)}")
        for line_no, row in enumerate(reader, start=2):
            try:
                records.append(Record(
                    timestamp=row[fields["timestamp"]].strip(),
                    identifier=row[fields["identifier"]].strip(),
                    status=row[fields["status"]].strip(),
                    line_no=line_no,
                ))
            except (KeyError, AttributeError):
                LOG.warning("Skipping malformed CSV row %d in %s", line_no, path)
                skipped += 1
    return records, skipped


def parse_jsonl(path: Path) -> tuple[list[Record], int]:
    records: list[Record] = []
    skipped = 0
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                records.append(Record(
                    timestamp=str(obj.get("timestamp", "")).strip(),
                    identifier=str(obj.get("identifier", "")).strip(),
                    status=str(obj.get("status", "")).strip(),
                    line_no=line_no,
                ))
            except (json.JSONDecodeError, AttributeError) as exc:
                LOG.warning("Skipping malformed JSONL line %d in %s: %s", line_no, path, exc)
                skipped += 1
    return records, skipped


def parse_text(path: Path) -> tuple[list[Record], int]:
    records: list[Record] = []
    skipped = 0
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 3:
                LOG.warning("Skipping malformed text line %d in %s: %r", line_no, path, line)
                skipped += 1
                continue
            timestamp, identifier, status = parts[0], parts[1], parts[2]
            records.append(Record(timestamp=timestamp, identifier=identifier, status=status, line_no=line_no))
    return records, skipped


PARSERS = {"csv": parse_csv, "jsonl": parse_jsonl, "text": parse_text}


def parse_log(path: Path, fmt: str) -> tuple[list[Record], int]:
    parser = PARSERS[fmt]
    records, skipped = parser(path)
    if not records:
        raise LogParseError(f"{path} produced zero usable records (format={fmt})")
    return records, skipped


def aggregate(records: list[Record], pass_values: set, fail_values: set) -> dict[str, IdentifierSummary]:
    summaries: dict[str, IdentifierSummary] = {}
    for rec in records:
        if not rec.identifier:
            continue
        summary = summaries.setdefault(rec.identifier, IdentifierSummary(identifier=rec.identifier))
        status = normalize_status(rec.status, pass_values, fail_values)
        summary.total += 1
        if status == "PASS":
            summary.passed += 1
        elif status == "FAIL":
            summary.failed += 1
        else:
            summary.unknown += 1
        summary.statuses_seen.append(status)
        # Records are assumed to already be in chronological order within the log;
        # the last one processed wins as "most recent" without needing timestamp parsing.
        summary.last_status = status
        summary.last_timestamp = rec.timestamp
    return summaries


def resolve_output_folder_name(log_path: Path, explicit_date: str | None) -> str:
    if explicit_date:
        return explicit_date
    from_name = extract_timestamp_from_name(log_path)
    if from_name:
        return from_name[:10] if len(from_name) >= 10 else from_name
    return datetime.now().strftime("%Y-%m-%d")


def write_csv_summary(summaries: dict[str, IdentifierSummary], dest: Path) -> None:
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["identifier", "total", "passed", "failed", "unknown",
                          "pass_rate", "overall_status", "last_status", "last_timestamp"])
        for identifier in sorted(summaries):
            s = summaries[identifier]
            writer.writerow([s.identifier, s.total, s.passed, s.failed, s.unknown,
                              f"{s.pass_rate:.4f}", s.overall_status, s.last_status, s.last_timestamp])


def write_markdown_summary(summaries: dict[str, IdentifierSummary], dest: Path, source_log: Path,
                            skipped_lines: int, generated_at: str) -> None:
    total_ids = len(summaries)
    passing = sum(1 for s in summaries.values() if s.overall_status == "PASS")
    failing = sum(1 for s in summaries.values() if s.overall_status == "FAIL")
    unknown = total_ids - passing - failing
    lines = [
        "# QA Results Summary",
        "",
        f"- Source log: `{source_log.name}`",
        f"- Generated: {generated_at}",
        f"- Identifiers: {total_ids} (pass {passing} / fail {failing} / unknown {unknown})",
    ]
    if skipped_lines:
        lines.append(f"- Skipped malformed lines: {skipped_lines}")
    lines += [
        "",
        "| Identifier | Total | Passed | Failed | Pass rate | Status | Last seen |",
        "|---|---:|---:|---:|---:|:---:|---|",
    ]
    for identifier in sorted(summaries):
        s = summaries[identifier]
        marker = {"PASS": "✅", "FAIL": "❌", "UNKNOWN": "❓"}[s.overall_status]
        lines.append(f"| {s.identifier} | {s.total} | {s.passed} | {s.failed} | {s.pass_rate:.0%} | "
                     f"{marker} {s.overall_status} | {s.last_timestamp or '–'} |")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(log_arg: str, output_dir: str, fmt: str, date_override: str | None,
        pass_values: set, fail_values: set, keep_copy: bool, dry_run: bool) -> int:
    log_input = Path(log_arg).expanduser().resolve()
    if not log_input.exists():
        LOG.error("Log path does not exist: %s", log_input)
        return 2

    if log_input.is_dir():
        try:
            log_path = find_latest_log(log_input)
        except FileNotFoundError as exc:
            LOG.error(str(exc))
            return 2
        LOG.info("Selected most recent log in %s: %s", log_input, log_path.name)
    else:
        log_path = log_input

    resolved_fmt = fmt if fmt != "auto" else detect_format(log_path)
    LOG.debug("Using format=%s for %s", resolved_fmt, log_path)

    try:
        records, skipped = parse_log(log_path, resolved_fmt)
    except LogParseError as exc:
        LOG.error(str(exc))
        return 3

    if skipped:
        LOG.warning("Skipped %d malformed line(s) while parsing %s", skipped, log_path)

    summaries = aggregate(records, pass_values, fail_values)
    if not summaries:
        LOG.error("No identifiers found after aggregation; nothing to report")
        return 4

    folder_name = resolve_output_folder_name(log_path, date_override)
    out_root = Path(output_dir).expanduser().resolve()
    out_folder = out_root / folder_name

    generated_at = datetime.now().isoformat(timespec="seconds")
    csv_dest = out_folder / "summary.csv"
    md_dest = out_folder / "summary.md"
    log_copy_dest = out_folder / log_path.name

    LOG.info("Aggregated %d identifier(s) from %d record(s) (%d skipped)", len(summaries), len(records), skipped)

    if dry_run:
        LOG.info("[dry-run] Would write %s, %s%s", csv_dest, md_dest,
                  f" and copy {log_path.name}" if keep_copy else "")
        return 0

    out_folder.mkdir(parents=True, exist_ok=True)
    write_csv_summary(summaries, csv_dest)
    write_markdown_summary(summaries, md_dest, log_path, skipped, generated_at)
    LOG.info("Wrote summary: %s", csv_dest)
    LOG.info("Wrote summary: %s", md_dest)

    if keep_copy:
        shutil.copy2(log_path, log_copy_dest)
        LOG.info("Archived source log copy: %s", log_copy_dest)

    failing = [s.identifier for s in summaries.values() if s.overall_status == "FAIL"]
    if failing:
        LOG.warning("%d identifier(s) failing: %s", len(failing), ", ".join(sorted(failing)))

    return 0


def parse_status_set(raw: str | None, default: set) -> set:
    if not raw:
        return default
    return {v.strip().lower() for v in raw.split(",") if v.strip()}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate a timestamped QA results log into a per-identifier pass/fail summary.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--log", required=True,
                         help="Path to a results log file, or a directory to pick the most recent log from")
    parser.add_argument("--output-dir", default="qa_reports",
                         help="Root directory under which a dated subfolder is created for the summary and log copy")
    parser.add_argument("--format", choices=("auto", "csv", "jsonl", "text"), default="auto",
                         help="Log format; auto-detects from file extension when set to 'auto'")
    parser.add_argument("--date", default=None,
                         help="Override the dated output subfolder name (default: derived from the log's "
                              "filename timestamp, falling back to today's date)")
    parser.add_argument("--pass-values", default=None,
                         help=f"Comma-separated status tokens treated as PASS (default: {sorted(DEFAULT_PASS_VALUES)})")
    parser.add_argument("--fail-values", default=None,
                         help=f"Comma-separated status tokens treated as FAIL (default: {sorted(DEFAULT_FAIL_VALUES)})")
    parser.add_argument("--no-copy", action="store_true",
                         help="Do not copy the source log into the dated output folder")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse and aggregate but do not write any files")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only log warnings and errors")
    parser.add_argument("--log-file", default=None, help="Optional path to also write log output to")
    return parser


def configure_logging(verbose: bool, quiet: bool, log_file: str | None) -> None:
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handlers = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose, args.quiet, args.log_file)

    pass_values = parse_status_set(args.pass_values, DEFAULT_PASS_VALUES)
    fail_values = parse_status_set(args.fail_values, DEFAULT_FAIL_VALUES)
    overlap = pass_values & fail_values
    if overlap:
        LOG.error("Status token(s) listed in both --pass-values and --fail-values: %s", ", ".join(sorted(overlap)))
        return 2

    try:
        return run(
            log_arg=args.log,
            output_dir=args.output_dir,
            fmt=args.format,
            date_override=args.date,
            pass_values=pass_values,
            fail_values=fail_values,
            keep_copy=not args.no_copy,
            dry_run=args.dry_run,
        )
    except Exception:
        LOG.exception("Unhandled error while aggregating QA log")
        return 1


if __name__ == "__main__":
    sys.exit(main())
