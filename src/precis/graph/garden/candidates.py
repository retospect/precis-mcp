"""Deterministic candidate detectors for the graph garden.

Every function here is read-only: SQL over the live graph plus cheap
Python scoring, returning :class:`GardenCandidate` rows the proposer turns
into review-ledger proposals (or drops). No LLM calls, no writes — a
candidate is evidence, never a verdict.

The opt-in ``memory-review`` pass adds review debt, title shape and undated
absence signals. Expiry defaults are computed on read, using updated_at
plus the title/section shape's lifetime; put-side metadata and acknowledgements
are outside this pass. Incident suggestions retain only a verified dated
cause, fix and open uncertainty. No expiry authorizes retirement.

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

``restated_claim`` compares bounded sentence windows within one memory hub,
stopping at nested hubs so a shared root cannot make unrelated hubs peers.
Sentence vectors are ephemeral default-embedder reads, never stored embeddings.
Both semantic and lexical legs require a working embedder; outages are reported
as ``not_checked`` rather than silently weakening the evidence to lexical only.

The opt-in memory-review pass compares sources against recorded pre-touch
content. Git supplies file-backed section/symbol/skill snapshots; stored
entities need revision or STATUS-event evidence. Missing history is unknown,
not a stale claim. These signals have no apply-time re-check: their proposals
stay pending outside consumer review, and direct memory apply refuses them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import islice
from typing import TYPE_CHECKING, Any

import numpy as np
from psycopg.errors import QueryCanceled

from precis.graph.garden._anchors import AnchorResolver, ProdAncestry
from precis.graph.garden._drift_refs import ref_change
from precis.graph.garden._drift_repo import RepoDriftResolver
from precis.reading.concepts import normalize_name
from precis.store._mappers import _coerce_vector
from precis.taxonomy.nodes import dimension_clash
from precis.todo import TERMINAL_TODO_STATUSES
from precis.utils.deadline import DeadlineExceeded, call_with_deadline, deadline_at
from precis.utils.handle_registry import parse as parse_handle
from precis.utils.handle_registry import try_format
from precis.utils.lexical import content_words, lexical_overlap
from precis.utils.memory_threads import INDEX_TAG, THREADS_TAG, first_left, left_is_done

if TYPE_CHECKING:
    from psycopg import Connection

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

#: A node considered-and-unchanged within this many days is left out of
#: candidate sets — a fresh review stamp supersedes re-proposal (the
#: ``kg-review:<model>:<date>:<verdict>`` stamp on the trust ledger;
#: ``0`` disables the skip).
CONSIDERED_DAYS = 30
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
    "memory": (
        "dangling_mention",
        "thread_shape",
        "anchor_rot",
        "compact",
        "grab_bag",
        "griped",
    ),
    "memory-review": (
        "entity_claim_stale",
        "negative_undated",
        "restated_claim",
        "review_due",
        "source_drift",
        "title_shape",
    ),
    # One-off host pass (gr560767): the census sync is the operator's
    # hand step, then this pass rewrites host prose to dated events.
    "host-facts": ("host_facts",),
}

#: Passes ``all`` skips. Memory detectors spend proposer calls, so a job
#: must name their pass explicitly; memory-review is independent of memory;
#: host-facts is a one-off hand-run pass over the machine census.
OPT_IN_PASSES: frozenset[str] = frozenset({"memory", "memory-review", "host-facts"})

#: Job ``params.pass`` values → detectors. ``all`` runs everything not opt-in.
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
    action: str  # merge | split | relink | prune | tag | rewrite | supersede | retire
    target_kind: str  # 'ref' | 'link' (reviews.target_kind)
    target_id: int
    other_id: int | None = None
    title: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        key = f"{self.action}:{self.target_kind}:{self.target_id}:{self.other_id or 0}"
        if self.detector == "anchor_rot":
            # A mention rewrite is a different question on the same body;
            # repo movement can also change an anchor verdict without an edit.
            return f"{key}:anchor_rot:{self.payload.get('main_sha') or 'unavailable'}"
        if self.detector in {"review_due", "title_shape", "negative_undated"}:
            # Independent questions about the same unchanged body must not
            # suppress one another's proposals or rejections.
            if self.detector == "review_due":
                # No-change today must not suppress later expiry or another
                # review cycle on content that has not been edited.
                generation = (
                    self.evidence.get("reason"),
                    self.evidence.get("reviewed_at"),
                    self.evidence.get("expires"),
                    self.evidence.get("review_by"),
                    self.evidence.get("review_state"),
                )
                return f"{key}:review_due:" + ":".join(
                    str(v or "none") for v in generation
                )
            return f"{key}:{self.detector}"
        if self.detector in {"source_drift", "entity_claim_stale"}:
            return f"{key}:{self.detector}:{self.payload['evidence_sha']}"
        if self.detector == "griped":
            # The question a reader gripe asks re-opens whenever its story,
            # route or named contents move; distinct complaints on one
            # memory are distinct keys (evidence_sha covers both).
            return f"{key}:griped:{self.payload.get('evidence_sha') or 'none'}"
        if self.detector == "host_facts":
            # A census fact changing (the sync, a hand put) is new evidence
            # on the same body — a distinct proposal question.
            return f"{key}:host_facts:{self.payload.get('evidence_sha') or 'none'}"
        return key


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


def freshly_considered(
    store: Store,
    ref_ids: list[int],
    link_ids: list[int],
    *,
    days: int = CONSIDERED_DAYS,
) -> set[tuple[str, int]]:
    """``{(target_kind, target_id)}`` of the given refs/links that were
    considered within ``days`` and have not changed since — a ``consider``
    act in the window whose ``expected_sha`` equals the target's content
    sha now, while the stamp still reads ``considered`` (a demote or a
    content edit re-opens the node).

    The review's date is the stamp's ``payload.kg_review.date`` when the
    act carries one — a reviewer may stamp a review older than the act's
    recording time — else the revision's ``at``.
    """
    if not ref_ids and not link_ids:
        return set()
    fresh = (
        "coalesce((a->'payload'->'kg_review'->>'date')::date, "
        "(r.at AT TIME ZONE 'UTC')::date) "
        ">= ((now() - make_interval(days => %(days)s)) "
        "     AT TIME ZONE 'UTC')::date"
    )
    out: set[tuple[str, int]] = set()
    with store.pool.connection() as conn:
        for kind, table, pk, ids in (
            ("ref", "refs", "ref_id", ref_ids),
            ("link", "links", "link_id", link_ids),
        ):
            if not ids:
                continue
            rows = conn.execute(
                f"SELECT DISTINCT r.target_id FROM revisions r "
                f"JOIN {table} t ON t.{pk} = r.target_id "
                f"CROSS JOIN LATERAL jsonb_array_elements(r.trust_acts) a "
                f"WHERE r.target_kind = '{kind}' AND r.target_id = ANY(%(ids)s) "
                f"  AND t.trust_confidence = 'considered' "
                f"  AND a->>'action' = 'consider' "
                f"  AND a->>'expected_sha' = precis_target_sha('{kind}', r.target_id) "
                f"  AND {fresh}",
                {"ids": list(ids), "days": days},
            ).fetchall()
            out.update((kind, int(r[0])) for r in rows)
    return out


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
                "AND (kind <> 'agentlog' OR meta->>'trace' IS DISTINCT FROM 'search') "
                "AND ref_id > %s ORDER BY ref_id LIMIT %s",
                (kind, after, size),
            ).fetchall()
        ]
    return ids, (ids[-1] if len(ids) >= size else 0)


_MEMORY_HANDLE = re.compile(r"\b[a-zA-Z]{2}[0-9]{1,19}\b")


def memory_handle_mentions(body: str, handle: str) -> list[re.Match[str]]:
    """The dangling-mention token predicate, narrowed to one record handle."""
    wanted = parse_handle(handle)
    return [
        m for m in _MEMORY_HANDLE.finditer(body) if parse_handle(m.group()) == wanted
    ]


#: Bound redirect reads and per-memory prompt evidence independently of scan_cap.
SUCCESSOR_MAX_HOPS = 16
MENTION_MATCH_CAP = 20
ANCHOR_MATCH_CAP = 20
#: Commit tokens read off one Left line before the ancestry cap gives up —
#: a single sha already on origin/prod makes the whole line stale work.
THREAD_COMMIT_CHECK_CAP = 5

# Explicit code/document syntax limits prose false positives. Dotted names
# must name a package in the pinned tree, even inside code spans; arbitrary
# domains and metadata fields are not Python symbols. Alternatives consume
# entire anchors so a hash in a filename or dotted suffix is not checked twice.
_ANCHOR_PATH = r"(?:[\w.-]+/)*[\w.-]+"
_ANCHOR_QUAL = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
_MEMORY_ANCHOR = re.compile(
    rf"(?P<python_path>{_ANCHOR_PATH}\.py)::(?P<symbol>{_ANCHOR_QUAL})"
    rf"|(?P<doc>{_ANCHOR_PATH}\.(?:md|markdown))"
    r"(?:`?[ \t]*§[ \t]*`?(?P<section>[^`\n;]+)"
    r"|(?P<fragment>\#[\w-]+))"
    rf"|(?<![\w/])(?P<path>(?:docs|scripts)/{_ANCHOR_PATH})"
    rf"|`(?![^`\n]+\.(?:md|markdown|py|js|json|yaml|toml|txt)`)"
    rf"(?:main::)?(?P<dotted>{_ANCHOR_QUAL}\.[A-Za-z_]\w*)`"
    rf"|(?<![\w./`:])(?:main::)?(?P<project>{_ANCHOR_QUAL}\.[A-Za-z_]\w*)"
    r"(?![\w`]|\.[A-Za-z_])"
    r"|\b(?P<commit>(?=[a-fA-F0-9]*[a-fA-F])(?=[a-fA-F0-9]*[0-9])"
    r"[a-fA-F0-9]{7,40})\b"
)


def _anchor_lookup(match: re.Match[str]) -> tuple[str, str, str | None]:
    """Translate a prose match to one resolver lookup, without guessing repairs."""
    if path := match.group("python_path"):
        return "python", path, match.group("symbol")
    if doc := match.group("doc"):
        return (
            "section",
            doc,
            (match.group("section") or match.group("fragment")).strip(),
        )
    if path := match.group("path"):
        return "path", path.rstrip("."), None
    if dotted := match.group("dotted") or match.group("project"):
        return "python", dotted, None
    return "commit", match.group("commit"), None


def _memory_anchor_matches(
    body: str, python_packages: set[str]
) -> Iterator[re.Match[str]]:
    """Scan inside section prose too: a title must not hide a later anchor."""
    pos = 0
    while match := _MEMORY_ANCHOR.search(body, pos):
        dotted = match.group("dotted") or match.group("project")
        if not dotted or dotted.partition(".")[0] in python_packages:
            yield match
        pos = match.start("section") if match.group("section") else match.end()


def anchor_rot_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Report missing repo anchors, pinned to memory content and local main.

    The dispatcher supplies the detector deadline. Each body gets at most
    ``ANCHOR_MATCH_CAP`` lookups, including successful/unavailable ones, so
    long bodies cannot multiply git/index work without bound. Git blobs feed
    the existing indexes: a dirty checkout cannot masquerade as the main SHA.
    Unavailable classes go into ``not_checked`` alongside verified failures;
    a body with only unavailable anchors emits no rot proposal. A missing
    anchor is evidence only: the evaluator decides rename/removal/history.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "anchor_rot:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    resolver: AnchorResolver | None = None
    out: list[GardenCandidate] = []
    for rid, body, sha in _memory_bodies(store, window):
        if not _MEMORY_ANCHOR.search(body):
            continue
        if resolver is None:
            resolver = AnchorResolver()
        matches = list(
            islice(
                _memory_anchor_matches(body, resolver.python_packages),
                ANCHOR_MATCH_CAP + 1,
            )
        )
        if not matches:
            continue
        anchors: list[dict[str, Any]] = []
        unchecked: list[dict[str, Any]] = []
        ops: list[dict[str, Any]] = []
        for match in matches[:ANCHOR_MATCH_CAP]:
            kind, target, detail = _anchor_lookup(match)
            exists, tried = resolver.check(kind, target, detail)
            if exists is True:
                continue
            info = {
                "anchor": match.group(),
                "kind": kind,
                "span": [match.start(), match.end()],
                "context": body[max(0, match.start() - 300) : match.end() + 300],
                "tried": tried,
            }
            if exists is None:
                unchecked.append(info)
                continue
            anchors.append(info)
            ops.append({"find": match.group(), "text": None, "span": info["span"]})
        if anchors:
            out.append(
                GardenCandidate(
                    detector="anchor_rot",
                    action="rewrite",
                    target_kind="ref",
                    target_id=rid,
                    title=f"me{rid} names missing repo anchors",
                    evidence={
                        "anchors": anchors,
                        "content_sha": sha,
                        "main_sha": resolver.main_sha,
                    },
                    payload={
                        "base_sha": sha,
                        "main_sha": resolver.main_sha,
                        "ops": ops,
                        "not_checked": unchecked,
                        "matches_capped": len(matches) > ANCHOR_MATCH_CAP,
                        "match_cap": ANCHOR_MATCH_CAP,
                    },
                )
            )
        if len(out) >= limit:
            nxt = rid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


def _memory_bodies(store: Store, ids: list[int]) -> list[tuple[int, str, str]]:
    """Live body text and target SHA in one snapshot, excluding DREAMs.

    Joining chunks in body order makes spans independent of chunk boundaries.
    DREAMs remain in the duplicate/merge lane only.
    """
    if not ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, coalesce((SELECT string_agg(c.text, E'\\n' "
            "ORDER BY c.ord, c.chunk_id) FROM chunks c WHERE c.ref_id = r.ref_id "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL), ''), precis_target_sha('ref', r.ref_id) "
            "FROM refs r WHERE r.ref_id = ANY(%s) AND r.kind = 'memory' "
            "AND r.retired_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'DREAM') "
            "ORDER BY r.ref_id",
            (ids,),
        ).fetchall()
    return [(int(rid), str(body), str(sha)) for rid, body, sha in rows]


def _live_meta_successor(
    store: Store, conn: Connection[Any], ref_id: int
) -> str | None:
    """Reuse tombstone reads' conversion-aware resolver, excluding chunk targets."""
    # The shared resolver counts the origin too; retain 16 successor hops.
    address = store.follow_supersede_address(
        ref_id, conn=conn, _cap=SUCCESSOR_MAX_HOPS + 1
    )
    if address is None:
        return None
    if address.startswith("md:") and "~" in address:
        return address
    if address.startswith("python:") and ("::" in address or "/" in address):
        return address
    parsed = parse_handle(address)
    return address if parsed is not None and not parsed[1] else None


