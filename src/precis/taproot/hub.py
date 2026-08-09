"""Taproot Phase 2 — the single write door for claim hubs + evidence edges.

Build ticket: ``docs/backlog/taproot-phase2-hub-node.md``; governance:
Taproot evidence relations; design: ``docs/backlog/taproot.md`` §"The core model".

**Single write path (open #16).** Every hub-finding and every
``establishes``/``corroborates``/``contradicts`` evidence edge is written
through this module. A raw ``INSERT`` / ``store.add_link`` for these
relations elsewhere bypasses the vocabulary + ``TAPROOT:claim`` guards below
and is a defect — the exact silent-junk-edge error taproot exists to prevent.

Four functions:

1. :func:`mint_hub` — create a ``TAPROOT:claim`` ``finding`` hub for a
   paper-grounded claim (open #15: only paper-sourced claims become hubs).
2. :func:`attach_evidence` — write one ``paper --role--> hub`` edge, ``role``
   in :data:`HUB_ROLES`, guarding the target is actually a claim hub and the
   source is a paper/patent ref (:data:`_EVIDENCE_SRC_KINDS` backstop).
   Also the single choke point for the deterministic prophetic-example
   caveat (patent-evidence-parity phase 4): a patent source whose grounding
   chunk carries ``PATENT_EXAMPLE:prophetic`` (``data/axes/
   patent_example.yaml``) gets :data:`PROPHETIC_EXAMPLE_CAVEAT` appended to
   ``meta.caveats`` here, mechanically — never via the verify LLM prompt,
   which is unchanged.
3. :func:`apply_placement` — bridge a :class:`~precis.taproot.canon.Placement`
   (the canonicalizer's verdict) to the writes above; a ``needs_review``
   placement files a ``kind='todo'`` (via an injected ``todo_fn``) and never
   auto-attaches (open #16).
4. :func:`link_claims` — the claim->claim advisory ``refines`` edge
   (:data:`CLAIM_LINK_RELATIONS`, migration 0100): link-don't-merge, carries
   no evidence flow.

Callers that populate the edges: ``workers/chase.py::_taproot_bridge`` (the
forward bridge, supplies the verdict ``meta``), ``workers/hub_refine.py``,
and the authoring/backfill doors (:mod:`precis.taproot.authoring` /
:mod:`precis.taproot.backfill`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from psycopg.errors import UniqueViolation

from precis.errors import BadInput
from precis.handlers._link_tag_ops import validate_relation
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.store.types import BlockInsert, Tag
from precis.taproot.canon import (
    TAPROOT_CLAIM,
    TAPROOT_NAMESPACE,
    CanonicalClaim,
    Placement,
)

log = logging.getLogger(__name__)

#: The three evidence-edge roles a ``paper --> hub`` link may carry
#: (taproot.md Axis A). ``establishes`` = originator (migration 0094);
#: ``corroborates`` (0085) and ``contradicts`` (0001) reuse existing slugs —
#: endpoint kinds disambiguate. Each is still validated through
#: :func:`validate_relation` against the live ``relations`` table before write.
HUB_ROLES: frozenset[str] = frozenset({"establishes", "corroborates", "contradicts"})

#: Claim→claim advisory link relations a hub may carry to ANOTHER hub
#: (migration 0100, taproot evidence relations amendment). ``refines`` = "source hub is a
#: sharper/reworded version of the target hub" — link-don't-merge, NO
#: evidence flow (each hub keeps its own paper→hub edges). Written through
#: :func:`link_claims` (the single write door), distinct from the paper→hub
#: evidence edges of :data:`HUB_ROLES`. A frozenset so v1's one relation can
#: grow (e.g. a future ``related-to`` claim-link) without touching callers.
CLAIM_LINK_RELATIONS: frozenset[str] = frozenset({"refines"})

#: The default role :func:`apply_placement` attaches with. ``corroborates`` is
#: the *safe* assumption — never falsely claim a paper is the originator.
#: Promotion to ``establishes`` is a derivation over the citation graph
#: (taproot.md §"Seniority is derived", Phase 2c/3), not a write-time guess.
_DEFAULT_ROLE = "corroborates"

#: :func:`attach_evidence`'s src-kind guard (open #15: only paper-sourced
#: claims get evidence) — defense-in-depth behind
#: :func:`precis.taproot.authoring.resolve_paper_ref_id`'s authoritative
#: check, for any caller that reaches this door directly.
_EVIDENCE_SRC_KINDS: frozenset[str] = frozenset({"paper", "patent"})

_STATUS_NS = "STATUS"
_STATUS_CANONICAL = "canonical"


def _grounding_chunk_ord(
    store: Any, *, paper_ref_id: int, meta: dict[str, Any] | None
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


def _is_claim_hub(store: Any, ref_id: int, *, conn: Any) -> bool:
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


def mint_hub(
    store: Any,
    claim: CanonicalClaim,
    *,
    set_by: str = "agent",
    conn: Any = None,
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
        ref = store.insert_ref(
            kind="finding",
            slug=None,
            title=claim.sentence.strip()[:200],
            meta={"scope": dict(claim.scope), "source": "taproot"},
            conn=c,
        )
        store.insert_blocks(
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
    store: Any,
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
    (truncated ``[:200]``), the ``finding_body`` chunk at ``ord=0``, and
    implicitly in the content-derived ``pub_id``. This is the retitle door
    that keeps all three in sync when a hub's wording needs fixing (e.g. a
    claim-quality rubric flags a dangling demonstrative) — there is otherwise
    no way to reword a hub short of deleting and re-minting it, which would
    orphan every evidence edge already attached.

    Writes, all inside one transaction:

    1. ``refs.title = sentence.strip()[:200]``; ``meta.scope`` is replaced
       (not merged) when ``scope`` is given, else the hub's existing scope
       is kept.
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
        "pub_id_alias_kept"}`` — ``pub_id`` is the (possibly unchanged)
        current pub_id after the write; ``pub_id_alias_kept`` is True iff a
        new pub_id row was inserted (the old one stays live as an alias).

    Raises:
        ValueError: ``hub_ref_id`` isn't a live ``TAPROOT:claim`` hub,
            ``sentence`` is empty/whitespace, or the new pub_id already
            belongs to a different ref (names that ref_id).
    """
    stripped = sentence.strip() if sentence else ""
    if not stripped:
        raise ValueError("refine_claim_sentence requires a non-empty sentence")

    def _do(c: Any) -> dict[str, Any]:
        if not _is_claim_hub(store, hub_ref_id, conn=c):
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

        new_title = stripped[:200]
        store.update_ref(
            hub_ref_id,
            title=new_title,
            meta_patch={"scope": effective_scope} if scope is not None else None,
            conn=c,
        )

        # (2) finding_body chunk — DELETE+INSERT at ord=0.
        store.replace_body_chunk(
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
        }

    if conn is not None:
        return _do(conn)
    with store.tx() as c:
        return _do(c)


