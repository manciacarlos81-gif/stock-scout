import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.qa_log_aggregator import (  # noqa: E402
    LogParseError,
    aggregate,
    detect_format,
    extract_timestamp_from_name,
    find_latest_log,
    main,
    normalize_status,
    parse_csv,
    parse_jsonl,
    parse_text,
    resolve_output_folder_name,
)

PASS_VALUES = {"pass", "passed", "ok", "success", "true", "1"}
FAIL_VALUES = {"fail", "failed", "error", "false", "0"}


def write_csv_log(path: Path, rows: list[tuple[str, str, str]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "identifier", "status"])
        writer.writerows(rows)


def test_normalize_status_case_insensitive():
    assert normalize_status("PASS", PASS_VALUES, FAIL_VALUES) == "PASS"
    assert normalize_status("Failed", PASS_VALUES, FAIL_VALUES) == "FAIL"
    assert normalize_status("weird", PASS_VALUES, FAIL_VALUES) == "UNKNOWN"


def test_detect_format_by_extension(tmp_path):
    assert detect_format(tmp_path / "x.csv") == "csv"
    assert detect_format(tmp_path / "x.jsonl") == "jsonl"
    assert detect_format(tmp_path / "x.log") == "text"


def test_extract_timestamp_from_name():
    assert extract_timestamp_from_name(Path("results_2026-09-24T10-15-00.csv")) == "2026-09-24T10-15-00"
    assert extract_timestamp_from_name(Path("results_2026-09-24.csv")) == "2026-09-24"
    assert extract_timestamp_from_name(Path("no_timestamp_here.csv")) is None


def test_parse_csv_roundtrip(tmp_path):
    log = tmp_path / "results.csv"
    write_csv_log(log, [
        ("2026-09-24T10:00:00", "job-a", "PASS"),
        ("2026-09-24T10:00:01", "job-b", "FAIL"),
    ])
    records, skipped = parse_csv(log)
    assert skipped == 0
    assert len(records) == 2
    assert records[0].identifier == "job-a"
    assert records[1].status == "FAIL"


def test_parse_csv_missing_columns_raises(tmp_path):
    log = tmp_path / "bad.csv"
    log.write_text("foo,bar\n1,2\n")
    with pytest.raises(LogParseError):
        parse_csv(log)


def test_parse_csv_empty_raises(tmp_path):
    log = tmp_path / "empty.csv"
    log.write_text("")
    with pytest.raises(LogParseError):
        parse_csv(log)


def test_parse_jsonl_skips_malformed_lines(tmp_path):
    log = tmp_path / "results.jsonl"
    log.write_text(
        json.dumps({"timestamp": "t1", "identifier": "job-a", "status": "pass"}) + "\n"
        "not json\n"
        + json.dumps({"timestamp": "t2", "identifier": "job-b", "status": "fail"}) + "\n"
    )
    records, skipped = parse_jsonl(log)
    assert skipped == 1
    assert len(records) == 2


def test_parse_text_basic(tmp_path):
    log = tmp_path / "results.log"
    log.write_text(
        "2026-09-24T10:00:00 job-a PASS\n"
        "# a comment\n"
        "\n"
        "malformed-line\n"
        "2026-09-24T10:00:01 job-b FAIL\n"
    )
    records, skipped = parse_text(log)
    assert skipped == 1
    assert len(records) == 2
    assert records[0].identifier == "job-a"
    assert records[1].status == "FAIL"


def test_aggregate_computes_overall_status_and_last_seen():
    log = None  # unused, parse_text used indirectly via parse_csv style records
    from tools.qa_log_aggregator import Record

    records = [
        Record("t1", "job-a", "PASS", 1),
        Record("t2", "job-a", "PASS", 2),
        Record("t3", "job-b", "PASS", 3),
        Record("t4", "job-b", "FAIL", 4),
        Record("t5", "job-c", "bogus", 5),
    ]
    summaries = aggregate(records, PASS_VALUES, FAIL_VALUES)
    assert summaries["job-a"].overall_status == "PASS"
    assert summaries["job-a"].total == 2
    assert summaries["job-b"].overall_status == "FAIL"
    assert summaries["job-b"].last_status == "FAIL"
    assert summaries["job-b"].last_timestamp == "t4"
    assert summaries["job-c"].overall_status == "UNKNOWN"


def test_find_latest_log_picks_newest_mtime(tmp_path):
    older = tmp_path / "a.csv"
    newer = tmp_path / "b.csv"
    older.write_text("x")
    newer.write_text("y")
    import os
    import time
    time.sleep(0.01)
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    assert find_latest_log(tmp_path) == newer


def test_find_latest_log_raises_when_empty(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_latest_log(tmp_path)


def test_resolve_output_folder_name_prefers_explicit_date():
    assert resolve_output_folder_name(Path("results_2026-09-24.csv"), "2099-01-01") == "2099-01-01"


def test_resolve_output_folder_name_falls_back_to_filename_timestamp():
    assert resolve_output_folder_name(Path("results_2026-09-24T101500.csv"), None) == "2026-09-24"


def test_end_to_end_writes_summary_and_archives_log(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "results_2026-09-24T101500.csv"
    write_csv_log(log_path, [
        ("2026-09-24T10:15:00", "job-a", "PASS"),
        ("2026-09-24T10:15:01", "job-a", "PASS"),
        ("2026-09-24T10:15:02", "job-b", "FAIL"),
    ])
    out_dir = tmp_path / "qa_reports"

    exit_code = main(["--log", str(log_path), "--output-dir", str(out_dir)])
    assert exit_code == 0

    dated_folder = out_dir / "2026-09-24"
    assert (dated_folder / "summary.csv").exists()
    assert (dated_folder / "summary.md").exists()
    assert (dated_folder / log_path.name).exists()

    with (dated_folder / "summary.csv").open() as fh:
        rows = {r["identifier"]: r for r in csv.DictReader(fh)}
    assert rows["job-a"]["overall_status"] == "PASS"
    assert rows["job-b"]["overall_status"] == "FAIL"

    md = (dated_folder / "summary.md").read_text()
    assert "job-a" in md and "job-b" in md


def test_no_copy_flag_skips_archiving_log(tmp_path):
    log_path = tmp_path / "results.csv"
    write_csv_log(log_path, [("t1", "job-a", "PASS")])
    out_dir = tmp_path / "qa_reports"

    exit_code = main(["--log", str(log_path), "--output-dir", str(out_dir), "--date", "2026-09-24", "--no-copy"])
    assert exit_code == 0

    dated_folder = out_dir / "2026-09-24"
    assert (dated_folder / "summary.csv").exists()
    assert not (dated_folder / log_path.name).exists()


def test_dry_run_writes_nothing(tmp_path):
    log_path = tmp_path / "results.csv"
    write_csv_log(log_path, [("t1", "job-a", "PASS")])
    out_dir = tmp_path / "qa_reports"

    exit_code = main(["--log", str(log_path), "--output-dir", str(out_dir), "--dry-run"])
    assert exit_code == 0
    assert not out_dir.exists()


def test_missing_log_path_returns_error_code(tmp_path):
    exit_code = main(["--log", str(tmp_path / "nope.csv"), "--output-dir", str(tmp_path / "out")])
    assert exit_code == 2


def test_overlapping_pass_fail_values_returns_error(tmp_path):
    log_path = tmp_path / "results.csv"
    write_csv_log(log_path, [("t1", "job-a", "PASS")])
    exit_code = main(["--log", str(log_path), "--pass-values", "weird", "--fail-values", "weird"])
    assert exit_code == 2
