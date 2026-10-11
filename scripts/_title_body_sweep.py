"""title-body-sweep — read-only title/body mismatch outlier list (gr264184).

Pages live papers from prod via ``scripts/prod-psql --ro`` (CSV over ``COPY``),
scores each with :func:`precis.utils.title_body.title_body_score` against the
first ``--chunks`` body paragraphs, and prints the lowest scorers. SELECT only.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

PROD_PSQL = Path(__file__).resolve().parent / "prod-psql"
PAGE = 500
MIN_BODY_CHARS = 600  # shorter bodies are stubs/abstract-only: nothing to judge


def _psql(sql: str) -> str:
    env = {**os.environ, "PRECIS_PROD_PSQL_OPTS": "-q"}
    res = subprocess.run(
        [str(PROD_PSQL), "--ro", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )
    if res.returncode != 0:
        sys.exit(f"prod-psql failed ({res.returncode}): {res.stderr.strip()}")
    return res.stdout


def _select(where: str, n_chunks: int, limit: int) -> str:
    return (
        "COPY (SELECT r.ref_id, r.title, COALESCE(b.body, '') FROM refs r "
        "CROSS JOIN LATERAL (SELECT string_agg(text, ' ' ORDER BY ord) AS body "
        "FROM (SELECT text, ord FROM chunks WHERE ref_id = r.ref_id "
        "AND chunk_kind = 'paragraph' AND ord >= 0 AND retired_at IS NULL "
        f"ORDER BY ord LIMIT {n_chunks}) c) b "
        f"WHERE r.kind = 'paper' AND r.retired_at IS NULL AND {where} "
        f"ORDER BY r.ref_id LIMIT {limit}) TO STDOUT WITH CSV;"
    )


def _rows(sql: str) -> list[tuple[int, str, str]]:
    return [(int(r[0]), r[1], r[2]) for r in csv.reader(io.StringIO(_psql(sql)))]


def fetch(
    n_chunks: int, limit: int | None, refs: list[int]
) -> Iterator[tuple[int, str, str]]:
    if refs:
        ids = ",".join(str(int(i)) for i in refs)
        where = f"r.ref_id = ANY(ARRAY[{ids}]::bigint[])"
        yield from _rows(_select(where, n_chunks, len(refs)))
        return
    after, seen = 0, 0
    while limit is None or seen < limit:
        take = PAGE if limit is None else min(PAGE, limit - seen)
        page = _rows(_select(f"r.ref_id > {after}", n_chunks, take))
        if not page:
            return
        yield from page
        seen += len(page)
        after = page[-1][0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chunks", type=int, default=12, help="leading paragraphs")
    ap.add_argument("--limit", type=int, default=None, help="max papers scanned")
    ap.add_argument("--top", type=int, default=40, help="outliers to print")
    ap.add_argument("--ref", type=int, action="append", default=[], help="ref_id")
    args = ap.parse_args()

    from precis.utils.title_body import is_placeholder_title, title_body_score

    scored = []
    placeholders: list[tuple[int, str]] = []
    scanned = skipped = 0
    for ref_id, title, body in fetch(args.chunks, args.limit, args.ref):
        scanned += 1
        if is_placeholder_title(title):
            placeholders.append((ref_id, title))
            continue
        s = title_body_score(title, body) if len(body) >= MIN_BODY_CHARS else None
        if s is None:
            skipped += 1
            continue
        scored.append((s.rank_score, ref_id, title, s))
    scored.sort(key=lambda t: (t[0], t[1]))
    print(
        f"scanned {scanned}, scored {len(scored)}, skipped {skipped} "
        f"(stub/no terms), placeholder titles {len(placeholders)}"
    )
    for score, ref_id, title, s in scored[: args.top]:
        miss = ",".join(s.missing[:6])
        print(f"{score:.2f}  pa{ref_id}  {title[:90]!r}  missing={miss}")
    print(f"\n== placeholder titles ({len(placeholders)}; retitle, not mismatch) ==")
    for ref_id, title in placeholders[: args.top]:
        print(f"pa{ref_id}  {title[:90]!r}")


if __name__ == "__main__":
    main()
