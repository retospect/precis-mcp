"""Build the ``link-add`` lane's frozen eval set from prod (read-only).

Task shape (``local-mesh-upkeep.md`` §1 row 2): given an evidence chunk and
a candidate list of claim hubs, propose the ``(hub, relation)`` edges the
chunk supports. The candidate list is the chunk's top-k embedding
neighbours over the hub index (``finding_body`` ord-0, bge-m3 — the same
index ``canon.block`` queries), padded with the gold endpoints so the
positives are always among the candidates.

Gold:

- **positives** — the prod ``links`` rows carrying ``meta.verified_by``
  with ``src_chunk_id`` = this chunk (the verifier's recorded yes).
- **judged negatives** — hubs whose ``meta.taproot_rejected`` memo names
  this chunk's paper (the verifier's recorded no). Better than the spec's
  "neighbours with no edge, judged by the frontier": the judge already
  ran; the memo IS its verdict.
- **unjudged negatives** — embedding neighbours with no edge either way,
  labelled ``absence`` (weaker gold; the spec's frontier-judge pass can
  re-label them — they score as wrong proposals until then, which is the
  conservative direction for an upkeep model).

    uv run python scripts/llm_eval/build_mesh_link_gold.py --prod \
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

DEFAULT_OUT = "scripts/llm_eval/gold_set/local/mesh_link-add_v1.jsonl"
DEFAULT_HOLDOUT = "scripts/llm_eval/gold_set/local/mesh_link-add_holdout_v1.jsonl"

#: Chunks carrying a verified evidence edge to a live claim hub.
_SEED_SQL = """
    SELECT chunk_id FROM (
        SELECT DISTINCT c.chunk_id
          FROM links l
          JOIN chunks c ON c.chunk_id = l.src_chunk_id AND c.retired_at IS NULL
          JOIN refs r ON r.ref_id = l.dst_ref_id AND r.retired_at IS NULL
         WHERE l.meta ? 'verified_by'
    ) s
     ORDER BY md5(chunk_id::text || %(seed)s)
     LIMIT %(limit)s
"""

_CHUNK_SQL = """
    SELECT c.chunk_id, c.text, c.ref_id, p.title AS paper_title
      FROM chunks c JOIN refs p ON p.ref_id = c.ref_id
     WHERE c.chunk_id = %s
"""

#: Verified edges off this chunk -> (hub, relation).
_POS_SQL = """
    SELECT l.dst_ref_id, l.relation
      FROM links l JOIN refs r ON r.ref_id = l.dst_ref_id AND r.retired_at IS NULL
     WHERE l.src_chunk_id = %s AND l.meta ? 'verified_by'
"""

#: Hubs whose rejection memo names this chunk's paper (verifier said no).
_NEG_SQL = """
    SELECT ref_id, title FROM refs
     WHERE kind = 'finding' AND retired_at IS NULL
       AND meta->'taproot_rejected' ? %s
"""

#: Hubs linked from this paper AT ALL (any chunk, verified or not) — a
#: neighbour carrying one is not an "absence" negative even when the link
#: came off a different chunk.
_ANY_LINK_SQL = """
    SELECT DISTINCT l.dst_ref_id FROM links l
      JOIN chunks c ON c.chunk_id = l.src_chunk_id
     WHERE c.ref_id = %s
    UNION
    SELECT DISTINCT l.src_ref_id FROM links l
      JOIN chunks c ON c.chunk_id = l.src_chunk_id
     WHERE c.ref_id = %s
    UNION
    SELECT l.dst_ref_id FROM links l WHERE l.src_ref_id = %s
    UNION
    SELECT l.src_ref_id FROM links l WHERE l.dst_ref_id = %s
"""

#: Top-k hub neighbours of the chunk's own embedding, over the same
#: finding_body index canon.block queries.
_NN_SQL = """
    SELECT r.ref_id, r.title,
           ce.vector <=> (SELECT vector FROM chunk_embeddings
                          WHERE chunk_id = %(chunk)s AND embedder = 'bge-m3'
                            AND status = 'ok') AS dist
      FROM refs r
      JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = 0
                   AND c.chunk_kind = 'finding_body' AND c.retired_at IS NULL
      JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                   AND ce.embedder = 'bge-m3' AND ce.status = 'ok'
     WHERE r.kind = 'finding' AND r.retired_at IS NULL
     ORDER BY dist ASC
     LIMIT %(k)s
