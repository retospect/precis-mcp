"""``edit(kind='finding', ...)`` — pick_candidate / title / meta={'scope'} /
unacquirable_note / testable_by+motivation / the body edit.

Split out of ``finding.py`` (docs/backlog/codereview-residuals.md):
this state machine (~350 lines across the mutually-exclusive ops) only
ever touched ``self.store``/``self.kind``, never any other handler state,
so it moves as free functions taking the store (and the finding kind
string) explicitly. ``FindingHandler.edit`` calls :func:`edit` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from precis.errors import BadInput, Gone, NotFound
from precis.handlers import _finding_hypothesis
from precis.handlers._finding_common import fetch_ref_any_kind
from precis.handlers.plaintext import _require_find_and_text
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.response import Response
from precis.store.types import Tag
from precis.taproot import authoring, hub
from precis.taproot.consumer_review import render_users
from precis.taproot.seniority import is_claim_hub
from precis.taproot.sentence_lint import lint_scope
from precis.utils.edit_resolve import (
    EditOp,
    apply_edit,
    format_unified_diff,
    normalize_dry_run,
    render_dry_run_full,
    render_dry_run_header,
)
from precis.utils.secret_scan import mask_secrets
from precis.workers.embed import nudge_fresh_embed

if TYPE_CHECKING:
    from precis.store import Store

_STATUS_NAMESPACE = "STATUS"
_STATUS_TRACING = "tracing"

#: ``next=`` for a retitle/rescope refused after signing
#: (:class:`precis.taproot.hub.HubFrozenError`).
_FROZEN_NEXT = (
    "the hub has signed nanopub bytes, so its identity is frozen — "
    "supersede the nanopub (a human door) and edit the successor instead"
)


def edit(
    store: Store,
    *,
    kind: str,
    id: int | str | None = None,
    mode: str | None = None,
    find: str | None = None,
    text: str | None = None,
    before: str | None = None,
    after: str | None = None,
    where: str | None = None,
    match: str | None = None,
    nth: int | None = None,
    pick_candidate: str | int | None = None,
    title: str | None = None,
    unacquirable_note: str | None = None,
    unacquirable_mode: str | None = None,
    testable_by: str | None = None,
    motivation: str | None = None,
    reason: str | None = None,
    meta: dict[str, Any] | None = None,
    dry_run: bool | str | None = None,
) -> Response:
    """Resolve a ``STATUS:multi_candidate`` finding by picking one cite,
    retitle a ``TAPROOT:claim`` hub, record an author's
    unacquirable-source override, or sharpen a live hypothesis's
    falsification terms. Mutually exclusive kwargs — pass exactly one
    (``testable_by=``/``motivation=`` are the one exception: they may be
    combined in a single call, since both sharpen the same conjecture).

    **Body edit** — ``mode='find-replace'`` (``find=`` + ``text=``,
    ``text=''`` deletes the span) or ``mode='replace'`` (``text=`` is the
    whole new body) rewrites a plain finding's ``finding_body`` chunk
    (gr509620): the claim prose an agent needs to correct without a
    delete+re-put that would lose the id and every inbound link. The
    shared anchored-edit vocabulary applies (``before=``/``after=``/
    ``match=``/``nth=``; ``dry_run=`` previews the diff). The write is
    delete+reinsert (never an in-place UPDATE — embeddings/keywords
    re-derive), the old text lands in a ``body_replaced`` ``ref_events``
    row with ``reason=``, and the recorded chunk citations in
    ``meta.chain`` are re-validated in the same transaction — a dangling
    one refuses the edit whole. A ``TAPROOT:claim`` hub is refused: its
    body *is* the claim sentence, which is identity (``refs.title`` +
    the content-derived ``pub_id``) — the ``title=`` door below is the
    only in-sync rewrite.

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

    ``pick_candidate='self'`` is the **dead_chain close-out**, not a
    candidate pick: a ``STATUS:dead_chain`` finding whose frontier paper
    is fetched and held (live body chunks) is resolved against that held
    paper — the finding links ``derived-from`` it, any lingering
    candidate links drop, ``meta.close_out='self'`` stamps the manual
    resolution, and the chase's own chain-snapshot pass
    (:func:`precis.workers.chase._snapshot_chain`) records
    ``primary_cite_key``/``via_cite_keys``/``caveats``, re-emits
    ``card_combined``, and flips ``STATUS:dead_chain`` ->
    ``STATUS:established``. ``reason=`` (only accepted on this call) is
    recorded as ``meta.close_out_reason`` — the audit trail for a chain
    resolved by hand. Refuses when the finding isn't dead_chain, its
    ``meta.chain`` has no frontier, the frontier ref is gone, or the
    paper has no live body chunks (a stub the fetch never filled is
    still dead):

        edit(kind='finding', id=42, pick_candidate='self',
             reason='held paper chunk states the figure; dead_chain stale')

    ``title=`` is a **different** operation, only valid on a
    ``TAPROOT:claim`` hub (``id`` must resolve to one — see
    :func:`~precis.taproot.authoring.resolve_hub_ref_id`): it reroutes
    through :func:`precis.taproot.hub.refine_claim_sentence`, the single
    write door that keeps ``refs.title``, the ``finding_body`` chunk, and
    the content-derived ``pub_id`` in sync when a hub's claim sentence is
    reworded (fixing a claim-quality issue, e.g. a dangling
    demonstrative). A plain (non-hub) finding has no ``edit(title=…)``
    door — mutate its claim via a fresh ``put()``.

    **Rescope a hub.** ``meta={'scope': {...}}`` is the write door for a
    claim hub's ``refs.meta.scope`` (only the ``scope`` key is accepted in
    ``meta=`` here — any other key is a ``BadInput`` naming the accepted
    set). Same single write door as ``title=`` — a scope edit **is** an
    identity edit: ``scope`` is part of the content-derived ``pub_id``
    (:func:`~precis.identity.make_taproot_hub_paper_id`) and of the
    ``(sentence, scope)`` dedup key, so changing it moves the claim's
    identity exactly as a retitle does, and it goes through
    :func:`~precis.taproot.hub.refine_claim_sentence` (``scope=`` replaces
    ``meta.scope``, it does not merge) rather than a bare meta patch: the
    ``pub_id`` is re-derived and the old one kept as an alias, so
    existing ``[handle]`` cites still resolve, and a new ``pub_id`` that
    collides with a *different* live hub raises (a merge candidate — never
    merged silently). A scope left stale after a retitle is a hub the
    dedup key no longer matches, so a later mint of the same claim forks a
    duplicate hub instead of converging.

        edit(kind='finding', id='fi<N>', meta={'scope': {'material': 'C60'}})
        edit(kind='finding', id='fi<N>', meta={'scope': {}})   # clear scope
        edit(kind='finding', id='fi<N>', title='<reworded>',
             meta={'scope': {...}})                            # both at once

    ``scope`` must be a dict of str -> str; ``{}`` clears it. Scope lint
    (:func:`~precis.taproot.sentence_lint.lint_scope`) is **advisory**
    here exactly as at mint — free-text values and keys outside
    ``SCOPE_KEYS`` are reported in the response, never refused (a refusal
    would make a hub the mint path accepts impossible to correct).
    ``dry_run=True`` previews old -> new scope and old -> new ``pub_id`` and
    writes nothing. Like ``title=``, unsigned candidate/reviewed hubs are
    editable in place; signed hubs require a successor. Changed edits return
    the complete list of graph users flagged for re-review.
    Mutually exclusive with ``pick_candidate=`` / ``unacquirable_note=`` /
    ``testable_by=`` / ``motivation=``; combinable with ``title=``.

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

    **Sharpen a hypothesis.** A ``hypothesis`` finding
    (``meta.artifact_type == 'hypothesis'``, see
    :mod:`precis.handlers._finding_hypothesis`) freezes its
    ``testable_by``/``motivation`` prose at mint — but a hypothesis is
    meant to sit in the corpus accumulating for/against evidence, and the
    discriminating experiment that best separates it from vibes moves
    with the literature. This door lets it:

        edit(kind='finding', id='fi<N>', testable_by='<sharpened experiment>')
        edit(kind='finding', id='fi<N>', motivation='<sharpened leap>')
        edit(kind='finding', id='fi<N>', testable_by='...', motivation='...')

    Only valid on a hypothesis — a ``BadInput`` on any other finding,
    mirroring how ``title=`` rejects a non-hub. Re-runs the same
    non-empty mandatory-field check :func:`_finding_hypothesis.
    put_hypothesis` runs at mint, patches ``meta.proposed_payload``, and
    appends the prior value to ``meta.testable_by_history`` /
    ``meta.motivation_history`` (``{value, replaced_at}``) so a sharpened
    discriminator is visibly distinct from the original conjecture.
    Candidate/reviewed hypotheses edit in place and flag their consumers.
    An unsigned review is reopened atomically because its grounding froze
    the old prose; signed/anchored/published content refuses correction here.

    Only the body edit and the ``meta={'scope': …}`` door support
    ``dry_run`` (see above); every other op rejects it.
    """
    meta_scope = _scope_from_meta(meta) if meta is not None else None
    if mode not in (None, "find-replace", "replace"):
        # Dispatch's mode gate catches this for MCP callers (the wrapper
        # default 'find-replace' is exempt — it arrives on every edit()
        # whether or not the caller meant it); a direct in-process call
        # still gets the loud version rather than a silent drop.
        raise BadInput(
            f"edit(kind='finding') only supports mode='find-replace' | "
            f"'replace' (the body edit), got {mode!r} — every other op "
            "takes no mode=",
            options=["find-replace", "replace"],
            next=(
                "edit(kind='finding', id='fi<N>', mode='find-replace', "
                "find='<exact span>', text='<replacement>')"
            ),
        )
    anchors_given = any(
        v is not None and v != "" for v in (find, before, after, where, nth)
    ) or (match is not None and match != "unique")
    # ``mode`` alone can't signal body-edit intent: 'find-replace' is the
    # wrapper default that lands on every call. A body edit is mode=
    # 'replace', or the default/omitted mode plus a body argument
    # (find=/text= or an anchor).
    body_edit = mode == "replace" or (
        mode in (None, "find-replace") and (anchors_given or text is not None)
    )
    given = [
        name
        for name, value in (
            ("pick_candidate", pick_candidate),
            ("title", title),
            ("unacquirable_note", unacquirable_note),
        )
        if value is not None
    ]
    sharpening = testable_by is not None or motivation is not None
    if sharpening:
        given.append("testable_by/motivation")
    rescoping = meta_scope is not None
    if rescoping:
        given.append("meta['scope']")
    if body_edit:
        given.append("find=/text=")
    # ``title`` + ``meta['scope']`` is the one allowed pairing: both reword
    # the same hub through a single ``refine_claim_sentence`` call.
    exclusive = [g for g in given if not (rescoping and g == "title")]
    if len(exclusive) > 1:
        raise BadInput(
            "edit(kind='finding') accepts exactly one of pick_candidate, "
            "title (optionally with meta={'scope': …}), unacquirable_note, "
            "testable_by=/motivation=, or a body edit (find=/text= with "
            f"mode='find-replace'|'replace') — got {', '.join(given)}",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') / "
                "edit(kind='finding', id='fi<N>', title='<reworded claim>') / "
                "edit(kind='finding', id=<N>, unacquirable_note='<why>') / "
                "edit(kind='finding', id='fi<N>', testable_by='<sharpened "
                "experiment>') / "
                "edit(kind='finding', id='fi<N>', meta={'scope': {...}}) / "
                "edit(kind='finding', id='fi<N>', mode='find-replace', "
                "find='<span>', text='<replacement>')"
            ),
        )
    is_self_pick = (
        isinstance(pick_candidate, str) and pick_candidate.strip().lower() == "self"
    )
    if reason is not None and not is_self_pick and not body_edit:
        raise BadInput(
            "edit(kind='finding') reason= applies to pick_candidate='self' "
            "or to a body edit (it lands on the body_replaced ref_event) — "
            "the other ops record no reason",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='self', "
                "reason='<why the held paper settles it>') or "
                "edit(kind='finding', id='fi<N>', mode='find-replace', "
                "find=…, text=…, reason='<why>')"
            ),
        )
    if is_self_pick and reason is not None and not reason.strip():
        raise BadInput(
            "edit(kind='finding') reason= must be non-empty — it is the "
            "audit trail for a manual close-out",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='self', "
                "reason='<why the held paper settles it>')"
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
    if body_edit:
        return _edit_body(
            store,
            kind=kind,
            raw_id=id,
            mode="replace" if mode == "replace" else "find-replace",
            find=find,
            text=text,
            before=before or "",
            after=after or "",
            where=where,
            match=match or "unique",
            nth=nth,
            dry_run=dry_run,
            reason=reason,
        )
    if rescoping:
        assert meta_scope is not None
        return _rescope_hub(
            store, id=id, title=title, scope=meta_scope, dry_run=bool(dry_run)
        )
    if title is not None:
        if dry_run:
            raise BadInput(
                "edit(kind='finding', title=…) does not support dry_run — "
                "the retitle has no preview; omit dry_run to apply",
                next="edit(kind='finding', id='fi<N>', title='<reworded claim>')",
            )
        return _retitle_hub(store, id=id, title=title)
    if sharpening:
        if dry_run:
            raise BadInput(
                "edit(kind='finding', testable_by=…/motivation=…) does not "
                "support dry_run — the sharpen has no preview; omit dry_run "
                "to apply",
                next="edit(kind='finding', id='fi<N>', testable_by='<experiment>')",
            )
        if id is None:
            raise BadInput(
                "edit(kind='finding', testable_by=…/motivation=…) requires "
                "id=<hypothesis ref_id or fi<N> handle>",
                next="edit(kind='finding', id='fi<N>', testable_by='<experiment>')",
            )
        return _sharpen_hypothesis(
            store,
            kind=kind,
            raw_id=id,
            testable_by=testable_by,
            motivation=motivation,
        )
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

    if is_self_pick:
        return _close_out_self(
            store, kind=kind, finding_ref_id=finding_ref_id, reason=reason
        )

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


#: The only ``meta=`` key the finding edit handler accepts — the hub
#: ``scope`` write door. Anything else is a BadInput naming this set;
#: ``claim_type`` gets its own refusal pointing at the human doors
#: (this door cannot tell a human from an agent).
_META_KEYS = frozenset({"scope"})


def _scope_from_meta(meta: dict[str, Any]) -> dict[str, str]:
    """Validate ``meta=`` for ``edit(kind='finding')`` and return the
    ``scope`` dict. Only ``scope`` is accepted; it must be a dict of
    str -> str (``{}`` clears it)."""
    if "claim_type" in meta:
        raise BadInput(
            "edit(kind='finding') does not set claim_type — reclassification "
            "is a human door",
            next=(
                "web: the type form on /claim/<head>; CLI: precis taproot "
                "classify --hub fi<N> --set <type> --apply"
            ),
        )
    extra = sorted(set(meta) - _META_KEYS)
    if extra or "scope" not in meta:
        raise BadInput(
            f"edit(kind='finding') meta= accepts only {sorted(_META_KEYS)!r} "
            f"— got {sorted(meta)!r}",
            next="edit(kind='finding', id='fi<N>', meta={'scope': {'material': '…'}})",
        )
    scope = meta["scope"]
    if not isinstance(scope, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in scope.items()
    ):
        raise BadInput(
            "edit(kind='finding') meta['scope'] must be a dict of str -> str "
            f"(got {scope!r}); {{}} clears the scope",
            next="edit(kind='finding', id='fi<N>', meta={'scope': {'material': 'C60'}})",
        )
    return dict(scope)


def _resolve_claim_hub(store: Store, id: int | str) -> int | None:
    """Resolve ``id`` to a live ``TAPROOT:claim`` hub ref_id, else ``None``."""
    try:
        return authoring.resolve_hub_ref_id(store, id)
    except BadInput:
        return None


def _rescope_hub(
    store: Store,
    *,
    id: int | str | None,
    title: str | None,
    scope: dict[str, str],
    dry_run: bool,
) -> Response:
    """``edit(kind='finding', meta={'scope': …}[, title=…])`` — replace a
    claim hub's scope through :func:`~precis.taproot.hub.refine_claim_sentence`
    (see the module's "Rescope a hub" docstring for why a scope edit is an
    identity edit). ``title=None`` keeps the current sentence."""
    if id is None:
        raise BadInput(
            "edit(kind='finding', meta={'scope': …}) requires id=<hub ref_id, "
            "fi<id> handle, or pub_id>",
            next="edit(kind='finding', id='fi<N>', meta={'scope': {...}})",
        )
    hub_ref_id = _resolve_claim_hub(store, id)
    if hub_ref_id is None:
        raise BadInput(
            f"edit(kind='finding', meta={{'scope': …}}) only rescopes a "
            f"TAPROOT:claim hub — id={id!r} does not resolve to one",
            next=(
                "a plain (non-hub) finding has no scope-edit door — "
                "record a fresh put(kind='finding', title=…, scope=…) instead"
            ),
        )
    ref = store.get_ref(kind="finding", id=hub_ref_id)
    assert ref is not None
    old_title = str(ref.title or "")
    old_scope = dict((ref.meta or {}).get("scope") or {})
    new_title = title.strip() if title is not None else old_title
    # Advisory exactly as at mint (authoring.seed_claim_hub's ``scope_lint``):
    # free text / unknown keys warn, never refuse. A deliberate clear is not
    # linted ("scope-empty" would nag the caller who just asked for it).
    lints = lint_scope(scope) if scope else []
    old_pub_id = make_pub_id(make_taproot_hub_paper_id(old_title, old_scope))
    if dry_run:
        if not new_title:
            raise BadInput(
                "edit(kind='finding', title=…) requires a non-empty title",
                next="edit(kind='finding', id='fi<N>', title='<non-empty claim "
                "sentence>', meta={'scope': {...}}, dry_run=True)",
            )
        new_pub_id = make_pub_id(make_taproot_hub_paper_id(new_title, scope))
        with store.pool.connection() as conn:
            owner = conn.execute(
                "SELECT ref_id FROM ref_identifiers "
                "WHERE id_kind = 'pub_id' AND id_value = %s",
                (new_pub_id,),
            ).fetchone()
        if owner is not None and int(owner[0]) != hub_ref_id:
            raise BadInput(
                f"dry-run: new pub_id={new_pub_id} already belongs to "
                f"ref_id={int(owner[0])} — this looks like a dedup/merge "
                "candidate; the real edit would refuse",
                next="pick a different scope/title, or resolve the dup by hand",
            )
        return Response(
            body=_rescope_body(
                f"dry-run: would rescope claim hub fi{hub_ref_id} (nothing written)",
                old_title,
                new_title,
                old_scope,
                scope,
                f"pub_id: {old_pub_id} -> {new_pub_id}",
                lints,
            )
        )
    try:
        result = hub.refine_claim_sentence(
            store, hub_ref_id, new_title, scope=scope, set_by="agent"
        )
    except ValueError as exc:
        raise BadInput(
            f"edit(kind='finding', id='fi{hub_ref_id}', meta={{'scope': …}}) "
            f"failed: {exc}",
            next=_FROZEN_NEXT
            if isinstance(exc, hub.HubFrozenError)
            else (
                "the rescoped claim's pub_id collides with a different hub — "
                "resolve the dedup by hand (link_claims / delete one hub) "
                "before rescoping"
            ),
        ) from exc
    alias_note = (
        " (old pub_id kept as an alias — existing [handle] cites still resolve)"
        if result["pub_id_alias_kept"]
        else ""
    )
    return Response(
        body=_rescope_body(
            f"rescoped claim hub fi{hub_ref_id}",
            old_title,
            result["new_title"],
            old_scope,
            scope,
            f"pub_id: {old_pub_id} -> {result['pub_id']}{alias_note}",
            lints,
        )
        + render_users(result["needs_review"])
    )


def _rescope_body(
    head: str,
    old_title: str,
    new_title: str,
    old_scope: dict[str, Any],
    new_scope: dict[str, Any],
    pub_line: str,
    lints: list[str],
) -> str:
    lines = [head, f"scope: {old_scope!r} -> {new_scope!r}"]
    if new_title != old_title:
        lines += [f"old: {old_title}", f"new: {new_title}"]
    lines.append(pub_line)
    if lints:
        lines.append("lint (advisory):")
        lines += [f"  - {w}" for w in lints]
    return "\n".join(lines)


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
    hub_ref_id = _resolve_claim_hub(store, id)
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
            next=_FROZEN_NEXT
            if isinstance(exc, hub.HubFrozenError)
            else (
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
            + render_users(result["needs_review"])
        )
    )


def _sharpen_hypothesis(
    store: Store,
    *,
    kind: str,
    raw_id: int | str,
    testable_by: str | None,
    motivation: str | None,
) -> Response:
    """``edit(kind='finding', testable_by=…/motivation=…)`` — sharpen a
    live hypothesis's falsification terms (gr263258).

    ``id`` must resolve to a hypothesis hub
    (``meta.artifact_type == 'hypothesis'``) — a ``BadInput`` on any
    other finding, mirroring the ``title=`` non-hub rejection. The shared
    unsigned guard and review propagation run inside the write transaction.
    """
    finding_ref_id = _resolve_finding_ref_id(store, kind=kind, raw_id=raw_id)
    ref = store.fetch_refs_by_ids([finding_ref_id]).get(finding_ref_id)
    if (
        ref is None
        or (ref.meta or {}).get(_finding_hypothesis.META_ARTIFACT_TYPE)
        != _finding_hypothesis.ARTIFACT_HYPOTHESIS
    ):
        raise BadInput(
            f"edit(kind='finding', testable_by=…/motivation=…) only "
            f"sharpens a hypothesis — id={raw_id!r} does not resolve to one",
            next=(
                "an ordinary finding's claim has evidence behind it and no "
                "testable_by/motivation fields to sharpen — mutate it via a "
                "fresh put() instead"
            ),
        )
    try:
        return _finding_hypothesis.update_hypothesis(
            store,
            hub_ref_id=finding_ref_id,
            testable_by=testable_by,
            motivation=motivation,
        )
    except hub.HubFrozenError as exc:
        raise BadInput(str(exc), next=_FROZEN_NEXT) from exc


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

    Accepts a numeric ref_id, a numeric-string ref_id, a ``fi<N>``
    handle (the form output hands back), or a ``pub_id`` (the
    agent-facing placeholder shape).
    """
    if isinstance(raw_id, int):
        ref = store.get_ref(kind=kind, id=raw_id)
        if ref is None:
            raise BadInput(
                f"no finding with ref_id={raw_id}",
                next="search(kind='finding', q='<claim keywords>') finds the "
                "finding's ref_id",
            )
        return raw_id
    s = str(raw_id).strip()
    if s.isdigit():
        return _resolve_finding_ref_id(store, kind=kind, raw_id=int(s))
    if s[:2] == "fi" and s[2:].isdigit():
        return _resolve_finding_ref_id(store, kind=kind, raw_id=int(s[2:]))
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
        raise BadInput(
            f"no finding with pub_id={s!r}",
            next="search(kind='finding', q='<claim keywords>') finds the "
            "finding — address it by its ref_id",
        )
    return int(row[0])