def _retired_mentions(store: Store, tokens: set[str]) -> dict[str, dict[str, Any]]:
    """Join decoded record/chunk handles to retired refs, never mention edges.

    A chunk handle (pcN, etc.) joins through its owning ref. A successor
    replaces only a record handle: moving to a new paper does not identify
    the corresponding chunk in that paper.
    """
    decoded = {
        token: parsed
        for token in sorted(tokens)
        if (parsed := parse_handle(token)) is not None
        and parsed[2] <= 9223372036854775807
    }
    if not decoded:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "WITH mentioned AS ("
            "SELECT r.ref_id, r.kind, false AS is_chunk, r.ref_id AS pk "
            "FROM refs r WHERE r.ref_id = ANY(%s) UNION ALL "
            "SELECT r.ref_id, r.kind, true, c.chunk_id FROM chunks c "
            "JOIN refs r USING (ref_id) WHERE c.chunk_id = ANY(%s)) "
            "SELECT m.kind, m.is_chunk, m.pk, r.ref_id, r.retired_at, "
            "r.meta->>'superseded_by', s.ref_id, s.kind, s.title, r.title, "
            "r.meta->'garden_merge'->>'winner' "
            "FROM mentioned m JOIN refs r USING (ref_id) "
            "LEFT JOIN LATERAL (SELECT live.ref_id, live.kind, live.title "
            "FROM links l "
            "JOIN refs live ON live.ref_id = l.src_ref_id "
            "WHERE l.dst_ref_id = r.ref_id AND l.relation = 'supersedes' "
            "AND live.retired_at IS NULL ORDER BY live.ref_id LIMIT 1) s ON true "
            "WHERE r.retired_at IS NOT NULL",
            (
                [pk for _, chunk, pk in decoded.values() if not chunk],
                [pk for _, chunk, pk in decoded.values() if chunk],
            ),
        ).fetchall()
        # Resolve each distinct tombstone once; never suggest a retired ref.
        successors = {
            rid: _live_meta_successor(store, conn, rid)
            for rid in {int(row[3]) for row in rows if row[5]}
        }
    by_address = {}
    for kind, chunk, pk, rid, retired, raw, sid, skind, stitle, rtitle, winner in rows:
        successor = successors.get(int(rid))
        if successor is None and sid is not None:
            successor = try_format(str(skind), int(sid))
        duplicate = _duplicate_successor(
            successor, merge_winner=winner, sid=sid, rtitle=rtitle, stitle=stitle
        )
        by_address[(kind, bool(chunk), int(pk))] = {
            "retired_ref": try_format(str(kind), int(rid)),
            "retired_at": retired.isoformat(),
            "successor": successor,
            "chunk": bool(chunk),
            "duplicate_successor": duplicate,
        }
    return {
        token: by_address[address]
        for token, address in decoded.items()
        if address in by_address
    }


def _duplicate_successor(
    successor: str | None,
    *,
    merge_winner: Any,
    sid: Any,
    rtitle: Any,
    stitle: Any,
) -> str | None:
    """Whether a mention's resolved successor is a duplicate of the retired
    ref — the one fate whose handle swap needs no model judgment (gr564554):

    - ``"merge"``: the tombstone's ``meta.garden_merge`` names the merge
      winner and the resolved successor *is* that winner, so the merge
      folded the node into the successor and the handle swap cannot change
      the referent. A redirect chain that walked past the winner — the
      winner itself later retired, superseded into different content —
      breaks that identity: the verdict is ``None`` and the mention stays
      with the evaluator (td627131).
    - ``"same_title"``: the resolved successor is exactly the live ref whose
      inbound ``supersedes`` edge claims the replacement, and both titles
      normalise equal — the restated claim is the same node. An
      agent-written supersede to *different* content returns ``None`` and
      stays with the evaluator.
    """
    if successor is None:
        return None
    parsed = parse_handle(successor)
    if merge_winner is not None:
        try:
            winner_id = int(merge_winner)
        except (TypeError, ValueError):
            return None
        if parsed is not None and not parsed[1] and parsed[2] == winner_id:
            return "merge"
        return None
    retired_title = normalize_name(str(rtitle or ""))
    if (
        sid is not None
        and parsed is not None
        and not parsed[1]
        and int(parsed[2]) == int(sid)
        and retired_title
        and retired_title == normalize_name(str(stitle or ""))
    ):
        return "same_title"
    return None


def dangling_mention_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Propose anchored rewrites of retired handles in live memory bodies.

    One proposal per memory carries at most ``MENTION_MATCH_CAP`` matching
    spans (zero-based, end exclusive), with ``matches_capped`` in its payload
    when more remain. Missing successors leave ``text=None`` for the evaluator;
    this is not a deletion instruction. Even a supplied successor needs
    semantic review — except a verified ``duplicate_successor`` mention
    (``"merge"``/``"same_title"``; see :func:`_duplicate_successor`), whose
    bare handle swap the consumer applies with no model call (gr564554).
    No write/apply path is invoked here. The dispatcher
    supplies the same deadline as every other wide scan.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "dangling_mention:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    bodies = _memory_bodies(store, window)
    matches = {rid: list(_MEMORY_HANDLE.finditer(body)) for rid, body, _ in bodies}
    retired = _retired_mentions(
        store, {m.group() for spans in matches.values() for m in spans}
    )
    out: list[GardenCandidate] = []
    for rid, body, sha in bodies:
        evidence = []
        ops: list[dict[str, Any]] = []
        capped = False
        for match in matches[rid]:
            info = retired.get(match.group())
            if info is None:
                continue
            if len(ops) >= MENTION_MATCH_CAP:
                capped = True
                break
            span = [match.start(), match.end()]
            evidence.append(
                {
                    "handle": match.group(),
                    "span": span,
                    "context": body[max(0, match.start() - 300) : match.end() + 300],
                    **info,
                }
            )
            ops.append(
                {
                    "find": match.group(),
                    "text": None if info["chunk"] else info["successor"],
                    "span": span,
                    "successor": info["successor"],
                }
            )
        if ops:
            out.append(
                GardenCandidate(
                    detector="dangling_mention",
                    action="rewrite",
                    target_kind="ref",
                    target_id=rid,
                    title=f"me{rid} mentions retired handles",
                    evidence={
                        "mentions": evidence,
                        "content_sha": sha,
                        "supporting_shas": _memory_supporting_shas(
                            store,
                            [
                                info["successor"]
                                for info in evidence
                                if info["successor"]
                            ],
                        ),
                    },
                    payload={
                        "base_sha": sha,
                        "ops": ops,
                        "matches_capped": capped,
                        "match_cap": MENTION_MATCH_CAP,
                    },
                )
            )
        if len(out) >= limit:
            # Never skip later candidates merely because the output filled.
            nxt = rid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


def _memory_supporting_shas(store: Store, handles: list[str]) -> dict[str, str]:
    """Pin supporting content before review, including an explicit empty map.

    Apply compares fresh detector evidence under dependency locks. A missing
    map on an older proposal therefore fails closed instead of claiming that
    its reviewer saw the supporting content that exists now.
    """
    ids = {
        handle: parsed[2]
        for handle in handles
        if (parsed := parse_handle(handle)) is not None and not parsed[1]
    }
    shas = _ref_shas(store, list(ids.values()))
    return {handle: shas[rid] for handle, rid in ids.items() if rid in shas}


def _thread_bodies(store: Store, ids: list[int]) -> list[tuple[int, str, str]]:
    """Thread-tagged bodies, including children; section hubs stay protected."""
    if not ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN' AND t.value = %s "
            "AND NOT EXISTS (SELECT 1 FROM ref_tags ix JOIN tags it USING (tag_id) "
            "WHERE ix.ref_id = rt.ref_id AND it.namespace = 'OPEN' AND it.value = %s)",
            (ids, THREADS_TAG, INDEX_TAG),
        ).fetchall()
    return _memory_bodies(store, [int(row[0]) for row in rows])


def _terminal_thread_work(store: Store, lefts: list[str]) -> dict[str, str]:
    """Terminal todo/gripe handles in Left lines, with kind-checked ids."""
    ids = set()
    for left in lefts:
        for match in _MEMORY_HANDLE.finditer(left):
            parsed = parse_handle(match.group())
            if (
                parsed is not None
                and parsed[0] in {"todo", "gripe"}
                and not parsed[1]
                and parsed[2] <= 9223372036854775807
            ):
                ids.add(parsed[2])
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.kind, t.value FROM refs r "
            "JOIN ref_tags rt USING (ref_id) JOIN tags t USING (tag_id) "
            "WHERE r.ref_id = ANY(%s) AND t.namespace = 'STATUS' "
            "AND ((r.kind = 'todo' AND t.value = ANY(%s)) "
            "OR (r.kind = 'gripe' AND t.value IN ('done', 'wontfix'))) "
            "ORDER BY r.ref_id, t.value",
            (sorted(ids), sorted(TERMINAL_TODO_STATUSES)),
        ).fetchall()
    return {str(try_format(kind, int(rid))): str(status) for rid, kind, status in rows}


def _closed_left_handles(left: str, terminals: dict[str, str]) -> dict[str, str]:
    """Only handles actually named on this node's Left line."""
    return {
        token: terminals[token]
        for match in _MEMORY_HANDLE.finditer(left)
        if (token := match.group().lower()) in terminals
    }


