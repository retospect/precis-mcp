"""``edit(kind='finding', ...)`` — pick_candidate / title / unacquirable_note.

Split out of ``finding.py`` (docs/backlog/codereview-handler-size-cleanups.md):
this state machine (~350 lines across three mutually-exclusive ops) only
ever touched ``self.store``/``self.kind``, never any other handler state,
so it moves as free functions taking the store (and the finding kind
string) explicitly. ``FindingHandler.edit`` calls :func:`edit` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers._finding_common import fetch_ref_any_kind
from precis.response import Response
from precis.store.types import Tag
from precis.taproot import authoring, hub

if TYPE_CHECKING:
    from precis.store import Store

_STATUS_NAMESPACE = "STATUS"
_STATUS_TRACING = "tracing"


def edit(
    store: Store,
    *,
    kind: str,
    id: int | str | None = None,
    pick_candidate: str | int | None = None,
    title: str | None = None,
    unacquirable_note: str | None = None,
    unacquirable_mode: str | None = None,
    dry_run: bool | str | None = None,
) -> Response:
    """Resolve a ``STATUS:multi_candidate`` finding by picking one cite,
    retitle a ``TAPROOT:claim`` hub, or record an author's
    unacquirable-source override. Mutually exclusive kwargs — pass
    exactly one.

    **Pick a candidate.** When the chase reaches a chunk citing
    multiple references (e.g. ``[12,13]``) and can't disambiguate
    automatically, it tags the finding ``STATUS:multi_candidate`` and
    writes one ``derived-from`` link per candidate with
    ``meta.candidate=true``. The user reads the candidates via
    ``get(kind='finding', id=N)``, then promotes one with:

        edit(kind='finding', id=N, pick_candidate='miller23a')
        edit(kind='finding', id=N, pick_candidate=42)   # by ref_id

    Effect:
    * The chosen candidate link loses its ``meta.candidate`` marker
      (becomes a regular ``derived-from`` edge).
    * The other candidate links are deleted.
    * The finding's status flips back to ``STATUS:tracing`` so the
      chase advances on the next pass.
    * ``meta.chain``'s frontier entry is replaced with the picked
      target so the next chase pass walks the right path.

    Idempotent — picking the same candidate twice is fine (re-flips to
    tracing, no-op on links).

    ``title=`` is a **different** operation, only valid on a
    ``TAPROOT:claim`` hub (``id`` must resolve to one — see
    :func:`~precis.taproot.authoring.resolve_hub_ref_id`): it reroutes
    through :func:`precis.taproot.hub.refine_claim_sentence`, the single
    write door that keeps ``refs.title``, the ``finding_body`` chunk, and
    the content-derived ``pub_id`` in sync when a hub's claim sentence is
    reworded (fixing a claim-quality issue, e.g. a dangling
    demonstrative). A plain (non-hub) finding has no ``edit(title=…)``
    door — mutate its claim via a fresh ``put()``.

    **Unacquirable override.** A print-only / undigitized source is
    legitimately citeable even when no digital copy is obtainable.
    Recording that intent suppresses the trust surfaces' "unverified"
    mark on this claim (the trust-surfaces override door; this is a
    **claim-level** declaration about THIS finding, never inherited from
    its source paper — a paper's own Meta-tab "can't get it" is a plain
    acquirability fact and never softens a claim by itself, see
    ``precis-taproot-help``. Never applies to the "unsupported" mark — a
    negative terminal verification always outranks the override, the
    paper was read):

        edit(kind='finding', id=N, unacquirable_note='print-only 1962 monograph')
        edit(kind='finding', id=N, unacquirable_note='abstract states the figure',
             unacquirable_mode='abstract')

    Sets ``meta.unacquirable_override = {mode, by, at, note}``.
    ``unacquirable_mode`` picks the trust state: ``'abstract'`` → Ⓐ
    (the abstract on file backs THIS claim, full text unread) vs
    ``'vouched'`` (✍ — author vouches, source unobtainable), the default
    when omitted (also how a legacy no-``mode`` override reads on the way
    in). Only meaningful alongside ``unacquirable_note``; supplying it
    without a note is a ``BadInput``. Settable pre-emptively on ANY
    lifecycle state — not gated to ``STATUS:dead_chain(reason=unacquirable)``,
    since the author may know a source is print-only before the chase
    ever attempts acquisition. ``note`` is required (empty/whitespace
    rejected — a silent override defeats the audit purpose); ``at`` is
    server-stamped; ``by`` is ``'agent'`` today (no caller-identity
    channel exists yet for a handler to read one from). Idempotent —
    re-setting just overwrites the prior ``mode``/``by``/``at``/``note``.

    No op here supports ``dry_run`` (see below).
    """
    given = [
        name
        for name, value in (
            ("pick_candidate", pick_candidate),
            ("title", title),
            ("unacquirable_note", unacquirable_note),
        )
        if value is not None
    ]
    if len(given) > 1:
        raise BadInput(
            "edit(kind='finding') accepts exactly one of pick_candidate, "
            f"title, or unacquirable_note — got {', '.join(given)}",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') / "
                "edit(kind='finding', id='fi<N>', title='<reworded claim>') / "
                "edit(kind='finding', id=<N>, unacquirable_note='<why>')"
            ),
        )
    if unacquirable_mode is not None and unacquirable_note is None:
        raise BadInput(
            "edit(kind='finding') requires unacquirable_note when "
            "unacquirable_mode is given",
            next=(
                "edit(kind='finding', id=<N>, unacquirable_note='<why>', "
                "unacquirable_mode='abstract')"
            ),
        )
    if title is not None:
        if dry_run:
            raise BadInput(
                "edit(kind='finding', title=…) does not support dry_run — "
                "the retitle has no preview; omit dry_run to apply",
                next="edit(kind='finding', id='fi<N>', title='<reworded claim>')",
            )
        return _retitle_hub(store, id=id, title=title)
    if dry_run:
        # Neither op has a faithful preview yet: pick_candidate rewrites
        # links + flips status; unacquirable_note writes an audit-trail
        # meta patch. Reject loudly rather than silently apply on
        # dry_run (a data-loss footgun either way).
        raise BadInput(
            "edit(kind='finding') does not support dry_run — it either "
            "promotes a candidate cite (rewrites links + flips status) or "
            "records an unacquirable-source override; omit dry_run to apply",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') or "
                "edit(kind='finding', id=<N>, unacquirable_note='<why>')"
            ),
        )
    if id is None:
        raise BadInput(
            "edit(kind='finding') requires id=<finding ref_id or pub_id>",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') or "
                "edit(kind='finding', id=<N>, unacquirable_note='<why>')"
            ),
        )
    if unacquirable_note is not None:
        return _set_unacquirable_override(
            store, kind=kind, raw_id=id, note=unacquirable_note, mode=unacquirable_mode
        )
    if pick_candidate is None or (
        isinstance(pick_candidate, str) and not pick_candidate.strip()
    ):
        raise BadInput(
            "edit(kind='finding') requires pick_candidate=<cite_key or ref_id> "
            "or unacquirable_note=<why source can't be digitally acquired>",
            next=(
                "pick_candidate='miller23a' (or the candidate's ref_id) — "
                "see get(kind='finding', id=N) for the candidate list"
            ),
        )

    finding_ref_id = _resolve_finding_ref_id(store, kind=kind, raw_id=id)

    # Pull all candidate links (outbound derived-from with
    # meta.candidate=true). The chase worker writes these as a
    # batch when it hits a multi-cite chunk.
    candidates = [
        link
        for link in store.links_for(
            finding_ref_id, direction="out", relation="derived-from"
        )
        if (link.meta or {}).get("candidate") is True
    ]
    if not candidates:
        raise BadInput(
            f"finding id={finding_ref_id} has no candidate links — nothing to pick",
            next=(
                "get(kind='finding', id=<N>) — the chain may already "
                "be resolved (STATUS:established) or this finding is "
                "in a different state"
            ),
        )

    picked_link, other_links = _match_candidate(
        store, candidates, pick_candidate=pick_candidate
    )

    with store.tx() as conn:
        # Promote the picked link: clear the candidate flag.
        # No store-level helper for "patch one link's meta", so
        # update by primary key directly — the candidate marker
        # was the only meaningful key on these links.
        conn.execute(
            "UPDATE links SET meta = meta - 'candidate' WHERE link_id = %s",
            (picked_link.id,),
        )
        # Drop the losing candidates by primary key (the
        # store-level ``remove_link`` matches endpoint pairs;
        # we have the exact link rows already so this is
        # tighter and skips the chunk_id resolution dance).
        if other_links:
            conn.execute(
                "DELETE FROM links WHERE link_id = ANY(%s)",
                ([link.id for link in other_links],),
            )

        # Replace the chain's frontier entry with the picked
        # target so the next chase pass walks from there.
        ref = store.get_ref(kind=kind, id=finding_ref_id)
        assert ref is not None
        meta = dict(ref.meta or {})
        chain = list(meta.get("chain") or [])
        if chain:
            # The frontier (last hop) is the multi-cite source —
            # swap it for the picked next-hop so the chain reads
            # as "this is what the chase advanced to."
            chain[-1] = {
                "ref_id": picked_link.dst_ref_id,
                "chunk_id": None,
                "ord": picked_link.dst_ord,
            }
            store.update_ref(finding_ref_id, meta_patch={"chain": chain}, conn=conn)

        # Flip status back to tracing so the chase worker
        # re-claims this row on the next pass.
        store.add_tag(
            finding_ref_id,
            Tag.closed(_STATUS_NAMESPACE, _STATUS_TRACING),
            set_by="user",
            replace_prefix=True,
            conn=conn,
        )

    # Resolve a human-friendly handle for the response body.
    picked_ref = fetch_ref_any_kind(store, picked_link.dst_ref_id)
    picked_handle = picked_ref.slug or f"ref:{picked_link.dst_ref_id}"
    return Response(
        body=(
            f"picked candidate {picked_handle} on finding id={finding_ref_id}\n"
            f"dropped {len(other_links)} other candidate(s); "
            f"status flipped to STATUS:{_STATUS_TRACING}\n"
            f"next: precis worker --only chase --once  "
            f"(or wait for the next pass)"
        )
    )


def _retitle_hub(store: Store, *, id: int | str | None, title: str) -> Response:
    """``edit(kind='finding', title=…)`` — reword a claim hub's sentence.

    ``id`` must resolve to a live ``TAPROOT:claim`` hub (mirrors the
    ``link()`` Taproot-routing check in ``finding.py``). A plain
    finding — no ``edit(title=…)`` door exists for it — raises the same
    sharp ``BadInput`` an unresolvable/non-hub target does.
    """
    if id is None:
        raise BadInput(
            "edit(kind='finding', title=…) requires id=<hub ref_id, "
            "fi<id> handle, or pub_id>",
            next="edit(kind='finding', id='fi<N>', title='<reworded claim>')",
        )
    try:
        hub_ref_id = authoring.resolve_hub_ref_id(store, id)
    except BadInput:
        hub_ref_id = None
    if hub_ref_id is None:
        raise BadInput(
            f"edit(kind='finding', title=…) only retitles a TAPROOT:claim "
            f"hub — id={id!r} does not resolve to one",
            next=(
                "a plain (non-hub) finding has no title-edit door — "
                "record a fresh put(kind='finding', title=…) instead"
            ),
        )
    try:
        result = hub.refine_claim_sentence(store, hub_ref_id, title, set_by="agent")
    except ValueError as exc:
        raise BadInput(
            f"edit(kind='finding', id='fi{hub_ref_id}', title=…) failed: {exc}",
            next=(
                "the reworded sentence's pub_id collides with a different "
                "hub — pick distinct wording, or resolve the dedup by hand "
                "(link_claims / delete one hub) before retitling"
            ),
        ) from exc
    alias_note = (
        " (old pub_id kept as an alias — existing [handle] cites still resolve)"
        if result["pub_id_alias_kept"]
        else ""
    )
    return Response(
        body=(
            f"retitled claim hub fi{hub_ref_id}\n"
            f"old: {result['old_title']}\n"
            f"new: {result['new_title']}\n"
            f"pub_id: {result['pub_id']}{alias_note}"
        )
    )


def _set_unacquirable_override(
    store: Store,
    *,
    kind: str,
    raw_id: int | str,
    note: str,
    mode: str | None = None,
) -> Response:
    """Write ``meta.unacquirable_override`` — the write path behind
    ``edit(kind='finding', unacquirable_note=…)`` (the trust-surfaces
    override door, claim-level — never inherited from the source paper).
    ``note`` required non-empty; the override is otherwise settable on
    any finding regardless of its current lifecycle status. ``mode``
    must be ``'abstract'`` or ``'vouched'`` when given; ``None`` defaults
    to ``'vouched'`` (matches how a legacy no-``mode`` override reads on
    the way in)."""
    if not note.strip():
        raise BadInput(
            "edit(kind='finding') requires a non-empty unacquirable_note "
            "— a silent override defeats the audit purpose",
            next=(
                "edit(kind='finding', id=<N>, "
                "unacquirable_note='<why this source cannot be digitally acquired>')"
            ),
        )
    if mode is not None and mode not in ("abstract", "vouched"):
        raise BadInput(
            f"edit(kind='finding') unacquirable_mode must be 'abstract' or "
            f"'vouched', got {mode!r}",
            next=(
                "edit(kind='finding', id=<N>, unacquirable_note='<why>', "
                "unacquirable_mode='abstract')"
            ),
        )
    resolved_mode = mode or "vouched"
    finding_ref_id = _resolve_finding_ref_id(store, kind=kind, raw_id=raw_id)
    override = {
        "mode": resolved_mode,
        "by": "agent",
        "at": datetime.now(UTC).isoformat(),
        "note": note.strip(),
    }
    store.update_ref(finding_ref_id, meta_patch={"unacquirable_override": override})
    mark = "Ⓐ abstract-backs-it" if resolved_mode == "abstract" else "✍ author-vouched"
    return Response(
        body=(
            f"recorded unacquirable override on finding id={finding_ref_id}\n"
            f"note: {override['note']}\n"
            f"trust surfaces now render this claim {mark} — a calm mark, no "
            "longer the ⚠ unverified triangle, but NOT clean (the full text "
            "was never read). A terminal verification that the source "
            "doesn't back it still outranks the override."
        )
    )


def _resolve_finding_ref_id(store: Store, *, kind: str, raw_id: int | str) -> int:
    """Resolve ``id=`` to a finding ref_id.

    Accepts a numeric ref_id, a numeric-string ref_id, or a ``pub_id``
    (the agent-facing placeholder shape).
    """
    if isinstance(raw_id, int):
        ref = store.get_ref(kind=kind, id=raw_id)
        if ref is None:
            raise BadInput(f"no finding with ref_id={raw_id}")
        return raw_id
    s = str(raw_id).strip()
    if s.isdigit():
        return _resolve_finding_ref_id(store, kind=kind, raw_id=int(s))
    # Treat as pub_id.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id FROM ref_identifiers ri "
            "JOIN refs r ON r.ref_id = ri.ref_id "
            "WHERE ri.id_kind = 'pub_id' AND ri.id_value = %s "
            "  AND r.kind = 'finding' AND r.retired_at IS NULL",
            (s,),
        ).fetchone()
    if row is None:
        raise BadInput(f"no finding with pub_id={s!r}")
    return int(row[0])


def _match_candidate(
    store: Store, candidates: list, *, pick_candidate: str | int
) -> tuple[Any, list]:
    """Pick the link matching ``pick_candidate``; return
    ``(picked, others)``. Accepts a cite_key (slug) or ref_id."""
    if isinstance(pick_candidate, int) or (
        isinstance(pick_candidate, str) and pick_candidate.strip().isdigit()
    ):
        target_ref_id = int(pick_candidate)
        picked = [c for c in candidates if c.dst_ref_id == target_ref_id]
        if not picked:
            raise BadInput(
                f"ref_id={target_ref_id} is not in the candidate list",
                options=sorted(str(c.dst_ref_id) for c in candidates),
            )
        return picked[0], [c for c in candidates if c.id != picked[0].id]

    # Match by cite_key (slug). Resolve each candidate ref's
    # cite_key once and look the input up against that map.
    target_slug = str(pick_candidate).strip()
    for c in candidates:
        ref = fetch_ref_any_kind(store, c.dst_ref_id)
        if (ref.slug or "") == target_slug:
            return c, [other for other in candidates if other.id != c.id]
    candidate_slugs = sorted(
        (fetch_ref_any_kind(store, c.dst_ref_id).slug or f"ref:{c.dst_ref_id}")
        for c in candidates
    )
    raise BadInput(
        f"no candidate matches pick_candidate={target_slug!r}",
        options=candidate_slugs,
    )