def _close_out_self(
    store: Store, *, kind: str, finding_ref_id: int, reason: str | None
) -> Response:
    """``pick_candidate='self'`` — the dead_chain close-out for a finding
    that already HOLDS its terminal paper.

    The chase gives up (``STATUS:dead_chain``) when it can't pin a
    grounding passage, even when the frontier paper itself is fetched and
    held. The human close-out says "the held paper IS the evidence — stop
    chasing": link the finding ``derived-from`` its frontier paper
    (terminal, non-candidate), stamp ``meta.close_out='self'`` (plus
    ``meta.close_out_reason`` when the caller passes ``reason=`` — the
    audit trail for a chain resolved by hand), then run the chase's own
    chain-snapshot pass (:func:`precis.workers.chase._snapshot_chain`) so
    ``primary_cite_key``/``via_cite_keys``/``caveats``, the
    ``card_combined`` re-emit, and the ``STATUS:dead_chain`` ->
    ``STATUS:established`` flip read exactly like a chase-resolved chain.

    Refuses (never a partial write) when the finding is not
    ``STATUS:dead_chain``, its ``meta.chain`` has no frontier, the
    frontier ref is gone, or it has no live body chunks (not actually
    held — a stub the fetch never filled is still a dead end).
    """
    from precis.workers.chase import _snapshot_chain

    ref = store.get_ref(kind=kind, id=finding_ref_id)
    assert ref is not None

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = %s AND t.value = %s",
            (finding_ref_id, _STATUS_NAMESPACE, "dead_chain"),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"finding id={finding_ref_id} is not STATUS:dead_chain — "
            "pick_candidate='self' only closes out a dead chain that is "
            "holding its terminal paper",
            next="get(kind='finding', id=<N>) for its live status",
        )

    meta = dict(ref.meta or {})
    chain = list(meta.get("chain") or [])
    frontier = chain[-1] if chain else None
    frontier_ref_id = int(frontier["ref_id"]) if frontier else None
    if frontier_ref_id is None:
        raise BadInput(
            f"finding id={finding_ref_id} has an empty meta.chain — no "
            "frontier paper to close out against",
            next="get(kind='finding', id=<N>) to inspect the chain",
        )

    with store.pool.connection() as conn:
        held = conn.execute(
            "SELECT 1 FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "AND retired_at IS NULL LIMIT 1",
            (frontier_ref_id,),
        ).fetchone()
    held_ref = fetch_ref_any_kind(store, frontier_ref_id)
    if held_ref is None:
        raise BadInput(
            f"the frontier ref (id={frontier_ref_id}) of finding "
            f"id={finding_ref_id} is gone — nothing held to close out on",
            next=(
                f"get(kind='finding', id='fi{finding_ref_id}') to inspect "
                "meta.chain, then re-chase or retire the finding"
            ),
        )
    if held is None:
        raise BadInput(
            f"the frontier paper of finding id={finding_ref_id} has no "
            "body chunks — it is not held yet, so there is no evidence "
            "to close out on",
            next="fetch the paper first, then pick_candidate='self'",
        )

    with store.tx() as conn:
        # Terminal derived-from edge finding -> held paper. Same manual
        # row write as the pick path: promote-or-create means drop any
        # lingering candidate flags on this pair, insert if absent.
        conn.execute(
            "UPDATE links SET meta = meta - 'candidate' "
            "WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'derived-from'",
            (finding_ref_id, frontier_ref_id),
        )
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by, meta) "
            "SELECT %s, %s, 'derived-from', 'user', '{}'::jsonb "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "  AND relation = 'derived-from')",
            (finding_ref_id, frontier_ref_id, finding_ref_id, frontier_ref_id),
        )
        # Any remaining candidate links are now wrong — drop them.
        conn.execute(
            "DELETE FROM links WHERE src_ref_id = %s "
            "AND relation = 'derived-from' AND meta->>'candidate' = 'true'",
            (finding_ref_id,),
        )
        close_meta: dict[str, Any] = {"close_out": "self"}
        if reason is not None:
            close_meta["close_out_reason"] = mask_secrets(reason.strip())
        store.update_ref(finding_ref_id, meta_patch=close_meta, conn=conn)
        # The chain snapshot is the chase's own termination pass —
        # primary/via cite keys, aggregated caveats, the card_combined
        # re-emit and the STATUS:established flip all land here, in the
        # same transaction as the edge writes.
        _snapshot_chain(conn, store, finding_ref_id, chain)

    settled = store.get_ref(kind=kind, id=finding_ref_id)
    primary = (settled.meta or {}).get("primary_cite_key") if settled else None
    handle = held_ref.slug or f"ref:{frontier_ref_id}"
    return Response(
        body=(
            f"closed out finding id={finding_ref_id} against its held "
            f"paper {handle}\nstatus flipped STATUS:dead_chain -> "
            f"STATUS:established; primary_cite_key={primary}"
        )
    )


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