def _left_commit_tokens(left: str) -> list[str]:
    """Commit-shaped tokens on a Left line, first occurrence order.

    Reuses the anchor grammar so handle-shaped words (``me123``) and pure
    digit ids never masquerade as shas.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _MEMORY_ANCHOR.finditer(left):
        token = match.group("commit")
        if token and token.lower() not in seen:
            seen.add(token.lower())
            out.append(token)
    return out


def _current_thread_ancestor(
    store: Store,
    ref_id: int,
    *,
    left_is_stale: Callable[[str], bool] | None = None,
) -> tuple[int, str, str] | None:
    """Nearest live part-of ancestor with a nonempty, unfinished Left line.

    Breadth-first traversal crosses untagged intermediate nodes and skips
    malformed/closed thread ancestors; ``left_is_stale`` additionally
    excludes ancestors whose Left names landed work (a sha on
    ``origin/prod``). A visited set handles cycles; equal distance ties use
    ref id for deterministic proposals. Deadline ownership stays with
    run_detectors, including every query in this traversal.
    """
    seen = {ref_id}
    frontier = [ref_id]
    while frontier:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT p.ref_id FROM links l JOIN refs p "
                "ON p.ref_id = l.dst_ref_id WHERE l.src_ref_id = ANY(%s) "
                "AND l.relation = 'part-of' AND p.retired_at IS NULL "
                "ORDER BY p.ref_id",
                (frontier,),
            ).fetchall()
        frontier = [int(row[0]) for row in rows if int(row[0]) not in seen]
        seen.update(frontier)
        bodies = _thread_bodies(store, frontier)
        lefts = {rid: first_left(body) for rid, body, _ in bodies}
        terminals = _terminal_thread_work(
            store, [left for left in lefts.values() if left]
        )
        for rid, _body, sha in bodies:
            left = lefts[rid]
            if (
                left
                and not left_is_done(left)
                and not _closed_left_handles(left, terminals)
                and not (left_is_stale is not None and left_is_stale(left))
            ):
                return rid, left, sha
    return None


def thread_shape_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Propose superseding malformed/stale thread snapshots, else retirement.

    Left grammar is shared with the daily sweep. Terminal todo/gripe STATUS
    is DB evidence. A commit sha on a Left line names landed work when
    :class:`ProdAncestry` confirms it as an ancestor of ``origin/prod`` in
    the configured main checkout (a sha on main alone is still live work).
    When the main root or the prod ref is unavailable every sha verdict
    stays unknown: such a node is not flagged for that reason and the
    evidence keeps ``sha_on_prod: not_checked``. Preservation is a review
    requirement in the payload, not an edit here.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "thread_shape:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    bodies = _thread_bodies(store, window)
    lefts = {rid: first_left(body) for rid, body, _ in bodies}
    terminals = _terminal_thread_work(store, [left for left in lefts.values() if left])
    out: list[GardenCandidate] = []
    ancestry: ProdAncestry | None = None

    def landed_left_tokens(text: str) -> list[str]:
        """Left-line commit tokens confirmed as ancestors of ``origin/prod``."""
        nonlocal ancestry
        out: list[str] = []
        for token in _left_commit_tokens(text)[:THREAD_COMMIT_CHECK_CAP]:
            if ancestry is None:
                ancestry = ProdAncestry()
            if ancestry.is_deployed(token):
                out.append(token.lower())
        return out

    for rid, body, sha in bodies:
        left = lefts[rid]
        closed = _closed_left_handles(left or "", terminals)
        landed = (
            {}
            if left is None or closed
            else {token: True for token in landed_left_tokens(left)}
        )
        if left is not None and not closed and not landed:
            continue
        ancestor = _current_thread_ancestor(
            store, rid, left_is_stale=lambda text: bool(landed_left_tokens(text))
        )
        successor = ancestor[0] if ancestor else None
        shas = {str(rid): sha}
        if ancestor:
            shas[str(ancestor[0])] = ancestor[2]
        reason = (
            "missing_left"
            if left is None
            else ("terminal_work" if closed else "landed_sha")
        )
        title_tail = {
            "missing_left": "no Left: line",
            "terminal_work": "closed Left: work",
            "landed_sha": "landed Left: sha",
        }[reason]
        out.append(
            GardenCandidate(
                detector="thread_shape",
                action="supersede" if ancestor else "retire",
                target_kind="ref",
                target_id=rid,
                other_id=successor,
                title=f"me{rid} has {title_tail}",
                evidence={
                    "reason": reason,
                    "left": left,
                    "terminal_work": closed,
                    "landed_shas": landed,
                    "prod_ref": ancestry.prod_ref if ancestry is not None else None,
                    "supporting_shas": {
                        **_memory_supporting_shas(store, list(closed)),
                        **({f"me{ancestor[0]}": ancestor[2]} if ancestor else {}),
                    },
                    "content_sha": sha,
                    "ancestor_left": ancestor[1] if ancestor else None,
                    "sha_on_prod": (
                        "checked"
                        if ancestry is not None and ancestry.prod_ref
                        else "not_checked"
                    ),
                },
                payload={
                    "base_sha": sha,
                    "shas": shas,
                    "successor": f"me{successor}" if successor else None,
                    "requires_compaction": True,
                    "preserve": {
                        "target": successor,
                        "section": "Earlier" if successor else None,
                        "instruction": (
                            "Fold still-needed figures into the ancestor's Earlier: line "
                            "before superseding; never discard still-true facts."
                            if successor
                            else "Review and preserve still-needed facts before retirement."
                        ),
                    },
                },
            )
        )
        if len(out) >= limit:
            nxt = rid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


MEMORY_REVIEW_MATCH_CAP = 20
_REVIEW_REASONS = ("expired", "never_reviewed", "sha_changed", "version_bumped", "aged")
_DATE = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)\s+\d{4})(?!\d)",
    re.IGNORECASE,
)
_SNAPSHOT_TITLE = re.compile(
    r"\b(?:handoff|resume|state\s+(?:on|as\s+of)|as\s+of)\b", re.I
)
_CAMPAIGN_RANGE_TITLE = re.compile(
    r"\bwaves?\s+\d+\s*[-–—]\s*\d+\b|"
    r"\b\d{4}-\d{2}-\d{2}\s*(?:[-–—]|to|through)\s*(?:\d{4}-)?\d{2}-\d{2}\b",
    re.I,
)
_ABSENCE = re.compile(
    r"\b(?:no\s+[`*_\"']*\w+|none|not\s+yet|does\s+not\s+exist)\b", re.I
)
_ABSENCE_WORDS = frozenset({"none", "not", "yet", "does", "exist", "exists"})
_PATH_TITLE = re.compile(
    r"(?:^|\s)(?:[~./\\]|[A-Za-z]:[\\/])?"
    r"[\w.~-]+(?:[/\\][\w.~-]+)+(?:::[\w.]+|#[\w-]+)?|^\S+\.(?:md|py|sh|json|ya?ml|txt)$"
)


def memory_expiry_shape(title: str, sections: frozenset[str]) -> tuple[str, int | None]:
    """Classify expiry from section tags and title alone (gr560763).

    An operational title overrides a standing section: a handoff filed as
    reference still expires. Incident/campaign write-ups keep 90 days;
    ordinary reference, gotchas and runbooks have no default expiry.
    DREAM exclusion belongs to the reader, before this helper is called.
    """
    if "threads" in sections:
        return "thread", 14
    if sections & {"handoff", "resume"} or _SNAPSHOT_TITLE.search(title):
        return "snapshot", 14
    if sections & {"incident", "incidents"} or re.search(
        r"\b(?:incident|post[- ]?mortem)\b", title, re.I
    ):
        return "incident", 90
    if (
        sections & {"campaign", "campaigns"}
        or re.search(r"\bcampaign\b", title, re.I)
        or _CAMPAIGN_RANGE_TITLE.search(title)
    ):
        return "campaign", 90
    return "standing", None


def _review_date(raw: Any) -> datetime | None:
    """Parse metadata/stamp dates without turning malformed input into a finding."""
    if not isinstance(raw, (str, datetime)):
        return None
    try:
        value = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _ReviewMemory:
    ref_id: int
    title: str
    body: str
    sha: str
    meta: dict[str, Any]
    sections: frozenset[str]
    updated_at: datetime
    review: dict[str, Any] | None
    confidence: str


def _memory_review_rows(store: Store, ids: list[int]) -> list[_ReviewMemory]:
    """Read content, tags and best kg-review stamp in one DB snapshot.

    Prefer a stamp for the current SHA and checklist over a later stale
    stamp. The ledger has no versioned writer yet: an unversioned stamp
    remains evidence of a review, but cannot close this checklist version.
    """
    from precis.graph.garden.proposer import MEMORY_PROMPT_VERSION

    if not ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title, coalesce((SELECT string_agg(c.text, E'\\n' "
            "ORDER BY c.ord, c.chunk_id) FROM chunks c WHERE c.ref_id = r.ref_id "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL), ''), precis_target_sha('ref', r.ref_id), "
            "r.meta, ARRAY(SELECT substring(t.value FROM 9) "
            "FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
            "AND starts_with(t.value, 'section:')), "
            "r.updated_at, stamp.act, r.trust_confidence "
            "FROM refs r LEFT JOIN LATERAL ("
            "SELECT a AS act FROM revisions rev "
            "CROSS JOIN LATERAL jsonb_array_elements(rev.trust_acts) a "
            "WHERE rev.target_kind = 'ref' AND rev.target_id = r.ref_id "
            "AND a->>'action' = 'consider' "
            "AND a->'payload'->'kg_review'->>'verdict' IN ('ok', 'fixed') "
            "ORDER BY (a->>'expected_sha' = precis_target_sha('ref', r.ref_id)) "
            "DESC NULLS LAST, (a->'payload'->>'prompt_version' = %s) DESC NULLS LAST, "
            "a->'payload'->'kg_review'->>'date' DESC NULLS LAST, "
            "rev.revision_id DESC LIMIT 1) stamp ON true "
            "WHERE r.ref_id = ANY(%s) AND r.kind = 'memory' "
            "AND r.retired_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'DREAM') ORDER BY r.ref_id",
            (MEMORY_PROMPT_VERSION, ids),
        ).fetchall()
    return [
        _ReviewMemory(
            int(rid),
            title,
            body,
            sha,
            meta,
            frozenset(sections),
            updated,
            review,
            confidence,
        )
        for rid, title, body, sha, meta, sections, updated, review, confidence in rows
    ]


def _review_due_candidate(row: _ReviewMemory, now: datetime) -> GardenCandidate | None:
    from precis.graph.garden.proposer import MEMORY_PROMPT_VERSION

    shape, days = memory_expiry_shape(row.title, row.sections)
    unchecked: list[str] = []
    expires: datetime | None = None
    if "expires" in row.meta:
        expires = _review_date(row.meta["expires"])
        if row.meta["expires"] is not None and expires is None:
            unchecked.append("meta.expires")
    elif days is not None:
        expires = row.updated_at + timedelta(days=days)
    review_by = _review_date(row.meta.get("review_by"))
    if row.meta.get("review_by") is not None and review_by is None:
        unchecked.append("meta.review_by")
    review = row.review
    payload: dict[str, Any] = review.get("payload", {}) if review else {}
    reviewed_at = _review_date(payload.get("kg_review", {}).get("date"))
    age_days = (
        30
        if row.sections & {"reference", "gotchas"}
        else 60
        if "runbooks" in row.sections
        else None
    )
    if row.sections & {"threads", "workflow", "rulings"}:
        age_days = None
    if expires is not None and expires <= now:
        reason = "expired"
    elif review is None:
        reason = "never_reviewed"
    elif review.get("expected_sha") != row.sha:
        reason = "sha_changed"
    elif row.confidence != "considered":
        # A demote invalidates the review without changing content. Treat it
        # as missing a usable review, retaining the historical stamp and
        # explicit invalidation state in the evidence.
        reason = "never_reviewed"
    elif payload.get("prompt_version") != MEMORY_PROMPT_VERSION:
        reason = "version_bumped"
    elif reviewed_at is None:
        # A malformed stamp is not a current review. Do not claim it aged.
        reason = "never_reviewed"
        unchecked.append("kg_review.date")
    elif (
        row.sections & {"workflow", "rulings"}
        and review_by is not None
        and reviewed_at < review_by <= now
    ) or (age_days is not None and reviewed_at + timedelta(days=age_days) <= now):
        reason = "aged"
    else:
        return None
    suggested = (
        "After verifying the incident, condense it to a dated cause, verified fix "
        "and open uncertainty; never invent a fix."
        if shape == "incident"
        else "Review current truth; preserve still-needed facts before proposing "
        "retirement, or re-arm expiry when this is still live state."
    )
    return GardenCandidate(
        detector="review_due",
        action="rewrite",
        target_kind="ref",
        target_id=row.ref_id,
        title=f"me{row.ref_id} is due for memory review ({reason})",
        evidence={
            "reason": reason,
            "shape": shape,
            "content_sha": row.sha,
            "expires": expires.isoformat() if expires else None,
            "expiry_source": "meta.expires" if "expires" in row.meta else "shape",
            "review_by": review_by.isoformat() if review_by else None,
            "reviewed_at": reviewed_at.isoformat() if reviewed_at else None,
            "review_sha": review.get("expected_sha") if review else None,
            "review_prompt_version": payload.get("prompt_version"),
            "review_state": "invalidated"
            if review and row.confidence != "considered"
            else "recorded"
            if review
            else "missing",
            "prompt_version": MEMORY_PROMPT_VERSION,
            "checked_at": now.isoformat(),
        },
        payload={
            "base_sha": row.sha,
            "suggested_action": suggested,
            "not_checked": [*unchecked, "current_truth"],
            "requires_compaction": reason == "expired",
            "match_cap": MEMORY_REVIEW_MATCH_CAP,
            "matches_capped": False,
        },
    )


def review_due_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
    now: datetime | None = None,
) -> list[GardenCandidate]:
    """Review debt, ordered expired/never/SHA/version/age, without writes.

    A priority cursor sweeps each reason before the next; the ref cursor
    resumes within that priority. Sorting a ref window then advancing to
    the largest emitted ID would silently skip lower-priority, lower-ID
    memories when limit fills. At most scan_cap IDs are inspected across
    all priorities in a call; a full sweep resets both cursors. As with
    the other memory detectors, run_detectors supplies the deadline.
    Expiry is computed here only: no put acknowledgement or meta write.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key, priority_key = "review_due:memory", "review_due:priority"
    saved: dict[str, int] = cursors or {}
    after = saved.get(key, 0)
    priority = saved.get(priority_key, 0) % len(_REVIEW_REASONS)
    remaining = scan_cap
    checked_at = now or datetime.now(UTC)
    out: list[GardenCandidate] = []
    while remaining > 0:
        window, nxt = _scan_window(store, "memory", after=after, size=remaining)
        remaining -= len(window)
        for row in _memory_review_rows(store, window):
            candidate = _review_due_candidate(row, checked_at)
            if (
                candidate is None
                or candidate.evidence["reason"] != _REVIEW_REASONS[priority]
            ):
                continue
            out.append(candidate)
            if len(out) >= limit:
                nxt = row.ref_id
                break
        after = nxt
        if len(out) >= limit or nxt:
            break
        priority += 1
        if priority == len(_REVIEW_REASONS):
            priority = 0
            break
    if next_cursors is not None:
        next_cursors[key] = after
        next_cursors[priority_key] = priority
    return out


def title_shape_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Bounded title smells, pinned to the same snapshot as the target SHA.

    Four possible reasons bound each candidate; title-only evidence does
    not authorize inventing the body's conclusion. Dispatcher deadline.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "title_shape:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    out: list[GardenCandidate] = []
    for row in _memory_review_rows(store, window):
        reasons: list[str] = []
        if re.fullmatch(r"[a-z0-9_]+", row.title):
            reasons.append("slug")
        if _PATH_TITLE.fullmatch(row.title.strip("` ")):
            reasons.append("path_like")
        if len(row.title.split()) > 16:
            reasons.append("too_many_words")
        if "threads" in row.sections and (
            _DATE.search(row.title) or _CAMPAIGN_RANGE_TITLE.search(row.title)
        ):
            reasons.append("dated_snapshot")
        if not reasons:
            continue
        out.append(
            GardenCandidate(
                detector="title_shape",
                action="rewrite",
                target_kind="ref",
                target_id=row.ref_id,
                title=f"me{row.ref_id} has a title-shape smell",
                evidence={
                    "title": row.title,
                    "reasons": reasons,
                    "content_sha": row.sha,
                },
                payload={
                    "base_sha": row.sha,
                    "new_title": None,
                    "match_cap": 4,
                    "matches_capped": False,
                },
            )
        )
        if len(out) >= limit:
            nxt = row.ref_id
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


def negative_undated_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Absence claims without a date on that line; not proof of falsity.

    The grep brief is a deterministic content-word proxy for the line's
    nouns, not a semantic parser or a claimed repo search. Dispatcher
    deadline and a per-body match cap bound the evidence.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "negative_undated:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    out: list[GardenCandidate] = []
    for rid, body, sha in _memory_bodies(store, window):
        lines: list[dict[str, Any]] = []
        offset = 0
        capped = False
        for number, raw in enumerate(body.splitlines(keepends=True), 1):
            line = raw.rstrip("\r\n")
            start = offset
            offset += len(raw)
            if not _ABSENCE.search(line) or _DATE.search(line):
                continue
            if len(lines) == MEMORY_REVIEW_MATCH_CAP:
                capped = True
                break
            lines.append(
                {
                    "line": line,
                    "line_number": number,
                    "span": [start, start + len(line)],
                    "grep_brief": sorted(content_words(line) - _ABSENCE_WORDS),
                }
            )
        if not lines:
            continue
        out.append(
            GardenCandidate(
                detector="negative_undated",
                action="rewrite",
                target_kind="ref",
                target_id=rid,
                title=f"me{rid} asserts absence without a line date",
                evidence={"lines": lines, "content_sha": sha},
                payload={
                    "base_sha": sha,
                    "not_checked": ["absence_claims"],
                    "match_cap": MEMORY_REVIEW_MATCH_CAP,
                    "matches_capped": capped,
                },
            )
        )
        if len(out) >= limit:
            nxt = rid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


