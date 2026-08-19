"""Taproot Phase 2 — the single write door for claim hubs + evidence edges.

Build ticket: ``docs/backlog/taproot-phase2-hub-node.md``; governance:
Taproot evidence relations; design: ``docs/backlog/taproot.md`` §"The core model".

**Single write path (open #16).** Every hub-finding and every
``establishes``/``corroborates``/``contradicts`` evidence edge is written
through this module. A raw ``INSERT`` / ``store.add_link`` for these
relations elsewhere bypasses the vocabulary + ``TAPROOT:claim`` guards below
and is a defect — the exact silent-junk-edge error taproot exists to prevent.

Five functions:

1. :func:`mint_hub` — create a ``TAPROOT:claim`` ``finding`` hub for a
   paper-grounded claim (open #15: only paper-sourced claims become hubs).
2. :func:`attach_evidence` — write one ``paper --role--> hub`` edge, ``role``
   in :data:`HUB_ROLES`, guarding the target is actually a claim hub, is
   NOT a **compound** hub (docs/backlog/taproot-atomic-claims.md step 3 —
   evidence attaches only to atoms), and the source is an evidence-source
   ref (:data:`EVIDENCE_SRC_KINDS` backstop). Also the single choke point
   for the deterministic prophetic-example caveat (patent-evidence-parity
   phase 4): a patent source whose grounding chunk carries
   ``PATENT_EXAMPLE:prophetic`` (``data/axes/patent_example.yaml``) gets
   :data:`PROPHETIC_EXAMPLE_CAVEAT` appended to ``meta.caveats`` here,
   mechanically — never via the verify LLM prompt, which is unchanged.
3. :func:`apply_placement` — bridge a :class:`~precis.taproot.canon.Placement`
   (the canonicalizer's verdict) to the writes above; a ``needs_review``
   placement files a ``kind='todo'`` (via an injected ``todo_fn``) and never
   auto-attaches (open #16); an ``attach`` onto a compound hub downgrades to
   the same ``needs_review`` path rather than raising.
4. :func:`link_claims` — the claim->claim advisory ``refines``/``conjunct-of``
   edges (:data:`CLAIM_LINK_RELATIONS`, migrations 0100/0126): link-don't-merge,
   carries no evidence flow.
5. :func:`apply_extraction` — the decomposition-aware orchestrator over a
   full :class:`~precis.taproot.canon.ClaimExtraction`: atoms through
   :func:`apply_placement`, the compound minted/converged with no evidence
   edge, ``conjunct-of`` links between them, and the not-a-claim audit memo
   (step 8) on the compound's ``meta``.

Callers that populate the edges: ``workers/chase.py::_taproot_bridge`` (the
forward bridge, supplies the verdict ``meta``), ``workers/hub_refine.py``,
and the authoring/backfill doors (:mod:`precis.taproot.authoring` /
:mod:`precis.taproot.backfill`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg.errors import UniqueViolation

from precis.errors import BadInput
from precis.handlers._link_tag_ops import validate_relation
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.ingest.provenance import check_ref_retraction
from precis.store.types import ActorSlug, BlockInsert, Tag
from precis.taproot.canon import (
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    CanonicalClaim,
    NotClaim,
    Placement,
    claim_sha,
)
from precis.taproot.notation import lint_notation

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The three evidence-edge roles a ``paper --> hub`` link may carry
#: (taproot.md Axis A). ``establishes`` = originator (migration 0094);
#: ``corroborates`` (0085) and ``contradicts`` (0001) reuse existing slugs —
#: endpoint kinds disambiguate. Each is still validated through
#: :func:`validate_relation` against the live ``relations`` table before write.
HUB_ROLES: frozenset[str] = frozenset({"establishes", "corroborates", "contradicts"})

#: Claim→claim advisory link relations a hub may carry to ANOTHER hub
#: (migration 0100, taproot evidence relations amendment; migration 0126,
#: taproot-atomic-claims). ``refines`` = "source hub is a sharper/reworded
#: version of the target hub"; ``conjunct-of`` = "source hub is one atomic
#: conjunct of the target compound hub" (docs/backlog/taproot-atomic-claims.md)
#: — both link-don't-merge, NO evidence flow (each hub keeps its own
#: paper→hub edges). Written through :func:`link_claims` (the single write
#: door), distinct from the paper→hub evidence edges of :data:`HUB_ROLES`.
CLAIM_LINK_RELATIONS: frozenset[str] = frozenset({"refines", "conjunct-of"})

#: The default role :func:`apply_placement` attaches with. ``corroborates`` is
#: the *safe* assumption — never falsely claim a paper is the originator.
#: Promotion to ``establishes`` is a derivation over the citation graph
#: (taproot.md §"Seniority is derived", Phase 2c/3), not a write-time guess.
_DEFAULT_ROLE = "corroborates"

#: :func:`attach_evidence`'s src-kind guard (open #15: only paper-sourced
#: claims get evidence) — defense-in-depth behind
#: :func:`precis.taproot.authoring.resolve_paper_ref_id`'s authoritative
#: check, for any caller that reaches this door directly. The single
#: definition (:mod:`precis.taproot.seniority`'s read side and
#: :mod:`precis.taproot.authoring`'s ``_SUPPORTER_KINDS`` both import this
#: rather than each keeping their own copy — the three-way hand-duplication
#: this replaced was exactly the "KindSpec facts re-hardcoded downstream"
#: drift class).
#:
#: **Deliberately NOT derived from ``KindSpec.corpus_role``.** Every kind
#: flagged ``corpus_role="evidence"`` is ``{paper, patent, datasheet,
#: edgar}`` — a pure derivation would silently widen what counts as
#: scientific-claim evidence, which is a scope call on what "evidence"
#: means for a Taproot claim hub (taproot.md open #15: "only paper-sourced
#: claims become hubs"; the patent addition itself came from a deliberate
#: design doc, docs/backlog/patent-evidence-parity.md; ``edgar`` was
#: approved as an evidence source by a matching human call, as was
#: ``datasheet`` — a manufacturer datasheet is a primary technical document
#: the same way a patent is), not a mechanical fact this codebase already
#: declared elsewhere. The set currently *equals* the ``corpus_role``
#: derivation, but a future evidence-flagged kind still joins here only by
#: human call — widening stays a decision, and
#: ``tests/test_kind_totality.py`` pins the two sets against each other so
#: divergence in either direction is a visible failure, not drift. See also
#: :mod:`precis.taproot.seniority`'s read-query docstring, which needs the
#: same set to stay in lock-step with whatever this evolves to.
EVIDENCE_SRC_KINDS: frozenset[str] = frozenset(
    {"paper", "patent", "edgar", "datasheet"}
)

_STATUS_NS = "STATUS"
_STATUS_CANONICAL = "canonical"


class TitleRoundTripError(RuntimeError):
    """Raised when a just-written ``refs.title`` doesn't read back byte-for-
    byte equal to the sentence the write door intended to persist.

    The failure mode ``docs/backlog/hub-title-200-truncation-via-stale-mcp.md``
    shipped silently for three weeks: a stale MCP build was serving a
    handler from before a title-length-cap removal, so every hub it minted
    got a ``refs.title`` cut at exactly 200 characters mid-word while the
    ``finding_body`` chunk stayed full-length — nothing asserted the two
    matched, so the divergence was invisible until a human spotted it.
    :func:`_assert_title_round_trip` closes that gap at both real write
    doors (:func:`mint_hub`, :func:`refine_claim_sentence`): raised inside
    the write's own transaction/savepoint, so it rolls the write back
    rather than leaving a truncated title committed — a failed mint is
    always better than a silently truncated one.
    """


def _assert_title_round_trip(conn: Any, ref_id: int, intended: str, *, op: str) -> None:
    """Read ``refs.title`` for ``ref_id`` back on ``conn`` and assert it
    equals ``intended`` exactly, raising :class:`TitleRoundTripError`
    otherwise. Must be called inside the same transaction/savepoint as the
    title write, before it commits — see the class docstring."""
    row = conn.execute("SELECT title FROM refs WHERE ref_id = %s", (ref_id,)).fetchone()
    persisted = str(row[0]) if row is not None and row[0] is not None else None
    if persisted != intended:
        raise TitleRoundTripError(
            f"{op}: ref_id={ref_id} title round-trip mismatch after write -- "
            f"intended {len(intended)} chars, persisted {len(persisted or '')} "
            f"chars. Likely cause: a caller running stale code that still "
            "truncates refs.title (see "
            "docs/backlog/hub-title-200-truncation-via-stale-mcp.md)."
        )


def _grounding_chunk_ord(
    store: Store, *, paper_ref_id: int, meta: dict[str, Any] | None
) -> int | None:
    """Resolve an evidence edge's grounding chunk to its ``ord``, or ``None``.

    Taproot's ``meta.source_handle`` (a ``pc<chunk_id>`` universal handle,
    the "grounded at this passage" pointer the chase verdict / authoring
    spec carries) names the *specific paper chunk* that supports the claim.
    Storing it only in ``meta`` left the edge itself ref-level, so the link
    graph — and every reader built on it (the ``fi`` link table, the
    citation tree) — could only ever cite the whole paper (``pa<id>``), not
    the passage (``pc<id>``). Returning the chunk's ``ord`` here lets
    :func:`attach_evidence` pass ``src_pos`` to ``store.add_link``, which
    materialises ``src_chunk_id`` so the edge renders ``pc<id>`` and two
    distinct passages of the same paper become two edges ("the set of
    chunks that support this point"), not one collapsed ref-level edge.

    Two ``source_handle`` forms are recognised — the ``pc<chunk_id>``
    universal handle (the authoring / mint spec form) and the ``slug~ord``
    pointer the chase writes per hop
    (:func:`~precis.workers.chase._evidence_edge_meta`).

    Best-effort and defensive: a missing / unresolvable ``source_handle``,
    one that isn't a chunk, one whose chunk belongs to a *different* paper
    than this edge's source (a spec/verdict bug), or an ``ord`` with no
    live chunk yields ``None`` — the edge stays ref-level rather than
    grounding at the wrong paper or handing ``add_link`` a non-existent
    ``(ref, ord)`` (which would raise and fail the write).
    """
    if not meta:
        return None
    handle = meta.get("source_handle")
    if not handle or not isinstance(handle, str):
        return None

    # Resolve the handle to a *candidate* ord, by form:
    candidate: int | None = None

    # Form 1: pc<chunk_id> universal handle → resolve to (ref, ord).
    try:
        resolved = store.resolve_handle(handle)
    except Exception:  # defensive — a malformed handle never fails the write
        resolved = None
    if resolved is not None and resolved.chunk_id is not None:
        if resolved.chunk_ord is None:
            return None
        if resolved.ref_id != paper_ref_id:
            log.warning(
                "taproot: source_handle %r resolves to ref_id=%s, not the "
                "edge's paper ref_id=%s — attaching ref-level (no grounding)",
                handle,
                resolved.ref_id,
                paper_ref_id,
            )
            return None
        candidate = resolved.chunk_ord
    else:
        # Form 2: slug~ord (chase's per-hop pointer). The ord is the chunk
        # position within this edge's paper; trust paper_ref_id
        # (authoritative) and take the ord from the tail.
        _, sep, tail = handle.rpartition("~")
        if not sep:
            return None
        try:
            candidate = int(tail)
        except ValueError:
            return None

    # Shared live-body-chunk verification for BOTH forms: ground only at a
    # live (``retired_at IS NULL``) real body chunk (``ord >= 0``) of this
    # paper. Guards the resolve_handle path — which does NOT filter retired
    # rows or card variants — against grounding at a soft-retired passage
    # (re-ingest/dedup) or an ``ord < 0`` card chunk, and keeps a stale
    # ``add_link(src_pos=ord)`` from a since-removed passage from raising.
    if candidate is None or candidate < 0:
        return None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM chunks WHERE ref_id = %s AND ord = %s AND "
            "retired_at IS NULL AND ord >= 0",
            (paper_ref_id, candidate),
        ).fetchone()
    return candidate if row is not None else None


#: The chunk-classifier axis (``data/axes/patent_example.yaml``) that tags a
#: patent description paragraph ``worked`` / ``prophetic`` / ``none`` — see
#: that file's header for the US tense-of-performance convention.
_PATENT_EXAMPLE_NS = "PATENT_EXAMPLE"
_PATENT_EXAMPLE_PROPHETIC = "prophetic"

#: Fixed, deterministic caveat text appended to an evidence edge whose
#: grounding chunk is a patent paragraph the ``patent_example`` axis tagged
#: ``prophetic`` (patent-evidence-parity phase 4, docs/backlog/patent-
#: evidence-parity.md). Mechanical injection only — this never touches the
#: taproot verify LLM prompt; an unclassified chunk (axis hasn't run yet)
#: or a ``worked``/``none`` tag gets no caveat at all.
PROPHETIC_EXAMPLE_CAVEAT = (
    "prophetic example (proposed, not performed) — corroborates at best"
)


def _prophetic_caveat(c: Any, *, ref_id: int, ord_: int | None) -> str | None:
    """:data:`PROPHETIC_EXAMPLE_CAVEAT` iff the grounding chunk
    ``(ref_id, ord_)`` carries ``PATENT_EXAMPLE:prophetic`` — else ``None``.

    Reads ``chunk_tags`` directly (not ``v_chunk_tags_all``): the axis is
    chunk-level, so only the chunk's own tag counts, never an inherited
    ref-level tag. ``ord_ is None`` (a ref-level edge — no grounding chunk
    was resolved) always yields ``None``; there is no "the chunk" to check.
    """
    if ord_ is None:
        return None
    row = c.execute(
        "SELECT 1 FROM chunk_tags ct "
        "JOIN tags t ON t.tag_id = ct.tag_id "
        "JOIN chunks ch ON ch.chunk_id = ct.chunk_id "
        "WHERE ch.ref_id = %s AND ch.ord = %s "
        "AND t.namespace = %s AND t.value = %s",
        (ref_id, ord_, _PATENT_EXAMPLE_NS, _PATENT_EXAMPLE_PROPHETIC),
    ).fetchone()
    return PROPHETIC_EXAMPLE_CAVEAT if row is not None else None


def _is_claim_hub(ref_id: int, *, conn: Any) -> bool:
    """True iff ``ref_id`` is a live ``finding`` carrying ``TAPROOT:claim``."""
    row = conn.execute(
        """
        SELECT 1
        FROM refs r
        JOIN ref_tags rt ON rt.ref_id = r.ref_id
        JOIN tags t ON t.tag_id = rt.tag_id
                   AND t.namespace = %(ns)s AND t.value = %(val)s
        WHERE r.ref_id = %(rid)s AND r.kind = 'finding' AND r.deleted_at IS NULL
        LIMIT 1
        """,
        {"ns": TAPROOT_NAMESPACE, "val": TAPROOT_CLAIM, "rid": ref_id},
    ).fetchone()
    return row is not None


def _is_compound_hub(ref_id: int, *, conn: Any) -> bool:
    """True iff ``ref_id`` carries a live inbound ``conjunct-of`` edge from a
    live ``finding`` — i.e. it is a **compound** claim hub with atomic
    conjuncts linked to it, rather than an atom or a plain (undecomposed)
    claim hub (docs/backlog/taproot-atomic-claims.md).

    Deliberately the literal predicate — ``links l JOIN refs a ON
    a.ref_id = l.src_ref_id WHERE l.dst_ref_id = %s AND l.relation =
    'conjunct-of' AND a.kind = 'finding' AND a.deleted_at IS NULL`` — with
    **no** ``TAPROOT:claim`` tag join on the source. This is a deliberate
    seam, not an oversight: :mod:`precis.taproot.seniority`'s mirror of this
    predicate (``conjunct_atoms_bulk``) *does* re-check the tag (that
    module's idiom — ``_is_claim_hub`` always checks tags), while
    :mod:`precis.workers.hub_refine`'s copies (``_claim_hubs_due_for_refine``'s
    ``NOT EXISTS`` filter, ``_is_compound_hub``) omit it, same as here. Both
    are correct: :func:`link_claims` (the single write door for
    ``conjunct-of`` edges) already guards **both** endpoints are live
    ``TAPROOT:claim`` findings at write time, so a live inbound
    ``conjunct-of`` edge can only ever originate from a claim hub — the tag
    re-check downstream is redundant, not wrong, and each module keeps its
    own copy of the predicate rather than sharing a connection-agnostic
    helper (the ``seniority._is_claim_hub``-mirrors-``hub._is_claim_hub``
    precedent this build follows throughout).
    """
    row = conn.execute(
        """
        SELECT 1
          FROM links l
          JOIN refs a ON a.ref_id = l.src_ref_id
         WHERE l.dst_ref_id = %s
           AND l.relation = 'conjunct-of'
           AND a.kind = 'finding'
           AND a.deleted_at IS NULL
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return row is not None


#: The compound hub's not-a-claim audit memo key (step 8,
#: docs/backlog/taproot-atomic-claims.md) — mirrors ``hub_refine``'s
#: ``taproot_rejected`` memo *shape* (deduped by key, never re-litigated)
#: but keyed by ``claim_sha(text)`` of the rejected fragment, **not** a ref
#: id: a rejected conjunct is never minted, so it has no ref id to key by.
#: Don't "fix" this to a ref-id key later — there is no ref for it to be.
_NOT_CLAIMS_META_KEY = "taproot_not_claims"


def mint_hub(
    store: Store,
    claim: CanonicalClaim,
    *,
    set_by: ActorSlug = "agent",
    conn: Any = None,
    extra_meta: dict[str, Any] | None = None,
) -> int:
    """Create a new ``TAPROOT:claim`` ``finding`` hub. Returns its ref_id.

    Only paper-grounded claims become hubs (open #15); the caller
    (:func:`apply_placement`, driven by the canonicalizer over a paper chunk)
    supplies that provenance — this primitive just writes the hub.

    The hub is a ``finding`` (reuse, not a new kind — the argument graph precedent):
    ``claim.sentence`` → ``title`` (list-view scannability) *and* a
    ``finding_body`` chunk at ``ord=0`` (so it embeds + full-text-searches,
    and the card pass emits the ``card_combined`` that :func:`canon.block`
    ANN-retrieves over); ``claim.scope`` → ``meta.scope``; ``STATUS:canonical``;
    ``TAPROOT:claim``. This is taproot's *system-writer* path — the agent-facing
    door is ``FindingHandler.put`` (pub_id dedup + a frontier ``derived-from``);
    taproot dedups upstream via canonicalization, so the hub write is direct.

    The freshly-written ``refs.title`` is read back and asserted equal to
    ``claim.sentence.strip()`` inside the same savepoint
    (:func:`_assert_title_round_trip`) — see :class:`TitleRoundTripError`.

    Citability (slice F): the hub also gets a ``pub_id`` written
    to ``ref_identifiers`` — the same 6-char ``[a-z2-7]`` handle
    ``FindingHandler.put`` mints — so agent draft prose can cite it as
    ``[ab12c3]`` and ``precis resolve`` / ``refeye.resolve_link_targets``
    (which already mine ``ref_identifiers(id_kind='pub_id')``) pick it up
    for free. Seeded via :func:`make_taproot_hub_paper_id` — content-derived
    off ``claim.sentence`` + ``claim.scope`` (a hub has no citing occasion
    to anchor :func:`make_finding_paper_id`'s ``initial_cite_pub_id``) — so
    the pub_id is deterministic per canonicalized claim.

    **Converge-to-attach on a pub_id collision.** A freshly minted hub's
    ``card_combined`` chunk + embedding are written *async* (the derived queue —
    card_forge/embed run later), so :func:`~precis.taproot.canon.block` can
    return zero candidates for a claim whose hub was just minted but not
    yet embedded. Two findings asserting the identical claim (successive
    chase passes, or concurrent workers) can then both resolve
    ``place() == "new"`` and both call this function for the *same*
    deterministic ``pub_id``. Because that's the identity contract
    (same claim content -> same pub_id), the collision means the other
    caller's hub already *is* the hub for this claim — so this looks the
    pub_id up first (attach path, no write) and, if a second caller still
    races past that check, catches the ``ref_identifiers`` PK
    ``UniqueViolation`` on insert, rolls back only its own partial
    ref/chunk/tags write (a savepoint — never the caller's surrounding
    transaction), and resolves to the winner's ref_id. Either way the
    caller always gets back a real hub ref_id to attach evidence to,
    never a raised exception or a dropped edge.

    ``extra_meta`` (step 8, docs/backlog/taproot-atomic-claims.md) merges
    additional top-level ``meta`` keys into the hub at insert time — one
    write, no follow-up ``update_ref`` — used by
    :func:`apply_extraction` to seed a freshly-minted compound hub's
    :data:`_NOT_CLAIMS_META_KEY` memo. Applied **only** on an actual insert
    (the ``_mint`` path below); the converge-to-existing branches (a
    pub_id already resolved, or a raced ``UniqueViolation``) return the
    existing hub untouched — merging into an *existing* hub's meta
    non-destructively is a distinct operation (see the module's
    ``_merge_not_claims_memo`` helper, used on the ``attach`` side of
    :func:`apply_extraction`), not this parameter's job.
    """
    paper_id = make_taproot_hub_paper_id(claim.sentence, claim.scope)
    pub_id = make_pub_id(paper_id)

    def _existing_hub(c: Any) -> int | None:
        row = c.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
            ("pub_id", pub_id),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _mint(c: Any) -> int:
        meta: dict[str, Any] = {"scope": dict(claim.scope), "source": "taproot"}
        if extra_meta:
            meta.update(extra_meta)
        intended_title = claim.sentence.strip()
        ref = store.insert_ref(
            kind="finding",
            slug=None,
            title=intended_title,
            meta=meta,
            conn=c,
        )
        _assert_title_round_trip(c, ref.id, intended_title, op="mint_hub")
        store.blocks.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0,
                    text=claim.sentence.strip(),
                    meta={"chunk_kind": "finding_body"},
                )
            ],
            conn=c,
        )
        # pub_id — same (id_kind, id_value, ref_id, source) shape
        # FindingHandler.put inserts, so the same resolve-side query
        # (`ref_identifiers WHERE id_kind = 'pub_id'`) finds this hub.
        # No ON CONFLICT: a collision here means a concurrent mint_hub
        # call won the race for this exact claim (see the converge-to-
        # attach note above) — caught by the caller, not swallowed here.
        c.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
            "VALUES (%s, %s, %s, %s)",
            ("pub_id", pub_id, ref.id, "taproot"),
        )
        # STATUS:canonical — a canonicalized claim node, not an in-flight
        # chase; its state is its derived evidence, not a chase lifecycle
        # (system-set — deliberately NOT STATUS:tracing like a put() finding,
        # which would drag the hub into the chase claim + hide it from the
        # default finding search).
        store.add_tag(
            ref.id,
            Tag.closed(_STATUS_NS, _STATUS_CANONICAL),
            set_by="system",
            replace_prefix=True,
            conn=c,
        )
        store.add_tag(
            ref.id,
            Tag.closed(TAPROOT_NAMESPACE, TAPROOT_CLAIM),
            set_by=set_by,
            replace_prefix=True,
            conn=c,
        )
        log.info("taproot: minted hub ref_id=%s pub_id=%s", ref.id, pub_id)
        return int(ref.id)

    def _do(c: Any) -> int:
        existing = _existing_hub(c)
        if existing is not None:
            log.info(
                "taproot: hub already exists for pub_id=%s ref_id=%s "
                "(converged to attach, no new hub minted)",
                pub_id,
                existing,
            )
            return existing
        try:
            # Savepoint: isolates the ref/chunk/tags/pub_id write so a
            # losing race (UniqueViolation on the pub_id PK) rolls back
            # only this attempt, never the caller's surrounding
            # transaction (e.g. the chase bridge's per-finding conn=).
            with c.transaction():
                return _mint(c)
        except UniqueViolation:
            resolved = _existing_hub(c)
            if resolved is None:  # pragma: no cover — defensive
                raise
            log.info(
                "taproot: mint_hub collided on pub_id=%s — converged to "
                "existing hub ref_id=%s",
                pub_id,
                resolved,
            )
            return resolved

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


