"""Render ANY pcb design fixture to an SVG a human can peek at.

An env-gated utility lane, not an assertion suite — the acceptance
fixtures (``test_pcb_reference_end_to_end.py``,
``test_pcb_fab_render_all_layers.py``, ``test_pcb_second_reference_
end_to_end.py``) own the baselines; this exists so "author a board, look
at it" doesn't require writing a new test per design. Skipped entirely
unless ``PRECIS_PCB_RENDER_FIXTURE`` is set, so it costs the gate one
skip line.

    PRECIS_PCB_RENDER_FIXTURE=nano_oc_switch.json \\
    PRECIS_PCB_RENDER_OUT=board_nano.svg \\
    PRECIS_PCB_RENDER_SEED=2 \\
    UV_WITH="--with shapely" scripts/test tests/test_pcb_render_fixture.py -q -s

- ``PRECIS_PCB_RENDER_FIXTURE``: fixture filename under
  ``tests/fixtures/pcb/`` (or an absolute path).
- ``PRECIS_PCB_RENDER_OUT``: where the fab-level SVG is written
  (default ``board_fixture.svg`` in the repo root, gitignored via
  ``board*.svg``).
- ``PRECIS_PCB_RENDER_SEED``: the anneal draw (default 1). Seeds differ —
  see the multi-seed fixtures' own docstrings — so re-render a few if
  the first draw looks congested.

The fixture JSON may carry an optional top-level ``"planes"`` key — a
list of ``{"layer": ..., "net": ...}`` entries — declaring copper pours
the same way ``test_pcb_fab_render_all_layers.py`` does inline. Planes
are DECLARED, not discovered (see that file's docstring), so a fixture
authored without one renders with no fill at all. This key is popped
before the design is created (it isn't part of the create-op schema) and
each entry is applied via ``op='plane_net'`` before place/route.

The DRC tally is PRINTED (run with ``-s``), never asserted: a peek
utility that fails on an imperfect board can't show you the imperfection.
"""

from __future__ import annotations

import collections
import json
import os
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors.job_inproc import run_job_inproc_pass

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not os.environ.get("PRECIS_PCB_RENDER_FIXTURE"),
        reason="render utility: set PRECIS_PCB_RENDER_FIXTURE to use",
    ),
]

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pcb"


def _drain_one_job(store: Store) -> None:
    result = run_job_inproc_pass(store, limit=1)
    assert result["claimed"] == 1, f"expected exactly one queued job, got {result}"
    assert result["failed"] == 0, f"job failed to drain cleanly: {result}"


def test_render_the_named_fixture_to_svg(store: Store) -> None:
    name = os.environ["PRECIS_PCB_RENDER_FIXTURE"]
    path = Path(name) if os.path.isabs(name) else _FIXTURE_DIR / name
    with path.open(encoding="utf-8") as fh:
        design: dict[str, Any] = json.load(fh)
    planes = design.pop("planes", None) or []
    seed = int(os.environ.get("PRECIS_PCB_RENDER_SEED", "1"))
    out = Path(os.environ.get("PRECIS_PCB_RENDER_OUT", "board_fixture.svg"))

    pcb = PcbHandler(hub=Hub(store=store))
    assert "created" in pcb.put(id="renderfixture", args=design).body
    for entry in planes:
        resp = pcb.put(
            id="renderfixture",
            args={"op": "plane_net", "layer": entry["layer"], "net": entry["net"]},
        )
        assert "assigned to plane layer" in resp.body
    assert (
        "enqueued"
        in pcb.put(id="renderfixture", args={"op": "place", "seed": seed}).body
    )
    _drain_one_job(store)
    assert (
        "enqueued"
        in pcb.put(id="renderfixture", args={"op": "route", "seed": seed}).body
    )
    _drain_one_job(store)

    svg = pcb.get(id="renderfixture", view="svg", args={"level": "fab"}).body
    out.write_text(svg, encoding="utf-8")

    # DRC is pull-based (see test_pcb_fab_render_all_layers.py's warning):
    # run it so the tally below is a measurement, not a vacuous empty list.
    pcb.get(id="renderfixture", view="drc")
    ref = store.get_ref(kind="pcb", id="renderfixture")
    assert ref is not None
    _run_id, findings = store.pcb_drc_findings_latest(ref.id)
    tally = collections.Counter(
        str(f["rule"]) for f in findings if f["severity"] == "error"
    )
    print(f"\nrendered {path.name} seed={seed} -> {out} ({len(svg)} bytes)")
    print(f"DRC error tally (informational): {dict(tally) or 'CLEAN'}")
