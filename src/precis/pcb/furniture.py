"""Board furniture assembly — the ONE place fiducials, the title block,
the serial-number patch and per-part silkscreen are minted and ordered.

This was ``PcbHandler._board_furniture`` until ``pcb_argue`` (the worker
job) needed the same assembly and the workers->handlers layering contract
refused the import. It lives here — above :mod:`precis.pcb.silk` and
:mod:`precis.pcb.planes` whose builders it orchestrates — so the handler's
three render sites (:meth:`_render_gerber`, :meth:`_render_drc`,
:meth:`_render_fab_svg`) and the worker's selection catalogue share ONE
model: one rule, one call site, the discipline this package's docstrings
keep recording as the recurring defect.
"""

from __future__ import annotations

from typing import Any

from precis.pcb import ir as pcb_ir
from precis.pcb import planes as pcb_planes
from precis.pcb import silk as pcb_silk
from precis.pcb.capabilities import CapabilityRow

__all__ = ["board_furniture"]


def board_furniture(
    ir: pcb_ir.PcbIR,
    pads: list[dict[str, Any]],
    *,
    vias: list[dict[str, Any]],
    instance_sides: dict[str, str],
    copper: list[dict[str, Any]],
    outline: list[list[float]] | list[tuple[float, float]] | None,
    layer_names: list[str],
    slug: str,
    clearance_mm: float | None,
    capability: CapabilityRow | None = None,
    date: str | None = None,
    polarized: frozenset[str] = frozenset(),
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    list[str],
    tuple[pcb_silk.SilkPlacement, ...],
    list[dict[str, Any]],
]:
    """Fiducials, title block and silkscreen, assembled ONCE.

    Returns ``(pads, silk_draws, warnings, census, copper)`` — ``pads``
    with the fiducial flashes folded in (they are ordinary
    ``model["pads"]`` entries, so copper and the swelled mask opening
    fall out of the existing pad pipeline untouched), ``census`` (one
    :class:`~precis.pcb.silk.SilkPlacement` per per-instance courtyard/
    pin-1/refdes item :func:`~precis.pcb.silk.build_silk` considered)
    for a caller wiring ``view='drc'`` (the DRC render (:meth:`precis.handlers.pcb.PcbHandler._render_drc`)) — the
    structured record :func:`precis.pcb.drc.check_silk_missing`/
    :func:`~precis.pcb.drc.check_silk_printability` read, rather than
    this function's own ``warnings`` prose — and ``copper``, which is
    the ``copper`` PARAMETER given back with any plane pour, ON EVERY
    LAYER a fiducial occupies, antipadded around the fiducial discs
    just placed (:func:`precis.pcb.planes.cut_antipads`, called below
    once :func:`~precis.pcb.silk.build_fiducials` has run). A caller
    MUST use this returned ``copper``, not the one it passed in, for
    everything downstream (the gerber/DRC/fab-SVG model's
    ``"copper"`` key) — this function does not mutate the input list
    in place, so keeping the old reference silently un-does the cut
    this exists to make. ``clearance_mm`` is the caller's own
    resolved figure (:func:`precis.pcb.session.furniture_clearance_mm`) for that same
    cut — ``None`` only when the board's stackup has no capability
    row at all (an unsupported layer count), which reports as a
    warning and leaves any pour on any layer a fiducial occupies
    un-cut rather than raising, since a board in that state carries no
    pour to cut in the first place (that same docstring's own
    reasoning).

    ``polarized`` is passed straight through to
    :func:`~precis.pcb.silk.build_silk` — the refdes of every instance
    that carries REAL polarity (an electrolytic/tantalum cap, a
    polarized inductor), determined by the caller from the design's
    component flags/labels (this function only sees ``ir``/``pads``,
    never a label). Defaults to empty, same "every pre-2026-09-01
    caller's behaviour is unchanged" contract ``build_silk`` itself
    documents.

    **This exists because there are two render sites** —
    :meth:`_render_gerber` (the fab set a human orders from) and the
    fab SVG (the picture they inspect it with) — and this subsystem's
    recurring defect is one rule implemented at two call sites that
    then drift. A board whose picture shows fiducials the gerbers
    lack is exactly that bug, and it would look like a rendering
    nicety rather than a fab defect.

    **Order is load-bearing, not stylistic — and this paragraph
    describes what the code below actually does, not the reverse.**
    An EARLIER version of this function placed fiducials first (they
    are optical targets a machine is told to look at, so the instinct
    is "they should not have to move for anything else") — but a
    fiducial pinned to the outline's bottom-right corner then blocked
    every candidate in the title block's own margin ladder and
    silently dropped it from the rendered board (found by rendering
    and looking, not by any test; see the comment ahead of the title
    block call below). The title block and the S/N patch are each
    anchored to a specific outline-bbox corner with a short,
    fixed-order fallback ladder (:func:`precis.pcb.silk.
    build_title_block`/:func:`~precis.pcb.silk.build_sn_patch`) — they
    have almost nowhere else to go. Fiducials, by contrast,
    :func:`~precis.pcb.silk.build_fiducials` walks ALL four corners
    and is content with whichever three clear first, so THEY are the
    one board-level feature with real room to move around something
    already committed. The actual order below is therefore: **title
    block first, the S/N patch second** (against the title block,
    which becomes an obstacle for it, not merely an anchor — see the
    comment ahead of that call), **fiducials third** (checked against
    both), **and per-part silk last of all**, against everything the
    first three placed — the only ordering under which every board-
    level feature that CAN move out of the way for another one
    actually does.
    """
    warnings: list[str] = []
    if not outline:
        # Both builders need a bounding box. Say so rather than
        # returning a board that is quietly missing its fab targets.
        warnings.append(
            "no outline feature — skipped fiducials and the title block "
            "(both are placed relative to the board's bounding box)"
        )
        silk_only = pcb_silk.build_silk(
            ir,
            pads,
            vias=vias,
            instance_sides=instance_sides,
            capability=capability,
            polarized=polarized,
        )
        warnings.extend(f"silk: {m}" for m in silk_only.dropped)
        warnings.extend(f"silk: {m}" for m in silk_only.relocated)
        return pads, silk_only.draws, warnings, silk_only.census, copper

    # **Title block FIRST, fiducials second.** Both want a corner of the
    # same bounding box, and only the fiducials can go somewhere else:
    # `build_fiducials` walks a list of corners and takes the first that
    # are clear, while `build_title_block` stacks at ONE corner and is
    # dropped outright if that corner is occupied.
    #
    # Placing fiducials first put one at the outline's bottom-right,
    # which blocked every title-block candidate inside its margin ladder
    # and silently dropped the block from the rendered board — found by
    # rendering and looking, not by any test. `build_title_block`'s own
    # docstring says to place it first; this is that instruction.
    # Routed vias are obstacles for BOTH furniture pieces (silk
    # dodges copper, the module's normal direction): neither builder
    # used to see them, and the S/N patch — the one silk feature
    # that exists to be WRITTEN on — ended up with stitch-via bumps
    # under the Sharpie area (round-3 review item 2).
    via_avoid = pcb_silk.via_obstacles(vias)
    # Part COURTYARDS too (as bbox obstacles): the furniture ladders
    # gained inward rungs (edge-slide, edge-centre — to escape corner
    # mounting hardware), which can now propose spots inside the
    # parts field. The builders only check pads, so a slid title
    # block landed beside C14 on the 40mm stress fixture and cost
    # that part its courtyard silk (clipped below legibility) — the
    # furniture must yield to per-part silk it cannot see.
    # Inflated past the bare courtyard hull (`world_courtyard_rings`
    # is clearance-0): the DRAWN courtyard outline sits a silk
    # clearance further out, and a furniture box that clears the hull
    # but not the stroke still clips that stroke below legibility —
    # exactly the C14 drop above, reproduced at hull-only inflation.
    # At least the slot a refdes label hung above/below the box needs
    # (`refdes_label_slot_mm`): a tighter margin lets furniture settle
    # where every label candidate is blocked -> silk_missing.
    silk_clearance = pcb_silk.silk_clearance_mm(
        capability, stroke_width_mm=pcb_silk.DEFAULT_SILK_WIDTH_MM
    )
    court_margin = max(
        silk_clearance + 1.0, pcb_silk.refdes_label_slot_mm(silk_clearance)
    )
    courtyard_avoid = [
        pcb_silk.obstacle_from_bbox(
            [
                (
                    min(p[0] for p in ring) - court_margin,
                    min(p[1] for p in ring) - court_margin,
                ),
                (
                    max(p[0] for p in ring) + court_margin,
                    min(p[1] for p in ring) - court_margin,
                ),
                (
                    max(p[0] for p in ring) + court_margin,
                    max(p[1] for p in ring) + court_margin,
                ),
                (
                    min(p[0] for p in ring) - court_margin,
                    max(p[1] for p in ring) + court_margin,
                ),
            ]
        )
        for ring in pcb_silk.world_courtyard_rings(ir)
        if ring
    ]
    furniture_avoid = courtyard_avoid + via_avoid
    title = pcb_silk.build_title_block(
        outline,
        pads,
        name=slug,
        date=date,
        avoid=furniture_avoid,
        capability=capability,
    )
    warnings.extend(f"title block: {m}" for m in title.dropped)

    # The serial-number patch sits against the title block, so it is
    # placed immediately after it and before anything that would
    # compete for the same corner.
    # The title block is an OBSTACLE for the patch, not merely an anchor.
    # Passing only `title_bbox` positioned the patch relative to the
    # block but left nothing stopping it from overlapping — and a solid
    # silk box painted over the board name erases it, which is worse
    # than either element being dropped.
    sn = pcb_silk.build_sn_patch(
        outline,
        pads,
        title_bbox=title.bbox,
        avoid=(
            [pcb_silk.obstacle_from_bbox(title.bbox)] if title.bbox is not None else []
        )
        + furniture_avoid,
        capability=capability,
    )
    warnings.extend(f"S/N patch: {m}" for m in sn.dropped)

    # Every copper layer, not just the top one: a fiducial is a
    # whole-stack registration mark — inner layers and the bottom film
    # need the same optical target the top one gets, or a fab has
    # nothing to align them against (this is that fix). Falls back to
    # a single synthetic "F.Cu" only for the no-stackup-known case
    # `build_fiducials` already tolerated before this change.
    fid_layers = list(layer_names) if layer_names else ["F.Cu"]
    # `obstacle_from_bbox` returns a pad-shaped dict, which is exactly
    # what `build_fiducials` checks its candidate corners against — so
    # the block becomes an ordinary obstacle rather than a special case.
    fid_obstacles = list(pads)
    for placed_bbox in (title.bbox, sn.bbox):
        if placed_bbox is not None:
            fid_obstacles.append(pcb_silk.obstacle_from_bbox(placed_bbox))
    # `ir=` feeds the parts-thicket exclusion — the SAME filter the
    # router's pre-claim (`realize._claim_fiducial_keepouts`) applies,
    # off the same IR, so the mint can only ever choose a site the
    # router already reserved copper-free.
    fid = pcb_silk.build_fiducials(outline, fid_obstacles, layers=fid_layers, ir=ir)
    warnings.extend(f"fiducial: {m}" for m in fid.dropped)

    # `FiducialResult.plane_blockers` cut in HERE, at render time, off
    # the already-realized+persisted pours — `planes.plane_pours` ran
    # at REALIZE time, before these fiducials existed, so it could not
    # have antipadded them (see that dataclass's own docstring for the
    # timing gap). `cut_antipads` punches the same no-pour ring
    # `plane_pours` would have, sized off the SAME `clearance_mm` this
    # handler resolves from `capabilities.py` (the caller's own
    # docstring), around each fiducial's MASK opening (the blocker
    # discs are already sized to `mask_dia_mm`, not the bare copper) —
    # ON EVERY LAYER a blocker names (`fid.plane_blockers` now carries
    # all of `fid_layers`, so this one call already cuts a pour on an
    # inner layer or B.Cu exactly as it always cut one on F.Cu; no
    # per-layer loop needed, `cut_antipads` matches a blocker's
    # `layers` list against each pour's own layer internally). Non-pour
    # copper (tracks/vias/pads) passes through untouched.
    pour_items = [c for c in copper if c.get("ctype") == "pour"]
    if fid.plane_blockers and pour_items and clearance_mm is None:
        # No capability row for this stackup (see
        # `_furniture_clearance_mm`'s own docstring) — a board that
        # somehow carries a pour anyway (it should not: pouring one
        # requires the same lookup to have already succeeded at
        # realize time) is reported here rather than crashing a
        # render that otherwise has nothing to do with this cut.
        warnings.append(
            "fiducial: no fab capability row for this stackup — "
            "pour(s) left uncut around the fiducials on every layer"
        )
    elif fid.plane_blockers and pour_items:
        assert clearance_mm is not None  # narrowed by the branch above
        cut_pours = pcb_planes.cut_antipads(
            pour_items, fid.plane_blockers, clearance_mm=clearance_mm
        )
        copper = [c for c in copper if c.get("ctype") != "pour"] + cut_pours
        # The cut is geometry, not a promise — verify the invariant it
        # exists for (a fiducial's mask opening is clear of copper on
        # EVERY layer it occupies, not just one) rather than trusting
        # it silently. The one way this can legitimately still fail:
        # the antipad ring's own area falls under `MIN_FRAGMENT_MM2`
        # (`cut_antipads` reuses the SAME noise floor `plane_pours`
        # uses for a sliver fragment, shared via `_pour_item`), so the
        # hole is filtered out as buffering noise and the pour
        # re-solidifies over the fiducial — a real fiducial-mask-
        # opening-vs-fab-noise-floor collision, not a timing gap this
        # function can no longer close.
        for fx, fy in fid.fiducials:
            still_covered = sorted(
                {
                    str(p.get("layer") or "")
                    for p in cut_pours
                    if pcb_planes.point_in_pour(p, fx, fy)
                }
            )
            if still_covered:
                warnings.append(
                    f"fiducial: antipad ring at ({fx:.2f}, {fy:.2f}) on "
                    f"{', '.join(still_covered)} was too small to survive "
                    f"as a real hole (< {pcb_planes.MIN_FRAGMENT_MM2}mm^2) "
                    "— the pour still covers this fiducial there"
                )

    pads = pads + fid.pads

    # Everything already placed becomes an obstacle for the per-part
    # labels, on BOTH sides: a fiducial is a copper/mask feature and
    # blocks silk wherever it lands.
    board_obstacles = list(fid.silk_keepouts)
    for placed_bbox in (title.bbox, sn.bbox):
        if placed_bbox is not None:
            board_obstacles.append(pcb_silk.obstacle_from_bbox(placed_bbox))
    reserved = {side: list(board_obstacles) for side in ("top", "bottom")}

    silk_result = pcb_silk.build_silk(
        ir,
        pads,
        vias=vias,
        instance_sides=instance_sides,
        reserved=reserved,
        capability=capability,
        outline=[(float(p[0]), float(p[1])) for p in outline],
        polarized=polarized,
    )
    warnings.extend(f"silk: {m}" for m in silk_result.dropped)
    warnings.extend(f"silk: {m}" for m in silk_result.relocated)

    draws = {side: list(items) for side, items in silk_result.draws.items()}
    draws.setdefault("top", []).extend(title.draws)
    draws.setdefault("top", []).extend(sn.draws)
    return pads, draws, warnings, silk_result.census, copper