RESTATED_SENTENCE_CAP = 128
RESTATED_NODE_SENTENCE_CAP = 20
RESTATED_MATCH_CAP = 20
RESTATED_MIN_COSINE = 0.9
RESTATED_MIN_LEXICAL = 0.7
RESTATED_EMBED_SECONDS = 10.0
_RESTATED_PREFIX = "restated_claim:"
_SENTENCE = re.compile(r"\S[\s\S]*?(?:[.!?](?=\s|$)|$)")
_BOILERPLATE = re.compile(r"^(?:source|left|meta|title)\s*:", re.IGNORECASE)
_OWNER_RANK = {"section:reference": 0, "section:gotchas": 1, "section:threads": 2}


def _claim_sentences(
    body: str, title: str, *, min_words: int = 8
) -> Iterator[tuple[str, list[int]]]:
    """Exact sentence spans, omitting short text, headings and metadata lines.

    Join soft-wrapped prose within a paragraph; a boilerplate line terminates
    the paragraph so it cannot leak into a neighbouring sentence's span.
    """
    start = 0
    paragraph_start: int | None = None
    ranges: list[tuple[int, int]] = []
    for line in body.splitlines(keepends=True):
        clean = line.strip()
        plain = re.sub(r"^[*_-]+\s*", "", clean).strip("*_ ")
        excluded = (
            not clean
            or clean.startswith("#")
            or plain == title.strip()
            or bool(_BOILERPLATE.match(plain))
        )
        if excluded and paragraph_start is not None:
            ranges.append((paragraph_start, start))
            paragraph_start = None
        elif not excluded and paragraph_start is None:
            paragraph_start = start
        start += len(line)
    if paragraph_start is not None:
        ranges.append((paragraph_start, len(body)))
    for begin, end in ranges:
        for match in _SENTENCE.finditer(body, begin, end):
            text = match.group().rstrip()
            if len(re.findall(r"\b\w+\b", text)) >= min_words:
                yield text, [match.start(), match.start() + len(text)]


def _restated_window(
    store: Store, saved: dict[str, int], size: int
) -> tuple[int, list[int]]:
    """One live section:index hub's descendants, excluding nested hubs.

    UNION deduplicates recursive paths, including cycles. Only live memory
    parents are traversed; a retired summary cannot establish shared scope.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id FROM refs r JOIN ref_tags rt USING (ref_id) "
            "JOIN tags t USING (tag_id) WHERE r.kind = 'memory' "
            "AND r.retired_at IS NULL AND t.namespace = 'OPEN' AND t.value = %s "
            "AND r.ref_id > %s ORDER BY r.ref_id LIMIT 1",
            (INDEX_TAG, saved.get(_RESTATED_PREFIX + "after_hub", 0)),
        ).fetchone()
        if row is None:
            return 0, []
        hub = int(row[0])
        after = (
            saved.get(_RESTATED_PREFIX + "memory", 0)
            if saved.get(_RESTATED_PREFIX + "hub") == hub
            else 0
        )
        rows = conn.execute(
            "WITH RECURSIVE members(ref_id) AS (SELECT %s::bigint UNION "
            "SELECT r.ref_id FROM members m JOIN links l ON l.dst_ref_id = m.ref_id "
            "JOIN refs r ON r.ref_id = l.src_ref_id WHERE l.relation = 'part-of' "
            "AND r.kind = 'memory' AND r.retired_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' AND t.value = %s)) "
            "SELECT ref_id FROM members WHERE ref_id <> %s AND ref_id > %s "
            "ORDER BY ref_id LIMIT %s",
            (hub, INDEX_TAG, hub, after, size),
        ).fetchall()
    return hub, [int(row[0]) for row in rows]


def _restated_embeddings(store: Store, texts: list[str]) -> list[list[float]]:
    """Use the configured embedder only if it matches the registry default.

    No cold local model load, mock fallback, LLM route or stored-vector fill.
    The caller bounds construction, readiness, identity and the batch together.
    """
    from precis.config import load_config
    from precis.embedder import make_embedder_from_config

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name, dim FROM embedders WHERE is_default = TRUE "
            "ORDER BY name LIMIT 1"
        ).fetchone()
    if row is None:
        raise RuntimeError("default embedder unavailable")
    embedder = make_embedder_from_config(load_config(), dim=int(row[1]))
    if (
        not getattr(embedder, "is_production", True)
        or not embedder.is_ready()
        or embedder.model != str(row[0])
        or embedder.dim != int(row[1])
    ):
        raise RuntimeError("configured embedder is not the ready default embedder")
    vectors = embedder.embed(texts)
    array = np.asarray(vectors, dtype=float)
    if (
        array.shape != (len(texts), int(row[1]))
        or not np.isfinite(array).all()
        or (np.linalg.norm(array, axis=1) == 0).any()
    ):
        raise ValueError("default embedder returned invalid vectors")
    return vectors


def restated_claim_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
    not_checked: dict[str, str] | None = None,
) -> list[GardenCandidate]:
    """Propose one keeper and sentence-to-handle rewrites within a hub window.

    At most 128 sentences (20 per node) are embedded per tick. Candidate
    pairs resume inside the same window when limit fills; completed windows
    advance by memory id, completed hubs by hub id. Comparisons stay within
    each window, not an exhaustive comparison of the corpus.
    The dispatcher bounds SQL too; the embed batch has its own shorter bound.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    saved = dict(cursors or {})
    size = min(scan_cap, RESTATED_SENTENCE_CAP)
    hub, window = _restated_window(store, saved, size)
    if not hub:
        if next_cursors is not None:
            next_cursors.update(
                {
                    _RESTATED_PREFIX + key: 0
                    for key in ("hub", "after_hub", "memory", "pair")
                }
            )
        return []
    refs = store.fetch_refs_by_ids(window)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id, t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN' "
            "AND t.value = ANY(%s)",
            (window, list(_OWNER_RANK)),
        ).fetchall()
    ranks: dict[int, int] = {}
    for rid, tag in rows:
        ranks[int(rid)] = min(ranks.get(int(rid), 3), _OWNER_RANK[str(tag)])
    sentences: list[tuple[int, str, list[int]]] = []
    shas: dict[str, str] = {}
    capped: set[int] = set()
    bodies = {rid: (body, sha) for rid, body, sha in _memory_bodies(store, window)}
    last = 0
    window_capped = False
    for rid in window:
        if rid not in bodies:
            last = rid
            continue
        body, sha = bodies[rid]
        spans = list(
            islice(
                _claim_sentences(body, refs[rid].title or ""),
                RESTATED_NODE_SENTENCE_CAP + 1,
            )
        )
        if (
            len(sentences) + min(len(spans), RESTATED_NODE_SENTENCE_CAP)
            > RESTATED_SENTENCE_CAP
        ):
            window_capped = True
            break
        if len(spans) > RESTATED_NODE_SENTENCE_CAP:
            capped.add(rid)
        sentences.extend(
            (rid, text, span) for text, span in spans[:RESTATED_NODE_SENTENCE_CAP]
        )
        shas[str(rid)] = sha
        last = rid

    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    matches_capped: set[tuple[int, int]] = set()
    if len({rid for rid, _, _ in sentences}) >= 2:
        budget = RESTATED_EMBED_SECONDS
        if (deadline := deadline_at()) is not None:
            budget = min(budget, max(0.001, deadline - time.monotonic() - 0.1))
        try:
            vectors = call_with_deadline(
                lambda: _restated_embeddings(store, [text for _, text, _ in sentences]),
                deadline_s=budget,
                label="garden:restated_claim:embed",
            )
        except (RuntimeError, ValueError, OSError, ImportError) as exc:
            # DeadlineExceeded inherits TimeoutError/OSError. Never turn an
            # unavailable semantic source into a lexical-only finding.
            if not_checked is not None:
                not_checked["restated_claim"] = "embedder_unavailable"
            log.info("garden: restated_claim not_checked (%s)", type(exc).__name__)
            return []
        for i, (a, a_text, a_span) in enumerate(sentences):
            for j in range(i + 1, len(sentences)):
                b, b_text, b_span = sentences[j]
                if a == b:
                    continue
                cosine = 1.0 - _cosine(vectors[i], vectors[j])
                overlap = lexical_overlap(a_text, b_text)
                if cosine < RESTATED_MIN_COSINE and overlap < RESTATED_MIN_LEXICAL:
                    continue
                keeper, target = sorted(
                    (a, b), key=lambda rid: (ranks.get(rid, 3), rid)
                )
                key = (target, keeper)
                matches = grouped.setdefault(key, [])
                if len(matches) == RESTATED_MATCH_CAP:
                    matches_capped.add(key)
                    continue
                matches.append(
                    {
                        "target": {
                            "text": a_text if target == a else b_text,
                            "span": a_span if target == a else b_span,
                        },
                        "keeper": {
                            "text": a_text if keeper == a else b_text,
                            "span": a_span if keeper == a else b_span,
                        },
                        "distance": 1.0 - cosine,
                        "lexical_overlap": overlap,
                    }
                )
    pairs = sorted(grouped)
    offset = (
        saved.get(_RESTATED_PREFIX + "pair", 0)
        if saved.get(_RESTATED_PREFIX + "hub") == hub
        else 0
    )
    out: list[GardenCandidate] = []
    for target, keeper in pairs[offset : offset + limit]:
        matches = grouped[(target, keeper)]
        ops: list[dict[str, Any]] = []
        seen: set[tuple[int, ...]] = set()
        for match in matches:
            span = match["target"]["span"]
            if tuple(span) not in seen:
                seen.add(tuple(span))
                ops.append(
                    {
                        "find": match["target"]["text"],
                        "span": span,
                        "text": f"See me{keeper}.",
                    }
                )
        out.append(
            GardenCandidate(
                detector="restated_claim",
                action="rewrite",
                target_kind="ref",
                target_id=target,
                other_id=keeper,
                title=f"me{target} restates a claim in me{keeper}",
                evidence={
                    "hub": f"me{hub}",
                    "matches": matches,
                    "content_sha": shas[str(target)],
                    "keeper_sha": shas[str(keeper)],
                    "owner_ranks": {
                        str(rid): ranks.get(rid, 3) for rid in (target, keeper)
                    },
                },
                payload={
                    "base_sha": shas[str(target)],
                    "shas": {str(rid): shas[str(rid)] for rid in (target, keeper)},
                    "keeper": f"me{keeper}",
                    "ops": ops,
                    "matches_capped": (target, keeper) in matches_capped,
                    "match_cap": RESTATED_MATCH_CAP,
                    "sentences_capped": bool(capped & {target, keeper})
                    or window_capped,
                    "sentence_cap": RESTATED_SENTENCE_CAP,
                },
            )
        )
    if next_cursors is not None:
        more_pairs = offset + len(out) < len(pairs)
        more_nodes = window_capped or len(window) == size
        next_cursors.update(
            {
                _RESTATED_PREFIX + "hub": hub if more_pairs or more_nodes else 0,
                _RESTATED_PREFIX + "after_hub": saved.get(
                    _RESTATED_PREFIX + "after_hub", 0
                )
                if more_pairs or more_nodes
                else hub,
                _RESTATED_PREFIX + "memory": saved.get(_RESTATED_PREFIX + "memory", 0)
                if more_pairs
                else (last if more_nodes else 0),
                _RESTATED_PREFIX + "pair": offset + len(out) if more_pairs else 0,
            }
        )
    return out


