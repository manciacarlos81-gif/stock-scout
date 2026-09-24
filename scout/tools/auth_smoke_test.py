#!/usr/bin/env python3
"""QA smoke test for the dev authentication endpoint.

Reads a list of seeded test-account identifiers from a local file and submits
them one at a time, at a fixed configurable interval, against a development
auth endpoint the team owns. Records each response's status alongside the
identifier that produced it, so a bad deploy/migration that broke test-account
login shows up as a diff in the output file.

This tool talks only to endpoints you explicitly pass in --endpoint. It is
meant for internal dev/staging environments the caller controls.

Usage:
    python -m scout.tools.auth_smoke_test \\
        --input accounts.txt \\
        --endpoint https://dev.internal.example.com/api/auth/check \\
        --interval 1.0 \\
        --output results.csv

Input file format: one identifier per line. Blank lines and lines starting
with '#' are ignored.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("auth_smoke_test")


@dataclass
class ProbeResult:
    identifier: str
    timestamp: str
    status_code: Optional[int]
    ok: bool
    elapsed_ms: Optional[float]
    error: Optional[str]


def load_identifiers(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"input file not found: {path}")

    identifiers: list[str] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            identifiers.append(line)
            logger.debug("loaded identifier at line %d: %s", lineno, line)

    if not identifiers:
        raise ValueError(f"no identifiers found in {path}")

    return identifiers


def build_session(timeout: float, retries: int, backoff: float) -> requests.Session:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.packages.urllib3.util.retry.Retry(
            total=retries,
            backoff_factor=backoff,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.request = _with_timeout(session.request, timeout)  # type: ignore[method-assign]
    return session


def _with_timeout(request_fn, timeout: float):
    def wrapped(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return request_fn(*args, **kwargs)

    return wrapped


def probe_identifier(
    session: requests.Session,
    endpoint: str,
    identifier: str,
    method: str,
    field_name: str,
    extra_fields: dict,
    use_query_params: bool,
) -> ProbeResult:
    payload = {field_name: identifier, **extra_fields}
    timestamp = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()

    try:
        if use_query_params:
            response = session.request(method, endpoint, params=payload)
        else:
            response = session.request(method, endpoint, json=payload)
        elapsed_ms = (time.monotonic() - started) * 1000
        return ProbeResult(
            identifier=identifier,
            timestamp=timestamp,
            status_code=response.status_code,
            ok=response.ok,
            elapsed_ms=round(elapsed_ms, 2),
            error=None,
        )
    except requests.exceptions.RequestException as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.warning("request failed for %s: %s", identifier, exc)
        return ProbeResult(
            identifier=identifier,
            timestamp=timestamp,
            status_code=None,
            ok=False,
            elapsed_ms=round(elapsed_ms, 2),
            error=str(exc),
        )


def write_results_csv(path: Path, results: list[ProbeResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["identifier", "timestamp", "status_code", "ok", "elapsed_ms", "error"])
        for r in results:
            writer.writerow([r.identifier, r.timestamp, r.status_code, r.ok, r.elapsed_ms, r.error])


def write_results_jsonl(path: Path, results: list[ProbeResult]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r.__dict__) + "\n")


def parse_extra_fields(pairs: list[str]) -> dict:
    extra = {}
    for pair in pairs:
        if "=" not in pair:
            raise argparse.ArgumentTypeError(f"--extra-field must be key=value, got: {pair}")
        key, _, value = pair.partition("=")
        extra[key] = value
    return extra


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Submit test-account identifiers to a dev auth endpoint at a fixed interval "
        "and record the response status for each.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", "-i", required=True, type=Path, help="path to file with one identifier per line")
    parser.add_argument("--endpoint", "-e", required=True, help="dev authentication endpoint URL")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds to wait between requests")
    parser.add_argument("--output", "-o", type=Path, default=Path("auth_smoke_test_results.csv"), help="output file path")
    parser.add_argument(
        "--output-format", choices=["csv", "jsonl"], default="csv", help="output file format"
    )
    parser.add_argument("--method", default="POST", help="HTTP method to use")
    parser.add_argument(
        "--field-name", default="identifier", help="request field/param name that carries the identifier"
    )
    parser.add_argument(
        "--extra-field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="additional static field to send with every request (repeatable)",
    )
    parser.add_argument(
        "--use-query-params",
        action="store_true",
        help="send fields as URL query parameters instead of a JSON body",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    parser.add_argument("--retries", type=int, default=2, help="retries for 5xx / connection errors")
    parser.add_argument("--retry-backoff", type=float, default=0.5, help="backoff factor between retries")
    parser.add_argument(
        "--dry-run", action="store_true", help="load and print identifiers without sending any requests"
    )
    parser.add_argument("-v", "--verbose", action="count", default=0, help="increase output verbosity (-v, -vv)")
    parser.add_argument("--log-file", type=Path, default=None, help="optional path to also write logs to a file")

    args = parser.parse_args(argv)
    args.extra_fields = parse_extra_fields(args.extra_field)
    return args


def configure_logging(verbosity: int, log_file: Optional[Path]) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=handlers,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose, args.log_file)

    try:
        identifiers = load_identifiers(args.input)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 1

    logger.info("loaded %d identifier(s) from %s", len(identifiers), args.input)

    if args.dry_run:
        for identifier in identifiers:
            print(identifier)
        logger.info("dry run complete, no requests sent")
        return 0

    session = build_session(timeout=args.timeout, retries=args.retries, backoff=args.retry_backoff)

    results: list[ProbeResult] = []
    total = len(identifiers)
    exit_code = 0

    for index, identifier in enumerate(identifiers, start=1):
        logger.info("probing %d/%d: %s", index, total, identifier)
        result = probe_identifier(
            session=session,
            endpoint=args.endpoint,
            identifier=identifier,
            method=args.method,
            field_name=args.field_name,
            extra_fields=args.extra_fields,
            use_query_params=args.use_query_params,
        )
        results.append(result)

        if result.ok:
            logger.info("%s -> %s (%.2f ms)", identifier, result.status_code, result.elapsed_ms or 0.0)
        else:
            exit_code = 1
            if result.error:
                logger.error("%s -> ERROR: %s", identifier, result.error)
            else:
                logger.error("%s -> %s (%.2f ms)", identifier, result.status_code, result.elapsed_ms or 0.0)

        if index < total:
            time.sleep(args.interval)

    if args.output_format == "csv":
        write_results_csv(args.output, results)
    else:
        write_results_jsonl(args.output, results)

    logger.info("wrote %d result(s) to %s", len(results), args.output)

    failures = sum(1 for r in results if not r.ok)
    if failures:
        logger.warning("%d/%d identifier(s) did not authenticate successfully", failures, total)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
