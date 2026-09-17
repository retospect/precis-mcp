"""Rung 4 of docs/backlog/se-print-implementer.md: ``view='print'`` (the
fdm process-DRC report + STL/3MF export), ``view='fab'`` (the whole
design's fabrication plan), ``set_build_frame``/``clear_build_frame``,
``MODE_FAMILIES['fdm'].implemented = True``, and the ``drc``/``validate``
pointer lines.

Reuses the seat-clamp fixture (``tests.test_se_fasten_seatclamp`` /
``tests.test_se_print_solid``) and the T-shape fixture
(``tests.test_cad_printability``) — the worked examples se-print-
implementer.md names for this rung's acceptance criteria.

No new migration ships with this rung: ``se_blocks.build_frame jsonb`` has
existed, dark, since migration ``0001_se_kind.sql`` (se-print-
implementer.md's own "Schema" section says so) — this rung is the first
read/write of that column, wired through ``ops.py``/``persist.py`` exactly
like ``process_overrides`` was in rung 1, no ``ALTER TABLE`` needed.

Every design slug in this file is unique **per test** — the shared,
unisolated test DB / ``-n auto`` concurrency rule
(``[[test_db_shared_singleton]]``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.handlers.cad import CadHandler
from precis.handlers.component import ComponentHandler
from precis.store import Store
from precis_se import modes as se_modes
from precis_se import persist
from precis_se import printing as se_printing
from precis_se.handler import SeHandler
from precis_se.ops import SeTree
from tests.test_cad_printability import _T_SHAPE
from tests.test_se_fasten_seatclamp import _ensure_fastener_specs, _seat_clamp

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: The one line every ``put(kind='component', series=..., size=...)``
#: response ends with, whatever the rest of the body says (test_se_print_
#: solid.py's own helper, transferred — a fresh mint's first line is prose,
#: not a bare slug).
_COMPONENT_SLUG_RE = re.compile(r"id='([^']+)'\)\s*$")


def _mint_slug(hub: Hub, series: str, size: str) -> str:
    resp = ComponentHandler(hub=hub).put(series=series, size=size)
    assert "skipped" not in resp.body, f"incomplete mint: {resp.body}"
    match = _COMPONENT_SLUG_RE.search(resp.body)
    assert match is not None, f"could not find the minted slug in: {resp.body}"
    return match.group(1)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    _ensure_fastener_specs(store)
    return SeHandler(hub=hub)


def _load(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def test_fdm_family_is_implemented() -> None:
    assert se_modes.MODE_FAMILIES["fdm"].implemented is True


class TestUnrealizedAndAbstractJoint:
    def test_unrealized_member_reports_unrealized(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print4-unrealized", text=_seat_clamp(screw_slug))
        resp = handler.get(
            id="print4-unrealized", view="print", args={"block": "clamp"}
        )
        assert "unrealized" in resp.body

    def test_abstract_joint_appears_then_clears(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        payload = json.loads(_seat_clamp(screw_slug))
        unbound_ops = [op for op in payload["ops"] if op.get("op") != "set_binding"]
        handler.put(id="print4-abstract", text=json.dumps({"ops": unbound_ops}))
        handler.edit(
            id="print4-abstract",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        resp = handler.get(id="print4-abstract", view="print", args={"block": "clamp"})
        assert "abstract_joint" in resp.body

        handler.edit(
            id="print4-abstract",
            ops=[
                {
                    "op": "set_binding",
                    "block": "bolt",
                    "kind": "component",
                    "design": screw_slug,
                }
            ],
        )
        resp2 = handler.get(id="print4-abstract", view="print", args={"block": "clamp"})
        assert "abstract_joint" not in resp2.body


class TestPrintExport:
    def test_seatclamp_and_hub_export_stl_and_3mf(
        self, handler: SeHandler, hub: Hub, tmp_path: Path
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print4-export", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print4-export",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        stl_path = tmp_path / "clamp.stl"
        resp = handler.get(
            id="print4-export",
            view="print",
            args={"block": "clamp", "fmt": "stl", "path": str(stl_path)},
        )
        assert stl_path.exists()
        assert stl_path.stat().st_size > 0
        assert str(stl_path) in resp.body
        assert "down=" in resp.body

        # the second worked example: a hub-and-spokes fixture, realized
        # via `realize` (se-print-implementer.md names both).
        handler.put(
            id="print4-hub",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "hub",
                            "envelope": "cyl:r0.02h0.01",
                        },
                        {
                            "op": "add_block",
                            "name": "spoke",
                            "envelope": "box:w0.002d0.002h0.05",
                            "pose": [0.03, 0, 0],
                        },
                        {
                            "op": "array_block",
                            "name": "spokes",
                            "template": "spoke",
                            "polar": {"count": 4, "radius": 0.03},
                        },
                    ]
                }
            ),
        )
        handler.edit(
            id="print4-hub",
            ops=[{"op": "realize", "block": "hub", "mode": "fdm/pla"}],
        )
        handler.edit(
            id="print4-hub",
            ops=[{"op": "realize", "block": "spokes", "mode": "fdm/pla"}],
        )
        mf_path = tmp_path / "hub.3mf"
        resp2 = handler.get(
            id="print4-hub",
            view="print",
            args={"block": "hub", "fmt": "3mf", "path": str(mf_path)},
        )
        assert mf_path.exists()
        assert mf_path.stat().st_size > 0
        assert str(mf_path) in resp2.body


class TestBuildFrameProposal:
    def test_t_shape_proposes_stem_up(self, handler: SeHandler, hub: Hub) -> None:
        CadHandler(hub=hub).put(id="print4-tshape", text=_T_SHAPE)
        handler.put(
            id="print4-tshape-se",
            text=json.dumps(
                {
                    "ops": [
                        {"op": "add_block", "name": "part"},
                        {
                            "op": "set_binding",
                            "block": "part",
                            "kind": "cad",
                            "design": "print4-tshape",
                        },
                        {"op": "set_mode", "block": "part", "mode": "fdm/pla"},
                    ]
                }
            ),
        )
        tree = _load(handler, "print4-tshape-se")
        report = se_printing.report_for(tree, "part", cad_store_reader=handler.store)
        assert report is not None
        assert not report.pinned
        assert report.chosen_down is not None
        assert np.allclose(report.chosen_down, [0.0, 0.0, 1.0], atol=1e-6)

    def test_no_fdm_block_is_honest(self, handler: SeHandler) -> None:
        handler.put(
            id="print4-nofdm",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "x",
                            "envelope": "box:w0.01d0.01h0.01",
                        }
                    ]
                }
            ),
        )
        resp = handler.get(id="print4-nofdm", view="print")
        assert "no fdm-mode block" in resp.body


class TestBuildFramePin:
    def test_pin_survives_set_envelope_and_reports_worse_score(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        CadHandler(hub=hub).put(id="print4-tshape-pin", text=_T_SHAPE)
        handler.put(
            id="print4-pin",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "part",
                            "envelope": "box:w0.01d0.01h0.01",
                        },
                        {
                            "op": "set_binding",
                            "block": "part",
                            "kind": "cad",
                            "design": "print4-tshape-pin",
                        },
                        {"op": "set_mode", "block": "part", "mode": "fdm/pla"},
                    ]
                }
            ),
        )
        # pin the worse (stem-down) direction.
        handler.edit(
            id="print4-pin",
            ops=[{"op": "set_build_frame", "block": "part", "down": [0, 0, -1]}],
        )
        tree = _load(handler, "print4-pin")
        assert tree.blocks["part"].build_frame == {
            "down": [0.0, 0.0, -1.0],
            "origin": "user",
        }
        report = se_printing.report_for(tree, "part", cad_store_reader=handler.store)
        assert report is not None
        assert report.pinned
        assert report.chosen_down is not None
        assert np.allclose(report.chosen_down, [0.0, 0.0, -1.0], atol=1e-6)
        assert report.best_other is not None  # the pin is worse than the best

        resp = handler.get(id="print4-pin", view="print", args={"block": "part"})
        assert "pinned" in resp.body
        assert "vs best" in resp.body

        # the pin survives a later set_envelope (slice-4 origin contract).
        handler.edit(
            id="print4-pin",
            ops=[
                {
                    "op": "set_envelope",
                    "block": "part",
                    "envelope": "box:w0.02d0.02h0.02",
                }
            ],
        )
        tree2 = _load(handler, "print4-pin")
        assert tree2.blocks["part"].build_frame == {
            "down": [0.0, 0.0, -1.0],
            "origin": "user",
        }

        # clear_build_frame removes the pin; the search resumes proposing.
        handler.edit(
            id="print4-pin", ops=[{"op": "clear_build_frame", "block": "part"}]
        )
        tree3 = _load(handler, "print4-pin")
        assert tree3.blocks["part"].build_frame is None
        report3 = se_printing.report_for(tree3, "part", cad_store_reader=handler.store)
        assert report3 is not None
        assert not report3.pinned
        assert report3.chosen_down is not None
        assert np.allclose(report3.chosen_down, [0.0, 0.0, 1.0], atol=1e-6)


class TestFab:
    def test_seatclamp_fab_table(self, handler: SeHandler, hub: Hub) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print4-fab", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print4-fab",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        resp = handler.get(id="print4-fab", view="fab")
        assert "clamp" in resp.body
        assert "fdm" in resp.body
        assert "realized" in resp.body
        assert "view='print'" in resp.body
        assert "'fmt': 'stl'" in resp.body
        assert screw_slug in resp.body
        assert "view='bom'" in resp.body
        assert "totals:" in resp.body

    def test_unassigned_block_shows_dash_and_set_mode(self, handler: SeHandler) -> None:
        handler.put(
            id="print4-fab-unassigned",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "x",
                            "envelope": "box:w0.01d0.01h0.01",
                        }
                    ]
                }
            ),
        )
        resp = handler.get(id="print4-fab-unassigned", view="fab")
        assert "—" in resp.body
        assert "set_mode" in resp.body

    def test_array_member_collapses_to_one_row(self, handler: SeHandler) -> None:
        handler.put(
            id="print4-fab-array",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "spoke",
                            "envelope": "box:w0.002d0.002h0.05",
                        },
                        {"op": "set_mode", "block": "spoke", "mode": "fdm/pla"},
                        {
                            "op": "array_block",
                            "name": "spokes",
                            "template": "spoke",
                            "polar": {"count": 4, "radius": 0.05},
                        },
                    ]
                }
            ),
        )
        resp = handler.get(id="print4-fab-array", view="fab")
        # the array node itself never gets its own row — only its template,
        # with qty = its own placement (1) + the array's 4 members.
        assert "spokes" not in resp.body
        assert "spoke\t5\t" in resp.body
        assert len(re.findall(r"\bspoke\t", resp.body)) == 1


class TestPointers:
    def test_drc_shows_fdm_print_check_line(self, handler: SeHandler, hub: Hub) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print4-drc", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print4-drc",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        resp = handler.get(id="print4-drc", view="drc")
        assert "fdm print-check" in resp.body
        assert "clamp" in resp.body
        assert "view='print'" in resp.body

    def test_validate_header_shows_fdm_ratio(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        screw_slug = _mint_slug(hub, "iso-10642", "M4x12")
        handler.put(id="print4-validate", text=_seat_clamp(screw_slug))
        handler.edit(
            id="print4-validate",
            ops=[{"op": "realize", "block": "clamp", "mode": "fdm/pla"}],
        )
        resp = handler.get(id="print4-validate", view="validate")
        assert "fdm block(s) print-checked" in resp.body


class TestBuildFramePersistence:
    def test_pinned_frame_round_trips_through_persist(self, handler: SeHandler) -> None:
        """``se_blocks.build_frame`` has carried this column since
        migration 0001 (dark) — this rung's first write/read of it, no new
        migration needed (module docstring). Round-trips save → load."""
        handler.put(
            id="print4-persist",
            text=json.dumps(
                {
                    "ops": [
                        {
                            "op": "add_block",
                            "name": "part",
                            "envelope": "box:w0.01d0.01h0.01",
                        }
                    ]
                }
            ),
        )
        handler.edit(
            id="print4-persist",
            ops=[{"op": "set_build_frame", "block": "part", "down": [0, 0, -3]}],
        )
        tree = _load(handler, "print4-persist")
        assert tree.blocks["part"].build_frame == {
            "down": [0.0, 0.0, -1.0],
            "origin": "user",
        }
