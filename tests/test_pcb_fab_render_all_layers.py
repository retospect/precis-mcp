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

    # Declared, not discovered — see the module docstring.
    pcb.put(id="fabrender", args={"op": "plane_net", "layer": "In1.Cu", "net": "GND"})
    pcb.put(
        id="fabrender", args={"op": "plane_net", "layer": "In2.Cu", "net": "VCC3V3"}
    )

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
    for plane_layer in ("In1_Cu", "In2_Cu"):
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
