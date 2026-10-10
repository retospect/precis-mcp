"""``edit(kind='finding', ...)`` — pick_candidate / title / meta={'scope'} /
unacquirable_note.

Split out of ``finding.py`` (docs/backlog/codereview-residuals.md):
this state machine (~350 lines across the mutually-exclusive ops) only
ever touched ``self.store``/``self.kind``, never any other handler state,
so it moves as free functions taking the store (and the finding kind
string) explicitly. ``FindingHandler.edit`` calls :func:`edit` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers import _finding_hypothesis
from precis.handlers._finding_common import fetch_ref_any_kind
from precis.identity import make_pub_id, make_taproot_hub_paper_id
from precis.response import Response
from precis.store.types import Tag
from precis.taproot import authoring, hub
from precis.taproot.consumer_review import render_users
from precis.taproot.sentence_lint import lint_scope

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
    pick_candidate: str | int | None = None,
    title: str | None = None,
    unacquirable_note: str | None = None,
    unacquirable_mode: str | None = None,
    testable_by: str | None = None,
    motivation: str | None = None,
    meta: dict[str, Any] | None = None,
    dry_run: bool | str | None = None,
) -> Response:
    """Resolve a ``STATUS:multi_candidate`` finding by picking one cite,
    retitle a ``TAPROOT:claim`` hub, record an author's
    unacquirable-source override, or sharpen a live hypothesis's
    falsification terms. Mutually exclusive kwargs — pass exactly one
    (``testable_by=``/``motivation=`` are the one exception: they may be
    combined in a single call, since both sharpen the same conjecture).

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

    Only the ``meta={'scope': …}`` door supports ``dry_run`` (see above);
    every other op rejects it.
    """
    meta_scope = _scope_from_meta(meta) if meta is not None else None
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
    # ``title`` + ``meta['scope']`` is the one allowed pairing: both reword
    # the same hub through a single ``refine_claim_sentence`` call.
    exclusive = [g for g in given if not (rescoping and g == "title")]
    if len(exclusive) > 1:
        raise BadInput(
            "edit(kind='finding') accepts exactly one of pick_candidate, "
            "title (optionally with meta={'scope': …}), unacquirable_note, "
            f"or testable_by=/motivation= — got {', '.join(given)}",
            next=(
                "edit(kind='finding', id=<N>, pick_candidate='<cite_key>') / "
                "edit(kind='finding', id='fi<N>', title='<reworded claim>') / "
                "edit(kind='finding', id=<N>, unacquirable_note='<why>') / "
                "edit(kind='finding', id='fi<N>', testable_by='<sharpened "
                "experiment>') / "
                "edit(kind='finding', id='fi<N>', meta={'scope': {...}})"
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
            raise BadInput("edit(kind='finding', title=…) requires a non-empty title")
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
        raise BadInput(str(exc)) from exc


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
