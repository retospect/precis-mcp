"""Cite-seeded claim-hub authoring — a helper over the taproot write door.

A human (or a backfill) already knows a draft cites specific papers for a
specific sentence; this module turns that ``sentence + scope + supporters``
spec into hub/evidence writes **through the existing primitives**
(:func:`precis.taproot.hub.mint_hub` / :func:`~precis.taproot.hub.attach_evidence`)
— it is a thin authoring layer, not a second write path. The forward
(canonicalizer-driven) path stays :func:`precis.taproot.hub.apply_placement`;
this one exists for the case where a human already has the citation evidence
in hand and wants to mint the hub directly, e.g. backfilling a legacy draft's
paper-chunk citations into taproot claim hubs.

Idempotent by construction: :func:`mint_hub` converges to the same hub for
identical ``(sentence, scope)`` content, and :func:`seed_claim_hub` skips
re-attaching an evidence edge that already exists (checked directly against
``links`` before writing) so a re-run of the same spec is a no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.store.types import ActorSlug
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import (
    _DEFAULT_ROLE,
    EVIDENCE_SRC_KINDS,
    HUB_ROLES,
    _grounding_chunk_ord,
    attach_evidence,
    mint_hub,
)
from precis.utils.mentions import resolve_handle_ref, resolve_handle_target

if TYPE_CHECKING:
    from precis.store.store import Store

__all__ = ["resolve_hub_ref_id", "resolve_paper_ref_id", "seed_claim_hub"]


#: Supporter refs must be one of these kinds (taproot evidence relations /
#: hub.py open #15: only paper-sourced claims get evidence) — the same
#: :data:`precis.taproot.hub.EVIDENCE_SRC_KINDS` :func:`~precis.taproot.
#: hub.attach_evidence` itself gates on, so a supporter this module accepts
#: can never fail that door's own kind check. See that module's docstring
#: for why the set is hand-maintained rather than ``KindSpec.corpus_role``-
#: derived.
_SUPPORTER_KINDS: frozenset[str] = EVIDENCE_SRC_KINDS


def resolve_paper_ref_id(store: Store, paper: int | str) -> int:
    """Resolve a supporter's ``paper`` to a live ``ref_id``.

    Accepts a bare ``ref_id`` (``int``), a universal handle (``pa5``), a
    ``cite_key`` slug, or a ``pub_id`` slug — reusing the same resolvers the
    write-time autolinker and ``[handle]`` mention parser already use
    (:func:`precis.utils.mentions.resolve_handle_target` /
    :func:`~precis.utils.mentions.resolve_handle_ref`), not a new parser.

    Raises:
        BadInput: ``paper`` doesn't resolve to a live ref, or resolves to a
            live ref whose kind isn't in :data:`_SUPPORTER_KINDS` (a typo'd/
            wrong handle must never mint a non-evidence-sourced edge —
            open #15).
    """
    if isinstance(paper, bool):  # bool is an int subclass -- guard the footgun
        raise BadInput(f"cannot resolve supporter paper: {paper!r}")

    ref_id: int | None
    if isinstance(paper, int):
        ref_id = paper
    else:
        token = paper.strip()
        target = resolve_handle_target(store, token)
        if target is not None:
            ref_id = target.dst_ref_id
        else:
            ref = resolve_handle_ref(store, token, include_deleted=False)
            ref_id = int(ref.id) if ref is not None else None

    if ref_id is not None:
        live = store.fetch_refs_by_ids([ref_id], include_deleted=False).get(ref_id)
        if live is not None:
            if live.kind not in _SUPPORTER_KINDS:
                raise BadInput(
                    f"supporter {paper!r} resolved to a {live.kind!r} ref "
                    f"(ref_id={ref_id}), not an evidence-source kind",
                    next=(
                        "supporters must be evidence-sourced (kind "
                        f"{'/'.join(sorted(_SUPPORTER_KINDS))}) — check for "
                        "a typo'd/wrong handle"
                    ),
                )
            return ref_id

    raise BadInput(
        f"cannot resolve supporter paper: {paper!r}",
        next=(
            "pass a paper ref_id (int), a 'pa<id>' handle, a cite_key, or a "
            "pub_id slug for a live paper ref"
        ),
    )


def resolve_hub_ref_id(store: Store, hub: int | str) -> int:
    """Resolve a claim-hub reference to a live ``TAPROOT:claim`` hub ref_id.

    Accepts a bare ``ref_id`` (``int``), a ``fi<id>`` finding handle, a
    cite_key, or a ``pub_id`` slug — reusing the same resolvers
    :func:`resolve_paper_ref_id` does (:func:`resolve_handle_target` /
    :func:`resolve_handle_ref`), then gating on the resolved ref actually
    being a claim hub (the ``precis taproot refine`` endpoints, and the
    :func:`~precis.taproot.hub.link_claims` guard, both require a hub — this
    is the friendly pre-flight, so a typo'd/wrong handle fails with a clear
    message before the write door's raw ``BadInput``).

    Raises:
        BadInput: ``hub`` doesn't resolve to a live ref, or resolves to one
            that isn't a ``TAPROOT:claim`` finding.
    """
    from precis.taproot.seniority import is_claim_hub

    if isinstance(hub, bool):  # bool is an int subclass -- guard the footgun
        raise BadInput(f"cannot resolve claim hub: {hub!r}")

    ref_id: int | None
    if isinstance(hub, int):
        ref_id = hub
    else:
        token = hub.strip()
        target = resolve_handle_target(store, token)
        if target is not None:
            ref_id = target.dst_ref_id
        else:
            ref = resolve_handle_ref(store, token, include_deleted=False)
            ref_id = int(ref.id) if ref is not None else None

    if ref_id is not None and is_claim_hub(store, ref_id):
        return ref_id

    raise BadInput(
        f"cannot resolve claim hub: {hub!r}",
        next=(
            "pass a hub ref_id (int), a 'fi<id>' finding handle, a cite_key, "
            "or a pub_id slug for a live TAPROOT:claim hub — mint it first "
            "with 'precis taproot mint' if it doesn't exist yet"
        ),
    )


def _evidence_edge_exists(
    store: Store,
    *,
    paper_ref_id: int,
    hub_ref_id: int,
    role: str,
    src_ord: int | None = None,
    conn: Any = None,
) -> bool:
    """True iff a ``paper --role--> hub`` edge grounded at ``src_ord`` exists.

    ``src_ord`` is the grounding chunk's ordinal (``None`` for a ref-level
    edge). Chunk-scoped so two passages of the same paper are distinct
    edges: re-running a spec re-finds the *exact* (paper, hub, role, chunk)
    edge rather than any edge for the paper — which would have let the
    second passage collapse into the first (the ref-level limitation this
    grounding work removes). The ``chunks`` LEFT JOIN maps ``src_chunk_id``
    back to ``ord`` (NULL for a ref-level edge), and ``IS NOT DISTINCT
    FROM`` makes NULL==NULL match so a ref-level lookup still works.

    ``conn`` lets a caller looping over many supporters (e.g.
    :func:`seed_claim_hub`) reuse one connection instead of opening a
    throwaway one per call; ``conn=None`` keeps the original
    open-a-short-lived-connection behavior.
    """

    def _query(c: Any) -> bool:
        row = c.execute(
            "SELECT 1 FROM links l "
            "LEFT JOIN chunks c ON c.chunk_id = l.src_chunk_id "
            "WHERE l.src_ref_id = %s AND l.dst_ref_id = %s AND l.relation = %s "
            "AND c.ord IS NOT DISTINCT FROM %s",
            (paper_ref_id, hub_ref_id, role, src_ord),
        ).fetchone()
        return row is not None

    if conn is not None:
        return _query(conn)
    with store.pool.connection() as own_conn:
        return _query(own_conn)


def find_hub_by_pub_id(store: Store, pub_id: str) -> int | None:
    """Look up a claim hub's ``ref_id`` by its ``pub_id`` — read-only.

    Mirrors :func:`precis.taproot.hub.mint_hub`'s internal converge-to-attach
    lookup; exposed here so a caller (e.g. the ``precis taproot mint``
    ``--dry-run`` path) can report "already minted" without writing.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers WHERE id_kind = %s AND id_value = %s",
            ("pub_id", pub_id),
        ).fetchone()
    return int(row[0]) if row is not None else None


