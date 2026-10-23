"""Deterministic candidate detectors for the graph garden.

Every function here is read-only: SQL over the live graph plus cheap
Python scoring, returning :class:`GardenCandidate` rows the proposer turns
into review-ledger proposals (or drops). No LLM calls, no writes — a
candidate is evidence, never a verdict.

Bounds: each detector takes ``limit`` (max candidates emitted) and the
wide scans also take ``scan_cap`` (max refs inspected per kind per run).
The wide scans are windowed per kind past a saved cursor
(:attr:`DetectorRun.cursors`, persisted on the job's ``meta.garden``)
and wrap at the end, so successive runs walk every kind instead of
re-reading the oldest refs (gr504843). :func:`run_detectors` adds a
per-detector wall-clock deadline plus skip-and-continue on a Postgres
statement timeout, so one slow detector cannot zero the tick
(gr503580 — ``untagged`` did exactly that on the first prod run).

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
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import numpy as np
from psycopg.errors import QueryCanceled

from precis.reading.concepts import normalize_name
from precis.store._mappers import _coerce_vector
from precis.taxonomy.nodes import dimension_clash
from precis.utils.deadline import DeadlineExceeded, call_with_deadline
from precis.utils.lexical import content_words, lexical_overlap

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

#: Which chunk's vector the dup embedding leg compares, per kind (default
#: ``card_combined``). A memory's card is its title cut to 80 chars and
#: dream memories open alike, so memories compare their body (gr504843).
DUP_VECTOR_CHUNK: dict[str, str] = {"memory": "memory_body"}

#: Content-word Jaccard on card text for the lexical leg — the
#: nearest-sibling refusal's fallback bar (``SIBLING_MIN_OVERLAP``).
DUP_MIN_LEXICAL = 0.7

#: Name length bound of :func:`short_sibling_pair`, in content words.
SHORT_SIBLING_MAX_WORDS = 2

#: The reject reason that rule records — machine-readable, counted per
#: detector by ``precis garden stats``.
SIBLING_REJECT_REASON = "sibling_distinct_names"

#: Relink-narrowing floor (graph-gardener-value §3, 2026-10-21 ruling).
#: An orphan taxon whose nearest start node sits *inside* this card-cosine
#: distance is dropped: within the fisheye ``+recall`` rung's distance
#: floor (``eye_render._RECALL_MAX_DISTANCE``; re-stated like
#: ``DUP_MAX_DISTANCE`` — this layer must not reach into render code) the
#: index already lists that start node as a neighbour, so a
#: ``specialises`` proposal to it restates what retrieval recomputes on
#: demand. Past it the pair never co-retrieves, so the edge holds
#: something RAG cannot. A taxon with no card vector keeps — the index
#: has nothing to offer it either way.
RELINK_MIN_DISTANCE = 0.6

#: Redundant-link bars (graph-gardener-value §3, same ruling): a machine
#: ``related-to`` edge whose endpoints are mutual top-``PRUNE_TOP_K``
#: card-vector neighbours *and* inside ``PRUNE_MAX_DISTANCE`` duplicates
#: the index — the ``+recall`` rung lists the pair for each other anyway
#: — so it is a prune proposal. ``PRUNE_MAX_DISTANCE`` shares the
#: recall-floor justification of ``RELINK_MIN_DISTANCE``, kept as its own
#: constant so the two passes can diverge.
PRUNE_TOP_K = 5
PRUNE_MAX_DISTANCE = 0.6

#: redundant_link probes neighbours for this many pairs per statement,
#: closest pairs first, and stops probing past ``PRUNE_PROBE_BUDGET_S`` —
#: under :data:`DETECTOR_DEADLINE_S` so a slow index returns the
#: candidates found so far instead of none (gr504843).
PRUNE_PROBE_BATCH = 32
PRUNE_PROBE_BUDGET_S = 40.0

#: A live node untouched, unlinked and unviewed for this many days is a
#: prune candidate (proposal only — retire stays a human decision).
STALE_DAYS = 180

#: Relations that record provenance — where a node came from, what
#: replaced or corrected it. The edge stays true after either end
#: retires, so a retired endpoint never makes it a dangling-edge
#: candidate (gr504843: ``supersedes`` → retired paper is by design).
PROVENANCE_RELATIONS: frozenset[str] = frozenset(
    {
        "supersedes",
        "superseded-by",
        "superseded-in",
        "derived-from",
        "derived-into",
        "fixes",
        "fixed-by",
        "retracts",
        "retracted-by",
        "corrects",
        "corrected-by",
        "cites",
        "cited-by",
    }
)

#: Kinds whose retired refs are history, not dead nodes: a done todo or a
#: retired agentlog/job is the record of how its live neighbour was made.
HISTORY_KINDS: frozenset[str] = frozenset({"agentlog", "todo", "job"})

#: Normalized titles shorter than this never pair two papers — equal
#: short titles ("Editorial", "Introduction") say nothing about identity.
PAPER_MIN_TITLE_CHARS = 20

#: Per-detector wall-clock budget inside :func:`run_detectors`
#: (gr503580). Past it the detector is *abandoned* — the daemon thread
#: keeps running until its queries hit the role's ``statement_timeout``
#: and the pool reclaims the connections, exactly like the scheduler's
#: per-cadence bound. Bounded scans finish in seconds; 60 s is already
#: ~30x a healthy detector's time and stays well under the 120 s
#: ``agent_rw`` statement timeout that ends the abandoned work.
DETECTOR_DEADLINE_S = 60.0

#: How many same-kind refs must already carry a namespace before its
#: absence elsewhere is a tag-sweep candidate (avoids proposing a
#: namespace nobody uses).
TAG_SWEEP_MIN_SEEDED = 1

_DETECTOR_BY_PASS = {
    "merge": ("dup_pair",),
    "split": ("polysemy",),
    "relink": ("orphan",),
    "prune": ("dangling_edge", "cycle_edge", "stale", "redundant_link"),
    "tag-sweep": ("untagged",),
    "siblings": ("sibling_confusion",),
    "paper-identity": ("paper_identity",),
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


def _names(meta: dict[str, Any], title: str) -> list[str]:
    """A node's raw names: title, ``meta.name`` and aliases."""
    raw = [title, meta.get("name"), *(meta.get("aliases") or [])]
    return list(dict.fromkeys(str(n) for n in raw if isinstance(n, str) and n))