def refine_claim_sentence(
    store: Store,
    hub_ref_id: int,
    sentence: str,
    *,
    scope: dict[str, Any] | None = None,
    set_by: str = "agent",
    conn: Any = None,
) -> dict[str, Any]:
    """Reword a ``TAPROOT:claim`` hub's claim sentence in place. Returns a
    summary dict (see below).

    The claim sentence lives in three places (:func:`mint_hub`): ``refs.title``
    (full length — a claim sentence carries all its meaning; it is exactly as
    long as it needs to be), the ``finding_body`` chunk at ``ord=0``, and
    implicitly in the content-derived ``pub_id``. This is the retitle door
    that keeps all three in sync when a hub's wording needs fixing (e.g. a
    claim-quality rubric flags a dangling demonstrative) — there is otherwise
    no way to reword a hub short of deleting and re-minting it, which would
    orphan every evidence edge already attached.

    Writes, all inside one transaction:

    1. ``refs.title = sentence.strip()`` (never truncated); ``meta.scope`` is replaced
       (not merged) when ``scope`` is given, else the hub's existing scope
       is kept. Read back and asserted equal to the intended title inside
       this same transaction (:func:`_assert_title_round_trip`) — see
       :class:`TitleRoundTripError`.
    2. The ``finding_body`` chunk (``ord=0``) is replaced via DELETE+INSERT
       (:meth:`~precis.store.Store.replace_body_chunk`) — never an in-place
       text UPDATE, so the embedding/summary cascade re-runs on the new
       wording (repo convention: ``chunks`` is append-only except for the
       DELETE+INSERT re-emit path).
    3. Every card variant (``ord < 0``) is deleted. No pass in this codebase
       re-derives a hub's ``card_combined`` off ``finding_body``/title
       content changes — :func:`mint_hub` itself never emits one, and the
       one pass that DELETE+INSERTs a finding's ``card_combined``
       (``workers/chase.py::_snapshot_chain``) only runs at chain
       termination for a ``STATUS:tracing`` finding, never for a
       ``STATUS:canonical`` hub. Deleting here is the safe branch: a stale
       card must never keep matching the OLD wording in
       :func:`~precis.taproot.canon.block`'s ANN dedup index; the async
       card-forge path :func:`mint_hub` already documents as populating a
       fresh hub's card re-emits the new one.
    4. The ``pub_id`` is recomputed from ``(new sentence, effective scope)``
       via :func:`~precis.identity.make_taproot_hub_paper_id` /
       :func:`~precis.identity.make_pub_id`. If it differs from the hub's
       existing pub_id(s), the new one is INSERTed as an additional
       ``ref_identifiers`` row (``id_kind='pub_id'``) and the OLD row is
       **kept** as an alias — draft prose that already cites the old
       ``[<pub_id>]`` handle must keep resolving. If the new pub_id already
       belongs to a *different* live ref, that's a dedup/merge candidate —
       raised as a :class:`ValueError` rather than silently merged (the
       caller decides).

    Args:
        hub_ref_id: The claim hub's ref_id. Must be a live ``TAPROOT:claim``
            ``finding``.
        sentence: The new claim sentence. Required non-empty.
        scope: When given, replaces ``meta.scope`` wholesale and feeds the
            new pub_id derivation. ``None`` (default) keeps the hub's
            existing scope.
        set_by: Audit actor for the chunk-replace event.
        conn: An open transaction to fold this write into (mirrors every
            other function in this module); ``None`` opens its own
            ``store.tx()``.

    Returns:
        ``{"hub_ref_id", "old_title", "new_title", "pub_id",
        "pub_id_alias_kept", "notation"}`` — ``pub_id`` is the (possibly
        unchanged) current pub_id after the write; ``pub_id_alias_kept`` is
        True iff a new pub_id row was inserted (the old one stays live as an
        alias). ``notation`` is
        :func:`~precis.taproot.notation.lint_notation`'s advisory warnings
        for the new ``sentence`` — never raises, never blocks the reword,
        never rewrites the sentence.

    Raises:
        ValueError: ``hub_ref_id`` isn't a live ``TAPROOT:claim`` hub,
            ``sentence`` is empty/whitespace, or the new pub_id already
            belongs to a different ref (names that ref_id).
        TitleRoundTripError: the written ``refs.title`` didn't read back
            equal to the intended sentence (rolls the transaction back).
    """
    stripped = sentence.strip() if sentence else ""
    if not stripped:
        raise ValueError("refine_claim_sentence requires a non-empty sentence")

    def _do(c: Any) -> dict[str, Any]:
        if not _is_claim_hub(hub_ref_id, conn=c):
            raise ValueError(f"ref_id={hub_ref_id} is not a TAPROOT:claim hub")

        row = c.execute(
            "SELECT title, meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (hub_ref_id,),
        ).fetchone()
        if (
            row is None
        ):  # pragma: no cover — defensive, _is_claim_hub already checked live
            raise ValueError(f"ref_id={hub_ref_id} not found")
        old_title = str(row[0] or "")
        current_meta = dict(row[1] or {})
        effective_scope = (
            dict(scope) if scope is not None else dict(current_meta.get("scope") or {})
        )

        new_title = stripped
        store.update_ref(
            hub_ref_id,
            title=new_title,
            meta_patch={"scope": effective_scope} if scope is not None else None,
            conn=c,
        )
        _assert_title_round_trip(c, hub_ref_id, new_title, op="refine_claim_sentence")

        # (2) finding_body chunk — DELETE+INSERT at ord=0.
        store.blocks.replace_body_chunk(
            hub_ref_id, stripped, chunk_kind="finding_body", source=set_by, conn=c
        )

        # (3) card variants — no staleness-detecting pass exists for a hub
        # (see the docstring); DELETE so a stale card never keeps matching
        # the old wording, relying on the async card-forge re-emit.
        c.execute("DELETE FROM chunks WHERE ref_id = %s AND ord < 0", (hub_ref_id,))

        # (4) pub_id — recompute, alias the old one, guard a cross-ref collision.
        new_paper_id = make_taproot_hub_paper_id(stripped, effective_scope)
        new_pub_id = make_pub_id(new_paper_id)
        existing = c.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'pub_id' AND id_value = %s",
            (new_pub_id,),
        ).fetchone()
        alias_kept = False
        if existing is None:
            c.execute(
                "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
                "VALUES (%s, %s, %s, %s)",
                ("pub_id", new_pub_id, hub_ref_id, "taproot"),
            )
            alias_kept = True
        elif int(existing[0]) != hub_ref_id:
            raise ValueError(
                f"new pub_id={new_pub_id} for the reworded sentence already "
                f"belongs to ref_id={int(existing[0])} — this looks like a "
                "dedup/merge candidate; refine_claim_sentence never merges "
                "hubs silently"
            )
        # else: unchanged (or reverted-to-a-previous-wording) pub_id already
        # on this hub — no-op.

        log.info(
            "taproot: refined hub ref_id=%s title=%r -> %r pub_id=%s",
            hub_ref_id,
            old_title,
            new_title,
            new_pub_id,
        )
        return {
            "hub_ref_id": hub_ref_id,
            "old_title": old_title,
            "new_title": new_title,
            "pub_id": new_pub_id,
            "pub_id_alias_kept": alias_kept,
            "notation": lint_notation(new_title),
        }

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


