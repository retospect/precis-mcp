"""sample-gold — pick a stratified sample of papers for hand-labeling.

Reads clusters.csv (produced by cluster-papers) and emits a
gold_set.yaml form with N papers stratified across clusters. Each
paper has one row per axis, pre-filled with `?`. The user replaces
each `?` with the correct value from the axis vocabulary.

The form is intentionally yaml not csv — multiple `value/rationale`
fields per paper, comments are useful, and the eval harness reads
yaml.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

# Make scripts/_common.py importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import open_store

# Axis ids in the order they appear in gold_set.yaml. Keep in sync
# with src/precis/data/axes/*.yaml — the eval harness only reads
# axes that are listed here AND have a YAML file.
GOLD_AXES = [
    "domain",
    "studytype",
    "scale",
    "dim",
    "material",
    "property",
    "transport",
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--n", type=int, default=30, help="sample size (default 30)")
    p.add_argument(
        "--clusters",
        type=Path,
        default=Path(__file__).parent / "clusters.csv",
        help="clusters.csv produced by cluster-papers",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "gold_set" / "gold_set.yaml",
        help="output yaml path",
    )
    p.add_argument("--seed", type=int, default=42, help="rng seed")
    args = p.parse_args()

    if not args.clusters.exists():
        print(
            f"error: {args.clusters} not found. Run cluster-papers first.",
            file=sys.stderr,
        )
        sys.exit(1)

    by_cluster: dict[int, list[dict]] = defaultdict(list)
    with args.clusters.open() as f:
        for row in csv.DictReader(f):
            by_cluster[int(row["cluster"])].append(row)

    rng = random.Random(args.seed)
    clusters = sorted(by_cluster.keys())
    if not clusters:
        print("error: no clusters in input", file=sys.stderr)
        sys.exit(2)

    # Round-robin draw across clusters until we hit N. Within a
    # cluster, randomise + prefer journal diversity.
    picked: list[dict] = []
    seen_journals_per_cluster: dict[int, set[str]] = defaultdict(set)
    pools = {c: rng.sample(by_cluster[c], len(by_cluster[c])) for c in clusters}
    while len(picked) < args.n and any(pools.values()):
        for c in clusters:
            if len(picked) >= args.n:
                break
            pool = pools[c]
            if not pool:
                continue
            # Prefer a paper whose journal we haven't used yet in this cluster.
            chosen = next(
                (
                    pool.pop(i)
                    for i, r in enumerate(pool)
                    if r["journal"] and r["journal"] not in seen_journals_per_cluster[c]
                ),
                pool.pop(0),
            )
            seen_journals_per_cluster[c].add(chosen["journal"])
            picked.append(chosen)

    # Pull abstracts for the picked slugs.
    store, _cfg = open_store()
    abstracts: dict[str, str] = {}
    try:
        with store.pool.connection() as conn:
            for row in picked:
                slug = row["slug"]
                # First block whose density says abstract, else block 0.
                ab = conn.execute(
                    """
                    SELECT b.text FROM blocks b
                    JOIN refs r ON r.id = b.ref_id
                    WHERE r.slug = %s AND r.kind = 'paper'
                    ORDER BY (b.density = 'abstract') DESC NULLS LAST, b.pos ASC
                    LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                abstracts[slug] = (ab[0] if ab else "")[:1500]
    finally:
        store.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        f.write("# gold_set — hand-labeled papers for classifier eval.\n")
        f.write(f"# {len(picked)} papers stratified across {len(clusters)} clusters.\n")
        f.write("# Replace each `?` with the correct value from the axis vocabulary.\n")
        f.write("# Use `n-a` if the axis does not apply.\n")
        f.write("# Axis vocabularies live in src/precis/data/axes/*.yaml.\n\n")
        f.write("papers:\n")
        for row in picked:
            slug = row["slug"]
            title = row["title"].replace('"', "'")
            f.write(f"  - slug: {slug}\n")
            f.write(f'    title: "{title}"\n')
            f.write(f"    journal: {row['journal']!r}\n")
            f.write(f"    year: {row['year'] or 'null'}\n")
            f.write(f"    cluster: {row['cluster']}\n")
            ab = abstracts.get(slug, "").replace('"', "'").replace("\n", " ")
            f.write(f'    abstract: "{ab[:600]}"\n')
            f.write("    labels:\n")
            for axis in GOLD_AXES:
                f.write(f"      {axis}: ?\n")
            f.write("\n")

    print(f"wrote {args.output} ({len(picked)} papers)", file=sys.stderr)
    print(
        f"clusters represented: {len({r['cluster'] for r in picked})} / {len(clusters)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
