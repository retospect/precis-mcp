"""classify — the production chunk-axis classifier (ADR 0047).

A self-contained pass (shaped like `llm_summarize`, NOT a WorkerHandler
subclass: it needs DB JOINs + an outbound LLM call, which WorkerHandler
forbids in `process`). It:

  1. claims paper body chunks that lack this axis's tag (leased in the
     shared `chunk_claims` table under artifact `classify:<axis>-v<ver>`),
  2. builds the same context packet the gold set used (section_path,
     position, title, ref_tags, neighbor gists),
  3. asks the local model for one JSON label,
  4. writes a chunk tag `Tag.closed("<AXIS>", value)` (namespace =
     uppercased axis id) via `store.add_tag(..., pos=ord)`.

Default is DRY-RUN: it classifies and prints the label distribution but
writes nothing. Pass `--commit` to write tags. `--limit` bounds the run
(a full-corpus run is ~1.3M chunks — do that deliberately, not by
accident). Read-only DB access unless `--commit`.

Idempotent: the claim excludes chunks already carrying the axis tag, and
the `chunk_claims` lease (artifact carries the version) prevents two
workers racing the same chunk. Bump `--version` to re-tag the corpus.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _common import open_store

HERE = Path(__file__).resolve().parent


def load_axis(axis_id: str, axes_dir: Path) -> dict:
    import yaml

    p = axes_dir / f"{axis_id}.yaml"
    if not p.exists():
        sys.exit(f"error: no axis def at {p}")
    return yaml.safe_load(p.open())


# ---- claim: body chunks lacking this axis's tag -----------------------


def claim_chunks(conn, *, namespace: str, artifact: str, limit: int) -> list[dict]:
    """FOR UPDATE SKIP LOCKED claim of unclassified body chunks + lease."""
    sql = """
    WITH cand AS (
      SELECT c.chunk_id, c.ref_id, c.ord, c.text, c.section_path
      FROM chunks c
      JOIN refs r ON r.ref_id = c.ref_id
      WHERE r.kind = 'paper' AND r.deleted_at IS NULL
        AND c.ord >= 0 AND c.chunk_kind = 'paragraph'
        AND length(c.text) > 120
        AND NOT EXISTS (
          SELECT 1 FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id
          WHERE ct.chunk_id = c.chunk_id AND t.namespace = %(ns)s)
        AND NOT EXISTS (
          SELECT 1 FROM chunk_claims cl
          WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(artifact)s)
      ORDER BY c.chunk_id
      LIMIT %(limit)s
      FOR UPDATE OF c SKIP LOCKED
    ), leased AS (
      INSERT INTO chunk_claims (chunk_id, artifact)
      SELECT chunk_id, %(artifact)s FROM cand
      ON CONFLICT DO NOTHING
    )
    SELECT chunk_id, ref_id, ord, text, section_path FROM cand
    """
    rows = conn.execute(
        sql, {"ns": namespace, "artifact": artifact, "limit": limit}
    ).fetchall()
    return [
        {
            "chunk_id": r[0],
            "ref_id": r[1],
            "ord": r[2],
            "text": r[3],
            "section_path": list(r[4] or []),
        }
        for r in rows
    ]


def enrich(conn, rows: list[dict]) -> None:
    """Attach title / position / ref_tags / neighbor gists (gold-parity)."""
    for row in rows:
        ref_id, ord_ = row["ref_id"], row["ord"]
        meta = conn.execute(
            """
            SELECT r.title,
                   (SELECT count(*) FROM chunks c2
                      WHERE c2.ref_id = r.ref_id AND c2.ord >= 0)
            FROM refs r WHERE r.ref_id = %s
            """,
            (ref_id,),
        ).fetchone()
        row["title"] = meta[0] if meta else ""
        row["n_chunks"] = meta[1] if meta else 0
        row["position"] = f"{ord_}/{row['n_chunks']}"
        row["section_path"] = " ▸ ".join(row["section_path"]) or "(none)"
        neigh = {}
        for nord in (ord_ - 1, ord_ + 1):
            if nord < 0:
                continue
            nr = conn.execute(
                """
                SELECT c.text,
                       (SELECT s.text FROM chunk_summaries s
                          WHERE s.chunk_id = c.chunk_id
                            AND s.summarizer = 'llm-v1' AND s.status = 'ok' LIMIT 1)
                FROM chunks c WHERE c.ref_id = %s AND c.ord = %s LIMIT 1
                """,
                (ref_id, nord),
            ).fetchone()
            if nr:
                neigh[nord] = (nr[1] or nr[0] or "").strip().replace("\n", " ")[:160]
        row["prev_gist"] = neigh.get(ord_ - 1, "")
        row["next_gist"] = neigh.get(ord_ + 1, "")
        rt = conn.execute(
            """
            SELECT t.namespace, t.value FROM ref_tags rt
            JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = %s
            ORDER BY t.namespace, t.value LIMIT 10
            """,
            (ref_id,),
        ).fetchall()
        row["ref_tags"] = [v if ns == "OPEN" else f"{ns}:{v}" for ns, v in rt]


_ROLE3_VALS = {"own", "background", "furniture"}


def classify_cascade(row, junk_axis, role3_axis, *, model, escalate_model):
    """Tier1 cascade: junk-gate -> role3 (-> optional escalate on 'own').

    Returns (value, path). The junk gate cheaply short-circuits furniture
    (skips the role3 call); only substantive chunks get the role3 call; and
    if --escalate-model is set, chunks the local model calls `own` (the
    citation-critical, error-prone class) are re-judged by the stronger
    model — the frugal Tier 2 that spends the expensive model only on the
    attribution-ambiguous residual.
    """
    import _eval
    from _llm import classify_one

    jr = classify_one(_eval.build_prompt(row, junk_axis), model)
    if (jr or {}).get("value") == "junk":
        return "furniture", "junk-gate"
    r3 = classify_one(_eval.build_prompt(row, role3_axis), model)
    val = (r3 or {}).get("value")
    if val not in _ROLE3_VALS:
        return None, "role3-error"
    if val == "own" and escalate_model:
        er = classify_one(_eval.build_prompt(row, role3_axis), escalate_model)
        ev = (er or {}).get("value")
        if ev in _ROLE3_VALS:
            return ev, f"escalate:{escalate_model}"
    return val, "role3"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--axis",
        default=None,
        help="single-axis mode (e.g. role); omit when using --cascade",
    )
    p.add_argument(
        "--cascade",
        action="store_true",
        help="junk-gate -> role3 pipeline; writes the ROLE3 namespace",
    )
    p.add_argument(
        "--escalate-model",
        default=None,
        help="stronger model to re-judge 'own' chunks (Tier 2)",
    )
    p.add_argument("--limit", type=int, default=50, help="max chunks (default 50)")
    p.add_argument("--version", default="1", help="version -> artifact suffix")
    p.add_argument("--model", default=None, help="LLM backend (default local)")
    p.add_argument("--commit", action="store_true", help="write tags (else dry-run)")
    p.add_argument(
        "--axes-dir",
        type=Path,
        default=HERE.parent.parent / "src" / "precis" / "data" / "axes",
    )
    args = p.parse_args()
    if not args.axis and not args.cascade:
        sys.exit("error: pass --axis <id> or --cascade")

    import _eval  # prompt/context builders (gold-parity)
    from _llm import classify_one

    from precis.store.types import Tag

    if args.cascade:
        namespace = "ROLE3"
        artifact = f"classify:cascade-v{args.version}"
        junk_axis = load_axis("junk", args.axes_dir)
        role3_axis = load_axis("role3", args.axes_dir)
        valid = _ROLE3_VALS
    else:
        axis = load_axis(args.axis, args.axes_dir)
        namespace = args.axis.upper()
        artifact = f"classify:{args.axis}-v{args.version}"
        valid = set(axis["values"])

    store, _cfg = open_store()
    try:
        with store.pool.connection() as conn:
            rows = claim_chunks(
                conn, namespace=namespace, artifact=artifact, limit=args.limit
            )
            if not args.commit:
                conn.rollback()  # release the lease we took in dry-run
            enrich(conn, rows)
            if args.commit:
                conn.commit()
        mode = "cascade->ROLE3" if args.cascade else f"'{args.axis}'"
        print(
            f"claimed {len(rows)} chunks for {mode} "
            f"(namespace {namespace}, artifact {artifact}, "
            f"{'COMMIT' if args.commit else 'DRY-RUN'})",
            file=sys.stderr,
        )

        dist = Counter()
        paths = Counter()
        written = skipped = errored = 0
        for row in rows:
            if args.cascade:
                val, path = classify_cascade(
                    row,
                    junk_axis,
                    role3_axis,
                    model=args.model,
                    escalate_model=args.escalate_model,
                )
                paths[path] += 1
            else:
                pred = classify_one(_eval.build_prompt(row, axis), args.model)
                val = (pred or {}).get("value")
            if not val or val not in valid:
                errored += 1
                dist["<error>"] += 1
                continue
            dist[val] += 1
            if val == "unknown":
                skipped += 1
                continue
            if args.commit:
                with store.pool.connection() as conn:
                    store.add_tag(
                        row["ref_id"],
                        Tag.closed(namespace, val),
                        pos=row["ord"],
                        set_by="agent",
                        replace_prefix=True,
                        conn=conn,
                    )
                    conn.commit()
                written += 1
        print(f"\nlabel distribution: {dict(dist.most_common())}", file=sys.stderr)
        if args.cascade:
            print(f"cascade paths: {dict(paths.most_common())}", file=sys.stderr)
        print(
            f"written={written} skipped(unknown)={skipped} errored={errored}",
            file=sys.stderr,
        )
        if not args.commit:
            print("(dry-run: no tags written; pass --commit to write)", file=sys.stderr)
    finally:
        store.close()


if __name__ == "__main__":
    main()