def run_retraction_checks(
    store: Store, paper_ref_ids: list[int], *, hub_ref_id: int | None = None
) -> None:
    """Drain deferred trigger-1 checks. **Call only outside a transaction.**

    The companion to :func:`attach_evidence`'s ``pending_checks`` sink:
    workers that hand us their own ``conn`` collect the ref ids during the
    write and drain them here once committed. Failures are logged and
    swallowed — the evidence edges are already durable, and an
    opportunistic integrity check must never be able to undo them.
    """
    for paper_ref_id in paper_ref_ids:
        try:
            check_ref_retraction(store, paper_ref_id)
        except Exception:
            log.warning(
                "taproot: retraction check failed for paper_ref_id=%s "
                "(evidence edge%s already written)",
                paper_ref_id,
                f" to hub_ref_id={hub_ref_id}" if hub_ref_id is not None else "",
                exc_info=True,
            )


def attach_evidence(
    store: Store,
    *,
    hub_ref_id: int,
    paper_ref_id: int,
    role: str,
    meta: dict[str, Any] | None = None,
    set_by: str = "agent",
    conn: Any = None,
    check_retraction: bool = True,
    pending_checks: list[int] | None = None,
) -> None:
    """Write one ``paper --role--> hub`` evidence edge.

    ``role`` must be one of :data:`HUB_ROLES` *and* a registered relation
    (checked via :func:`validate_relation` — the friendly pre-flight for the
    ``links_relation_fkey`` FK). ``hub_ref_id`` must be a ``TAPROOT:claim``
    finding — never attach evidence to a ``TAPROOT:review`` note or a non-finding
    (that is what the classifier + this guard exist to prevent). The edge is
    directed **paper → hub**; the hub reads its evidence via
    ``links_for(direction='in', relation=role)``. ``meta`` carries the chase
    verdict (``support``/``support_reason``/``caveats``/``char_offset``/
    ``source_handle``), populated in Phase 3.

    **Trigger 1 of the demand-driven retraction model** (trigger 2 is
    the draft's watch button, ``precis.export.retraction``): a paper
    entering the claim graph is the moment its integrity starts to
    matter, so a
    ``paper`` source (a patent has no DOI to check) gets checked via
    :func:`precis.ingest.provenance.check_ref_retraction`. The check is
    TTL-gated (30 days), so a chase pass re-attaching over an
    already-checked paper set costs no network, and any failure is
    swallowed and logged — it must never fail or roll back the edge,
    which is the durable thing here.

    **The check never runs inside an open transaction.** It does a
    Crossref HTTP round-trip *and* opens its own connections; holding a
    pgbouncer'd Postgres transaction across that risks pool-exhaustion
    deadlock under load. So:

    * ``conn=None`` — we own the write, and the check runs after our
      ``store.tx()`` commits.
    * ``conn=<caller's>`` — the caller's transaction is still open when
      we return, so we cannot check here. Pass ``pending_checks=[]`` and
      drain it with :func:`run_retraction_checks` after committing.
      Without that list the check is silently skipped, which is why
      every worker call site threads one through.

    ``check_retraction=False`` is the opt-out for bulk/backfill callers
    and tests that must not touch the network.
    """
    if role not in HUB_ROLES:
        raise BadInput(
            f"invalid evidence role: {role!r}",
            options=sorted(HUB_ROLES),
            next=f"role must be one of {sorted(HUB_ROLES)}",
        )
    # FK/vocab pre-flight (raises BadInput on an unregistered slug).
    validated = validate_relation(role, store=store)

    # Box for the source ref's kind, set inside ``_do`` and read after the
    # transaction closes — see the retraction-check call below, which must
    # run outside the write-door transaction.
    src_kind_box: dict[str, str] = {}

    def _do(c: Any) -> None:
        if not _is_claim_hub(hub_ref_id, conn=c):
            raise BadInput(
                f"hub_ref_id={hub_ref_id} is not a TAPROOT:claim finding",
                next=(
                    "evidence edges attach only to claim hubs — tag the "
                    "finding TAPROOT:claim (axis:taproot) or pick a claim hub"
                ),
            )
        # Hard backstop (docs/backlog/taproot-atomic-claims.md step 3) behind
        # apply_placement's softer needs_review downgrade — same
        # defense-in-depth as the EVIDENCE_SRC_KINDS check below sitting
        # behind authoring.resolve_paper_ref_id's authoritative check.
        # apply_extraction's own ordering (atoms attach before the
        # conjunct-of link is ever written) never trips this.
        if _is_compound_hub(hub_ref_id, conn=c):
            raise BadInput(
                "evidence attaches to atom hubs; this hub is a compound — "
                "attach to its conjunct atoms",
                next=(
                    "resolve the compound's conjunct atom hubs "
                    "(taproot.seniority.derive_conjuncts) and attach evidence "
                    "to the specific atom this source supports"
                ),
            )
        src = store.fetch_refs_by_ids([paper_ref_id], include_deleted=True).get(
            paper_ref_id
        )
        if src is None or src.kind not in EVIDENCE_SRC_KINDS:
            kind_desc = "unknown" if src is None else src.kind
            raise BadInput(
                f"paper_ref_id={paper_ref_id} is a {kind_desc!r} ref, not a "
                "paper/patent",
                next="evidence edges attach only from a paper/patent source ref",
            )
        src_kind_box["kind"] = src.kind
        # Ground the edge at the specific supporting passage when the
        # verdict/spec named one (meta.source_handle → src_chunk_id), so the
        # edge is pc<id>-granular. Falls back to a ref-level (pa<id>) edge
        # when no chunk was named or it can't be resolved to this paper.
        src_pos = _grounding_chunk_ord(store, paper_ref_id=paper_ref_id, meta=meta)
        # Deterministic prophetic-example caveat (patent-evidence-parity
        # phase 4): a patent source whose grounding chunk the
        # ``patent_example`` axis tagged ``prophetic`` gets the fixed
        # caveat appended here, mechanically — never via the verify LLM.
        # This is the single choke point every evidence-edge write
        # (``workers/chase.py``'s forward bridge AND intermediate-hop
        # attach, ``workers/hub_refine.py``'s discovery attach) funnels
        # through, so it's checked once, here, rather than at each caller.
        edge_meta = meta
        if src.kind == "patent":
            caveat = _prophetic_caveat(c, ref_id=paper_ref_id, ord_=src_pos)
            if caveat is not None:
                edge_meta = dict(meta or {})
                caveats = list(edge_meta.get("caveats") or [])
                if caveat not in caveats:
                    caveats.append(caveat)
                edge_meta["caveats"] = caveats
        store.add_link(
            src_ref_id=paper_ref_id,
            src_pos=src_pos,
            dst_ref_id=hub_ref_id,
            relation=validated,
            meta=edge_meta,
            set_by=set_by,
            conn=c,
        )

    if conn is not None:
        _do(conn)
    else:
        with store.tx() as c:
            _do(c)

    # Trigger 1. Patents have no DOI to check Crossref against, so only
    # a paper source qualifies.
    if not (check_retraction and src_kind_box.get("kind") == "paper"):
        return

    if conn is None:
        # We owned the transaction and it has committed. Safe to check here.
        run_retraction_checks(store, [paper_ref_id], hub_ref_id=hub_ref_id)
    elif pending_checks is not None:
        # The CALLER owns the transaction and it is still open — doing HTTP
        # here would hold a pgbouncer'd server connection across a network
        # round-trip, and check_ref_retraction opens its own connections on
        # top of that (a pool-exhaustion deadlock under load). Hand the ref
        # back instead; the caller drains after its commit.
        pending_checks.append(paper_ref_id)


