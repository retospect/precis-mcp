"""Build the ``merge`` lane's frozen eval set from prod (read-only).

Task (``local-mesh-upkeep.md`` §1 row 4): given a hub pair, same or
different. **Positives** are the recorded merges — a retired ref whose
``refines`` edge points at the live winner (the merge collapse marker
``merge_hubs`` writes, ``taproot.hub.MERGE_COLLAPSE_RELATION``), plus any
``superseded-by`` retirements. **Negatives** are unmerged near
neighbours: live same-kind pairs whose ``finding_body`` ord-0 embeddings
sit in the "plausibly confusable" band — at/above the observed merged-twin
distance (0.102 for fi176861/fi178714, ``hub-duplicate-reconcile.md``) —
with no merge edge between them. Pairs *below* the floor are likely real
unmerged twins and would be bad negatives; they are excluded, not
claimed.

Merge never auto-applies (the spec is explicit); the set exists to
measure whether a local model's merge *proposals* are good enough for a
bigger model to review cheaply.

    uv run python scripts/llm_eval/build_mesh_merge_gold.py --prod \
        --holdout 20
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _prod_ro import add_db_args, connect_ro

DEFAULT_OUT = "scripts/llm_eval/gold_set/local/mesh_merge_v1.jsonl"
DEFAULT_HOLDOUT = "scripts/llm_eval/gold_set/local/mesh_merge_holdout_v1.jsonl"

#: Embedding distance floor for negatives: the observed merged-twin
#: distance was 0.102 (bge-m3 cosine, finding_body); a margin above it
#: keeps true unmerged twins out of the negative pool.
_NEG_DIST_FLOOR = 0.14
_NEG_DIST_CEIL = 0.45

_POS_SQL = """
    SELECT s.ref_id, s.title, s.kind, d.ref_id, d.title
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id
      JOIN refs d ON d.ref_id = l.dst_ref_id
     WHERE l.relation IN ('refines', 'superseded-by')
       AND s.retired_at IS NOT NULL AND d.retired_at IS NULL
     ORDER BY md5(s.ref_id::text || %(seed)s)
"""

#: Live finding hubs with a finding_body embedding — the negative pool's
#: sampling base.
_HUB_SQL = """
    SELECT r.ref_id, r.title
      FROM refs r
      JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = 0
                   AND c.chunk_kind = 'finding_body' AND c.retired_at IS NULL
      JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                   AND ce.embedder = 'bge-m3' AND ce.status = 'ok'
     WHERE r.kind = 'finding' AND r.retired_at IS NULL
       AND EXISTS (
           SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
           WHERE rt.ref_id = r.ref_id AND t.namespace = 'TAPROOT'
             AND t.value = 'claim')
     ORDER BY md5(r.ref_id::text || %(seed)s)
     LIMIT %(limit)s
"""

#: A hub's nearest same-kind live neighbours in the band — negatives.
_BAND_SQL = """
    SELECT r.ref_id, r.title,
           ce.vector <=> (SELECT ce2.vector
                            FROM chunks c2
                            JOIN chunk_embeddings ce2
                              ON ce2.chunk_id = c2.chunk_id
                             AND ce2.embedder = 'bge-m3' AND ce2.status = 'ok'
                           WHERE c2.ref_id = %(ref)s AND c2.ord = 0
                             AND c2.chunk_kind = 'finding_body'
                             AND c2.retired_at IS NULL
                           LIMIT 1) AS dist
      FROM refs r
      JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = 0
                   AND c.chunk_kind = 'finding_body' AND c.retired_at IS NULL
      JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                   AND ce.embedder = 'bge-m3' AND ce.status = 'ok'
     WHERE r.kind = 'finding' AND r.retired_at IS NULL AND r.ref_id <> %(ref)s
       AND EXISTS (
           SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
           WHERE rt.ref_id = r.ref_id AND t.namespace = 'TAPROOT'
             AND t.value = 'claim')
     ORDER BY dist ASC
     LIMIT 10
"""

_LINKED_SQL = """
    SELECT 1 FROM links
     WHERE (src_ref_id = %s AND dst_ref_id = %s)
        OR (src_ref_id = %s AND dst_ref_id = %s)
     LIMIT 1
