"""inbound_ground — a newly ingested paper is checked against the claim set.

Part (b) of ``docs/backlog/taproot-inbound-grounding.md``: the claim-hub
variant of inbound completeness. ``workers/inbound_chase.py`` (part a)
finds support along the *citation graph* — who cites this paper — and so
cannot see a paper that supports a claim without any citation edge to it.
This pass turns the question around: the moment a paper's body is
embedded, which existing claim hubs does it speak to, and does it support
or deny them? Evidence found this way needs no citation path at all.

Shape: the reverse-ANN probe ``workers/chase_trigger.py`` already runs
(paper chunks against the tiny ``claim_embeddings`` index), but
**paper-grained and verdict-bearing**: where ``chase_trigger`` marks a hub
``TAPROOT_DUE`` for ``hub_refine`` to pick up later, this pass verifies
each (paper, hub) pair itself and writes the edge — the paper lands, the
graph answers. The two coexist: a hub this pass attaches is in
``hub_refine``'s attached-source precheck (and in the shared rejection
memo), so a later refine never re-spends on the same pair.

One pass, per claimed paper:

1. **Claim** (:func:`_claim_papers`) — up to ``papers_per_pass`` live
   ``paper`` refs that (i) have at least one embeddable body chunk, (ii)
   have *every* embeddable body chunk embedded (``chunk_embeddings.status
   = 'ok'`` for the pass embedder — "finished ingest"), (iii) carry no
   ``INBOUND_GROUND:<version>`` ref tag (the ``classify``/``chase_trigger``
   done-marker idiom: the tag IS the claim), and (iv) were created inside
   the :func:`_max_age_days` window. Newest first, ``FOR UPDATE SKIP
   LOCKED``. The age window is what makes this *inbound*: enabling the
   pass sweeps the recent intake, not the corpus — the full papers x claims
   backfill is part (c), a separate batch backstop.
2. **Match** (:func:`_near_hubs`) — one set-based query crosses the paper's
   embedded body chunks against ``claim_embeddings`` (flat scan; migration
   0101 sizes the table as trivial), keeps the few nearest chunks per hub
   within the :func:`_min_sim_default` cosine-distance floor, drops
   composite and hypothesis hubs (same exclusions ``chase_trigger`` and
   ``hub_refine`` apply), then in Python picks each hub's nearest chunk
   that is evidence-eligible (:func:`taproot.grounding.has_grounding_prose`,
   not a hearsay section) and ranks hubs by that distance. **Top-k hubs**
   (:func:`_topk_default`, default 5) survive — the first cost bound.
3. **Filter** — a hub already joined to this paper by an evidence role or
   a ``disputes`` edge, or whose rejection memo (``meta.taproot_rejected``,
   shared with ``hub_refine``) already names this paper, is skipped before
   any LLM spend.
4. **Verify** — the sanctioned shared seam
   ``workers/_chase_llm._verify_support_with_caveats`` (never forked; it
   dispatches through ``utils/llm/router.route``), with the chunk's
   neighbours, section path and the paper's identity block. At most
   :func:`_max_llm_default` calls per paper (default 5) — the second cost
   bound; a per-pass bound of ``papers_per_pass x max_llm`` follows.
   :func:`classify_verdict` folds the verifier's ``supports``/``contradicts``
   into the three-way vocabulary this pass acts on: ``support`` (the
   corroboration rule ``_chase_llm.is_corroborating`` — ``yes``, or a
   scoping ``partial``), ``deny`` (``contradicts`` true), ``neutral``
   (on-topic, doesn't test the claim).
5. **Write** — ``support`` → ``taproot.hub.attach_evidence`` (role
   ``corroborates``, never ``establishes``: originators are derived at read
   time), with the full verified stamp (``support``/``support_reason``/
   ``caveats``/``source_handle``/``verified_by``/``verified_at``/
   ``verified_claim_sha``) so the edge is born certified, not withheld.
   ``deny`` → a non-blocking ``disputes`` link (``meta.via =
   'inbound_ground'``), filed only when the verifier also says the
   passage is the same setup and terminal (the 2026-10-03 citation rule
   ``hub_refine._disputes_allowed`` applies; a cross-setup or recited
   contradiction is memo-only). No demotion is queued here — that is
   ``hub_refine``'s move, on its own judge path. ``deny`` and ``neutral``
   both land in the hub's rejection memo (``via: 'inbound_ground'``) so
   neither this pass nor ``hub_refine`` judges the pair again.
6. **Mark** — the paper gets the ``INBOUND_GROUND:<version>`` tag and a
   ``ref_events`` row (``source='inbound_ground'``, ``event='grounded'``)
   in the same transaction as every write above, so a mid-paper failure
   rolls back whole and re-claims next pass. A verify call that *failed*
   (dispatch error, ``None``) leaves the paper unmarked: settled pairs stay
   settled through the filter in step 3, so the retry re-spends only on
   the pair that errored. Bump :data:`INBOUND_GROUND_VERSION` to lazily
   re-ground every in-window paper.

Retraction checks (``attach_evidence`` trigger 1) are collected via
``pending_checks`` and drained after the commit (``run_retraction_checks``)
— never inside the held transaction.

Ship dark: a **service**, flipped with ``precis service prio <host>
inbound_ground 1`` (live, no redeploy; ``PRECIS_INBOUND_GROUND_ENABLED`` is
the seed-time name only, per §L). Enable on exactly **one host**: the
rejection memo is a read-modify-write on hub ``meta`` (this pass takes a
row lock on the hub for the write, but ``hub_refine`` does not). Needs an
embedder (claim-embedding refresh reuses ``chase_trigger``'s step (a), so
the pass is self-sufficient when ``chase_trigger`` is dark); absent one,
the pass logs and no-ops the cycle.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.store.types import Tag
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    claim_hub_predicate_sql,
    claim_sha,
    not_hypothesis_predicate_sql,
)
from precis.taproot.claim_type import widenable_predicate_sql
from precis.taproot.grounding import has_grounding_prose
from precis.taproot.hub import (
    HUB_ROLES,
    META_REJECTED,
    attach_evidence,
    run_retraction_checks,
)
from precis.taproot.reground import is_hearsay_section
from precis.utils import handle_registry
from precis.utils.relations import validate_relation
from precis.workers._chase_llm import (
    SourceIdentity,
    _verify_support_with_caveats,
    is_corroborating,
)
from precis.workers.chase_trigger import _refresh_claim_embeddings

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The claim-hub and not-a-hypothesis predicates (``taproot.canon`` — the
#: single definition), re-applied at match time: ``claim_embeddings`` is
#: refreshed under them, but a hub demoted since its row was written must
#: not reach ``attach_evidence`` (which would raise and roll the paper back).
_CLAIM_HUB_SQL = claim_hub_predicate_sql()
_NOT_HYPOTHESIS_SQL = not_hypothesis_predicate_sql()
#: The "not a landscape sentence" clause (``taproot.claim_type`` policy
#: ``widen=False``). A landscape hub states the common case for a whole
#: class of systems, so it is an embedding attractor: fi449493 collected
#: 14 far-field edges from this arm. Never widened.
_WIDENABLE_SQL = widenable_predicate_sql()

__all__ = [
    "INBOUND_GROUND_VERSION",
    "VerifyFn",
    "classify_verdict",
    "run_inbound_ground_pass",
]

#: Bump to lazily re-ground every paper inside the age window.
INBOUND_GROUND_VERSION = "1"
#: The done-marker ref tag: ``INBOUND_GROUND:<version>`` on a grounded paper.
_MARKER_NS = "INBOUND_GROUND"

#: ``ref_events.source`` / the ``via`` and ``verified_by`` fingerprint on
#: every edge and memo entry this pass writes — distinct from
#: ``hub_refine``'s ``'hub-refine'`` and ``verify_edges``'s ``'verify-edges'``
#: so a verdict is always traceable to the judge that issued it.
_SOURCE = "inbound_ground"
_VERIFIED_BY = "inbound-ground"

#: The one evidence role this pass attaches with (see module docstring).
_ROLE = "corroborates"

#: Nearest chunks per hub fetched from SQL before the Python eligibility
#: filter — enough that a front-matter or bibliography nearest chunk does
#: not cost the hub its slot when a real body passage sits just behind it.
_CHUNKS_PER_HUB = 3

#: Chunk kinds that never ground evidence (the embed worker skips them too).
_SKIP_CHUNK_KINDS: tuple[str, ...] = ("references", "field")

#: Verifier hook signature (``_verify_support_with_caveats``; a stub in
#: tests). Returns the parsed verdict dict, or ``None`` on dispatch failure.
VerifyFn = Callable[..., "dict[str, Any] | None"]


# ── knobs ───────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _topk_default() -> int:
    """``PRECIS_INBOUND_GROUND_TOPK`` — hubs verified per paper (default 5)."""
    return _env_int("PRECIS_INBOUND_GROUND_TOPK", 5)


def _max_llm_default() -> int:
    """``PRECIS_INBOUND_GROUND_MAX_LLM`` — verifier calls per paper (default 5)."""
    return _env_int("PRECIS_INBOUND_GROUND_MAX_LLM", 5)


def _papers_per_pass_default() -> int:
    """``PRECIS_INBOUND_GROUND_PAPERS_PER_PASS`` — papers claimed per pass."""
    return _env_int("PRECIS_INBOUND_GROUND_PAPERS_PER_PASS", 4)


def _max_age_days() -> int:
    """``PRECIS_INBOUND_GROUND_MAX_AGE_DAYS`` — only papers created inside this
    window are candidates (default 30); older ones belong to the batch
    backfill, part (c)."""
    return _env_int("PRECIS_INBOUND_GROUND_MAX_AGE_DAYS", 30)


def _min_sim_default() -> float:
    """``PRECIS_INBOUND_GROUND_MIN_SIM`` — cosine-distance floor (default 0.45,
    the same floor ``chase_trigger`` probes with)."""
    raw = os.environ.get("PRECIS_INBOUND_GROUND_MIN_SIM")
    if raw is None or not raw.strip():
        return 0.45
    try:
        return float(raw)
    except ValueError:
        return 0.45


# ── (1) claim ───────────────────────────────────────────────────────────


def _claim_papers(
    conn: Connection, *, embedder_model: str, limit: int, max_age_days: int
) -> list[int]:
    """Lock and return up to ``limit`` paper ref_ids that finished embedding
    inside the age window and carry no done-marker (module docstring step 1)."""
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.retired_at IS NULL
           AND r.created_at >= now() - make_interval(days => %(max_age)s)
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(ns)s AND t.value = %(ver)s
               )
           AND EXISTS (
                 SELECT 1 FROM chunks c
                  WHERE c.ref_id = r.ref_id AND c.ord >= 0
                    AND c.retired_at IS NULL
                    AND c.chunk_kind <> ALL(%(skip_kinds)s)
                    AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
               )
           AND NOT EXISTS (
                 SELECT 1 FROM chunks c
                  WHERE c.ref_id = r.ref_id AND c.ord >= 0
                    AND c.retired_at IS NULL
                    AND c.chunk_kind <> ALL(%(skip_kinds)s)
                    AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
                    AND NOT EXISTS (
                          SELECT 1 FROM chunk_embeddings ce
                           WHERE ce.chunk_id = c.chunk_id
                             AND ce.embedder = %(embedder)s
                             AND ce.status = 'ok'
                        )
               )
         ORDER BY r.created_at DESC, r.ref_id DESC
         LIMIT %(limit)s
           FOR UPDATE OF r SKIP LOCKED
        """,
        {
            "embedder": embedder_model,
            "ns": _MARKER_NS,
            "ver": INBOUND_GROUND_VERSION,
            "skip_kinds": list(_SKIP_CHUNK_KINDS),
            "max_age": max_age_days,
            "limit": limit,
        },
    ).fetchall()
    return [int(r[0]) for r in rows]