def seed_claim_hub(
    store: Store,
    *,
    sentence: str,
    scope: dict[str, str],
    supporters: list[dict[str, Any]],
    set_by: ActorSlug = "agent",
) -> dict[str, Any]:
    """Mint (or converge onto) a claim hub for ``sentence``/``scope`` and
    attach each of ``supporters`` as evidence.

    ``supporters`` is a list of dicts:

    * ``paper`` (required) — a ``ref_id`` int, ``pa<id>`` handle, cite_key,
      or pub_id slug (see :func:`resolve_paper_ref_id`).
    * ``role`` (default ``'corroborates'``) — one of
      :data:`precis.taproot.hub.HUB_ROLES`.
    * ``source_handle`` (optional) — the grounding chunk pointer
      (``pc<chunk_id>`` / ``slug~ord``), stored in the edge ``meta``.
    * ``support`` (default ``'yes'``) / ``caveats`` (default ``[]``) — the
      chase-verdict-shaped fields carried in the edge ``meta``.

    Calls :func:`~precis.taproot.hub.mint_hub` ONCE for the claim (idempotent
    by its content-derived pub_id) then :func:`~precis.taproot.hub.attach_evidence`
    once per supporter, skipping any ``(paper, hub, role)`` edge that already
    exists — so re-running with an identical spec mints no second hub and
    attaches no duplicate edge.

    Returns ``{'pub_id', 'hub_ref_id', 'attached', 'already', 'collapsed',
    'ungrounded'}`` — ``attached``/``already`` count new vs.
    skipped-as-already-present evidence edges; ``ungrounded`` counts how
    many of the *newly attached* edges landed ref-level (no resolvable
    ``source_handle`` chunk), i.e. cite the whole paper rather than a
    passage — the CLI surfaces it as a nudge to supply ``source_handle``.
    ``collapsed`` is a list of supporter dicts that mapped to the *same*
    ``(paper, hub, role, chunk)`` edge as an earlier supporter **in this
    same call** (the edge dedup key can only carry one ``meta``, so a
    second supporter differing only by e.g. ``support`` would otherwise
    vanish silently — this surfaces it instead of hiding it in
    ``already``; the earlier supporter's meta always wins).

    Raises:
        BadInput: a supporter's ``paper`` doesn't resolve (or resolves to a
            non-paper/patent ref), or its ``role`` isn't in
            :data:`~precis.taproot.hub.HUB_ROLES`.
    """
    claim = CanonicalClaim(sentence=sentence, scope=dict(scope or {}))
    hub_ref_id = mint_hub(store, claim, set_by=set_by)
    pub_id = make_pub_id(make_taproot_hub_paper_id(claim.sentence, claim.scope))

    attached = 0
    already = 0
    ungrounded = 0
    collapsed: list[dict[str, Any]] = []
    # Dedup key is (paper, role, grounding-chunk): two passages of the same
    # paper are distinct edges now that grounding lands on src_chunk_id, so
    # only a supporter naming the *same* passage collapses.
    seen_edges: set[tuple[int, str, int | None]] = set()
    # One connection reused across every supporter's `_evidence_edge_exists`
    # check (below) rather than opening N throwaway pool connections for an
    # N-supporter mint.
    with store.pool.connection() as conn:
        for supporter in supporters:
            paper = supporter.get("paper")
            if paper is None:
                raise BadInput("supporter missing required 'paper' field")
            role = supporter.get("role") or _DEFAULT_ROLE
            if role not in HUB_ROLES:
                raise BadInput(
                    f"invalid evidence role: {role!r}",
                    options=sorted(HUB_ROLES),
                    next=f"role must be one of {sorted(HUB_ROLES)}",
                )
            paper_ref_id = resolve_paper_ref_id(store, paper)
            src_ord = _grounding_chunk_ord(
                store,
                paper_ref_id=paper_ref_id,
                meta={"source_handle": supporter.get("source_handle")},
            )

            edge_key = (paper_ref_id, role, src_ord)
            if edge_key in seen_edges:
                collapsed.append(
                    {
                        "paper": paper,
                        "paper_ref_id": paper_ref_id,
                        "role": role,
                        "source_handle": supporter.get("source_handle"),
                    }
                )
                continue

            if _evidence_edge_exists(
                store,
                paper_ref_id=paper_ref_id,
                hub_ref_id=hub_ref_id,
                role=role,
                src_ord=src_ord,
                conn=conn,
            ):
                already += 1
                seen_edges.add(edge_key)
                continue

            meta = {
                "support": supporter.get("support", "yes"),
                "caveats": list(supporter.get("caveats") or []),
                "source_handle": supporter.get("source_handle"),
            }
            attach_evidence(
                store,
                hub_ref_id=hub_ref_id,
                paper_ref_id=paper_ref_id,
                role=role,
                meta=meta,
                set_by=set_by,
            )
            attached += 1
            if src_ord is None:
                ungrounded += 1
            seen_edges.add(edge_key)

    return {
        "pub_id": pub_id,
        "hub_ref_id": hub_ref_id,
        "attached": attached,
        "already": already,
        "collapsed": collapsed,
        "ungrounded": ungrounded,
    }