"""


def _prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    return (
        "Two nodes from the knowledge graph:\n\n"
        f"A. [{a['kind']}] {a['title']}\n"
        f"B. [{b['kind']}] {b['title']}\n\n"
        "Do A and B state the same underlying claim — i.e. one of them is "
        "a duplicate that should merge into the other? Judge the claim, "
        "not the wording: differing phrasing of the same statement is a "
        "merge; overlapping but distinct claims are not.\n"
        "Reply with a JSON object and nothing else: "
        '{"same": true, "survivor": "a"|"b", "reason": "<short>"} or '
        '{"same": false, "reason": "<short>"}.'
    )


def _item(lane_id: str, a: dict, b: dict, gold: dict, meta_extra: dict) -> Any:
    from precis.mesh_eval.sets import MeshItem

    return MeshItem(
        item_id=lane_id,
        lane="merge",
        prompt=_prompt(a, b),
        gold=gold,
        meta={"a": a, "b": b, **meta_extra},
    )


def build_items(conn: Any, *, n_neg: int, seed: str) -> list[Any]:
    import random

    items: list[Any] = []
    rng = random.Random(seed)

    positives = conn.execute(_POS_SQL, {"seed": seed}).fetchall()
    for i, (loser, ltitle, kind, winner, wtitle) in enumerate(positives):
        a, b = (
            {"ref_id": int(loser), "kind": str(kind), "title": str(ltitle)},
            {"ref_id": int(winner), "kind": str(kind), "title": str(wtitle)},
        )
        if rng.random() < 0.5:
            a, b = b, a
        survivor = "a" if a["ref_id"] == int(winner) else "b"
        items.append(
            _item(
                f"merge-pos-{i}",
                a,
                b,
                {"same": True, "survivor": survivor},
                {"gold_source": "recorded merge (retired refines/superseded-by)"},
            )
        )

    hubs = conn.execute(_HUB_SQL, {"seed": seed, "limit": n_neg * 4}).fetchall()
    seen_pairs: set[frozenset[int]] = set()
    for hub_id, hub_title in hubs:
        if len(items) - len(positives) >= n_neg:
            break
        for nid, ntitle, dist in conn.execute(
            _BAND_SQL, {"ref": int(hub_id)}
        ).fetchall():
            dist = float(dist)
            if not (_NEG_DIST_FLOOR <= dist <= _NEG_DIST_CEIL):
                continue
            pair = frozenset({int(hub_id), int(nid)})
            if pair in seen_pairs:
                continue
            if conn.execute(
                _LINKED_SQL, (int(hub_id), int(nid), int(nid), int(hub_id))
            ).fetchone():
                continue
            if str(hub_title).strip().casefold() == str(ntitle).strip().casefold():
                continue  # identical titles are suspect as negatives
            seen_pairs.add(pair)
            a = {"ref_id": int(hub_id), "kind": "finding", "title": str(hub_title)}
            b = {"ref_id": int(nid), "kind": "finding", "title": str(ntitle)}
            items.append(
                _item(
                    f"merge-neg-{len(seen_pairs)}",
                    a,
                    b,
                    {"same": False, "survivor": None},
                    {"gold_source": "unmerged near neighbour", "distance": dist},
                )
            )
            break  # one negative per hub keeps pairs independent
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    ap.add_argument("--n-neg", type=int, default=48, help="Negative pairs to build.")
    ap.add_argument("--holdout", type=int, default=20)
    ap.add_argument("--seed", default="1")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--holdout-out", default=DEFAULT_HOLDOUT)
    args = ap.parse_args(argv)

    from precis.mesh_eval.sets import dump_set, split_holdout

    conn = connect_ro(args)
    try:
        items = build_items(conn, n_neg=args.n_neg, seed=str(args.seed))
    finally:
        conn.rollback()
        conn.close()

    graded, holdout = split_holdout(items, args.holdout, seed=int(args.seed))
    meta = {
        "lane": "merge",
        "version": "merge-v1",
        "built_at": datetime.now(UTC).isoformat(),
        "source": "prod: recorded merges + unmerged embedding near-neighbours",
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
