"""title-body-sweep — read-only title/body mismatch outlier list (gr264184).

Pages live papers from prod via ``scripts/prod-psql --ro`` (CSV over ``COPY``),
scores each with :func:`precis.utils.title_body.title_body_score` against the
first ``--chunks`` body paragraphs, and prints the lowest scorers. SELECT only.

``--multi-article`` (gr353777, ruled 2026-10-12) instead lists *candidate*
multi-article scans: papers whose first ``--head`` paragraphs disagree with
the title while the rest agree (foreign lead), or the reverse (foreign tail).
It only lists — a human reads each candidate and applies the ``multi-article``
tag by hand. Already-tagged papers are excluded from the corpus scan (an
explicit ``--ref`` still scores, so the known cases can be re-checked).
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

PROD_PSQL = Path(__file__).resolve().parent / "prod-psql"
PAGE = 500
MIN_BODY_CHARS = 600  # shorter bodies are stubs/abstract-only: nothing to judge
MIN_HEAD_CHARS = 400  # a shorter lead window can't carry or clear the title

_CHUNK_FILTER = (
    "FROM chunks WHERE ref_id = r.ref_id "
    "AND chunk_kind = 'paragraph' AND ord >= 0 AND retired_at IS NULL"
)


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
        f"({_CHUNK_FILTER} ORDER BY ord LIMIT {n_chunks}) c) b "
        f"WHERE r.kind = 'paper' AND r.retired_at IS NULL AND {where} "
        f"ORDER BY r.ref_id LIMIT {limit}) TO STDOUT WITH CSV;"
    )


def _select_multi(where: str, head: int, limit: int, *, skip_tagged: bool) -> str:
    tagged = (
        "AND NOT EXISTS (SELECT 1 FROM ref_tags rt "
        "JOIN tags tg ON tg.tag_id = rt.tag_id "
        "WHERE rt.ref_id = r.ref_id AND tg.namespace = 'OPEN' "
        "AND tg.value = 'multi-article' "
        "AND (rt.expires_at IS NULL OR rt.expires_at > now())) "
        if skip_tagged
        else ""
    )
    return (
        "COPY (SELECT r.ref_id, r.title, "
        "COALESCE(h.body, ''), COALESCE(t.body, '') FROM refs r "
        "CROSS JOIN LATERAL (SELECT string_agg(text, ' ' ORDER BY ord) AS body "
        f"({_CHUNK_FILTER} ORDER BY ord LIMIT {head}) c) h "
        "CROSS JOIN LATERAL (SELECT string_agg(text, ' ' ORDER BY ord) AS body "
        f"({_CHUNK_FILTER} ORDER BY ord OFFSET {head}) c) t "
        f"WHERE r.kind = 'paper' AND r.retired_at IS NULL {tagged}AND {where} "
        f"ORDER BY r.ref_id LIMIT {limit}) TO STDOUT WITH CSV;"
    )


def _rows(sql: str) -> list[tuple[int, str, str]]:
    return [(int(r[0]), r[1], r[2]) for r in csv.reader(io.StringIO(_psql(sql)))]


def _rows_multi(sql: str) -> list[tuple[int, str, str, str]]:
    return [(int(r[0]), r[1], r[2], r[3]) for r in csv.reader(io.StringIO(_psql(sql)))]


def _paged(
    select: Callable[[str, int], list[tuple]],
    where_refs: list[int],
    limit: int | None,
) -> Iterator[tuple]:
    if where_refs:
        ids = ",".join(str(int(i)) for i in where_refs)
        yield from select(f"r.ref_id = ANY(ARRAY[{ids}]::bigint[])", len(where_refs))
        return
    after, seen = 0, 0
    while limit is None or seen < limit:
        take = PAGE if limit is None else min(PAGE, limit - seen)
        page = select(f"r.ref_id > {after}", take)
        if not page:
            return
        yield from page
        seen += len(page)
        after = page[-1][0]


def fetch(
    n_chunks: int, limit: int | None, refs: list[int]
) -> Iterator[tuple[int, str, str]]:
    yield from _paged(
        lambda where, take: _rows(_select(where, n_chunks, take)), refs, limit
    )


def fetch_multi(
    head: int, limit: int | None, refs: list[int]
) -> Iterator[tuple[int, str, str, str]]:
    # An explicit --ref re-checks known cases, so it ignores the tag.
    yield from _paged(
        lambda where, take: _rows_multi(
            _select_multi(where, head, take, skip_tagged=not refs)
        ),
        refs,
        limit,
    )


def multi_verdict(
    head_rank: float, tail_rank: float, agree: float, disagree: float
) -> str | None:
    """``lead``/``tail`` when one end of the body fits the title and the other
    does not; ``None`` when both ends agree (normal) or both fail (a plain
    title/body mismatch — the main ranking's territory)."""
    if head_rank < disagree and tail_rank >= agree:
        return "lead"
    if tail_rank < disagree and head_rank >= agree:
        return "tail"
    return None


def _sweep(args: argparse.Namespace) -> None:
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


def _multi(args: argparse.Namespace) -> None:
    from precis.utils.title_body import is_placeholder_title, title_body_score

    cands = []
    scanned = skipped = placeholders = 0
    for ref_id, title, head, tail in fetch_multi(args.head, args.limit, args.ref):
        scanned += 1
        if is_placeholder_title(title):
            placeholders += 1
            continue
        hs = title_body_score(title, head) if len(head) >= MIN_HEAD_CHARS else None
        ts = title_body_score(title, tail) if len(tail) >= MIN_BODY_CHARS else None
        if hs is None or ts is None:
            skipped += 1
            continue
        side = multi_verdict(hs.rank_score, ts.rank_score, args.agree, args.disagree)
        if side is not None:
            cands.append(
                (abs(hs.rank_score - ts.rank_score), side, ref_id, title, hs, ts)
            )
    cands.sort(key=lambda t: (-t[0], t[2]))
    print(
        f"scanned {scanned}, candidates {len(cands)}, skipped {skipped} "
        f"(short side/no terms), placeholder titles {placeholders} "
        f"(disagree <{args.disagree}, agree >={args.agree})"
    )
    for _, side, ref_id, title, hs, ts in cands[: args.top]:
        print(
            f"{hs.rank_score:.2f}/{ts.rank_score:.2f}  {side}  pa{ref_id}  "
            f"{title[:80]!r}  missing({side})="
            f"{','.join((hs if side == 'lead' else ts).missing[:6])}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--multi-article",
        action="store_true",
        help="list multi-article candidates (read-only; humans tag)",
    )
    ap.add_argument("--chunks", type=int, default=12, help="leading paragraphs")
    ap.add_argument("--head", type=int, default=6, help="lead-window paragraphs")
    ap.add_argument("--agree", type=float, default=0.5, help="fit threshold")
    ap.add_argument("--disagree", type=float, default=0.3, help="misfit ceiling")
    ap.add_argument("--limit", type=int, default=None, help="max papers scanned")
    ap.add_argument("--top", type=int, default=40, help="outliers to print")
    ap.add_argument("--ref", type=int, action="append", default=[], help="ref_id")
    args = ap.parse_args()
    _multi(args) if args.multi_article else _sweep(args)


if __name__ == "__main__":
    main()