_WORDS = re.compile(r"[a-z0-9]+")


def _without_names(text: str, names: list[str]) -> str:
    """``text`` lowercased with the node's own names cut out, as a word
    string — what is left of a definition once its name is removed."""
    out = text.lower()
    for name in sorted(names, key=len, reverse=True):
        out = out.replace(name.lower(), " ")
    return " ".join(_WORDS.findall(out))


def _text_evidence(
    ta: str, ma: dict[str, Any], tb: str, mb: dict[str, Any]
) -> dict[str, Any]:
    """Name and definition evidence for a taxon/concept pair, kept apart
    (gr504843). The old single ``lexical_overlap`` ran on name +
    definition, so legacy placeholder definitions ("Pedal: a component
    category, carried over from the legacy … registry") scored Pedal and
    Wheel 0.833 and the proposer read it as name agreement.

    ``name_overlap`` compares titles/names/aliases; ``definition_overlap``
    the definitions; ``definition_template`` marks two definitions that
    are the same text once each node's own name is cut out while the
    names share no term — a placeholder, evidence of nothing."""
    na, nb = _names(ma, ta), _names(mb, tb)
    ev: dict[str, Any] = {
        "name_overlap": round(lexical_overlap(" ".join(na), " ".join(nb)), 3)
    }
    da, db = str(ma.get("definition") or ""), str(mb.get("definition") or "")
    if da and db:
        ev["definition_overlap"] = round(lexical_overlap(da, db), 3)
        rest = _without_names(da, na)
        if (
            rest
            and rest == _without_names(db, nb)
            and _ref_terms(ma, ta).isdisjoint(_ref_terms(mb, tb))
        ):
            ev["definition_template"] = True
    return ev


def short_sibling_pair(store: Store, a: int, b: int) -> bool:
    """The short-sibling rule (gr504843): True when ``a`` and ``b`` are
    taxa under one parent, each named in at most
    :data:`SHORT_SIBLING_MAX_WORDS` content words, with no word shared
    across their names and aliases — distinct children of one family,
    not one term minted twice. Reviews 8288 (Pedal/Wheel), 8300
    (Height/Width) and 8302 (Hose/Pipe) are this shape; their cards embed
    0.15-0.18 apart because family siblings share definition wording.

    Why the word bound: the one embedding-only sibling merge a judge
    approved on prod (460157 "Price per gram" into 460129 "Cost per unit
    mass") has 2 and 3 content words; requiring both names short keeps
    it a candidate. Same-name duplicates share a word and never match."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs "
            "WHERE ref_id = ANY(%s) AND kind = 'taxon'",
            ([a, b],),
        ).fetchall()
    if len(rows) != 2:
        return False
    nodes = {int(r[0]): (str(r[1] or ""), r[2] or {}) for r in rows}
    parents = _taxon_parents(store, [a, b])
    if not parents.get(a, set()) & parents.get(b, set()):
        return False
    words = []
    for rid in (a, b):
        title, meta = nodes[rid]
        head = content_words(str(meta.get("name") or title))
        if not head or len(head) > SHORT_SIBLING_MAX_WORDS:
            return False
        words.append(
            frozenset().union(*(content_words(n) for n in _names(meta, title)))
        )
    return words[0].isdisjoint(words[1])


def _taxon_parents(store: Store, ids: list[int]) -> dict[int, set[int]]:
    """``{taxon: its live parents}`` over the specialises/generalises
    edges, for the ``siblings_under`` evidence."""
    if not ids:
        return {}
    from precis.store._taxon_ops import _EDGES

    with store.pool.connection() as conn:
        rows = conn.execute(
            f"WITH {_EDGES} SELECT child, parent FROM e WHERE child = ANY(%s)",
            (sorted(set(ids)),),
        ).fetchall()
    out: dict[int, set[int]] = {}
    for child, parent in rows:
        out.setdefault(int(child), set()).add(int(parent))
    return out


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


def _top_k_card_neighbours(
    store: Store,
    ref_ids: set[int],
    vecs: dict[int, list[float]],
    *,
    k: int,
) -> dict[int, frozenset[int]]:
    """``{ref_id: its top-``k`` neighbour ref_ids}`` against the live card
    index, same ``card_combined`` / default-embedder scope as
    :func:`_card_vectors`. Refs without a card vector are absent from the
    result (no neighbours to compare).

    One statement for the whole batch: a LATERAL ANN probe per endpoint
    on one connection with the filtered-ANN settings applied once — the
    per-endpoint round trip and connection checkout was what pushed
    redundant_link past its 60 s deadline (gr504843)."""
    from precis.store._chunks_ops import _prepare_filtered_ann

    ids = [rid for rid in sorted(ref_ids) if vecs.get(rid) is not None]
    if not ids:
        return {}
    with store.pool.connection() as conn:
        # Filtered-ANN under-fill guard (see _prepare_filtered_ann): card
        # chunks are a minority of the index, so a plain HNSW scan can
        # return fewer than k card rows.
        _prepare_filtered_ann(conn, k * 2)
        rows = conn.execute(
            "SELECT q.rid, n.ref_id FROM unnest(%(ids)s::bigint[], "
            "  %(vs)s::text[]) WITH ORDINALITY AS q(rid, v, o) "
            "CROSS JOIN LATERAL ("
            "  SELECT c.ref_id, ce.vector <=> q.v::vector AS d FROM chunks c "
            "  JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
            "    AND ce.status = 'ok' AND ce.vector IS NOT NULL "
            "  JOIN embedders em ON em.name = ce.embedder AND em.is_default "
            "  WHERE c.chunk_kind = 'card_combined' AND c.retired_at IS NULL "
            "    AND c.ref_id <> q.rid "
            "  ORDER BY ce.vector <=> q.v::vector LIMIT %(n)s) n "
            "ORDER BY q.o, n.d",
            {
                "ids": ids,
                # pgvector text form — one text[] binds the whole batch.
                "vs": ["[" + ",".join(map(str, vecs[rid])) + "]" for rid in ids],
                "n": k * 2,
            },
        ).fetchall()
    tops: dict[int, list[int]] = {rid: [] for rid in ids}
    for rid, other in rows:
        top = tops[int(rid)]
        if int(other) not in top and len(top) < k:  # >1 card chunk per ref
            top.append(int(other))
    return {rid: frozenset(top) for rid, top in tops.items()}


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


def _scan_window(
    store: Store, kind: str, *, after: int, size: int
) -> tuple[list[int], int]:
    """The next ``size`` live ``kind`` ref ids past ``after`` (ascending)
    and the cursor to save: the window's last id, or ``0`` when the window
    reached the end of the kind — the next run wraps to the start."""
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE retired_at IS NULL AND kind = %s "
                "AND ref_id > %s ORDER BY ref_id LIMIT %s",
                (kind, after, size),
            ).fetchall()
        ]
    return ids, (ids[-1] if len(ids) >= size else 0)


def _kind_count(store: Store, kind: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE retired_at IS NULL AND kind = %s",
            (kind,),
        ).fetchone()
    return int(row[0]) if row else 0


def dup_window_size(n_live: int, scan_cap: int) -> int:
    """Refs per run the dup scan takes from one kind: each window ref is
    compared with every live ref of its kind, so the window shrinks as
    the kind grows to keep the comparisons at ``scan_cap**2`` — the
    cost of the old 2000-by-2000 window. Never below 1 or above
    ``scan_cap``."""
    if n_live <= 0:
        return scan_cap
    return max(1, min(scan_cap, (scan_cap * scan_cap) // n_live))


def _dup_embedding_pairs(
    store: Store,
    kind: str,
    window: list[int],
    *,
    max_distance: float,
    limit: int,
) -> list[tuple[int, int, float]]:
    """``(a, b, distance)`` for window refs ``a`` whose nearest same-kind
    neighbour ``b`` (any live ref of the kind) sits inside
    ``max_distance``. Vectors: :data:`DUP_VECTOR_CHUNK` for the kind —
    the body for memory, the card elsewhere. The neighbour set is a
    MATERIALIZED CTE, so each lookup is an exact scan of the kind, not a
    filtered HNSW probe that under-fills."""
    chunk_kind = DUP_VECTOR_CHUNK.get(kind, "card_combined")
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH kind_vecs AS MATERIALIZED (
              SELECT DISTINCT ON (r.ref_id) r.ref_id, ce.vector
                FROM refs r
                JOIN chunks c ON c.ref_id = r.ref_id
                             AND c.chunk_kind = %(ck)s
                             AND c.retired_at IS NULL
                JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                                        AND ce.status = 'ok'
                                        AND ce.vector IS NOT NULL
                JOIN embedders em ON em.name = ce.embedder AND em.is_default
               WHERE r.retired_at IS NULL AND r.kind = %(kind)s
               ORDER BY r.ref_id, c.ord
            )
            SELECT a.ref_id, b.ref_id, a.vector <=> b.vector AS dist
              FROM kind_vecs a
              JOIN LATERAL (
                SELECT b.ref_id, b.vector
                  FROM kind_vecs b
                 WHERE b.ref_id <> a.ref_id
                 ORDER BY b.vector <=> a.vector
                 LIMIT 5
              ) b ON TRUE
             WHERE a.ref_id = ANY(%(win)s)
               AND a.vector <=> b.vector < %(max_d)s
             ORDER BY dist
             LIMIT %(n)s
            """,
            {
                "ck": chunk_kind,
                "kind": kind,
                "win": window,
                "max_d": max_distance,
                "n": limit * 2,  # each pair can surface from both ends
            },
        ).fetchall()
    return [(int(a), int(b), float(d)) for a, b, d in rows]


