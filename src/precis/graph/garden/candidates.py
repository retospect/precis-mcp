"""Deterministic candidate detectors for the graph garden.

Every function here is read-only: SQL over the live graph plus cheap
Python scoring, returning :class:`GardenCandidate` rows the proposer turns
into review-ledger proposals (or drops). No LLM calls, no writes — a
candidate is evidence, never a verdict.

Bounds: each detector takes ``limit`` (max candidates emitted) and the
wide scans also take ``scan_cap`` (max refs inspected, oldest-first by
``ref_id`` — deterministic across ticks, so a bounded pass eventually
covers the corpus over successive runs).

Similarity legs: the embedding leg joins ``card_combined`` chunks
against ``chunk_embeddings`` restricted to the *default* embedder
(``embedders.is_default``), so mixed-embedder rows never produce a
nonsense distance. The lexical leg is the same ``lexical_overlap`` +
normalized-name rules the taxon mint-time dedup uses
(``precis.utils.text`` / ``precis.taxonomy.nodes.dimension_clash`` —
this layer must not import handlers).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.reading.concepts import normalize_name
from precis.store._mappers import _coerce_vector
from precis.taxonomy.nodes import dimension_clash
from precis.utils.lexical import lexical_overlap

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Ref kinds the gardener gardens. taxon (the taxonomy DAG), concept and
#: memory are the kinds the spec's passes name; papers/findings/jobs are
#: deliberately out — they have their own upkeep lanes.
GARDEN_KINDS: tuple[str, ...] = ("taxon", "concept", "memory")

#: Embedding-leg distance floor for duplicate candidates — the same
#: "same term, reworded" bar as the taxon mint-time dedup refusal
#: (``handlers.taxon.DEDUP_MAX_DISTANCE``; re-stated, not imported,
#: because this layer must not reach into handlers).
DUP_MAX_DISTANCE = 0.25

#: Content-word Jaccard on card text for the lexical leg — the
#: nearest-sibling refusal's fallback bar (``SIBLING_MIN_OVERLAP``).
DUP_MIN_LEXICAL = 0.7

#: A live node untouched, unlinked and unviewed for this many days is a
#: prune candidate (proposal only — retire stays a human decision).
STALE_DAYS = 180

#: How many same-kind refs must already carry a namespace before its
#: absence elsewhere is a tag-sweep candidate (avoids proposing a
#: namespace nobody uses).
TAG_SWEEP_MIN_SEEDED = 1

_DETECTOR_BY_PASS = {
    "merge": ("dup_pair",),
    "split": ("polysemy",),
    "relink": ("orphan",),
    "prune": ("dangling_edge", "cycle_edge", "stale"),
    "tag-sweep": ("untagged",),
    "siblings": ("sibling_confusion",),
}

#: Job ``params.pass`` values → detectors. ``all`` runs everything.
PASS_CHOICES: tuple[str, ...] = tuple(_DETECTOR_BY_PASS) + ("all",)


@dataclass(frozen=True, slots=True)
class GardenCandidate:
    """One deterministic upkeep signal — the proposer's input.

    ``target_kind``/``target_id`` is the reviews-ledger target the
    proposal will be written against (``ref`` or ``link``);
    ``other_id`` is the second party of a pair proposal (merge winner,
    relink parent, …). ``key`` is the dedup handle: one pending proposal
    per key (see :mod:`precis.graph.garden.proposals`).
    """

    detector: str
    action: str  # merge | split | relink | prune | tag
    target_kind: str  # 'ref' | 'link' (reviews.target_kind)
    target_id: int
    other_id: int | None = None
    title: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.action}:{self.target_kind}:{self.target_id}:{self.other_id or 0}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref_terms(meta: dict[str, Any], title: str) -> frozenset[str]:
    """Normalized name set of a node: title + ``meta.name`` + aliases."""
    terms = {normalize_name(title)}
    name = meta.get("name")
    if isinstance(name, str) and name:
        terms.add(normalize_name(name))
    for a in meta.get("aliases") or []:
        if isinstance(a, str) and a:
            terms.add(normalize_name(a))
    return frozenset(t for t in terms if t)


def _card_text(meta: dict[str, Any], title: str) -> str:
    """The text the lexical leg compares: name + definition when the node
    carries one, else the title."""
    definition = str(meta.get("definition") or "")
    name = str(meta.get("name") or title)
    return f"{name} {definition}".strip()


def _cosine(a: Any, b: Any) -> float:
    na, nb = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(na) * np.linalg.norm(nb))
    if denom == 0:
        return 1.0
    return 1.0 - float(np.dot(na, nb)) / denom


def _card_vectors(store: Store, ref_ids: list[int]) -> dict[int, list[float]]:
    """``{ref_id: vector}`` — the default-embedder ``card_combined``
    embedding for each ref that has one."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.ref_id, ce.vector FROM chunk_embeddings ce "
            "JOIN chunks c ON c.chunk_id = ce.chunk_id "
            "  AND c.chunk_kind = 'card_combined' AND c.retired_at IS NULL "
            "JOIN embedders em ON em.name = ce.embedder AND em.is_default "
            "WHERE c.ref_id = ANY(%(ids)s) AND ce.status = 'ok' "
            "  AND ce.vector IS NOT NULL",
            {"ids": list(ref_ids)},
        ).fetchall()
    out: dict[int, list[float]] = {}
    for ref, raw in rows:
        vec = _coerce_vector(raw)
        if vec is not None:
            out[int(ref)] = vec
    return out


