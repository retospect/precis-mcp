"""Taproot Phase 2 — the single write door for claim hubs + evidence edges.

Build ticket: ``docs/backlog/taproot-phase2-hub-node.md``; governance:
Taproot evidence relations; design: ``docs/backlog/taproot.md`` §"The core model".

**Single write path (open #16).** Every hub-finding and every
``establishes``/``corroborates``/``contradicts`` evidence edge is written
through this module. A raw ``INSERT`` / ``store.add_link`` for these
relations elsewhere bypasses the vocabulary + ``TAPROOT:claim`` guards below
and is a defect — the exact silent-junk-edge error taproot exists to prevent.

Six functions:

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
6. :func:`merge_hubs` — collapse one already-minted hub (the "loser")
   into another ("winner"): repoint every evidence/claim-link edge, dedup
   against edges the winner already holds, drop self-loops, and retire the
   loser. The merge door (docs/backlog/claim-hub-merge-door.md) none of
   the above provide — :func:`apply_placement` only prevents a *second*
   hub being minted for the same claim, it can't collapse two that both
   already exist.

Plus, since reground (``docs/backlog/taproot-reground.md``), a matching
**removal** door — :func:`remove_evidence` / :func:`reattach_as_contradicts`,
with :func:`append_reground_log`'s ``meta.reground_log`` audit trail and
the :class:`WouldStrandHub` guard. ``links`` has no ``deleted_at``, so
dropping an edge is a hard delete: the log is the only record it ever
happened, and the strand guard is what keeps a half-applied prune plan
from silently emptying a hub.

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
from typing import TYPE_CHECKING, Any, Literal

from psycopg.errors import UniqueViolation

from precis.errors import BadInput
from precis.handlers._link_tag_ops import validate_relation
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.ingest.provenance import check_ref_retraction
from precis.store.types import ActorSlug, BlockInsert, Tag
from precis.taproot.canon import (
    STATUS_CANONICAL,
    STATUS_NAMESPACE,
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
    and is the chunk :func:`canon.block` ANN-retrieves over for dedup);
    ``claim.scope`` → ``meta.scope``; ``STATUS:canonical``;
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
    ``finding_body`` chunk lands in *this* transaction, but its embedding is
    written async (the derived queue — ``embed:bge-m3`` runs later), so
    :func:`~precis.taproot.canon.block` can return zero candidates for a
    claim whose hub was just minted but not yet embedded. Two findings
    asserting the identical claim (successive
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
            Tag.closed(STATUS_NAMESPACE, STATUS_CANONICAL),
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
    3. Every card variant (``ord < 0``) is deleted, and nothing re-emits one.
       No pass in this codebase derives a hub's ``card_combined``:
       :func:`mint_hub` never emits one, the handler create door's
       ``emits_card`` branch is off for ``finding``, and the single pass that
       DELETE+INSERTs a finding's ``card_combined``
       (``workers/chase.py::_snapshot_chain``) runs only at chain termination
       for a ``STATUS:tracing`` finding, never for a ``STATUS:canonical`` hub.
       That is a closed gap rather than an open one: since 2026-08-19
       :func:`~precis.taproot.canon.block` ANN-retrieves over the
       ``finding_body`` chunk replaced in step 2, so the dedup index tracks
       the new wording by construction. Deleting is still the right branch —
       a stale card must never keep matching the OLD wording anywhere.
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


# ── reground: the edge-REMOVAL door ─────────────────────────────────
#
# ``attach_evidence`` above is the single *write* door; until reground
# (docs/backlog/taproot-reground.md) there was no *removal* door at all,
# because ``hub_refine`` was strictly additive. Reground audits existing
# edges and auto-removes proxy grounding, so removal needs the same
# single-choke-point treatment — plus one thing attach doesn't need: an
# audit trail. ``links`` has **no ``deleted_at`` column**, so a removal is
# a HARD DELETE; nothing is left behind to read afterwards. The log below
# IS the record.


#: ``finding.meta`` key carrying a claim hub's reground audit trail
#: (docs/backlog/taproot-reground.md stage 2): an append-only list of
#: :func:`reground_log_entry` records — one per removal, contradicts
#: re-attach, or deliberately *withheld* action. Read by the claim page /
#: any human asking "why is this edge gone?".
META_REGROUND_LOG = "reground_log"

#: Ceiling on :data:`META_REGROUND_LOG` length — oldest entries drop
#: first. Reground is converging-by-construction (the ``reground_seen``
#: sha-memo in :mod:`precis.workers.hub_refine`), so a hub only ever
#: approaches this bound if its claim sentence is re-edited dozens of
#: times; leaving the list unbounded would let one pathological hub grow
#: its ``meta`` row without limit.
REGROUND_LOG_MAX = 200


@dataclass(frozen=True)
class EvidenceHandle:
    """One committed evidence edge at the grain reground diffs on:
    ``(source ref, grounding chunk, relation)``.

    Deliberately **not** the ``links.link_id`` surrogate: the applier's
    read-back (docs/backlog/taproot-reground.md §"Applier must enforce
    add-first") has to compare an *intent* it formed before the write
    against state it re-read after the commit, and an intent has no
    link_id. ``src_chunk_id`` is ``None`` for a ref-level (``pa<id>``)
    edge — the same NULL the ``links`` UNIQUE index treats as a distinct
    endpoint.
    """

    src_ref_id: int
    src_chunk_id: int | None
    relation: str


def live_evidence_handles(conn: Any, hub_ref_id: int) -> set[EvidenceHandle]:
    """Every evidence edge (:data:`HUB_ROLES`) currently pointing at
    ``hub_ref_id``, as :class:`EvidenceHandle`\\ s.

    No ``deleted_at IS NULL`` filter — ``links`` has no such column, and
    adding one to the predicate *errors* rather than merely
    under-returning (docs/backlog/taproot-reground.md §"applier
    contract"). Every row here is live by construction.
    """
    rows = conn.execute(
        "SELECT src_ref_id, src_chunk_id, relation FROM links "
        "WHERE dst_ref_id = %s AND relation = ANY(%s)",
        (hub_ref_id, sorted(HUB_ROLES)),
    ).fetchall()
    return {
        EvidenceHandle(
            src_ref_id=int(r[0]),
            src_chunk_id=int(r[1]) if r[1] is not None else None,
            relation=str(r[2]),
        )
        for r in rows
    }


def live_evidence_count(conn: Any, hub_ref_id: int) -> int:
    """How many live evidence edges ``hub_ref_id`` carries — the
    "would this removal strand the hub at zero?" guard's input."""
    return len(live_evidence_handles(conn, hub_ref_id))


def reground_log_entry(
    *,
    src_ref_id: int,
    src_chunk_id: int | None,
    relation: str,
    verdict: str,
    reason: str,
    action: str,
    sha: str | None = None,
    handle: str | None = None,
) -> dict[str, Any]:
    """Build one :data:`META_REGROUND_LOG` record.

    The spec's four required fields are ``edge`` / ``verdict`` / ``reason``
    / ``sha``; the structured ``src_ref_id``/``src_chunk_id``/``relation``
    triple rides along so the log is queryable without re-parsing
    ``edge``, and ``action`` distinguishes what actually happened
    (``removed`` / ``reattached-contradicts`` / ``added`` / ``withheld``)
    from what was judged.
    """
    edge = handle or (
        f"ref:{src_ref_id}"
        if src_chunk_id is None
        else f"ref:{src_ref_id}#chunk:{src_chunk_id}"
    )
    return {
        "at": datetime.now(UTC).isoformat(),
        "edge": edge,
        "src_ref_id": src_ref_id,
        "src_chunk_id": src_chunk_id,
        "relation": relation,
        "verdict": verdict,
        "reason": reason,
        "action": action,
        "sha": sha,
    }


def append_reground_log(
    store: Store,
    hub_ref_id: int,
    entries: list[dict[str, Any]],
    *,
    conn: Any = None,
) -> None:
    """Append ``entries`` to ``meta.reground_log``, truncating to the
    newest :data:`REGROUND_LOG_MAX`.

    A read-modify-write on ``meta`` (``store.update_ref``'s ``meta_patch``
    is a *top-level* ``meta || patch`` merge, so the list has to be rebuilt
    whole). Same lost-update exposure as ``hub_refine``'s
    ``taproot_rejected`` memo under two concurrent passes on one hub — the
    unresolved "conflict-safe memo write" open item in
    ``docs/backlog/taproot-reground.md``; run reground on one host, as the
    enablement runbook already requires. A vanished hub is a silent no-op
    rather than a raise: the log is an audit nicety, never worth failing a
    removal that already happened.
    """
    if not entries:
        return

    def _do(c: Any) -> None:
        row = c.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (hub_ref_id,),
        ).fetchone()
        if row is None:
            return
        existing = list(dict(row[0] or {}).get(META_REGROUND_LOG) or [])
        existing.extend(entries)
        store.update_ref(
            hub_ref_id,
            meta_patch={META_REGROUND_LOG: existing[-REGROUND_LOG_MAX:]},
            conn=c,
        )

    if conn is not None:
        _do(conn)
        return
    with store.tx() as c:
        _do(c)


class WouldStrandHub(BadInput):
    """Raised by :func:`remove_evidence` when the removal would take a
    claim hub to **zero** live evidence edges and the caller did not pass
    ``allow_last=True``.

    The failure this exists to prevent is on the record: the manual
    123-hub pass over draft 173020 ran add-first as a *prompt instruction*
    only, and under partial failure (adds blocked, paired prunes not) two
    hubs were pruned to zero live edges before a human caught it
    (docs/backlog/taproot-reground.md). A hub SHOULD be allowed to reach
    zero — that is how "questionable" is expressed (emergent
    ``unverified`` via ``.trust``) — but only as a deliberate, authorized
    RETIRE, never as the accidental residue of a half-applied plan. So
    the door refuses by default and the caller has to say so out loud.
    """


def remove_evidence(
    store: Store,
    *,
    hub_ref_id: int,
    src_ref_id: int,
    role: str,
    src_chunk_id: int | None = None,
    reason: str,
    verdict: str = "PRUNE",
    claim_sha: str | None = None,
    handle: str | None = None,
    conn: Any = None,
    allow_last: bool = False,
    log: bool = True,
) -> int:
    """Remove one ``source --role--> hub`` evidence edge. Returns the
    number of rows deleted (``0`` when the edge was already gone).

    The removal counterpart of :func:`attach_evidence`, and the only
    sanctioned way to drop a :data:`HUB_ROLES` edge. Guards, in order:

    * ``role`` must be one of :data:`HUB_ROLES`;
    * ``hub_ref_id`` must be a live ``TAPROOT:claim`` finding (same guard
      attach applies — a caller pointing at a review note or a non-finding
      is a bug either way);
    * unless ``allow_last=True``, the hub must still carry at least one
      live evidence edge *after* this removal, else
      :class:`WouldStrandHub` is raised **inside the transaction**, so a
      caller sharing its ``conn`` rolls the whole attempt back rather than
      landing a stranded hub.

      Caveat: this strand guard reads live edges with a plain
      ``SELECT`` (:func:`live_evidence_handles`), not ``SELECT ... FOR
      UPDATE`` — it takes no row lock. Two concurrent removals of
      *different* edges on the same hub can each observe the other's
      edge as still live, each pass the ``<=1`` check, and both commit,
      stranding the hub. Covered by the same documented one-host
      enablement rule as the ``meta`` read-modify-write races
      (:func:`append_reground_log`'s docstring,
      ``docs/runbooks/taproot-chase-enablement.md``) — not a bug this
      function fixes on its own.

    Then a chunk-id-exact ``DELETE``, scoped to ``dst_chunk_id IS NULL``
    — every :data:`HUB_ROLES` evidence edge lands on the hub itself
    (never a specific chunk of it) by convention, and this predicate
    enforces that convention at the hard-delete door rather than trusting
    every caller upstream to have kept to it. Deliberately not
    ``store.remove_link``: that door resolves its endpoint from an
    ``ord``, and a stale/renumbered/retired grounding chunk (seen for
    real — fi191322's ``source_handle`` in the pilot) would silently match
    nothing. ``links`` carries no retraction-ripple hook for
    ``establishes``/``corroborates``/``contradicts``
    (``store._argument_ops.RETRACTION_RELATIONS``), so nothing is bypassed
    by going direct.

    ``log=True`` appends a :func:`reground_log_entry` to
    :data:`META_REGROUND_LOG` — on by default, because a hard delete that
    records nothing is exactly the un-auditable removal this door exists
    to prevent. Only a caller that writes its own richer entry (the
    contradicts re-attach below) passes ``log=False``.
    """
    if role not in HUB_ROLES:
        raise BadInput(
            f"invalid evidence role: {role!r}",
            options=sorted(HUB_ROLES),
            next=f"role must be one of {sorted(HUB_ROLES)}",
        )

    def _do(c: Any) -> int:
        if not _is_claim_hub(hub_ref_id, conn=c):
            raise BadInput(
                f"hub_ref_id={hub_ref_id} is not a TAPROOT:claim finding",
                next=(
                    "evidence edges (and their removal) belong to claim hubs — "
                    "pick a TAPROOT:claim finding"
                ),
            )
        live = live_evidence_handles(c, hub_ref_id)
        target = EvidenceHandle(
            src_ref_id=src_ref_id, src_chunk_id=src_chunk_id, relation=role
        )
        if target not in live:
            return 0
        if not allow_last and len(live) <= 1:
            raise WouldStrandHub(
                f"removing {role} edge (src_ref_id={src_ref_id}, "
                f"src_chunk_id={src_chunk_id}) would leave hub "
                f"{hub_ref_id} with zero evidence edges",
                next=(
                    "attach the replacement primary FIRST and confirm it "
                    "committed, or pass allow_last=True if this is an "
                    "authorized retire"
                ),
            )
        cur = c.execute(
            "DELETE FROM links WHERE dst_ref_id = %s AND dst_chunk_id IS NULL "
            "AND src_ref_id = %s AND src_chunk_id IS NOT DISTINCT FROM %s "
            "AND relation = %s",
            (hub_ref_id, src_ref_id, src_chunk_id, role),
        )
        n = cur.rowcount or 0
        if n and log:
            append_reground_log(
                store,
                hub_ref_id,
                [
                    reground_log_entry(
                        src_ref_id=src_ref_id,
                        src_chunk_id=src_chunk_id,
                        relation=role,
                        verdict=verdict,
                        reason=reason,
                        action="removed",
                        sha=claim_sha,
                        handle=handle,
                    )
                ],
                conn=c,
            )
        return n

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


def reattach_as_contradicts(
    store: Store,
    *,
    hub_ref_id: int,
    src_ref_id: int,
    src_chunk_id: int | None = None,
    from_role: str = "corroborates",
    reason: str,
    claim_sha: str | None = None,
    handle: str | None = None,
    meta: dict[str, Any] | None = None,
    set_by: str = "system",
    conn: Any = None,
) -> bool:
    """Convert one evidence edge from ``from_role`` to ``contradicts``.
    Returns ``True`` when the ``contradicts`` edge is committed.

    The contradictor path (taproot evidence relations, ADR 0073): a
    passage carrying *primary content that runs counter to the claim* is
    the most informative edge a hub can hold — it must never be
    plain-dropped by the prune stage. So this door **adds first**: the
    ``contradicts`` edge goes in through :func:`attach_evidence`, is read
    back from ``links`` (never trusting the write's return —
    docs/backlog/taproot-reground.md), and only a confirmed re-attach
    releases the removal of the old edge. If the read-back misses, the
    old edge stays and ``False`` comes back for the caller to flag.

    Both halves run in ONE transaction on purpose: unlike the applier's
    cross-transaction add→prune pairing (where the add commits with the
    enrichment pass and the prune follows later), these two writes are
    two faces of one decision — rolling them back together can never
    strand the hub, whereas committing the removal without the re-attach
    can. ``allow_last=True`` on the removal is therefore safe *and*
    necessary: the replacement is already in the same transaction, so the
    strand guard would otherwise refuse a hub whose only edge is the one
    being converted.
    """

    def _do(c: Any) -> bool:
        attach_evidence(
            store,
            hub_ref_id=hub_ref_id,
            paper_ref_id=src_ref_id,
            role="contradicts",
            meta=meta,
            set_by=set_by,
            conn=c,
            check_retraction=False,
        )
        committed = live_evidence_handles(c, hub_ref_id)
        if not any(
            h.src_ref_id == src_ref_id and h.relation == "contradicts"
            for h in committed
        ):
            log.warning(
                "taproot: contradicts re-attach for hub %s src %s did not read "
                "back — leaving the %s edge in place",
                hub_ref_id,
                src_ref_id,
                from_role,
            )
            return False
        removed = remove_evidence(
            store,
            hub_ref_id=hub_ref_id,
            src_ref_id=src_ref_id,
            src_chunk_id=src_chunk_id,
            role=from_role,
            reason=reason,
            verdict="CONTRADICTS",
            claim_sha=claim_sha,
            handle=handle,
            conn=c,
            allow_last=True,
            log=False,
        )
        append_reground_log(
            store,
            hub_ref_id,
            [
                reground_log_entry(
                    src_ref_id=src_ref_id,
                    src_chunk_id=src_chunk_id,
                    relation=from_role,
                    verdict="CONTRADICTS",
                    reason=reason,
                    action=(
                        "reattached-contradicts"
                        if removed
                        else "reattached-contradicts (no prior edge)"
                    ),
                    sha=claim_sha,
                    handle=handle,
                )
            ],
            conn=c,
        )
        return True

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


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


#: The relation a hypothesis hub carries to each artifact that provoked it
#: (migration 0135). Motivation, **not** evidence: it never flows support,
#: and :mod:`precis.workers.hub_refine` must never widen along it.
MOTIVATION_RELATION = "motivated-by"

#: Kinds a hypothesis may name as a motivator. Papers and patents (the
#: :data:`EVIDENCE_SRC_KINDS` sources) plus other claim hubs — a conjecture
#: can be provoked by a passage nobody ever minted as a claim, which is why
#: this is wider than :func:`link_claims`' hub↔hub contract. Also a
#: ``structure`` — an instrument measurement is an observation a hypothesis
#: can be provoked by, same as a paper. Memories and quests are deliberately
#: absent: a dream may *think* with its own prior notes, but a signed
#: artifact cites sources, and a note is not one — and a quest is a
#: container, not an observation.
MOTIVATION_SRC_KINDS: frozenset[str] = frozenset(
    {"paper", "patent", "finding", "structure"}
)


def attach_motivation(
    store: Store,
    *,
    hub_ref_id: int,
    motivator_ref_id: int,
    meta: dict[str, Any] | None = None,
    set_by: str = "agent",
    conn: Any = None,
) -> bool:
    """Write one ``hub --motivated-by--> artifact`` edge. Returns ``True`` if
    a new edge was written, ``False`` if it already existed.

    The graph form of a ``hypothesis`` nanopub's provenance: the artifact
    emits ``prov:wasDerivedFrom`` + ``precis:motivatedBy`` pointing at what
    provoked the conjecture (:func:`precis.nanopub.assemble._provenance`,
    hypothesis branch), and this edge points the same way — hub → motivator,
    the derived-node-to-source direction :func:`link_claims` established.

    This is the third taproot write door, beside :func:`attach_evidence`
    (paper→hub support) and :func:`link_claims` (hub↔hub advisory). Keeping
    motivation out of :data:`HUB_ROLES` is load-bearing rather than tidy:
    :mod:`precis.workers.hub_refine` widens a claim by searching for evidence
    that supports it, and aiming that at a conjecture makes a confirmation
    engine of it (``docs/backlog/claim-review-mechanism.md``). A motivator is
    what prompted the guess, never support for it.

    ``meta['source_handle']`` (a ``pc<id>`` handle) grounds the edge at the
    specific passage, materialised as ``dst_chunk_id`` — the motivator is on
    the *destination* side here, the mirror of an evidence edge's ``src_pos``.
    Idempotent on ``(hub, motivator, relation)``.
    """
    if hub_ref_id == motivator_ref_id:
        raise BadInput(
            f"a hypothesis cannot be motivated by itself (ref_id={hub_ref_id})",
            next="name a paper, patent, a different claim hub, or a measured structure",
        )
    validated = validate_relation(MOTIVATION_RELATION, store=store)

    def _do(c: Any) -> bool:
        if not _is_claim_hub(hub_ref_id, conn=c):
            raise BadInput(
                f"hub_ref_id={hub_ref_id} is not a TAPROOT:claim finding",
                next="motivation edges hang off a claim hub",
            )
        src = store.fetch_refs_by_ids([motivator_ref_id]).get(motivator_ref_id)
        if src is None or src.kind not in MOTIVATION_SRC_KINDS:
            kind_desc = "unknown" if src is None else src.kind
            raise BadInput(
                f"motivator_ref_id={motivator_ref_id} is a {kind_desc!r} ref",
                options=sorted(MOTIVATION_SRC_KINDS),
                next=(
                    "a hypothesis is motivated by a paper, a patent, another "
                    "claim hub, or a measured structure (an instrument "
                    "observation) — not by a note"
                ),
            )
        existing = c.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = %s",
            (hub_ref_id, motivator_ref_id, validated),
        ).fetchone()
        if existing is not None:
            return False
        # `_grounding_chunk_ord` is named for the evidence path but is
        # generic: it resolves meta['source_handle'] and refuses a chunk
        # belonging to any ref other than the one named. Here that ref is the
        # motivator, which lands on dst_pos rather than src_pos.
        dst_pos = _grounding_chunk_ord(store, paper_ref_id=motivator_ref_id, meta=meta)
        store.add_link(
            src_ref_id=hub_ref_id,
            dst_ref_id=motivator_ref_id,
            dst_pos=dst_pos,
            relation=validated,
            meta=meta,
            set_by=set_by,
            conn=c,
        )
        log.info(
            "taproot: hub %s --%s--> %s (%s)",
            hub_ref_id,
            validated,
            motivator_ref_id,
            "passage" if dst_pos is not None else "ref-level",
        )
        return True

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


