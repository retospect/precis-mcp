"""The finding delete door's publish-state guard and the hub retirement
procedure (gr760607, ruled by Reto 2026-11-22).

A claim hub past ``'candidate'`` in ``nanopub_publish`` (reviewed, signed,
anchored, published, or carrying a terminal row) has a frozen identity: a
merge refuses it for every actor (gr754451), and so does the plain
``delete(kind='finding', ...)``. Retiring a candidate-state hub, superseded
by anything including a past-candidate hub, is unchanged.

The retirement procedure is the one way such a hub retires, by explicit
opt-in on the same door::

    delete(kind='finding', id='fi<N>', superseded_by='fi<M>' | omitted,
           reason='<why>', args={'retire_published': True})

In the delete transaction it

* requires a non-empty ``reason`` and, when given, a live finding as the
  successor;
* records the retirement on the hub's publish record: the live row moves to
  ``superseded`` (a successor is named) or ``retracted`` (none is) under
  :func:`precis.nanopub.state.check_retirement`, with the reason and the
  successor in ``retired_reason`` / ``retired_successor_ref_id``; a hub whose
  rows are all terminal already keeps their state and gets the retirement
  recorded on its newest row. The row is never deleted;
* rewrites live draft ``[fi<N>]`` cites to the successor (the merge's cite
  rewrite), while the audited retirement moves the links;
* files one owner todo (``ask-user``) when a row carries a signed, anchored
  or published artifact: a retraction or erratum is owed outside precis,
  and this procedure does not pretend to withdraw it.

Reads of the retired id stay resolvable: the tombstone carries
:func:`tombstone_line`, and a superseded hub's ``fi`` handle redirects to the
successor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.nanopub.state import TERMINAL, check_retirement
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.taproot.hub import (
    MergeCiteRewrite,
    _publish_states_past_candidate,
    apply_cite_rewrites,
    plan_cite_rewrites,
    retire_published_call,
)

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store._nanopub_ops import RetireRow


@dataclass(frozen=True)
class RetirePlan:
    """What the retirement procedure writes for one hub — computed on the
    delete transaction before any write, so a dry run reports it."""

    ref_id: int
    successor_ref_id: int | None
    reason: str
    to_state: str
    #: Every publish row of the hub, oldest first.
    rows: list[RetireRow]
    #: The row the retirement lands on: the live row, else the newest.
    target: RetireRow
    cite_rewrites: list[MergeCiteRewrite]

    @property
    def new_state(self) -> str:
        """The target row's state after the write (a terminal row keeps its own)."""
        return self.to_state if self.target.state not in TERMINAL else self.target.state

    @property
    def external(self) -> list[RetireRow]:
        """Rows whose artifact exists outside the working row."""
        return [r for r in self.rows if r.external]

    def summary(self) -> list[str]:
        """Receipt lines, the same for a dry run and the applied write."""
        t = self.target
        moved = (
            f"publish row {t.id}: {t.state} -> {self.new_state}"
            if t.state != self.new_state
            else f"publish row {t.id}: stays {t.state} (already terminal)"
        )
        lines = [f"{moved} (reason and successor recorded, row kept)"]
        if self.cite_rewrites:
            lines.append(
                f"draft cites: {len(self.cite_rewrites)} [fi{self.ref_id}] -> "
                f"[fi{self.successor_ref_id}]"
            )
        if self.external:
            lines.append(
                "owner todo: a retraction or erratum is owed for "
                f"{len(self.external)} signed/published artifact(s)"
            )
        return lines


def _plain_delete_call(ref_id: int, successor: str | None) -> str:
    sup = f"superseded_by='{successor}', " if successor else ""
    return f"delete(kind='finding', id='fi{ref_id}', {sup}reason='<why>')"


def guard(
    store: Store,
    conn: Any,
    ref_id: int,
    *,
    retire_published: bool,
    successor_ref_id: int | None,
    successor_handle: str | None,
    successor_kind: str | None,
    reason: str | None,
) -> RetirePlan | None:
    """Refuse what the door must not do; return the procedure's plan when
    ``retire_published`` is set, else ``None`` (the plain retirement runs
    unchanged). Reads only; the publish rows are locked on ``conn``."""
    states = _publish_states_past_candidate(conn, ref_id)
    hub = f"fi{ref_id}"
    if not retire_published:
        if not states:
            return None
        # A non-finding successor would be refused by the procedure too, so
        # the suggested call names a finding placeholder instead.
        bad_successor = successor_handle is not None and successor_kind != "finding"
        sup = "fi<M>" if bad_successor else successor_handle
        note = (
            f"; its successor must be a finding, not {successor_handle}"
            if bad_successor
            else ""
        )
        raise BadInput(
            f"cannot retire {hub}: it has nanopub_publish state(s) {states} "
            "(past 'candidate'), so its identity is frozen (reviewed, signed "
            f"or published) and the plain delete door refuses it{note}",
            next=[
                "retire it through the retirement procedure (keeps the "
                "publish record; superseded_by= a finding or omitted, reason= "
                "required): " + retire_published_call(hub, sup, "<why>"),
                "get(kind='skill', id='precis-taproot-hub-edit-help')  # "
                "'Retire a hub past candidate'",
            ],
        )
    if not states:
        raise BadInput(
            f"{hub} is not past 'candidate'; args={{'retire_published': True}} "
            "is the retirement procedure for a frozen hub only",
            next=_plain_delete_call(ref_id, successor_handle),
        )
    why = (reason or "").strip()
    if not why:
        raise BadInput(
            f"retiring {hub} through the retirement procedure needs reason=",
            next=retire_published_call(hub, successor_handle, "<why>"),
        )
    if successor_handle is not None and successor_kind != "finding":
        raise BadInput(
            f"a hub past 'candidate' is superseded by another finding, not "
            f"{successor_handle}",
            next=[
                retire_published_call(hub, "fi<M>", why),
                retire_published_call(hub, None, why) + "  # retract instead",
            ],
        )
    rows = store.nanopub_retire_rows(ref_id, conn=conn)
    live = [r for r in rows if r.state not in TERMINAL]
    to_state = "superseded" if successor_ref_id is not None else "retracted"
    if live:
        target = live[-1]
        try:
            check_retirement(target.state, to_state)
        except ValueError as exc:
            raise BadInput(
                f"cannot retire {hub}: {exc}",
                next=(
                    "a 'publishing' row is a registry POST in flight: wait, or "
                    "run `precis nanopub reconcile`, then retry"
                ),
            ) from exc
    else:
        target = max(rows, key=lambda r: (r.updated_at, r.id))
    rewrites = (
        plan_cite_rewrites(conn, old_ref_id=ref_id, new_ref_id=successor_ref_id)
        if successor_ref_id is not None
        else []
    )
    return RetirePlan(
        ref_id=ref_id,
        successor_ref_id=successor_ref_id,
        reason=why,
        to_state=to_state,
        rows=rows,
        target=target,
        cite_rewrites=rewrites,
    )