def link_claims(
    store: Store,
    *,
    from_hub_ref_id: int,
    to_hub_ref_id: int,
    relation: str = "refines",
    set_by: str = "agent",
    conn: Any = None,
) -> bool:
    """Write one hub ``--relation--> hub`` advisory claim-link. Returns
    ``True`` if a new edge was written, ``False`` if it already existed.

    ``relation`` must be one of :data:`CLAIM_LINK_RELATIONS` (``refines``,
    ``conjunct-of``) *and* a registered relation (checked via
    :func:`validate_relation` — the friendly pre-flight for the
    ``links_relation_fkey`` FK). **Both**
    endpoints must be live ``TAPROOT:claim`` findings — a claim-link joins two
    claim hubs (never a paper, a review note, or a non-finding), and the two
    must differ (a hub can't link to itself).

    This is the single write door for claim→claim links, the sibling of
    :func:`attach_evidence` for paper→hub evidence edges (open #16).
    Unlike evidence, a claim-link carries **no evidence flow** — the hubs keep
    their own paper→hub edges. The fisheye Claims ring
    (:mod:`precis.utils.refeye`) surfaces only ``refines`` today
    (``derive_refines``); ``conjunct-of`` edges are not yet rendered there —
    backlog item ``docs/backlog/fisheye-conjunct-of-surfacing.md``. Idempotent:
    an identical
    ``(from, to, relation)`` edge already present is a no-op returning
    ``False``, so a re-run of the same authoring step writes nothing.
    """
    if relation not in CLAIM_LINK_RELATIONS:
        raise BadInput(
            f"invalid claim-link relation: {relation!r}",
            options=sorted(CLAIM_LINK_RELATIONS),
            next=f"relation must be one of {sorted(CLAIM_LINK_RELATIONS)}",
        )
    if from_hub_ref_id == to_hub_ref_id:
        raise BadInput(
            f"a claim hub cannot {relation} itself (ref_id={from_hub_ref_id})",
            next="from and to must be two distinct claim hubs",
        )
    # FK/vocab pre-flight (raises BadInput on an unregistered slug).
    validated = validate_relation(relation, store=store)

    def _do(c: Any) -> bool:
        for ref_id, label in ((from_hub_ref_id, "from"), (to_hub_ref_id, "to")):
            if not _is_claim_hub(ref_id, conn=c):
                raise BadInput(
                    f"{label}_hub_ref_id={ref_id} is not a TAPROOT:claim finding",
                    next=(
                        "claim-links join two claim hubs — mint the sharper "
                        "claim as its own hub (precis taproot mint) first"
                    ),
                )
        existing = c.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = %s",
            (from_hub_ref_id, to_hub_ref_id, validated),
        ).fetchone()
        if existing is not None:
            return False
        store.add_link(
            src_ref_id=from_hub_ref_id,
            dst_ref_id=to_hub_ref_id,
            relation=validated,
            set_by=set_by,
            conn=c,
        )
        log.info(
            "taproot: linked hub %s --%s--> hub %s",
            from_hub_ref_id,
            validated,
            to_hub_ref_id,
        )
        return True

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