def _memory_body_twins(store: Store, window: list[int]) -> list[tuple[int, int]]:
    """``(a, b)`` live memory pairs, ``a`` in the window, whose
    ``memory_body`` text is byte-identical (md5 over the body chunks in
    ``ord`` order) — the body-hash leg."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "WITH h AS ("
            "  SELECT c.ref_id, md5(string_agg(c.text, E'\\n' ORDER BY c.ord)) AS sha"
            "    FROM chunks c JOIN refs r ON r.ref_id = c.ref_id"
            "   WHERE r.kind = 'memory' AND r.retired_at IS NULL"
            "     AND c.chunk_kind = 'memory_body' AND c.retired_at IS NULL"
            "     AND c.ord >= 0"
            "   GROUP BY c.ref_id) "
            "SELECT a.ref_id, b.ref_id FROM h a "
            "JOIN h b ON b.sha = a.sha AND b.ref_id <> a.ref_id "
            "WHERE a.ref_id = ANY(%(win)s) ORDER BY a.ref_id, b.ref_id",
            {"win": window},
        ).fetchall()
    return [(int(a), int(b)) for a, b in rows]


def _lexical_pairs(
    store: Store, kind: str, window: list[int]
) -> dict[tuple[int, int], list[str]]:
    """``{(lo, hi): shared normalized terms}`` for same-kind pairs sharing
    a name/alias term, at least one of them in the window."""
    with store.pool.connection() as conn:
        ref_rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs "
            "WHERE retired_at IS NULL AND kind = %s ORDER BY ref_id",
            (kind,),
        ).fetchall()
    in_window = set(window)
    term_index: dict[str, list[int]] = {}
    for rid, title, meta in ref_rows:
        for term in _ref_terms(meta or {}, str(title or "")):
            term_index.setdefault(term, []).append(int(rid))
    out: dict[tuple[int, int], list[str]] = {}
    for term, rids in term_index.items():
        if len(rids) < 2:
            continue
        for i, a in enumerate(rids):
            for b in rids[i + 1 :]:
                if a in in_window or b in in_window:
                    out.setdefault((a, b), []).append(term)
    return out


def _ref_shas(store: Store, ids: list[int]) -> dict[int, str]:
    """``{ref_id: precis_target_sha('ref', id)}`` for the given live refs —
    the per-endpoint pin a proposal carries so apply can tell the graph
    moved (review finding: a merge may apply only while both endpoints
    are the refs the judge saw)."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        return {
            int(r[0]): str(r[1])
            for r in conn.execute(
                "SELECT ref_id, precis_target_sha('ref', ref_id) "
                "FROM refs WHERE ref_id = ANY(%s)",
                (sorted(set(ids)),),
            ).fetchall()
            if r[1] is not None
        }


