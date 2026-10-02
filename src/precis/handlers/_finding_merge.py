"""``get(kind='finding', id='fi<winner>', view='merge-plan', args={'loser': …})``
— the agent-side, read-only dry run of a claim-hub merge.

``docs/backlog/taproot-merge-mcp-surface.md``: applying a merge hard-DELETEs
redundant ``links`` rows and soft-deletes a hub, so it is a **human door**
(``POST /nanopub/fi{hub}/merge`` on the claim page). Agents get only the
plan: this view always runs :func:`precis.taproot.hub.merge_hubs` with
``dry_run=True`` and takes no apply/confirm parameter — there is nothing
here for an agent to flip.
"""

from __future__ import annotations

from precis.errors import BadInput
from precis.response import Response
from precis.store import Store
from precis.taproot.authoring import resolve_merge_loser_ref_id
from precis.taproot.hub import MERGE_COLLAPSE_RELATION, MergePlan, merge_hubs

_CALL_SHAPE = "get(kind='finding', id='fi<winner>', view='merge-plan', args={'loser': 'fi<loser>'})"


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


def format_merge_plan(plan: MergePlan) -> str:
    """Compact markdown for a :class:`MergePlan` — the same facts the CLI's
    ``precis taproot merge --dry-run`` prints, grouped by what happens to
    each edge."""
    lo, wi = f"fi{plan.loser_ref_id}", f"fi{plan.winner_ref_id}"
    lines = [f"# merge plan (dry run): {lo} -> {wi}", ""]

    if plan.already_merged:
        lines.append(f"already_merged: true - {lo} was already merged into {wi}; no-op")
    else:
        lines.append("already_merged: false")
        lines.append(f"can_merge: {'true' if plan.can_merge else 'false'}")
        if not plan.can_merge:
            lines.append(f"refused: {plan.block_reason}")
            lines.append(f"{lo} would stay live.")
        lines.extend(_edge_sections(plan, lo, wi))

    lines.append("")
    lines.append(
        f"Applying a merge is a human door: open /nanopub/{wi} (the claim "
        "page's merge form). This view never writes."
    )
    return "\n".join(lines)


def _edge_sections(plan: MergePlan, lo: str, wi: str) -> list[str]:
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
    out.append(f"## edges to repoint ({len(repoint)})")
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
    if plan.can_merge:
        out.append("")
        out.append(
            f"On apply: {lo} is retired and recorded as {lo} "
            f"--{MERGE_COLLAPSE_RELATION}--> {wi}; dropped links are "
            "hard-deleted (no undo)."
        )
    return out
