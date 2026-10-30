"""gr529278 — ``view='validate'`` is wall-clock bounded on field-backed
designs.

An rd-rounded (``needs_field_backend``) printed solid used to make
``view='validate'`` unbounded: ``_fdm_print_status`` →
``se_printing.report_for`` meshed every fdm block at the house
``layer_height`` pitch through the sampled-field backend (narrow-band
marching cubes), then ran the ~130-candidate orientation search — a
pure-Python per-triangle adjacency pass per candidate — over the
resulting mesh. box3-fdm held the call for 7+ minutes.

Now the view runs under one monotonic deadline
(:data:`precis_se.validate.VALIDATE_VIEW_BUDGET_S`): a field-backed solid
meshes at a ``BOUNDED_MESH_CELLS``-capped pitch (noted), and a check the
deadline has passed is reported "not run: budget exceeded" instead of
started.

Every design slug in this file is unique **per test** — the shared,
unisolated test DB / ``-n auto`` concurrency rule
(``[[test_db_shared_singleton]]``).
"""

from __future__ import annotations

import json
import time

import pytest

from precis.cad import bulk as cad_bulk
from precis.cad import printability as cad_printability
from precis.cad.scene import NodeSpec, SceneSpec, build_design
from precis.cad.vec import identity
from precis.dispatch import Hub
from precis.handlers.cad import CadHandler
from precis.store import Store
from precis_se import fasten as se_fasten
from precis_se import handler as se_handler_mod
from precis_se import persist, printsolid
from precis_se import printing as se_printing
from precis_se import validate as se_validate
from precis_se.handler import SeHandler
from precis_se.ops import SeTree

#: A small rd-rounded solid — ``needs_field_backend(spec)`` is True, so
#: the mesh route is the sampled-field backend.
_RBOX = "component p\nbody add box:w40mmd20mmh10mmrd2mm\n"


@pytest.fixture
def handler(store: Store, hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


def _rd_fdm_design(handler: SeHandler, hub: Hub, slug: str) -> None:
    """An se design whose one fdm block binds an rd-rounded cad part."""
    CadHandler(hub=hub).put(id=f"{slug}-cad", text=_RBOX)
    handler.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "part"},
                    {
                        "op": "set_binding",
                        "block": "part",
                        "kind": "cad",
                        "design": f"{slug}-cad",
                    },
                    {"op": "set_mode", "block": "part", "mode": "fdm/pla"},
                ]
            }
        ),
    )