def _mint_for_placement(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    paper_ref_id: int | None,
    role: str,
    meta: dict[str, Any] | None,
    set_by: ActorSlug,
    conn: Any,
    pending_checks: list[int] | None,
    attach_paper: bool,
    extra_meta: dict[str, Any] | None = None,
) -> int:
    """Shared ``new``/``new_contradicts`` mint-or-converge, for both
    :func:`apply_placement` (atoms, ``attach_paper=True``) and
    :func:`apply_extraction`'s compound handling (``attach_paper=False`` —
    a compound never gets a direct evidence edge, step 3). One mint-logic
    path rather than a fork: :func:`mint_hub` (+ the hub<->hub
    ``contradicts`` link for ``new_contradicts``), optionally followed by
    :func:`attach_evidence`.

    ``extra_meta`` passes through to :func:`mint_hub` — the not-a-claim
    memo (step 8) for a freshly-minted compound.
    """
    owned_checks: list[int] = []
    sink = pending_checks if conn is not None else owned_checks

    def _do(c: Any) -> int:
        hub = mint_hub(store, claim, set_by=set_by, conn=c, extra_meta=extra_meta)
        if attach_paper:
            if paper_ref_id is None:  # pragma: no cover — defensive
                raise BadInput("attach_paper=True requires a paper_ref_id")
            attach_evidence(
                store,
                hub_ref_id=hub,
                paper_ref_id=paper_ref_id,
                role=role,
                meta=meta,
                set_by=set_by,
                conn=c,
                pending_checks=sink,
            )
        if placement.action == "new_contradicts":
            if placement.contradicts_hub_ref_id is None:
                raise BadInput(
                    "new_contradicts placement has no contradicts_hub_ref_id"
                )
            # Hub <-> hub: opposite *claims* (distinct from a paper->hub
            # `contradicts` evidence edge; same slug, different endpoints).
            store.add_link(
                src_ref_id=hub,
                dst_ref_id=placement.contradicts_hub_ref_id,
                relation=validate_relation("contradicts", store=store),
                set_by=set_by,
                conn=c,
            )
        return hub

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        hub_id = _do(c)
    if attach_paper:
        # Transaction committed — now it is safe to reach the network.
        run_retraction_checks(store, owned_checks, hub_ref_id=hub_id)
    return hub_id