#: Compaction triggers (gr561025): no node retires and no sentence drops
#: before a per-sentence plan runs. ``expired`` reads the same expiry the
#: review_due detector computes (meta.expires, else the shape default);
#: ``cold`` is zero reads in COMPACT_COLD_DAYS on an older node
#: (``ref_reads``, 0261); ``history`` is at least half the non-empty
#: lines carrying a date, a sha or a landed/shipped/done marker; and
#: ``retire_proposal`` is any undecided garden retire proposal on the
#: node — compaction runs before that retirement may apply.
COMPACT_COLD_DAYS = 90
COMPACT_HISTORY_MIN = 0.5
COMPACT_SENTENCE_CAP = 128
COMPACT_RELATED_CAP = 24
_SHA_TOKEN = re.compile(r"\b[0-9a-f]{7,40}\b")
_HISTORY_WORD = re.compile(r"\b(?:landed|shipped|done)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _CompactRow:
    ref_id: int
    title: str
    body: str
    sha: str
    meta: dict[str, Any]
    sections: frozenset[str]
    updated_at: datetime
    created_at: datetime
    n_agent: int
    n_human: int
    last_read_at: datetime | None


def _compact_rows(store: Store, ids: list[int]) -> list[_CompactRow]:
    """Body, expiry inputs and the read counter in one snapshot, no DREAMs."""
    if not ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title, coalesce((SELECT string_agg(c.text, E'\\n' "
            "ORDER BY c.ord, c.chunk_id) FROM chunks c WHERE c.ref_id = r.ref_id "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL), ''), precis_target_sha('ref', r.ref_id), "
            "r.meta, ARRAY(SELECT substring(t.value FROM 9) "
            "FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
            "AND starts_with(t.value, 'section:')), "
            "r.updated_at, r.created_at, coalesce(rr.n_agent, 0), "
            "coalesce(rr.n_human, 0), "
            "greatest(rr.last_agent_at, rr.last_human_at) "
            "FROM refs r LEFT JOIN ref_reads rr ON rr.ref_id = r.ref_id "
            "WHERE r.ref_id = ANY(%s) AND r.kind = 'memory' "
            "AND r.retired_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'DREAM') ORDER BY r.ref_id",
            (ids,),
        ).fetchall()
    return [
        _CompactRow(
            int(rid),
            title,
            body,
            sha,
            meta or {},
            frozenset(sections),
            updated,
            created,
            int(na),
            int(nh),
            last,
        )
        for rid, title, body, sha, meta, sections, updated, created, na, nh, last in rows
    ]


def _pending_retire_proposals(store: Store, ids: list[int]) -> dict[int, list[int]]:
    """Undecided garden retire proposals per memory: the compact-first trigger."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT p.target_id, p.review_id FROM reviews p "
            "WHERE p.target_kind = 'ref' AND p.target_id = ANY(%s) "
            "AND p.verdict = 'proposed' AND p.note LIKE %s AND p.note LIKE %s "
            "AND NOT EXISTS (SELECT 1 FROM reviews d WHERE d.note LIKE %s "
            "AND d.note ~ ('\"resolves\": ' || p.review_id || '[^0-9]'))",
            (ids, '%"garden"%', '%"action": "retire"%', '%"resolves"%'),
        ).fetchall()
    out: dict[int, list[int]] = {}
    for target_id, review_id in rows:
        out.setdefault(int(target_id), []).append(int(review_id))
    return out


def _compact_related(store: Store, ids: list[int]) -> dict[int, dict[str, list[int]]]:
    """The live-memory neighbourhood a compaction may move claims into.

    Hub parents and their live children, qualifies targets, the remaining
    outgoing-edge neighbours, ``+recall`` card neighbours inside the
    recall floor, and pending ``restated_claim`` matches — every
    destination the compaction brief names. Bounded per node; the apply
    door refuses plan edits to refs outside this set, and only live
    memory refs ever enter it (``_checked_compact_plan`` requires the
    whole set live).
    """
    related: dict[int, dict[str, list[int]]] = {
        rid: {
            "parents": [],
            "siblings": [],
            "qualifies": [],
            "neighbours": [],
            "recall": [],
            "restated": [],
        }
        for rid in ids
    }
    if not ids:
        return related
    with store.pool.connection() as conn:
        parent_rows = conn.execute(
            "SELECT l.src_ref_id, l.dst_ref_id FROM links l JOIN refs r "
            "ON r.ref_id = l.dst_ref_id WHERE l.relation = 'part-of' "
            "AND l.src_ref_id = ANY(%s) AND r.kind = 'memory' "
            "AND r.retired_at IS NULL",
            (ids,),
        ).fetchall()
        by_parent: dict[int, list[int]] = {}
        for src, dst in parent_rows:
            related[int(src)]["parents"].append(int(dst))
            by_parent.setdefault(int(dst), []).append(int(src))
        if by_parent:
            sib_rows = conn.execute(
                "SELECT l.dst_ref_id, l.src_ref_id FROM links l JOIN refs r "
                "ON r.ref_id = l.src_ref_id WHERE l.relation = 'part-of' "
                "AND l.dst_ref_id = ANY(%s) AND r.kind = 'memory' "
                "AND r.retired_at IS NULL",
                (list(by_parent),),
            ).fetchall()
            for parent, child in sib_rows:
                for rid in by_parent[int(parent)]:
                    if int(child) != rid:
                        related[rid]["siblings"].append(int(child))
        edge_rows = conn.execute(
            "SELECT l.src_ref_id, l.dst_ref_id, l.relation FROM links l "
            "JOIN refs r ON r.ref_id = l.dst_ref_id "
            "WHERE l.src_ref_id = ANY(%s) AND l.relation <> 'part-of' "
            "AND r.kind = 'memory' AND r.retired_at IS NULL",
            (ids,),
        ).fetchall()
        # Undecided restated_claim proposals pair a restating memory with
        # the keeper holding its claim — either side is a named match.
        restated_rows = conn.execute(
            "SELECT p.target_id, p.note::jsonb #>> '{garden,payload,keeper}' "
            "FROM reviews p WHERE p.verdict = 'proposed' "
            "AND p.note LIKE %(det)s AND (p.target_id = ANY(%(ids)s) OR "
            "p.note::jsonb #>> '{garden,payload,keeper}' = ANY(%(keepers)s)) "
            "AND NOT EXISTS (SELECT 1 FROM reviews d WHERE d.note LIKE "
            "%(res)s AND d.note ~ ('\"resolves\": ' || p.review_id || '[^0-9]'))",
            {
                "det": '%"detector": "restated_claim"%',
                "ids": ids,
                "keepers": [f"me{i}" for i in ids],
                "res": '%"resolves"%',
            },
        ).fetchall()
        partners = {int(t) for t, _ in restated_rows}
        for _t, keeper_raw in restated_rows:
            parsed = parse_handle(str(keeper_raw or ""))
            if parsed is not None and not parsed[1]:
                partners.add(int(parsed[2]))
        outside = partners - set(ids)  # scanned refs are already known live
        live_partners = partners & set(ids)
        if outside:
            live_partners |= {
                int(r[0])
                for r in conn.execute(
                    "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
                    "AND kind = 'memory' AND retired_at IS NULL",
                    (sorted(outside),),
                ).fetchall()
            }
    for src, dst, relation in edge_rows:
        bucket = "qualifies" if str(relation) == "qualifies" else "neighbours"
        related[int(src)][bucket].append(int(dst))
    for target_id, keeper_raw in restated_rows:
        parsed = parse_handle(str(keeper_raw or ""))
        keeper = int(parsed[2]) if parsed is not None and not parsed[1] else None
        if int(target_id) in related and keeper in live_partners:
            related[int(target_id)]["restated"].append(keeper)
        if keeper in related and int(target_id) in live_partners:
            related[keeper]["restated"].append(int(target_id))
    # ``+recall`` neighbours — the card-index rung the writer's fisheye
    # already lists: top-k card neighbours inside the recall floor.
    vecs = _card_vectors(store, ids)
    tops = _top_k_card_neighbours(store, set(ids), vecs, k=PRUNE_TOP_K)
    recall_ids = sorted({n for ns in tops.values() for n in ns})
    if recall_ids:
        with store.pool.connection() as conn:
            live_cards = {
                int(r[0])
                for r in conn.execute(
                    "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) "
                    "AND kind = 'memory' AND retired_at IS NULL",
                    (recall_ids,),
                ).fetchall()
            }
        neigh_vecs = _card_vectors(store, sorted(live_cards))
        for rid, ns in tops.items():
            for n in ns:
                if n in neigh_vecs and _cosine(vecs[rid], neigh_vecs[n]) <= (
                    PRUNE_MAX_DISTANCE
                ):
                    related[rid]["recall"].append(n)
    for rel in related.values():
        for bucket, values in rel.items():
            rel[bucket] = sorted(set(values))[:COMPACT_RELATED_CAP]
    return related


def _compact_sentences(body: str) -> tuple[list[dict[str, Any]], bool]:
    """Every sentence span in the body, capped — the plan's work list."""
    found = [
        {
            "span": [m.start(), m.start() + len(m.group().rstrip())],
            "text": m.group().rstrip(),
        }
        for m in islice(_SENTENCE.finditer(body), COMPACT_SENTENCE_CAP + 1)
    ]
    return found[:COMPACT_SENTENCE_CAP], len(found) > COMPACT_SENTENCE_CAP


def _compact_expiry(row: _CompactRow) -> tuple[datetime | None, str | None]:
    """The expiry instant and whether meta.expires or the shape supplied it."""
    if "expires" in row.meta:
        return _review_date(row.meta["expires"]), "meta.expires"
    _shape, days = memory_expiry_shape(row.title, row.sections)
    if days is None:
        return None, None
    return row.updated_at + timedelta(days=days), "shape"


def compact_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
    now: datetime | None = None,
) -> list[GardenCandidate]:
    """Memories owing a per-sentence compaction plan before any retirement.

    Read-only. The evaluator (proposer) fills ``payload.plan``; the apply
    door is ``apply_compact`` behind the memory apply gate. Evidence pins
    only what recomputes identically: mutable figures (read counters) ride
    along only for the trigger that fired on them.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "compact:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    rows = _compact_rows(store, window)
    pending_retire = _pending_retire_proposals(store, [row.ref_id for row in rows])
    related = _compact_related(store, [row.ref_id for row in rows])
    shas = _ref_shas(
        store,
        sorted(
            {rid for rel in related.values() for ids in rel.values() for rid in ids}
        ),
    )
    checked_at = now or datetime.now(UTC)
    out: list[GardenCandidate] = []
    for row in rows:
        reasons: list[str] = []
        evidence: dict[str, Any] = {"content_sha": row.sha}
        expires, expiry_source = _compact_expiry(row)
        if expires is not None and expires <= checked_at:
            reasons.append("expired")
            evidence["expires"] = expires.isoformat()
            evidence["expiry_source"] = expiry_source
        if row.created_at <= checked_at - timedelta(days=COMPACT_COLD_DAYS) and (
            row.last_read_at is None
            or row.last_read_at <= checked_at - timedelta(days=COMPACT_COLD_DAYS)
        ):
            reasons.append("cold")
            evidence["created_at"] = row.created_at.isoformat()
            evidence["reads"] = {
                "n_agent": row.n_agent,
                "n_human": row.n_human,
                "last_read_at": row.last_read_at.isoformat()
                if row.last_read_at
                else None,
            }
        nonempty = [line for line in row.body.splitlines() if line.strip()]
        history = sum(
            bool(
                _DATE.search(line)
                or _SHA_TOKEN.search(line)
                or _HISTORY_WORD.search(line)
            )
            for line in nonempty
        )
        if nonempty and history / len(nonempty) >= COMPACT_HISTORY_MIN:
            reasons.append("history")
            evidence["history_density"] = round(history / len(nonempty), 3)
            evidence["history_lines"] = history
            evidence["nonempty_lines"] = len(nonempty)
        if row.ref_id in pending_retire:
            reasons.append("retire_proposal")
            evidence["retire_proposals"] = pending_retire[row.ref_id]
        if not reasons:
            continue
        evidence["reasons"] = reasons
        sentences, capped = _compact_sentences(row.body)
        evidence["sentences"] = sentences
        evidence["sentences_capped"] = capped
        evidence["related"] = related[row.ref_id]
        out.append(
            GardenCandidate(
                detector="compact",
                action="compact",
                target_kind="ref",
                target_id=row.ref_id,
                title=f"me{row.ref_id} owes a compaction plan ({', '.join(reasons)})",
                evidence=evidence,
                payload={
                    "base_sha": row.sha,
                    "shas": {
                        str(rid): sha
                        for rid, sha in sorted(shas.items())
                        if rid
                        in {i for ids in related[row.ref_id].values() for i in ids}
                    },
                    "plan": None,
                },
            )
        )
        if len(out) >= limit:
            nxt = row.ref_id
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


#: ``grab_bag`` (gr561019) — a memory carrying many independent claims is a
#: split candidate: recall cuts on title/hook words, so most of a grab-bag's
#: content is unfindable under its one card. The body leg needs >=4 top-level
#: bullets or headings whose sentence embeddings cluster into >=3 groups; the
#: title leg fires on conjunction/dual/slash/dated-campaign titles on its own.
GRAB_BAG_MIN_ITEMS = 4
GRAB_BAG_MIN_CLUSTERS = 3
GRAB_BAG_SENTENCE_CAP = 32
GRAB_BAG_MIN_WORDS = 4
GRAB_BAG_MAX_DISTANCE = 0.5
GRAB_BAG_EMBED_SECONDS = 10.0
#: Bounded bodies scanned per tick — each eligible body costs one embed call.
GRAB_BAG_SCAN_CAP = 64
_GRAB_BAG_ITEM = re.compile(r"(?:[-*+]|\d+[.)])\s+\S|#{1,6}\s+\S")
_GRAB_BAG_AND = re.compile(r"\band\b", re.IGNORECASE)
_GRAB_BAG_TWO = re.compile(r"\btwo\b", re.IGNORECASE)


def _grab_bag_title_reasons(title: str) -> list[str]:
    """Deterministic multi-claim title smells (gr561019)."""
    reasons = [
        reason
        for reason, hit in (
            ("and", _GRAB_BAG_AND.search(title)),
            ("two", _GRAB_BAG_TWO.search(title)),
            ("slash", "/" in title),
            (
                "campaign_or_dated",
                _CAMPAIGN_RANGE_TITLE.search(title) or _DATE.search(title),
            ),
        )
        if hit
    ]
    return reasons


def _memory_parents(store: Store, ids: list[int]) -> dict[int, list[int]]:
    """Live memory ``part-of`` parents per ref id, ascending."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.src_ref_id, l.dst_ref_id FROM links l JOIN refs r "
            "ON r.ref_id = l.dst_ref_id WHERE l.src_ref_id = ANY(%s) "
            "AND l.relation = 'part-of' AND r.kind = 'memory' "
            "AND r.retired_at IS NULL ORDER BY l.src_ref_id, l.dst_ref_id",
            (ids,),
        ).fetchall()
    out: dict[int, list[int]] = {}
    for src, dst in rows:
        out.setdefault(int(src), []).append(int(dst))
    return out


