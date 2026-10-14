"""Build the ``finding`` lane's frozen eval set from prod (read-only).

Task (``local-mesh-upkeep.md`` §1 row 3): given a paper chunk, state the
claims it supports. Gold = the claim hubs whose edge to that chunk is
verified (``links.meta.verified_by``, the ``hub-refine`` supported
verdict); hubs at nanopub ``anchored``/``published`` weight double — the
spec's "anchored and published hubs weighted up", since their claims are
the ones the corpus has committed to.

The deterministic scorer measures weighted title coverage; the spec's
real grade is the frontier claim-match judge on a sample — wire it via
``precis mesh eval --judge-*`` (that judging spend IS the reviewer cost
the reviewed bar reads).

    uv run python scripts/llm_eval/build_mesh_finding_gold.py --prod \
        --n 120 --holdout 20
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _prod_ro import add_db_args, connect_ro

DEFAULT_OUT = "scripts/llm_eval/gold_set/local/mesh_finding_v1.jsonl"
DEFAULT_HOLDOUT = "scripts/llm_eval/gold_set/local/mesh_finding_holdout_v1.jsonl"

#: Chunks carrying verified corroborates edges, with the hub's claim title
#: and its nanopub state for weighting. Stratified by ``premium``: chunks
#: touching an anchored/published hub are drawn in their own pool so the
#: weighted side is exercised.
_POOL_SQL = """
    SELECT c.chunk_id, c.text, p.title AS paper_title,
           r.ref_id AS hub_id, r.title AS claim,
           (np.state IN ('anchored', 'publishing', 'published')) AS premium
      FROM links l
      JOIN chunks c ON c.chunk_id = l.src_chunk_id AND c.retired_at IS NULL
      JOIN refs p ON p.ref_id = c.ref_id
      JOIN refs r ON r.ref_id = l.dst_ref_id
                   AND r.kind = 'finding' AND r.retired_at IS NULL
      LEFT JOIN nanopub_publish np ON np.claim_ref_id = r.ref_id
     WHERE l.meta ? 'verified_by' AND l.relation = 'corroborates'
       AND length(c.text) BETWEEN 200 AND 6000
     ORDER BY md5(c.chunk_id::text || %(seed)s)
"""


def _prompt(chunk_text: str, paper_title: str) -> str:
    return (
        f"The following passage is from the paper {paper_title!r}.\n\n"
        f"Passage: {chunk_text}\n\n"
        "List the distinct scientific claims this passage's own evidence "
        "supports — each a self-contained statement a knowledge graph could "
        "hold as a claim node. Reply with a JSON array of strings and "
        "nothing else. If the passage supports no standalone claim, "
        "reply []."
    )


def build_items(conn: Any, *, n_pool: int, seed: str) -> list[Any]:
    from precis.mesh_eval.sets import MeshItem

    rows = conn.execute(_POOL_SQL, {"seed": seed}).fetchall()
    by_chunk: dict[int, dict[str, Any]] = {}
    for chunk_id, text, paper_title, hub_id, claim, premium in rows:
        rec = by_chunk.setdefault(
            int(chunk_id),
            {"text": str(text), "paper_title": str(paper_title or ""), "claims": []},
        )
        rec["claims"].append(
            {
                "hub_ref_id": int(hub_id),
                "title": str(claim),
                "weight": 2.0 if premium else 1.0,
            }
        )
        rec["premium"] = rec.get("premium", False) or bool(premium)

    # Stratified: half the pool from chunks that touch a premium hub.
    premium = [c for c, r in by_chunk.items() if r.get("premium")]
    plain = [c for c, r in by_chunk.items() if not r.get("premium")]
    want_premium = min(len(premium), (n_pool + 1) // 2)
    chosen = premium[:want_premium] + plain[: n_pool - want_premium]

    items: list[MeshItem] = []
    for chunk_id in chosen:
        rec = by_chunk[chunk_id]
        items.append(
            MeshItem(
                item_id=f"find-{chunk_id}",
                lane="finding",
                prompt=_prompt(rec["text"], rec["paper_title"]),
                gold={"claims": rec["claims"]},
                meta={
                    "chunk_id": chunk_id,
                    "paper_title": rec["paper_title"],
                    "gold_source": "verified_by corroborates",
                },
            )
        )
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--holdout", type=int, default=20)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--holdout-out", default=DEFAULT_HOLDOUT)
    args = ap.parse_args(argv)

    from precis.mesh_eval.sets import dump_set, split_holdout

    conn = connect_ro(args)
    try:
        items = build_items(conn, n_pool=args.n + args.holdout, seed=str(args.seed))
    finally:
        conn.rollback()
        conn.close()

    graded, holdout = split_holdout(items, args.holdout, seed=int(args.seed))
    meta = {
        "lane": "finding",
        "version": "finding-v1",
        "built_at": datetime.now(UTC).isoformat(),
        "source": "prod: verified corroborates edges, anchored/published weighted 2x",
        "seed": str(args.seed),
    }
    out = dump_set(graded, meta, args.out)
    print(f"wrote {len(graded)} graded items -> {out}", file=sys.stderr)
    if holdout:
        hout = dump_set(
            holdout, {**meta, "holdout_of": str(args.out)}, args.holdout_out
        )
        print(f"wrote {len(holdout)} holdout items -> {hout}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
