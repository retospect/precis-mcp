"""Every film a fab receives must also reach the viewer.

The layer-selector SVG (``view='svg' args={'level':'fab'}``) is rendered
FROM the gerbers rather than from the model, precisely so that what a
human inspects is the artefact and not a stylistically different picture
of it. That only holds if the gerber set is complete: a film that
``export_fab`` never writes is a film nobody can look at, and the way that
failure presents is an *absence* — which is exactly the shape of bug this
subsystem has repeatedly shipped (silkscreen empty for the whole build,
solder paste missing entirely, drills unrendered).

So this test asserts the set, not a sample. It drives the real tool
surface end to end — author, declare the planes, place, route, render —
and checks that every expected layer group is present in the SVG *and*
carries geometry.

**Ground planes are DECLARED here, not discovered.** ``op='plane_net'`` is
the authored path; the annealer does not choose plane promotion on this
fixture (measured: 79 PLANE_PROMOTE moves proposed over 3000 iterations,
all rejected on cost). Declaring GND and VCC3V3 is both what a human
designing a 4-layer board actually does and the only thing that exercises
the pour path end to end.

Set ``PRECIS_PCB_RENDER_OUT=/path/to/board.svg`` to keep the rendered
board for inspection; unset, the test still runs and asserts everything.
"""

from __future__ import annotations

import collections
import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass

pytestmark = pytest.mark.slow

FIXTURE = Path(__file__).parent / "fixtures" / "pcb" / "esp32c3_reference.json"

#: The reference fixture's own outline is 300x300mm for ~44mm of parts —
#: a known sensitivity gap (a fixture that cannot get harder cannot detect
#: a router getting worse). Rendering it at 300mm produces a speck in the
#: corner of its own board. Measured previously: 60/45/35mm all give 0
#: unrouted / 0 islands / 0 DRC errors, so tightening here changes what
#: the picture SHOWS without changing what it PROVES.
_BOARD_MM = 40.0

#: Asserted over EVERY seed (worst-case), one parametrized test each —
#: same rationale as ``tests/test_pcb_reference_end_to_end.py``'s
#: ``SEEDS``: a single pinned seed measures one lottery draw of the
#: anneal, and on THIS deliberately over-packed board the draw-to-draw
#: variance is highest, which is exactly where a single-seed waiver
#: invites constant-nudging.
SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)

#: ``PRECIS_PCB_RENDER_OUT`` writes one board, not five — by default the
#: first seed's, so the kept artifact stays comparable run to run;
#: ``PRECIS_PCB_RENDER_SEED`` overrides which draw gets written (e.g. seed
#: 2, the ledger's clean draw, for a best-case picture).
_RENDER_SEED = int(os.environ.get("PRECIS_PCB_RENDER_SEED", str(SEEDS[0])))