"""

_TITLE_SQL = "SELECT ref_id, title FROM refs WHERE ref_id = ANY(%s)"


def _prompt(chunk_text: str, paper_title: str, candidates: list[dict[str, Any]]) -> str:
    lines = [
        "You are doing upkeep on a knowledge graph. Below is an evidence "
        f"passage from the paper {paper_title!r}, then candidate claim-hub "
        "nodes.",
        "",
        f"Passage: {chunk_text}",
        "",
        "Candidate hubs:",
    ]
    for c in candidates:
        lines.append(f"  [id={c['ref_id']}] {c['title']}")
    lines += [
        "",
        "Which candidates does the passage's own evidence support? Reply "
        "with a JSON array and nothing else, e.g. "
        '[{"dst": 123, "relation": "corroborates"}].',
        "Relations:",
        "  corroborates — the passage's evidence supports the claim",
        "  establishes  — the passage is the claim's primary/originating evidence",
        "  disputes     — the passage's evidence contradicts the claim",
        "Omit candidates the passage does not support. Topical adjacency "
        "is not support — the passage must bear on the claim itself.",
    ]
    return "\n".join(lines)


def build_items(conn: Any, *, n_pool: int, k: int, seed: str) -> list[Any]:
    from precis.mesh_eval.sets import MeshItem

    chunk_ids = [
        int(r[0])
        for r in conn.execute(_SEED_SQL, {"seed": seed, "limit": n_pool}).fetchall()
    ]
    items: list[MeshItem] = []
    for chunk_id in chunk_ids:
        row = conn.execute(_CHUNK_SQL, (chunk_id,)).fetchone()
        if not row:
            continue
        _cid, text, paper_ref, paper_title = (
            int(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3] or ""),
        )

        positives: list[dict[str, Any]] = [
            {"dst_ref_id": int(r[0]), "relation": str(r[1])}
            for r in conn.execute(_POS_SQL, (chunk_id,)).fetchall()
        ]
        rejected = conn.execute(_NEG_SQL, (str(paper_ref),)).fetchall()
        linked = {
            int(r[0])
            for r in conn.execute(
                _ANY_LINK_SQL, (paper_ref, paper_ref, paper_ref, paper_ref)
            ).fetchall()
        }
        neighbours = conn.execute(_NN_SQL, {"chunk": chunk_id, "k": k}).fetchall()

        pos_ids: set[int] = {int(e["dst_ref_id"]) for e in positives}
        neg_judged = {
            int(r[0]): str(r[1]) for r in rejected if int(r[0]) not in pos_ids
        }
        nn = {int(r[0]): (str(r[1]), float(r[2])) for r in neighbours}
        # Candidates: positives + judged negatives + embedding neighbours,
        # deduped, ordered by distance where known.
        cand_ids: list[int] = sorted(
            {int(x) for x in pos_ids | set(neg_judged) | set(nn)},
            key=lambda i: nn[i][1] if i in nn else 9.9,
        )
        titles = {
            int(r[0]): str(r[1])
            for r in conn.execute(_TITLE_SQL, (list(cand_ids),)).fetchall()
        }
        candidates = [
            {
                "ref_id": int(i),
                "title": titles.get(int(i)) or neg_judged.get(int(i)) or "",
            }
            for i in cand_ids
        ]
        no_edge_judged = [i for i in cand_ids if i in neg_judged]
        no_edge_absence = [
            i
            for i in cand_ids
            if i in nn and i not in pos_ids and i not in neg_judged and i not in linked
        ]
        if not positives:
            continue
        items.append(
            MeshItem(
                item_id=f"link-{chunk_id}",
                lane="link-add",
                prompt=_prompt(text, paper_title, candidates),
                gold={
                    "edges": positives,
                    "no_edge": no_edge_judged + no_edge_absence,
                    "no_edge_judged": no_edge_judged,
                },
                meta={
                    "chunk_id": chunk_id,
                    "paper_ref_id": paper_ref,
                    "paper_title": paper_title,
                    "candidates": cand_ids,
                    "gold_source": {
                        "positives": "verified_by",
                        "negatives": "taproot_rejected+absence",
                    },
                },
            )
        )
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--holdout", type=int, default=20)
    ap.add_argument("--k", type=int, default=10, help="Embedding neighbours per chunk.")
    ap.add_argument("--seed", default="1")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--holdout-out", default=DEFAULT_HOLDOUT)
    args = ap.parse_args(argv)

    from precis.mesh_eval.sets import dump_set, split_holdout

    conn = connect_ro(args)
    try:
        items = build_items(
            conn, n_pool=args.n + args.holdout, k=args.k, seed=str(args.seed)
        )
    finally:
        conn.rollback()
        conn.close()

    graded, holdout = split_holdout(items, args.holdout, seed=int(args.seed))
    meta = {
        "lane": "link-add",
        "version": "link-add-v1",
        "built_at": datetime.now(UTC).isoformat(),
        "source": "prod: verified links + taproot_rejected memos + bge-m3 neighbours",
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
