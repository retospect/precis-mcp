"""sample-gold-chunks — stratified sample of paper *chunks* for the
chunk-level axes (`role:`, `open-question:`; ADR 0047).

Unlike ``sample-gold`` (whole papers × ref axes), this samples
individual body chunks. The rhetorical ``role:`` axis is
section-driven, so we stratify by a *weak-label* bucket derived from
section_path + text regex — this guarantees the rare roles
(limitation, future-work) and both ``open-question`` values appear in
the gold set, which a flat random draw would miss.

The weak bucket is only a sampling aid and a labeling hint; it is NOT
the gold label. Every emitted row still has ``role: ?`` and
``open-question: ?`` for a human/LLM to fill from the axis vocabulary.

Read-only. Points at whatever ``PRECIS_DATABASE_URL`` names — run it
on a cluster node against ``precis_prod`` for a real-corpus sample.
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

# Make scripts/_common.py importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import open_store

# Weak-label buckets. A CASE over section_path + text regex. Order
# matters: the first match wins, so put the specific rare-role signals
# (future-work, limitation) BEFORE the coarse section buckets — a
# "future work" sentence inside a Discussion section should land in the
# rare bucket, not "interp". Keep in sync with the roster in role.yaml.
_BUCKET_CASE = r"""
CASE
  WHEN body.text ~* '(future (work|studies|research|directions)|remains (unclear|to be|an open)|beyond the scope|has not (yet )?been|no study has|open question|warrants further)'
       THEN 'future-open'
  WHEN body.text ~* '(limitation|shortcoming|caveat|does not (capture|account|include|consider)|we did not|were unable to|failed to)'
       THEN 'limitation'
  WHEN body.secp ~ '(referen|acknowledg|copyright|licen|©|all rights reserved|creative commons|supplementary|supporting information)'
       THEN 'boilerplate'
  WHEN body.secp ~ '(method|experimental|computational|procedure|synthesis|fabricat|protocol)'
       THEN 'method'
  WHEN body.secp ~ 'result'
       THEN 'result'
  WHEN body.secp ~ 'discuss'
       THEN 'interp'
  WHEN body.secp ~ '(introduc|background|related work|motivation)'
       THEN 'motiv-related'
  WHEN body.numeric_ratio > 0.35
       THEN 'data'
  ELSE 'other'
END
"""

# Rough target mix (fractions of --n). Rare roles are over-weighted vs
# their corpus frequency so the gold set can actually measure them;
# boilerplate is under-weighted (it's easy and huge). Normalised at
# runtime, so exact values only set relative emphasis.
_BUCKET_WEIGHTS = {
    "future-open": 3.0,
    "limitation": 3.0,
    "method": 2.0,
    "result": 2.0,
    "interp": 2.0,
    "motiv-related": 2.0,
    "data": 1.5,
    "boilerplate": 1.0,
    "other": 2.0,
}


def _fetch_pool(store, per_bucket: int) -> list[dict]:
    """Draw up to ``per_bucket`` random chunks per weak bucket."""
    sql = f"""
    WITH body AS (
      SELECT c.ref_id, c.ord, c.text, c.section_path,
             lower(array_to_string(c.section_path, ' ')) AS secp,
             CASE WHEN length(c.text) = 0 THEN 0
                  ELSE (length(c.text) - length(regexp_replace(c.text, '[0-9]', '', 'g')))::float
                       / length(c.text) END AS numeric_ratio
      FROM chunks c
      JOIN refs r ON r.ref_id = c.ref_id
      WHERE r.kind = 'paper' AND r.deleted_at IS NULL
            AND c.ord >= 0 AND c.chunk_kind = 'paragraph'
            AND length(c.text) > 120
    ),
    tagged AS (SELECT body.*, {_BUCKET_CASE} AS weak FROM body),
    ranked AS (
      SELECT ref_id, ord, text, section_path, weak,
             row_number() OVER (PARTITION BY weak ORDER BY random()) AS rn
      FROM tagged
    )
    SELECT ref_id, ord, text, section_path, weak
    FROM ranked WHERE rn <= %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (per_bucket,)).fetchall()
    return [
        {
            "ref_id": r[0],
            "ord": r[1],
            "text": r[2],
            "section_path": list(r[3] or []),
            "weak": r[4],
        }
        for r in rows
    ]


def _stratified_pick(
    pool: list[dict], n: int, per_paper: int, rng: random.Random
) -> list[dict]:
    """Draw ``n`` chunks weighted by bucket, capped per paper."""
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for row in pool:
        by_bucket[row["weak"]].append(row)
    for rows in by_bucket.values():
        rng.shuffle(rows)

    total_w = sum(_BUCKET_WEIGHTS.get(b, 1.0) for b in by_bucket)
    quota = {
        b: max(1, round(n * _BUCKET_WEIGHTS.get(b, 1.0) / total_w)) for b in by_bucket
    }

    picked: list[dict] = []
    per_paper_count: dict[int, int] = defaultdict(int)

    def _try_take(row: dict) -> bool:
        if per_paper_count[row["ref_id"]] >= per_paper:
            return False
        picked.append(row)
        per_paper_count[row["ref_id"]] += 1
        return True

    # First pass: honour each bucket's quota.
    for b, rows in by_bucket.items():
        taken = 0
        for row in rows:
            if taken >= quota[b] or len(picked) >= n:
                break
            if _try_take(row):
                taken += 1
    # Second pass: top up to n from any remaining, still capped per paper.
    if len(picked) < n:
        leftovers = [r for rows in by_bucket.values() for r in rows if r not in picked]
        rng.shuffle(leftovers)
        for row in leftovers:
            if len(picked) >= n:
                break
            _try_take(row)
    return picked[:n]


