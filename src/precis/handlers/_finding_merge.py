"""The agent surface of a claim-hub merge: the read-only plan and the
``merge_into`` edit door.

* ``get(kind='finding', id='fi<winner>', view='merge-plan', args={'loser':
  …})`` — :func:`render_merge_plan_view`. Always runs
  :func:`precis.taproot.hub.merge_hubs` with ``dry_run=True``; it reports a
  blocked merge (``can_merge: false``) instead of raising, and never writes.
* ``edit(kind='finding', id='fi<loser>', args={'merge_into': 'fi<winner>',
  'reason': '…'}, dry_run=…)`` — :func:`merge_door`, the apply (gr684893).
  ``id=`` is the hub that retires, as on the paper fold.

Why agents may apply (Reto 2026-11-15): a merge hard-DELETEs redundant
``links`` rows and soft-deletes a hub, but while both hubs are still at
``'candidate'`` in ``nanopub_publish`` nothing reviewed, signed or published
is re-identified, and the door runs the same :func:`merge_hubs` the CLI and
the claim page run (publish-state check, redundant-edge drop, draft-cite
rewrite, ``refines`` collapse record). Past ``'candidate'`` the door refuses
in both modes and hands over the operator's CLI line. The earlier
plan-only surface forced every duplicate the dedup sweep found into a
prod-CLI round trip for the owner.
"""

from __future__ import annotations

from precis.errors import BadInput
from precis.response import Response
from precis.store import Store
from precis.taproot.authoring import resolve_hub_ref_id, resolve_merge_loser_ref_id
from precis.taproot.hub import MERGE_COLLAPSE_RELATION, MergePlan, merge_hubs
from precis.utils.edit_resolve import normalize_dry_run
from precis.utils.secret_scan import mask_secrets

_CALL_SHAPE = "get(kind='finding', id='fi<winner>', view='merge-plan', args={'loser': 'fi<loser>'})"
_DOOR_SHAPE = (
    "edit(kind='finding', id='fi<loser>', args={'merge_into': 'fi<winner>', "
    "'reason': '<why they are the same claim>'}, dry_run=True)"
)


def _door_call(lo: str, wi: str) -> str:
    """The paste-ready apply call for this pair."""
    return (
        f"edit(kind='finding', id='{lo}', args={{'merge_into': '{wi}', "
        "'reason': '<why they are the same claim>'})"
    )


def _cli_line(lo: str, wi: str) -> str:
    """The operator hand-off for a pair past ``'candidate'``. ``--dry-run``
    because the CLI enforces the same publish-state guard: the line shows
    the operator the blocked plan, it does not force the merge."""
    return f"precis taproot merge --loser {lo} --winner {wi} --dry-run"


def _hub_ref(raw: str | int) -> str | int:
    """``'123'`` and ``'fi123'`` to the bare ref_id, so a merged-away loser
    still resolves (an ``fi`` handle cannot follow a merge; see
    :func:`resolve_merge_loser_ref_id`)."""
    if isinstance(raw, str):
        token = raw.strip()
        digits = token[2:] if token[:2] == "fi" else token
        if digits.isdigit():
            return int(digits)
        return token
    return raw


def render_merge_plan_view(
    store: Store, winner_ref_id: int, loser: str | int | None
) -> Response:
    """Render the dry-run plan for collapsing ``loser`` into the hub
    ``winner_ref_id``. ``loser`` takes ``'fi<N>'``, a bare int (or digit
    string) or a ``pub_id`` slug — the forms ``id=`` takes. A loser that
    is already merged away resolves by int/pub_id only (an ``fi`` handle
    cannot follow a merge; see :func:`resolve_merge_loser_ref_id`).

    Raises:
        BadInput: no ``loser`` given, an unresolvable loser, or anything
            :func:`merge_hubs` refuses structurally (same hub on both
            sides, a non-hub ref, …) — passed through unchanged.
    """
    if loser is None or (isinstance(loser, str) and not loser.strip()):
        raise BadInput(
            "view='merge-plan' needs args={'loser': ...} - the hub that "
            "would be collapsed into id=",
            next=_CALL_SHAPE,
        )
    if isinstance(loser, str) and loser.strip().lstrip("-").isdigit():
        loser = int(loser.strip())
    loser_ref_id = resolve_merge_loser_ref_id(store, loser)
    plan = merge_hubs(
        store, loser_ref_id=loser_ref_id, winner_ref_id=winner_ref_id, dry_run=True
    )
    return Response(body=format_merge_plan(plan))


