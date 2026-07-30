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

from typing import Any

from precis.errors import BadInput
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import _DEFAULT_ROLE, HUB_ROLES, attach_evidence, mint_hub
from precis.utils.mentions import resolve_handle_ref, resolve_handle_target

__all__ = ["resolve_paper_ref_id", "seed_claim_hub"]


#: Supporter refs must be one of these kinds (ADR 0073 / hub.py open #15:
#: only paper-sourced claims get evidence). ``patent`` is included alongside
#: ``paper`` — both are citable primary-source documents in this corpus.
_SUPPORTER_KINDS: frozenset[str] = frozenset({"paper", "patent"})


def resolve_paper_ref_id(store: Any, paper: int | str) -> int:
    """Resolve a supporter's ``paper`` to a live ``ref_id``.

    Accepts a bare ``ref_id`` (``int``), a universal handle (``pa5``), a
    ``cite_key`` slug, or a ``pub_id`` slug — reusing the same resolvers the
    write-time autolinker and ``[handle]`` mention parser already use
    (:func:`precis.utils.mentions.resolve_handle_target` /
    :func:`~precis.utils.mentions.resolve_handle_ref`), not a new parser.

    Raises:
        BadInput: ``paper`` doesn't resolve to a live ref, or resolves to a
            live ref that isn't a ``paper``/``patent`` (a typo'd/wrong handle
            must never mint a non-paper-sourced evidence edge — open #15).
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
                    f"(ref_id={ref_id}), not a paper/patent",
                    next=(
                        "supporters must be paper-sourced (kind 'paper' or "
                        "'patent') — check for a typo'd/wrong handle"
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


def _evidence_edge_exists(
    store: Any, *, paper_ref_id: int, hub_ref_id: int, role: str
) -> bool:
    """True iff a ``paper --role--> hub`` edge is already written."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = %s",
            (paper_ref_id, hub_ref_id, role),
        ).fetchone()
    return row is not None


def find_hub_by_pub_id(store: Any, pub_id: str) -> int | None:
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
    store: Any,
    *,
    sentence: str,
    scope: dict[str, str],
    supporters: list[dict[str, Any]],
    set_by: str = "agent",
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

    Returns ``{'pub_id', 'hub_ref_id', 'attached', 'already', 'collapsed'}``
    — ``attached``/``already`` count new vs. skipped-as-already-present
    evidence edges; ``collapsed`` is a list of supporter dicts that mapped
    to the *same* ``(paper, hub, role)`` edge as an earlier supporter **in
    this same call** (the edge dedup key can only carry one ``meta``, so a
    second supporter differing only by e.g. ``source_handle`` would
    otherwise vanish silently — this surfaces it instead of hiding it in
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
    collapsed: list[dict[str, Any]] = []
    seen_edges: set[tuple[int, str]] = set()
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

        edge_key = (paper_ref_id, role)
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
            store, paper_ref_id=paper_ref_id, hub_ref_id=hub_ref_id, role=role
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
        seen_edges.add(edge_key)

    return {
        "pub_id": pub_id,
        "hub_ref_id": hub_ref_id,
        "attached": attached,
        "already": already,
        "collapsed": collapsed,
    }