def _grab_bag_clusters(vectors: list[list[float]]) -> list[list[int]]:
    """Sentence-index groups: single-linkage components under the distance bar."""
    parent = list(range(len(vectors)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            if _cosine(vectors[i], vectors[j]) < GRAB_BAG_MAX_DISTANCE:
                pi, pj = find(i), find(j)
                if pi != pj:
                    parent[pj] = pi
    groups: dict[int, list[int]] = {}
    for i in range(len(vectors)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values(), key=lambda g: g[0])


def grab_bag_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
    not_checked: dict[str, str] | None = None,
) -> list[GardenCandidate]:
    """Grab-bag memories: many independent claims under one title (gr561019).

    Two deterministic legs, either sufficient: a multi-claim title smell, or
    >=4 top-level bullets/headings whose sentence embeddings cluster into >=3
    groups. Sentence vectors are ephemeral default-embedder reads, never
    stored; an unavailable embedder reports ``not_checked`` and resumes the
    scan at the failed body next tick, keeping evidence reproducible.
    ``sentence`` spans pin the exact claim text the evaluator may split —
    the apply door accepts no span outside this evidence.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    key = "grab_bag:memory"
    window, nxt = _scan_window(
        store,
        "memory",
        after=(cursors or {}).get(key, 0),
        size=min(scan_cap, GRAB_BAG_SCAN_CAP),
    )
    parents = _memory_parents(store, window)
    handles = [f"me{p}" for ids in parents.values() for p in ids]
    supporting = _memory_supporting_shas(store, handles)
    out: list[GardenCandidate] = []
    evaluated_last = 0
    aborted = False
    for row in _memory_review_rows(store, window):
        reasons = _grab_bag_title_reasons(row.title)
        items = sum(1 for line in row.body.splitlines() if _GRAB_BAG_ITEM.match(line))
        spans = list(
            islice(
                _claim_sentences(row.body, row.title, min_words=GRAB_BAG_MIN_WORDS),
                GRAB_BAG_SENTENCE_CAP + 1,
            )
        )
        capped = len(spans) > GRAB_BAG_SENTENCE_CAP
        spans = spans[:GRAB_BAG_SENTENCE_CAP]
        clusters: list[list[int]] | None = None
        if items >= GRAB_BAG_MIN_ITEMS and len(spans) >= 2:
            budget = GRAB_BAG_EMBED_SECONDS
            if (deadline := deadline_at()) is not None:
                budget = min(budget, max(0.001, deadline - time.monotonic() - 0.1))
            try:
                vectors = call_with_deadline(
                    partial(
                        _restated_embeddings,
                        store,
                        [text for text, _ in spans],
                    ),
                    deadline_s=budget,
                    label="garden:grab_bag:embed",
                )
            except (RuntimeError, ValueError, OSError, ImportError) as exc:
                # DeadlineExceeded inherits TimeoutError/OSError. Resume the
                # scan at this body next tick so evidence stays reproducible.
                if not_checked is not None:
                    not_checked["grab_bag"] = "embedder_unavailable"
                log.info("garden: grab_bag not_checked (%s)", type(exc).__name__)
                aborted = True
                break
            clusters = _grab_bag_clusters(vectors)
        evaluated_last = row.ref_id
        if not reasons and len(clusters or ()) < GRAB_BAG_MIN_CLUSTERS:
            continue
        out.append(
            GardenCandidate(
                detector="grab_bag",
                action="split",
                target_kind="ref",
                target_id=row.ref_id,
                title=f"me{row.ref_id} carries many claims under one title",
                evidence={
                    "title": row.title,
                    "title_reasons": reasons,
                    "items": items,
                    "clusters": clusters,
                    "sentences": [{"text": text, "span": span} for text, span in spans],
                    "sentences_capped": capped,
                    "parents": parents.get(row.ref_id, []),
                    "supporting_shas": {
                        f"me{p}": supporting[f"me{p}"]
                        for p in parents.get(row.ref_id, [])
                        if f"me{p}" in supporting
                    },
                    "content_sha": row.sha,
                },
                payload={
                    "base_sha": row.sha,
                    "hub": next(iter(parents.get(row.ref_id, [])), None),
                    "children": None,
                    "parent_fate": None,
                    "match_cap": MEMORY_REVIEW_MATCH_CAP,
                    "matches_capped": False,
                },
            )
        )
        if len(out) >= limit:
            nxt = row.ref_id
            break
    if next_cursors is not None:
        # An aborted embed resumes at the failed body; a completed window
        # saves the scan cursor (limit-break keeps its own resume point).
        next_cursors[key] = evaluated_last if aborted else nxt
    return out


DRIFT_MATCH_CAP = 20
DRIFT_MEMORY_EXCERPT_CAP = 1200
_DRIFT_SECTION = re.compile(
    r"(?P<path>docs/(?:[\w.-]+/)*[\w.-]+\.(?:md|markdown))"
    r"(?:`?[ \t]*§[ \t]*`?(?P<heading>[^`\n;]+)|[~#](?P<slug>[\w-]+))"
)
_SOURCE_LINE = re.compile(r"(?im)^[ \t]*(?:\*\*)?Source:(?:\*\*)?[^\n]*")
_ENTITY_HANDLE = re.compile(
    r"\b(?:mh[0-9]{1,19}|sk[0-9]{1,19}|md[0-9]{1,19}|"
    r"gr[0-9]{1,19}|td[0-9]{1,19}|ma_[\w-]+|precis-[\w-]+)\b"
)
_STATE_VERB = re.compile(
    r"\b(?:is\s+(?:permanently\s+)?(?:retired|the\s+only|live|open|closed|done)"
    r"|runs|has\s+no)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n[ \t]*\n|\n(?=[ \t]*(?:[-*+]|\d+[.)])\s)")


def _drift_bodies(store: Store, ids: list[int]) -> list[tuple[int, str, str, datetime]]:
    """Pin text, SHA and last touch together; chunk edits need not touch refs."""
    if not ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, coalesce(b.body, ''), "
            "precis_target_sha('ref', r.ref_id), "
            "greatest(r.updated_at, b.at, (SELECT max(v.at) FROM revisions v "
            "WHERE v.target_kind = 'ref' AND v.target_id = r.ref_id)) "
            "FROM refs r LEFT JOIN LATERAL ("
            "SELECT string_agg(c.text, E'\\n' ORDER BY c.ord, c.chunk_id) AS body, "
            "max(c.created_at) AS at FROM chunks c WHERE c.ref_id = r.ref_id "
            "AND c.chunk_kind = 'memory_body' AND c.ord >= 0 "
            "AND c.retired_at IS NULL) b ON true "
            "WHERE r.ref_id = ANY(%s) AND r.kind = 'memory' "
            "AND r.retired_at IS NULL AND NOT EXISTS ("
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'DREAM') "
            "ORDER BY r.ref_id",
            (ids,),
        ).fetchall()
    return [(int(rid), str(body), str(sha), at) for rid, body, sha, at in rows]


def _cited_sections(store: Store, rid: int, body: str) -> Iterator[dict[str, Any]]:
    """Read explicit section addresses on outgoing cites edges, bounded per node.

    source_handle is the established link provenance field. A chunk cite can
    instead supply the document address on its ref and a section_path heading.
    No title similarity or inferred source is a citation.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.link_id, l.meta->>'source_handle', r.title, "
            "(SELECT i.id_value FROM ref_identifiers i WHERE i.ref_id = r.ref_id "
            "AND i.id_kind = 'cite_key' LIMIT 1), c.section_path "
            "FROM links l JOIN refs r ON r.ref_id = l.dst_ref_id "
            "LEFT JOIN chunks c ON c.chunk_id = l.dst_chunk_id "
            "WHERE l.src_ref_id = %s AND l.relation = 'cites' "
            "ORDER BY l.link_id LIMIT %s",
            (rid, DRIFT_MATCH_CAP + 1),
        ).fetchall()
    for link_id, source, title, slug, section_path in rows:
        pointers = [source, title, slug]
        if section_path:
            pointers.extend(
                f"{address} § {section_path[-1]}"
                for address in (title, slug)
                if address
            )
        for pointer in pointers:
            if not pointer:
                continue
            for match in _DRIFT_SECTION.finditer(pointer):
                yield {
                    "anchor": match.group(),
                    "path": match["path"],
                    "heading": match["heading"] or "#" + match["slug"],
                    "link_id": int(link_id),
                    "cites_capped": len(rows) > DRIFT_MATCH_CAP,
                    # No anchor line locates the claim; extend ref_excerpts' prefix.
                    "memory_excerpt": (body[600:] or body)[-DRIFT_MEMORY_EXCERPT_CAP:],
                }


def _source_sections(store: Store, rid: int, body: str) -> Iterator[dict[str, Any]]:
    for line in _SOURCE_LINE.finditer(body):
        # The supported claim may be beyond the proposer's leading body excerpt.
        preceding = body[: line.start()].rstrip()[-DRIFT_MEMORY_EXCERPT_CAP:]
        memory_excerpt = re.split(r"\n[ \t]*\n", preceding)[-1]
        for match in _DRIFT_SECTION.finditer(line.group()):
            yield {
                "anchor": match.group(),
                "path": match["path"],
                "heading": match["heading"] or "#" + match["slug"],
                "span": [line.start() + match.start(), line.start() + match.end()],
                "context": line.group(),
                "memory_excerpt": memory_excerpt,
            }
    yield from _cited_sections(store, rid, body)


def _state_sentences(body: str) -> Iterator[tuple[int, str]]:
    """Join soft line breaks, preserving offsets and paragraph/list boundaries."""
    start = 0
    for end in _SENTENCE_END.finditer(body):
        if end.group() == "." and re.search(
            r"(?:^|[ \t])\d+$", body[start : end.start()]
        ):
            continue
        yield start, body[start : end.end()].replace("\n", " ")
        start = end.end()
    if start < len(body):
        yield start, body[start:].replace("\n", " ")


def _state_entities(body: str) -> Iterator[dict[str, Any]]:
    """Only handles/explicit repo anchors inside the same state-verb sentence."""
    for offset, text in _state_sentences(body):
        if not _STATE_VERB.search(text):
            continue
        for match in _ENTITY_HANDLE.finditer(text):
            yield {
                "anchor": match.group(),
                "kind": "ref",
                "target": match.group(),
                "span": [
                    offset + match.start(),
                    offset + match.end(),
                ],
                "context": text[max(0, match.start() - 300) : match.end() + 300],
            }
        for match in _MEMORY_ANCHOR.finditer(text):
            kind, target, detail = _anchor_lookup(match)
            if kind not in {"section", "python"}:
                continue
            # Bare dotted prose is too ambiguous for a claim subject. Code
            # spans and explicit file::symbol anchors are intentional handles.
            if kind == "python" and not (
                match["python_path"]
                or match["dotted"]
                or match.group().startswith("main::")
            ):
                continue
            yield {
                "anchor": match.group(),
                "kind": kind,
                "target": target,
                "detail": detail,
                "span": [
                    offset + match.start(),
                    offset + match.end(),
                ],
                "context": text[max(0, match.start() - 300) : match.end() + 300],
            }
        for match in _DRIFT_SECTION.finditer(text):
            if not match["slug"] or "~" not in match.group():
                continue  # Other section spellings were handled above.
            yield {
                "anchor": match.group(),
                "kind": "section",
                "target": match["path"],
                "detail": "#" + match["slug"],
                "span": [
                    offset + match.start(),
                    offset + match.end(),
                ],
                "context": text[max(0, match.start() - 300) : match.end() + 300],
            }


def _drift_candidates(
    store: Store,
    detector: str,
    *,
    scan_cap: int,
    limit: int,
    cursors: dict[str, int] | None,
    next_cursors: dict[str, int] | None,
) -> list[GardenCandidate]:
    if limit <= 0 or scan_cap <= 0:
        return []
    key = f"{detector}:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    resolver: RepoDriftResolver | None = None
    out: list[GardenCandidate] = []
    for rid, body, sha, touched in _drift_bodies(store, window):
        matches = list(
            islice(
                _source_sections(store, rid, body)
                if detector == "source_drift"
                else _state_entities(body),
                DRIFT_MATCH_CAP + 1,
            )
        )
        changes: list[dict[str, Any]] = []
        unchecked: list[dict[str, Any]] = []
        for match in matches[:DRIFT_MATCH_CAP]:
            if match.get("kind") == "ref":
                result = ref_change(store, match["target"], touched)
                if result["status"] == "not_checked":
                    slug = result.get("slug") if result.get("kind") == "skill" else None
                    if re.fullmatch(r"precis-[\w-]+", match["target"]):
                        slug = match["target"]
                    if isinstance(slug, str) and re.fullmatch(r"[\w-]+", slug):
                        resolver = resolver or RepoDriftResolver()
                        result = {
                            **result,
                            "kind": "skill",
                            "slug": slug,
                            **resolver.document(
                                f"src/precis/data/skills/{slug}.md", touched
                            ),
                        }
                        if result["status"] != "not_checked":
                            result.pop("reason", None)
                            result["basis"] = "committed_skill_document"
            else:
                resolver = resolver or RepoDriftResolver()
                if detector == "source_drift":
                    result = resolver.section(match["path"], match["heading"], touched)
                elif match["kind"] == "section":
                    result = resolver.section(match["target"], match["detail"], touched)
                else:
                    result = resolver.python(match["target"], match["detail"], touched)
            if result["status"] == "changed":
                changes.append({**match, **result})
                unchecked.extend(
                    {**match, "reason": reason}
                    for reason in result.get("not_checked", [])
                )
            elif result["status"] == "not_checked":
                unchecked.append({**match, **result})
        if changes:
            # Commit IDs locate evidence; only claim/source content identifies
            # the question, so unrelated commits cannot reopen its review.
            judged_changes = [
                {
                    key: change[key]
                    for key in (
                        "path",
                        "heading",
                        "kind",
                        "target",
                        "detail",
                        "ref_id",
                        "old_sha",
                        "new_sha",
                        "changes",
                    )
                    if key in change
                }
                for change in changes
            ]
            evidence_sha = hashlib.sha256(
                json.dumps(
                    {"memory_sha": sha, "changes": judged_changes}, sort_keys=True
                ).encode("utf-8")
            ).hexdigest()
            out.append(
                GardenCandidate(
                    detector=detector,
                    action="rewrite",
                    target_kind="ref",
                    target_id=rid,
                    title=f"me{rid} cites changed sources"
                    if detector == "source_drift"
                    else f"me{rid} states claims about changed entities",
                    evidence={
                        "changes": changes,
                        "content_sha": sha,
                        "memory_touched_at": touched.isoformat(),
                    },
                    payload={
                        "base_sha": sha,
                        "evidence_sha": evidence_sha,
                        "ops": [
                            {"find": m["anchor"], "text": None, "span": m["span"]}
                            for m in changes
                            if "span" in m
                        ],
                        "not_checked": unchecked,
                        "matches_capped": len(matches) > DRIFT_MATCH_CAP
                        or any(m.get("cites_capped") for m in matches),
                        "match_cap": DRIFT_MATCH_CAP,
                    },
                )
            )
        if len(out) >= limit:
            nxt = rid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