#: The relation :func:`merge_hubs` writes from the retired loser to the
#: winner to record a collapse (requirement 6, docs/backlog/claim-hub-merge-
#: door.md). Reused, not invented: it is exactly the relation
#: ``precis taproot refine`` / :func:`link_claims` already writes for
#: "source hub is subsumed by target hub" (its default ``relation="refines"``)
#: — a merge collapse is the limit case of a refine (the loser contributes
#: nothing a reader can't already get from the winner). Deliberately **not**
#: ``supersedes``/``superseded-by``: those are live in the ``relations``
#: table but reserved for **nanopub artifact** versioning
#: (``nanopub_publish``'s state machine / the mirror ops that derive
#: ``retracted_by``/``superseded_by`` from that edge) — overloading them for
#: hub *identity* collapse would make a nanopub-artifact reader see a claim
#: hub in its supersession graph that was never itself published. Also
#: deliberately not ``meta.superseded_by`` (the cross-kind soft-delete
#: tombstone :meth:`~precis.store.Store.follow_supersede` transparently
#: redirects universal-handle resolution through, used by paper dedup /
#: memory consolidation) for the same reason — same word, different job;
#: stamping it here would make a `fi<loser>` handle silently resolve as the
#: winner post-merge, which is a *bigger* behavior change than this door was
#: asked to make (open question, not decided here — see the module's
#: ``merge_hubs`` docstring "Known gap").
MERGE_COLLAPSE_RELATION = "refines"