def _pair_vector_distance(store: Store, kind: str, a: int, b: int) -> float | None:
    """The pair's cosine distance on the kind's dup-leg vector
    (:data:`DUP_VECTOR_CHUNK` — ``memory_body`` for memories, the card
    elsewhere), ``None`` when either side lacks an embedded vector."""
    chunk_kind = DUP_VECTOR_CHUNK.get(kind, "card_combined")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (c.ref_id) c.ref_id, ce.vector "
            "FROM chunks c "
            "JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id "
            "  AND ce.status = 'ok' AND ce.vector IS NOT NULL "
            "JOIN embedders em ON em.name = ce.embedder AND em.is_default "
            "WHERE c.ref_id = ANY(%(ids)s) AND c.chunk_kind = %(ck)s "
            "  AND c.retired_at IS NULL "
            "ORDER BY c.ref_id, c.ord",
            {"ids": [a, b], "ck": chunk_kind},
        ).fetchall()
    vecs = {int(r[0]): _coerce_vector(r[1]) for r in rows}
    va, vb = vecs.get(a), vecs.get(b)
    if va is None or vb is None:
        return None
    return _cosine(va, vb)


def _memory_bodies_identical(store: Store, a: int, b: int) -> bool:
    """The body-hash leg for one pair: the two memories' ``memory_body``
    text is byte-identical (same md5 as :func:`_memory_body_twins`)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.ref_id, md5(string_agg(c.text, E'\\n' ORDER BY c.ord)) "
            "FROM chunks c WHERE c.ref_id = ANY(%s) "
            "  AND c.chunk_kind = 'memory_body' AND c.retired_at IS NULL "
            "  AND c.ord >= 0 GROUP BY c.ref_id",
            ([a, b],),
        ).fetchall()
    shas = {int(r[0]): r[1] for r in rows if r[1] is not None}
    return shas.get(a) is not None and shas.get(a) == shas.get(b)


def dup_pair_qualifies(store: Store, a: int, b: int) -> str | None:
    """The dup detector's bars recomputed for one pair at apply time —
    the consumer's deterministic re-check before an approved merge
    writes (review finding: the model's verdict is only as good as the
    state it judged). Returns ``None`` when the pair still qualifies:
    both live, same kind, no taxon dimension clash, and at least one
    leg — embedding distance under :data:`DUP_MAX_DISTANCE` on the
    kind's vector chunk, a shared name/alias term (non-memory), or an
    identical memory body. Else a short phrase for the reject ``why``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, kind, title, meta, retired_at FROM refs "
            "WHERE ref_id = ANY(%s)",
            ([a, b],),
        ).fetchall()
    nodes = {int(r[0]): r for r in rows}
    ra, rb = nodes.get(a), nodes.get(b)
    if ra is None or rb is None:
        return "an endpoint no longer exists"
    if ra[4] is not None or rb[4] is not None:
        return "an endpoint is retired"
    kind = str(ra[1])
    if kind != str(rb[1]):
        return "the pair is not same-kind"
    ta, ma = str(ra[2] or ""), ra[3] or {}
    tb, mb = str(rb[2] or ""), rb[3] or {}
    if kind == "taxon" and dimension_clash(ma, mb):
        return "the taxa clash on dimension"
    if kind == "memory" and _memory_bodies_identical(store, a, b):
        return None
    if kind != "memory" and _ref_terms(ma, ta) & _ref_terms(mb, tb):
        return None
    dist = _pair_vector_distance(store, kind, a, b)
    if dist is not None and dist < DUP_MAX_DISTANCE:
        return None
    return (
        "no shared term and no sub-floor embedding distance"
        if dist is None
        else f"the pair now embeds {dist:.3f} apart (floor {DUP_MAX_DISTANCE})"
    )