def attach_evidence(
    store: Any,
    *,
    hub_ref_id: int,
    paper_ref_id: int,
    role: str,
    meta: dict[str, Any] | None = None,
    set_by: str = "agent",
    conn: Any = None,
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
    """
    if role not in HUB_ROLES:
        raise BadInput(
            f"invalid evidence role: {role!r}",
            options=sorted(HUB_ROLES),
            next=f"role must be one of {sorted(HUB_ROLES)}",
        )
    # FK/vocab pre-flight (raises BadInput on an unregistered slug).
    validated = validate_relation(role, store=store)

    def _do(c: Any) -> None:
        if not _is_claim_hub(store, hub_ref_id, conn=c):
            raise BadInput(
                f"hub_ref_id={hub_ref_id} is not a TAPROOT:claim finding",
                next=(
                    "evidence edges attach only to claim hubs — tag the "
                    "finding TAPROOT:claim (axis:taproot) or pick a claim hub"
                ),
            )
        src = store.fetch_refs_by_ids([paper_ref_id], include_deleted=True).get(
            paper_ref_id
        )
        if src is None or src.kind not in _EVIDENCE_SRC_KINDS:
            kind_desc = "unknown" if src is None else src.kind
            raise BadInput(
                f"paper_ref_id={paper_ref_id} is a {kind_desc!r} ref, not a "
                "paper/patent",
                next="evidence edges attach only from a paper/patent source ref",
            )
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


def link_claims(
    store: Any,
    *,
    from_hub_ref_id: int,
    to_hub_ref_id: int,
    relation: str = "refines",
    set_by: str = "agent",
    conn: Any = None,
) -> bool:
    """Write one hub ``--relation--> hub`` advisory claim-link. Returns
    ``True`` if a new edge was written, ``False`` if it already existed.

    ``relation`` must be one of :data:`CLAIM_LINK_RELATIONS` (v1: ``refines``)
    *and* a registered relation (checked via :func:`validate_relation` — the
    friendly pre-flight for the ``links_relation_fkey`` FK). **Both**
    endpoints must be live ``TAPROOT:claim`` findings — a claim-link joins two
    claim hubs (never a paper, a review note, or a non-finding), and the two
    must differ (a hub can't refine itself).

    This is the single write door for claim→claim links, the sibling of
    :func:`attach_evidence` for paper→hub evidence edges (open #16).
    Unlike evidence, a claim-link carries **no evidence flow** — the hubs keep
    their own paper→hub edges; the link is surfaced read-only by the fisheye
    Claims ring (:mod:`precis.utils.refeye`). Idempotent: an identical
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
            if not _is_claim_hub(store, ref_id, conn=c):
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


def apply_placement(
    store: Any,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    paper_ref_id: int,
    role: str = _DEFAULT_ROLE,
    meta: dict[str, Any] | None = None,
    todo_fn: Callable[[CanonicalClaim, Placement], Any] | None = None,
    set_by: str = "agent",
    conn: Any = None,
) -> int | None:
    """Persist a canonicalizer :class:`Placement` through the write door.

    Routing (mirrors :func:`precis.taproot.canon.place`):

    * ``attach`` — attach this paper as evidence on the matched hub.
    * ``new`` — mint a hub, attach the paper.
    * ``new_contradicts`` — mint a hub, attach the paper, and link the new hub
      ``contradicts`` the existing (opposite-*claim*) hub.
    * ``needs_review`` — file a ``kind='todo'`` via ``todo_fn`` and attach
      **nothing** (open #16: a risky merge is never auto-applied).

    Returns the hub ref_id it attached to / minted, or ``None`` for
    ``needs_review``. ``role`` is the evidence role for the paper edge
    (default :data:`_DEFAULT_ROLE`); originator promotion is derived later.

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
    if action == "attach":
        if placement.hub_ref_id is None:
            raise BadInput("attach placement has no hub_ref_id")
        attach_evidence(
            store,
            hub_ref_id=placement.hub_ref_id,
            paper_ref_id=paper_ref_id,
            role=role,
            meta=meta,
            set_by=set_by,
            conn=conn,
        )
        return placement.hub_ref_id

    if action in ("new", "new_contradicts"):

        def _do(c: Any) -> int:
            hub = mint_hub(store, claim, set_by=set_by, conn=c)
            attach_evidence(
                store,
                hub_ref_id=hub,
                paper_ref_id=paper_ref_id,
                role=role,
                meta=meta,
                set_by=set_by,
                conn=c,
            )
            if action == "new_contradicts":
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
            return _do(c)

    if action == "needs_review":
        if todo_fn is not None:
            todo_fn(claim, placement)
        else:
            log.warning(
                "taproot: needs_review placement dropped (no todo_fn): %s",
                placement.reason,
            )
        return None

    raise BadInput(f"unknown placement action: {action!r}")


__all__ = [
    "CLAIM_LINK_RELATIONS",
    "HUB_ROLES",
    "apply_placement",
    "attach_evidence",
    "link_claims",
    "mint_hub",
    "refine_claim_sentence",
]