@dataclass(frozen=True)
class MergeEdge:
    """One ``links`` row touching the loser hub in a :func:`merge_hubs`
    plan, and what the merge did (or would do, under ``dry_run=True``)
    with it.

    ``direction`` is relative to the **loser**: ``"outbound"`` means the
    loser was ``src_ref_id`` (repointing moves ``src_ref_id`` to the
    winner); ``"inbound"`` means the loser was ``dst_ref_id`` (repointing
    moves ``dst_ref_id``). ``other_ref_id`` is the edge's non-loser
    endpoint.
    """

    link_id: int
    relation: str
    direction: Literal["outbound", "inbound"]
    other_ref_id: int
    src_chunk_id: int | None
    dst_chunk_id: int | None
    meta: dict[str, Any]
    action: Literal["repoint", "drop_redundant", "drop_self_loop"]
    #: Set only when ``action == "drop_redundant"`` — the winner-side
    #: ``link_id`` this edge would collide with after repointing.
    duplicate_of_link_id: int | None = None


@dataclass(frozen=True)
class MergePlan:
    """:func:`merge_hubs`'s full plan — the same object is returned for a
    dry run (nothing applied) and a real run (exactly what was applied),
    so the CLI has one formatter for both (requirement 8, merge-door doc).
    """

    loser_ref_id: int
    winner_ref_id: int
    #: True iff the loser was already retired with a recorded collapse
    #: link to this exact winner — a no-op re-run (requirement 7).
    #: ``edges``/``can_merge``/``block_reason`` are meaningless when this
    #: is True (nothing left to compute).
    already_merged: bool
    #: False iff either side is past ``candidate`` in ``nanopub_publish``
    #: (requirement 5) — the merge is refused. Always True when
    #: ``already_merged`` is True.
    can_merge: bool
    block_reason: str | None
    edges: list[MergeEdge]


