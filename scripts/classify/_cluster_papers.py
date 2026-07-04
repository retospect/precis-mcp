"""cluster-papers — k-means cluster paper refs by bge-m3 embedding.

Pulls every non-deleted `paper` ref from the precis store, computes
a per-paper representative vector (mean of the first N body-chunk
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
            # Resolve the embedder from the DATA, not config: the worker
            # runs with `--embedder remote`, so cfg.embedder is "remote",
            # but chunk_embeddings.embedder stores the real model name
            # (bge-m3). Pick the most common one actually present.
            row = conn.execute(
                """
                SELECT embedder FROM chunk_embeddings
                WHERE status = 'ok'
                GROUP BY embedder ORDER BY count(*) DESC LIMIT 1
                """
            ).fetchone()
            embedder = row[0] if row else "bge-m3"
            print(f"using embedder '{embedder}'", file=sys.stderr)
            # Average the first N body-chunk vectors per paper. Slug is
            # the cite_key in ref_identifiers (no refs.slug column);
            # embeddings live in chunk_embeddings keyed by chunk_id.
            rows = conn.execute(
                """
                WITH head AS (
                    SELECT c.ref_id,
                           e.vector,
                           ROW_NUMBER() OVER (PARTITION BY c.ref_id
                                              ORDER BY c.ord) AS rn
                    FROM chunks c
                    JOIN chunk_embeddings e
                      ON e.chunk_id = c.chunk_id
                     AND e.embedder = %(embedder)s AND e.status = 'ok'
                    JOIN refs r ON r.ref_id = c.ref_id
                    WHERE r.kind = 'paper' AND r.deleted_at IS NULL
                      AND c.ord >= 0
                )
                SELECT COALESCE((SELECT id_value FROM ref_identifiers
                          WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                          LIMIT 1), 'ref' || r.ref_id)          AS slug,
                       r.title,
                       r.meta->>'journal'                        AS journal,
                       r.year                                    AS year,
                       (SELECT count(*) FROM chunks c2
                          WHERE c2.ref_id = r.ref_id AND c2.ord >= 0) AS n_blocks,
                       AVG(h.vector)::vector                     AS rep
                FROM refs r
                JOIN head h ON h.ref_id = r.ref_id AND h.rn <= %(head)s
                WHERE r.kind = 'paper' AND r.deleted_at IS NULL
                GROUP BY r.ref_id
                HAVING AVG(h.vector) IS NOT NULL
                ORDER BY slug
                """,
                {"embedder": embedder, "head": args.head_blocks},
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