def _start_node_ids(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        return [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'taxon' "
                "AND retired_at IS NULL AND meta->>'start' = 'true' "
                "ORDER BY ref_id"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# merge pass — duplicate nodes
# ---------------------------------------------------------------------------


def duplicate_candidates(
    store: Store,
    *,
    kinds: tuple[str, ...] = GARDEN_KINDS,
    limit: int = 50,
    scan_cap: int = 2000,
    max_distance: float = DUP_MAX_DISTANCE,
) -> list[GardenCandidate]:
    """Same-kind node pairs that look like one term minted twice.

    Two legs, either suffices (the pair is then judged by the proposer):

    - *embedding*: default-embedder ``card_combined`` vectors within
      ``max_distance`` cosine of each other, found via a LATERAL
      nearest-neighbour scan so the HNSW index serves each lookup.
    - *lexical*: a shared normalized name/alias term.

    Hard gate (both legs): a ``taxon`` pair whose metas
    :func:`~precis.taxonomy.nodes.dimension_clash` never qualifies —
    "band gap" in eV and in K are not duplicates however similar the
    embeddings say they are.

    Pair direction convention: ``other_id`` (the lower ref_id, the
    established node) is the proposed *winner*; ``target_id`` is the
    proposed loser the reviews row anchors on.
    """
    pairs: dict[tuple[int, int], dict[str, Any]] = {}

    # -- embedding leg -------------------------------------------------
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH mine AS (
              SELECT r.ref_id, r.kind, ce.vector
                FROM refs r
                JOIN chunks c ON c.ref_id = r.ref_id
                             AND c.chunk_kind = 'card_combined'
                             AND c.retired_at IS NULL
                JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                                        AND ce.status = 'ok'
                                        AND ce.vector IS NOT NULL
                JOIN embedders em ON em.name = ce.embedder AND em.is_default
               WHERE r.retired_at IS NULL AND r.kind = ANY(%(kinds)s)
               ORDER BY r.ref_id
               LIMIT %(cap)s
            )
            SELECT a.ref_id AS a_id, a.kind, b.ref_id AS b_id,
                   a.vector <=> b.vector AS dist
              FROM mine a
              JOIN LATERAL (
                SELECT b.ref_id, b.vector
                  FROM mine b
                 WHERE b.ref_id <> a.ref_id AND b.kind = a.kind
                 ORDER BY b.vector <=> a.vector
                 LIMIT 5
              ) b ON TRUE
             WHERE a.vector <=> b.vector < %(max_d)s
               AND a.ref_id < b.ref_id
             ORDER BY dist
             LIMIT %(n)s
            """,
            {
                "kinds": list(kinds),
                "cap": scan_cap,
                "max_d": max_distance,
                "n": limit,
            },
        ).fetchall()
    for a_id, kind, b_id, dist in rows:
        pairs.setdefault((int(a_id), int(b_id)), {"kind": str(kind)})["distance"] = (
            round(float(dist), 4)
        )

    # -- lexical leg ----------------------------------------------------
    with store.pool.connection() as conn:
        ref_rows = conn.execute(
            "SELECT ref_id, kind, title, meta FROM refs "
            "WHERE retired_at IS NULL AND kind = ANY(%(kinds)s) "
            "ORDER BY ref_id LIMIT %(cap)s",
            {"kinds": list(kinds), "cap": scan_cap},
        ).fetchall()
    nodes: dict[int, tuple[str, str, dict[str, Any]]] = {
        int(rid): (str(kind), str(title or ""), meta or {})
        for rid, kind, title, meta in ref_rows
    }
    term_index: dict[str, list[int]] = {}
    for rid, (_kind, title, meta) in nodes.items():
        for term in _ref_terms(meta, title):
            term_index.setdefault(term, []).append(rid)
    for term, rids in term_index.items():
        if len(rids) < 2:
            continue
        for i, a in enumerate(sorted(rids)):
            for b in sorted(rids)[i + 1 :]:
                if nodes[a][0] != nodes[b][0]:
                    continue  # cross-kind near-names are not merges
                ev = pairs.setdefault((a, b), {"kind": nodes[a][0]})
                ev.setdefault("shared_terms", []).append(term)

    out: list[GardenCandidate] = []
    for (a, b), ev in pairs.items():
        ka = nodes.get(a, (ev["kind"], "", {}))
        kb = nodes.get(b, (ev["kind"], "", {}))
        ev.setdefault("titles", [ka[1], kb[1]])
        overlap = lexical_overlap(_card_text(ka[2], ka[1]), _card_text(kb[2], kb[1]))
        if overlap:
            ev["lexical_overlap"] = round(overlap, 3)
        if ev["kind"] == "taxon" and dimension_clash(ka[2], kb[2]):
            continue  # hard gate — never a candidate
        out.append(
            GardenCandidate(
                detector="dup_pair",
                action="merge",
                target_kind="ref",
                target_id=b,  # convention: higher id = proposed loser
                other_id=a,
                title=f"dup pair {ev['kind']} {a}↔{b}",
                evidence=ev,
                payload={"loser": b, "winner": a, "kind": ev["kind"]},
            )
        )
    out.sort(key=lambda c: (c.evidence.get("distance", 9.9), c.target_id))
    return out[:limit]


# ---------------------------------------------------------------------------
# relink pass — orphan taxa (reach no start node)
# ---------------------------------------------------------------------------


def orphan_taxon_candidates(store: Store, *, limit: int = 50) -> list[GardenCandidate]:
    """Live taxon refs reaching no start node (``store.taxon_unrooted``).

    Each candidate names a *candidate parent* for the proposer/judge: the
    nearest start node by card-embedding distance when the orphan has an
    embedded card, else ``None`` (evidence still lists the live starts so
    the judge has the real choices). A store with no start nodes yields
    nothing — there is nothing to relink toward.
    """
    unrooted = store.taxon_unrooted()[:limit]
    if not unrooted:
        return []
    starts = _start_node_ids(store)
    if not starts:
        return []
    orphan_vecs = _card_vectors(store, unrooted)
    start_vecs = _card_vectors(store, starts)

    out: list[GardenCandidate] = []
    for rid in unrooted:
        vec = orphan_vecs.get(rid)
        parent: int | None = None
        dist: float | None = None
        if vec is not None and start_vecs:
            q = np.asarray(vec, dtype=float)
            best = min(
                start_vecs.items(),
                key=lambda kv: _cosine(q, np.asarray(kv[1], dtype=float)),
            )
            dist = _cosine(q, np.asarray(best[1], dtype=float))
            parent = best[0]
        ev: dict[str, Any] = {"reaches_no_start": True, "start_nodes": starts}
        if parent is not None and dist is not None:
            ev["candidate_parent_distance"] = round(dist, 4)
        out.append(
            GardenCandidate(
                detector="orphan",
                action="relink",
                target_kind="ref",
                target_id=rid,
                other_id=parent,
                title=f"taxon {rid} reaches no start node",
                evidence=ev,
                payload={"parent": parent, "relation": "specialises"},
            )
        )
    return out


# ---------------------------------------------------------------------------
# prune pass — stale nodes, dangling and contradictory edges
# ---------------------------------------------------------------------------


def stale_candidates(
    store: Store,
    *,
    kinds: tuple[str, ...] = GARDEN_KINDS,
    stale_days: int = STALE_DAYS,
    limit: int = 50,
) -> list[GardenCandidate]:
    """Live gardened-kind nodes no edge touches and nobody has viewed or
    touched since ``stale_days`` — retire proposals for the judge.
    Start nodes are exempt (they are anchors, not content)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.kind, r.title, "
            "  EXTRACT(EPOCH FROM (now() - r.updated_at)) / 86400 AS age_days, "
            "  r.last_viewed_at IS NULL AS never_viewed "
            "FROM refs r "
            "WHERE r.retired_at IS NULL AND r.kind = ANY(%(kinds)s) "
            "  AND r.updated_at < now() - make_interval(days => %(days)s) "
            "  AND (r.last_viewed_at IS NULL "
            "       OR r.last_viewed_at < now() - make_interval(days => %(days)s)) "
            "  AND COALESCE(r.meta->>'start', 'false') <> 'true' "
            "  AND NOT EXISTS (SELECT 1 FROM links l "
            "                  WHERE l.src_ref_id = r.ref_id "
            "                     OR l.dst_ref_id = r.ref_id) "
            "ORDER BY r.ref_id LIMIT %(n)s",
            {"kinds": list(kinds), "days": stale_days, "n": limit},
        ).fetchall()
    return [
        GardenCandidate(
            detector="stale",
            action="prune",
            target_kind="ref",
            target_id=int(rid),
            title=f"stale {kind} {rid}: {title!r}",
            evidence={
                "kind": kind,
                "age_days": round(float(age_days), 1),
                "never_viewed": bool(never_viewed),
                "degree": 0,
            },
            payload={"retire": True},
        )
        for rid, kind, title, age_days, never_viewed in rows
    ]


def dangling_edge_candidates(store: Store, *, limit: int = 50) -> list[GardenCandidate]:
    """Link rows whose src or dst ref is retired — edges to a dead node.
    (A *missing* endpoint is impossible under the FK; retired is the real
    case — ``retire_ref`` does not cascade links.)"""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.link_id, l.src_ref_id, l.dst_ref_id, l.relation, "
            "  (src.retired_at IS NOT NULL) AS src_retired, "
            "  (dst.retired_at IS NOT NULL) AS dst_retired "
            "FROM links l "
            "JOIN refs src ON src.ref_id = l.src_ref_id "
            "JOIN refs dst ON dst.ref_id = l.dst_ref_id "
            "WHERE src.retired_at IS NOT NULL OR dst.retired_at IS NOT NULL "
            "ORDER BY l.link_id LIMIT %(n)s",
            {"n": limit},
        ).fetchall()
    return [
        GardenCandidate(
            detector="dangling_edge",
            action="prune",
            target_kind="link",
            target_id=int(link_id),
            other_id=int(dst),
            title=f"edge {link_id} {rel}: {src}→{dst} touches a retired ref",
            evidence={
                "src": int(src),
                "dst": int(dst),
                "relation": rel,
                "src_retired": bool(src_ret),
                "dst_retired": bool(dst_ret),
            },
            payload={"delete_link": int(link_id)},
        )
        for link_id, src, dst, rel, src_ret, dst_ret in rows
    ]


