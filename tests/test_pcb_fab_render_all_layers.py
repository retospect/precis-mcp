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

_SEED = 1

#: **A waiver, not a baseline — this board is knowingly not manufacturable.**
#:
#: Each entry is an engine defect that PRE-DATES the DRC rules which now
#: report it; none is a regression. They became visible together on
#: 2026-08-30 when ``_render_drc`` began folding board furniture into the
#: DRC model and the plane stitcher started reporting honestly. The
#: ``board_edge_clearance`` and ``connectivity`` numbers are corroborated
#: verbatim by a checkpoint written BEFORE that work landed
#: (``docs/backlog/pcb-engine-plan.md``, "0.390 vs 0.400mm — 10um short";
#: "GND in 3 pieces; VCC3V3 in 2").
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
#: - ``connectivity`` (1, was 2): GND's poured copper is in 3 pieces.
#:   VCC3V3's split is FIXED — its orphan was the same drop via that sat
#:   past the board edge, so placing it legally also put it back in the
#:   pour. GND's is a structural gap, not a tuning one: GND is poured on
#:   ``F.Cu`` ONLY (measured: 4 fragments, all one layer), and a via joins
#:   LAYERS, not lateral gaps — so the stitcher has no second sheet to
#:   detour through and provably cannot close it. Needs the two-via/
#:   spare-layer jumper ``_stitch_one_net`` scopes out. Tracked in
#:   ``docs/backlog/pcb-same-layer-plane-bridge.md``.
#:   ``_stitch_one_net`` adds bridging vias and never removes copper, so it
#:   cannot have caused this; it closes some gaps and reports the rest.
#: - ``silk_missing`` (61): courtyard outlines that cannot be drawn without
#:   landing on a pad. Fixed structurally by
#:   ``docs/backlog/pcb-courtyard-polygon.md``.
#:
#: The assertion below permits EXACTLY these counts. A new rule, or more of
#: any existing one, still fails — the waiver buys silence for known
#: defects, never for new ones. **Lower each number as its item ships**; a
#: stale allowance is indistinguishable from an unnoticed regression.
KNOWN_OPEN_DRC_ERRORS = {
    "connectivity": 1,
    "silk_missing": 61,
}

#: The films this board must produce with geometry on them. Listed
#: explicitly rather than derived from the exporter, because a test that
#: asks the exporter what it exports cannot notice the exporter forgetting
#: something.
#:
#: ``B_Mask``/``B_Paste``/``B_Silkscreen`` are deliberately absent: every
#: part on this fixture is top-side, so those films are legitimately empty
#: and requiring geometry on them would assert a fiction. That they are
#: *written at all* is covered by ``tests/test_pcb_fab_export.py``.
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


def _drain_one_job(store: Store) -> None:
    result = run_job_inproc_pass(store, limit=1)
    assert result["claimed"] == 1, f"expected exactly one queued job, got {result}"
    assert result["failed"] == 0, f"job failed to drain cleanly: {result}"


def test_the_fab_svg_carries_every_film_including_the_declared_ground_planes(
    store: Store,
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
        "enqueued" in pcb.put(id="fabrender", args={"op": "place", "seed": _SEED}).body
    )
    _drain_one_job(store)
    assert (
        "enqueued" in pcb.put(id="fabrender", args={"op": "route", "seed": _SEED}).body
    )
    _drain_one_job(store)

    svg = pcb.get(id="fabrender", view="svg", args={"level": "fab"}).body

    out = os.environ.get("PRECIS_PCB_RENDER_OUT")
    if out:
        Path(out).write_text(svg, encoding="utf-8")

    # **The board this renders must be a board, not just a picture.**
    # This test originally asserted only that every film reached the
    # viewer, and it passed happily over a design carrying 57 DRC errors —
    # 55 of them plane drop-vias sitting on their own pads. A render handed
    # to a human as a deliverable implies the board behind it is sound, so
    # assert that here rather than letting the picture speak for it.
    #
    # That claim is currently WAIVED for a named set of pre-existing engine
    # defects — see `KNOWN_OPEN_DRC_ERRORS`. This board is not manufacturable
    # as rendered; the waiver keeps the test sensitive to NEW breakage while
    # those items are open, and is not a statement that the board is sound.
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
    over_budget = {
        rule: (n, KNOWN_OPEN_DRC_ERRORS.get(rule, 0))
        for rule, n in by_rule.items()
        if n > KNOWN_OPEN_DRC_ERRORS.get(rule, 0)
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
        f"\nfull tally: {dict(by_rule)}\nsee KNOWN_OPEN_DRC_ERRORS -- a rule "
        f"absent from it is allowed ZERO\n{detail}"
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