def merge_door(
    store: Store,
    *,
    loser: str | int | None,
    winner: str | int,
    reason: str | None,
    dry_run: bool | str | None,
) -> Response:
    """``edit(kind='finding', id=<loser>, args={'merge_into': <winner>,
    'reason': …})`` — collapse the duplicate hub ``loser`` into ``winner``.

    One transaction through :func:`merge_hubs`: every live link on the
    loser is repointed to the winner (or dropped when the winner already
    holds it), live draft prose citing ``[fi<loser>]`` is rewritten to the
    winner, the loser is retired and recorded as ``loser --refines-->
    winner``. ``reason`` lands on a ``hub_merged`` ref_event on the winner
    and a ``merged_into`` one on the loser. ``dry_run`` returns the same
    plan and opens no write. Re-running an applied merge is a reported
    no-op.

    Raises:
        BadInput: no ``id=`` or ``reason``; a winner that is not a live
            claim hub; either side past ``'candidate'`` (both modes, with
            the operator's CLI line as ``next=``); a draft chunk citing the
            loser that cannot be rewritten (``retire_ref``'s cited-finding
            refusal, which lists it); anything :func:`merge_hubs` refuses
            structurally.
    """
    dry = normalize_dry_run(dry_run) is not None
    if loser is None or (isinstance(loser, str) and not loser.strip()):
        raise BadInput(
            "args={'merge_into': ...} needs id=<the duplicate hub that retires>",
            next=_DOOR_SHAPE,
        )
    reason = mask_secrets(reason).strip() if reason else ""
    if not reason:
        raise BadInput(
            "args['merge_into'] needs args['reason'] - why the two hubs are "
            "the same claim; it lands on both hubs' event log",
            next=_DOOR_SHAPE,
        )
    winner_ref_id = resolve_hub_ref_id(store, _hub_ref(winner))
    loser_ref_id = resolve_merge_loser_ref_id(store, _hub_ref(loser))
    lo, wi = f"fi{loser_ref_id}", f"fi{winner_ref_id}"

    def _refuse_blocked(plan: MergePlan) -> None:
        if plan.already_merged or plan.can_merge:
            return
        raise BadInput(
            f"cannot merge {lo} into {wi}: {plan.block_reason} - a hub past "
            "'candidate' is reviewed, signed or published, and merging it "
            "is operator-only",
            next=[
                f"hand the operator: {_cli_line(lo, wi)}",
                "the blocked plan, read-only: get(kind='finding', "
                f"id='{wi}', view='merge-plan', args={{'loser': '{lo}'}})",
            ],
        )

    if dry:
        plan = merge_hubs(
            store,
            loser_ref_id=loser_ref_id,
            winner_ref_id=winner_ref_id,
            dry_run=True,
        )
        _refuse_blocked(plan)
        return Response(body=format_merge_plan(plan, door="dry_run"))

    with store.tx() as conn:
        plan = merge_hubs(
            store,
            loser_ref_id=loser_ref_id,
            winner_ref_id=winner_ref_id,
            dry_run=True,
            conn=conn,
        )
        _refuse_blocked(plan)
        if not plan.already_merged:
            plan = merge_hubs(
                store,
                loser_ref_id=loser_ref_id,
                winner_ref_id=winner_ref_id,
                conn=conn,
            )
            for ref_id, event, peer in (
                (winner_ref_id, "hub_merged", {"loser_ref_id": loser_ref_id}),
                (loser_ref_id, "merged_into", {"winner_ref_id": winner_ref_id}),
            ):
                store.append_event(
                    ref_id,
                    source="agent",
                    event=event,
                    payload={**peer, "reason": reason},
                    conn=conn,
                )
    return Response(body=format_merge_plan(plan, door="applied"))


