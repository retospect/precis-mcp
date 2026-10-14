"""Build the ``summarise`` lane's frozen eval set — same sampling as
``build_summarize_gold.py``, emitted as mesh-eval JSONL.

The task is the production summariser path verbatim (its prompt, its
``max_tokens``, its own parse/reject gates as the scorer). Gold is what
``score_summary`` needs: ``chunk_text``, ``nonprose`` (the incumbent's
parenthetical-tag call — a bias the existing builder documents), and the
incumbent text for review.

    uv run python scripts/llm_eval/build_mesh_summarise_gold.py --prod \
        --n 60 --holdout 0
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_summarize_gold
from _prod_ro import add_db_args, connect_ro

DEFAULT_OUT = "scripts/llm_eval/gold_set/local/mesh_summarise_v1.jsonl"
DEFAULT_HOLDOUT = "scripts/llm_eval/gold_set/local/mesh_summarise_holdout_v1.jsonl"


def build_items(conn: Any, *, n_pool: int, seed: str) -> list[Any]:
    from precis.mesh_eval.sets import MeshItem
    from precis.workers.llm_summarize import fetch_doc_card

    rows = build_summarize_gold.sample_rows(conn, n=n_pool, seed=seed)
    cards: dict[int, str] = {}
    items: list[MeshItem] = []
    for row in rows:
        rid = int(row["ref_id"])
        if rid not in cards:
            cards[rid] = fetch_doc_card(conn, rid)
        t = build_summarize_gold.row_to_task(row, cards[rid])
        items.append(
            MeshItem(
                item_id=t["task_id"],
                lane="summarise",
                prompt="",
                messages=t["messages"],
                gold={
                    "chunk_text": t["expect"]["chunk_text"],
                    "nonprose": t["expect"]["nonprose"],
                    "incumbent": t["expect"]["incumbent"],
                },
                meta={"gold_source": "incumbent summary judgement"},
                max_tokens=t.get("max_tokens"),
            )
        )
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_db_args(ap)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--holdout", type=int, default=0)
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
        "lane": "summarise",
        "version": "summarise-v1",
        "built_at": datetime.now(UTC).isoformat(),
        "source": "prod: incumbent-summarised chunks (build_summarize_gold sampling)",
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