# ── (2) match ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _HubMatch:
    hub_ref_id: int
    title: str
    scope: dict[str, str]
    rejected: dict[str, Any]
    chunk_id: int
    chunk_ord: int
    chunk_text: str
    section_path: str | None
    distance: float


def _near_hubs(
    conn: Connection,
    paper_ref_id: int,
    *,
    embedder_model: str,
    floor: float,
    topk: int,
) -> list[_HubMatch]:
    """The ``topk`` claim hubs nearest to any evidence-eligible body chunk of
    ``paper_ref_id``, each paired with that chunk (module docstring step 2)."""
    rows = conn.execute(
        f"""
        WITH near AS (
            SELECT cl.hub_ref_id, c.chunk_id, c.ord, c.text,
                   array_to_string(c.section_path, ' > ') AS section_path,
                   (cl.vector <=> ce.vector) AS dist,
                   row_number() OVER (
                       PARTITION BY cl.hub_ref_id
                       ORDER BY (cl.vector <=> ce.vector), c.ord
                   ) AS rn
              FROM chunks c
              JOIN chunk_embeddings ce
                ON ce.chunk_id = c.chunk_id
               AND ce.embedder = %(embedder)s
               AND ce.status = 'ok'
              JOIN claim_embeddings cl
                ON cl.embedder = %(embedder)s
               AND cl.vector IS NOT NULL
               AND (cl.vector <=> ce.vector) <= %(floor)s
             WHERE c.ref_id = %(paper)s
               AND c.ord >= 0
               AND c.retired_at IS NULL
               AND c.chunk_kind <> ALL(%(skip_kinds)s)
        )
        SELECT n.hub_ref_id, r.title, r.meta, n.chunk_id, n.ord, n.text,
               n.section_path, n.dist
          FROM near n
          JOIN refs r ON r.ref_id = n.hub_ref_id
         WHERE n.rn <= {_CHUNKS_PER_HUB}
           AND r.retired_at IS NULL
           AND {_CLAIM_HUB_SQL}
           AND {_NOT_HYPOTHESIS_SQL}
           AND {_WIDENABLE_SQL}
           AND NOT EXISTS (
                 SELECT 1 FROM links l
                  JOIN refs a ON a.ref_id = l.src_ref_id
                 WHERE l.dst_ref_id = n.hub_ref_id
                   AND l.relation = 'conjunct-of'
                   AND a.kind = 'finding'
                   AND a.retired_at IS NULL
               )
         ORDER BY n.hub_ref_id, n.dist, n.ord
        """,
        {
            "embedder": embedder_model,
            "floor": floor,
            "paper": paper_ref_id,
            "skip_kinds": list(_SKIP_CHUNK_KINDS),
            **CLAIM_HUB_PREDICATE_PARAMS,
            **NOT_HYPOTHESIS_PREDICATE_PARAMS,
        },
    ).fetchall()

    best: dict[int, _HubMatch] = {}
    for hub_id, title, meta, chunk_id, ord_, text, section_path, dist in rows:
        hub_id = int(hub_id)
        if hub_id in best:
            continue
        text_str = str(text or "")
        section = str(section_path) if section_path else None
        if is_hearsay_section(section) or not has_grounding_prose(text_str):
            continue
        meta_d = dict(meta or {})
        best[hub_id] = _HubMatch(
            hub_ref_id=hub_id,
            title=str(title or "").strip(),
            scope={str(k): str(v) for k, v in (meta_d.get("scope") or {}).items()},
            rejected=dict(meta_d.get(META_REJECTED) or {}),
            chunk_id=int(chunk_id),
            chunk_ord=int(ord_),
            chunk_text=text_str,
            section_path=section,
            distance=float(dist),
        )
    ranked = sorted(best.values(), key=lambda m: (m.distance, m.hub_ref_id))
    return [m for m in ranked if m.title][:topk]