def apply_placement(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    paper_ref_id: int,
    role: str = _DEFAULT_ROLE,
    meta: dict[str, Any] | None = None,
    todo_fn: Callable[[CanonicalClaim, Placement], Any] | None = None,
    set_by: ActorSlug = "agent",
    conn: Any = None,
    pending_checks: list[int] | None = None,
) -> int | None:
    """Persist a canonicalizer :class:`Placement` through the write door.

    Routing (mirrors :func:`precis.taproot.canon.place`):

    * ``attach`` — attach this paper as evidence on the matched hub.
    * ``new`` — mint a hub, attach the paper.
    * ``new_contradicts`` — mint a hub, attach the paper, and link the new hub
      ``contradicts`` the existing (opposite-*claim*) hub.
    * ``needs_review`` — file a ``kind='todo'`` via ``todo_fn`` and attach
      **nothing** (open #16: a risky merge is never auto-applied).

    **Compound downgrade** (docs/backlog/taproot-atomic-claims.md step 2):
    an ``attach`` placement whose ``hub_ref_id`` is a **compound** hub (has
    ≥1 live inbound ``conjunct-of`` edge, :func:`_is_compound_hub`) is
    downgraded to the ``needs_review`` path instead of attaching — evidence
    must never land on a compound (step 3's own hard guard in
    :func:`attach_evidence` would raise), and letting that raise happen
    inside a caller's savepoint (e.g. the chase bridge's per-finding
    transaction) would drop the evidence with only a log line rather than
    filing a todo a human can act on.

    Returns the hub ref_id it attached to / minted, or ``None`` for
    ``needs_review`` (including the compound downgrade above). ``role`` is
    the evidence role for the paper edge (default :data:`_DEFAULT_ROLE`);
    originator promotion is derived later.

    ``conn`` (Phase 3 W1) lets a caller that already holds an open
    transaction (chase's per-finding ``conn``) fold the hub mint + evidence
    attach into it, so the write commits atomically with whatever else the
    caller is doing in the same transaction. ``None`` (default) preserves
    the original behaviour: ``attach`` writes standalone via
    :func:`attach_evidence`'s own ``store.tx()``, and ``new``/
    ``new_contradicts`` open one shared ``store.tx()`` for the mint +
    attach (+ optional contradicts link) pair. ``needs_review`` is the one
    exception: ``conn`` is never passed to ``todo_fn`` — filing the review
    todo is an intentionally separate, self-committing side-effect (there
    is no hub/edge write on this path to keep atomic with it), not part of
    the evidence write ``conn`` exists to bundle.
    """
    action = placement.action

    def _file_needs_review() -> None:
        if todo_fn is not None:
            todo_fn(claim, placement)
        else:
            log.warning(
                "taproot: needs_review placement dropped (no todo_fn): %s",
                placement.reason,
            )

    if action == "attach":
        hub_ref_id = placement.hub_ref_id
        if hub_ref_id is None:
            raise BadInput("attach placement has no hub_ref_id")

        if conn is not None:
            is_compound = _is_compound_hub(hub_ref_id, conn=conn)
        else:
            with store.pool.connection() as c:
                is_compound = _is_compound_hub(hub_ref_id, conn=c)
        if is_compound:
            log.info(
                "taproot: attach placement on compound hub_ref_id=%s downgraded "
                "to needs_review (evidence attaches to atom hubs only)",
                hub_ref_id,
            )
            _file_needs_review()
            return None

        # conn=None here means attach_evidence owns (and commits) its own
        # transaction, so it runs the check itself and the sink stays empty.
        attach_evidence(
            store,
            hub_ref_id=hub_ref_id,
            paper_ref_id=paper_ref_id,
            role=role,
            meta=meta,
            set_by=set_by,
            conn=conn,
            pending_checks=pending_checks if conn is not None else [],
        )
        return hub_ref_id

    if action in ("new", "new_contradicts"):
        return _mint_for_placement(
            store,
            claim,
            placement,
            paper_ref_id=paper_ref_id,
            role=role,
            meta=meta,
            set_by=set_by,
            conn=conn,
            pending_checks=pending_checks,
            attach_paper=True,
        )

    if action == "needs_review":
        _file_needs_review()
        return None

    raise BadInput(f"unknown placement action: {action!r}")


