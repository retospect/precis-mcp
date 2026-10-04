"""The measures substrate (migration 0187, docs/backlog/measures-substrate.md,
pilot Build A). Each test names the acceptance criterion or "Done when" item
it pins. Real PG (the ``store`` fixture); the two migration tests replay the
chain up to 0186, insert legacy rows, then apply 0187.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis.errors import BadInput
from precis.store import Migrator, Store
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.taxonomy.nodes import slugify, validate_taxon_meta
from tests.workers._helpers import seed_chunk, seed_ref

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"

FE_TEXT = (
    "The Faradaic efficiency reached 95% at -0.5 V vs RHE, and 61% at -0.9 V vs RHE "
    "for the Cu NWA sample. NH3 yield rate was 10 µg h⁻¹ cm⁻²."
)


# ── builders ───────────────────────────────────────────────────────────────


def _taxon(
    store: Store,
    name: str,
    *,
    unit: str | None = None,
    required: list[str] | None = None,
    parent: int | None = None,
    condition: str | None = None,
) -> int:
    meta: dict[str, Any] = {
        "name": name,
        "norm_name": name.lower(),
        "slug": slugify(name),
        "definition": f"{name}.",
        "aliases": [],
        "status": "proposed",
    }
    if unit is not None:
        meta["canonical_unit"] = unit
    if required is not None:
        meta["required_conditions"] = required
    if condition is not None:
        meta["condition"] = condition
    ref = store.insert_ref(kind="taxon", slug=None, title=name, meta=meta)
    if parent is not None:
        store.add_link(src_ref_id=ref.id, dst_ref_id=parent, relation="specialises")
    return ref.id


@pytest.fixture
def world(store: Store) -> dict[str, Any]:
    """A measurand tree (start node -> FE / yield / potential / product) and
    one paper with a chunk of printed numbers."""
    start = _taxon(store, "measurand")
    paper = seed_ref(store, title="a NORR paper", kind="paper")
    chunk = seed_chunk(store, ref_id=paper, text=FE_TEXT)
    w: dict[str, Any] = {"start": start, "paper": paper, "chunk": chunk}
    w["fe"] = _taxon(
        store,
        "Faradaic efficiency",
        unit="%",
        required=["product", "potential"],
        parent=start,
    )
    w["yield_geo"] = _taxon(
        store,
        "product yield rate per geometric area",
        unit="mol s⁻¹ m⁻²",
        required=["product"],
        parent=start,
    )
    w["yield_cat"] = _taxon(
        store,
        "product yield rate per catalyst mass",
        unit="mol s⁻¹ kg⁻¹",
        required=["product"],
        parent=start,
    )
    w["potential"] = _taxon(store, "potential", unit="V", parent=start)
    w["temperature"] = _taxon(store, "temperature", unit="K", parent=start)
    w["product"] = _taxon(store, "reaction product", parent=start)
    w["energy"] = _taxon(store, "adsorption energy", unit="eV", parent=start)
    return w


def _anchor(
    w: dict[str, Any], span: Any = "s1", scheme: str = "sentence"
) -> MeasureAnchor:
    return MeasureAnchor(w["paper"], w["chunk"], scheme, span)


def _product(w: dict[str, Any], formula: str = "NH3") -> MeasureSpec:
    return MeasureSpec(
        measurand_ref_id=w["product"],
        literal=formula,
        subject_ref_id=w["paper"],
        value_text=formula,
        measurand_status="explicit",
        meta={"condition": "product"},
    )


def _potential(w: dict[str, Any], literal: str, **kw: Any) -> MeasureSpec:
    return MeasureSpec(
        measurand_ref_id=w["potential"],
        literal=literal,
        subject_ref_id=w["paper"],
        reported_unit="V",
        reference="RHE",
        **kw,
    )


def _row(store: Store, measure_id: int) -> dict[str, Any]:
    (r,) = [
        m
        for m in store.measures_for(
            _subject_of(store, measure_id), include_superseded=True
        )
        if m["id"] == measure_id
    ]
    return r


def _subject_of(store: Store, measure_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT subject_ref_id FROM measures WHERE id = %s", (measure_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def _update(store: Store, sql: str, params: tuple[Any, ...]) -> None:
    with store.pool.connection() as conn:
        conn.execute(sql, params)


# ── AC 1: compatibility views over measures ────────────────────────────────


class TestCompatViews:
    def test_material_insert_lands_in_measures_and_the_view(self, store: Store) -> None:
        mat = seed_ref(store, title="Al 6061", kind="material")
        mid = store.material_value_insert(
            material_ref_id=mat,
            property_id="density",
            value_num=2700.0,
            maturity="commercial",
            method="datasheet",
            set_by="agent",
            notes="n",
        )
        (view,) = store.material_values_for_ref(mat)
        assert view["id"] == mid and view["property_id"] == "density"
        assert view["value_num"] == 2700.0 and view["set_by"] == "agent"
        assert view["method"] == "datasheet" and view["maturity"] == "commercial"
        m = _row(store, mid)
        assert m["subject_ref_id"] == mat
        assert (m["literal"], m["value_form"]) == ("2700", "point")
        assert m["actor"] == "agent" and m["run_key"] == f"legacy:{mid}"
        assert m["direction"] == "output" and m["measurand"] == "Density"

    def test_legacy_null_set_by_is_actor_legacy_and_reads_back_null(
        self, store: Store
    ) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        mid = store.material_value_insert(
            material_ref_id=mat, property_id="density", value_num=1.0
        )
        assert _row(store, mid)["actor"] == "legacy"
        assert store.material_values_for_ref(mat)[0]["set_by"] is None

    def test_literal_rule_covers_text_range_and_bool(self, store: Store) -> None:
        with store.pool.connection() as conn:
            got = conn.execute(
                "SELECT precis_measure_literal(NULL, NULL, NULL, 'Cordierite', NULL), "
                "       precis_measure_literal(NULL, 550, 575, NULL, NULL), "
                "       precis_measure_literal(NULL, NULL, NULL, NULL, true), "
                "       precis_measure_literal(9.6, NULL, NULL, NULL, NULL)"
            ).fetchone()
        assert got == ("Cordierite", "550–575", "true", "9.6")

    def test_component_insert_folds_into_measures(self, store: Store) -> None:
        comp = seed_ref(store, title="bolt", kind="component")
        cid = store.component_value_insert(
            component_ref_id=comp, spec_id="mass", value_num=0.012, set_by="agent"
        )
        (view,) = store.component_values_for_ref(comp)
        assert view["id"] == cid and view["spec_id"] == "mass"
        m = _row(store, cid)
        assert m["subject_ref_id"] == comp and m["measurand"] == "Mass"
        current = store.component_current_spec_value(comp, "mass")
        assert current is not None and current["value_num"] == 0.012

    def test_views_show_live_rows_only(self, store: Store) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        old = store.material_value_insert(
            material_ref_id=mat, property_id="density", value_num=1.0
        )
        new = store.material_value_insert(
            material_ref_id=mat, property_id="density", value_num=2.0
        )
        _update(
            store,
            "UPDATE measures SET superseded_by = %s, superseded_at = now() WHERE id = %s",
            (new, old),
        )
        assert [v["id"] for v in store.material_values_for_ref(mat)] == [new]

    def test_no_update_or_delete_door_on_the_views(self, store: Store) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        store.material_value_insert(
            material_ref_id=mat, property_id="density", value_num=1.0
        )
        with pytest.raises(psycopg.Error):
            _update(store, "UPDATE material_values SET value_num = 2", ())
        with pytest.raises(psycopg.Error):
            _update(store, "DELETE FROM component_spec_values", ())


def _chain_dir_without_0187(tmp: Path) -> None:
    for f in MIGRATIONS_DIR.glob("*.sql"):
        if f.name >= "0187":
            continue
        shutil.copy(f, tmp / f.name)


def _revision_trigger_oids(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT tgname, oid FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid IN ('refs'::regclass, 'links'::regclass, 'chunks'::regclass) "
            "AND tgname LIKE '%revision%' ORDER BY tgname"
        ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def _legacy_world(dsn: str) -> dict[str, int]:
    """Replay the chain to 0186 and write legacy rows through the old tables."""
    tmp = Path(tempfile.mkdtemp(prefix="precis_test_0187_"))
    try:
        _chain_dir_without_0187(tmp)
        Migrator(dsn, tmp).apply_all()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for key, kind, title in (
            ("mat", "material", "Al"),
            ("comp", "component", "bolt"),
        ):
            row = conn.execute(
                "INSERT INTO refs (kind, title) VALUES (%s, %s) RETURNING ref_id",
                (kind, title),
            ).fetchone()
            assert row is not None
            out[key] = int(row[0])
        conn.execute(
            "INSERT INTO material_values (material_ref_id, property_id, value_num, "
            " set_by, method) VALUES (%s, 'density', 2700, 'agent', 'datasheet')",
            (out["mat"],),
        )
        conn.execute(
            "INSERT INTO material_values (material_ref_id, property_id, value_low, "
            " value_high) VALUES (%s, 'elongation_at_break', 10, 12)",
            (out["mat"],),
        )
        conn.execute(
            "INSERT INTO material_values (material_ref_id, property_id, value_text) "
            "VALUES (%s, 'density', 'dense')",
            (out["mat"],),
        )
        conn.execute(
            "INSERT INTO component_spec_values (component_ref_id, spec_id, value_num, "
            " set_by) VALUES (%s, 'mass', 0.012, 'agent')",
            (out["comp"],),
        )
    return out


def test_migration_backfills_legacy_rows_and_folds_component(fresh_db: str) -> None:
    ids = _legacy_world(fresh_db)
    before = _snapshot(fresh_db)
    triggers_before = _revision_trigger_oids(fresh_db)
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    # 0187's UPDATE kinds (taxon gains required_conditions) fires 0185's refresh;
    # the covered list is unchanged for taxon's WHEN, so no trigger is rebuilt
    assert triggers_before and _revision_trigger_oids(fresh_db) == triggers_before
    with psycopg.connect(fresh_db) as conn:
        measures = conn.execute(
            "SELECT m.id, m.subject_ref_id, m.literal, m.value_form, m.actor, "
            "       m.run_key, t.meta -> 'legacy_source' ->> 'key' "
            "FROM measures m JOIN refs t ON t.ref_id = m.measurand_ref_id ORDER BY m.id"
        ).fetchall()
        view = conn.execute(
            "SELECT id, material_ref_id, property_id, value_num, value_low, "
            "       value_high, value_text, set_by FROM material_values ORDER BY id"
        ).fetchall()
        cview = conn.execute(
            "SELECT component_ref_id, spec_id, value_num, set_by "
            "FROM component_spec_values"
        ).fetchall()
        kind = conn.execute(
            "SELECT relkind FROM pg_class WHERE relname = 'component_spec_values'"
        ).fetchone()
    assert len(measures) == 4  # 3 material + 1 component, nothing lost
    by_key = {(m[6], m[2]): m for m in measures}
    assert by_key[("density", "2700")][3:5] == ("point", "agent")
    assert by_key[("elongation_at_break", "10–12")][3] == "interval"
    assert by_key[("density", "dense")][3] == "categorical"
    comp = by_key[("mass", "0.012")]
    assert comp[1] == ids["comp"] and comp[5].startswith("legacy:component:")
    assert all(m[5].startswith("legacy:") for m in measures)
    # identical values through the view, as before the migration
    assert [tuple(r) for r in view] == [tuple(r) for r in before["material"]]
    assert cview == [(ids["comp"], "mass", 0.012, "agent")]
    assert kind == ("v",)


def test_migration_mints_a_taxon_for_a_property_with_no_taxon(fresh_db: str) -> None:
    ids = _legacy_world(fresh_db)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO material_properties (prop_id, name, canonical_unit, "
            " dimension, value_type, status) VALUES "
            "('zz_orphan', 'Orphan', 'K', 'temperature', 'quantity', 'proposed')"
        )
        conn.execute(
            "INSERT INTO material_values (material_ref_id, property_id, value_num) "
            "VALUES (%s, 'zz_orphan', 1)",
            (ids["mat"],),
        )
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT t.title, t.meta ->> 'canonical_unit', m.literal "
            "FROM measures m JOIN refs t ON t.ref_id = m.measurand_ref_id "
            "WHERE t.meta -> 'legacy_source' ->> 'key' = 'zz_orphan'"
        ).fetchone()
        n_taxa = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'taxon' "
            "AND meta -> 'legacy_source' ->> 'key' = 'zz_orphan'"
        ).fetchone()
        view = conn.execute(
            "SELECT property_id FROM material_values WHERE property_id = 'zz_orphan'"
        ).fetchone()
    assert row == ("Orphan", "K", "1")
    assert n_taxa == (1,) and view == ("zz_orphan",)


def _snapshot(dsn: str) -> dict[str, list[tuple[Any, ...]]]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT id, material_ref_id, property_id, value_num, value_low, "
            "       value_high, value_text, set_by FROM material_values ORDER BY id"
        ).fetchall()
    return {"material": [tuple(r) for r in rows]}


# ── AC 2: the anchored-edge guard ──────────────────────────────────────────


class TestGuards:
    def test_measured_without_anchor_is_refused_naming_the_rule(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        spec = MeasureSpec(
            world["energy"],
            "-0.42",
            world["paper"],
            reported_unit="eV",
            tier="measured",
        )
        with pytest.raises(BadInput, match="anchored primary edge"):
            store.insert_measure(spec, actor="reader")
        assert store.measures_for(world["paper"]) == []

    def test_measured_with_an_anchor_lands_with_row_anchor_and_shared_edge(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        spec = MeasureSpec(
            world["energy"],
            "-0.42",
            world["paper"],
            reported_unit="eV",
            tier="measured",
            source_attribution="own_work",
            anchor=_anchor(world, "FE95", "sentence"),
        )
        run = store.insert_measure(spec, actor="reader", model="m1")
        row = _row(store, run.output_id)
        assert row["tier"] == "measured" and row["primary_link_id"] is not None
        (link,) = store.links_for(
            world["paper"], direction="out", relation="quantifies"
        )
        assert link.id == row["primary_link_id"]
        assert link.dst_ref_id == world["energy"] and link.meta == {}
        assert (row["anchor_scheme"], row["span"]) == ("sentence", "FE95")

    def test_cited_work_is_refused_as_measured_and_lands_asserted(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        kw: dict[str, Any] = dict(
            reported_unit="eV",
            source_attribution="cited_work",
            anchor=_anchor(world),
        )
        with pytest.raises(BadInput, match="cited_work"):
            store.insert_measure(
                MeasureSpec(
                    world["energy"], "-0.4", world["paper"], tier="measured", **kw
                ),
                actor="reader",
            )
        run = store.insert_measure(
            MeasureSpec(world["energy"], "-0.4", world["paper"], tier="asserted", **kw),
            actor="reader",
        )
        assert _row(store, run.output_id)["tier"] == "asserted"

    def test_unit_with_no_dimension_match_is_refused_naming_both(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        spec = MeasureSpec(world["potential"], "5", world["paper"], reported_unit="mA")
        with pytest.raises(BadInput) as err:
            store.insert_measure(spec, actor="reader")
        assert "'mA'" in str(err.value) and "'V'" in str(err.value)

    def test_unparseable_unit_is_refused(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        spec = MeasureSpec(
            world["potential"], "5", world["paper"], reported_unit="blorp"
        )
        with pytest.raises(BadInput, match="blorp"):
            store.insert_measure(spec, actor="reader")

    def test_a_run_needs_an_actor(self, store: Store, world: dict[str, Any]) -> None:
        with pytest.raises(BadInput, match="actor"):
            store.insert_measure(
                MeasureSpec(world["energy"], "1", world["paper"]), actor=" "
            )

    def test_measurand_must_be_a_live_taxon(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        with pytest.raises(BadInput, match="not a live taxon"):
            store.insert_measure(
                MeasureSpec(world["paper"], "1", world["paper"]), actor="reader"
            )


# ── AC 3: round trips of the hand-built fixture rows ───────────────────────


class TestRoundTrips:
    @pytest.mark.parametrize(
        ("literal", "form", "num", "low", "high", "err"),
        [
            ("9.6 ± 1.7", "point", 9.6, None, None, 1.7),
            ("<1", "upper_bound", 1.0, None, None, None),
            (">3", "lower_bound", 3.0, None, None, None),
            ("550–575", "interval", None, 550.0, 575.0, None),
            ("~12", "approximate_point", 12.0, None, None, None),
            ("4.2", "point", 4.2, None, None, None),
        ],
    )
    def test_numeric_literal_forms(
        self,
        store: Store,
        world: dict[str, Any],
        literal: str,
        form: str,
        num: float | None,
        low: float | None,
        high: float | None,
        err: float | None,
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(world["energy"], literal, world["paper"], reported_unit="eV"),
            actor="reader",
        )
        r = _row(store, run.output_id)
        assert r["literal"] == literal and r["value_form"] == form
        assert (r["value_num"], r["value_low"], r["value_high"], r["value_err"]) == (
            num,
            low,
            high,
            err,
        )

    def test_categorical_row(self, store: Store, world: dict[str, Any]) -> None:
        run = store.insert_measure(
            MeasureSpec(world["energy"], "Cordierite", world["paper"]), actor="reader"
        )
        r = _row(store, run.output_id)
        assert (r["value_form"], r["value_text"], r["value_num"]) == (
            "categorical",
            "Cordierite",
            None,
        )

    def test_null_tier_stays_null(self, store: Store, world: dict[str, Any]) -> None:
        run = store.insert_measure(
            MeasureSpec(world["energy"], "1.0", world["paper"]), actor="reader"
        )
        assert _row(store, run.output_id)["tier"] is None

    def test_table_recipe_row_has_no_reported_unit(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(world["energy"], "0.3", world["paper"], reported_unit=None),
            actor="reader",
        )
        r = _row(store, run.output_id)
        assert r["reported_unit"] is None and r["value_num"] == 0.3

    def test_two_results_same_measurand_different_subject_labels(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        for label, lit in (("Cu NWA", "95"), ("Cu foil", "40")):
            store.insert_measure(
                MeasureSpec(
                    world["fe"], lit, world["paper"], reported_unit="%", subject=label
                ),
                actor="reader",
            )
        rows = [
            m for m in store.measures_for(world["paper"]) if m["direction"] == "output"
        ]
        assert sorted((m["subject"], m["value_num"]) for m in rows) == [
            ("Cu NWA", 95.0),
            ("Cu foil", 40.0),
        ]

    def test_ambiguous_measurand_status_is_stored(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(
                world["energy"], "1", world["paper"], measurand_status="ambiguous"
            ),
            actor="reader",
        )
        assert _row(store, run.output_id)["measurand_status"] == "ambiguous"

    @pytest.mark.parametrize(
        ("scheme", "span"),
        [
            ("sentence", "6132.4"),
            ("sentence_range", "6132.4-6"),
            ("numeric_atom", "6132#3"),
            ("offsets", [7, 4, 20]),
        ],
    )
    def test_one_row_anchor_per_span_form(
        self, store: Store, world: dict[str, Any], scheme: str, span: Any
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(
                world["energy"],
                "1",
                world["paper"],
                tier="measured",
                anchor=_anchor(world, span, scheme),
            ),
            actor="reader",
        )
        row = _row(store, run.output_id)
        assert (row["anchor_scheme"], row["span"]) == (scheme, span)
        (link,) = store.links_for(
            world["paper"], direction="out", relation="quantifies"
        )
        assert link.id == row["primary_link_id"]

    def test_preparation_condition_does_not_satisfy_a_required_context_one(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        temp_req = _taxon(
            store,
            "annealing response",
            unit="%",
            required=["temperature"],
            parent=world["start"],
        )
        out = MeasureSpec(temp_req, "5", world["paper"], reported_unit="%")
        temp = MeasureSpec(
            world["temperature"], "773", world["paper"], reported_unit="K"
        )
        prep = store.insert_measure(
            out, [_with(temp, role="preparation")], actor="reader"
        )
        ctx = store.insert_measure(out, [_with(temp, role="context")], actor="reader")
        assert _flags(store, prep.output_id) == ["temperature"]
        assert _flags(store, ctx.output_id) == []


def _with(spec: MeasureSpec, **kw: Any) -> MeasureSpec:
    from dataclasses import replace

    return replace(spec, **kw)


def _flags(store: Store, measure_id: int) -> list[str]:
    esc = _row(store, measure_id)["meta"].get("escalation") or []
    return [e["condition"] for e in esc if e["rule"] == "required_condition_missing"]


# ── AC 4: two measures on one chunk keep their own spans ───────────────────


def test_same_chunk_different_spans_share_one_edge_and_keep_their_own_span(
    store: Store, world: dict[str, Any]
) -> None:
    rows = []
    for span in ("s1", "s2"):
        run = store.insert_measure(
            MeasureSpec(
                world["energy"],
                "1",
                world["paper"],
                tier="measured",
                anchor=_anchor(world, span),
            ),
            actor="reader",
        )
        rows.append(_row(store, run.output_id))
    assert rows[0]["primary_link_id"] == rows[1]["primary_link_id"]
    assert [r["span"] for r in rows] == ["s1", "s2"]
    assert (
        len(store.links_for(world["paper"], direction="out", relation="quantifies"))
        == 1
    )


def test_extra_anchors_get_their_own_edges_listed_in_meta(
    store: Store, world: dict[str, Any]
) -> None:
    other = seed_chunk(store, ref_id=world["paper"], text="second chunk", ord=1)
    run = store.insert_measure(
        MeasureSpec(
            world["energy"],
            "1",
            world["paper"],
            tier="measured",
            anchor=_anchor(world, "s1"),
            extra_anchors=[MeasureAnchor(world["paper"], other, "sentence", "s9")],
        ),
        actor="reader",
    )
    row = _row(store, run.output_id)
    (extra,) = row["meta"]["extra_anchors"]
    assert extra["link_id"] != row["primary_link_id"]
    assert (extra["anchor_scheme"], extra["span"]) == ("sentence", "s9")
    assert (
        len(store.links_for(world["paper"], direction="out", relation="quantifies"))
        == 2
    )


# ── runs, flags and conversions (pilot "Done when") ────────────────────────


class TestRuns:
    def test_two_potentials_in_one_finding_land_as_two_runs(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        runs = [
            store.insert_measure(
                MeasureSpec(
                    world["fe"],
                    lit,
                    world["paper"],
                    reported_unit="%",
                    subject_group="fi123",
                ),
                [_product(world), _potential(world, pot)],
                actor="reader",
            )
            for lit, pot in (("95", "-0.5"), ("61", "-0.9"))
        ]
        assert runs[0].run_key != runs[1].run_key
        by_run: dict[str, list[dict[str, Any]]] = {}
        for m in store.measures_for(world["paper"]):
            by_run.setdefault(m["run_key"], []).append(m)
        assert len(by_run) == 2
        for run, (fe, pot) in zip(runs, ((95.0, -0.5), (61.0, -0.9)), strict=True):
            rows = by_run[run.run_key]
            assert [r["direction"] for r in rows] == ["output", "input", "input"]
            assert rows[0]["value_num"] == fe
            (potential,) = [r for r in rows if r["measurand"] == "potential"]
            assert potential["value_num"] == pot and potential["reference"] == "RHE"
            assert {r["subject_group"] for r in rows if r["direction"] == "output"} == {
                "fi123"
            }
            assert rows[0]["meta"].get("escalation") is None  # both required present

    def test_missing_required_condition_flags_and_never_refuses(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(world["fe"], "52", world["paper"], reported_unit="%"),
            [_product(world)],
            actor="reader",
        )
        row = _row(store, run.output_id)
        assert row["value_num"] == 52.0  # landed
        assert [e["condition"] for e in row["meta"]["escalation"]] == ["potential"]

    def test_required_conditions_inherit_along_specialises(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        parent = _taxon(
            store, "rate", unit="%", required=["product"], parent=world["start"]
        )
        child = _taxon(
            store, "narrow rate", unit="%", required=["potential"], parent=parent
        )
        run = store.insert_measure(
            MeasureSpec(child, "1", world["paper"], reported_unit="%"), actor="reader"
        )
        assert sorted(_flags(store, run.output_id)) == ["potential", "product"]

    def test_ug_per_h_per_cm2_nh3_yield_converts_through_molar_mass(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(
                world["yield_geo"],
                "10 ± 2",
                world["paper"],
                reported_unit="µg h⁻¹ cm⁻²",
                tier="measured",
                anchor=_anchor(world, [0, 70, 105], "offsets"),
            ),
            [_product(world, "NH3")],
            actor="reader",
        )
        r = _row(store, run.output_id)
        # 10 µg h⁻¹ cm⁻² / 17.031 g mol⁻¹ = 10e-6/17.031/3600*1e4 mol s⁻¹ m⁻²
        expected = 10e-6 / 17.031 / 3600 * 1e4
        assert r["value_num"] == pytest.approx(expected, rel=1e-4)
        assert r["value_err"] == pytest.approx(expected * 0.2, rel=1e-4)
        assert r["reported_unit"] == "µg h⁻¹ cm⁻²" and r["literal"] == "10 ± 2"
        assert r["meta"]["conversion"]["molar_mass_g_mol"] == pytest.approx(
            17.031, abs=1e-3
        )
        assert r["meta"].get("escalation") is None

    def test_mol_rate_converts_without_a_formula(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(
                world["yield_geo"], "3", world["paper"], reported_unit="mol h⁻¹ cm⁻²"
            ),
            [_product(world, "NH3")],
            actor="reader",
        )
        assert _row(store, run.output_id)["value_num"] == pytest.approx(3 / 3600 * 1e4)

    def test_mass_rate_with_no_product_formula_flags_the_row(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(
                world["yield_geo"], "10", world["paper"], reported_unit="µg h⁻¹ cm⁻²"
            ),
            actor="reader",
        )
        r = _row(store, run.output_id)
        assert r["value_num"] is None and r["value_form"] == "not_established"
        assert r["literal"] == "10"
        assert "no_molar_mass" in [e["rule"] for e in r["meta"]["escalation"]]

    def test_basis_label_moves_to_normalization(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(
                world["yield_cat"],
                "9.25",
                world["paper"],
                reported_unit="mg h⁻¹ mg_cat⁻¹",
            ),
            [_product(world, "NH3")],
            actor="reader",
        )
        r = _row(store, run.output_id)
        assert (r["normalization"], r["normalization_status"]) == (
            "per catalyst mass",
            "explicit",
        )
        # 9.25 mg NH3 / mg_cat / h -> mol s⁻¹ kg⁻¹
        assert r["value_num"] == pytest.approx(9.25 / 17.031 / 3600 * 1e3, rel=1e-3)

    def test_prefix_scaling_for_a_plain_unit(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(world["energy"], "150", world["paper"], reported_unit="meV"),
            actor="reader",
        )
        assert _row(store, run.output_id)["value_num"] == pytest.approx(0.15)

    def test_extraction_status_is_computed_from_the_span(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        hit = store.insert_measure(
            MeasureSpec(
                world["fe"],
                "95%",
                world["paper"],
                value_num=95.0,
                anchor=_anchor(world, [0, 0, len(FE_TEXT)], "offsets"),
            ),
            actor="reader",
        )
        wrong = store.insert_measure(
            MeasureSpec(
                world["fe"],
                "88%",
                world["paper"],
                value_num=88.0,
                anchor=_anchor(world, "s2"),
            ),
            actor="reader",
        )
        no_anchor = store.insert_measure(
            MeasureSpec(world["fe"], "5", world["paper"]), actor="reader"
        )
        assert _row(store, hit.output_id)["extraction_status"] == "anchor_matched"
        assert _row(store, wrong.output_id)["extraction_status"] == "anchor_mismatch"
        assert _row(store, no_anchor.output_id)["extraction_status"] == "unverified"

    def test_offset_span_narrows_the_match_to_its_slice(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        # "61%" is in the chunk but outside the sliced span -> mismatch
        run = store.insert_measure(
            MeasureSpec(
                world["fe"],
                "61%",
                world["paper"],
                value_num=61.0,
                anchor=_anchor(world, [0, 0, 20], "offsets"),
            ),
            actor="reader",
        )
        assert _row(store, run.output_id)["extraction_status"] == "anchor_mismatch"

    def test_supersede_writes_a_pointer_and_hides_the_old_row(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        old = store.insert_measure(
            MeasureSpec(world["energy"], "1.0", world["paper"]), actor="reader"
        )
        new = store.insert_measure(
            MeasureSpec(
                world["energy"], "1.1", world["paper"], supersedes=old.output_id
            ),
            actor="reader",
        )
        assert [m["id"] for m in store.measures_for(world["paper"])] == [new.output_id]
        both = store.measures_for(world["paper"], include_superseded=True)
        by_id = {m["id"]: m for m in both}
        assert by_id[old.output_id]["superseded_by"] == new.output_id
        assert by_id[new.output_id]["supersedes"] == old.output_id
        with pytest.raises(BadInput, match="already superseded"):
            store.insert_measure(
                MeasureSpec(
                    world["energy"], "1.2", world["paper"], supersedes=old.output_id
                ),
                actor="reader",
            )

    def test_a_failed_run_writes_nothing(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        bad_input = MeasureSpec(
            world["potential"], "5", world["paper"], reported_unit="mA"
        )
        with pytest.raises(BadInput):
            store.insert_measure(
                MeasureSpec(
                    world["energy"],
                    "1",
                    world["paper"],
                    tier="measured",
                    anchor=_anchor(world),
                ),
                [bad_input],
                actor="reader",
            )
        assert store.measures_for(world["paper"]) == []
        assert (
            store.links_for(world["paper"], direction="out", relation="quantifies")
            == []
        )


# ── append-only + the reviews ledger ───────────────────────────────────────


class TestAppendOnlyAndReviews:
    def _measure(self, store: Store, w: dict[str, Any]) -> int:
        run = store.insert_measure(
            MeasureSpec(
                w["fe"],
                "95",
                w["paper"],
                reported_unit="%",
                tier="measured",
                anchor=_anchor(w, [0, 0, len(FE_TEXT)], "offsets"),
            ),
            [_product(w), _potential(w, "-0.5")],
            actor="reader",
            model="m1",
        )
        return run.output_id

    def test_reviewed_measure_round_trips_as_current(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measure(store, world)
        assert store.target_sha("measure", mid)
        store.record_target_review(
            "measure",
            mid,
            actor="checker",
            model="big-model",
            version="v2",
            verdict="approved",
        )
        (rev,) = store.reviews_for("measure", mid)
        assert (rev.verdict, rev.model, rev.current) == ("approved", "big-model", True)

    @pytest.mark.parametrize(
        ("column", "value"),
        [
            ("literal", "'96'"),
            ("value_num", "1"),
            ("reported_unit", "'V'"),
            ("measurand_ref_id", "MEASURAND"),
            ("run_key", "'x'"),
            ("tier", "'asserted'"),
            ("actor", "'someone'"),
            ("model", "'other'"),
            ("primary_link_id", "primary_link_id + 1000000"),
            ("role", "'model'"),
            ("source_attribution", "'own_work'"),
            ("measurand_status", "'ambiguous'"),
            ("anchor_scheme", "'range'"),
            ("span", "'\"s9\"'::jsonb"),
            ("meta", "'{\"x\": 1}'::jsonb"),
        ],
    )
    def test_frozen_field_update_is_refused_naming_the_rule(
        self, store: Store, world: dict[str, Any], column: str, value: str
    ) -> None:
        mid = self._measure(store, world)
        value = value.replace("MEASURAND", str(world["energy"]))
        with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
            _update(
                store, f"UPDATE measures SET {column} = {value} WHERE id = %s", (mid,)
            )
        assert _row(store, mid)["literal"] == "95"

    def test_trusted_update_passes_and_does_not_stale_the_review(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measure(store, world)
        store.record_target_review("measure", mid, actor="checker", verdict="approved")
        _update(store, "UPDATE measures SET trusted = true WHERE id = %s", (mid,))
        _update(
            store,
            "UPDATE measures SET extraction_status = 'human_checked' WHERE id = %s",
            (mid,),
        )
        assert _row(store, mid)["trusted"] is True
        assert store.reviews_for("measure", mid)[0].current

    def test_extraction_status_may_only_become_human_checked(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measure(store, world)
        with pytest.raises(psycopg.errors.CheckViolation, match="human_checked"):
            _update(
                store,
                "UPDATE measures SET extraction_status = 'anchor_mismatch' WHERE id = %s",
                (mid,),
            )

    def test_the_review_sha_covers_the_frozen_fields_only(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        a = self._measure(store, world)
        sha = store.target_sha("measure", a)
        _update(store, "UPDATE measures SET trusted = false WHERE id = %s", (a,))
        assert store.target_sha("measure", a) == sha
        b = self._measure(store, world)
        assert store.target_sha("measure", b) != sha  # different run_key / link


class TestAnchorLossAndSupersession:
    def _measured(
        self, store: Store, w: dict[str, Any], subject: int | None = None
    ) -> int:
        run = store.insert_measure(
            MeasureSpec(
                w["energy"],
                "1",
                subject or w["paper"],
                tier="measured",
                anchor=_anchor(w, "s1"),
            ),
            actor="reader",
        )
        return run.output_id

    def test_deleting_the_anchoring_chunk_nulls_the_link_and_stales_the_review(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measured(store, world)
        store.record_target_review("measure", mid, actor="checker", verdict="approved")
        assert store.reviews_for("measure", mid)[0].current
        # what ingest/db_writer's markup backfill does to a paper's body
        _update(
            store,
            "DELETE FROM chunks WHERE ref_id = %s AND ord >= 0",
            (world["paper"],),
        )
        row = _row(store, mid)
        assert row["primary_link_id"] is None and row["anchor_lost"] is True
        assert row["tier"] == "measured"
        assert not store.reviews_for("measure", mid)[0].current

    def test_deleting_the_source_ref_nulls_source_ref_id(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mat = seed_ref(store, title="Cu", kind="material")
        mid = self._measured(store, world, subject=mat)
        assert _row(store, mid)["source_ref_id"] == world["paper"]
        _update(store, "DELETE FROM refs WHERE ref_id = %s", (world["paper"],))
        row = _row(store, mid)
        assert row["source_ref_id"] is None and row["primary_link_id"] is None
        assert row["literal"] == "1"

    def test_primary_link_goes_to_null_only_once_the_link_is_gone(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measured(store, world)
        for sql in (
            "UPDATE measures SET primary_link_id = NULL WHERE id = %s",
            "UPDATE measures SET primary_link_id = primary_link_id + 1 WHERE id = %s",
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="primary_link_id"):
                _update(store, sql, (mid,))

    def test_source_ref_goes_to_null_only_once_the_ref_is_gone(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measured(store, world)
        with pytest.raises(psycopg.errors.CheckViolation, match="source_ref_id"):
            _update(
                store, "UPDATE measures SET source_ref_id = NULL WHERE id = %s", (mid,)
            )

    def test_superseded_by_needs_superseded_at_in_the_same_update(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        a = self._measured(store, world)
        b = self._measured(store, world)
        with pytest.raises(psycopg.errors.CheckViolation, match="superseded_by"):
            _update(
                store, "UPDATE measures SET superseded_by = %s WHERE id = %s", (b, a)
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="superseded_at"):
            _update(
                store, "UPDATE measures SET superseded_at = now() WHERE id = %s", (a,)
            )

    def test_superseded_by_cannot_point_at_itself(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        a = self._measured(store, world)
        with pytest.raises(psycopg.errors.CheckViolation, match="superseded_by"):
            _update(
                store,
                "UPDATE measures SET superseded_by = id, superseded_at = now() "
                "WHERE id = %s",
                (a,),
            )

    def test_superseded_by_must_state_the_same_number(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        a = self._measured(store, world)
        other_measurand = store.insert_measure(
            MeasureSpec(world["potential"], "1", world["paper"], reported_unit="V"),
            actor="reader",
        ).output_id
        other_subject = self._measured(
            store, world, subject=seed_ref(store, title="Zn", kind="material")
        )
        for target in (other_measurand, other_subject):
            with pytest.raises(psycopg.errors.CheckViolation, match="superseded_by"):
                _update(
                    store,
                    "UPDATE measures SET superseded_by = %s, superseded_at = now() "
                    "WHERE id = %s",
                    (target, a),
                )

    def test_superseded_by_must_be_live(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        a = self._measured(store, world)
        b = self._measured(store, world)
        c = self._measured(store, world)
        _update(
            store,
            "UPDATE measures SET superseded_by = %s, superseded_at = now() WHERE id = %s",
            (c, b),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="superseded_by"):
            _update(
                store,
                "UPDATE measures SET superseded_by = %s, superseded_at = now() "
                "WHERE id = %s",
                (b, a),  # b is itself superseded
            )

    def test_a_cached_ref_holding_measures_is_not_replaced(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        from precis.store.types import ChunkInsert

        def put() -> Any:
            return store.put_cache_entry(
                kind="news",
                slug="a-story",
                title="a story",
                body_blocks=[ChunkInsert(ord=0, text="body")],
                provider="news",
                request_hash="h",
                ttl_seconds=None,
            )

        ref, _ = put()
        store.insert_measure(MeasureSpec(world["energy"], "1", ref.id), actor="reader")
        with pytest.raises(BadInput, match="holds 1 measure"):
            put()
        assert store.get_ref(kind="news", id=ref.id) is not None

    def test_superseded_by_is_set_once(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        a = self._measured(store, world)
        b = self._measured(store, world)
        c = self._measured(store, world)
        _update(
            store,
            "UPDATE measures SET superseded_by = %s, superseded_at = now() WHERE id = %s",
            (b, a),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="superseded_by"):
            _update(
                store,
                "UPDATE measures SET superseded_by = %s, superseded_at = now() "
                "WHERE id = %s",
                (c, a),
            )

    def test_measures_cannot_be_deleted_or_truncated(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mid = self._measured(store, world)
        with pytest.raises(psycopg.errors.CheckViolation, match="cannot be deleted"):
            _update(store, "DELETE FROM measures WHERE id = %s", (mid,))
        with pytest.raises(psycopg.errors.CheckViolation, match="TRUNCATE"):
            _update(store, "TRUNCATE measures", ())
        assert len(store.measures_for(world["paper"])) == 1

    def test_a_subject_or_measurand_with_measures_cannot_be_hard_deleted(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        self._measured(store, world)
        for ref in (world["paper"], world["energy"]):
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                _update(store, "DELETE FROM refs WHERE ref_id = %s", (ref,))
        assert len(store.measures_for(world["paper"])) == 1

    def test_purge_keeps_a_tombstone_that_has_measures(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        from precis.cli.maintenance import _purge_soft_deleted

        self._measured(store, world)
        bare = seed_ref(store, title="a bare tombstone", kind="paper")
        for ref in (world["paper"], bare):
            _update(
                store,
                "UPDATE refs SET retired_at = now() - interval '400 days' "
                "WHERE ref_id = %s",
                (ref,),
            )
        assert _purge_soft_deleted(store=store, older_than_days=30, dry_run=True) == 1
        assert _purge_soft_deleted(store=store, older_than_days=30, dry_run=False) == 1
        assert store.get_ref(kind="paper", id=bare) is None
        assert len(store.measures_for(world["paper"])) == 1

    def test_the_sha_does_not_move_with_session_settings(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        mid = store.material_value_insert(
            material_ref_id=mat,
            property_id="density",
            value_num=0.1 + 0.2,
            value_low=1 / 3,
            as_of="2026-10-03",
        )
        shas = []
        with store.pool.connection() as conn:
            with conn.transaction():
                for setting in (
                    "SET LOCAL TimeZone = 'UTC'",
                    "SET LOCAL TimeZone = 'Pacific/Auckland'",
                    "SET LOCAL extra_float_digits = 0",
                    "SET LOCAL extra_float_digits = 3",
                    "SET LOCAL DateStyle = 'SQL, DMY'",
                ):
                    conn.execute(setting)
                    row = conn.execute(
                        "SELECT precis_target_sha('measure', %s)", (mid,)
                    ).fetchone()
                    assert row is not None
                    shas.append(row[0])
        assert len(set(shas)) == 1 and shas[0]

    def test_cross_measurand_supersession_is_refused(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        old = self._measured(store, world)
        with pytest.raises(BadInput, match="different measurand"):
            store.insert_measure(
                MeasureSpec(
                    world["potential"],
                    "1",
                    world["paper"],
                    reported_unit="V",
                    supersedes=old,
                ),
                actor="reader",
            )
        with pytest.raises(BadInput, match="different measurand"):
            store.insert_measure(
                MeasureSpec(
                    world["energy"],
                    "1",
                    seed_ref(store, title="other paper", kind="paper"),
                    supersedes=old,
                ),
                actor="reader",
            )

    def test_second_supersession_of_the_same_row_is_refused(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        old = self._measured(store, world)
        spec = MeasureSpec(world["energy"], "2", world["paper"], supersedes=old)
        store.insert_measure(spec, actor="reader")
        with pytest.raises(BadInput, match="already superseded"):
            store.insert_measure(spec, actor="reader")
        assert len(store.measures_for(world["paper"])) == 1

    def test_a_retired_anchor_chunk_is_refused(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        _update(
            store,
            "UPDATE chunks SET retired_at = now() WHERE chunk_id = %s",
            (world["chunk"],),
        )
        with pytest.raises(BadInput, match="retired"):
            self._measured(store, world)

    def test_an_input_row_cannot_claim_direction_output(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        with pytest.raises(BadInput, match="direction"):
            store.insert_measure(
                MeasureSpec(world["energy"], "1", world["paper"]),
                [_with(_potential(world, "1"), direction="output")],
                actor="reader",
            )
        assert store.measures_for(world["paper"]) == []


class TestLiteralAndUnits:
    @pytest.mark.parametrize(
        ("literal", "text", "matched"),
        [
            ("5", "reached 1950 mol", False),
            ("95", "reached 1950 mol", False),
            ("5", "a value of 0.5 V", False),
            ("5", "a value of 5.5 V", False),
            ("95", "FE of 95% at 25 C", True),
            ("95", "FE was 95.", True),
            ("-0.5", "at -0.5 V vs RHE", True),
            ("9.6 ± 1.7", "9.6 ± 1.7 mg", True),
        ],
    )
    def test_extraction_status_matches_on_number_boundaries(
        self,
        store: Store,
        world: dict[str, Any],
        literal: str,
        text: str,
        matched: bool,
    ) -> None:
        chunk = seed_chunk(store, ref_id=world["paper"], text=text, ord=3)
        run = store.insert_measure(
            MeasureSpec(
                world["energy"],
                literal,
                world["paper"],
                value_num=1.0,
                anchor=MeasureAnchor(world["paper"], chunk, "sentence", "s1"),
            ),
            actor="reader",
        )
        want = "anchor_matched" if matched else "anchor_mismatch"
        assert _row(store, run.output_id)["extraction_status"] == want

    @pytest.mark.parametrize(
        ("canonical", "reported", "literal", "expected"),
        [
            ("1", "%", "95", 0.95),
            ("J", "eV", "0.5", 8.01088317e-20),
            ("m", "Å", "1.4", 1.4e-10),
            ("K", "°C", "25", 298.15),  # absolute temperature, not a delta
            ("K", "degC", "-273.15", 0.0),
        ],
    )
    def test_si_canonical_units_convert_from_common_reported_units(
        self,
        store: Store,
        world: dict[str, Any],
        canonical: str,
        reported: str,
        literal: str,
        expected: float,
    ) -> None:
        tax = _taxon(store, f"si {canonical}", unit=canonical, parent=world["start"])
        run = store.insert_measure(
            MeasureSpec(tax, literal, world["paper"], reported_unit=reported),
            actor="reader",
        )
        row = _row(store, run.output_id)
        assert row["value_num"] == pytest.approx(expected, rel=1e-6, abs=1e-12)
        assert row["reported_unit"] == reported and row["literal"] == literal

    def test_a_temperature_uncertainty_converts_as_a_delta(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        tax = _taxon(store, "si K", unit="K", parent=world["start"])
        run = store.insert_measure(
            MeasureSpec(tax, "25 ± 2", world["paper"], reported_unit="°C"),
            actor="reader",
        )
        row = _row(store, run.output_id)
        assert row["value_num"] == pytest.approx(298.15)
        assert row["value_err"] == pytest.approx(2.0)

    def test_form_comes_from_the_literal_even_when_a_value_is_supplied(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        run = store.insert_measure(
            MeasureSpec(world["energy"], "<1", world["paper"], value_num=1.0),
            actor="reader",
        )
        assert _row(store, run.output_id)["value_form"] == "upper_bound"

    def test_non_pint_canonical_unit_accepts_the_identical_unit(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        cost = _taxon(store, "unit cost", unit="USD", parent=world["start"])
        run = store.insert_measure(
            MeasureSpec(cost, "12.5", world["paper"], reported_unit="USD"),
            actor="reader",
        )
        r = _row(store, run.output_id)
        assert r["value_num"] == 12.5 and r["reported_unit"] == "USD"
        assert r["meta"].get("conversion") is None

    def test_non_pint_canonical_unit_refuses_another_unit_naming_both(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        cost = _taxon(store, "unit cost", unit="USD", parent=world["start"])
        with pytest.raises(BadInput) as err:
            store.insert_measure(
                MeasureSpec(cost, "12.5", world["paper"], reported_unit="EUR"),
                actor="reader",
            )
        assert "'EUR'" in str(err.value) and "'USD'" in str(err.value)

    def test_the_required_conditions_walk_uses_the_runs_connection(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        # taxa minted inside the caller's transaction are visible to the walk
        with store.tx() as conn:
            parent = store.insert_ref(
                kind="taxon",
                slug=None,
                title="p",
                meta={"name": "p", "slug": "p", "required_conditions": ["ph"]},
                conn=conn,
            )
            child = store.insert_ref(
                kind="taxon",
                slug=None,
                title="c",
                meta={"name": "c", "slug": "c", "canonical_unit": "%"},
                conn=conn,
            )
            store.add_link(
                src_ref_id=child.id,
                dst_ref_id=parent.id,
                relation="specialises",
                conn=conn,
            )
            run = store.insert_measure(
                MeasureSpec(child.id, "1", world["paper"], reported_unit="%"),
                actor="reader",
                conn=conn,
            )
        assert _flags(store, run.output_id) == ["ph"]


# ── taxon: canonical_unit guard + required_conditions key ──────────────────


class TestTaxon:
    def test_taxon_with_live_measures_refuses_a_canonical_unit_change(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        store.insert_measure(
            MeasureSpec(world["energy"], "1", world["paper"], reported_unit="eV"),
            actor="reader",
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="canonical_unit"):
            store.update_ref(world["energy"], meta_patch={"canonical_unit": "J"})
        # an unrelated meta change is fine, and so is a unit change on a taxon
        # nothing is stored against
        store.update_ref(world["energy"], meta_patch={"definition": "new wording."})
        store.update_ref(world["temperature"], meta_patch={"canonical_unit": "degC"})
        ref = store.get_ref(kind="taxon", id=world["energy"])
        assert ref is not None and ref.meta["canonical_unit"] == "eV"

    def test_dimension_kind_and_si_vector_are_guarded_too(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        dim = _taxon(store, "dimensioned", unit="K", parent=world["start"])
        kelvin = {"dimension_kind": "si", "si_vector": "0,0,0,0,1,0,0"}
        store.update_ref(dim, meta_patch=kelvin)
        free = _taxon(store, "undimensioned", unit="K", parent=world["start"])
        store.insert_measure(
            MeasureSpec(dim, "300", world["paper"], reported_unit="K"), actor="reader"
        )
        for patch in (
            {"dimension_kind": "dimensionless"},
            {"si_vector": "0,0,1,0,0,0,0"},
        ):
            with pytest.raises(psycopg.errors.CheckViolation, match="re-base"):
                store.update_ref(dim, meta_patch=patch)
        store.update_ref(free, meta_patch=kelvin)

    def test_dimension_may_be_filled_in_from_null_while_measures_exist(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        tax = _taxon(store, "unmapped dimension", unit="K", parent=world["start"])
        store.insert_measure(
            MeasureSpec(tax, "300", world["paper"], reported_unit="K"), actor="reader"
        )
        store.update_ref(
            tax, meta_patch={"dimension_kind": "si", "si_vector": "0,0,0,0,1,0,0"}
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="re-base"):
            store.update_ref(tax, meta_patch={"dimension_kind": "count"})
        ref = store.get_ref(kind="taxon", id=tax)
        assert ref is not None and ref.meta["dimension_kind"] == "si"

    def test_a_number_with_a_unit_is_refused_on_a_unit_less_taxon(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        tax = _taxon(store, "unitless so far", parent=world["start"])
        with pytest.raises(BadInput, match="one unit per measurand") as err:
            store.insert_measure(
                MeasureSpec(tax, "300", world["paper"], reported_unit="K"),
                actor="reader",
            )
        assert "canonical_unit" in str(err.value.next)
        assert store.measures_for(world["paper"]) == []

    def test_a_unitless_value_lands_on_a_unit_less_taxon_and_pins_the_unit(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        tax = _taxon(store, "a count", parent=world["start"])
        run = store.insert_measure(
            MeasureSpec(tax, "12", world["paper"]), actor="reader"
        )
        assert _row(store, run.output_id)["value_num"] == 12.0
        label = store.insert_measure(
            MeasureSpec(tax, "Cordierite", world["paper"]), actor="reader"
        )
        assert _row(store, label.output_id)["value_form"] == "categorical"
        with pytest.raises(psycopg.errors.CheckViolation, match="canonical_unit"):
            store.update_ref(tax, meta_patch={"canonical_unit": "mK"})

    def test_a_minted_legacy_taxon_copies_the_registry_unit(self, store: Store) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        store.material_value_insert(
            material_ref_id=mat, property_id="density", value_num=2700.0
        )
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT meta ->> 'canonical_unit' FROM refs WHERE kind = 'taxon' "
                "AND meta -> 'legacy_source' ->> 'key' = 'density'"
            ).fetchone()
        assert row == ("kg/m3",)

    def test_required_conditions_key_is_validated(self) -> None:
        assert validate_taxon_meta({"required_conditions": ["product"]})
        for bad in ("product", [1], [""], ["a", " "]):
            with pytest.raises(BadInput, match="required_conditions"):
                validate_taxon_meta({"required_conditions": bad})

    def test_required_conditions_is_allowed_off_a_start_node(self) -> None:
        assert validate_taxon_meta({"required_conditions": ["x"], "start": False})

    def test_quantifies_is_registered_with_its_inverse(self, store: Store) -> None:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT slug, inverse_slug FROM relations "
                "WHERE slug IN ('quantifies', 'quantified-by') ORDER BY slug"
            ).fetchall()
        assert rows == [
            ("quantified-by", "quantifies"),
            ("quantifies", "quantified-by"),
        ]
        from precis.store.types import _INVERSE_RELATIONS

        assert _INVERSE_RELATIONS["quantifies"] == "quantified-by"


def test_merging_a_paper_that_anchors_measures_is_refused_naming_the_count(
    store: Store, world: dict[str, Any]
) -> None:
    for lit in ("1", "2"):
        store.insert_measure(
            MeasureSpec(
                world["energy"],
                lit,
                world["paper"],
                tier="measured",
                anchor=_anchor(world, f"s{lit}"),
            ),
            actor="reader",
        )
    survivor = seed_ref(store, title="the same paper again", kind="paper")
    with pytest.raises(BadInput, match="2 live measure"):
        store.merge_refs(world["paper"], survivor)
    paper = store.get_ref(kind="paper", id=world["paper"])
    assert paper is not None  # nothing was touched
    assert len(store.measures_for(world["paper"])) == 2
    assert (
        len(store.links_for(world["paper"], direction="out", relation="quantifies"))
        == 1
    )


def test_measures_for_orders_outputs_before_inputs(
    store: Store, world: dict[str, Any]
) -> None:
    store.insert_measure(
        MeasureSpec(world["fe"], "95", world["paper"], reported_unit="%"),
        [_potential(world, "-0.5"), _product(world)],
        actor="reader",
    )
    rows = store.measures_for(world["paper"])
    assert [r["direction"] for r in rows] == ["output", "input", "input"]
    assert {r["role"] for r in rows if r["direction"] == "input"} == {"context"}


# ── Build B: SI stored, display unit shown, and the reads ──────────────────


class TestBuildBDisplayAndReads:
    """The store-to-display round trip the Build B kinds rely on; the kind and
    quest view themselves are in ``tests/test_measure_kind.py``."""

    def test_taxon_put_validates_display_unit_against_canonical(
        self, store: Store
    ) -> None:
        from precis.dispatch import Hub
        from precis.handlers.taxon import TaxonHandler

        h = TaxonHandler(hub=Hub(store=store))
        resp = h.put(
            text="bond length — distance between two bonded nuclei",
            meta={"canonical_unit": "m", "display_unit": "Å"},
        )
        assert "tn" in resp.body
        with pytest.raises(BadInput, match="different dimension"):
            h.put(
                text="oxide thickness — layer depth",
                meta={"canonical_unit": "m", "display_unit": "eV"},
            )

    def test_stored_si_value_shows_in_the_display_unit(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        from precis.taxonomy.measure_units import format_value

        bond = _taxon(store, "bond length", unit="m", parent=world["start"])
        run = store.insert_measure(
            MeasureSpec(bond, "1.4", world["paper"], reported_unit="Å", subject="Cu"),
            actor="reader",
        )
        row = _row(store, run.output_id)
        assert row["value_num"] == pytest.approx(1.4e-10)
        shown = format_value(row["value_num"], canonical_unit="m", display_unit="Å")
        assert shown == "1.4 Å"
        assert format_value(row["value_num"], canonical_unit="m") == "140 pm"

    def test_census_and_best_read_the_same_rows_measures_for_lists(
        self, store: Store, world: dict[str, Any]
    ) -> None:
        quest = store.insert_ref(kind="quest", slug=None, title="q").id
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        store.insert_measure(
            MeasureSpec(world["energy"], "0.5", world["paper"], reported_unit="eV"),
            actor="reader",
        )
        assert sum(r["n"] for r in store.measures_census(world["energy"])) == 1
        (group,) = store.best_measure(world["energy"], serving=quest, sense="min")
        assert group["n"] == 1
        assert group["best"]["id"] == store.measures_for(world["paper"])[0]["id"]