def _enrich(store, picked: list[dict]) -> None:
    """Attach slug, title, position, neighbor gists, ref tags in place."""
    with store.pool.connection() as conn:
        for row in picked:
            ref_id, ord_ = row["ref_id"], row["ord"]
            meta = conn.execute(
                """
                SELECT r.title,
                       (SELECT id_value FROM ref_identifiers
                          WHERE ref_id = r.ref_id AND id_kind = 'cite_key' LIMIT 1),
                       (SELECT count(*) FROM chunks c2
                          WHERE c2.ref_id = r.ref_id AND c2.ord >= 0)
                FROM refs r WHERE r.ref_id = %s
                """,
                (ref_id,),
            ).fetchone()
            row["title"] = meta[0] if meta else ""
            row["slug"] = meta[1] if meta and meta[1] else f"ref{ref_id}"
            row["n_chunks"] = meta[2] if meta else 0

            # Neighbor gists: prefer the llm-v1 summary, else truncate text.
            neigh: dict[int, str] = {}
            for nord in (ord_ - 1, ord_ + 1):
                if nord < 0:
                    continue
                nr = conn.execute(
                    """
                    SELECT c.text,
                           (SELECT s.text FROM chunk_summaries s
                              WHERE s.chunk_id = c.chunk_id
                                AND s.summarizer = 'llm-v1' AND s.status = 'ok'
                              LIMIT 1)
                    FROM chunks c WHERE c.ref_id = %s AND c.ord = %s
                    LIMIT 1
                    """,
                    (ref_id, nord),
                ).fetchone()
                if nr:
                    gist = (nr[1] or nr[0] or "").strip().replace("\n", " ")
                    neigh[nord] = gist[:160]
            row["prev_gist"] = neigh.get(ord_ - 1, "")
            row["next_gist"] = neigh.get(ord_ + 1, "")

            tag_rows = conn.execute(
                """
                SELECT t.namespace, t.value FROM ref_tags rt
                JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = %s
                ORDER BY t.namespace, t.value LIMIT 10
                """,
                (ref_id,),
            ).fetchall()
            row["ref_tags"] = [
                (v if ns == "OPEN" else f"{ns}:{v}") for ns, v in tag_rows
            ]


def _yaml_str(s: str, limit: int) -> str:
    return s.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()[:limit]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--n", type=int, default=200, help="sample size (default 200)")
    p.add_argument(
        "--per-bucket",
        type=int,
        default=400,
        help="candidate pool size per weak bucket before the weighted draw",
    )
    p.add_argument(
        "--per-paper", type=int, default=2, help="max chunks from one paper (default 2)"
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "gold_set" / "gold_set_chunks.yaml",
        help="output yaml path",
    )
    p.add_argument("--seed", type=int, default=42, help="rng seed")
    args = p.parse_args()

    store, _cfg = open_store()
    try:
        print(f"drawing candidate pool (≤{args.per_bucket}/bucket)…", file=sys.stderr)
        pool = _fetch_pool(store, args.per_bucket)
        rng = random.Random(args.seed)
        picked = _stratified_pick(pool, args.n, args.per_paper, rng)
        print(f"picked {len(picked)}; enriching…", file=sys.stderr)
        _enrich(store, picked)
    finally:
        store.close()

    # Stable, readable order: by bucket then slug.
    picked.sort(key=lambda r: (r["weak"], r["slug"], r["ord"]))

    bucket_counts: dict[str, int] = defaultdict(int)
    for r in picked:
        bucket_counts[r["weak"]] += 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        f.write("# gold_set_chunks — hand/LLM-labeled paper CHUNKS for the\n")
        f.write("# chunk-level axes role: and open-question: (ADR 0047).\n")
        f.write(f"# {len(picked)} chunks. Weak-bucket mix: ")
        f.write(", ".join(f"{b}={n}" for b, n in sorted(bucket_counts.items())))
        f.write("\n# Replace each `?` with a value from the axis vocabulary:\n")
        f.write("#   role:          src/precis/data/axes/role.yaml\n")
        f.write("#   open-question: src/precis/data/axes/open-question.yaml\n")
        f.write("# `weak` is a sampling hint, NOT the label. Address a chunk by\n")
        f.write("# ref_id+ord (stable); slug is for opening the paper.\n\n")
        f.write("chunks:\n")
        for r in picked:
            secp = " ▸ ".join(r["section_path"]) if r["section_path"] else "(none)"
            f.write(f"  - ref_id: {r['ref_id']}\n")
            f.write(f"    ord: {r['ord']}\n")
            f.write(f"    slug: {r['slug']}\n")
            f.write(f'    title: "{_yaml_str(r["title"], 140)}"\n')
            f.write(f"    position: {r['ord']}/{r['n_chunks']}\n")
            f.write(f'    section_path: "{_yaml_str(secp, 160)}"\n')
            f.write(f"    weak: {r['weak']}\n")
            if r["ref_tags"]:
                f.write(f"    ref_tags: {r['ref_tags']}\n")
            if r["prev_gist"]:
                f.write(f'    prev_gist: "{_yaml_str(r["prev_gist"], 160)}"\n')
            f.write(f'    text: "{_yaml_str(r["text"], 1400)}"\n')
            if r["next_gist"]:
                f.write(f'    next_gist: "{_yaml_str(r["next_gist"], 160)}"\n')
            f.write("    labels:\n")
            f.write("      role: ?\n")
            f.write("      open-question: ?\n\n")

    print(f"wrote {args.output} ({len(picked)} chunks)", file=sys.stderr)
    print(
        "bucket mix: "
        + ", ".join(f"{b}={n}" for b, n in sorted(bucket_counts.items())),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