def _not_claims_memo(not_claims: tuple[NotClaim, ...]) -> dict[str, dict[str, Any]]:
    """Build the sha-keyed memo dict step 8 stores on the compound hub —
    ``{claim_sha(text): {"text", "reason", "at"}}``. ``{}`` for an empty
    ``not_claims`` (the caller should treat that as "nothing to write")."""
    now = datetime.now(UTC).isoformat()
    return {
        claim_sha(nc["text"]): {"text": nc["text"], "reason": nc["reason"], "at": now}
        for nc in not_claims
    }


def _merge_not_claims_memo(
    store: Store, hub_ref_id: int, memo: dict[str, dict[str, Any]], *, conn: Any
) -> None:
    """Merge ``memo`` into an existing compound hub's
    ``meta[_NOT_CLAIMS_META_KEY]``, existing keys winning — the
    ``attach`` counterpart to :func:`mint_hub`'s ``extra_meta`` (a fresh
    hub has no existing entries to protect, so that path just writes).
    A re-extraction of the same rejected fragment computes the same
    ``claim_sha`` and finds its entry already present; nothing is
    overwritten or duplicated."""

    def _do(c: Any) -> None:
        row = c.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (hub_ref_id,),
        ).fetchone()
        current = dict(row[0] or {}) if row is not None else {}
        existing_memo = dict(current.get(_NOT_CLAIMS_META_KEY) or {})
        merged = {**memo, **existing_memo}  # existing keys win
        store.update_ref(hub_ref_id, meta_patch={_NOT_CLAIMS_META_KEY: merged}, conn=c)

    if conn is not None:
        _do(conn)
    else:
        with store.tx() as c:
            _do(c)