def source_drift_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Compare cited section chunks before memory touch with pinned main.

    Source lines and cites edges name docs sections; both section hashes and
    new text go to the evaluator, which decides whether the claim agrees.
    The dispatcher enforces the deadline. Only verified changes are findings;
    missing baselines accompany them as not_checked, never standalone drift.
    """
    return _drift_candidates(
        store,
        "source_drift",
        scan_cap=scan_cap,
        limit=limit,
        cursors=cursors,
        next_cursors=next_cursors,
    )


def entity_claim_stale_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Bound state-sentence entity checks to recorded post-touch changes.

    Content, STATUS and machine-role evidence remain distinct. Repo sections,
    symbols and file-backed skills use committed snapshots; DB refs require
    recorded history. No model, writes or apply; the dispatcher owns deadlines.
    """
    return _drift_candidates(
        store,
        "entity_claim_stale",
        scan_cap=scan_cap,
        limit=limit,
        cursors=cursors,
        next_cursors=next_cursors,
    )


# ---------------------------------------------------------------------------
# host_facts — host role/status claims in memory prose belong on the ref (gr560767)
# ---------------------------------------------------------------------------

#: Per-candidate bound on matched host sentences (one memory can name
#: several hosts); the ops list is the allow-list the reviewed apply door
#: re-verifies, so it cannot grow without bound.
HOST_FACTS_MATCH_CAP = 20

#: Host state predicates. A census hostname plus one of these in one
#: sentence is a current-state claim — the one-fact-one-ref rule's target.
#: Broad on purpose: the evaluator gate filters domain collisions (a
#: "spark" analytics-engine sentence carries no host claim); narrowing
#: here would hide real host state words.
_HOST_STATE_VERB = re.compile(
    r"\b(?:"
    r"retired|decommissioned|rejoin\w*|join\w*|left|migrat\w*|replac\w*|reviv\w*"
    r"|offline|online|live|down|gone|dead|active|quiet"
    r"|compute|gpu|worker|storage|database|serving|hosting|heartbeat"
    r")\b",
    re.IGNORECASE,
)

#: A sentence already carrying a machine handle or an ISO date is a dated
#: event (or this pass's own rewrite output), not a current-state claim —
#: the scan must never re-flag its own work.
_HOST_FACTS_DATED = re.compile(r"\b(?:ma_[\w-]+|mh\d+|20\d{2}-\d{2}(?:-\d{2})?)\b")


def _machine_hosts(store: Store) -> list[dict[str, Any]]:
    """Live ``machine`` refs — the census prose is matched against.

    ``meta.host`` is matched as a whole word, so ``ma_spark`` and
    ``sparkle`` never match ``spark``. Facts stay read-only here; the
    census refresh (``put(kind='machine', mode='sync')`` plus hand-added
    hosts) is the operator's step before this pass runs.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.meta, r.updated_at, "
            "(SELECT i.id_value FROM ref_identifiers i WHERE i.ref_id = r.ref_id "
            "AND i.id_kind = 'cite_key' LIMIT 1) "
            "FROM refs r WHERE r.kind = 'machine' AND r.retired_at IS NULL "
            "ORDER BY r.ref_id"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for rid, meta, updated, slug in rows:
        meta = meta or {}
        host = str(meta.get("host") or "").strip()
        if not host:
            continue
        out.append(
            {
                "ref_id": int(rid),
                "host": host,
                "handle": try_format("machine", int(rid)) or f"ma_{slug}",
                "role": meta.get("role"),
                "ref_updated_at": updated,
                "pattern": re.compile(
                    rf"(?<![\w-]){re.escape(host)}(?![\w-])", re.IGNORECASE
                ),
            }
        )
    return out


def host_facts_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Host role/status claims in memory prose belong on the machine ref (gr560767).

    One candidate per live memory carrying every sentence that names a
    census host (a live ``machine`` ref) with a state verb and neither an
    ISO date nor a machine handle — those are current-state claims; dated
    events and handle-bearing sentences are records and stay untouched.
    Each matched sentence is an anchored rewrite op (``text=None``: the
    evaluator writes the dated-event-plus-handle replacement). For hosts
    whose ref fact is older than the memory, ``payload.machine_put`` names
    the ``put(kind='machine', id='<host>', args={'role': …})`` that moves
    the verified fact onto the ref. The reviewed rewrite door re-runs this
    predicate before applying.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    machines = _machine_hosts(store)
    if not machines:
        return []
    key = "host_facts:memory"
    window, nxt = _scan_window(
        store, "memory", after=(cursors or {}).get(key, 0), size=scan_cap
    )
    out: list[GardenCandidate] = []
    for rid, body, sha, touched in _drift_bodies(store, window):
        sentences = list(_claim_sentences(body, "", min_words=3))
        matches: list[tuple[dict[str, Any], str, list[int]]] = []
        capped = False
        for machine in machines:
            for text, span in sentences:
                if len(matches) >= HOST_FACTS_MATCH_CAP:
                    capped = True
                    break
                if machine["pattern"].search(text) is None:
                    continue
                if not _HOST_STATE_VERB.search(text):
                    continue
                if _HOST_FACTS_DATED.search(text):
                    continue  # a dated event or already handle-bearing
                matches.append((machine, text, span))
            if capped:
                break
        if not matches:
            continue
        hosts_evidence: list[dict[str, Any]] = []
        ops: list[dict[str, Any]] = []
        machine_put: list[dict[str, Any]] = []
        by_host: dict[int, dict[str, Any]] = {}
        for machine, text, span in matches:
            entry = by_host.get(machine["ref_id"])
            if entry is None:
                updated = machine["ref_updated_at"]
                entry = {
                    "host": machine["host"],
                    "handle": machine["handle"],
                    "ref_id": machine["ref_id"],
                    "role": machine["role"],
                    "ref_updated_at": updated.isoformat() if updated else None,
                    "memory_touched": touched.isoformat() if touched else None,
                    "newer_than_ref": bool(
                        updated is not None
                        and touched is not None
                        and touched > updated
                    ),
                    "sentences": [],
                }
                by_host[machine["ref_id"]] = entry
                hosts_evidence.append(entry)
                if entry["newer_than_ref"]:
                    machine_put.append(
                        {
                            "id": machine["host"],
                            "args": {"role": None},
                            "why": (
                                "the memory is newer than the ref's fact — "
                                "fill role from the verified current truth"
                            ),
                        }
                    )
            entry["sentences"].append(
                {
                    "text": text,
                    "span": span,
                    "context": body[max(0, span[0] - 300) : span[1] + 300],
                }
            )
            ops.append({"find": text, "text": None, "span": span})
        evidence_sha = hashlib.sha256(
            json.dumps(hosts_evidence, ensure_ascii=False, sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()[:16]
        payload: dict[str, Any] = {
            "base_sha": sha,
            "ops": ops,
            "evidence_sha": evidence_sha,
            "matches_capped": capped,
        }
        if machine_put:
            payload["machine_put"] = machine_put
        out.append(
            GardenCandidate(
                detector="host_facts",
                action="rewrite",
                target_kind="ref",
                target_id=rid,
                title=f"me{rid} states a host's role/status in prose",
                evidence={"hosts": hosts_evidence, "content_sha": sha},
                payload=payload,
            )
        )
        if len(out) >= limit:
            nxt = rid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out


# ---------------------------------------------------------------------------
# griped — a reader's project:memory gripe is a replayable test (gr561873)
# ---------------------------------------------------------------------------

#: A live ``project:memory`` gripe carrying none of these STATUS values is
#: open for the detector — the fixer only replays complaints a reader still
#: waits on (mirrors the gripe kind's open-listing exclusion).
_GRIPED_CLOSED_STATUS = ("done", "wontfix", "parked")

#: Hit depth a route replay records per recorded search — the same index-view
#: page the reader ranked off.
ROUTE_REPLAY_K = 8

#: Per-candidate evidence bounds: named memories, entity refs and the
#: sentence-grain rewrite ops the evaluator may resolve.
GRIPED_MENTION_CAP = 16
GRIPED_ENTITY_CAP = 16
GRIPED_OP_CAP = 40
GRIPED_STORY_CHARS = 1500

#: Upper bound on open ``project:memory`` gripes read per windowed or
#: targeted pass — far above any realistic backlog; the dispatcher deadline
#: owns the wall clock either way.
GRIPED_OPEN_SCAN = 500


def _griped_window(store: Store, *, after: int, size: int) -> tuple[list[int], int]:
    """Open ``project:memory`` gripe ids past ``after`` (ascending), and the
    cursor to save — the window's last id, or ``0`` when it reached the end
    (same contract as :func:`_scan_window`)."""
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT r.ref_id FROM refs r WHERE r.kind = 'gripe' "
                "AND r.retired_at IS NULL AND r.ref_id > %s "
                "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
                "  USING (tag_id) WHERE rt.ref_id = r.ref_id "
                "  AND t.namespace = 'OPEN' AND t.value = 'project:memory') "
                "AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
                "  USING (tag_id) WHERE rt.ref_id = r.ref_id "
                "  AND t.namespace = 'STATUS' AND t.value = ANY(%s)) "
                "ORDER BY r.ref_id LIMIT %s",
                (after, list(_GRIPED_CLOSED_STATUS), size),
            ).fetchall()
        ]
    return ids, (ids[-1] if len(ids) >= size else 0)


def _gripe_story(
    store: Store, ids: list[int]
) -> dict[int, tuple[str, str, dict[str, Any]]]:
    """``{gripe: (title, story, meta)}`` — body plus comments in chunk
    order (the reader's whole filing) and meta (``meta.route`` lives
    there). Content drift is covered by the story text itself, so no
    separate sha rides along."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.title, r.meta, "
            "coalesce((SELECT string_agg(c.text, E'\\n' ORDER BY c.ord, "
            "  c.chunk_id) FROM chunks c WHERE c.ref_id = r.ref_id "
            "  AND c.chunk_kind IN ('gripe_body', 'gripe_comment') "
            "  AND c.retired_at IS NULL), '') "
            "FROM refs r WHERE r.ref_id = ANY(%s) AND r.kind = 'gripe' "
            "AND r.retired_at IS NULL ORDER BY r.ref_id",
            (list(ids),),
        ).fetchall()
    return {int(r[0]): (str(r[1] or ""), str(r[3] or ""), r[2] or {}) for r in rows}


def _live_ref_rows(store: Store, ids: list[int]) -> dict[int, tuple[str, str]]:
    """``{ref_id: (kind, title)}`` for the given live refs."""
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, kind, title FROM refs "
            "WHERE ref_id = ANY(%s) AND retired_at IS NULL",
            (list(ids),),
        ).fetchall()
    return {int(r[0]): (str(r[1]), str(r[2] or "")) for r in rows}