# ── the body edit ──────────────────────────────────────────────────────────
#
# A plain finding's claim prose lives in the ``finding_body`` chunk (ord=0).
# The gripe (gr509620): kin-evidence minted findings whose body carried a
# clause it then needed to strike — no edit door existed, so the only fix
# was delete+re-put, which loses the id, the links, and the chase state.
# The door reuses the file kinds' anchored vocabulary (``find``/``text``/
# ``before``/``after``/``match``/``nth``/``dry_run``) resolved by
# :mod:`precis.utils.edit_resolve`, and the write goes through
# :meth:`Store.chunks.replace_body_chunk` — DELETE+INSERT, never UPDATE —
# which lands the old text in a ``body_replaced`` ``ref_events`` row.


def _edit_body(
    store: Store,
    *,
    kind: str,
    raw_id: int | str | None,
    mode: str,
    find: str | None,
    text: str | None,
    before: str,
    after: str,
    where: str | None,
    match: str,
    nth: int | None,
    dry_run: bool | str | None,
    reason: str | None,
) -> Response:
    """``mode='find-replace' | 'replace'`` on a plain finding's body prose.

    Refused on a ``TAPROOT:claim`` hub — there the body *is* the claim
    sentence, and ``refs.title`` + the content-derived ``pub_id`` must move
    with it, so the only in-sync rewrite is the ``title=`` door
    (:func:`_retitle_hub`). For a plain finding the ``pub_id`` stays put:
    it is a stored identifier, not a live hash — the dedup key only bites
    at ``put`` (a re-put of the *original* body still converges onto this
    finding's row).
    """
    if raw_id is None:
        raise BadInput(
            "edit(kind='finding', mode=…) requires id=<finding ref_id, "
            "fi<N> handle, or pub_id>",
            next=(
                "edit(kind='finding', id='fi<N>', mode='find-replace', "
                "find='<exact span>', text='<replacement>')"
            ),
        )
    finding_ref_id = _resolve_finding_ref_id(store, kind=kind, raw_id=raw_id)
    if is_claim_hub(store, finding_ref_id):
        raise BadInput(
            f"fi{finding_ref_id} is a TAPROOT:claim hub — its body is the "
            "claim sentence, which is identity: refs.title, the "
            "finding_body chunk and the content-derived pub_id stay in "
            "lockstep only through the title= door",
            next=(
                f"edit(kind='finding', id='fi{finding_ref_id}', "
                "title='<reworded claim sentence>')"
            ),
        )
    with store.pool.connection() as conn:
        body_rows = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'finding_body' AND retired_at IS NULL "
            "ORDER BY ord",
            (finding_ref_id,),
        ).fetchall()
    if not body_rows:
        raise BadInput(
            f"finding id={finding_ref_id} has no live finding_body chunk — "
            "nothing to edit",
            next="get(kind='finding', id='fi<N>') to see the finding's state",
        )
    old_chunk_ids = [int(r[0]) for r in body_rows]
    old_body = str(body_rows[0][1] or "")
    label = f"finding fi{finding_ref_id}"
    text = mask_secrets(text)
    reason = mask_secrets(reason)
    reason_clean = reason.strip() if reason and reason.strip() else None
    dry_mode = normalize_dry_run(dry_run)
    edited_spans: tuple[tuple[int, int], ...]
    match_policy = "replace"
    if mode == "replace":
        stray = [
            name
            for name, value in (
                ("find", find),
                ("before", before or None),
                ("after", after or None),
                ("where", where),
                ("nth", nth),
                ("match", match if match != "unique" else None),
            )
            if value is not None
        ]
        if stray:
            raise BadInput(
                f"mode='replace' takes text= only ({'/'.join(stray)}= "
                "given) — it rewrites the whole body",
                next="edit(kind='finding', id='fi<N>', mode='replace', "
                "text='<new body>')",
            )
        if not text or not text.strip():
            raise BadInput(
                "mode='replace' requires text= (the whole new body)",
                next="edit(kind='finding', id='fi<N>', mode='replace', "
                "text='<new body>')",
            )
        new_body = text.strip()
        edited_spans = ((1, new_body.count("\n") + 1),)
    else:
        if not find and text is not None:
            # text= alone on the default mode: refuse rather than guess a
            # whole-body overwrite (memory's rule).
            raise BadInput(
                "mode='find-replace' requires find= (the exact text to "
                "locate) — text= alone can't pick a span",
                next=(
                    "edit(kind='finding', id='fi<N>', mode='find-replace', "
                    "find='<exact text>', text='<replacement>') — or, to "
                    "rewrite the whole body, mode='replace' with text="
                ),
            )
        _require_find_and_text(
            op_kind="edit",
            kind=kind,
            slug=str(finding_ref_id),
            find=find,
            text=text,
            before=before,
            after=after,
            where=where,
            match=match,
            nth=nth,
        )
        assert text is not None and find is not None
        op = EditOp(
            op="edit",
            find=find,
            text=text,
            before=before,
            after=after,
            where=cast("Any", where),
            match=cast("Any", match),
            nth=nth,
            region_label=label,
        )
        result = apply_edit(old_body, op)
        new_body = result.new_buffer
        edited_spans = result.edited_spans
        match_policy = op.match
    if not new_body.strip():
        raise BadInput(
            f"edit would leave {label} with an empty body",
            next=(
                f"delete(kind='finding', id={finding_ref_id}) to retire the "
                "finding, or edit with replacement text"
            ),
        )
    if dry_mode is not None:
        header = render_dry_run_header(
            region_label=label,
            edited_spans=edited_spans,
            match_policy=match_policy,
        )
        if dry_mode == "full":
            preview = render_dry_run_full(
                new_body, edited_spans=edited_spans, region_label=label
            )
        else:
            diff = format_unified_diff(old_body, new_body, file_label=label)
            preview = diff.rstrip("\n") or "(no diff - pre and post are identical)"
        return Response(body="\n".join([*header, "", preview]))

    with store.tx() as conn:
        locked = conn.execute(
            "SELECT retired_at, meta FROM refs WHERE ref_id = %s AND kind = %s "
            "FOR NO KEY UPDATE",
            (finding_ref_id, kind),
        ).fetchone()
        if locked is None:
            raise NotFound(
                f"finding id={finding_ref_id} not found",
                next="search(kind='finding', q='<claim keywords>') for a live id",
            )
        if locked[0] is not None:
            raise Gone(
                f"finding id={finding_ref_id} has been deleted",
                next="search(kind='finding', q='<claim keywords>') for a live one",
            )
        # The rewrite re-mints the body chunk (delete+insert); a link
        # pinned at the outgoing chunk_id would be silently severed by
        # the ON DELETE CASCADE on links.{src,dst}_chunk_id — refuse
        # rather than drop an edge nobody asked to lose.
        pinned_row = conn.execute(
            "SELECT COUNT(*) FROM links WHERE src_chunk_id = ANY(%s) "
            "OR dst_chunk_id = ANY(%s)",
            (old_chunk_ids, old_chunk_ids),
        ).fetchone()
        if pinned_row is not None and pinned_row[0]:
            raise BadInput(
                f"{label}'s body chunk is the endpoint of {pinned_row[0]} link(s) "
                "— rewriting it (delete+insert) would sever them",
                next=(
                    "remove the chunk-pinned links on fi<N>~<ord> first, or "
                    "supersede the finding"
                ),
            )
        replaced = store.chunks.replace_body_chunk(
            finding_ref_id,
            new_body,
            chunk_kind="finding_body",
            source="agent",
            reason=reason_clean,
            conn=conn,
        )
        if (replaced or "") != old_body:
            raise BadInput(
                f"{label} changed during the edit; nothing written",
                next=(
                    f"get(kind='finding', id='fi{finding_ref_id}'), then "
                    "retry the edit against the current body"
                ),
            )
        _assert_chunk_citations(conn, finding_ref_id=finding_ref_id, meta=locked[1])
        nudge_fresh_embed(store, conn=conn, reason="edit finding body")

    verb = "replaced" if mode == "replace" else "edited"
    spans = (
        ""
        if mode == "replace"
        else f", {len(edited_spans)} span{'s' if len(edited_spans) != 1 else ''}"
    )
    return Response(
        body=(
            f"{verb} body of {label} ({len(old_body.split())} → "
            f"{len(new_body.split())} words{spans}).\n"
            "chunk citation re-validated: the chain still resolves. "
            "old text kept as a body_replaced ref_event."
        )
    )