#: **57 errors down to 2, over 2026-08-30.** This board is deliberately
#: too small for its own parts — ~44mm of parts on a 40mm outline — so it
#: is the stress fixture, not the acceptance one
#: (``tests/test_pcb_reference_end_to_end.py`` is that, at natural size,
#: and holds at zero).
#:
#: Every entry below was an engine defect that PRE-DATED the DRC rules
#: which reported it; none was a regression. They became visible together
#: when ``_render_drc`` began folding board furniture into the DRC model
#: and the plane stitcher started reporting honestly. The
#: ``board_edge_clearance`` and ``connectivity`` numbers are corroborated
#: verbatim by a checkpoint written BEFORE that work landed
#: (``docs/backlog/pcb-engine-plan.md``, "0.390 vs 0.400mm — 10um short";
#: "GND in 3 pieces; VCC3V3 in 2").
#:
#: The retired entries are kept because a shrinking waiver is the one
#: thing that cannot explain itself: a reader cannot tell a board that was
#: always clean from one whose defects were fixed, and each fix here was
#: structural rather than a tuning nudge. **Do not add an entry back, or
#: raise one, to make a red run green** — see the ``silk_missing`` note
#: for what that costs when it is genuinely warranted.
#:
#: - ``clearance`` -- FIXED. A fiducial (net ``""``) used to come back
#:   flooded by a GND pour: fiducials are synthesised at RENDER time, pour
#:   antipads used to be cut only at REALIZE time, and neither pass knew
#:   about the other, so the optical alignment targets shipped buried under
#:   copper on the delivered gerber. The fix is exactly what a fiducial
#:   inside a flood always needed — a no-pour ring, not relocation:
#:   ``handlers/pcb.py::_board_furniture`` now cuts one straight into the
#:   already-realized pour dicts via ``planes.cut_antipads`` immediately
#:   after ``build_fiducials`` runs, sized off the same fab-capability
#:   ``clearance_mm`` ``plane_pours`` itself uses.
#: - ``board_edge_clearance``: FIXED. Was a VCC3V3 via 10um inside the
#:   0.400mm 4-layer V-cut floor. Not a missing edge check —
#:   ``maze.grid_for`` rounded its node count UP, putting the last grid
#:   node up to a pitch outside the very clip ``_outline_clip`` had just
#:   inset. Flooring it handed the margin back.
#: - ``connectivity`` (was 2, then 1, now 0 — FIXED, waiver removed).
#:   VCC3V3's split went first: its orphan was the same drop via that sat
#:   past the board edge, so placing it legally also put it back in the
#:   pour. GND's outlasted it and was structural, not a tuning failure —
#:   GND is poured on ``F.Cu`` ONLY (measured: 4 fragments, all one layer),
#:   and a via joins LAYERS, not lateral gaps, so the stitcher had no
#:   second sheet to detour through and provably could not close it. Fixed
#:   by the two-via/spare-layer jumper (``realize.py::_try_plane_jumper``),
#:   which routes across a layer that is neither fragment's own.
#: - ``silk_missing`` (61 → 25 → 0 — FIXED, waiver removed). Four
#:   mechanisms, and each was a different defect wearing the same rule:
#:
#:   1. A courtyard is now the hull of the part's OWN pads offset by the
#:      fab chain (``ir.instance_courtyard_polygon``), so landing on its
#:      own copper is unrepresentable rather than merely rare.
#:   2. An outline that meets someone else's copper is BROKEN around it
#:      and drawn in pieces (``silk._clip_polyline``) instead of thrown
#:      away whole — what a fab would have trimmed it to anyway. This is
#:      the one that mattered: (1) alone made the count WORSE, because the
#:      oversized square it replaced had been ENCLOSING each part's plane
#:      fan-out vias and an honest polygon passes through them instead.
#:   3. The refdes ladder became a ring sweep (``silk._refdes_candidates``,
#:      37 spots). Every remaining label finding was an artifact of the
#:      six fixed spots it replaced — measured, 16 of 16 here and 8 of 8
#:      on the natural-size reference board.
#:   4. A pin-1 tick whose courtyard corner is taken by a fan-out via
#:      falls back to a DOT beside pin 1 (``silk._pin1_dot_candidates``),
#:      the industry's other spelling for exactly that situation. A tick
#:      marks a corner and has nowhere else to go; a dot does.
#:
#: - ``silk_missing`` (0 -> 2 -> 1). It reached zero on 2026-08-30 and lost
#:   it the same day to a CORRECTNESS fix rather than a regression:
#:   ``optimize._gen_rotate`` never checked placement legality, which was
#:   right while a keep-out was a CIRCLE (rotation-invariant — spinning a
#:   part could not bring it into a neighbour) and wrong once it became a
#:   polygon. Nothing downstream caught it either: no cost term reads
#:   ``inst_rot``, so the anneal accepted every generated rotation
#:   unconditionally. Gating it removed moves the search had been using to
#:   pack a board that is deliberately too small — ~44mm of parts on 40mm.
#:
#:   **That was the one entry ever raised here rather than lowered**, and
#:   it came back down the same day. The remaining 1 is R2's refdes label,
#:   dropped because all 37 candidate spots are taken. On a board this
#:   over-packed that is the honest answer rather than a defect: the label
#:   has nowhere legal to print, and the census says exactly that.
#:
#:   Three more fixes retired the rest — all found by LOOKING at the
#:   rendered board, none by the counter, which had been reporting success
#:   over marks that were not on it:
#:
#:   5. A closed courtyard ring was clipped as an OPEN polyline
#:      (``silk._clip_polyline``), so the arc spanning its seam vertex came
#:      out as two runs. They abut exactly, so no count was ever wrong —
#:      but each run draws as its own polyline, so a seam landing on a
#:      corner lost the mitre join and showed a notch that no obstacle
#:      explained, and a seam stub under the debris floor was deleted,
#:      widening a real gap. Measured: 15 of 25 clipped outlines carried
#:      one. The splice runs before the debris filter, which is the point.
#:   6. **The pin-1 corner tick is a cut of the courtyard outline, so it
#:      printed ON that outline** — measured 0.0000mm from it for all 20
#:      ticked parts. A mark indistinguishable from the line it annotates
#:      is not a mark, and being inside the courtyard it ends up under the
#:      part once assembled. ``check_silk_missing`` passed every one of
#:      them, because it proves a draw EXISTS and cannot see that it is
#:      invisible. The dot beside pin 1 is now tried first for every part:
#:      visible pin-1 marks on this board went 8 -> 27.
#:   7. A pin-1 dot, and then a refdes label, could be committed onto a
#:      spot that a part processed LATER needed for its own body outline —
#:      the later part losing its whole courtyard on nothing more
#:      principled than refdes order. Both now yield to every instance's
#:      courtyard ring, resolved up front (``silk.build_silk``'s
#:      ``courtyard_ring``), so precedence no longer depends on processing
#:      order. This one alone took the tally 5 -> 3 -> 1.
#:
#: Nothing here may be raised again without the kind of reason written out
#: for the rotation gate above.
#:
#: A new rule, or more of an existing one, still fails — a rule absent
#: from this mapping is allowed zero.
#: 8. **2026-08-31 — the waiver became a PER-SEED ledger** when this test
#:    started asserting over ``SEEDS`` instead of one pinned draw. The
#:    scalar form's history above still governs: every count may only go
#:    DOWN, a rule absent from a seed's entry is allowed ZERO, and no
#:    entry may be raised (or added) to green a red run without a
#:    written-out reason of the rotation-gate standard. The entries below
#:    are not regressions — they are the first honest measurement of
#:    draw-to-draw variance on a board that is deliberately too small
#:    (~44mm of parts on 40mm), taken the day multi-seed assertion
#:    landed:
#:
#:    - **Seed 2 was CLEAN — every rule zero** — proving the engine can
#:      fully resolve even this over-packed board on some draws; a clean
#:      seed's empty entry is the standard the others are measured
#:      against (seeds 4 and 5 hold it in the current ledger).
#:    - Seed 3's ``clearance`` (at 0.000mm) + ``via_pad_keepout`` were
#:      the one entry class that is a GUARANTEE HOLE, not capacity:
#:      copper reached the board without claiming its corridor.
#:    - ``unrouted``/``connectivity`` entries are the plane stitcher and
#:      router honestly reporting what they provably cannot close on
#:      that draw (no spare layer, no pour-free corridor); the
#:      capacity-limit answer on a stress board.
#:    - ``silk_missing`` counts ride the same congestion, and every
#:      nonzero seed includes the known dot-suppression coupling — each
#:      dropped courtyard also silences its pin-1 dot
#:      (``docs/backlog/pcb-courtyard-polygon.md``'s open pin-1 item).
#:
#: 9. **2026-08-31 (later) — the guarantee hole is CLOSED and the ledger
#:    re-pinned under the octilinear engine.** Root cause of item 8's
#:    clearance/via_pad_keepout family: render-time FIDUCIALS were never
#:    claimed on the routing grid (they are minted by the handler AFTER
#:    routing), so a legal route could cross the corner the mint later
#:    landed on. ``realize._claim_fiducial_keepouts`` now pre-claims the
#:    obstacle-independent candidate superset
#:    (``silk.fiducial_candidate_sites``) before any pad/track claim —
#:    ``clearance`` and ``via_pad_keepout`` are ZERO on every seed and
#:    may never be waived again. The same session made routing fully
#:    octilinear (every emitter 90/45 + the ``octilinear`` DRC rule) and
#:    re-drew every seed, so the capacity entries re-rolled: seeds 4/5
#:    came back fully clean, seed 1 keeps one dropped silk, seed 3 two,
#:    and seed 2's draw now leaves BOOT and SDA unrouted — the honest
#:    re-measurement, entered the day the engine changed, not a
#:    quiet raise.
#:
#: 10. **2026-09-01 — re-measured again** after the alignment cost term
#:    (``cost.CostConfig.alignment_usd_per_pair``, tuned DOWN from 0.01
#:    to 0.002 the same day when the stronger value steered a reference
#:    seed unroutable), the per-courtyard fiducial candidate filter, the
#:    pour-rim edge inset fix, and the redundant-drop-via prune. Item
#:    9's entries all RESOLVED — seeds 1-4 are fully clean — and seed
#:    5's new draw leaves EN congested (1 connectivity + 1 unrouted),
#:    the lone capacity entry on a board that is deliberately too small.
#:
#:    The natural-size acceptance fixture
#:    (``tests/test_pcb_reference_end_to_end.py``) holds hard ZEROS on
#:    all five seeds — that is where the engine's quality claim lives;
#:    this ledger is where its variance is recorded.
KNOWN_OPEN_DRC_ERRORS: dict[int, dict[str, int]] = {
    1: {},
    2: {},
    3: {},
    4: {},
    5: {"connectivity": 1, "unrouted": 1},
}