def _publish_states_past_candidate(conn: Any, ref_id: int) -> list[str]:
    """The distinct ``nanopub_publish.state`` values recorded for
    ``ref_id`` that are not ``'candidate'`` — empty when the hub never
    entered the publish flow, or every row is still ``candidate``. **Any**
    non-candidate row blocks a merge (requirement 5), including a
    terminal ``superseded``/``retracted``/``rejected`` one: those still
    mean an immutable ``nanopub_artifacts`` row exists (or, for
    ``rejected``, that a human reviewed this exact claim string) keyed to
    this ``claim_ref_id`` — merging the hub away would orphan that
    history's referent."""
    rows = conn.execute(
        "SELECT DISTINCT state FROM nanopub_publish "
        "WHERE claim_ref_id = %s AND state != 'candidate'",
        (ref_id,),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def _collapse_recorded(conn: Any, *, loser_ref_id: int, winner_ref_id: int) -> bool:
    """True iff the loser already carries the :data:`MERGE_COLLAPSE_RELATION`
    edge to this exact winner — the idempotency check (requirement 7)."""
    row = conn.execute(
        "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
        "AND relation = %s LIMIT 1",
        (loser_ref_id, winner_ref_id, MERGE_COLLAPSE_RELATION),
    ).fetchone()
    return row is not None


def _build_merge_plan(conn: Any, *, loser_ref_id: int, winner_ref_id: int) -> MergePlan:
    """Read-only: compute the full :class:`MergePlan` for collapsing
    ``loser_ref_id`` into ``winner_ref_id`` on ``conn``. Never writes.

    Raises :class:`BadInput` for structural/usage problems (a ref that
    doesn't exist at all, a live-but-not-a-claim-hub ref, or a
    soft-deleted loser with no recorded merge into this winner) — those
    are caller mistakes, not part of "the plan" a dry-run reports
    (requirement 8's plan is about the merge's own mechanics, e.g. the
    publish-state refusal, which IS reported rather than raised here).
    """
    loser_row = conn.execute(
        "SELECT deleted_at FROM refs WHERE ref_id = %s", (loser_ref_id,)
    ).fetchone()
    if loser_row is None:
        raise BadInput(f"loser ref_id={loser_ref_id} does not exist")

    if loser_row[0] is not None:  # loser already soft-deleted
        if _collapse_recorded(
            conn, loser_ref_id=loser_ref_id, winner_ref_id=winner_ref_id
        ):
            return MergePlan(
                loser_ref_id=loser_ref_id,
                winner_ref_id=winner_ref_id,
                already_merged=True,
                can_merge=True,
                block_reason=None,
                edges=[],
            )
        raise BadInput(
            f"loser ref_id={loser_ref_id} is already deleted, but carries no "
            f"recorded {MERGE_COLLAPSE_RELATION!r} merge link into winner "
            f"ref_id={winner_ref_id} -- refusing to guess what deleted it",
            next=(
                "if this loser was merged into a DIFFERENT winner, that's not "
                "this merge's business; if it was deleted for an unrelated "
                "reason, taproot merge cannot restore/redirect it"
            ),
        )

    if not _is_claim_hub(loser_ref_id, conn=conn):
        raise BadInput(
            f"loser ref_id={loser_ref_id} is not a live TAPROOT:claim hub",
            next="merge only collapses one claim hub into another",
        )
    if not _is_claim_hub(winner_ref_id, conn=conn):
        raise BadInput(
            f"winner ref_id={winner_ref_id} is not a live TAPROOT:claim hub",
            next="merge only collapses one claim hub into another",
        )

    blocked: list[str] = []
    for label, ref_id in (("loser", loser_ref_id), ("winner", winner_ref_id)):
        states = _publish_states_past_candidate(conn, ref_id)
        if states:
            blocked.append(
                f"{label} ref_id={ref_id} has nanopub_publish state(s) {states} "
                "(past 'candidate')"
            )
    can_merge = not blocked
    block_reason = "; ".join(blocked) if blocked else None

    # Enumerate EVERY live link touching the loser, in either direction,
    # regardless of relation -- the live vocabulary, not a hardcoded list
    # (requirement 1: evidence edges, cites from draft chunks, refines,
    # contradicts, conjunct-of, and anything a future relation adds).
    rows = conn.execute(
        "SELECT link_id, src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
        "       relation, meta "
        "FROM links WHERE src_ref_id = %s OR dst_ref_id = %s",
        (loser_ref_id, loser_ref_id),
    ).fetchall()

    edges: list[MergeEdge] = []
    for (
        link_id,
        src_ref_id,
        src_chunk_id,
        dst_ref_id,
        dst_chunk_id,
        relation,
        meta,
    ) in rows:
        outbound = src_ref_id == loser_ref_id
        new_src = winner_ref_id if src_ref_id == loser_ref_id else src_ref_id
        new_dst = winner_ref_id if dst_ref_id == loser_ref_id else dst_ref_id
        other_ref_id = dst_ref_id if outbound else src_ref_id
        direction: Literal["outbound", "inbound"] = (
            "outbound" if outbound else "inbound"
        )
        edge_meta = dict(meta or {})

        if new_src == new_dst:
            # The loser was linked directly to the winner (or this row is
            # itself a stale collapse edge from a prior partial attempt) --
            # repointing would make the winner point at itself. Drop
            # (requirement 3), never insert a self-loop.
            edges.append(
                MergeEdge(
                    link_id=int(link_id),
                    relation=relation,
                    direction=direction,
                    other_ref_id=int(other_ref_id),
                    src_chunk_id=src_chunk_id,
                    dst_chunk_id=dst_chunk_id,
                    meta=edge_meta,
                    action="drop_self_loop",
                )
            )
            continue

        # Collision check (requirement 2): does the winner already hold
        # this exact edge? Full endpoint match -- src/dst ref AND chunk,
        # not just (src_ref, dst_ref, relation) -- so two chunk-grounded
        # cites from DIFFERENT loser chunks stay distinct (they are NOT
        # redundant: "the set of chunks that support this point").
        dup = conn.execute(
            "SELECT link_id FROM links "
            "WHERE src_ref_id = %s AND src_chunk_id IS NOT DISTINCT FROM %s "
            "AND dst_ref_id = %s AND dst_chunk_id IS NOT DISTINCT FROM %s "
            "AND relation = %s AND link_id != %s LIMIT 1",
            (new_src, src_chunk_id, new_dst, dst_chunk_id, relation, link_id),
        ).fetchone()

        if dup is not None:
            edges.append(
                MergeEdge(
                    link_id=int(link_id),
                    relation=relation,
                    direction=direction,
                    other_ref_id=int(other_ref_id),
                    src_chunk_id=src_chunk_id,
                    dst_chunk_id=dst_chunk_id,
                    meta=edge_meta,
                    action="drop_redundant",
                    duplicate_of_link_id=int(dup[0]),
                )
            )
        else:
            edges.append(
                MergeEdge(
                    link_id=int(link_id),
                    relation=relation,
                    direction=direction,
                    other_ref_id=int(other_ref_id),
                    src_chunk_id=src_chunk_id,
                    dst_chunk_id=dst_chunk_id,
                    meta=edge_meta,
                    action="repoint",
                )
            )

    return MergePlan(
        loser_ref_id=loser_ref_id,
        winner_ref_id=winner_ref_id,
        already_merged=False,
        can_merge=can_merge,
        block_reason=block_reason,
        edges=edges,
    )


def _apply_merge_plan(
    store: Store,
    conn: Any,
    plan: MergePlan,
    *,
    set_by: ActorSlug,
) -> None:
    """Write side of :func:`merge_hubs`: apply an already-computed,
    already-allowed (``plan.can_merge``) :class:`MergePlan` on ``conn``.

    Re-uses the plan's own per-edge decisions rather than re-querying for
    collisions -- the read-then-decide-then-write split means every write
    here is a plain ``UPDATE``/``DELETE`` by ``link_id``, no risk of a
    duplicate decision changing between planning and applying within the
    same transaction.
    """
    for edge in plan.edges:
        if edge.action == "repoint":
            column = "src_ref_id" if edge.direction == "outbound" else "dst_ref_id"
            conn.execute(
                f"UPDATE links SET {column} = %s WHERE link_id = %s",
                (plan.winner_ref_id, edge.link_id),
            )
        else:  # drop_redundant | drop_self_loop -- links has no deleted_at
            conn.execute("DELETE FROM links WHERE link_id = %s", (edge.link_id,))

    # Record the collapse BEFORE retiring the loser: link_claims requires
    # both endpoints to be live TAPROOT:claim hubs, which the loser still
    # is at this point in the transaction (requirement 6). Idempotent by
    # construction (link_claims no-ops on an existing edge) -- if the loser
    # already carried a refines->winner edge from a manual `taproot refine`
    # done before the merge, the loop above already dropped it as a
    # self-loop (loser->winner, repointed src loser->winner == dst winner),
    # so this always (re)creates exactly one collapse edge.
    link_claims(
        store,
        from_hub_ref_id=plan.loser_ref_id,
        to_hub_ref_id=plan.winner_ref_id,
        relation=MERGE_COLLAPSE_RELATION,
        set_by=set_by,
        conn=conn,
    )

    store.soft_delete_ref(plan.loser_ref_id, conn=conn)


def merge_hubs(
    store: Store,
    *,
    loser_ref_id: int,
    winner_ref_id: int,
    set_by: ActorSlug = "agent",
    dry_run: bool = False,
    conn: Any = None,
) -> MergePlan:
    """Collapse ``loser_ref_id`` into ``winner_ref_id`` -- the merge door
    (docs/backlog/claim-hub-merge-door.md) neither :func:`apply_placement`
    (dedups only against a hub that doesn't exist yet) nor
    :func:`refine_claim_sentence` / :func:`link_claims` (link-don't-merge,
    move no evidence) provide: two hubs that both already exist, one of
    which must absorb the other's evidence graph and stop existing.

    The winner's own ``refs.title`` / ``meta.scope`` / ``pub_id`` are never
    touched (requirement 10) -- rewording a hub for lint compliance is
    :func:`refine_claim_sentence`'s separate job; folding the two together
    would mean a failed reword takes the merge with it.

    Mechanics, all inside one transaction (requirement 9) when
    ``dry_run=False``:

    1. Every live ``links`` row touching the loser (either direction, any
       relation) is either repointed onto the winner, dropped as
       redundant (the winner already holds the identical edge --
       full endpoint match, so two chunk-grounded ``cites`` from
       *different* loser chunks both survive), or dropped as a self-loop
       (the loser and winner were directly linked). See :func:`_build_merge_plan`.
    2. The collapse is recorded via a fresh
       ``loser --{MERGE_COLLAPSE_RELATION}--> winner`` link (written
       while the loser is still live, through :func:`link_claims` -- the
       existing single write door for hub<->hub links, not a raw INSERT).
    3. The loser is soft-deleted (``refs.deleted_at``).

    ``links`` has no ``deleted_at`` -- every drop above is a **hard**
    DELETE with no undo (requirement 4). ``dry_run=True`` is therefore the
    only safety net: it runs the exact same planning logic
    (:func:`_build_merge_plan`) and returns the resulting :class:`MergePlan`
    **without ever opening a write transaction**, so there is nothing to
    roll back to get a truthful preview.

    **Refusal** (requirement 5): if either side has a ``nanopub_publish``
    row past ``'candidate'``, the merge changes an identity a
    signed/published artifact already froze. A *dry* run still computes
    and returns the check result (``plan.can_merge`` / ``plan.block_reason``)
    rather than raising -- "someone has to be able to read that and catch
    a mistake before it happens" (the spec's own words) requires the
    refusal to be visible in the printed plan, not just at write time. A
    *real* run (``dry_run=False``) raises :class:`BadInput` naming which
    side and what state before writing anything.

    **Idempotent** (requirement 7): a loser that is already soft-deleted
    *and* already carries the collapse-record edge to this exact winner
    short-circuits to ``plan.already_merged=True`` with no further reads
    or writes -- neither an error nor a second attempt to move edges that
    have already moved. A soft-deleted loser with NO such record (deleted
    for some unrelated reason, or merged into a *different* winner) is a
    caller mistake, not idempotency, and raises.

    **Known gap**: the loser's own ``nanopub_publish`` row(s) in state
    ``'candidate'`` (if any -- allowed, since ``'candidate'`` never blocks
    a merge) are left pointing at the now-deleted ``claim_ref_id``. Nothing
    here migrates or retargets them onto the winner -- whether a candidate
    publish attempt should follow its hub into the winner, or simply lapse,
    is a product decision this build doesn't make; flagged, not solved.

    Args:
        loser_ref_id: The hub being retired. Absorbed into ``winner_ref_id``.
        winner_ref_id: The hub that survives, unchanged (requirement 10).
        set_by: Audit actor for the edge writes.
        dry_run: Compute and return the plan; write nothing.
        conn: An open transaction to fold this write into; ``None`` opens
            its own (``store.tx()`` for a real run, a plain read connection
            for ``dry_run=True`` -- see above).

    Returns:
        The computed :class:`MergePlan` -- identical shape for a dry run
        (nothing applied) and a real run (exactly what was applied).

    Raises:
        BadInput: same ``ref_id`` on both sides; a ref that doesn't exist;
            a live ref that isn't a claim hub; a soft-deleted loser with no
            recorded merge into this winner; or (real run only) either
            side past ``candidate`` in ``nanopub_publish``.
    """
    if loser_ref_id == winner_ref_id:
        raise BadInput(
            f"cannot merge a hub into itself (ref_id={loser_ref_id})",
            next="loser and winner must be two distinct claim hubs",
        )

    def _do(c: Any) -> MergePlan:
        plan = _build_merge_plan(
            c, loser_ref_id=loser_ref_id, winner_ref_id=winner_ref_id
        )
        if dry_run or plan.already_merged:
            return plan
        if not plan.can_merge:
            raise BadInput(
                f"cannot merge fi{loser_ref_id} into fi{winner_ref_id}: "
                f"{plan.block_reason}",
                next=(
                    "a hub past 'candidate' in nanopub_publish has a frozen "
                    "identity -- merging it would retroactively re-identify "
                    "a reviewed/signed/published artifact"
                ),
            )
        _apply_merge_plan(store, c, plan, set_by=set_by)
        log.info(
            "taproot: merged hub ref_id=%s into ref_id=%s (%d edge(s) "
            "repointed, %d dropped)",
            loser_ref_id,
            winner_ref_id,
            sum(1 for e in plan.edges if e.action == "repoint"),
            sum(1 for e in plan.edges if e.action != "repoint"),
        )
        return plan

    if conn is not None:
        return _do(conn)
    if dry_run:
        # Read-only planning never needs a write transaction.
        with store.pool.connection() as c:
            return _do(c)
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
    "MERGE_COLLAPSE_RELATION",
    "META_REGROUND_LOG",
    "MOTIVATION_RELATION",
    "MOTIVATION_SRC_KINDS",
    "REGROUND_LOG_MAX",
    "EvidenceHandle",
    "ExtractionOutcome",
    "MergeEdge",
    "MergePlan",
    "TitleRoundTripError",
    "WouldStrandHub",
    "append_reground_log",
    "apply_extraction",
    "apply_placement",
    "attach_evidence",
    "attach_motivation",
    "link_claims",
    "live_evidence_count",
    "live_evidence_handles",
    "merge_hubs",
    "mint_hub",
    "reattach_as_contradicts",
    "refine_claim_sentence",
    "reground_log_entry",
    "remove_evidence",
]
