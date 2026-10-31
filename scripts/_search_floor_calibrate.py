"""search-floor-calibrate — re-measure the per-kind semantic-only floors.

``SEMANTIC_ONLY_FOLD_DISTANCE`` (gr514956) is calibrated on prod's bge-m3
cosine-distance distribution; this is the read-only probe behind that
calibration, so a re-measure is a command, not a rewrite. No embedder is
reachable from SQL, so the default mode reuses the stored-chunk proxy of
the 2026-10-25/30 measurements (the method note on the constant in
``precis.store._mappers``): a sampled ref's stored ``query_kind`` chunk
embedding stands in for a user query — real free-text queries sit
farther from their targets, so the relevant distances below are a lower
bound and any floor this tool proposes errs toward keeping.

Per sampled query, in ``<=>`` cosine distance (the operator
``search_chunks_fused``'s semantic leg uses):

* relevant — distance to the nearest body chunk (``ord >= 0``) of the
  SAME ref; the query chunk itself and ``Probe.exclude_target_kinds``
  (gripe's comments — queries in this method, never targets) are
  excluded;
* noise — distances to the ``--noise-k`` nearest body chunks of OTHER
  refs of the same kind (some legitimately on-topic, so the noise pool
  is an upper bound on what a floor may cut).

The report is per kind: percentile lines, the relevant-dropped /
noise-removed share at each candidate floor, the value currently in the
table, and a proposed one — the midpoint when the distributions
separate cleanly, else the ``1 - --max-rel-loss`` quantile of relevant
capped 0.10 under ``SEMANTIC_DISTANCE_FLOOR`` (the 0.55 shape paper and
web landed on). The script PROPOSES only; changing the table stays a
human decision.

Free-text mode (``--queries-tsv FILE``, ``query_text<TAB>ref_id`` per
line) embeds real queries through the configured embedder — the
re-measure the gripe defers to "once an embedder is reachable". It needs
exactly one ``--kind`` (the noise pool's kind) and skips the chunk
sampling entirely.

Usage (via the ``search-floor-calibrate`` shell wrapper):

    scripts/_prod-run scripts/search-floor-calibrate          # prod, all kinds
    scripts/search-floor-calibrate --kind memory --sample 60  # dev DB

READ-ONLY: every statement is a SELECT. ``PRECIS_DATABASE_URL`` picks the
DB (dev by default; ``scripts/_prod-run`` supplies prod's).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store

from precis.store import SEMANTIC_DISTANCE_FLOOR, SEMANTIC_ONLY_FOLD_DISTANCE


@dataclass(frozen=True)
class Probe:
    """How one kind's stored chunks stand in for free-text queries.

    ``query_kind`` is the chunk_kind whose stored embedding plays the
    query; ``query_min_chars`` a length bar on the sampled query chunk
    (web's >200-char paragraphs — a stub query teaches the floor
    nothing); ``exclude_target_kinds`` chunk kinds dropped from BOTH the
    relevant and noise target pools.
    """

    query_kind: str
    query_min_chars: int = 0
    exclude_target_kinds: tuple[str, ...] = ()


#: The kind/probe pairs the 2026-10-25 and 2026-10-30 prod measurements
#: used (the calibration table comment on ``SEMANTIC_ONLY_FOLD_DISTANCE``).
PROBES: dict[str, Probe] = {
    "memory": Probe("card_combined"),
    "paper": Probe("card_title"),
    "patent": Probe("patent_section"),
    "web": Probe("paragraph", query_min_chars=200),
    "gripe": Probe("gripe_comment", exclude_target_kinds=("gripe_comment",)),
}

#: Candidate floors the per-kind table reports loss/removal at — the
#: historic probes (0.42/0.45) plus the band under the 0.65 corpus floor.
CANDIDATE_FLOORS = (0.42, 0.45, 0.50, 0.55, 0.60, 0.65)

#: A proposed floor stays this far under ``SEMANTIC_DISTANCE_FLOOR`` —
#: at the corpus floor itself nothing reachable is left to cut.
_CORPUS_MARGIN = 0.10


@dataclass
class KindMeasurement:
    """One kind's collected distances.

    ``rel`` holds one relevant distance per sampled query (queries whose
    ref has no other embedded body chunk land in ``n_no_body`` instead);
    ``noise`` the per-query nearest-``k`` other-ref distances pooled.
    """

    kind: str
    n_queries: int = 0
    n_no_body: int = 0
    rel: list[float] = field(default_factory=list)
    noise: list[float] = field(default_factory=list)


#: One calibration query: the query vector in ``'[…]'`` text form (binds
#: straight into ``<=>`` as ``%s::vector`` with no float round-trip), the
#: ref its relevant chunks live on, and the chunk_ids to exclude from
#: that ref's pool (the query chunk itself; empty in free-text mode).
Query = tuple[str, int, list[int]]


def sample_queries(
    conn: Any, *, kind: str, probe: Probe, embedder: str, sample: int
) -> list[tuple[int, int, str]]:
    """Draw ``sample`` stored query chunks for ``kind`` at random.

    Returns ``(chunk_id, ref_id, vector_text)`` rows. Sampling is at the
    chunk, not the ref — a chatty gripe weighs its comments as the
    measurement did.
    """
    rows = conn.execute(
        "SELECT c.chunk_id, c.ref_id, ce.vector::text "
        "FROM chunks c "
        "JOIN refs r ON r.ref_id = c.ref_id "
        "JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
        " AND ce.embedder = %s "
        "WHERE r.kind = %s AND r.retired_at IS NULL "
        " AND c.chunk_kind = %s AND c.retired_at IS NULL "
        " AND ce.status = 'ok' AND ce.vector IS NOT NULL "
        " AND char_length(btrim(c.text)) >= %s "
        "ORDER BY random() LIMIT %s",
        (embedder, kind, probe.query_kind, probe.query_min_chars, sample),
    ).fetchall()
    return [(int(cid), int(rid), str(v)) for cid, rid, v in rows]


def relevant_distance(
    conn: Any,
    *,
    ref_id: int,
    embedder: str,
    qvec: str,
    exclude_kinds: tuple[str, ...] | list[str],
    exclude_chunk_ids: list[int],
) -> float | None:
    """Distance to the nearest body chunk of the SAME ref.

    ``None`` when the ref has no other embedded body chunk — the caller
    counts those separately rather than folding them into the percentiles.
    """
    row = conn.execute(
        "SELECT min(ce.vector <=> %s::vector) "
        "FROM chunks c JOIN chunk_embeddings ce "
        " ON ce.chunk_id = c.chunk_id AND ce.embedder = %s "
        "WHERE c.ref_id = %s AND c.ord >= 0 "
        " AND NOT c.chunk_id = ANY(%s) "
        " AND NOT c.chunk_kind = ANY(%s) "
        " AND c.retired_at IS NULL "
        " AND ce.status = 'ok' AND ce.vector IS NOT NULL",
        (qvec, embedder, ref_id, list(exclude_chunk_ids), list(exclude_kinds)),
    ).fetchone()
    return float(row[0]) if row is not None and row[0] is not None else None


def noise_distances(
    conn: Any,
    *,
    kind: str,
    ref_id: int,
    embedder: str,
    qvec: str,
    exclude_kinds: tuple[str, ...] | list[str],
    noise_k: int,
) -> list[float]:
    """Distances to the ``noise_k`` nearest body chunks of OTHER refs of
    the same kind (nearest-first — this is the ``<=>`` ANN ordering)."""
    rows = conn.execute(
        "SELECT ce.vector <=> %s::vector AS d "
        "FROM chunks c "
        "JOIN refs r ON r.ref_id = c.ref_id "
        "JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
        " AND ce.embedder = %s "
        "WHERE r.kind = %s AND r.retired_at IS NULL "
        " AND c.ref_id <> %s AND c.ord >= 0 "
        " AND NOT c.chunk_kind = ANY(%s) "
        " AND c.retired_at IS NULL "
        " AND ce.status = 'ok' AND ce.vector IS NOT NULL "
        "ORDER BY ce.vector <=> %s::vector "
        "LIMIT %s",
        (qvec, embedder, kind, ref_id, list(exclude_kinds), qvec, noise_k),
    ).fetchall()
    return [float(r[0]) for r in rows]


def measure(
    conn: Any,
    *,
    kind: str,
    queries: list[Query],
    embedder: str,
    exclude_kinds: tuple[str, ...] | list[str],
    noise_k: int,
) -> KindMeasurement:
    """Run the relevant/noise measurement over explicit ``Query`` rows —
    the shape both the stored-chunk proxy and ``--queries-tsv`` feed."""
    out = KindMeasurement(kind=kind)
    for qvec, ref_id, chunk_ids in queries:
        out.n_queries += 1
        d = relevant_distance(
            conn,
            ref_id=ref_id,
            embedder=embedder,
            qvec=qvec,
            exclude_kinds=exclude_kinds,
            exclude_chunk_ids=chunk_ids,
        )
        if d is None:
            out.n_no_body += 1
        else:
            out.rel.append(d)
        out.noise.extend(
            noise_distances(
                conn,
                kind=kind,
                ref_id=ref_id,
                embedder=embedder,
                qvec=qvec,
                exclude_kinds=exclude_kinds,
                noise_k=noise_k,
            )
        )
    return out


def quantile(vals: list[float], q: float) -> float:
    """Linear-interpolated ``q``-quantile (``q`` in [0, 1]); ``vals`` need
    not be sorted."""
    s = sorted(vals)
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def share_above(vals: list[float], floor: float) -> float:
    """Percent of ``vals`` strictly above ``floor`` — the share a floor
    drops (relevant) or removes (noise); the boundary is inclusive, like
    the fused leg's ``d <= floor`` keep."""
    if not vals:
        return 0.0
    return 100.0 * sum(1 for v in vals if v > floor) / len(vals)