# ── (3) filter ──────────────────────────────────────────────────────────


def _settled_hub_ids(
    conn: Connection, paper_ref_id: int, hub_ids: list[int]
) -> set[int]:
    """Hubs already joined to the paper by an evidence role or ``disputes``."""
    if not hub_ids:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT dst_ref_id FROM links "
        "WHERE src_ref_id = %s AND dst_ref_id = ANY(%s) AND relation = ANY(%s)",
        (paper_ref_id, hub_ids, [*HUB_ROLES, "disputes"]),
    ).fetchall()
    return {int(r[0]) for r in rows}


# ── (4) verify ──────────────────────────────────────────────────────────


def classify_verdict(verification: dict[str, Any]) -> str:
    """Fold a verifier verdict into ``support`` / ``deny`` / ``neutral``.

    ``support`` is exactly ``_chase_llm.is_corroborating`` (a ``yes``, or a
    ``partial`` that scopes rather than negates); ``deny`` is a
    ``contradicts`` verdict that is not corroborating; everything else is
    ``neutral`` — the passage shares the topic but does not test the claim.
    """
    if is_corroborating(verification):
        return "support"
    if bool(verification.get("contradicts")):
        return "deny"
    return "neutral"


def _disputes_allowed(verification: dict[str, Any]) -> bool:
    """The write gate for a ``disputes`` filing — the verifier must say
    ``contradicts`` AND same setup AND a terminal passage, the rule
    ``hub_refine._disputes_allowed`` applies on the widen arm (re-derived
    here so this pass does not import that module's privates)."""
    return (
        bool(verification.get("contradicts"))
        and verification.get("same_setup") is True
        and verification.get("terminal") is True
    )


