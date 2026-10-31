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
surface end to end — author, declare the planes, place, render —
and checks that every expected layer group is present in the SVG *and*
carries geometry.

**This fixture never routes.** ``put(op='route')`` is retired
(gr515107, Reto 2026-10-25: the vector router was deleted, tag
``vroute-final``; topo route rides ``op='place'`` with ``placer='topo'``,
``route=True``) and this test does not drive a route — so every film
that only realized copper could fill stays empty: the inner-layer pours
the declared plane nets used to produce (a ``plane_net`` declaration is
still recorded, but no route run realizes it on this fixture), the
``B.Cu`` pour, the tracks and vias, and the PTH drill file (vias were
the only plated holes on this fixture — it carries no mounting holes).
What still carries geometry is asserted non-empty; the rest renders as
present-but-empty groups and is not asserted.

**Ground planes are DECLARED here, not discovered.** ``op='plane_net'`` is
the authored path; the annealer does not choose plane promotion on this
fixture (measured: 79 PLANE_PROMOTE moves proposed over 3000 iterations,
all rejected on cost). The declarations are kept — they are what a human
designing a 4-layer board authors, and they are what the topological
realiser pours from — but with no route run on this fixture they
produce no copper, so there is no pour to assert.

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

#: **The DRC waiver ledger this board used to carry retired with the
#: router** (gr515107, 2026-10-25): every entry in it was a finding over
#: ROUTED copper — pours, drop vias, tracks — and on this all-synthesized
#: board ``view='drc'`` now answers the retirement body instead of a
#: findings table, so there is nothing left to waive. The ledger's last
#: state was all-zero (every entry FIXED — clearance, board_edge_clearance,
#: connectivity, silk_missing; see git history before this commit for the
#: full 11-item dated record, which named the fix for each).

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
#: stays unlisted here. Routing's retirement emptied the rest: the inner
#: pair and ``B_Cu`` carry no copper at all now, and ``PTH`` is not even
#: written (vias were the only plated holes). That every film is
#: *written at all* is covered by ``tests/test_pcb_fab_export.py``.
_EXPECTED_LAYERS = (
    "F_Cu",
    "F_Mask",
    "F_Paste",
    "F_Silkscreen",
    "Edge_Cuts",
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
            with store.pool.connection() as conn:
                meta_row = conn.execute(
                    "SELECT meta FROM refs WHERE ref_id=%s", (job_ref_id,)
                ).fetchone()
            assert meta_row is not None, f"job {job_ref_id} disappeared"
            job_meta = meta_row[0]
            evidence = {
                "job_id": job_ref_id,
                "status": status,
                "meta": job_meta,
                "routes": store.pcb_routes_get(parent_id),
            }
            path = Path(".scratch/r14-route-gate")
            path.mkdir(parents=True, exist_ok=True)
            seed = job_meta["params"].get("seed", 0)
            job_type = job_meta["job_type"]
            (path / f"fab-{job_type}-seed{seed}.json").write_text(
                json.dumps(evidence, indent=2, default=str), encoding="utf-8"
            )
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}; "
                f"error={job_meta.get('error')}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.timeout(600)  # CI junit worst 139s x4 for CI variance (gr503084)
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
    # pair carries traces AND a copper fill in the space between them —
    # nothing realizes the fill on this unrouted fixture (module docstring),
    # but the authored shape is what the topological realiser will read.
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

    svg = pcb.get(id="fabrender", view="svg", args={"level": "fab"}).body

    out = os.environ.get("PRECIS_PCB_RENDER_OUT")
    if out and seed == _RENDER_SEED:
        Path(out).write_text(svg, encoding="utf-8")

    # No route ran on this fixture: no pcb_copper rows exist, so DRC has
    # no real geometry to check — `view='drc'` answers the no-copper body
    # on this all-synthesized board rather than a findings table, and the
    # waiver ledger this test used to hold died with the route leg (see
    # the module record above). Report the route status — every
    # fanout>=2 net reads 'unrouted', the expected state — instead of a
    # findings tally.
    drc_view = pcb.get(id="fabrender", view="drc")
    assert "no copper stored" in drc_view.body
    unrouted = sorted(
        row["name"]
        for row in store.pcb_route_status(ref.id)
        if row["status"] == "unrouted"
    )
    print(f"REPORT seed={seed} unrouted_nets={unrouted}")

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

    # Silk must be real text/outlines, not one stray mark.
    assert groups["F_Silkscreen"].count("<path") > 20, (
        "F_Silkscreen has almost no geometry — refdes labels and part "
        "outlines should produce many strokes across 29 parts"
    )