def _apply_compound_placement(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    not_claims: tuple[NotClaim, ...],
    todo_fn: Callable[[CanonicalClaim, Placement], Any] | None,
    set_by: ActorSlug,
    conn: Any,
) -> int | None:
    """Mint-or-converge the compound hub for one :func:`extract_claim`
    result, **without** any evidence edge (step 3: compounds hold no
    direct evidence). ``attach`` resolves the existing hub id and merges
    the not-a-claim memo non-destructively; ``new``/``new_contradicts``
    mint via :func:`_mint_for_placement` (``attach_paper=False``) with the
    memo seeded at insert time; ``needs_review`` files the todo and mints
    nothing (no hub -> no memo target)."""
    action = placement.action
    memo = _not_claims_memo(not_claims) if not_claims else None

    if action == "attach":
        hub_ref_id = placement.hub_ref_id
        if hub_ref_id is None:
            raise BadInput("attach placement has no hub_ref_id")
        if memo:
            _merge_not_claims_memo(store, hub_ref_id, memo, conn=conn)
        return hub_ref_id

    if action in ("new", "new_contradicts"):
        extra_meta = {_NOT_CLAIMS_META_KEY: memo} if memo else None
        return _mint_for_placement(
            store,
            claim,
            placement,
            paper_ref_id=None,
            role=_DEFAULT_ROLE,
            meta=None,
            set_by=set_by,
            conn=conn,
            pending_checks=None,
            attach_paper=False,
            extra_meta=extra_meta,
        )

    if action == "needs_review":
        if todo_fn is not None:
            todo_fn(claim, placement)
        else:
            log.warning(
                "taproot: compound needs_review placement dropped (no todo_fn): %s",
                placement.reason,
            )
        return None

    raise BadInput(f"unknown placement action: {action!r}")


@dataclass(frozen=True)
class ExtractionOutcome:
    """The hub ids :func:`apply_extraction` wrote — ``atom_hub_ids`` in
    the same order as the ``atoms`` list passed in (a ``needs_review``
    atom contributes no id), and ``compound_hub_id`` (``None`` when there
    was no compound, or the compound placement itself was
    ``needs_review``)."""

    atom_hub_ids: list[int]
    compound_hub_id: int | None


def apply_extraction(
    store: Store,
    *,
    atoms: list[tuple[CanonicalClaim, Placement]],
    compound: tuple[CanonicalClaim, Placement] | None,
    not_claims: tuple[NotClaim, ...] = (),
    paper_ref_id: int,
    role: str = _DEFAULT_ROLE,
    meta: dict[str, Any] | None = None,
    todo_fn: Callable[[CanonicalClaim, Placement], Any] | None = None,
    set_by: ActorSlug = "agent",
    conn: Any = None,
    pending_checks: list[int] | None = None,
) -> ExtractionOutcome:
    """Persist a full :class:`~precis.taproot.canon.ClaimExtraction` through
    the write door — the decomposition-aware orchestrator on top of
    :func:`apply_placement` (docs/backlog/taproot-atomic-claims.md step 2).

    Canon (LLM/ANN) stays out of this module: the caller has already run
    ``block`` -> ``dedup_judge`` -> ``place`` per atom and for the compound,
    and hands in the resulting ``(claim, placement)`` pairs — this function
    only writes.

    1. Each ``atoms`` pair -> :func:`apply_placement` exactly as for a
       single claim (mint/attach + evidence edge; ``needs_review`` files a
       todo and contributes no hub id to
       :attr:`ExtractionOutcome.atom_hub_ids`).
    2. ``compound`` (if given) -> mint-or-converge with **no** evidence
       edge (step 3) via :func:`_apply_compound_placement`, which also
       writes/merges the ``not_claims`` audit memo (step 8) onto the
       compound hub.
    3. Every atom hub that was actually placed gets a
       ``link_claims(atom, compound, relation="conjunct-of")`` — the single
       write door, idempotent, so a re-run of the same extraction converges
       rather than duplicating edges.

    Idempotency falls out of the primitives it calls: :func:`mint_hub`'s
    content-derived pub_id converges re-runs, :func:`link_claims` no-ops on
    an existing edge, and :func:`_merge_not_claims_memo` is sha-keyed and
    existing-wins. ``conn``/``pending_checks``/``todo_fn``/``set_by`` all
    thread through to :func:`apply_placement` and the compound path
    unchanged from their single-claim meaning.
    """
    atom_hub_ids: list[int] = []
    for atom_claim, atom_placement in atoms:
        hub_id = apply_placement(
            store,
            atom_claim,
            atom_placement,
            paper_ref_id=paper_ref_id,
            role=role,
            meta=meta,
            todo_fn=todo_fn,
            set_by=set_by,
            conn=conn,
            pending_checks=pending_checks,
        )
        if hub_id is not None:
            atom_hub_ids.append(hub_id)

    compound_hub_id: int | None = None
    if compound is not None:
        compound_claim, compound_placement = compound
        compound_hub_id = _apply_compound_placement(
            store,
            compound_claim,
            compound_placement,
            not_claims=not_claims,
            todo_fn=todo_fn,
            set_by=set_by,
            conn=conn,
        )

    if compound_hub_id is not None:
        for atom_hub_id in atom_hub_ids:
            link_claims(
                store,
                from_hub_ref_id=atom_hub_id,
                to_hub_ref_id=compound_hub_id,
                relation="conjunct-of",
                set_by=set_by,
                conn=conn,
            )

    return ExtractionOutcome(atom_hub_ids=atom_hub_ids, compound_hub_id=compound_hub_id)


__all__ = [
    "CLAIM_LINK_RELATIONS",
    "EVIDENCE_SRC_KINDS",
    "HUB_ROLES",
    "ExtractionOutcome",
    "TitleRoundTripError",
    "apply_extraction",
    "apply_placement",
    "attach_evidence",
    "link_claims",
    "mint_hub",
    "refine_claim_sentence",
]
