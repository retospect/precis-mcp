"""gist-cleanup — delete leaked-reasoning LLM chunk summaries (gr478547).

Fetches ``chunk_summaries`` rows (summarizer ``llm-v1``, status ``ok``) from
prod via ``scripts/prod-psql`` (CSV over ``COPY ... TO STDOUT``), decides with
:func:`precis.workers.llm_summarize._reject_reason` — exact parity with the
worker's own gate — and, with ``--apply``, deletes the rejected rows by
primary key ``(chunk_id, summarizer)``. The summarizer then re-claims chunks
whose summary row is missing. Dry run reads through the ``--ro`` role.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path

SUMMARIZER = "llm-v1"
PROD_PSQL = Path(__file__).resolve().parent / "prod-psql"
PAGE = 20000


def reject_rows(rows: Iterable[tuple[int, str]]) -> list[tuple[int, str, str]]:
    """Return ``(chunk_id, reason, text)`` for every row the gate rejects."""
    from precis.workers.llm_summarize import _reject_reason

    out: list[tuple[int, str, str]] = []
    for chunk_id, text in rows:
        reason = _reject_reason(text)
        if reason is not None:
            out.append((chunk_id, reason, text))
    return out


def reason_class(reason: str) -> str:
    """Collapse a reason to its kind (drop the quoted needle / numbers)."""
    return reason.split(" '", 1)[0].split(" (", 1)[0]


def _psql(sql: str, *, ro: bool) -> str:
    cmd = [str(PROD_PSQL)] + (["--ro"] if ro else []) + [sql]
    env = {**os.environ, "PRECIS_PROD_PSQL_OPTS": "-q"}  # no BEGIN/COMMIT tags
    res = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=False
    )
    if res.returncode != 0:
        sys.exit(f"prod-psql failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout


def fetch_rows() -> Iterator[tuple[int, str]]:
    """Keyset-paged scan of the candidate rows."""
    after = 0
    while True:
        out = _psql(
            "COPY (SELECT chunk_id, COALESCE(text, '') FROM chunk_summaries "
            f"WHERE summarizer = '{SUMMARIZER}' AND status = 'ok' "
            f"AND chunk_id > {after} ORDER BY chunk_id LIMIT {PAGE}) "
            "TO STDOUT WITH CSV;",
            ro=True,
        )
        page = [(int(r[0]), r[1]) for r in csv.reader(io.StringIO(out))]
        if not page:
            return
        yield from page
        after = page[-1][0]


def delete_batch(ids: list[int]) -> None:
    arr = ",".join(str(int(i)) for i in ids)
    _psql(
        "BEGIN; DELETE FROM chunk_summaries "
        f"WHERE summarizer = '{SUMMARIZER}' AND status = 'ok' "
        f"AND chunk_id = ANY(ARRAY[{arr}]::bigint[]); COMMIT;",
        ro=False,
    )


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="delete rejected rows")
    p.add_argument("--sample", type=int, default=10, help="sample rows to print")
    p.add_argument("--batch", type=int, default=500, help="rows per delete txn")
    args = p.parse_args()

    scanned = 0

    def counted() -> Iterator[tuple[int, str]]:
        nonlocal scanned
        for row in fetch_rows():
            scanned += 1
            yield row

    rejected = reject_rows(counted())
    by_reason = Counter(reason_class(r) for _, r, _ in rejected)
    print(f"scanned:  {scanned}")
    print(f"rejected: {len(rejected)}")
    for reason, n in by_reason.most_common():
        print(f"  {n:>8}  {reason}")
    print("sample:")
    for chunk_id, reason, text in rejected[: args.sample]:
        print(f"  {chunk_id}  {reason}  {text[:80]!r}")

    if not args.apply:
        print("dry run — pass --apply to delete")
        return
    ids = [c for c, _, _ in rejected]
    for i in range(0, len(ids), args.batch):
        delete_batch(ids[i : i + args.batch])
        print(f"deleted {min(i + args.batch, len(ids))}/{len(ids)}")


if __name__ == "__main__":
    main()