def cycle_edge_candidates(store: Store, *, limit: int = 50) -> list[GardenCandidate]:
    """2-cycles in the taxon specialises/generalises DAG (A→B ∧ B→A).
    The write path guards cycle inserts; a cycle in the data is corrupted
    state — propose pruning the younger of the two edges."""
    from precis.store._taxon_ops import _EDGES

    with store.pool.connection() as conn:
        pairs = conn.execute(
            f"""
            WITH RECURSIVE {_EDGES}
            SELECT e1.child, e1.parent FROM e e1
             WHERE EXISTS (
               SELECT 1 FROM e e2
                WHERE e2.child = e1.parent AND e2.parent = e1.child)
               AND e1.child < e1.parent
             ORDER BY e1.child, e1.parent
             LIMIT %(n)s
            """,
            {"n": limit},
        ).fetchall()
    out: list[GardenCandidate] = []
    for child, parent in pairs:
        with store.pool.connection() as conn:
            links = conn.execute(
                "SELECT link_id FROM links "
                "WHERE ((src_ref_id = %(a)s AND dst_ref_id = %(b)s) "
                "   OR (src_ref_id = %(b)s AND dst_ref_id = %(a)s)) "
                "  AND relation IN ('specialises', 'generalises') "
                "ORDER BY link_id",
                {"a": int(child), "b": int(parent)},
            ).fetchall()
        if len(links) < 2:
            continue
        younger = max(int(l[0]) for l in links)
        out.append(
            GardenCandidate(
                detector="cycle_edge",
                action="prune",
                target_kind="link",
                target_id=younger,
                other_id=int(parent),
                title=f"taxon 2-cycle {child}↔{parent}: prune link {younger}",
                evidence={
                    "nodes": [int(child), int(parent)],
                    "cycle_links": [int(l[0]) for l in links],
                },
                payload={"delete_link": younger},
            )
        )
    return out


