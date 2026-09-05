"""Rung 2b wiring: a `component`-bound block gets its envelope and ports
from the catalog (``se-off-the-shelf-fabrication.md`` engine 1).

:mod:`precis_se.catalog` is unit-tested as pure arithmetic in
``test_se_catalog.py``. These are the *wiring* tests, and they exercise
the part that can actually go wrong: the store round trip, the mm→m
conversion across the ``component``/se unit boundary, and the rule that
the derivation is **recomputed on read, never stored**.

Same fixture shape as ``test_se_bom.py``: the shared test DB template
carries only core migrations, so the plugin's own are seeded directly.
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
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import PortSpec, SeBlock, SeTree, effective_envelope, effective_ports

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"
_CORE_MIGRATIONS = Path(precis.__file__).parent / "migrations"


#: 0093's *category-scoped* core specs — the ones a fastener mint needs.
#: Distinct from 0152's universal geometry specs, which is the whole
#: point of :func:`_ensure_fastener_specs`.
_FASTENER_SPECS = ("thread_size", "thread_pitch", "length", "grade", "drive_type")


def _ensure_fastener_specs(store: Store) -> None:
    """Guarantee 0093's category-scoped spec seed before minting.

    Works around a test-DB defect (see
    ``docs/backlog/component-seed-guard-misses-scoped-specs.md``): on the
    gate's per-worker clones these rows are absent while 0093's
    *universal* specs and 0152's are present, so a series mint silently
    skips four specs and the catalog derivation later reports "screw
    needs length". conftest's ``_ensure_component_seed`` is the intended
    guard and does not reliably reach those clones.

    A fixture asserting its own precondition is legitimate; hiding the
    gap would not be, which is why the mint below still asserts a
    complete write rather than tolerating a partial one."""
    core = _CORE_MIGRATIONS / "0093_component_kind.sql"
    if not core.exists():  # pragma: no cover — checkout predates component
        return
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT count(*) FROM component_specs "
            "WHERE status = 'core' AND category_id IS NOT NULL"
        ).fetchone()
        if row is not None and row[0] > 0:
            return
        body = core.read_text(encoding="utf-8")
        c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    _ensure_fastener_specs(store)
    return SeHandler(hub=hub)


def _bolt(hub: Hub, slug: str = "iso-4762-m6x30") -> str:
    """Mint the M6x30 socket cap through the rung-2a series path — the
    real producer of these spec rows, so the test covers the seam rather
    than a hand-built fixture.

    The assertion is load-bearing, not decoration: every test below
    assumes the mint wrote *all* the dimensions, and a partial mint would
    otherwise surface far downstream as a mystifying "screw needs length"
    from the catalog. Failing here says plainly which half is broken."""
    resp = ComponentHandler(hub=hub).put(id=slug, series="iso-4762", size="M6x30")
    assert "skipped" not in resp.body, f"incomplete mint: {resp.body}"
    return slug


def _design(name: str, slug: str, *, envelope: str | None = None) -> str:
    block: dict[str, Any] = {"op": "add_block", "name": "bolt"}
    if envelope is not None:
        block["envelope"] = envelope
    return json.dumps(
        {
            "description": "one bought bolt",
            "ops": [
                block,
                {"op": "set_mode", "block": "bolt", "mode": "purchase"},
                {
                    "op": "set_binding",
                    "block": "bolt",
                    "kind": "component",
                    "design": slug,
                },
            ],
        }
    )


def _load(handler: SeHandler, store: Store, name: str, body: str) -> SeTree:
    handler.put(id=name, text=body)
    ref = store.get_ref(kind="se", id=name)
    assert ref is not None
    return persist.load_tree(store, ref.id)


class TestDerivedEnvelope:
    def test_a_bound_bolt_gets_its_envelope_from_the_catalog(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        slug = _bolt(hub)
        tree = _load(handler, store, "cat-env", _design("cat-env", slug))
        node = tree.blocks["bolt"]
        assert node.envelope is None  # nothing was authored
        assert effective_envelope(tree, node) == "cyl:r0.005h0.036"

    def test_millimetres_become_metres_across_the_boundary(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        """`component` stores head_diameter=10 (mm); se is metres, so the
        bounding radius must be 0.005 — not 5, and not 10."""
        slug = _bolt(hub, "iso-4762-m6x30-units")
        tree = _load(handler, store, "cat-units", _design("cat-units", slug))
        env = effective_envelope(tree, tree.blocks["bolt"])
        assert env is not None
        assert "r0.005" in env
        assert "e-" not in env

    def test_an_authored_envelope_wins_over_the_catalog(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        """Overriding is legitimate — a part modified after purchase —
        so the catalog must not silently replace what someone drew."""
        slug = _bolt(hub, "iso-4762-m6x30-override")
        tree = _load(
            handler,
            store,
            "cat-override",
            _design("cat-override", slug, envelope="box:w1d1h1"),
        )
        assert effective_envelope(tree, tree.blocks["bolt"]) == "box:w1d1h1"


class TestDerivedPorts:
    def test_a_bound_bolt_gets_catalog_ports(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        """Without these, `connect` cannot attach to a bought part."""
        slug = _bolt(hub, "iso-4762-m6x30-ports")
        tree = _load(handler, store, "cat-ports", _design("cat-ports", slug))
        ports = effective_ports(tree, tree.blocks["bolt"])
        assert set(ports) == {"head", "shank", "thread"}
        assert ports["head"].roles == ["bearing-face"]

    def test_an_authored_port_overrides_only_its_own_name(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        slug = _bolt(hub, "iso-4762-m6x30-merge")
        tree = _load(handler, store, "cat-merge", _design("cat-merge", slug))
        node = tree.blocks["bolt"]
        node.ports["head"] = PortSpec(name="head", roles=["custom"])
        ports = effective_ports(tree, node)
        assert ports["head"].roles == ["custom"]  # mine wins
        assert set(ports) == {"head", "shank", "thread"}  # the rest survive


class TestDerivationIsNotStored:
    def test_the_envelope_column_stays_null(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        """Derived, never stored (the copper-derived rule): a save must
        not write the catalog's answer into se_blocks."""
        slug = _bolt(hub, "iso-4762-m6x30-nostore")
        tree = _load(handler, store, "cat-nostore", _design("cat-nostore", slug))
        assert effective_envelope(tree, tree.blocks["bolt"]) is not None
        ref = store.get_ref(kind="se", id="cat-nostore")
        assert ref is not None
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT envelope FROM se_blocks WHERE ref_id = %s "
                "AND name = 'bolt' AND retired_at IS NULL",
                (ref.id,),
            ).fetchone()
        assert row is not None and row[0] is None

    def test_a_re_saved_design_still_derives_on_the_next_read(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        """save→load→save→load must not lose the derivation, and must not
        smuggle it into the stored row on the way through: ``load_tree``
        hands ``save_tree`` a tree whose blocks already carry ``derived``,
        so a save that read the *effective* envelope rather than the
        authored one would silently promote a derived value to stored."""
        slug = _bolt(hub, "iso-4762-m6x30-resave")
        tree = _load(handler, store, "cat-resave", _design("cat-resave", slug))
        assert tree.blocks["bolt"].derived is not None  # loaded populated
        ref = store.get_ref(kind="se", id="cat-resave")
        assert ref is not None
        persist.save_tree(store, ref_id=ref.id, tree=tree, card_text="one bought bolt")
        again = persist.load_tree(store, ref.id)
        assert again.blocks["bolt"].envelope is None  # still not stored
        assert effective_envelope(again, again.blocks["bolt"]) == "cyl:r0.005h0.036"


class TestDegradesTotally:
    def test_a_dangling_component_slug_does_not_break_the_load(
        self, handler: SeHandler, store: Store
    ) -> None:
        """A load that raised because a catalog row vanished would take
        the whole design with it — the fragility name-keyed identity
        exists to avoid."""
        tree = _load(
            handler, store, "cat-dangling", _design("cat-dangling", "no-such-part")
        )
        node = tree.blocks["bolt"]
        assert effective_envelope(tree, node) is None
        assert node.derived is not None
        assert "not found" in (node.derived.why_not or "")

    def test_a_component_with_no_geometry_specs_reports_why(
        self, handler: SeHandler, store: Store, hub: Hub
    ) -> None:
        ComponentHandler(hub=hub).put(
            id="mystery-widget", title="Mystery widget", category="fastener"
        )
        tree = _load(handler, store, "cat-thin", _design("cat-thin", "mystery-widget"))
        node = tree.blocks["bolt"]
        assert effective_envelope(tree, node) is None
        assert "head_diameter" in (node.derived.why_not or "")

    def test_a_store_without_the_component_ops_is_a_no_op(self) -> None:
        """Plugin tests run against fakes that never grew the component
        surface; the pass must skip rather than demand it."""
        tree = SeTree()
        tree.blocks["bolt"] = SeBlock(
            name="bolt", bound_kind="component", bound="whatever"
        )
        persist.attach_catalog(object(), tree)
        assert tree.blocks["bolt"].derived is None

    def test_a_non_component_binding_is_left_alone(
        self, handler: SeHandler, store: Store
    ) -> None:
        tree = SeTree()
        tree.blocks["part"] = SeBlock(name="part", bound_kind="cad", bound="some-cad")
        persist.attach_catalog(store, tree)
        assert tree.blocks["part"].derived is None
