"""Build a summariser gold set from the live corpus (read-only) for ``score_summary``.

Samples paper chunks that already carry an incumbent ``chunk_summaries`` row
(summarizer ``SUMMARIZER_NAME``), applying the claim path's eligibility filters
(skip kinds, min/max chars, ``no_index``, numeric-dump) plus
``retired_at IS NULL`` on refs and chunks. For each chunk it rebuilds the exact
production prompt (``_Claimed`` -> ``fetch_doc_card`` -> ``build_messages``) and
emits a ``GoldTask`` (axis ``summarize-extract``, scorer ``summary``) whose
``messages`` replay that prompt verbatim against a candidate model.

Read-only: one transaction opened with ``SET TRANSACTION READ ONLY``
(transaction-scoped; never a session-level SET, which would poison
pgbouncer's pooled server connections), plain SELECTs, no
``chunk_claims``, no ``FOR UPDATE``.

Bias: ``expect.nonprose`` is derived from the *incumbent* summary (a brief that
is a parenthetical tag), i.e. the incumbent model's judgement is the label. A
candidate that disagrees with the incumbent on a borderline chunk is scored
wrong even when it is right. ~25% of the sample is drawn from incumbent-tag
chunks (when that many exist) so the tag path is exercised at all.

The output holds real paper text: it defaults to ``gold_set/local/`` which is
gitignored (the repo is PUBLIC) — never commit it.

    uv run python scripts/llm_eval/build_summarize_gold.py --n 40 --seed 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

AXIS = "summarize-extract"
DEFAULT_OUT = "scripts/llm_eval/gold_set/local/summarize_v1.json"

_COLS = (
    "c.chunk_id, c.ref_id, c.ord, c.chunk_kind, c.text, "
    "c.section_path, c.keywords, c.numerics, r.kind AS ref_kind, r.title"
)

# ``{tag_pred}`` selects incumbent-tag (nonprose) vs prose chunks.
_SAMPLE_SQL = f"""
    SELECT {_COLS}, cs.text AS incumbent
      FROM chunks c
      JOIN refs r ON r.ref_id = c.ref_id
      JOIN chunk_summaries cs
        ON cs.chunk_id = c.chunk_id AND cs.summarizer = %(summarizer)s
     WHERE r.kind = 'paper'
       AND r.retired_at IS NULL
       AND c.retired_at IS NULL
       AND c.chunk_kind <> ALL(%(skip_kinds)s)
       AND length(c.text) >= %(min_chars)s
       AND length(c.text) <= %(max_chars)s
       AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
       AND {{tag_pred}}
     ORDER BY md5(c.chunk_id::text || %(seed)s)
     LIMIT %(limit)s
"""

_TAG_PRED = "split_part(cs.text, E'\\n', 1) ~ '^\\s*\\(.*\\)\\s*$'"


def incumbent_is_tag(summary: str) -> bool:
    """Is the incumbent summary's brief a parenthetical tag?"""
    brief = (summary or "").split("\n", 1)[0].strip()
    return brief.startswith("(") and brief.endswith(")")


def row_to_task(row: dict[str, Any], doc_card: str) -> dict[str, Any]:
    """One sampled row (+ its doc card) -> a ``GoldTask`` JSON object."""
    from precis.workers.llm_summarize import _Claimed, build_messages

    claim = _Claimed(
        chunk_id=int(row["chunk_id"]),
        ref_id=int(row["ref_id"]),
        ord=int(row["ord"]),
        chunk_kind=str(row["chunk_kind"]),
        text=str(row["text"]),
        section_path=list(row["section_path"] or []),
        keywords=row["keywords"],
        numerics=list(row["numerics"] or []),
        ref_kind=str(row["ref_kind"]),
        title=str(row["title"] or ""),
    )
    incumbent = str(row["incumbent"] or "")
    return {
        "task_id": f"summ-{claim.chunk_id}",
        "axis": AXIS,
        "scorer": "summary",
        "prompt": "",
        "messages": build_messages(claim, doc_card=doc_card),
        "expect": {
            "chunk_text": claim.text,
            "nonprose": incumbent_is_tag(incumbent),
            "incumbent": incumbent,
        },
    }


def sample_rows(conn: Any, *, n: int, seed: str) -> list[dict[str, Any]]:
    """Stratified sample: ~25% incumbent-tag chunks (as many as exist), rest prose."""
    from psycopg.rows import dict_row

    from precis.workers.llm_summarize import (
        MAX_CHUNK_CHARS,
        MIN_CHUNK_CHARS,
        SKIP_KINDS,
        SUMMARIZER_NAME,
        _is_numeric_dump,
    )

    base = {
        "summarizer": SUMMARIZER_NAME,
        "skip_kinds": sorted(SKIP_KINDS),
        "min_chars": MIN_CHUNK_CHARS,
        "max_chars": MAX_CHUNK_CHARS,
        "seed": seed,
    }
    want_tag = max(1, n // 4)

    def fetch(pred: str, limit: int) -> list[dict[str, Any]]:
        # over-fetch: numeric dumps are dropped in Python, as in the worker.
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SAMPLE_SQL.format(tag_pred=pred), {**base, "limit": limit * 2})
            rows = [r for r in cur.fetchall() if not _is_numeric_dump(r["text"])]
        return rows[:limit]

    tags = fetch(_TAG_PRED, want_tag)
    prose = fetch(f"NOT ({_TAG_PRED})", n - len(tags))
    return tags + prose


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--database-url", default=None, help="else PRECIS_DATABASE_URL")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    from precis.cli._common import resolve_dsn
    from precis.store.store import Store
    from precis.workers.llm_summarize import fetch_doc_card

    store = Store.connect(resolve_dsn(args.database_url))
    try:
        with store.pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            rows = sample_rows(conn, n=args.n, seed=str(args.seed))
            cards: dict[int, str] = {}
            tasks = []
            for row in rows:
                rid = int(row["ref_id"])
                if rid not in cards:
                    cards[rid] = fetch_doc_card(conn, rid)
                tasks.append(row_to_task(row, cards[rid]))
            conn.rollback()
    finally:
        store.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tasks, indent=1, ensure_ascii=False), encoding="utf-8")
    n_tag = sum(1 for t in tasks if t["expect"]["nonprose"])
    print(f"wrote {len(tasks)} tasks ({n_tag} nonprose) -> {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