def _paper_identity(conn: Connection, ref_id: int) -> SourceIdentity:
    row = conn.execute(
        "SELECT title, year, left(meta->>'abstract', 600) FROM refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    if row is None:
        return SourceIdentity()
    return SourceIdentity(
        title=str(row[0]) if row[0] else None,
        year=int(row[1]) if row[1] else None,
        abstract=str(row[2]) if row[2] else None,
    )


def _chunk_neighbours(conn: Connection, ref_id: int, ord_: int) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM chunks WHERE ref_id = %s AND ord = ANY(%s) "
        "AND retired_at IS NULL AND ord >= 0 ORDER BY ord",
        (ref_id, [ord_ - 1, ord_ + 1]),
    ).fetchall()
    return [str(r[0] or "") for r in rows]


# ── (5) write ───────────────────────────────────────────────────────────


def _memo_rejection(
    conn: Connection,
    *,
    hub: _HubMatch,
    paper_ref_id: int,
    verdict: str,
    verification: dict[str, Any],
) -> None:
    """Append this paper to the hub's rejection memo (``meta.taproot_rejected``,
    the key ``hub_refine`` reads) under a row lock, so the pair is judged
    once by either pass."""
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (hub.hub_ref_id,))
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "supports": verification.get("supports"),
        "contradicts": bool(verification.get("contradicts")),
        "same_setup": verification.get("same_setup"),
        "terminal": verification.get("terminal"),
        "support_reason": verification.get("support_reason"),
        "verdict": verdict,
        "via": _SOURCE,
    }
    conn.execute(
        """
        UPDATE refs
           SET meta = jsonb_set(
                   meta,
                   %(path)s,
                   COALESCE(meta -> %(key)s, '{}'::jsonb) || %(entry)s,
                   true
               ),
               updated_at = now()
         WHERE ref_id = %(hub)s
        """,
        {
            "path": [META_REJECTED],
            "key": META_REJECTED,
            "entry": Jsonb({str(paper_ref_id): entry}),
            "hub": hub.hub_ref_id,
        },
    )