def _story_refs(
    store: Store, story: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(memories, entities)`` — live refs the story's handles resolve to,
    memory kind first, everything else second (a machine/skill/python/md
    ref the claims name). Chunk handles resolve through their owning ref."""
    parsed = {
        (p[0], p[1], p[2])
        for m in _MEMORY_HANDLE.finditer(story)
        if (p := parse_handle(m.group())) is not None and p[2] <= 9223372036854775807
    }
    record_ids = [pk for _k, chunk, pk in parsed if not chunk]
    rows = _live_ref_rows(store, record_ids)
    owners: dict[int, tuple[int, str]] = {}
    chunk_ids = [pk for _k, chunk, pk in parsed if chunk]
    if chunk_ids:
        with store.pool.connection() as conn:
            owners = {
                int(c): (int(r), str(k))
                for c, r, k in conn.execute(
                    "SELECT c.chunk_id, r.ref_id, r.kind FROM chunks c "
                    "JOIN refs r USING (ref_id) WHERE c.chunk_id = ANY(%s) "
                    "AND r.retired_at IS NULL",
                    (chunk_ids,),
                ).fetchall()
            }
    seen: dict[int, dict[str, Any]] = {}
    for kind, is_chunk, pk in parsed:
        if is_chunk:
            owner = owners.get(pk)
            if owner is None:
                continue
            ref_id, ref_kind = owner
            title = _live_ref_rows(store, [ref_id]).get(ref_id, (ref_kind, ""))[1]
        else:
            if pk not in rows:
                continue
            ref_id, (ref_kind, title) = pk, rows[pk]
        seen.setdefault(
            ref_id,
            {
                "handle": try_format(ref_kind, ref_id) or f"{ref_kind}:{ref_id}",
                "ref_id": ref_id,
                "kind": ref_kind,
                "title": title,
            },
        )
    refs = sorted(seen.values(), key=lambda r: int(r["ref_id"]))
    memories = [r for r in refs if r["kind"] == "memory"][:GRIPED_MENTION_CAP]
    entities = [r for r in refs if r["kind"] != "memory"][:GRIPED_ENTITY_CAP]
    return memories, entities


def _handle_ref_id(value: Any) -> int | None:
    """A ``get`` call's recorded ``id`` (``me123``, ``memory:me123[~…]``) →
    its record ref_id, or ``None`` when it names no record handle."""
    if not isinstance(value, str):
        return None
    token = value.strip().split("~", 1)[0]
    match = _MEMORY_HANDLE.search(token)
    if match is None:
        return None
    parsed = parse_handle(match.group())
    return int(parsed[2]) if parsed is not None and not parsed[1] else None


def _replay_handler(store: Store) -> Any:
    """The memory search handler with no embedder wired — the replay is the
    lexical leg only, deterministic and read-only (the salience bump and
    trace row live in ``_search_inner``, not ``_best_body_hits``)."""
    from precis.dispatch import Hub
    from precis.handlers.memory import MemoryHandler

    return MemoryHandler(hub=Hub(store=store))


def _replay_hits(
    store: Store,
    q: str,
    *,
    tags: list[str] | None,
    mode: str | None,
    scope: str | None,
    k: int,
    handler: Any,
) -> list[dict[str, Any]]:
    """One recorded query's ranked memory hits — the index view's own call
    (``hard_cut`` + fused rerank, which needs a vector and so stays lexical
    here) minus its render and bookkeeping."""
    from precis.store import Tag
    from precis.utils.memory_rulings import hidden_ids

    hits, _total, _fold = handler._best_body_hits(
        q,
        Tag.normalize_filter(tags, kind="memory"),
        k,
        page=1,
        mode=mode,
        exclude_ref_ids=sorted(hidden_ids(store)),
        chunk_scope=scope,
        hard_cut=True,
        rerank=True,
    )
    return [
        {
            "rank": i,
            "ref_id": int(ref.id),
            "handle": try_format("memory", ref.id) or f"memory:{ref.id}",
            "title": str(ref.title or ""),
        }
        for i, (_block, ref, _rank) in enumerate(hits, 1)
    ]


def route_replay(
    store: Store, route: Any, *, k: int = ROUTE_REPLAY_K
) -> list[dict[str, Any]]:
    """Replay a ``project:memory`` gripe's recorded route, read-only and
    deterministic (no model, no embedder, no ledger or salience writes).

    Each ``search`` row re-runs its recorded ``q`` (plus ``tags``/``mode``/
    ``scope`` when the masked sample kept them) and records the top-``k``
    hits — a goal node's rank is its position there, "what outranks it" the
    hits above. Each ``get`` row records whether the named handle still
    resolves to a live ref. A row the sample cannot drive (no ``q``, a
    rejected arg) reports ``not_replayed`` with the reason.
    """
    out: list[dict[str, Any]] = []
    handler = None
    for row in route or []:
        if not isinstance(row, dict):
            continue
        verb = str(row.get("verb") or "")
        sample = row.get("arg_sample")
        sample = sample if isinstance(sample, dict) else {}
        seq = row.get("seq")
        if verb == "search":
            entry: dict[str, Any] = {"seq": seq, "verb": "search", "q": sample.get("q")}
            tags = sample.get("tags")
            if isinstance(tags, list) and all(isinstance(t, str) for t in tags):
                entry["tags"] = tags
            if isinstance(sample.get("mode"), str):
                entry["mode"] = sample["mode"]
            if isinstance(sample.get("scope"), str):
                entry["scope"] = sample["scope"]
            q = entry["q"]
            if not isinstance(q, str) or not q.strip():
                entry["not_replayed"] = "no recorded query"
            else:
                if handler is None:
                    handler = _replay_handler(store)
                try:
                    entry["hits"] = _replay_hits(
                        store,
                        q,
                        tags=entry.get("tags"),
                        mode=entry.get("mode"),
                        scope=entry.get("scope"),
                        k=k,
                        handler=handler,
                    )
                except Exception as exc:
                    entry["not_replayed"] = f"{type(exc).__name__}: {exc}"
            out.append(entry)
        elif verb == "get":
            entry = {"seq": seq, "verb": "get", "id": sample.get("id")}
            rid = _handle_ref_id(entry["id"])
            if rid is not None:
                entry["ref_id"] = rid
                entry["resolves"] = rid in _live_ref_rows(store, [rid])
            else:
                entry["resolves"] = False
            out.append(entry)
    return out


def _griped_for_gripe(
    store: Store,
    gid: int,
    title: str,
    story: str,
    meta: dict[str, Any],
    *,
    only_memory: int | None = None,
) -> list[GardenCandidate]:
    """One ``rewrite`` candidate per live memory an open ``project:memory``
    gripe names (``only_memory`` restricts which target a candidate lands
    on — evidence always lists every named memory, so an apply re-check
    re-derives the identical pin).

    The candidate is the whole complaint pinned at once: story, named
    memories and entity refs, ``meta.route``, the deterministic replay and
    every named memory's content sha. ``payload.ops`` is the sentence-grain
    allow-list the evaluator resolves — same contract as the other memory
    rewrites, models supply replacement text only.
    """
    memories, entities = _story_refs(store, story)
    if not memories:
        return []
    route = meta.get("route")
    route = route if isinstance(route, list) else []
    replay = route_replay(store, route)
    mem_ids = [int(m["ref_id"]) for m in memories]
    shas = _ref_shas(store, mem_ids)
    bodies = {rid: b for rid, b, _sha in _memory_bodies(store, mem_ids)}
    listed = [
        {
            "handle": m["handle"],
            "ref_id": m["ref_id"],
            "title": m["title"],
            "sha": shas.get(m["ref_id"]),
        }
        for m in memories
    ]
    for entry in replay:
        hits = entry.get("hits")
        if hits is not None:
            entry["ranks"] = {
                m["handle"]: next(
                    (h["rank"] for h in hits if h["ref_id"] == m["ref_id"]), None
                )
                for m in memories
            }
    common: dict[str, Any] = {
        "gripe": gid,
        "gripe_handle": f"gr{gid}",
        "gripe_title": title,
        "story": story[:GRIPED_STORY_CHARS],
        "memories": listed,
        "entities": entities,
        "route": route,
        "replay": replay,
        "supporting_shas": {
            m["handle"]: shas[m["ref_id"]] for m in memories if m["ref_id"] in shas
        },
    }
    evidence_sha = hashlib.sha256(
        json.dumps(common, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    targets = [
        m for m in memories if only_memory is None or int(m["ref_id"]) == only_memory
    ]
    out: list[GardenCandidate] = []
    for m in targets:
        rid = int(m["ref_id"])
        body = bodies.get(rid)
        if body is None:
            # DREAM memories stay in the merge lane: no rewrite recheck can
            # re-derive this candidate, so it must never reach an apply door.
            continue
        ops = [
            {"find": sentence, "text": None, "span": span}
            for sentence, span in islice(
                _claim_sentences(body, str(m["title"])), GRIPED_OP_CAP
            )
        ]
        out.append(
            GardenCandidate(
                detector="griped",
                action="rewrite",
                target_kind="ref",
                target_id=rid,
                title=f"gr{gid} names me{rid} in a memory complaint",
                evidence={**common, "content_sha": shas.get(rid)},
                payload={
                    "base_sha": shas.get(rid),
                    "ops": ops,
                    "gripe": gid,
                    "evidence_sha": evidence_sha,
                },
            )
        )
    return out


def _griped_memory_target(
    store: Store,
    saved: dict[str, int],
    *,
    scan_cap: int,
    limit: int,
) -> list[GardenCandidate]:
    """Apply re-check mode: ``griped:memory`` windows memory ref ids and
    ``griped:gripe`` pins the story — re-emit the exact candidate for one
    (gripe, memory) pair so a proposal's evidence can be compared."""
    window, _nxt = _scan_window(
        store, "memory", after=saved["griped:memory"], size=scan_cap
    )
    only_gripe = saved.get("griped:gripe")
    gids, _nxt = _griped_window(store, after=0, size=GRIPED_OPEN_SCAN)
    stories = _gripe_story(store, gids)
    out: list[GardenCandidate] = []
    for gid in gids:
        row = stories.get(gid)
        if row is None or (only_gripe is not None and gid != only_gripe):
            continue
        for rid in window:
            if memory_handle_mentions(row[1], f"me{rid}"):
                out.extend(_griped_for_gripe(store, gid, *row, only_memory=rid))
                if len(out) >= limit:
                    return out[:limit]
    return out


def griped_candidates(
    store: Store,
    *,
    scan_cap: int = 2000,
    limit: int = 50,
    cursors: dict[str, int] | None = None,
    next_cursors: dict[str, int] | None = None,
) -> list[GardenCandidate]:
    """Open ``project:memory`` gripes naming live memories → one candidate
    per (gripe, memory). Read-only: the route replay runs the recorded
    searches lexically and pins every named memory's sha.

    A ``griped:memory`` cursor switches to the apply re-check's targeted
    mode (:func:`_griped_memory_target`); the ordinary ``griped:gripe``
    cursor windows open complaints like every other wide scan.
    """
    if limit <= 0 or scan_cap <= 0:
        return []
    saved = cursors or {}
    if "griped:memory" in saved:
        return _griped_memory_target(store, saved, scan_cap=scan_cap, limit=limit)
    key = "griped:gripe"
    window, nxt = _griped_window(store, after=saved.get(key, 0), size=scan_cap)
    stories = _gripe_story(store, window)
    out: list[GardenCandidate] = []
    for gid in window:
        row = stories.get(gid)
        if row is None:
            continue
        out.extend(_griped_for_gripe(store, gid, *row))
        if len(out) >= limit:
            nxt = gid
            break
    if next_cursors is not None:
        next_cursors[key] = nxt
    return out[:limit]


def _kind_count(store: Store, kind: str) -> int:
    return store.count_refs(kind=kind)


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
    Proposals only. The consumer applies one only past
    :func:`~precis.graph.garden.paper_gate.paper_identity` (gr512125):
    title, year and author agreement here is not identity evidence.
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
    out: list[str] = []
    if "all" in passes:
        for p, detectors in _DETECTOR_BY_PASS.items():
            if p not in OPT_IN_PASSES:
                out.extend(detectors)
    for p in passes:
        out.extend(_DETECTOR_BY_PASS.get(p, ()))
    return sorted(set(out))


@dataclass(slots=True)
class DetectorRun:
    """One :func:`run_detectors` pass: the concatenated candidates plus
    ``skipped`` — the names of detectors that hit their statement timeout
    or wall-clock budget and were skipped so the rest could still run —
    ``cursors``, the scan positions to save for the next run (only from
    detectors that finished) — and ``skipped_considered``, candidates
    dropped because their target was considered-and-unchanged inside the
    review window."""

    candidates: list[GardenCandidate] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Unavailable evidence sources, separate from verified findings.
    not_checked: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, int] = field(default_factory=dict)
    skipped_considered: int = 0


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
    considered_days: int = CONSIDERED_DAYS,
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
        unchecked: dict[str, str] = {}
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
        elif det in {
            "anchor_rot",
            "compact",
            "dangling_mention",
            "entity_claim_stale",
            "griped",
            "host_facts",
            "negative_undated",
            "review_due",
            "source_drift",
            "thread_shape",
            "title_shape",
        }:
            fn = partial(
                {
                    "anchor_rot": anchor_rot_candidates,
                    "compact": compact_candidates,
                    "dangling_mention": dangling_mention_candidates,
                    "entity_claim_stale": entity_claim_stale_candidates,
                    "griped": griped_candidates,
                    "host_facts": host_facts_candidates,
                    "negative_undated": negative_undated_candidates,
                    "review_due": review_due_candidates,
                    "source_drift": source_drift_candidates,
                    "thread_shape": thread_shape_candidates,
                    "title_shape": title_shape_candidates,
                }[det],
                store,
                scan_cap=scan_cap,
                limit=limit,
                cursors=saved,
                next_cursors=nxt,
            )
        elif det in {"restated_claim", "grab_bag"}:
            fn = partial(
                {
                    "restated_claim": restated_claim_candidates,
                    "grab_bag": grab_bag_candidates,
                }[det],
                store,
                scan_cap=scan_cap,
                limit=limit,
                cursors=saved,
                next_cursors=nxt,
                not_checked=unchecked,
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
        out.not_checked.update(unchecked)
    if considered_days > 0 and out.candidates:
        fresh = freshly_considered(
            store,
            [c.target_id for c in out.candidates if c.target_kind == "ref"],
            [c.target_id for c in out.candidates if c.target_kind == "link"],
            days=considered_days,
        )
        # review_due already checks the SHA, checklist version, explicit
        # expiry and type-specific age. The legacy 30-day skip knows neither
        # version nor expiry and would hide valid review debt.
        kept = [
            c
            for c in out.candidates
            if c.detector == "review_due" or (c.target_kind, c.target_id) not in fresh
        ]
        out.skipped_considered = len(out.candidates) - len(kept)
        out.candidates = kept
    return out


__all__ = [
    "CONSIDERED_DAYS",
    "DETECTOR_DEADLINE_S",
    "DUP_MAX_DISTANCE",
    "DUP_MIN_LEXICAL",
    "DUP_VECTOR_CHUNK",
    "GARDEN_KINDS",
    "HISTORY_KINDS",
    "MEMORY_REVIEW_MATCH_CAP",
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
    "anchor_rot_candidates",
    "compact_candidates",
    "cycle_edge_candidates",
    "dangling_edge_candidates",
    "dangling_mention_candidates",
    "detectors_for",
    "dup_pair_qualifies",
    "dup_window_size",
    "duplicate_candidates",
    "entity_claim_stale_candidates",
    "freshly_considered",
    "grab_bag_candidates",
    "griped_candidates",
    "host_facts_candidates",
    "memory_expiry_shape",
    "negative_undated_candidates",
    "orphan_taxon_candidates",
    "paper_identity_candidates",
    "polysemy_candidates",
    "redundant_link_candidates",
    "restated_claim_candidates",
    "review_due_candidates",
    "route_replay",
    "run_detectors",
    "short_sibling_pair",
    "sibling_confusion_candidates",
    "source_drift_candidates",
    "stale_candidates",
    "thread_shape_candidates",
    "title_shape_candidates",
    "untagged_candidates",
]
