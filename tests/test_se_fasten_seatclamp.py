"""The dogfood: a printed seat clamp, bolted, end to end through the real
surfaces (``se-off-the-shelf-fabrication.md`` rungs 2c/3b/3c).

Every other fastening test builds its tree by hand, which is the right
way to test arithmetic and the wrong way to find out whether the *product*
works. This one goes the whole way a person would: mint a countersunk M4
and a nut from the standards registry through `ComponentHandler`, author a
two-part printed assembly through `SeHandler`, and read ``view='fasten'``
— so it covers migration 0163's new specs, the mint, the mm→m boundary,
the catalog derivation, the strategy decision, the stamped features and
the rendered view in one pass.

The geometry is the unicycle's seat clamp in miniature and the numbers are
checkable by hand: a 4 mm rail on a 12 mm clamp block, an M4×12
countersunk screw driving ``+z`` from the rail's top face, a captive nut
in the clamp.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis
import precis_se
from precis.dispatch import Hub
from precis.handlers.component import ComponentHandler
from precis.store import Store
from precis_se.handler import SeHandler

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"
_CORE_MIGRATIONS = Path(precis.__file__).parent / "migrations"


def _ensure_fastener_specs(store: Store) -> None:
    """The same test-DB guard `test_se_catalog_binding.py` documents: on
    the gate's per-worker clones 0093's *category-scoped* specs can be
    absent while the universal ones are present, which would silently
    skip half a mint. Also replays 0163 (head_form/point_type/head_angle/
    drive_code), which a clone made before it exists would not have."""
    with store.pool.connection() as c:
        for name in ("0093_component_kind.sql", "0163_component_head_form_specs.sql"):
            path = _CORE_MIGRATIONS / name
            if not path.exists():  # pragma: no cover — checkout skew
                continue
            if name.startswith("0093"):
                row = c.execute(
                    "SELECT count(*) FROM component_specs "
                    "WHERE status = 'core' AND category_id IS NOT NULL"
                ).fetchone()
                if row is not None and row[0] > 0:
                    continue
            body = path.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    _ensure_fastener_specs(store)
    return SeHandler(hub=hub)


def _mint(hub: Hub, series: str, size: str) -> str:
    """Mint through the real series path and *assert the mint was
    complete* — a partial one surfaces far downstream as a mystifying
    "screw needs length"."""
    resp = ComponentHandler(hub=hub).put(series=series, size=size)
    assert "skipped" not in resp.body, f"incomplete mint: {resp.body}"
    return resp.body.splitlines()[0]


def _seat_clamp(screw_slug: str) -> str:
    """rail 0.004→0.008, clamp 0.008→0.020, screw driving +z from z=0.004.

    Both printed, because that is the case rung 3c exists for: the clamp
    is the terminal member and it is plastic, so the nut trap has to be
    asked for."""
    ops: list[dict[str, Any]] = [
        {
            "op": "add_block",
            "name": "rail",
            "envelope": "box:w0.03d0.02h0.004",
            "pose": [0, 0, 0.004],
        },
        {"op": "set_mode", "block": "rail", "mode": "fdm/asa"},
        {
            "op": "add_block",
            "name": "clamp",
            "envelope": "box:w0.03d0.02h0.012",
            "pose": [0, 0, 0.008],
        },
        {"op": "set_mode", "block": "clamp", "mode": "fdm/asa"},
        {"op": "add_block", "name": "bolt", "pose": [0, 0, 0.004]},
        {"op": "set_mode", "block": "bolt", "mode": "purchase"},
        {
            "op": "set_binding",
            "block": "bolt",
            "kind": "component",
            "design": screw_slug,
        },
        # The connect names the SCREW as one endpoint: a connect between
        # the two members says they are joined, not by what, and every
        # number the pass reports is read off the screw's own pose.
        {"op": "add_port", "block": "bolt", "name": "thread"},
        {"op": "add_port", "block": "clamp", "name": "boss"},
        {
            "op": "connect",
            "a": "bolt.thread",
            "b": "clamp.boss",
            "joint": {
                "class": "rigid",
                "mechanism": "screw",
                "params": {"thread_strategy": "nut-trap"},
            },
        },
    ]
    return json.dumps({"description": "printed seat clamp, bolted", "ops": ops})


@pytest.fixture
def clamped(handler: SeHandler, hub: Hub) -> str:
    slug = _mint(hub, "iso-10642", "M4x12")
    assert "iso-10642-m4x12" in slug
    handler.put(id="seat-clamp", text=_seat_clamp("iso-10642-m4x12"))
    return handler.get(id="seat-clamp", view="fasten").body


class TestTheWholeWayThrough:
    def test_the_stack_is_found_and_measured(self, clamped: str) -> None:
        assert "rail" in clamped and "clamp" in clamped
        assert "grip" in clamped
        # 4 mm of rail clamped against the 12 mm block below it.
        assert "4.0 mm" in clamped or "4 mm" in clamped

    def test_the_view_names_the_far_end_it_was_told_to_use(self, clamped: str) -> None:
        assert "far end: nut-trap into thermoplastic-rigid" in clamped

    def test_a_countersunk_head_cuts_a_cone_in_the_rail(self, clamped: str) -> None:
        assert "countersink" in clamped

    def test_the_clamp_gets_a_hex_pocket_with_an_across_flats(
        self, clamped: str
    ) -> None:
        assert "nut-pocket" in clamped
        assert "across flats" in clamped

    def test_it_says_which_tool_turns_the_screw(self, clamped: str) -> None:
        assert "driven with:" in clamped
        assert "hex key" in clamped

    def test_the_captive_nut_is_demanded_as_a_part(self, clamped: str) -> None:
        """A pocket with nothing in it is a hole."""
        assert "nut_trap_bom" in clamped
        assert "ISO 4032" in clamped

    def test_the_printed_holes_carry_their_provenance(self, clamped: str) -> None:
        assert "printed-hole compensation" in clamped
        assert "fdm/asa" in clamped

    def test_a_hex_socket_screw_draws_no_drive_complaint(self, clamped: str) -> None:
        assert "drive_not_preferred" not in clamped


class TestTheRefusal:
    def test_the_same_design_without_a_strategy_stamps_no_far_end(
        self, handler: SeHandler, hub: Hub
    ) -> None:
        """The rung in one assertion: the clamp is printed, so what the
        screw threads into is a decision, and the pass will not make it."""
        _mint(hub, "iso-10642", "M4x12")
        body = _seat_clamp("iso-10642-m4x12").replace(
            '"params": {"thread_strategy": "nut-trap"}', '"params": {}'
        )
        handler.put(id="seat-clamp-bare", text=body)
        out = handler.get(id="seat-clamp-bare", view="fasten").body
        assert "thread_strategy_undeclared" in out
        assert "far end: UNDECIDED" in out
        # No far-end FEATURE was stamped — checked by feature name
        # (`<subject>#<block>.<kind>`) rather than by the loose word, which
        # also appears in prose.
        assert "#clamp.nut-pocket" not in out
        assert "#clamp.tapped" not in out
        # …and the clearance hole through the rail is still there: the
        # refusal is about the far end, not the whole joint.
        assert "clearance" in out
