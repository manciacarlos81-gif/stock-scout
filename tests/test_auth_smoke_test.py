import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.tools.auth_smoke_test import (  # noqa: E402
    ProbeResult,
    diff_against_previous,
    find_previous_run_dir,
    load_identifiers,
    main,
    read_results_csv,
    write_results_csv,
    write_summary,
)


def make_result(identifier, ok, status_code=200, timestamp="2026-09-25T00:00:00+00:00"):
    return ProbeResult(
        identifier=identifier, timestamp=timestamp, status_code=status_code,
        ok=ok, elapsed_ms=12.3, error=None,
    )


def test_load_identifiers_skips_blank_and_comment_lines(tmp_path):
    path = tmp_path / "accounts.txt"
    path.write_text("user-a\n\n# a comment\nuser-b\n")
    assert load_identifiers(path) == ["user-a", "user-b"]


def test_load_identifiers_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_identifiers(tmp_path / "nope.txt")


def test_load_identifiers_empty_file_raises(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("\n# only comments\n")
    with pytest.raises(ValueError):
        load_identifiers(path)


def test_write_and_read_results_csv_roundtrip(tmp_path):
    results = [make_result("user-a", True), make_result("user-b", False, status_code=None)]
    path = tmp_path / "results.csv"
    write_results_csv(path, results)

    roundtripped = read_results_csv(path)
    assert roundtripped["user-a"].ok is True
    assert roundtripped["user-b"].ok is False
    assert roundtripped["user-b"].status_code is None


def test_write_summary_computes_pass_rate(tmp_path):
    results = [make_result("a", True), make_result("b", True), make_result("c", False)]
    path = tmp_path / "summary.json"
    summary = write_summary(path, results, "https://dev.example.com/auth", "2026-09-25T00:00:00+00:00")

    assert summary["total"] == 3
    assert summary["passed"] == 2
    assert summary["failed"] == 1
    assert summary["pass_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert json.loads(path.read_text())["endpoint"] == "https://dev.example.com/auth"


def test_diff_against_previous_flags_regressions_and_recoveries():
    previous = {
        "user-a": make_result("user-a", True),
        "user-b": make_result("user-b", False),
        "user-c": make_result("user-c", True),
    }
    current = [
        make_result("user-a", False),  # regressed
        make_result("user-b", True),   # recovered
        # user-c missing this run
        make_result("user-d", True),   # new
    ]
    changes = diff_against_previous(current, previous)
    by_identifier = {c["identifier"]: c for c in changes}

    assert by_identifier["user-a"]["change"] == "regressed"
    assert by_identifier["user-b"]["change"] == "recovered"
    assert by_identifier["user-c"]["change"] == "missing"
    assert by_identifier["user-d"]["change"] == "new"


def test_diff_against_previous_no_changes_when_stable():
    previous = {"user-a": make_result("user-a", True)}
    current = [make_result("user-a", True)]
    assert diff_against_previous(current, previous) == []


def test_find_previous_run_dir_picks_most_recent_dated_folder(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    for date in ("2026-09-23", "2026-09-24", "2026-09-25"):
        d = root / date
        d.mkdir()
        (d / "results.csv").write_text("identifier,timestamp,status_code,ok,elapsed_ms,error\n")

    current = root / "2026-09-25"
    previous = find_previous_run_dir(root, current)
    assert previous.name == "2026-09-24"


def test_find_previous_run_dir_returns_none_when_only_current_exists(tmp_path):
    root = tmp_path / "runs"
    current = root / "2026-09-25"
    current.mkdir(parents=True)
    (current / "results.csv").write_text("identifier,timestamp,status_code,ok,elapsed_ms,error\n")
    assert find_previous_run_dir(root, current) is None


def test_find_previous_run_dir_ignores_folders_without_results(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    (root / "2026-09-24").mkdir()  # no results.csv inside
    current = root / "2026-09-25"
    current.mkdir()
    assert find_previous_run_dir(root, current) is None


def _mock_response(status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    return resp


def test_end_to_end_run_writes_dated_folder_and_flags_change(tmp_path):
    input_path = tmp_path / "accounts.txt"
    input_path.write_text("user-a\nuser-b\n")
    output_dir = tmp_path / "qa_reports"

    with patch("scout.tools.auth_smoke_test.requests.Session.request") as mock_request:
        mock_request.side_effect = [_mock_response(200), _mock_response(500)]
        exit_code = main([
            "--input", str(input_path),
            "--endpoint", "https://dev.example.com/auth/check",
            "--output-dir", str(output_dir),
            "--date", "2026-09-24",
            "--interval", "0",
        ])
    assert exit_code == 1

    run_dir = output_dir / "2026-09-24"
    assert (run_dir / "results.csv").exists()
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "changes.json").exists()

    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["total"] == 2
    assert summary["failed"] == 1

    # First run: no prior baseline, so nothing should be flagged as a change.
    assert json.loads((run_dir / "changes.json").read_text()) == []

    # Second run: user-b recovers -> should be flagged versus the first run.
    with patch("scout.tools.auth_smoke_test.requests.Session.request") as mock_request:
        mock_request.side_effect = [_mock_response(200), _mock_response(200)]
        exit_code = main([
            "--input", str(input_path),
            "--endpoint", "https://dev.example.com/auth/check",
            "--output-dir", str(output_dir),
            "--date", "2026-09-25",
            "--interval", "0",
        ])
    assert exit_code == 0

    changes = json.loads((output_dir / "2026-09-25" / "changes.json").read_text())
    assert len(changes) == 1
    assert changes[0]["identifier"] == "user-b"
    assert changes[0]["change"] == "recovered"


def test_dry_run_makes_no_requests_and_writes_nothing(tmp_path, capsys):
    input_path = tmp_path / "accounts.txt"
    input_path.write_text("user-a\nuser-b\n")
    output_dir = tmp_path / "qa_reports"

    exit_code = main([
        "--input", str(input_path),
        "--endpoint", "https://dev.example.com/auth/check",
        "--output-dir", str(output_dir),
        "--dry-run",
    ])
    assert exit_code == 0
    assert not output_dir.exists()
    out = capsys.readouterr().out
    assert "user-a" in out and "user-b" in out


def test_missing_input_file_returns_error_code(tmp_path):
    exit_code = main([
        "--input", str(tmp_path / "nope.txt"),
        "--endpoint", "https://dev.example.com/auth/check",
        "--output-dir", str(tmp_path / "qa_reports"),
    ])
    assert exit_code == 1