# ---------------------------------------------------------------------------
# tag-sweep pass — under-tagged refs on an in-use namespace
# ---------------------------------------------------------------------------


def untagged_candidates(
    store: Store,
    *,
    namespaces: tuple[str, ...] = ("DOMAIN", "TOPIC"),
    kinds: tuple[str, ...] = ("finding", "paper", "concept", "memory"),
    min_seeded: int = TAG_SWEEP_MIN_SEEDED,
    limit: int = 50,
) -> list[GardenCandidate]:
    """Live refs of ``kinds`` carrying no tag in ``namespaces`` while at
    least ``min_seeded`` same-kind live refs do — the namespace is in use
    for the kind and this ref was skipped. The proposed *value* is the
    proposer's job; the candidate only carries the gap."""
    out: list[GardenCandidate] = []
    for ns in namespaces:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, r.kind, r.title FROM refs r "
                "WHERE r.retired_at IS NULL AND r.kind = ANY(%(kinds)s) "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
                "     WHERE rt.ref_id = r.ref_id AND t.namespace = %(ns)s) "
                "  AND (SELECT count(*) FROM ref_tags rt "
                "        JOIN tags t USING (tag_id) "
                "        JOIN refs r2 ON r2.ref_id = rt.ref_id "
                "         WHERE t.namespace = %(ns)s AND r2.kind = r.kind "
                "           AND r2.retired_at IS NULL) >= %(min_seed)s "
                "ORDER BY r.ref_id LIMIT %(n)s",
                {"kinds": list(kinds), "ns": ns, "min_seed": min_seeded, "n": limit},
            ).fetchall()
        for rid, kind, title in rows:
            out.append(
                GardenCandidate(
                    detector="untagged",
                    action="tag",
                    target_kind="ref",
                    target_id=int(rid),
                    title=f"{kind} {rid} has no {ns}: tag ({title!r})",
                    evidence={"namespace": ns, "kind": kind},
                    payload={"namespace": ns},
                )
            )
    return out[:limit]