def apply(store: Store, conn: Any, plan: RetirePlan) -> list[str]:
    """Write the plan on the delete transaction, before the hub retires:
    draft cites, the publish record, the owner todo. Returns receipt lines."""
    if plan.cite_rewrites:
        apply_cite_rewrites(
            store,
            conn,
            plan.cite_rewrites,
            source={
                "tool": "finding-retire",
                "superseded": f"fi{plan.ref_id}->fi{plan.successor_ref_id}",
            },
        )
    t = plan.target
    if not store.nanopub_record_retirement(
        t.id,
        to_state=plan.new_state,
        expect=t.state,
        reason=plan.reason,
        successor_ref_id=plan.successor_ref_id,
        conn=conn,
    ):
        raise BadInput(
            f"publish row {t.id} of fi{plan.ref_id} moved during the "
            "retirement; nothing was written",
            next="retry the same delete call",
        )
    lines = plan.summary()
    if plan.external:
        todo = _file_owner_todo(store, conn, plan)
        lines[-1] = f"{lines[-1]}: td{todo}"
    return lines


def _artifact_line(r: RetireRow) -> str:
    parts = [f"publish row {r.id} (was {r.state})"]
    for label, value in (
        ("trusty URI", r.trusty_uri),
        ("AIDA URI", r.aida_uri),
        ("registry URL", r.registry_url),
        ("published at", r.published_at.isoformat() if r.published_at else None),
        ("OTS batch", r.batch_id),
        ("artifact row", r.artifact_id),
    ):
        if value:
            parts.append(f"{label} {value}")
    return "- " + ", ".join(parts)


def _file_owner_todo(store: Store, conn: Any, plan: RetirePlan) -> int:
    """One ``ask-user`` todo naming every signed/published artifact the
    retired hub leaves outside precis; filed on the delete transaction."""
    from precis.todo.guards import check_ask_meta

    hub = f"fi{plan.ref_id}"
    done = (
        f"superseded by fi{plan.successor_ref_id}"
        if plan.successor_ref_id is not None
        else "retracted"
    )
    title = (
        f"Publish a retraction or erratum for {hub}'s nanopub: the hub was "
        f"{done} in precis"
    )
    body = "\n".join(
        [
            f"{hub} was retired through the retirement procedure ({done}): "
            f"{plan.reason}",
            "",
            "Its publish record is kept in precis, but these artifacts were "
            "signed, anchored or published and still stand outside it. "
            "Retiring the hub did not withdraw them; a retraction or erratum "
            "is owed for each:",
            *(_artifact_line(r) for r in plan.external),
            "",
            "Close this todo once the retraction or erratum is out, or with "
            "the reason none is needed.",
        ]
    )
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": [
                "retraction published",
                "erratum published",
                "none needed",
            ],
            "blocks": "nothing",
            "context": f"{hub} {done}: {plan.reason}"[:600],
        },
        "retired_hub": plan.ref_id,
    }
    check_ask_meta(meta, question=title)
    ref = store.insert_ref(kind="todo", slug=None, title=title, meta=meta, conn=conn)
    store.add_tag(
        ref.id,
        Tag.closed("STATUS", "open"),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    store.add_tag(ref.id, Tag.open("ask-user"), set_by="system", conn=conn)
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
        conn=conn,
    )
    return int(ref.id)


def tombstone_line(store: Store, ref_id: int) -> str | None:
    """The retired hub's tombstone line from its publish record:
    ``retired: superseded by fi<M> — <reason>`` or ``retracted: <reason>``;
    ``None`` when the retirement procedure never ran on it."""
    row = store.nanopub_retirement(ref_id)
    if row is None:
        return None
    if row.retired_successor_ref_id is not None:
        return (
            f"retired: superseded by fi{row.retired_successor_ref_id} — "
            f"{row.retired_reason}"
        )
    if row.state == "retracted":
        return f"retracted: {row.retired_reason}"
    return f"retired (publish record {row.state}): {row.retired_reason}"