def propose_floor(
    rel: list[float],
    noise: list[float],
    *,
    corpus_floor: float = SEMANTIC_DISTANCE_FLOOR,
    max_rel_loss: float = 0.05,
) -> tuple[float | None, str]:
    """The floor this measurement proposes; ``(None, why)`` when it can't.

    Clean separation — every relevant hit closer than the closest noise —
    proposes the midpoint. Otherwise the ``1 - max_rel_loss`` quantile of
    relevant: the loosest cut that loses no more relevant hits than
    tolerated, capped ``_CORPUS_MARGIN`` under the corpus floor (a value
    at the corpus floor is unreachable anyway, and paper/web's landed
    picks sit at exactly that cap).
    """
    if not rel:
        return None, "no relevant distances — nothing to calibrate against"
    rel_max = max(rel)
    noise_min = min(noise) if noise else None
    if noise_min is not None and rel_max < noise_min:
        floor = round((rel_max + noise_min) / 2.0, 2)
        return floor, (
            f"clean separation (relevant max {rel_max:.2f} < noise min "
            f"{noise_min:.2f}) — midpoint"
        )
    cap = round(corpus_floor - _CORPUS_MARGIN, 2)
    q = quantile(rel, 1.0 - max_rel_loss)
    pct = round((1.0 - max_rel_loss) * 100)
    if q >= cap:
        return cap, (
            f"no separation and relevant p{pct} is {q:.2f} — capped at "
            f"corpus−{_CORPUS_MARGIN:.2f}; trims only the weakest "
            "uncorroborated tail"
        )
    return round(q, 2), (
        f"distributions overlap — relevant p{pct} "
        f"(loses <= {max_rel_loss:.0%} of relevant hits)"
    )


