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
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

MANIFEST_VERSION = 1


def write_manifest(db_path: Path, root: Path) -> dict:
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
    out = db_path.parent / ".coverage.precis.json"
    out.write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str]) -> int:
    db = Path(argv[1]) if len(argv) > 1 else Path(".coverage")
    if not db.is_file():
        print(f"no coverage DB at {db}", file=sys.stderr)
        return 1
    manifest = write_manifest(db, db.parent.resolve())
    print(
        f"{db.parent / '.coverage.precis.json'}: {len(manifest['files'])} files hashed, "
        f"{manifest['contexts']} test contexts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