# ---------------------------------------------------------------------------
# split pass — polysemy signals
# ---------------------------------------------------------------------------


def polysemy_candidates(
    store: Store,
    *,
    kinds: tuple[str, ...] = ("taxon", "concept"),
    limit: int = 50,
) -> list[GardenCandidate]:
    """Two signal classes, different strengths:

    - *evidence*: a live ``finding`` linked to the node carrying
      ``meta.annotation='polysemy'`` (or ``meta.polysemy='true'``) — the
      split trigger the spec names. ``meta.uses`` (a string list) names
      the incompatible senses when the annotator captured them.
    - *hint*: a taxon reachable upward to ≥2 distinct start nodes — a
      term sitting in two root hierarchies is often two senses
      conflated. Marked ``hint=True``; weaker than a finding and the
      proposer prompt says so.
    """
    out: list[GardenCandidate] = []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT f.ref_id, f.title, f.meta, n.ref_id, n.title "
            "FROM refs f "
            "JOIN links l ON l.src_ref_id = f.ref_id OR l.dst_ref_id = f.ref_id "
            "JOIN refs n ON n.ref_id = (CASE WHEN l.src_ref_id = f.ref_id "
            "     THEN l.dst_ref_id ELSE l.src_ref_id END) "
            "WHERE f.kind = 'finding' AND f.retired_at IS NULL "
            "  AND (f.meta->>'annotation' = 'polysemy' "
            "       OR f.meta->>'polysemy' = 'true') "
            "  AND n.retired_at IS NULL AND n.kind = ANY(%(kinds)s) "
            "ORDER BY n.ref_id, f.ref_id LIMIT %(n)s",
            {"kinds": list(kinds), "n": limit},
        ).fetchall()
    for fid, ftitle, fmeta, nid, ntitle in rows:
        uses = (fmeta or {}).get("uses") or []
        out.append(
            GardenCandidate(
                detector="polysemy",
                action="split",
                target_kind="ref",
                target_id=int(nid),
                other_id=int(fid),
                title=f"polysemy finding fi{fid} on {ntitle!r}",
                evidence={
                    "finding_id": int(fid),
                    "finding_title": ftitle,
                    "uses": list(uses),
                    "hint": False,
                },
                payload={"uses": list(uses)},
            )
        )

    # disjoint-cluster hint: taxa with >=2 folded parents reachable to
    # >=2 distinct start nodes.
    from precis.store._taxon_ops import _EDGES

    with store.pool.connection() as conn:
        multi = conn.execute(
            f"""
            WITH RECURSIVE {_EDGES}
            SELECT e.child, array_agg(DISTINCT e.parent) AS parents
              FROM e GROUP BY e.child
             HAVING count(DISTINCT e.parent) >= 2
             ORDER BY e.child LIMIT %(n)s
            """,
            {"n": limit},
        ).fetchall()
    for child, parents in multi:
        starts = sorted(set(store.taxon_start_nodes_reached(int(child))))
        if len(starts) < 2:
            continue
        out.append(
            GardenCandidate(
                detector="polysemy",
                action="split",
                target_kind="ref",
                target_id=int(child),
                title=f"taxon {child} sits under {len(starts)} start nodes",
                evidence={
                    "hint": True,
                    "parents": [int(p) for p in parents],
                    "start_nodes": starts,
                },
                payload={"starts": starts},
            )
        )
    return out[:limit]