def report(
    m: KindMeasurement,
    *,
    current: float | None,
    corpus_floor: float = SEMANTIC_DISTANCE_FLOOR,
    max_rel_loss: float = 0.05,
) -> str:
    """Render one kind's measurement as the gripe's table shape."""
    lines = [f"{m.kind} — {m.n_queries} queries"]
    if m.n_no_body:
        lines[0] += f" ({m.n_no_body} with no other embedded body chunk)"
    lines[0] += f", {len(m.noise)} noise distances"
    if m.rel:
        lines.append(
            f"  relevant n={len(m.rel)}  "
            f"p10/p50/p90/max {quantile(m.rel, 0.10):.2f}/"
            f"{quantile(m.rel, 0.50):.2f}/{quantile(m.rel, 0.90):.2f}/"
            f"{max(m.rel):.2f}"
        )
    if m.noise:
        lines.append(
            f"  noise    n={len(m.noise)}  "
            f"p10/p50/p90/min {quantile(m.noise, 0.10):.2f}/"
            f"{quantile(m.noise, 0.50):.2f}/{quantile(m.noise, 0.90):.2f}/"
            f"{min(m.noise):.2f}"
        )
    floors = sorted({*CANDIDATE_FLOORS, *([current] if current is not None else [])})
    lines.append("   floor   rel-drop%  noise-rm%")
    for f in floors:
        mark = "  <- current" if f == current else ""
        lines.append(
            f"   {f:.2f}   {share_above(m.rel, f):8.1f} "
            f"{share_above(m.noise, f):9.1f}{mark}"
        )
    floor, why = propose_floor(
        m.rel, m.noise, corpus_floor=corpus_floor, max_rel_loss=max_rel_loss
    )
    cur = f"{current:.2f}" if current is not None else "none"
    prop = f"{floor:.2f}" if floor is not None else "none"
    lines.append(f"  current {cur}   proposed {prop} — {why}")
    return "\n".join(lines)