def duplicate_candidates(
    store: Store,
    *,
    kinds: tuple[str, ...] = GARDEN_KINDS,
    limit: int = 50,
    scan_cap: int = 2000,
    max_distance: float = DUP_MAX_DISTANCE,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Same-kind node pairs that look like one term minted twice.

    Scanned per kind, one window per run: the next
    :func:`dup_window_size` live refs past the kind's saved cursor
    (``cursors['dup_pair:<kind>']``), each compared with every live ref of
    its kind. The cursor to save lands in ``next_cursors`` — successive
    runs walk the kind and wrap at its end (gr504843: the oldest 2,000
    refs, all memories, were the only ones ever read).

    Legs, any suffices (the pair is then judged by the proposer):

    - *embedding*: default-embedder vectors within ``max_distance``
      cosine. taxon/concept compare their ``card_combined`` card; memory
      compares its ``memory_body`` — a memory card is its title cut to
      80 chars, and dream memories share an opening line, so the card
      put distinct memories at distance 0 (gr504843).
    - *body hash* (memory only): byte-identical body text.
    - *lexical* (not memory): a shared normalized name/alias term.

    Hard gate (all legs): a ``taxon`` pair whose metas
    :func:`~precis.taxonomy.nodes.dimension_clash` never qualifies —
    "band gap" in eV and in K are not duplicates however similar the
    embeddings say they are.

    Pair direction convention: ``other_id`` (the lower ref_id, the
    established node) is the proposed *winner*; ``target_id`` is the
    proposed loser the reviews row anchors on.
    """
    cursors = cursors or {}
    pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for kind in kinds:
        ckey = f"dup_pair:{kind}"
        size = dup_window_size(_kind_count(store, kind), scan_cap)
        window, nxt = _scan_window(store, kind, after=cursors.get(ckey, 0), size=size)
        if next_cursors is not None:
            next_cursors[ckey] = nxt
        if not window:
            continue
        for a, b, dist in _dup_embedding_pairs(
            store, kind, window, max_distance=max_distance, limit=limit
        ):
            ev = pairs.setdefault((min(a, b), max(a, b)), {"kind": kind})
            ev["distance"] = round(dist, 4)
            ev["vector"] = DUP_VECTOR_CHUNK.get(kind, "card_combined")
        if kind == "memory":
            for a, b in _memory_body_twins(store, window):
                ev = pairs.setdefault((min(a, b), max(a, b)), {"kind": kind})
                ev["body_identical"] = True
        else:
            for pair, terms in _lexical_pairs(store, kind, window).items():
                ev = pairs.setdefault(pair, {"kind": kind})
                ev["shared_terms"] = terms

    if not pairs:
        return []
    ids = sorted({i for pair in pairs for i in pair})
    with store.pool.connection() as conn:
        nodes: dict[int, tuple[str, dict[str, Any]]] = {
            int(rid): (str(title or ""), meta or {})
            for rid, title, meta in conn.execute(
                "SELECT ref_id, title, meta FROM refs WHERE ref_id = ANY(%s)",
                (ids,),
            ).fetchall()
        }
    parents = _taxon_parents(
        store, [i for pair, ev in pairs.items() if ev["kind"] == "taxon" for i in pair]
    )
    # The proposal pins both endpoints' shas so the consumer can tell at
    # apply time that the pair the judge saw is the pair it would merge.
    shas = _ref_shas(store, ids)
    out: list[GardenCandidate] = []
    for (a, b), ev in pairs.items():
        ta, ma = nodes.get(a, ("", {}))
        tb, mb = nodes.get(b, ("", {}))
        ev.setdefault("titles", [ta, tb])
        if ev["kind"] == "taxon" and dimension_clash(ma, mb):
            continue  # hard gate — never a candidate
        if ev["kind"] != "memory":
            ev.update(_text_evidence(ta, ma, tb, mb))
        if ev["kind"] == "taxon":
            shared = sorted(parents.get(a, set()) & parents.get(b, set()))
            if shared:
                ev["siblings_under"] = shared
        out.append(
            GardenCandidate(
                detector="dup_pair",
                action="merge",
                target_kind="ref",
                target_id=b,  # convention: higher id = proposed loser
                other_id=a,
                title=f"dup pair {ev['kind']} {a}↔{b}",
                evidence=ev,
                payload={
                    "loser": b,
                    "winner": a,
                    "kind": ev["kind"],
                    "shas": {str(i): shas[i] for i in (a, b) if i in shas},
                },
            )
        )
    out.sort(
        key=lambda c: (
            0 if c.evidence.get("body_identical") else 1,
            c.evidence.get("distance", 9.9),
            c.target_id,
        )
    )
    return out[:limit]


# ---------------------------------------------------------------------------
# paper-identity pass — one work ingested as two paper refs
# ---------------------------------------------------------------------------


def _norm_title_sql(col: str) -> str:
    """SQL for the normalized-title key: lowercase, every run of
    non-alphanumerics collapsed to one space, trimmed."""
    return f"btrim(regexp_replace(lower({col}), '[^[:alnum:]]+', ' ', 'g'))"


def _first_surname(authors: Any) -> str:
    """Lowercased first-author surname from a ``refs.authors`` value
    (``[{"family": …}]``, ``["Last, First"]`` or ``["First Last"]``), or
    ``""``."""
    if not isinstance(authors, list) or not authors:
        return ""
    first = authors[0]
    if isinstance(first, dict):
        name = str(first.get("family") or first.get("last") or first.get("name") or "")
    else:
        name = str(first)
    if "," in name:
        name = name.split(",", 1)[0]
    elif name.split():
        name = name.split()[-1]
    return "".join(ch for ch in name.lower() if ch.isalpha())


def paper_identity_candidates(
    store: Store,
    *,
    limit: int = 50,
    min_title_chars: int = PAPER_MIN_TITLE_CHARS,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Live main-text papers sharing a normalized title — the same work
    ingested twice (gr504843: 197 groups / 431 live papers on prod).

    Each extra member of a group pairs with the group's lowest ref_id
    (the established record, proposed *winner*). Evidence names what
    agrees and what is missing: DOI, publication year, first-author
    surname, PDF hash. Hard gates — never a candidate:

    - both papers carry a DOI and the DOIs differ (a preprint and its
      journal version, or an erratum, are distinct records);
    - both carry a year and the years differ by more than one;
    - both carry a first author and the surnames differ.

    Titles shorter than ``min_title_chars`` after normalization
    ("Editorial", "Introduction") are skipped: equal short titles say
    nothing. Supplements, appendices and front/back matter (``pdf_role``)
    are skipped: they share their parent's title by design.

    Groups are walked by lowest ref_id past ``cursors['paper_identity']``;
    the cursor to save lands in ``next_cursors`` (wrapping at the end).
    Proposals only — merging two papers is a human decision.
    """
    after = (cursors or {}).get("paper_identity", 0)
    norm = _norm_title_sql("r.title")
    with store.pool.connection() as conn:
        groups = conn.execute(
            f"SELECT min(r.ref_id) AS lo, array_agg(r.ref_id ORDER BY r.ref_id) "
            f"FROM refs r "
            f"WHERE r.kind = 'paper' AND r.retired_at IS NULL "
            f"  AND COALESCE(r.pdf_role, 'main') = 'main' "
            f"  AND length({norm}) >= %(min_chars)s "
            f"GROUP BY {norm} HAVING count(*) > 1 AND min(r.ref_id) > %(after)s "
            f"ORDER BY lo LIMIT %(n)s",
            {"min_chars": min_title_chars, "after": after, "n": limit},
        ).fetchall()
        if next_cursors is not None:
            next_cursors["paper_identity"] = (
                int(groups[-1][0]) if len(groups) >= limit else 0
            )
        ids = sorted({int(i) for _lo, members in groups for i in members})
        if not ids:
            return []
        rows = conn.execute(
            "SELECT r.ref_id, r.title, r.year, r.authors, r.pdf_sha256, "
            "  (SELECT array_agg(i.id_value ORDER BY i.id_value) "
            "     FROM ref_identifiers i "
            "    WHERE i.ref_id = r.ref_id AND i.id_kind = 'doi') "
            "FROM refs r WHERE r.ref_id = ANY(%s)",
            (ids,),
        ).fetchall()
    shas = _ref_shas(store, ids)
    info = {
        int(rid): {
            "title": str(title or ""),
            "year": None if year is None else int(year),
            "author": _first_surname(authors),
            "pdf": pdf,
            "dois": sorted({str(d).lower() for d in dois or []}),
        }
        for rid, title, year, authors, pdf, dois in rows
    }
    out: list[GardenCandidate] = []
    for _lo, members in groups:
        winner, *losers = (int(m) for m in members)
        w = info[winner]
        for loser in losers:
            x = info[loser]
            if w["dois"] and x["dois"] and not set(w["dois"]) & set(x["dois"]):
                continue
            if w["year"] and x["year"] and abs(w["year"] - x["year"]) > 1:
                continue
            if w["author"] and x["author"] and w["author"] != x["author"]:
                continue
            ev: dict[str, Any] = {
                "titles": [w["title"], x["title"]],
                "doi": "same"
                if set(w["dois"]) & set(x["dois"])
                else ("missing" if not (w["dois"] and x["dois"]) else "differs"),
                "dois": [w["dois"], x["dois"]],
                "years": [w["year"], x["year"]],
                "first_authors": [w["author"], x["author"]],
                "same_pdf": bool(w["pdf"]) and w["pdf"] == x["pdf"],
            }
            out.append(
                GardenCandidate(
                    detector="paper_identity",
                    action="merge",
                    target_kind="ref",
                    target_id=loser,
                    other_id=winner,
                    title=f"paper identity {winner}↔{loser}: {w['title'][:80]!r}",
                    evidence=ev,
                    payload={
                        "loser": loser,
                        "winner": winner,
                        "kind": "paper",
                        "shas": {str(i): shas[i] for i in (winner, loser) if i in shas},
                    },
                )
            )
    # Strongest evidence first: a shared DOI or PDF, then the rest.
    out.sort(
        key=lambda c: (
            0 if c.evidence["doi"] == "same" or c.evidence["same_pdf"] else 1,
            c.other_id or 0,
            c.target_id,
        )
    )
    return out[:limit]


# ---------------------------------------------------------------------------
# relink pass — orphan taxa (reach no start node)
# ---------------------------------------------------------------------------


def orphan_taxon_candidates(
    store: Store, *, limit: int = 50, min_distance: float = RELINK_MIN_DISTANCE
) -> list[GardenCandidate]:
    """Live taxon refs reaching no start node (``store.taxon_unrooted``),
    narrowed to the orphans a relink would actually add (the 2026-10-21
    ruling): a candidate survives only when the nearest start node sits
    *past* ``min_distance`` by card cosine — inside it the ``+recall``
    rung already lists that start node, so the proposal restates RAG —
    or when the taxon has no card vector at all.

    Each kept candidate names a *candidate parent* for the
    proposer/judge: the nearest start node by card-embedding distance
    when the orphan has an embedded card, else ``None`` (evidence still
    lists the live starts so the judge has the real choices). A store
    with no start nodes yields nothing — there is nothing to relink
    toward.
    """
    unrooted = store.taxon_unrooted()[:limit]
    if not unrooted:
        return []
    starts = _start_node_ids(store)
    if not starts:
        return []
    orphan_vecs = _card_vectors(store, unrooted)
    start_vecs = _card_vectors(store, starts)

    kept: list[tuple[int, int | None, float | None]] = []
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
            if dist <= min_distance:
                continue  # retrieval already surfaces this parent
            parent = best[0]
        kept.append((rid, parent, dist))

    # Pin each endpoint's sha so the relink apply door can tell the pair
    # the judge saw is the pair it would link (gr507641, same convention
    # as the merge detectors).
    shas = _ref_shas(store, [i for rid, parent, _d in kept for i in (rid, parent) if i])
    out: list[GardenCandidate] = []
    for rid, parent, dist in kept:
        ev: dict[str, Any] = {"start_nodes": starts}
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
                payload={
                    "child": rid,
                    "parent": parent,
                    "relation": "specialises",
                    "shas": {
                        str(i): shas[i]
                        for i in (rid, parent)
                        if i is not None and i in shas
                    },
                },
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
    """Link rows where exactly one endpoint is retired — a live node
    pointing at a dead one. (A *missing* endpoint is impossible under the
    FK; retired is the real case — ``retire_ref`` does not cascade links.)

    Not candidates (gr504843 — 20/20 of the first prod tick were these):

    - both endpoints retired: live reads never see the edge, and deleting
      it only loses unretire history;
    - a :data:`PROVENANCE_RELATIONS` edge: it records where something came
      from, which stays true after the source retires;
    - a retired endpoint of a :data:`HISTORY_KINDS` kind: a done todo or
      a retired agentlog is the record of how the live node was made.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.link_id, l.src_ref_id, l.dst_ref_id, l.relation, "
            "  (src.retired_at IS NOT NULL) AS src_retired, "
            "  (dst.retired_at IS NOT NULL) AS dst_retired "
            "FROM links l "
            "JOIN refs src ON src.ref_id = l.src_ref_id "
            "JOIN refs dst ON dst.ref_id = l.dst_ref_id "
            "WHERE (src.retired_at IS NULL) <> (dst.retired_at IS NULL) "
            "  AND l.relation <> ALL(%(prov)s) "
            "  AND NOT (src.retired_at IS NOT NULL AND src.kind = ANY(%(hist)s)) "
            "  AND NOT (dst.retired_at IS NOT NULL AND dst.kind = ANY(%(hist)s)) "
            "ORDER BY l.link_id LIMIT %(n)s",
            {
                "n": limit,
                "prov": sorted(PROVENANCE_RELATIONS),
                "hist": sorted(HISTORY_KINDS),
            },
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


def redundant_link_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    top_k: int = PRUNE_TOP_K,
    max_distance: float = PRUNE_MAX_DISTANCE,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
    probe_budget_s: float = PRUNE_PROBE_BUDGET_S,
) -> list[GardenCandidate]:
    """Machine ``related-to`` edges that only restate the index.

    A link qualifies when ``relation='related-to'`` and
    ``links.trust_origin`` is ``generated`` or ``computed`` (human- and
    legacy-authored edges are never candidates), both endpoints carry a
    default-embedder ``card_combined`` vector, the pair are *mutual*
    top-``top_k`` neighbours in the card index, and their cosine
    distance is at or below ``max_distance``. Inside the recall
    floor the fisheye already lists the endpoints for each other, so the
    stored edge adds nothing — it only makes the render noisier.

    Scans the next ``scan_cap`` machine ``related-to`` rows by
    ``link_id`` past the saved cursor (``cursors['redundant_link']``;
    the cursor to save lands in ``next_cursors`` and wraps at the end,
    so successive runs cover every link) and emits at most ``limit``
    candidates. Proposal rows only — apply stays merge-only, so a
    ``prune`` proposal is never carried out by code; it queues for a
    human.
    """
    after = (cursors or {}).get("redundant_link", 0)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT link_id, src_ref_id, dst_ref_id FROM links "
            "WHERE relation = 'related-to' "
            "  AND trust_origin IN ('generated', 'computed') "
            "  AND link_id > %(after)s "
            "ORDER BY link_id LIMIT %(cap)s",
            {"cap": scan_cap, "after": after},
        ).fetchall()
    if next_cursors is not None:
        next_cursors["redundant_link"] = (
            int(rows[-1][0]) if len(rows) >= scan_cap else 0
        )
    if not rows:
        return []
    vecs = _card_vectors(
        store, sorted({int(r[1]) for r in rows} | {int(r[2]) for r in rows})
    )
    # The distance bound is the cheap leg — filter per link first so the
    # ANN neighbour lookups run only for endpoints that could still
    # qualify.
    near: list[tuple[int, int, int, float]] = []
    for link_id, a, b in rows:
        va, vb = vecs.get(int(a)), vecs.get(int(b))
        if va is None or vb is None:
            continue
        dist = _cosine(va, vb)
        if dist <= max_distance:
            near.append((int(link_id), int(a), int(b), dist))
    if not near:
        return []
    # Closest pairs first, probed in batches: stop once ``limit``
    # candidates are found or the probe budget is spent, and return what
    # was found rather than being abandoned at the detector deadline.
    near.sort(key=lambda t: (t[3], t[0]))
    started = time.monotonic()
    neighbours: dict[int, frozenset[int]] = {}
    out: list[GardenCandidate] = []
    for i, (link_id, a, b, d) in enumerate(near):
        if len(out) >= limit:
            break
        if a not in neighbours or b not in neighbours:
            if time.monotonic() - started > probe_budget_s:
                log.info(
                    "garden: redundant_link probe budget spent after %d of %d pairs",
                    i,
                    len(near),
                )
                break
            batch = {
                e
                for _, x, y, _ in near[i : i + PRUNE_PROBE_BATCH]
                for e in (x, y)
                if e not in neighbours
            }
            neighbours.update(_top_k_card_neighbours(store, batch, vecs, k=top_k))
        if b not in neighbours.get(a, ()) or a not in neighbours.get(b, ()):
            continue
        out.append(
            GardenCandidate(
                detector="redundant_link",
                action="prune",
                target_kind="link",
                target_id=link_id,
                title=(
                    f"machine related-to {link_id} {a}↔{b} duplicates the "
                    f"index (mutual top-{top_k}, dist {d:.3f})"
                ),
                evidence={
                    "a": a,
                    "b": b,
                    "distance": round(d, 4),
                    "mutual_top_k": top_k,
                },
                payload={
                    "link_id": link_id,
                    "a": a,
                    "b": b,
                    "distance": round(d, 4),
                },
            )
        )
    out.sort(key=lambda c: (float(c.payload["distance"]), c.target_id))
    return out[:limit]


# ---------------------------------------------------------------------------
# tag-sweep pass — under-tagged refs on an in-use namespace
# ---------------------------------------------------------------------------


def untagged_candidates(
    store: Store,
    *,
    namespaces: tuple[str, ...] = ("DOMAIN", "TOPIC"),
    kinds: tuple[str, ...] = ("finding", "paper", "concept", "memory"),
    min_seeded: int = TAG_SWEEP_MIN_SEEDED,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Live refs of ``kinds`` carrying no tag in ``namespaces`` while at
    least ``min_seeded`` same-kind live refs do — the namespace is in use
    for the kind and this ref was skipped. The proposed *value* is the
    proposer's job; the candidate only carries the gap.

    Bounded like the other wide scans: per kind, the next ``scan_cap``
    live refs past the saved cursor (``cursors['untagged:<kind>']``) are
    inspected; the cursor to save lands in ``next_cursors`` and wraps at
    the kind's end, so successive runs walk the whole kind (gr504843).
    The per-kind seeded counts are computed ONCE in the ``seeded`` CTE —
    the original shape ran one ``ref_tags``×``tags``×``refs`` count per
    scanned row, an O(refs × ref_tags) nest that hit the statement
    timeout on the first prod run before any candidate was emitted
    (gr503580)."""
    cursors = cursors or {}
    windows: list[int] = []
    for kind in kinds:
        ckey = f"untagged:{kind}"
        window, nxt = _scan_window(
            store, kind, after=cursors.get(ckey, 0), size=scan_cap
        )
        if next_cursors is not None:
            next_cursors[ckey] = nxt
        windows += window
    if not windows:
        return []
    out: list[GardenCandidate] = []
    for ns in namespaces:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "WITH seeded AS ("
                "  SELECT r2.kind, count(*) AS n FROM ref_tags rt "
                "   JOIN tags t ON t.tag_id = rt.tag_id "
                "   JOIN refs r2 ON r2.ref_id = rt.ref_id "
                "  WHERE t.namespace = %(ns)s AND r2.retired_at IS NULL "
                "    AND r2.kind = ANY(%(kinds)s) GROUP BY r2.kind) "
                "SELECT s.ref_id, s.kind, s.title FROM refs s "
                "JOIN seeded d ON d.kind = s.kind AND d.n >= %(min_seed)s "
                "WHERE s.ref_id = ANY(%(win)s) AND NOT EXISTS ("
                "  SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
                "   WHERE rt.ref_id = s.ref_id AND t.namespace = %(ns)s) "
                "ORDER BY s.ref_id LIMIT %(n)s",
                {
                    "kinds": list(kinds),
                    "ns": ns,
                    "min_seed": min_seeded,
                    "win": windows,
                    "n": limit,
                },
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


@dataclass(slots=True)
class DetectorRun:
    """One :func:`run_detectors` pass: the concatenated candidates plus
    ``skipped`` — the names of detectors that hit their statement timeout
    or wall-clock budget and were skipped so the rest could still run —
    and ``cursors``, the scan positions to save for the next run (only
    from detectors that finished)."""

    candidates: list[GardenCandidate] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)


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
    detector_deadline_s: float = DETECTOR_DEADLINE_S,
    cursors: dict[str, int] | None = None,
) -> DetectorRun:
    """Run the detectors selected by ``passes`` and concatenate their
    candidates (each detector already bounded by ``limit``).

    ``cursors`` are the scan positions the last run saved
    (``dup_pair:<kind>``, ``untagged:<kind>``, ``redundant_link``); the
    windowed detectors start past them and report where the next run
    should start in ``DetectorRun.cursors``. A skipped detector reports
    nothing, so its window is retried.

    Each detector runs under ``detector_deadline_s`` wall-clock via
    :func:`call_with_deadline` (``<= 0`` disables) — an over-budget
    detector is abandoned on its daemon thread and its queries still die
    at the role's ``statement_timeout``. A detector cancelled by Postgres
    (``QueryCanceled``) or abandoned past its own budget is logged with
    its name and elapsed seconds, lands in ``skipped``, and the pass
    continues with the remaining detectors — one slow detector must not
    zero the tick (gr503580).
    """
    out = DetectorRun()
    saved = dict(cursors or {})
    for det in detectors_for(passes):
        fn: Callable[[], list[GardenCandidate]] | None = None
        # Each detector writes its own dict: an abandoned (timed-out)
        # detector's late writes land in a dict nobody reads.
        nxt: dict[str, int] = {}
        if det == "dup_pair":
            fn = partial(
                duplicate_candidates,
                store,
                kinds=kinds,
                limit=limit,
                scan_cap=scan_cap,
                cursors=saved,
                next_cursors=nxt,
            )
        elif det == "orphan":
            fn = partial(orphan_taxon_candidates, store, limit=limit)
        elif det == "stale":
            fn = partial(
                stale_candidates, store, kinds=kinds, stale_days=stale_days, limit=limit
            )
        elif det == "dangling_edge":
            fn = partial(dangling_edge_candidates, store, limit=limit)
        elif det == "cycle_edge":
            fn = partial(cycle_edge_candidates, store, limit=limit)
        elif det == "redundant_link":
            fn = partial(
                redundant_link_candidates,
                store,
                scan_cap=scan_cap,
                limit=limit,
                cursors=saved,
                next_cursors=nxt,
            )
        elif det == "untagged":
            fn = partial(
                untagged_candidates,
                store,
                namespaces=tag_namespaces,
                kinds=tag_kinds,
                scan_cap=scan_cap,
                limit=limit,
                cursors=saved,
                next_cursors=nxt,
            )
        elif det == "paper_identity":
            fn = partial(
                paper_identity_candidates,
                store,
                limit=limit,
                cursors=saved,
                next_cursors=nxt,
            )
        elif det == "polysemy":
            fn = partial(polysemy_candidates, store, limit=limit)
        elif det == "sibling_confusion":
            fn = partial(
                sibling_confusion_candidates,
                store,
                exclusive_axes=exclusive_axes,
                limit=limit,
            )
        if fn is None:
            continue
        started = time.monotonic()
        try:
            found = (
                call_with_deadline(
                    fn, deadline_s=detector_deadline_s, label=f"garden:{det}"
                )
                if detector_deadline_s > 0
                else fn()
            )
        except (QueryCanceled, DeadlineExceeded) as exc:
            log.warning(
                "garden: detector %s skipped after %.1fs — %s",
                det,
                time.monotonic() - started,
                exc,
            )
            out.skipped.append(det)
            continue
        out.candidates += found
        out.cursors.update(nxt)
    return out


__all__ = [
    "DETECTOR_DEADLINE_S",
    "DUP_MAX_DISTANCE",
    "DUP_MIN_LEXICAL",
    "DUP_VECTOR_CHUNK",
    "GARDEN_KINDS",
    "HISTORY_KINDS",
    "PAPER_MIN_TITLE_CHARS",
    "PASS_CHOICES",
    "PROVENANCE_RELATIONS",
    "PRUNE_MAX_DISTANCE",
    "PRUNE_TOP_K",
    "RELINK_MIN_DISTANCE",
    "SHORT_SIBLING_MAX_WORDS",
    "SIBLING_REJECT_REASON",
    "STALE_DAYS",
    "DetectorRun",
    "GardenCandidate",
    "cycle_edge_candidates",
    "dangling_edge_candidates",
    "detectors_for",
    "dup_pair_qualifies",
    "dup_window_size",
    "duplicate_candidates",
    "orphan_taxon_candidates",
    "paper_identity_candidates",
    "polysemy_candidates",
    "redundant_link_candidates",
    "run_detectors",
    "short_sibling_pair",
    "sibling_confusion_candidates",
    "stale_candidates",
    "untagged_candidates",
]
