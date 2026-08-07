"""Read-only summaries and exports for the local C# snippet corpus.

Examples:
    python scripts/export_csharp_history.py
    python scripts/export_csharp_history.py --format jsonl --output snippet_exports/snippets.jsonl
    python scripts/export_csharp_history.py --format csv --status completed --since 2026-08-01

JSONL and CSV contain exact source unless ``--without-code`` is supplied. Even
without source, results, captured output, options, and diagnostics can contain
sensitive values. CSV text cells are spreadsheet-formula hardened.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Iterable, Optional, TextIO

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from solidworks.csharp_history import default_history_path  # noqa: E402


JSON_COLUMNS = {
    "diagnostics_json": "diagnostics",
    "exception_json": "exception",
    "interop_symbols_json": "interopSymbols",
    "result_json": "result",
    "options_json": "options",
    "state_changes_json": "stateChanges",
    "rebuild_json": "rebuild",
}


def read_attempts(
    path: Path,
    *,
    since: Optional[str] = None,
    statuses: Optional[list[str]] = None,
) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"Snippet history does not exist: {path}")
    clauses = []
    params: list[str] = []
    if since:
        clauses.append("started_at_utc >= ?")
        params.append(since)
    if statuses:
        placeholders = ",".join("?" for _ in statuses)
        clauses.append(f"status IN ({placeholders})")
        params.extend(statuses)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT * FROM snippet_attempts{where} ORDER BY started_at_utc",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def decoded_row(row: dict, *, include_code: bool = True) -> dict:
    output = {}
    for key, value in row.items():
        if key == "code" and not include_code:
            continue
        mapped = JSON_COLUMNS.get(key, key)
        if key in JSON_COLUMNS and value is not None:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {"decodeError": True, "raw": value}
        output[mapped] = value
    return output


def summarize(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    statuses = Counter(row.get("status") or "unknown" for row in rows)
    operations = Counter(row.get("operation") or "unknown" for row in rows)
    hashes = Counter(row.get("code_sha256") for row in rows if row.get("code_sha256"))
    symbols: Counter[tuple[str, str]] = Counter()
    symbol_attempts: Counter[tuple[str, str]] = Counter()
    for row in rows:
        try:
            values = json.loads(row.get("interop_symbols_json") or "[]")
        except json.JSONDecodeError:
            continue
        seen_in_attempt = set()
        for value in values:
            signature = value.get("signature") if isinstance(value, dict) else None
            if signature:
                resolution = value.get("resolution") or "legacy_unspecified"
                key = (signature, resolution)
                occurrences = value.get("occurrenceCount", 1)
                if isinstance(occurrences, bool) or not isinstance(occurrences, int):
                    occurrences = 1
                symbols[key] += max(occurrences, 1)
                seen_in_attempt.add(key)
        for key in seen_in_attempt:
            symbol_attempts[key] += 1
    return {
        "attempts": len(rows),
        "byStatus": dict(statuses.most_common()),
        "byOperation": dict(operations.most_common()),
        "repeatedHashes": [
            {"codeSha256": digest, "attempts": count}
            for digest, count in hashes.most_common()
            if count > 1
        ],
        "topInteropSymbols": [
            {
                "signature": key[0],
                "resolution": key[1],
                "occurrences": count,
                "attempts": symbol_attempts[key],
            }
            for key, count in symbols.most_common(50)
        ],
    }


def write_jsonl(rows: Iterable[dict], output: TextIO, *, include_code: bool) -> None:
    for row in rows:
        output.write(
            json.dumps(decoded_row(row, include_code=include_code), ensure_ascii=False)
            + "\n"
        )


def write_csv(rows: Iterable[dict], output: TextIO, *, include_code: bool) -> None:
    decoded = [decoded_row(row, include_code=include_code) for row in rows]
    if not decoded:
        return
    fields = list(decoded[0])
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in decoded:
        def csv_value(value):
            rendered = (
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if isinstance(value, (dict, list))
                else value
            )
            if isinstance(rendered, str) and rendered.lstrip(" \t\r\n").startswith(
                ("=", "+", "-", "@")
            ):
                return "'" + rendered
            return rendered

        writer.writerow(
            {key: csv_value(value) for key, value in row.items()}
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_history_path())
    parser.add_argument("--format", choices=("summary", "jsonl", "csv"), default="summary")
    parser.add_argument("--output", type=Path, help="Output file (stdout when omitted)")
    parser.add_argument("--since", help="Minimum UTC timestamp/date, compared as ISO-8601 text")
    parser.add_argument("--status", action="append", dest="statuses", help="Include one status; repeatable")
    parser.add_argument(
        "--without-code",
        action="store_true",
        help="Omit exact source only; other exported fields may remain sensitive",
    )
    args = parser.parse_args(argv)

    try:
        rows = read_attempts(args.db, since=args.since, statuses=args.statuses)
    except (FileNotFoundError, sqlite3.Error) as exc:
        parser.error(str(exc))

    stream: TextIO
    close_stream = False
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        stream = args.output.open("w", encoding="utf-8", newline="")
        close_stream = True
    else:
        stream = sys.stdout
    try:
        if args.format == "summary":
            json.dump(summarize(rows), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        elif args.format == "jsonl":
            print(
                "WARNING: export may contain sensitive values even with --without-code",
                file=sys.stderr,
            )
            write_jsonl(rows, stream, include_code=not args.without_code)
        else:
            print(
                "WARNING: export may contain sensitive values even with --without-code",
                file=sys.stderr,
            )
            write_csv(rows, stream, include_code=not args.without_code)
    finally:
        if close_stream:
            stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