# ---------------------------------------------------------------------------
# siblings pass — exclusive-axis label confusion
# ---------------------------------------------------------------------------


def sibling_confusion_candidates(
    store: Store,
    *,
    exclusive_axes: tuple[str, ...] = (),
    limit: int = 50,
) -> list[GardenCandidate]:
    """Sibling taxa on a *declared-exclusive* axis a classifier can't
    separate: same parent, same ``meta.axis``, and either

    - a shared classified instance (one live ref ``instance-of``/``has-instance``
      both siblings), or
    - a per-run label swap: an instance's edge into sibling A carries
      ``links.meta.run=r1`` and into sibling B ``meta.run=r2``, ``r1≠r2``.

    The exclusivity declaration is a caller input
    (``params.exclusive_axes`` on the job) until the taxonomy lands axis
    declarations — nothing in the graph marks an axis exclusive today.
    Similarity alone is NOT a signal here (the 2026-10-05 sibling-
    disjointness ruling): disjoint siblings with similar labels yield
    nothing without a concrete confusion trace.
    """
    if not exclusive_axes:
        return []
    from precis.store._taxon_ops import _EDGES

    with store.pool.connection() as conn:
        pairs = conn.execute(
            f"""
            WITH RECURSIVE {_EDGES}
            SELECT e1.parent, e1.axis, e1.child AS a, e2.child AS b
              FROM e e1 JOIN e e2
                ON e2.parent = e1.parent AND e2.axis IS NOT DISTINCT FROM e1.axis
               AND e2.child > e1.child
             WHERE e1.axis = ANY(%(axes)s)
             ORDER BY e1.parent, e1.child, e2.child
             LIMIT %(n)s
            """,
            {"axes": list(exclusive_axes), "n": limit},
        ).fetchall()
    inst_links = (
        "SELECT l.src_ref_id AS inst, l.dst_ref_id AS tx, l.meta->>'run' AS run "
        "  FROM links l WHERE l.relation = 'instance-of' "
        " UNION ALL "
        "SELECT l.dst_ref_id, l.src_ref_id, l.meta->>'run' "
        "  FROM links l WHERE l.relation = 'has-instance'"
    )
    out: list[GardenCandidate] = []
    for parent, axis, a, b in pairs:
        with store.pool.connection() as conn:
            shared = [
                int(r[0])
                for r in conn.execute(
                    f"SELECT x.inst FROM ({inst_links}) x "
                    "WHERE x.tx = ANY(%(t)s) "
                    "GROUP BY x.inst HAVING count(DISTINCT x.tx) = 2 "
                    "ORDER BY x.inst",
                    {"t": [int(a), int(b)]},
                ).fetchall()
            ]
            swaps = conn.execute(
                f"SELECT DISTINCT a.inst, a.run, b.run FROM ({inst_links}) a "
                f"JOIN ({inst_links}) b ON b.inst = a.inst AND b.tx = %(b)s "
                "WHERE a.tx = %(a)s AND b.tx = %(b)s "
                "  AND a.run IS NOT NULL AND b.run IS NOT NULL "
                "  AND a.run <> b.run "
                "ORDER BY a.inst LIMIT %(n)s",
                {"a": int(a), "b": int(b), "n": limit},
            ).fetchall()
        if not shared and not swaps:
            continue
        out.append(
            GardenCandidate(
                detector="sibling_confusion",
                action="merge",  # judge may instead propose sharpen/re-axis
                target_kind="ref",
                target_id=int(b),
                other_id=int(a),
                title=f"sibling confusion on axis {axis!r}: {a} vs {b}",
                evidence={
                    "axis": axis,
                    "parent": int(parent),
                    "shared_instances": shared,
                    "run_swaps": [
                        {"instance": int(i), "run_a": ra, "run_b": rb}
                        for i, ra, rb in swaps
                    ],
                },
                payload={
                    "axis": axis,
                    "siblings": [int(a), int(b)],
                    "options": ["merge", "sharpen", "re-axis", "re-parent"],
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# dispatch table
# ---------------------------------------------------------------------------


def detectors_for(passes: tuple[str, ...]) -> list[str]:
    """Expand ``params.pass`` values into detector names."""
    if "all" in passes:
        return sorted({d for ds in _DETECTOR_BY_PASS.values() for d in ds})
    out: list[str] = []
    for p in passes:
        out.extend(_DETECTOR_BY_PASS.get(p, ()))
    return sorted(set(out))


def run_detectors(
    store: Store,
    *,
    passes: tuple[str, ...] = ("all",),
    kinds: tuple[str, ...] = GARDEN_KINDS,
    limit: int = 50,
    scan_cap: int = 2000,
    stale_days: int = STALE_DAYS,
    tag_namespaces: tuple[str, ...] = ("DOMAIN", "TOPIC"),
    tag_kinds: tuple[str, ...] = ("finding", "paper", "concept", "memory"),
    exclusive_axes: tuple[str, ...] = (),
) -> list[GardenCandidate]:
    """Run the detectors selected by ``passes`` and concatenate their
    candidates (each detector already bounded by ``limit``)."""
    out: list[GardenCandidate] = []
    for det in detectors_for(passes):
        if det == "dup_pair":
            out += duplicate_candidates(
                store, kinds=kinds, limit=limit, scan_cap=scan_cap
            )
        elif det == "orphan":
            out += orphan_taxon_candidates(store, limit=limit)
        elif det == "stale":
            out += stale_candidates(
                store, kinds=kinds, stale_days=stale_days, limit=limit
            )
        elif det == "dangling_edge":
            out += dangling_edge_candidates(store, limit=limit)
        elif det == "cycle_edge":
            out += cycle_edge_candidates(store, limit=limit)
        elif det == "untagged":
            out += untagged_candidates(
                store, namespaces=tag_namespaces, kinds=tag_kinds, limit=limit
            )
        elif det == "polysemy":
            out += polysemy_candidates(store, limit=limit)
        elif det == "sibling_confusion":
            out += sibling_confusion_candidates(
                store, exclusive_axes=exclusive_axes, limit=limit
            )
    return out


__all__ = [
    "DUP_MAX_DISTANCE",
    "DUP_MIN_LEXICAL",
    "GARDEN_KINDS",
    "PASS_CHOICES",
    "STALE_DAYS",
    "GardenCandidate",
    "cycle_edge_candidates",
    "dangling_edge_candidates",
    "detectors_for",
    "duplicate_candidates",
    "orphan_taxon_candidates",
    "polysemy_candidates",
    "run_detectors",
    "sibling_confusion_candidates",
    "stale_candidates",
    "untagged_candidates",
]