def _assert_chunk_citations(
    conn: Any, *, finding_ref_id: int, meta: dict[str, Any] | None
) -> None:
    """Re-validate the finding's recorded chunk citations after a body edit.

    ``meta.chain`` hops carry ``{ref_id, chunk_id, ord}`` — ``chunk_id``
    is a raw JSONB pointer with no FK, so a deleted or re-ingested source
    chunk leaves it dangling (the ``links`` row that rode with it already
    went away on the ``ON DELETE CASCADE``). A hop that pins a chunk —
    by ``chunk_id``, or by ``(ref_id, ord)`` when the chase never
    resolved an id — must still resolve to a live ``chunks`` row, or the
    claim's grounding silently evaporated: refuse the edit whole (the
    ``tx`` rolls back). A hop with neither (an unresolved frontier) pins
    nothing and is skipped. An empty chain — an acquisition-mode finding
    — cites through its provenance ``derived-from`` edge instead: that
    edge is gone only if its chunk died, so require a live one.
    """
    chain = list((meta or {}).get("chain") or [])
    dangling: list[str] = []
    for i, hop in enumerate(chain):
        if not isinstance(hop, dict):
            continue
        cid = hop.get("chunk_id")
        ord_ = hop.get("ord")
        hrid = hop.get("ref_id")
        if cid is not None:
            alive = (
                conn.execute(
                    "SELECT 1 FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
                    (int(cid),),
                ).fetchone()
                is not None
            )
        elif ord_ is not None and hrid is not None:
            alive = (
                conn.execute(
                    "SELECT 1 FROM chunks WHERE ref_id = %s AND ord = %s "
                    "AND retired_at IS NULL",
                    (int(hrid), int(ord_)),
                ).fetchone()
                is not None
            )
        else:
            continue
        if not alive:
            dangling.append(f"hop {i} (ref_id={hrid}, chunk_id={cid}, ord={ord_})")
    if dangling:
        raise BadInput(
            f"fi{finding_ref_id}'s chunk citation no longer resolves — "
            f"{'; '.join(dangling)}",
            next=(
                "the chunk the claim was read in is gone (deleted or "
                "re-ingested) — re-register it with a fresh "
                "put(kind='finding', …, cited_in='<live handle>') or "
                "delete(kind='finding', id=…) the stale one"
            ),
        )
    if not chain:
        edge = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s "
            "AND relation = 'derived-from' LIMIT 1",
            (finding_ref_id,),
        ).fetchone()
        if edge is None:
            raise BadInput(
                f"fi{finding_ref_id} has no live derived-from edge — its "
                "citation is gone (the cited chunk died and the edge "
                "cascaded with it)",
                next=(
                    "re-register the claim with a fresh put() or "
                    "delete(kind='finding', id=…) the stale one"
                ),
            )