#: The films this board must produce with geometry on them. Listed
#: explicitly rather than derived from the exporter, because a test that
#: asks the exporter what it exports cannot notice the exporter forgetting
#: something.
#:
#: ``B_Paste``/``B_Silkscreen`` are deliberately absent: every part on
#: this fixture is top-side, so those films are legitimately empty and
#: requiring geometry on them would assert a fiction. ``B_Mask`` is NOT
#: in that set — board fiducials span the whole stack (round 4), so the
#: bottom mask legitimately opens at each fiducial even on an all-top
#: board — but this tuple only lists layers asserted non-empty, so it
#: stays unlisted here. That every film is *written at all* is covered
#: by ``tests/test_pcb_fab_export.py``.
_EXPECTED_LAYERS = (
    "F_Cu",
    "In1_Cu",
    "In2_Cu",
    "B_Cu",
    "F_Mask",
    "F_Paste",
    "F_Silkscreen",
    "Edge_Cuts",
    "PTH",
)

_GROUP_RE = re.compile(
    r'<g id="layer-([A-Za-z0-9_]+)" class="[^"]*"[^>]*>(.*?)</g>', re.S
)


def _drain_one_job(store: Store, parent_id: int) -> None:
    """Drain the queued job most recently enqueued under ``parent_id``.

    Not just "whatever's next in the queue" (gr295496): the shared
    per-worker test DB can carry an orphaned job left behind by an
    unrelated test file, and ``run_job_inproc_pass(limit=1)`` claims
    strict priority/age order, so a stray row — not this design's own
    job — gets claimed first and its failure misreads as OUR drain
    failing. Loop passes, tolerating whatever unrelated row lands along
    the way, until THIS design's own job reaches a terminal status.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job was ever queued for parent_id={parent_id}"
    job_ref_id = row[0]

    for _ in range(25):
        with store.pool.connection() as conn:
            status = current_status(conn, job_ref_id)
        if status in TERMINAL:
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.parametrize("seed", SEEDS)
def test_the_fab_svg_carries_every_film_including_the_declared_ground_planes(
    store: Store, seed: int
) -> None:
    with FIXTURE.open(encoding="utf-8") as fh:
        design: dict[str, Any] = copy.deepcopy(json.load(fh))
    design["features"] = [
        {
            "ftype": "outline",
            "geom": {
                "path": [
                    [0, 0],
                    [_BOARD_MM, 0],
                    [_BOARD_MM, _BOARD_MM],
                    [0, _BOARD_MM],
                    [0, 0],
                ]
            },
        }
    ]

    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="fabrender", args=design).body

    # The arrangement a human actually asks for on 4 layers: signals route
    # on the inner pair with opposed preferred directions, and the outer
    # pair carries traces AND a copper fill in the space between them.
    #
    # Written straight to the board row because **there is no stackup
    # authoring path** — `_pcb_ensure_board` always creates
    # `DEFAULT_STACKUP` and no verb overrides it. That is a real gap in the
    # tool surface (an agent cannot ask for this board at all today), filed
    # rather than worked around; this test writes the row directly so the
    # engine below it can be exercised meanwhile.
    ref = store.get_ref(kind="pcb", id="fabrender")
    assert ref is not None
    board_id = store.pcb_ensure_board(ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_boards SET stackup = %s WHERE board_id = %s",
            (
                json.dumps(
                    [
                        {
                            "name": "F.Cu",
                            "role": "signal",
                            "routable": True,
                            "pourable": True,
                        },
                        {
                            "name": "In1.Cu",
                            "role": "signal",
                            "routable": True,
                            "pourable": False,
                        },
                        {
                            "name": "In2.Cu",
                            "role": "signal",
                            "routable": True,
                            "pourable": False,
                        },
                        {
                            "name": "B.Cu",
                            "role": "signal",
                            "routable": True,
                            "pourable": True,
                        },
                    ]
                ),
                board_id,
            ),
        )

    # Declared, not discovered — see the module docstring. Fill the two
    # OUTER layers; the inner pair stays clear for routing.
    pcb.put(id="fabrender", args={"op": "plane_net", "layer": "F.Cu", "net": "GND"})
    pcb.put(id="fabrender", args={"op": "plane_net", "layer": "B.Cu", "net": "VCC3V3"})

    assert (
        "enqueued" in pcb.put(id="fabrender", args={"op": "place", "seed": seed}).body
    )
    _drain_one_job(store, ref.id)
    assert (
        "enqueued" in pcb.put(id="fabrender", args={"op": "route", "seed": seed}).body
    )
    _drain_one_job(store, ref.id)

    svg = pcb.get(id="fabrender", view="svg", args={"level": "fab"}).body

    out = os.environ.get("PRECIS_PCB_RENDER_OUT")
    if out and seed == _RENDER_SEED:
        Path(out).write_text(svg, encoding="utf-8")

    # **The board this renders must be a board, not just a picture.**
    # This test originally asserted only that every film reached the
    # viewer, and it passed happily over a design carrying 57 DRC errors —
    # 55 of them plane drop-vias sitting on their own pads. A render handed
    # to a human as a deliverable implies the board behind it is sound, so
    # assert that here rather than letting the picture speak for it.
    #
    # Waived per SEED since 2026-08-31: `KNOWN_OPEN_DRC_ERRORS[seed]` is
    # this draw's known-open ledger entry (seed 2's is empty — fully
    # clean), and a rule absent from it is allowed zero. See that
    # mapping's own docstring for the full retired-and-open ledger — a
    # waiver cannot explain itself.
    #
    # Declaring a plane is what exercises the pour path, and it is also
    # what exposes the fan-out defects, so this assertion belongs on
    # exactly this test and not on a quieter one.
    # **DRC IS PULL-BASED. Nothing runs it for you.**
    # `run_geometric_drc` executes only from `get(view='drc')`; place and
    # route never trigger it. `pcb_drc_findings_latest` reads PERSISTED
    # rows, and its own docstring says "no run yet means 'not yet', not
    # 'clean'" — so reading it without this call returns an empty list and
    # `assert not errors` passes VACUOUSLY over any board at all.
    #
    # That is exactly what this test did when the assertion was first
    # added: it was written to stop a 57-error board being presented as a
    # deliverable, and it asserted nothing, because an absent run and a
    # clean run are the same empty list. The check that was supposed to
    # catch the trap fell into it.
    drc_view = pcb.get(id="fabrender", view="drc")
    run_id, findings = store.pcb_drc_findings_latest(ref.id)
    assert run_id is not None, (
        "no DRC run was recorded even after view='drc' — a missing run and "
        f"a clean run are indistinguishable downstream. View said: {drc_view.body[:200]!r}"
    )
    errors = [f for f in findings if f["severity"] == "error"]
    by_rule = collections.Counter(str(f["rule"]) for f in errors)
    # Compare per RULE against the waiver, never a total: a summed budget
    # lets a fixed defect pay for a new one silently.
    waiver = KNOWN_OPEN_DRC_ERRORS.get(seed, {})
    over_budget = {
        rule: (n, waiver.get(rule, 0))
        for rule, n in by_rule.items()
        if n > waiver.get(rule, 0)
    }
    # Report the OFFENDING findings with their objects, not the first six of
    # whatever the board produced. A message that names a rule and a count
    # sends the reader back to instrument a run before they can begin; the
    # objects carry the coordinates that say WHICH via or pad it is. (This
    # rule's 10um board-edge finding was diagnosed twice from the wrong
    # mechanism because this line printed prose without objects.)
    detail = "\n".join(
        f"  {f['rule']}: {f['detail']}\n    objects={f['objects']}"
        for f in errors
        if str(f["rule"]) in over_budget
    )[:3000]
    assert not over_budget, (
        f"DRC error(s) beyond the known-open waiver: "
        f"{ {r: f'{got} > {allowed}' for r, (got, allowed) in over_budget.items()} }"
        f"\nfull tally: {dict(by_rule)}\nsee KNOWN_OPEN_DRC_ERRORS[{seed}] -- a "
        f"rule absent from this seed's entry is allowed ZERO\n{detail}"
    )

    groups = {name: body for name, body in _GROUP_RE.findall(svg)}
    missing = [layer for layer in _EXPECTED_LAYERS if layer not in groups]
    assert not missing, (
        f"the fab SVG is missing layer group(s) {missing} — present: "
        f"{sorted(groups)}. A film the exporter never writes is a film "
        "nobody can inspect, and it fails silently as an absence."
    )

    # Present is not enough: an empty group renders as nothing and looks
    # identical to a layer that was never written.
    empty = [
        layer
        for layer in _EXPECTED_LAYERS
        if not re.search(r"<(path|circle|rect)\b", groups[layer])
    ]
    assert not empty, f"layer group(s) present but carrying no geometry: {empty}"

    # The planes specifically. A pour is a filled REGION (gerber G36/G37),
    # which the viewer renders as `<path d="..." fill="#rrggbb"/>`; a track
    # renders as `fill="none"` with a stroke, and a via as a `<circle>`.
    #
    # **Assert the fill, not merely that the group is non-empty.** A plane
    # layer always carries the barrels of every through via that passes
    # through it, so "In1_Cu has geometry" is satisfied by a board with no
    # plane at all — which is exactly how a declared-but-never-poured GND
    # went unnoticed until someone looked at the picture.
    for plane_layer in ("F_Cu", "B_Cu"):
        assert re.search(
            r'<path d="[^"]+" fill="#[0-9a-fA-F]{6}"', groups[plane_layer]
        ), (
            f"{plane_layer} carries no poured region — the net was declared "
            "a plane and no copper was poured for it. Note the layer is NOT "
            "empty: it holds the via barrels passing through, which is why "
            "a presence check does not catch this."
        )

    # Silk must be real text/outlines, not one stray mark.
    assert groups["F_Silkscreen"].count("<path") > 20, (
        "F_Silkscreen has almost no geometry — refdes labels and part "
        "outlines should produce many strokes across 29 parts"
    )
