"""``type='placement_legal'`` — pcb-guided-place-route placement gate.

Reads the design's LATEST persisted placement state (never recomputes an
anneal) and resolves ``True`` when:

1. every instance has a real ``(x, y)`` — no unplaced / NaN position;
2. no two courtyards overlap;
3. every courtyard sits inside the drawn board outline (when one exists);
4. locked (``fixed``) instances are placed same as anyone else — the
   INVARIANT that they are never *moved* is enforced structurally by
   :meth:`~precis.store._pcb_ops.PcbMixin.pcb_set_pose`'s per-axis SQL
   guard at write time (every ``pcb_place``/``pcb_route`` job write goes
   through it), not re-checked here by diffing against a prior snapshot.

Feeds the pcb-guided-place-route phase machine's placement gate (Slice 10
of docs/backlog/pcb-guided-place-route.md; ADR 0042 Slice 9's
"placement_legal" wording, superseded in shape by that spec's slice 10).

Spec
====

```json
{"type": "placement_legal", "pcb": "sensor-node"}
```

``pcb`` names the design by slug (or accepts a bare ref id string/int).

Scope, stated honestly (docs/conventions/llm-facing-prose.md): courtyard-
overlap and board-outline containment use each part's cached
``part_footprints.courtyard`` bbox (axis-aligned in the footprint's own
frame, rotated + translated to the instance's placed pose) — an instance
whose part has no footprint on file is SKIPPED from those two checks
(silently passing them would be wrong, but so would blocking the whole
gate on catalog coverage the placement phase can't itself fix); the board
outline check only runs when an ``outline`` feature is drawn. Full
geometric DRC (clearance/annular-ring/width, on REALIZED copper) is a
separate, later gate (``netlist_drc_clean`` / ``drc.py``) — this evaluator
never reads ``pcb_drc_findings``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers._slug_ref_shared import resolve_live_slug_ref

if TYPE_CHECKING:
    from precis.store import Store


def _resolve_pcb_ref_id(store: Store, spec: dict[str, Any]) -> int:
    raw = spec.get("pcb")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise BadInput(
            "placement_legal needs a pcb design",
            next="meta.auto_check.pcb='sensor-node' (slug, or a ref id)",
        )
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        raise BadInput("placement_legal.pcb must be a slug string or ref id")
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if s.isdigit():
        return int(s)
    ref = resolve_live_slug_ref(store, kind="pcb", id=s)
    return ref.id


def _courtyard_box(courtyard: Any, x: float, y: float, rot: float) -> Any:
    """A shapely polygon for one instance's courtyard at its placed pose,
    or ``None`` when no cached footprint courtyard is available."""
    if not isinstance(courtyard, dict):
        return None
    bbox = courtyard.get("bbox")
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        return None
    from shapely.affinity import rotate, translate  # type: ignore[import-untyped]
    from shapely.geometry import box  # type: ignore[import-untyped]

    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    poly = box(xmin, ymin, xmax, ymax)
    if rot:
        poly = rotate(poly, float(rot), origin=(0, 0))
    return translate(poly, xoff=float(x), yoff=float(y))


def _outline_polygon(store: Store, ref_id: int) -> Any:
    from shapely.geometry import Polygon

    for f in store.pcb_features_list(ref_id):
        geom = f.get("geom") or {}
        if str(f.get("ftype") or "") != "outline":
            continue
        path = geom.get("path")
        if not (isinstance(path, list) and len(path) >= 3):
            continue
        return Polygon([(float(p[0]), float(p[1])) for p in path])
    return None


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    ref_id = _resolve_pcb_ref_id(store, spec)
    design = store.pcb_load(ref_id)
    instances = design["instances"]
    if not instances:
        return False

    # 1. every instance placed (no unplaced / NaN position).
    for inst in instances:
        x, y = inst["x"], inst["y"]
        if x is None or y is None:
            return False
        if x != x or y != y:  # NaN != NaN
            return False

    footprints = store.pcb_footprints_for(ref_id)
    boxes: list[tuple[str, Any]] = []
    for inst in instances:
        fp = footprints.get(inst["part_lcsc"] or "")
        poly = _courtyard_box(
            (fp or {}).get("courtyard"), inst["x"], inst["y"], inst["rot"] or 0.0
        )
        if poly is not None:
            boxes.append((inst["refdes"], poly))

    # 2. no courtyard overlaps (best-effort — only cached-footprint parts
    #    participate, see the module docstring).
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if boxes[i][1].intersects(boxes[j][1]) and not boxes[i][1].touches(
                boxes[j][1]
            ):
                return False

    # 3. everything inside the drawn board outline (when there is one).
    outline = _outline_polygon(store, ref_id)
    if outline is not None:
        for _refdes, poly in boxes:
            if not outline.contains(poly):
                return False

    # 4. locked parts unmoved — see the module docstring: guaranteed by
    # construction at write time, nothing further to check here.
    return True