@dataclass
class _PaperResult:
    matched: int = 0
    verified: int = 0
    attached: int = 0
    disputes: int = 0
    llm_errors: int = 0


def _ground_one_paper(
    conn: Connection,
    store: Store,
    paper_ref_id: int,
    *,
    embedder_model: str,
    floor: float,
    topk: int,
    max_llm: int,
    verify_fn: VerifyFn,
    pending_checks: list[int],
) -> _PaperResult:
    """Match → filter → verify → write → mark, for one paper, inside the
    caller's transaction (module docstring steps 2-6). Re-locks the paper
    row and re-checks the done-marker first: the claim query's lock was
    released at its commit, so a sibling instance may have grounded the
    paper in between."""
    result = _PaperResult()
    conn.execute("SELECT 1 FROM refs WHERE ref_id = %s FOR UPDATE", (paper_ref_id,))
    if store.has_tag(paper_ref_id, _MARKER_NS, INBOUND_GROUND_VERSION):
        return result
    matches = _near_hubs(
        conn, paper_ref_id, embedder_model=embedder_model, floor=floor, topk=topk
    )
    result.matched = len(matches)
    settled = _settled_hub_ids(conn, paper_ref_id, [m.hub_ref_id for m in matches])
    identity = _paper_identity(conn, paper_ref_id)
    paper_key = f"paper:{paper_ref_id}"

    for m in matches:
        if result.verified >= max_llm:
            break
        if m.hub_ref_id in settled or str(paper_ref_id) in m.rejected:
            continue
        result.verified += 1
        try:
            verification = verify_fn(
                claim=m.title,
                scope=m.scope,
                target_cite_key=paper_key,
                target_chunk_ord=m.chunk_ord,
                target_chunk_text=m.chunk_text,
                source_kind="paper",
                section_path=m.section_path,
                neighbours=_chunk_neighbours(conn, paper_ref_id, m.chunk_ord),
                source_identity=identity,
            )
        except Exception:
            log.warning(
                "inbound_ground: verify hook raised for paper #%d hub #%d",
                paper_ref_id,
                m.hub_ref_id,
                exc_info=True,
            )
            verification = None
        if verification is None:
            result.llm_errors += 1
            continue

        verdict = classify_verdict(verification)
        handle = handle_registry.try_format("paper", m.chunk_id, chunk=True)
        sha = claim_sha(m.title)
        if verdict == "support":
            attach_evidence(
                store,
                hub_ref_id=m.hub_ref_id,
                paper_ref_id=paper_ref_id,
                role=_ROLE,
                meta={
                    "support": verification.get("supports"),
                    "support_reason": verification.get("support_reason"),
                    "caveats": list(verification.get("caveats") or []),
                    "source_handle": handle,
                    "verified_by": _VERIFIED_BY,
                    "verified_at": datetime.now(UTC).isoformat(),
                    "verified_claim_sha": sha,
                    _SOURCE: {
                        "version": INBOUND_GROUND_VERSION,
                        "distance": round(m.distance, 4),
                        "same_setup": verification.get("same_setup"),
                        "terminal": verification.get("terminal"),
                    },
                },
                set_by="system",
                conn=conn,
                pending_checks=pending_checks,
            )
            result.attached += 1
            continue

        _memo_rejection(
            conn,
            hub=m,
            paper_ref_id=paper_ref_id,
            verdict=verdict,
            verification=verification,
        )
        if verdict == "deny" and _disputes_allowed(verification):
            store.add_link(
                src_ref_id=paper_ref_id,
                dst_ref_id=m.hub_ref_id,
                relation=validate_relation("disputes", store=store),
                set_by="system",
                meta={
                    "support": "no",
                    "support_reason": verification.get("support_reason"),
                    "caveats": list(verification.get("caveats") or []),
                    "source_handle": handle,
                    "via": _SOURCE,
                    "verified_claim_sha": sha,
                },
                conn=conn,
            )
            result.disputes += 1

    if result.llm_errors == 0:
        store.add_tag(
            paper_ref_id,
            Tag.closed(_MARKER_NS, INBOUND_GROUND_VERSION),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    store.append_event(
        paper_ref_id,
        source=_SOURCE,
        event="grounded" if result.llm_errors == 0 else "grounded_partial",
        payload={
            "hubs_matched": result.matched,
            "verified": result.verified,
            "attached": result.attached,
            "disputes": result.disputes,
            "llm_errors": result.llm_errors,
            "version": INBOUND_GROUND_VERSION,
        },
        conn=conn,
    )
    return result


# ── runner ──────────────────────────────────────────────────────────────


def run_inbound_ground_pass(
    store: Store,
    *,
    embedder: Any | None,
    limit: int | None = None,
    topk: int | None = None,
    max_llm: int | None = None,
    min_sim: float | None = None,
    max_age_days: int | None = None,
    verify_fn: VerifyFn | None = None,
) -> dict[str, int]:
    """One pass: refresh claim embeddings, claim up to ``limit`` freshly
    embedded papers, ground each against its nearest claim hubs.

    Every keyword defaults to its ``PRECIS_INBOUND_GROUND_*`` env knob when
    omitted (tests pass them explicitly). ``verify_fn`` is the injectable
    LLM seam (default ``_chase_llm._verify_support_with_caveats``).
    ``embedder=None`` degrades the whole pass to a logged no-op.

    Returns ``{claimed, ok, failed, hubs_matched, verified, attached,
    disputes, llm_errors}`` — the first three are the standard
    ``BatchResult`` shape (``claimed == ok + failed``).
    """
    result = {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
        "hubs_matched": 0,
        "verified": 0,
        "attached": 0,
        "disputes": 0,
        "llm_errors": 0,
    }
    if embedder is None:
        log.warning("inbound_ground: no embedder available -- pass no-ops this cycle")
        return result

    embedder_model = str(embedder.model)
    papers_limit = limit if limit is not None else _papers_per_pass_default()
    resolved_topk = topk if topk is not None else _topk_default()
    resolved_max_llm = max_llm if max_llm is not None else _max_llm_default()
    resolved_floor = min_sim if min_sim is not None else _min_sim_default()
    resolved_age = max_age_days if max_age_days is not None else _max_age_days()
    verify = verify_fn or _verify_support_with_caveats

    try:
        with store.pool.connection() as conn:
            _refresh_claim_embeddings(conn, embedder, embedder_model, limit=64)
            conn.commit()
    except Exception:
        log.warning("inbound_ground: claim-embedding refresh failed", exc_info=True)

    with store.pool.connection() as conn:
        papers = _claim_papers(
            conn,
            embedder_model=embedder_model,
            limit=papers_limit,
            max_age_days=resolved_age,
        )
        conn.commit()
    result["claimed"] = len(papers)

    for paper_ref_id in papers:
        pending_checks: list[int] = []
        try:
            with store.pool.connection() as conn:
                r = _ground_one_paper(
                    conn,
                    store,
                    paper_ref_id,
                    embedder_model=embedder_model,
                    floor=resolved_floor,
                    topk=resolved_topk,
                    max_llm=resolved_max_llm,
                    verify_fn=verify,
                    pending_checks=pending_checks,
                )
                conn.commit()
        except Exception:
            log.warning(
                "inbound_ground: grounding failed for paper #%d -- rolled back",
                paper_ref_id,
                exc_info=True,
            )
            result["failed"] += 1
            continue
        result["ok"] += 1
        result["hubs_matched"] += r.matched
        result["verified"] += r.verified
        result["attached"] += r.attached
        result["disputes"] += r.disputes
        result["llm_errors"] += r.llm_errors
        if pending_checks:
            run_retraction_checks(store, pending_checks)

    return result