def format_merge_plan(plan: MergePlan, *, door: str = "view") -> str:
    """Compact markdown for a :class:`MergePlan` — the same facts the CLI's
    ``precis taproot merge --dry-run`` prints, grouped by what happens to
    each edge. ``door`` picks the frame: ``'view'`` (the read-only
    ``merge-plan`` view), ``'dry_run'`` (the edit door's preview) or
    ``'applied'`` (the edit door's ack, past tense)."""
    lo, wi = f"fi{plan.loser_ref_id}", f"fi{plan.winner_ref_id}"
    applied = door == "applied"

    if plan.already_merged:
        note = f"{lo} was already merged into {wi}; no-op"
        if door != "view":
            return f"already merged: {note} - nothing written"
        return "\n".join(
            [
                f"# merge plan (dry run): {lo} -> {wi}",
                "",
                f"already_merged: true - {note}",
                "",
                "Nothing to apply. This view never writes.",
            ]
        )

    if applied:
        repointed = sum(1 for e in plan.edges if e.action == "repoint")
        lines = [
            f"merged {lo} into {wi}: {repointed} edge(s) repointed, "
            f"{len(plan.edges) - repointed} dropped, "
            f"{len(plan.cite_rewrites)} draft cite(s) rewritten; {lo} is "
            f"retired and recorded as {lo} --{MERGE_COLLAPSE_RELATION}--> {wi}"
        ]
    else:
        lines = [f"# merge plan (dry run): {lo} -> {wi}", ""]
        lines.append("already_merged: false")
        lines.append(f"can_merge: {'true' if plan.can_merge else 'false'}")
        if not plan.can_merge:
            lines.append(f"refused: {plan.block_reason}")
            lines.append(f"{lo} would stay live.")
    lines.extend(_edge_sections(plan, lo, wi, applied=applied))

    if applied:
        return "\n".join(lines)
    lines.append("")
    if door == "dry_run":
        lines.append(f"Dry run, nothing written. Apply: {_door_call(lo, wi)}")
    elif plan.can_merge:
        lines.append(
            f"Apply: {_door_call(lo, wi)} - add dry_run=True to preview; "
            f"or the claim page's merge form, /nanopub/{wi}. This view never "
            "writes."
        )
    else:
        lines.append(
            "Past 'candidate' a merge is operator-only; hand the operator: "
            f"{_cli_line(lo, wi)} (claim page /nanopub/{wi}). This view "
            "never writes."
        )
    return "\n".join(lines)


def _edge_sections(
    plan: MergePlan, lo: str, wi: str, *, applied: bool = False
) -> list[str]:
    def _arrow(e, hub: str) -> str:
        peer = f"fi{e.other_ref_id}"
        if e.direction == "outbound":
            return f"{hub} --{e.relation}--> {peer}"
        return f"{peer} --{e.relation}--> {hub}"

    repoint = [e for e in plan.edges if e.action == "repoint"]
    redundant = [e for e in plan.edges if e.action == "drop_redundant"]
    loops = [e for e in plan.edges if e.action == "drop_self_loop"]

    out: list[str] = []
    out.append("")
    out.append(f"## edges {'repointed' if applied else 'to repoint'} ({len(repoint)})")
    out.extend(
        f"- link {e.link_id}: {_arrow(e, lo)}  =>  {_arrow(e, wi)}" for e in repoint
    )
    out.append("")
    out.append(f"## edges dropped as redundant ({len(redundant)})")
    out.extend(
        f"- link {e.link_id}: {_arrow(e, lo)} - {wi} already holds it as "
        f"link {e.duplicate_of_link_id}"
        for e in redundant
    )
    out.append("")
    out.append(f"## self-loops dropped ({len(loops)})")
    out.extend(
        f"- link {e.link_id}: {_arrow(e, lo)} - repointing would give "
        f"{wi} --{e.relation}--> {wi}"
        for e in loops
    )
    out.append("")
    out.append(f"## draft cites rewritten ({len(plan.cite_rewrites)})")
    out.extend(
        f"- dc{rw.chunk_id} (draft ref {rw.draft_ref_id}): [{lo}] => [{wi}]"
        for rw in plan.cite_rewrites
    )
    if plan.can_merge and not applied:
        out.append("")
        out.append(
            f"On apply: {lo} is retired and recorded as {lo} "
            f"--{MERGE_COLLAPSE_RELATION}--> {wi}; dropped links are "
            "hard-deleted (no undo)."
        )
    return out
