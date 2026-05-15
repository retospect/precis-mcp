"""cluster-papers — k-means cluster paper refs by bge-m3 embedding.

Pulls every non-deleted `paper` ref from the precis store, computes
a per-paper representative vector (mean of the first N block
embeddings), runs sklearn KMeans, and writes a CSV.

Used to validate the auto-tagging taxonomy (similar papers should
cluster together; tight clusters the taxonomy can't distinguish
indicate missing axes) and to stratify the gold-set sample.

Reads `PRECIS_DATABASE_URL` from the environment.

Outputs:
  clusters.csv       — slug, cluster, journal, year, n_blocks, title
  top-journals.txt   — most common journal names (--top-journals N)
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

# Make scripts/_common.py importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import open_store

# How many leading blocks to average for the per-paper vector.
DEFAULT_HEAD_BLOCKS = 4


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--k", type=int, default=30, help="number of clusters (default 30)")
    p.add_argument(
        "--head-blocks",
        type=int,
        default=DEFAULT_HEAD_BLOCKS,
        help=f"average first N block embeddings per paper (default {DEFAULT_HEAD_BLOCKS})",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "clusters.csv",
        help="output CSV path",
    )
    p.add_argument(
        "--top-journals",
        type=int,
        nargs="?",
        const=200,
        default=None,
        metavar="N",
        help="also dump the N most-common journal names to top-journals.txt",
    )
    p.add_argument(
        "--min-papers",
        type=int,
        default=20,
        help="bail if the corpus has fewer than this many papers",
    )
    args = p.parse_args()

    try:
        import numpy as np
        from sklearn.cluster import KMeans
    except ImportError as e:
        print(
            f"error: missing dependency ({e}). "
            "Run from the shared workspace venv at pips/.venv — it carries "
            "scikit-learn via sentence-transformers (pulled in by the "
            "workspace dev group's precis-mcp[paper] extra). "
            "Sync with `uv sync --all-packages` from pips/.",
            file=sys.stderr,
        )
        sys.exit(1)

    store, _cfg = open_store()
    try:
        with store.pool.connection() as conn:
            # Average first N block vectors per paper.
            rows = conn.execute(
                """
                WITH head AS (
                    SELECT b.ref_id,
                           b.embedding,
                           ROW_NUMBER() OVER (PARTITION BY b.ref_id ORDER BY b.pos) AS rn
                    FROM blocks b
                    JOIN refs r ON r.id = b.ref_id
                    WHERE r.kind = 'paper' AND r.deleted_at IS NULL
                      AND b.embedding IS NOT NULL
                )
                SELECT r.slug,
                       r.title,
                       r.meta->>'journal'                        AS journal,
                       (r.meta->>'year')::int                    AS year,
                       (SELECT count(*) FROM blocks b2
                          WHERE b2.ref_id = r.id)                AS n_blocks,
                       AVG(h.embedding)::vector                  AS rep
                FROM refs r
                JOIN head h ON h.ref_id = r.id AND h.rn <= %s
                WHERE r.kind = 'paper' AND r.deleted_at IS NULL
                GROUP BY r.id, r.slug, r.title, journal, year
                HAVING AVG(h.embedding) IS NOT NULL
                ORDER BY r.slug
                """,
                (args.head_blocks,),
            ).fetchall()
    finally:
        store.close()

    if len(rows) < args.min_papers:
        print(
            f"error: only {len(rows)} papers with embeddings — "
            f"need at least {args.min_papers}.",
            file=sys.stderr,
        )
        sys.exit(2)

    # pgvector returns Python list-of-floats by default via psycopg.
    slugs = [r[0] for r in rows]
    titles = [r[1] or "" for r in rows]
    journals = [r[2] or "" for r in rows]
    years = [r[3] for r in rows]
    n_blocks = [r[4] for r in rows]
    vecs = np.array([list(r[5]) for r in rows], dtype=np.float32)

    # Normalize so KMeans on euclidean ≈ cosine clustering (bge-m3
    # vectors are not unit-length out of the box).
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = vecs / norms

    k = min(args.k, len(rows))
    print(f"clustering {len(rows)} papers into {k} clusters...", file=sys.stderr)
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    labels = km.fit_predict(vecs)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["slug", "cluster", "journal", "year", "n_blocks", "title"])
        for slug, cluster, jr, yr, nb, ti in zip(
            slugs, labels, journals, years, n_blocks, titles
        ):
            w.writerow([slug, int(cluster), jr, yr or "", nb, ti])

    cluster_counts = Counter(int(x) for x in labels)
    print(f"wrote {args.output} ({len(rows)} rows, {k} clusters)", file=sys.stderr)
    print("cluster sizes (top 10):", file=sys.stderr)
    for cid, n in cluster_counts.most_common(10):
        print(f"  {cid:>3}  {n}", file=sys.stderr)

    if args.top_journals is not None:
        jcounts = Counter(j for j in journals if j)
        out = args.output.with_name("top-journals.txt")
        with out.open("w") as f:
            f.write(f"# top {args.top_journals} journal names by paper count\n")
            f.write("# (use to seed src/precis/data/axes/journal_domains.yaml)\n\n")
            for jr, n in jcounts.most_common(args.top_journals):
                f.write(f"{n:>5}  {jr}\n")
        print(f"wrote {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
