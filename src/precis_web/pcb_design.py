"""PCB binding for the shared tool-less chat engine, not another turn engine.

Shown identity is raw relational state, never replay-normalized or a routing job
hash. Only one canonical stored move may be explicitly applied. The short cache/
parent/leaf guard sees committed state after lock acquisition and shares every
read/write/history operation on one connection. Failed validation rolls back,
including handler copper/group collateral; model calls always run outside locks.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from psycopg import Connection, Error

from precis.design import history
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.pcb import PcbHandler
from precis.pcb.snapshot import VOLATILE, capture_raw

_UUID = re.compile(
    r"easyeda:packageDetail:([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$"
)


@dataclass(frozen=True)
class PcbView:
    ref: Any
    raw: dict[str, Any]
    token: str
    revision: int | None
    instances: list[dict[str, Any]]

    @property
    def context(self) -> dict[str, Any]:
        return {
            "board": {"ref_id": self.ref.id, "slug": self.ref.slug},
            "shown": {"snapshot": self.token, "revision": self.revision},
            "selected": [],
        }


def _stable(raw: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(raw)
    for table, rows in result["tables"].items():
        for row in rows:
            if table == "parts":
                for key in ("stock", "price", "basic", "jlcpcb_assemblable"):
                    row.pop(key, None)
            for key in VOLATILE | {"description_tsv"}:
                row.pop(key, None)
    return result


def _owners(raw: dict[str, Any]) -> None:
    """Foreign dependencies are rejected, never silently locked/captured."""
    tables = raw["tables"]
    rid = raw["design"]["ref_id"]
    ids = {
        key: {r[key] for r in tables[table]}
        for table, key in (
            ("pcb_boards", "board_id"),
            ("pcb_components", "component_id"),
            ("pcb_instances", "instance_id"),
            ("pcb_pins", "pin_id"),
            ("pcb_nets", "net_id"),
            ("pcb_routes", "route_id"),
        )
    }
    for table, rows in tables.items():
        if table in {"parts", "part_footprints"}:
            continue
        for row in rows:
            if "ref_id" in row and row["ref_id"] != rid:
                raise ValueError("cross-owner PCB dependency")
            for key, owned in ids.items():
                if key in row and row[key] is not None and row[key] not in owned:
                    raise ValueError(f"cross-owner PCB dependency: {table}.{key}")
    boards = tables["pcb_boards"]
    # A bare ref is a valid read-only reader. Without a board it has no
    # instances, so strict selection still refuses before model generation.
    if len(boards) > 1 or (boards and boards[0].get("retired_at")):
        raise ValueError("selection requires exactly one live physical board")


def capture_view(store: Any, ref: Any, *, conn: Connection) -> PcbView:
    current = store.get_ref(kind="pcb", id=ref.slug, conn=conn)
    if current is None or current.id != ref.id:
        raise ValueError("PCB binding changed; reload")
    raw = capture_raw(conn, str(current.slug))
    _owners(raw)
    tables = raw["tables"]
    # A reverse foreign child might be omitted by the board-scoped projection.
    foreign = conn.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM pcb_instances i JOIN pcb_components c USING(component_id)
            JOIN pcb_boards b USING(board_id)
          WHERE %s IN (i.ref_id,c.ref_id,b.ref_id)
            AND (i.ref_id<>c.ref_id OR i.ref_id<>b.ref_id)
          UNION ALL
          SELECT 1 FROM pcb_netconns k JOIN pcb_nets n USING(net_id)
            JOIN pcb_instances i USING(instance_id)
          WHERE %s IN (n.ref_id,i.ref_id) AND n.ref_id<>i.ref_id
          UNION ALL
          SELECT 1 FROM pcb_features f JOIN pcb_boards b USING(board_id)
          WHERE %s IN (f.ref_id,b.ref_id) AND f.ref_id<>b.ref_id
        )""",
        (ref.id, ref.id, ref.id),
    ).fetchone()
    if foreign and foreign[0]:
        raise ValueError("cross-owner PCB dependency")
    for table in (
        "pcb_routes",
        "pcb_planes",
        "pcb_copper",
        "pcb_pin_swaps",
        "pcb_fixed_copper",
    ):
        row = conn.execute(
            f"""SELECT EXISTS (
            SELECT 1 FROM {table} c JOIN pcb_boards b USING(board_id)
                LEFT JOIN pcb_nets n USING(net_id)
            WHERE %s IN (b.ref_id,n.ref_id) AND n.ref_id<>b.ref_id)""",
            (ref.id,),
        ).fetchone()
        if row and row[0]:
            raise ValueError("cross-owner board/net dependency")
    revisions = history.list_revisions(store, ref.id, conn=conn)
    rev = revisions[-1].rev if revisions else None
    clean = _stable(raw)
    token = (
        "pcb-view-v1:"
        + hashlib.sha256(
            json.dumps(
                clean, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
    )
    components = {r["component_id"]: r for r in tables["pcb_components"]}
    cached = {r["lcsc"]: r for r in tables["part_footprints"]}
    local = {r["name"]: r for r in tables["pcb_local_footprints"]}
    instances = []
    for row in tables["pcb_instances"]:
        if row.get("retired_at"):
            continue
        component = components[row["component_id"]]
        if component.get("retired_at"):
            raise ValueError("live instance depends on a retired component")
        part = component.get("part_lcsc")
        label = component.get("footprint")
        footprint: dict[str, Any] = {
            "scheme": None,
            "id": None,
            "part_lcsc": part,
            "label": label,
            "available": False,
        }
        if part:
            match = _UUID.fullmatch(str(cached.get(part, {}).get("source") or ""))
            if match:
                footprint.update(scheme="easyeda", id=match[1], available=True)
        elif label in local:
            footprint.update(
                scheme="pcb-local", id={"ref_id": ref.id, "name": label}, available=True
            )
        instances.append(
            {
                "instance_id": row["instance_id"],
                "refdes": row["refdes"],
                "footprint": footprint,
            }
        )
    return PcbView(current, raw, token, rev, instances)


def resolve_selection(view: PcbView, context: Any) -> dict[str, Any]:
    if not isinstance(context, dict) or set(context) != {"board", "shown", "selected"}:
        raise ValueError("invalid PCB selection context")
    if (
        context["board"] != view.context["board"]
        or context["shown"] != view.context["shown"]
    ):
        raise ValueError("shown PCB snapshot changed; reload and select again")
    selected = context["selected"]
    if not isinstance(selected, list) or len(selected) != 1:
        raise ValueError("select exactly one component instance")
    item = selected[0]
    if not isinstance(item, dict) or type(item.get("instance_id")) is not int:
        raise ValueError("invalid instance identity")
    actual = next(
        (r for r in view.instances if r["instance_id"] == item["instance_id"]), None
    )
    if actual is None or actual != item:
        raise ValueError("selected instance/footprint is absent, ambiguous or forged")
    return {**view.context, "selected": [copy.deepcopy(actual)]}


def view_context(store: Any, slug: str, context: Any) -> dict[str, Any]:
    with store.pcb_view_tx() as conn:
        ref = store.get_ref(kind="pcb", id=slug, conn=conn)
        if ref is None:
            raise NotFound("PCB design not found")
        return resolve_selection(capture_view(store, ref, conn=conn), context)


def vet_move(ops: list[dict[str, Any]], context: Any = None) -> str | None:
    if not ops:
        return None
    if len(ops) != 1:
        return "PCB supports exactly one selected instance move"
    op = ops[0]
    if set(op) - {"op", "refdes", "x", "y", "rot", "fixed"} or op.get("op") != "move":
        return "PCB supports only move{refdes,x?,y?,rot?,fixed?}"
    if not isinstance(op.get("refdes"), str) or not op["refdes"]:
        return "move requires exact refdes"
    if not set(op) & {"x", "y", "rot", "fixed"}:
        return "move requires at least one supported change"
    for key in ("x", "y", "rot"):
        if key in op and (
            type(op[key]) not in (int, float) or not math.isfinite(op[key])
        ):
            return f"move.{key} must be a finite number"
    if "fixed" in op and op["fixed"] not in (None, "xy", "rot", "both"):
        return "fixed must be null, xy, rot or both"
    if context is not None and op["refdes"] != context["selected"][0]["refdes"]:
        return "move target differs from the selected instance"
    return None


def _move(
    hub: Hub,
    view: PcbView,
    context: dict[str, Any],
    ops: list[dict[str, Any]],
    conn: Connection,
) -> dict[str, Any]:
    if error := vet_move(ops, context):
        raise ValueError(error)
    if not ops:
        raise ValueError("nothing to apply")
    selected = context["selected"][0]
    if {"x", "y", "rot"} & ops[0].keys():
        footprint = selected["footprint"]
        geometry = (
            [
                r
                for r in view.raw["tables"]["part_footprints"]
                if r["lcsc"] == footprint["part_lcsc"]
            ]
            if footprint["part_lcsc"]
            else [
                r
                for r in view.raw["tables"]["pcb_local_footprints"]
                if r["name"] == footprint["label"]
            ]
        )
        if not footprint["available"] or not geometry or not geometry[0].get("pads"):
            raise ValueError(
                "geometry move needs a trusted footprint; identity unavailable"
            )
        if len(view.instances) > 128 or len(view.raw["tables"]["pcb_pins"]) > 2048:
            raise ValueError("geometry move exceeds the bounded validator envelope")
    if all(
        next(
            r
            for r in view.raw["tables"]["pcb_instances"]
            if r["instance_id"] == selected["instance_id"]
        )[k]
        == v
        for k, v in ops[0].items()
        if k in {"x", "y", "rot", "fixed"}
    ):
        raise ValueError("move has no supported state change")
    PcbHandler(hub=hub)._op_move(view.ref, ops[0])
    after = capture_view(hub.live_store, view.ref, conn=conn).raw
    allowed = _stable(view.raw)
    for row in allowed["tables"]["pcb_instances"]:
        if row["instance_id"] == selected["instance_id"]:
            row.update(
                {k: v for k, v in ops[0].items() if k in {"x", "y", "rot", "fixed"}}
            )
    if allowed != _stable(after):
        raise ValueError(
            "move changes collateral/group/router/annotation state; refused"
        )
    return after


def dry_run(
    hub: Hub, ref: Any, context: dict[str, Any], ops: list[dict[str, Any]]
) -> str | None:
    started = time.monotonic()
    try:
        with hub.live_store.pcb_guarded_tx(ref.id, str(ref.slug)) as conn:
            view = capture_view(hub.live_store, ref, conn=conn)
            canonical = resolve_selection(view, context)
            with conn.transaction(force_rollback=True):
                _move(hub, view, canonical, ops, conn)
    except (ValueError, BadInput, NotFound, Error) as exc:
        return str(exc)
    finally:
        # Recorded by the existing web logger; tests measure residence separately.
        import logging

        logging.getLogger(__name__).info(
            "pcb dry guard %.6fs", time.monotonic() - started
        )
    return None


def digest(store: Any, slug: str, *, context: dict[str, Any] | None = None) -> str:
    with store.pcb_view_tx() as conn:
        ref = store.get_ref(kind="pcb", id=slug, conn=conn)
        if ref is None:
            raise NotFound("PCB design not found")
        view = capture_view(store, ref, conn=conn)
        if context is not None:
            resolve_selection(view, context)
        poses = {
            r["instance_id"]: {k: r[k] for k in ("x", "y", "rot", "fixed", "layer")}
            for r in view.raw["tables"]["pcb_instances"]
        }
        return json.dumps(
            {
                "board": view.context["board"],
                "shown": view.context["shown"],
                "instances": [
                    {**i, "pose": poses[i["instance_id"]]} for i in view.instances
                ],
            },
            sort_keys=True,
        )


def apply_proposal(hub: Hub, slug: str, turn: str | None, shown: str | None) -> Any:
    from precis_web import design_turn

    result = design_turn.TurnResult
    store = hub.live_store
    started = time.monotonic()
    try:
        ref = store.get_ref(kind="pcb", id=slug)
        cslug = design_turn.conv_slug(slug, kind="pcb")
        conv = store.get_ref(kind="conv", id=cslug)
        if ref is None or conv is None or not turn or not shown:
            raise ValueError("stored proposal and shown snapshot required")
        with store.pcb_guarded_tx(
            ref.id, slug, conv_id=conv.id, conv_slug=cslug
        ) as conn:
            current = store.get_ref(kind="conv", id=conv.id, conn=conn)
            if (
                current is None
                or current.meta.get("design_kind") != "pcb"
                or current.meta.get("design") != slug
            ):
                raise ValueError("conversation ownership mismatch")
            links = conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id=%s AND relation='related-to'",
                (conv.id,),
            ).fetchall()
            if links != [(ref.id,)]:
                raise ValueError("proposal ownership link changed")
            blocks = store.chunks.list_chunks_for_ref(conv.id, conn=conn)
            eligible = [
                b
                for b in blocks
                if (b.meta or {}).get("outcome", "").startswith("proposal ")
            ]
            latest = eligible[-1] if eligible else None
            if latest is None or turn != f"{cslug}~{latest.ord}":
                raise ValueError("proposal is absent or superseded")
            meta = latest.meta or {}
            if meta.get("design_kind") != "pcb" or meta.get("design") != slug:
                raise ValueError("proposal belongs to another design")
            if any(
                r.turn == turn for r in history.list_revisions(store, ref.id, conn=conn)
            ):
                raise ValueError("proposal already applied")
            view = capture_view(store, ref, conn=conn)
            context = resolve_selection(view, meta.get("context"))
            if shown != view.token:
                raise ValueError("shown snapshot changed; reload")
            ops = meta.get("ops")
            if not isinstance(ops, list) or not all(isinstance(op, dict) for op in ops):
                raise ValueError("invalid stored proposal")
            after = _move(hub, view, context, ops, conn)
            rev = (view.revision or 0) + 1
            if view.revision is None:
                history.save_checkpoint(
                    store,
                    ref.id,
                    label="pcb-before-first-chat",
                    payload=view.raw,
                    envelope_revision=0,
                    conn=conn,
                )
            checkpoint = history.save_checkpoint(
                store,
                ref.id,
                label=f"pcb-chat-{rev}",
                payload=after,
                envelope_revision=rev,
                conn=conn,
            )
            history.record_revision(
                store,
                ref.id,
                rev=rev,
                ops=ops,
                turn=turn,
                checkpoint_id=checkpoint.id,
                conn=conn,
            )
        return result(applied=True, ops=ops, valid=True, revision=rev, turn=turn)
    except (ValueError, BadInput, NotFound, Error) as exc:
        return result(applied=False, error=str(exc), turn=turn)
    finally:
        import logging

        logging.getLogger(__name__).info(
            "pcb apply guard %.6fs", time.monotonic() - started
        )
