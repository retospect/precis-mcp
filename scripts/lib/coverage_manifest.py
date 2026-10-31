#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Write ``.coverage.precis.json`` — the byte-fingerprint manifest for a
per-test-context coverage DB.

``scripts/test --coverage`` runs pytest with ``--cov-context=test`` so the
``.coverage`` SQLite DB records which lines each test id executed. The DB
itself stores no file-content hashes, so the python ``tests`` view cannot
tell whether the measured bytes are still what the index holds. This
script closes that gap: it reads the DB's ``file`` table (stdlib sqlite3 —
no coverage dependency), sha256s each measured path's current bytes, and
writes a JSON manifest next to the DB. The view treats the DB as
``measured`` only for files whose manifest hash matches the indexed
``bytes_sha256``.

Usage: ``coverage_manifest.py [db-path]`` (default ``.coverage``); the
manifest lands at the fixed sibling name ``.coverage.precis.json``.
``--suite-outcome <gh-actions outcome>`` marks the map ``partial`` when
the measured suite did not finish green (coverage-db uploads the map on
test reds — a partial map beats none), and ``--junit <xml>`` attaches the
failure count. The selector prints the marker; a partial map is still
used — missing edges widen statically, they do not corrupt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_VERSION = 1


def _junit_failed(path: Path | None) -> int | None:
    """failures+errors across a junit xml's testsuites, or None."""
    if path is None or not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        return sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    except (OSError, ET.ParseError, ValueError):
        return None


def write_manifest(
    db_path: Path,
    root: Path,
    *,
    suite_outcome: str | None = None,
    junit: Path | None = None,
) -> dict:
    """Hash every file recorded in ``db_path`` under ``root``; write JSON."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = [r[0] for r in con.execute("SELECT path FROM file")]
        contexts = sum(
            1
            for (c,) in con.execute("SELECT context FROM context")
            if c.split("|", 1)[0]
        )
    finally:
        con.close()

    files: dict[str, str] = {}
    for rel in rows:
        path = Path(rel)
        if not path.is_absolute():
            path = root / path
        try:
            files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue  # file deleted since the run — recorded as missing

    manifest = {
        "version": MANIFEST_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "contexts": contexts,
        "files": files,
    }
    if suite_outcome and suite_outcome != "success":
        manifest["partial"] = True
        failed = _junit_failed(junit)
        if failed is not None:
            manifest["failed_tests"] = failed
    out = db_path.parent / ".coverage.precis.json"
    out.write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db", nargs="?", default=".coverage")
    ap.add_argument(
        "--junit",
        type=Path,
        default=None,
        help="pytest --junitxml output; supplies the partial failure count",
    )
    ap.add_argument(
        "--suite-outcome",
        choices=["success", "failure", "cancelled", "skipped"],
        default=None,
        help="the measured suite's outcome — non-success marks the map partial",
    )
    args = ap.parse_args(argv[1:])
    db = Path(args.db)
    if not db.is_file():
        print(f"no coverage DB at {db}", file=sys.stderr)
        return 1
    manifest = write_manifest(
        db, db.parent.resolve(), suite_outcome=args.suite_outcome, junit=args.junit
    )
    line = (
        f"{db.parent / '.coverage.precis.json'}: {len(manifest['files'])} files hashed, "
        f"{manifest['contexts']} test contexts"
    )
    if manifest.get("partial"):
        line += f" — PARTIAL map ({manifest.get('failed_tests', '?')} failed tests)"
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