def _load(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def test_validate_returns_on_rd_field_backend_with_coarsen_note(
    handler: SeHandler, hub: Hub
) -> None:
    _rd_fdm_design(handler, hub, "bval-rd")
    resp = handler.get(id="bval-rd", view="validate")
    assert "fdm block(s) print-checked" in resp.body
    # the field-backed solid was judged at the capped pitch, and says so
    assert "coarsened pitch" in resp.body


def test_report_for_budget_skip_reports_not_run(
    handler: SeHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rd_fdm_design(handler, hub, "bval-skip")
    tree = _load(handler, "bval-skip")
    real = se_printing.printed_solid

    def slow(*args, **kwargs):
        time.sleep(0.25)
        return real(*args, **kwargs)

    monkeypatch.setattr(se_printing, "printed_solid", slow)
    report = se_printing.report_for(
        tree, "part", cad_store_reader=handler.store, mesh_checks=False, budget_s=0.05
    )
    assert report is not None
    assert report.checks_ran is False
    assert any("not run: budget exceeded" in n for n in report.check_notes)


def test_validate_view_budget_marks_unfinished_checks(
    handler: SeHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rd_fdm_design(handler, hub, "bval-view")
    real_validate = se_validate.validate

    def slow_validate(*args, **kwargs):
        time.sleep(0.2)
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(se_validate, "VALIDATE_VIEW_BUDGET_S", 0.05)
    monkeypatch.setattr(se_validate, "validate", slow_validate)
    resp = handler.get(id="bval-view", view="validate")
    assert "not run: budget exceeded" in resp.body
    assert "check_not_run" in resp.body
    # the fdm status pass got no budget left either — it says so, and the
    # unchecked block is not counted as print-checked
    assert "0/1 fdm block(s) print-checked" in resp.body


def test_validate_view_notes_partial_orient(
    handler: SeHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline reaches inside the candidate loop: a sweep stopped
    mid-way returns the best-so-far marked partial, the view says how far
    it got, and the block still counts as print-checked."""
    _rd_fdm_design(handler, hub, "bval-orient")
    real_orient = cad_printability.orient

    def partial_orient(mesh, rules, policy, loads=(), **kwargs):
        calls = {"n": 0}

        def stop_after_three() -> bool:
            calls["n"] += 1
            return calls["n"] > 3  # polled before each score — three run

        kwargs["should_stop"] = stop_after_three
        return real_orient(mesh, rules, policy, loads, **kwargs)

    monkeypatch.setattr(cad_printability, "orient", partial_orient)
    resp = handler.get(id="bval-orient", view="validate")
    assert "orient stopped at budget after 3/" in resp.body
    assert "1/1 fdm block(s) print-checked" in resp.body


# ---------------------------------------------------------------------------
# stamped-hole probes honour the same deadline (printsolid._apply_cuts)
# ---------------------------------------------------------------------------


def _rd_design():
    """A 40×20×10 mm rd-rounded box — the field backend whose ray
    quadrature is unbounded-cost."""
    return build_design(
        SceneSpec(
            nodes=[
                NodeSpec(
                    name="p",
                    op="add",
                    config="box:w0.04d0.02h0.01rd0.002",
                    component="p",
                )
            ],
            components=["p"],
        )
    )


def _hole(name: str, x: float) -> se_fasten.Hole:
    """A 4 mm through hole down -z, entering the box's top face."""
    return se_fasten.Hole(
        name=name,
        block="part",
        kind="clearance",
        diameter_m=0.004,
        depth_m=0.01,
        origin=[x, 0.0, 0.01],
        axis=[0.0, 0.0, -1.0],
        through=True,
    )


def test_apply_cuts_budget_marks_unprobed_holes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline reaches inside the per-hole loop: a hole the budget
    never reached is reported "not run: budget exceeded" — and never
    probed."""
    design = _rd_design()
    probed = []
    real_vf = cad_bulk.volume_field

    def spy(*args, **kwargs):
        probed.append(kwargs)
        return real_vf(*args, **kwargs)

    monkeypatch.setattr(cad_bulk, "volume_field", spy)
    holes = [_hole(f"h{i}", x) for i, x in enumerate((-0.01, 0.0, 0.01))]
    findings: list[se_validate.ValidationIssue] = []
    nodes, names, cut_any = printsolid._apply_cuts(
        design,
        "part",
        holes,
        identity(),
        findings,
        field_backend=True,
        deadline=time.monotonic() - 1.0,
    )
    assert not cut_any and nodes == [] and names == []
    assert probed == []
    assert len(findings) == len(holes)
    assert all(
        f.rule == "feature_not_cut" and "not run: budget exceeded" in f.detail
        for f in findings
    )


def test_field_probe_uses_probe_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-hole presence probe on a field backend samples at
    ``PROBE_FIELD_CELLS``, not the 250k ``FIELD_VOLUME_CELLS``."""
    design = _rd_design()
    seen = []
    real_vf = cad_bulk.volume_field

    def spy(*args, **kwargs):
        seen.append(kwargs.get("cells"))
        return real_vf(*args, **kwargs)

    monkeypatch.setattr(cad_bulk, "volume_field", spy)
    findings: list[se_validate.ValidationIssue] = []
    _nodes, names, cut_any = printsolid._apply_cuts(
        design, "part", [_hole("h", 0.0)], identity(), findings, field_backend=True
    )
    assert cut_any and names == ["h"]
    assert seen and all(c == printsolid.PROBE_FIELD_CELLS for c in seen)


def test_printed_solid_rd_holes_under_expired_budget(
    handler: SeHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An rd design carrying several stamped holes: printed_solid returns
    under an already-spent budget, the unprobed holes reported."""
    _rd_fdm_design(handler, hub, "bval-holes")
    tree = _load(handler, "bval-holes")
    holes = [_hole(f"h{i}", x) for i, x in enumerate((-0.01, 0.0, 0.01))]
    monkeypatch.setattr(printsolid.se_fasten, "features_for", lambda *_a, **_k: holes)
    result = printsolid.printed_solid(
        tree, "part", cad_store_reader=handler.store, deadline=time.monotonic()
    )
    assert result is not None
    assert result.features == []
    not_cut = [f for f in result.findings if f.rule == "feature_not_cut"]
    assert len(not_cut) == len(holes)
    assert all("not run: budget exceeded" in f.detail for f in not_cut)


# ---------------------------------------------------------------------------
# view='drc' — one deadline for the whole view, not a fresh 60 s pointer
# ---------------------------------------------------------------------------


def test_drc_view_shares_one_deadline(
    handler: SeHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fdm print-status pass and the validate digest inside
    ``view='drc'`` see the SAME deadline timestamp — opened once at the
    top of the view, so the view's total stays within one budget."""
    _rd_fdm_design(handler, hub, "bval-drc-one")
    seen: dict[str, float | None] = {}
    real_status = SeHandler._fdm_print_status
    real_findings = se_handler_mod.validate_findings

    def status_spy(self, tree, *, deadline=None):
        seen["status"] = deadline
        return real_status(self, tree, deadline=deadline)

    def findings_spy(store, tree, ref_id, load_cases=None, deadline=None):
        seen["findings"] = deadline
        return real_findings(
            store, tree, ref_id, load_cases=load_cases, deadline=deadline
        )

    monkeypatch.setattr(SeHandler, "_fdm_print_status", status_spy)
    monkeypatch.setattr(se_handler_mod, "validate_findings", findings_spy)
    resp = handler.get(id="bval-drc-one", view="drc")
    assert seen["status"] is not None
    assert seen["status"] == seen["findings"]
    assert "fdm print-check" in resp.body