def _resolve_probe(args: argparse.Namespace, kind: str, parser: Any) -> Probe:
    """The ``Probe`` for ``kind`` after ``--query-kind``/``--query-min-chars``/
    ``--exclude-kind`` overrides (applied to every ``--kind`` given)."""
    base = PROBES.get(kind)
    if base is None:
        if not args.query_kind:
            parser.error(
                f"--kind {kind!r} has no default probe; pass --query-kind to "
                "name the chunk kind standing in for queries"
            )
        base = Probe(args.query_kind)
    query_kind = args.query_kind or base.query_kind
    min_chars = (
        args.query_min_chars
        if args.query_min_chars is not None
        else base.query_min_chars
    )
    excluded = tuple(dict.fromkeys((*base.exclude_target_kinds, *args.exclude_kind)))
    return Probe(query_kind, min_chars, excluded)


def _tsv_queries(path: Path, *, embedder: Any, dim: int) -> list[Query]:
    """``query_text<TAB>ref_id`` lines → embedded ``Query`` rows.

    ``#`` lines and blanks are skipped. A row whose ref_id doesn't parse
    aborts the run — a silently skipped line reads as a shorter relevant
    tail than the operator judged.
    """
    queries: list[Query] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        text, sep, ref_s = line.rpartition("\t")
        if not sep:
            raise ValueError(f"{path}:{lineno}: expected 'text<TAB>ref_id'")
        ref_id = int(ref_s.strip())
        vec = embedder.embed_one(text)
        if len(vec) != dim:
            raise ValueError(
                f"{path}:{lineno}: embedder returned dim {len(vec)}, "
                f"store expects {dim}"
            )
        queries.append(("[" + ",".join(map(repr, vec)) + "]", ref_id, []))
    return queries


def main() -> None:
    p = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog=(
            "proposes only — SEMANTIC_ONLY_FOLD_DISTANCE changes are a "
            "human decision; re-measure with free text once an embedder "
            "is reachable (--queries-tsv)"
        ),
    )
    p.add_argument(
        "--kind",
        action="append",
        dest="kinds",
        metavar="KIND",
        help="ref kind to measure (repeatable; default: " + ", ".join(PROBES) + ")",
    )
    p.add_argument(
        "--query-kind",
        metavar="CHUNK_KIND",
        help="override the chunk kind standing in for queries "
        "(required for a --kind with no default probe)",
    )
    p.add_argument(
        "--query-min-chars",
        type=int,
        default=None,
        metavar="N",
        help="override the query chunk's minimum trimmed length",
    )
    p.add_argument(
        "--exclude-kind",
        action="append",
        default=[],
        metavar="CHUNK_KIND",
        help="extra chunk kind(s) dropped from the relevant/noise pools (repeatable)",
    )
    p.add_argument(
        "--sample",
        type=int,
        default=100,
        metavar="N",
        help="query chunks to draw per kind (default 100)",
    )
    p.add_argument(
        "--noise-k",
        type=int,
        default=10,
        metavar="K",
        help="nearest other-ref body chunks per query (default 10)",
    )
    p.add_argument(
        "--max-rel-loss",
        type=float,
        default=0.05,
        metavar="F",
        help="tolerated relevant-loss fraction for the proposal (default 0.05)",
    )
    p.add_argument(
        "--queries-tsv",
        type=Path,
        default=None,
        metavar="FILE",
        help="free-text mode: embed 'text<TAB>ref_id' lines instead of "
        "sampling stored chunks (needs exactly one --kind)",
    )
    args = p.parse_args()

    kinds = args.kinds or list(PROBES)
    if args.queries_tsv and len(kinds) != 1:
        p.error("--queries-tsv needs exactly one --kind (the noise pool's kind)")

    store, cfg = open_store()
    try:
        tsv_embedder = None
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
            ).fetchone()
            if row is None:
                raise RuntimeError("no default embedder registered")
            embedder_name = str(row[0])
            for kind in kinds:
                probe = _resolve_probe(args, kind, p)
                if args.queries_tsv:
                    if tsv_embedder is None:
                        from _common import make_embedder_for

                        tsv_embedder = make_embedder_for(store, cfg)
                    queries = _tsv_queries(
                        args.queries_tsv,
                        embedder=tsv_embedder,
                        dim=store.embedding_dim(),
                    )
                else:
                    sampled = sample_queries(
                        conn,
                        kind=kind,
                        probe=probe,
                        embedder=embedder_name,
                        sample=args.sample,
                    )
                    queries = [(vec, rid, [cid]) for cid, rid, vec in sampled]
                m = measure(
                    conn,
                    kind=kind,
                    queries=queries,
                    embedder=embedder_name,
                    exclude_kinds=probe.exclude_target_kinds,
                    noise_k=args.noise_k,
                )
                print(
                    report(
                        m,
                        current=SEMANTIC_ONLY_FOLD_DISTANCE.get(kind),
                        max_rel_loss=args.max_rel_loss,
                    )
                )
    finally:
        store.close()


if __name__ == "__main__":
    main()
