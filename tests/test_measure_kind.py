"""``kind='measure'`` and the Build B reads of the measures substrate
(docs/backlog/measures-substrate.md, "Build B: how agents reach measures"):
SI-stored values shown in a display unit, ``best_measure`` / ``measures_census`` /
``search_measures``, the handler's get and range-and-conditions search, and the
quest ``view='measures'``. Real PG (the ``store`` fixture); the display rules
are pure and need no DB.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers.measure import MeasureHandler, parse_query
from precis.handlers.quest import QuestHandler
from precis.store import Store
from precis.store._measures_ops import ConditionFilter, MeasureAnchor, MeasureSpec
from precis.taxonomy.measure_units import (
    display_numbers,
    format_value,
    make_converter,
    to_canonical,
    validate_display_unit,
)
from precis.taxonomy.nodes import slugify, validate_taxon_meta
from tests.workers._helpers import seed_chunk, seed_ref

# ── pure: display rules ────────────────────────────────────────────────────


class TestDisplay:
    def test_display_unit_wins(self) -> None:
        assert format_value(1.4e-10, canonical_unit="m", display_unit="Å") == "1.4 Å"

    def test_no_display_unit_takes_an_automatic_prefix(self) -> None:
        assert format_value(1.4e-10, canonical_unit="m") == "140 pm"

    def test_percent_only_when_the_display_unit_says_so(self) -> None:
        assert format_value(0.95, canonical_unit="1", display_unit="%") == "95 %"
        assert format_value(0.95, canonical_unit="1") == "0.95"

    def test_affine_units_display_as_absolute(self) -> None:
        assert format_value(298.15, canonical_unit="K", display_unit="°C") == "25 °C"

    def test_explicit_unit_overrides_display_unit(self) -> None:
        got = format_value(1.4e-10, canonical_unit="m", display_unit="Å", unit="nm")
        assert got == "0.14 nm"

    def test_energy_shows_in_ev(self) -> None:
        assert (
            format_value(1.602176634e-19, canonical_unit="J", display_unit="eV")
            == "1 eV"
        )

    def test_ph_is_never_prefixed_or_converted(self) -> None:
        assert format_value(0.001, canonical_unit="pH") == "0.001 pH"
        with pytest.raises(BadInput, match="logarithmic"):
            make_converter("pH", "1")
        with pytest.raises(BadInput, match="logarithmic"):
            make_converter("1", "dB")

    def test_unitless_canonical_stays_bare(self) -> None:
        assert format_value(3.0, canonical_unit=None) == "3"
        assert format_value(0.5, canonical_unit="1/s") == "0.5 1/s"

    def test_a_wrong_kind_of_explicit_unit_is_refused_naming_both(self) -> None:
        with pytest.raises(BadInput, match="'V'.*'m'|'m'.*'V'"):
            format_value(1.4e-10, canonical_unit="m", unit="V")

    def test_a_stale_display_unit_falls_back_to_si(self) -> None:
        got = format_value(1.4e-10, canonical_unit="m", display_unit="V")
        assert got == "140 pm"

    def test_an_interval_shares_one_unit(self) -> None:
        nums, label = display_numbers([1e-9, 2e-6], canonical_unit="m")
        assert label == "µm" and nums == pytest.approx([0.001, 2.0])

    def test_to_canonical_covers_the_pilot_cases(self) -> None:
        assert to_canonical(1.4, "Å", "m") == pytest.approx(1.4e-10)
        assert to_canonical(95, "%", "1") == pytest.approx(0.95)
        assert to_canonical(25, "°C", "K") == pytest.approx(298.15)
        assert to_canonical(10, "mA cm⁻²", "A m⁻²") == pytest.approx(100.0)
        assert to_canonical(1, "eV", "J") == pytest.approx(1.602176634e-19)
        with pytest.raises(BadInput, match="no canonical unit"):
            to_canonical(1, "m", None)


class TestDisplayUnitKey:
    def test_same_dimension_is_accepted(self) -> None:
        meta = validate_taxon_meta({"canonical_unit": "m", "display_unit": "Å"})
        assert meta["display_unit"] == "Å"
        validate_taxon_meta({"canonical_unit": "1", "display_unit": "%"})
        validate_taxon_meta({"canonical_unit": "K", "display_unit": "°C"})

    def test_another_dimension_is_refused(self) -> None:
        with pytest.raises(BadInput, match="different dimension"):
            validate_taxon_meta({"canonical_unit": "m", "display_unit": "s"})

    def test_a_non_pint_canonical_allows_only_itself(self) -> None:
        validate_taxon_meta({"canonical_unit": "USD", "display_unit": "USD"})
        with pytest.raises(BadInput, match="identical"):
            validate_taxon_meta({"canonical_unit": "USD", "display_unit": "EUR"})
        validate_taxon_meta({"canonical_unit": "pH", "display_unit": "pH"})
        with pytest.raises(BadInput, match="logarithmic"):
            validate_taxon_meta({"canonical_unit": "pH", "display_unit": "m"})

    def test_it_is_only_checked_once_canonical_unit_is_set(self) -> None:
        validate_taxon_meta({"display_unit": "Å"})  # merged and re-checked later

    def test_type_and_emptiness(self) -> None:
        with pytest.raises(BadInput, match="unit string"):
            validate_taxon_meta({"display_unit": 3})
        with pytest.raises(BadInput, match="non-empty"):
            validate_display_unit("  ", "m")


# ── pure: the q= grammar ───────────────────────────────────────────────────


class TestParseQuery:
    def test_terms_and_words(self) -> None:
        conds, text = parse_query("product=NH3 potential<-0.5 Cu NWA")
        assert conds == [
            ConditionFilter("product", "=", text="NH3"),
            ConditionFilter("potential", "<", value=-0.5),
        ]
        assert text == "Cu NWA"

    def test_a_unit_glued_or_as_the_next_word(self) -> None:
        conds, text = parse_query("potential<-0.5V temperature>300 K Cu")
        assert conds == [
            ConditionFilter("potential", "<", value=-0.5, unit="V"),
            ConditionFilter("temperature", ">", value=300.0, unit="K"),
        ]
        assert text == "Cu"

    def test_a_subject_word_after_a_number_is_not_swallowed_as_a_unit(self) -> None:
        conds, text = parse_query("potential<-0.5 nanowire")
        assert conds == [ConditionFilter("potential", "<", value=-0.5)]
        assert text == "nanowire"

    def test_quoted_text_value_with_a_space(self) -> None:
        conds, _ = parse_query('hardware="M2 Ultra"')
        assert conds == [ConditionFilter("hardware", "=", text="M2 Ultra")]

    def test_inclusive_operators_and_unicode_minus(self) -> None:
        conds, _ = parse_query("potential>=−0.9 V")
        assert conds == [ConditionFilter("potential", ">=", value=-0.9, unit="V")]

    def test_a_bound_on_text_is_refused(self) -> None:
        with pytest.raises(BadInput, match="need a number"):
            parse_query("product>NH3")

    def test_empty(self) -> None:
        assert parse_query(None) == ([], "")
        assert parse_query("  ") == ([], "")


# ── builders ───────────────────────────────────────────────────────────────

CHUNK_TEXT = "Faradaic efficiency 95% at -0.5 V vs RHE; bond 1.4 Å; decode 38.5."


def _taxon(
    store: Store,
    name: str,
    *,
    unit: str | None = None,
    display: str | None = None,
    higher: bool | None = None,
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
    if display is not None:
        meta["display_unit"] = display
    if higher is not None:
        meta["higher_is_better"] = higher
    if required is not None:
        meta["required_conditions"] = required
    if condition is not None:
        meta["condition"] = condition
    ref = store.insert_ref(kind="taxon", slug=None, title=name, meta=meta)
    if parent is not None:
        store.add_link(src_ref_id=ref.id, dst_ref_id=parent, relation="specialises")
    return ref.id


@pytest.fixture
def w(store: Store) -> dict[str, Any]:
    """Pilot-shaped SI taxa under a ``measurand`` start node, a quest tree
    (quest <- sub-quest <- paper; a second paper serves the quest directly; a
    third serves nothing) and a chunk of printed numbers."""
    start = _taxon(store, "measurand")
    d: dict[str, Any] = {"start": start}
    d["fe"] = _taxon(
        store,
        "Faradaic efficiency",
        unit="1",
        display="%",
        higher=True,
        required=["product", "potential"],
        parent=start,
    )
    d["yield_geo"] = _taxon(
        store,
        "product yield rate per geometric area",
        unit="mol s⁻¹ m⁻²",
        display="µmol h⁻¹ cm⁻²",
        higher=True,
        required=["product"],
        parent=start,
    )
    d["u_l"] = _taxon(
        store, "limiting potential", unit="V", display="V", higher=False, parent=start
    )
    d["potential"] = _taxon(store, "potential", unit="V", display="V", parent=start)
    d["temperature"] = _taxon(
        store, "temperature", unit="K", display="°C", parent=start
    )
    d["product"] = _taxon(store, "reaction product", parent=start)
    d["bond"] = _taxon(store, "bond length", unit="m", display="Å", parent=start)
    d["cc_bond"] = _taxon(
        store, "C-C bond length", unit="m", display="Å", parent=d["bond"]
    )
    d["unranked"] = _taxon(store, "particle size", unit="m", parent=start)

    d["quest"] = store.insert_ref(kind="quest", slug=None, title="NO to N").id
    d["sub"] = store.insert_ref(kind="quest", slug=None, title="Cu routes").id
    store.add_link(src_ref_id=d["sub"], dst_ref_id=d["quest"], relation="serves")
    d["paper"] = seed_ref(store, title="Deep paper", kind="paper")
    d["paper2"] = seed_ref(store, title="Direct paper", kind="paper")
    d["stray"] = seed_ref(store, title="Unserving paper", kind="paper")
    store.add_link(src_ref_id=d["paper"], dst_ref_id=d["sub"], relation="serves")
    store.add_link(src_ref_id=d["paper2"], dst_ref_id=d["quest"], relation="serves")
    d["chunk"] = seed_chunk(store, ref_id=d["paper"], text=CHUNK_TEXT)
    return d


def _cond(
    w: dict[str, Any],
    key: str,
    literal: str,
    *,
    unit: str | None = None,
    label: str | None = None,
    ref: str | None = None,
    subject: int | None = None,
) -> MeasureSpec:
    return MeasureSpec(
        measurand_ref_id=w[key],
        literal=literal,
        subject_ref_id=subject or w["paper"],
        reported_unit=unit,
        reference=ref,
        value_text=literal
        if unit is None and literal[0] not in "-0123456789"
        else None,
        meta={"condition": label} if label else {},
    )


def _put(
    store: Store,
    w: dict[str, Any],
    key: str,
    literal: str,
    *,
    unit: str | None = None,
    subject: int | None = None,
    label: str | None = "Cu NWA",
    conds: tuple[MeasureSpec, ...] = (),
    **kw: Any,
) -> Any:
    spec = MeasureSpec(
        measurand_ref_id=w[key],
        literal=literal,
        subject_ref_id=subject or w["paper"],
        reported_unit=unit,
        subject=label,
        **kw,
    )
    return store.insert_measure(spec, list(conds), actor="reader", model="m1")


def _fe_run(
    store: Store, w: dict[str, Any], pct: str, volts: str, *, subject: int | None = None
) -> Any:
    return _put(
        store,
        w,
        "fe",
        pct,
        unit="%",
        subject=subject,
        conds=(
            _cond(w, "product", "NH3", label="product"),
            _cond(w, "potential", volts, unit="V", ref="RHE"),
        ),
        reference="RHE",
    )


def _by_id(rows: list[dict[str, Any]], mid: int) -> dict[str, Any]:
    (r,) = [x for x in rows if x["id"] == mid]
    return r


# ── store: best_measure / census / search ──────────────────────────────────


class TestBestMeasure:
    def test_picks_the_best_over_everything_serving_at_any_depth(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        deep = _fe_run(store, w, "61", "-0.9", subject=w["paper"])
        direct = _fe_run(store, w, "95", "-0.5", subject=w["paper2"])
        _fe_run(store, w, "99", "-0.5", subject=w["stray"])  # serves nothing
        (g,) = store.best_measure(w["fe"], serving=w["quest"])
        assert g["n"] == 2 and g["sense"] == "higher"
        assert g["best"]["id"] == direct.output_id
        assert g["best"]["value_num"] == pytest.approx(0.95)  # SI: a fraction
        assert [c["name"] for c in g["best"]["conditions"]] == [
            "product",
            "potential",
        ]
        # the sub-quest sees only what serves it
        (g2,) = store.best_measure(w["fe"], serving=w["sub"])
        assert g2["best"]["id"] == deep.output_id

    def test_direction_comes_from_the_taxon_unless_sense_is_given(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        a = _put(store, w, "u_l", "-0.30", unit="V")
        b = _put(store, w, "u_l", "-0.10", unit="V")
        (g,) = store.best_measure(w["u_l"], serving=w["quest"])
        assert g["sense"] == "lower" and g["best"]["id"] == a.output_id
        (g,) = store.best_measure(w["u_l"], serving=w["quest"], sense="max")
        assert g["best"]["id"] == b.output_id
        with pytest.raises(BadInput, match="higher/max or lower/min"):
            store.best_measure(w["u_l"], serving=w["quest"], sense="biggest")

    def test_a_taxon_without_a_direction_reports_its_rows_and_no_best(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        _put(store, w, "unranked", "3", unit="nm")
        _put(store, w, "unranked", "5", unit="nm")
        (g,) = store.best_measure(w["unranked"], serving=w["quest"])
        assert g["best"] is None and g["sense"] is None and g["n"] == 2

    def test_covers_specialises_descendants(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        _put(store, w, "cc_bond", "1.54", unit="Å")
        (g,) = store.best_measure(w["bond"], serving=w["quest"], sense="max")
        assert g["measurand"] == "C-C bond length"
        assert store.best_measure(w["cc_bond"], serving=w["quest"], sense="max")

    def test_a_she_row_without_ph_never_stands_in_for_rhe(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        """AC 5: a reference filter keeps only rows stated against it; the SHE
        row is not converted (no pH, and no convert rule is built)."""
        rhe = _put(store, w, "u_l", "-0.40", unit="V", reference="RHE")
        _put(store, w, "u_l", "-0.80", unit="V", reference="SHE")
        (g,) = store.best_measure(w["u_l"], serving=w["quest"], reference="RHE")
        assert g["reference"] == "RHE" and g["n"] == 1
        assert g["best"]["id"] == rhe.output_id
        # without the filter they are two groups, each answering for itself
        groups = store.best_measure(w["u_l"], serving=w["quest"])
        assert [(x["reference"], x["n"]) for x in groups] == [("RHE", 1), ("SHE", 1)]

    def test_a_row_missing_a_required_condition_is_flagged_and_skipped(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        """AC 6, the chemistry side: FE requires product and potential."""
        bare = _put(store, w, "fe", "99", unit="%", reference="RHE")
        full = _fe_run(store, w, "80", "-0.5")
        assert "escalation" in store.measures_for(w["paper"])[0]["meta"]
        (g,) = store.best_measure(w["fe"], serving=w["quest"])
        assert g["n"] == 1 and g["best"]["id"] == full.output_id
        assert bare.output_id != full.output_id

    def test_a_normalization_pair_is_never_compared(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        """AC 3: 3.7 per geometric area against 9.25 per ECSA are two groups."""
        conds = (_cond(w, "product", "NH3", label="product"),)
        geo = _put(
            store,
            w,
            "yield_geo",
            "3.7",
            unit="mol s⁻¹ m⁻²",
            normalization="per geometric area",
            conds=conds,
        )
        ecsa = _put(
            store,
            w,
            "yield_geo",
            "9.25",
            unit="mol s⁻¹ m⁻²",
            normalization="per ECSA",
            conds=conds,
        )
        groups = store.best_measure(w["yield_geo"], serving=w["quest"])
        assert [(g["normalization"], g["best"]["id"]) for g in groups] == [
            ("per ECSA", ecsa.output_id),
            ("per geometric area", geo.output_id),
        ]

    def test_excluded_rows(self, store: Store, w: dict[str, Any]) -> None:
        """Ambiguous, distrusted, anchor-lost, superseded, interval, bound."""
        good = _put(store, w, "u_l", "-0.10", unit="V")
        _put(store, w, "u_l", "-0.90", unit="V", measurand_status="ambiguous")
        distrusted = _put(store, w, "u_l", "-0.95", unit="V")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE measures SET trusted = false WHERE id = %s",
                (distrusted.output_id,),
            )
        lost = _put(
            store,
            w,
            "u_l",
            "-0.97",
            unit="V",
            tier="measured",
            anchor=MeasureAnchor(w["paper"], w["chunk"], "sentence", "s1"),
        )
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (w["paper"],)
            )
        assert lost.output_id
        _put(store, w, "u_l", "-0.98 to -0.99", unit="V")  # an interval
        _put(store, w, "u_l", "<-0.99", unit="V")  # a bound
        old = _put(store, w, "u_l", "-0.96", unit="V")
        _put(store, w, "u_l", "-0.96", unit="V", supersedes=old.output_id)
        (g,) = store.best_measure(w["u_l"], serving=w["quest"])
        # only the good row and the superseding row remain candidates
        assert g["n"] == 2
        assert g["best"]["literal"] == "-0.96"  # lower is better
        assert good.output_id

    def test_serving_must_be_a_quest(self, store: Store, w: dict[str, Any]) -> None:
        with pytest.raises(BadInput, match="not a quest"):
            store.best_measure(w["fe"], serving=w["paper"])


class TestCensus:
    def test_counts_by_tier_reference_and_normalization(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        """AC 7's shape: how many potentials state a reference and how many do not."""
        for _ in range(3):
            _put(store, w, "u_l", "-0.1", unit="V", reference="RHE", tier="asserted")
        for _ in range(2):
            _put(store, w, "u_l", "-0.2", unit="V", tier="asserted")
        _put(store, w, "u_l", "-0.3", unit="V", reference="SHE")
        old = _put(store, w, "u_l", "-0.4", unit="V")
        _put(store, w, "u_l", "-0.5", unit="V", supersedes=old.output_id)  # live: 1
        got = store.measures_census(w["u_l"])
        assert got[0] == {
            "tier": "asserted",
            "reference": "RHE",
            "normalization": None,
            "n": 3,
        }
        by_key = {(r["tier"], r["reference"]): r["n"] for r in got}
        assert by_key == {
            ("asserted", "RHE"): 3,
            ("asserted", None): 2,
            (None, "SHE"): 1,
            (None, None): 1,
        }
        assert sum(r["n"] for r in got) == 7  # the superseded row is not counted

    def test_covers_descendants_and_inputs(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        _fe_run(store, w, "95", "-0.5")  # the potential is an input row
        (row,) = store.measures_census(w["potential"])
        assert (row["reference"], row["n"]) == ("RHE", 1)
        _put(store, w, "cc_bond", "1.5", unit="Å")
        assert sum(r["n"] for r in store.measures_census(w["bond"])) == 1


class TestSearchMeasures:
    def test_interval_overlap_in_si(self, store: Store, w: dict[str, Any]) -> None:
        for lit in ("0.9", "1.4", "2.1"):
            _put(store, w, "bond", lit, unit="Å")
        iv = _put(store, w, "bond", "1.8–2.2", unit="Å")
        found = store.search_measures(w["bond"], min_si=2.0e-10, max_si=2.5e-10)
        assert sorted(r["literal"] for r in found.rows) == ["1.8–2.2", "2.1"]
        assert iv.output_id in {r["id"] for r in found.rows}
        # the boundary is inclusive and survives float noise
        exact = store.search_measures(w["bond"], min_si=0.14e-9, max_si=0.14e-9)
        assert [r["literal"] for r in exact.rows] == ["1.4"]

    def test_bounds_reach_through_one_sided_rows(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        _put(store, w, "u_l", "<-0.5", unit="V")
        _put(store, w, "u_l", ">0.5", unit="V")
        low = store.search_measures(w["u_l"], max_si=-1.0)
        assert [r["literal"] for r in low.rows] == ["<-0.5"]
        high = store.search_measures(w["u_l"], min_si=1.0)
        assert [r["literal"] for r in high.rows] == [">0.5"]

    def test_text_matches_the_subject_label(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        _put(store, w, "bond", "1", unit="Å", label="Cu NWA")
        _put(store, w, "bond", "2", unit="Å", label="Pd foam")
        got = store.search_measures(w["bond"], text="cu nwa")
        assert [r["literal"] for r in got.rows] == ["1"]
        assert store.search_measures(w["bond"], text="100%").rows == []  # no wildcard

    def test_status_all_includes_the_left_out_rows(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        old = _put(store, w, "bond", "1", unit="Å")
        new = _put(store, w, "bond", "1.1", unit="Å", supersedes=old.output_id)
        _put(store, w, "bond", "5", unit="Å", measurand_status="ambiguous")
        live = store.search_measures(w["bond"])
        assert [r["id"] for r in live.rows] == [new.output_id]
        everything = store.search_measures(w["bond"], include_all=True)
        assert len(everything.rows) == 3

    def test_condition_filters_with_units(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        a = _fe_run(store, w, "95", "-0.5")
        b = _fe_run(store, w, "61", "-0.9")

        def ids(*conds: ConditionFilter) -> set[int]:
            got = store.search_measures(w["fe"], conditions=conds)
            return {r["id"] for r in got.rows}

        assert ids(ConditionFilter("potential", "<", value=-0.7)) == {b.output_id}
        assert ids(ConditionFilter("potential", "<", value=-700, unit="mV")) == {
            b.output_id
        }
        assert ids(ConditionFilter("potential", ">", value=-0.7, unit="V")) == {
            a.output_id
        }
        assert ids(ConditionFilter("potential", "=", value=-0.5)) == {a.output_id}
        assert ids(ConditionFilter("potential", "<=", value=-0.5)) == {
            a.output_id,
            b.output_id,
        }
        assert ids(ConditionFilter("product", "=", text="nh3")) == {
            a.output_id,
            b.output_id,
        }
        assert ids(ConditionFilter("product", "=", text="CO")) == set()
        assert ids(ConditionFilter("pressure", "=", value=1)) == set()
        # a unit of another kind than the input never matches
        assert ids(ConditionFilter("potential", "<", value=-0.7, unit="m")) == set()
        # both terms must hold
        assert ids(
            ConditionFilter("product", "=", text="NH3"),
            ConditionFilter("potential", "<", value=-0.7),
        ) == {b.output_id}

    def test_a_temperature_condition_reads_in_its_display_unit(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        run = _put(
            store,
            w,
            "bond",
            "1.4",
            unit="Å",
            conds=(_cond(w, "temperature", "25", unit="°C"),),
        )
        stored = store.measures_for(w["paper"])
        temp = [r for r in stored if r["direction"] == "input"][0]
        assert temp["value_num"] == pytest.approx(298.15)  # SI: kelvin

        def hit(**kw: Any) -> bool:
            f = ConditionFilter("temperature", **kw)
            return bool(store.search_measures(w["bond"], conditions=[f]).rows)

        assert hit(op="=", value=25.0)  # bare number: the display unit, °C
        assert hit(op=">", value=20, unit="°C")
        assert hit(op="<", value=300, unit="K")
        assert not hit(op=">", value=300, unit="K")
        assert run.output_id


# ── the non-chemistry round trip ───────────────────────────────────────────


class TestLlmRoundTrip:
    @pytest.fixture
    def llm(self, store: Store, w: dict[str, Any]) -> dict[str, Any]:
        """decode tokens/s on an ``llm`` subject: pint cannot parse ``tok``, so
        the canonical unit is 1/s and there is no display unit."""
        d: dict[str, Any] = {}
        d["model"] = store.insert_ref(kind="llm", slug=None, title="Qwen-Next 80B").id
        d["decode"] = _taxon(
            store, "decode throughput", unit="1/s", higher=True, parent=w["start"]
        )
        d["quant"] = _taxon(store, "quantisation", parent=w["start"])
        d["hardware"] = _taxon(store, "hardware", parent=w["start"])
        w.update(d)
        for q, hw, tps in (("Q4", "M2-Ultra", "38.5"), ("Q8", "M2-Ultra", "22.1")):
            _put(
                store,
                w,
                "decode",
                tps,
                unit="1/s",
                subject=d["model"],
                label="Qwen-Next 80B",
                tier="asserted",
                conds=(
                    MeasureSpec(
                        d["quant"],
                        q,
                        d["model"],
                        value_text=q,
                        meta={"condition": "quant"},
                    ),
                    MeasureSpec(d["hardware"], hw, d["model"], value_text=hw),
                ),
            )
        return d

    def test_search_by_condition_finds_it(
        self, store: Store, w: dict[str, Any], llm: dict[str, Any]
    ) -> None:
        h = MeasureHandler(hub=Hub(store=store))
        out = h.search(q="quant=Q4").body
        assert "38.5 1/s" in out and "22.1" not in out
        assert "quant=Q4" in out and "hardware=M2-Ultra" in out
        assert "asserted" in out and "Qwen-Next 80B" in out
        out = h.search(
            property="measurand/decode-throughput", q='hardware="M2 Ultra"'
        ).body
        assert "38.5 1/s" in out and "22.1 1/s" in out
        none = h.search(property="measurand/decode-throughput", q="quant=Q2").body
        assert none.startswith("no measures match")

    def test_best_over_a_quest_the_model_serves(
        self, store: Store, w: dict[str, Any], llm: dict[str, Any]
    ) -> None:
        store.add_link(
            src_ref_id=llm["model"], dst_ref_id=w["quest"], relation="serves"
        )
        (g,) = store.best_measure(llm["decode"], serving=w["quest"])
        assert g["best"]["literal"] == "38.5"


# ── the handler ────────────────────────────────────────────────────────────


@pytest.fixture
def h(store: Store) -> MeasureHandler:
    return MeasureHandler(hub=Hub(store=store))


class TestSearchHandler:
    def test_range_search_with_unit_and_default_display_unit(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        for lit in ("0.9", "1.4", "2.1"):
            _put(store, w, "bond", lit, unit="Å")
        out = h.search(
            property="measurand/bond-length", min=1.0, max=1.5, unit="Å"
        ).body
        assert out.startswith("# 1 measure(s)")
        assert "1.4 Å" in out and "2.1" not in out and "0.9 Å" not in out
        # no unit=: bounds read in the taxon's display unit (Å)
        same = h.search(property="measurand/bond-length", min=1.0, max=1.5).body
        assert same.splitlines()[1:] == out.splitlines()[1:]
        # another unit: bounds and output both follow it
        nm = h.search(
            property="measurand/bond-length", min=0.1, max=0.15, unit="nm"
        ).body
        assert "0.14 nm" in nm and "1.4 Å" not in nm
        # the output never reads 0.00000000014 m
        assert "e-10" not in out and "0.0000" not in out

    def test_property_resolves_like_taxon_addresses(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        _put(store, w, "cc_bond", "1.54", unit="Å")
        for prop in (
            f"tn{w['bond']}",
            f"taxon:{w['bond']}",
            str(w["bond"]),
            "bond length",
        ):
            assert "1.54 Å" in h.search(property=prop).body, prop

    def test_a_bound_needs_a_property_and_a_matching_unit(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        with pytest.raises(BadInput, match="need property"):
            h.search(min=1.0)
        with pytest.raises(BadInput, match="needs property=, or q="):
            h.search()
        with pytest.raises(BadInput, match="above max"):
            h.search(property=str(w["bond"]), min=2.0, max=1.0)
        with pytest.raises(BadInput):
            h.search(property=str(w["bond"]), min=1.0, unit="V")
        with pytest.raises(BadInput, match="status"):
            h.search(property=str(w["bond"]), status="bogus")

    def test_status_all_marks_left_out_rows(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        old = _put(store, w, "bond", "1", unit="Å")
        _put(store, w, "bond", "1.1", unit="Å", supersedes=old.output_id)
        _put(store, w, "bond", "9", unit="Å", measurand_status="ambiguous")
        live = h.search(property=str(w["bond"])).body
        assert "1.1 Å" in live and "9 Å" not in live and "superseded" not in live
        both = h.search(property=str(w["bond"]), status="all").body
        assert "superseded by" in both and "ambiguous measurand" in both
        assert both.startswith("# 3 measure(s)")

    def test_lines_carry_subject_value_conditions_tier_and_paper(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        _fe_run(store, w, "95", "-0.5")
        out = h.search(property="measurand/faradaic-efficiency", min=90, unit="%").body
        line = out.splitlines()[1]
        assert line.startswith("mx")
        assert "Cu NWA" in line and "Faradaic efficiency: 95 %" in line
        assert "product=NH3" in line and "potential=-0.5 V vs RHE" in line
        assert "tier unknown" in line
        assert line.endswith("—")  # no anchor: no source paper handle
        assert "vs RHE" in line

    def test_pagination(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        for i in range(5):
            _put(store, w, "bond", f"1.{i}", unit="Å")
        out = h.search(property=str(w["bond"]), page_size=2).body
        assert len(out.splitlines()) == 4 and "3 more: page=2" in out
        assert (
            len(
                h.search(property=str(w["bond"]), page_size=2, page=3).body.splitlines()
            )
            == 2
        )

    def test_interval_and_bound_values_render(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        _put(store, w, "bond", "1.8–2.2", unit="Å")
        _put(store, w, "bond", "<3", unit="Å")
        _put(store, w, "bond", "9.6 ± 1.7", unit="Å")
        _put(store, w, "bond", "Cordierite")
        out = h.search(property=str(w["bond"])).body
        assert "1.8–2.2 Å" in out and "< 3 Å" in out and "9.6 ± 1.7 Å" in out
        assert "Cordierite" in out


class TestDispatchSurface:
    @pytest.fixture
    def mounted(self, runtime_with_store: Any) -> Any:
        from precis.tools import core

        core._runtime = runtime_with_store
        try:
            yield core
        finally:
            core._runtime = None

    @staticmethod
    def _text(out: Any) -> str:
        content = getattr(out, "content", None)
        return content[0].text if content else str(out)

    def test_unit_is_a_declared_search_param(self) -> None:
        import inspect

        from precis.tools import core

        assert "unit" in inspect.signature(core.search).parameters
        assert "`unit=`" in (core.search.__doc__ or "")

    def test_search_through_the_verb(
        self, store: Store, w: dict[str, Any], mounted: Any
    ) -> None:
        _put(store, w, "bond", "1.4", unit="Å")
        out = self._text(
            mounted.search(
                kind="measure",
                property="measurand/bond-length",
                min=1.0,
                max=1.5,
                unit="Å",
            )
        )
        assert "1.4 Å" in out and "error" not in out.lower()

    def test_other_kinds_refuse_unit_and_measure_refuses_unknown_facets(
        self, store: Store, w: dict[str, Any], mounted: Any
    ) -> None:
        out = self._text(mounted.search(kind="memory", q="x", unit="Å"))
        assert "error" in out.lower() and "unit" in out
        out = self._text(
            mounted.search(
                kind="measure", property="measurand/bond-length", maturity="lab"
            )
        )
        assert "error" in out.lower() and "maturity" in out

    def test_get_by_id_and_by_handle(
        self, store: Store, w: dict[str, Any], mounted: Any
    ) -> None:
        run = _put(store, w, "bond", "1.4", unit="Å")
        for ident in (run.output_id, str(run.output_id), f"mx{run.output_id}"):
            out = self._text(mounted.get(kind="measure", id=ident))
            assert f"# mx{run.output_id}" in out and "literal: 1.4" in out, ident
        out = self._text(mounted.get(kind="measure", id=f"mx{run.output_id}"))
        assert "1.4 Å" in out
        # the handle alone (no kind=) routes to the measure kind
        assert "literal: 1.4" in self._text(mounted.get(id=f"mx{run.output_id}"))

    def test_fisheye_is_unsupported_with_a_reason(
        self, store: Store, w: dict[str, Any], mounted: Any
    ) -> None:
        run = _put(store, w, "bond", "1.4", unit="Å")
        out = self._text(mounted.get(kind="measure", id=run.output_id, view="fisheye"))
        assert "Unsupported" in out and "not a node" in out
        with pytest.raises(Unsupported, match="not a node"):
            MeasureHandler(hub=Hub(store=store)).get(
                id=run.output_id, view="fisheye+1hop"
            )
        with pytest.raises(Unsupported):
            MeasureHandler(hub=Hub(store=store)).get(
                id=run.output_id, view="kwd+recall"
            )

    def test_fisheye_totality_over_the_registry(self) -> None:
        """Every registered handler either has the ladder or says why not."""
        assert MeasureHandler.spec.is_numeric and MeasureHandler.spec.kind == "measure"


class TestGetRow:
    def _measured(self, store: Store, w: dict[str, Any]) -> Any:
        return _put(
            store,
            w,
            "fe",
            "95",
            unit="%",
            reference="RHE",
            tier="measured",
            source_attribution="own_work",
            anchor=MeasureAnchor(w["paper"], w["chunk"], "sentence", "s1"),
            conds=(
                _cond(w, "product", "NH3", label="product"),
                _cond(w, "potential", "-0.5", unit="V", ref="RHE"),
            ),
        )

    def test_renders_everything_a_review_needs(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        run = self._measured(store, w)
        out = h.get(id=run.output_id).body
        assert "literal: 95 (reported unit: %)" in out
        assert "value: 95 %   SI: 0.95" in out
        assert (
            f"measurand: tn{w['fe']} Faradaic efficiency (measurand/faradaic-efficiency)"
            in out
        )
        assert f"subject: pa{w['paper']} Deep paper" in out
        assert "subject label: Cu NWA" in out and f"run: {run.run_key}" in out
        assert "  - product=NH3" in out and "potential=-0.5 V vs RHE" in out
        assert "tier: measured · attribution: own_work" in out
        assert "extraction: anchor_matched" in out
        assert (
            f"anchor: pa{w['paper']} Deep paper · pc{w['chunk']} · sentence: s1" in out
        )
        assert "reviews:\n  (none)" in out and "supersession: none" in out
        assert "written: " in out and out.count("Z") >= 1

    def test_a_current_and_a_stale_review(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        run = self._measured(store, w)
        store.record_target_review(
            "measure", run.output_id, actor="alice", verdict="approved"
        )
        store.record_target_review(
            "measure",
            run.output_id,
            actor="checker",
            model="big-model",
            version="v2",
            verdict="rejected",
            note="wrong column",
        )
        out = h.get(id=run.output_id).body
        assert (
            "approved by alice, version 0" in out
            and "current" in out
            and "STALE" not in out
        )
        assert (
            "rejected by checker / big-model, version v2" in out
            and "— wrong column" in out
        )
        # deleting the anchoring chunk changes the frozen anchor link: every review goes stale
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (w["paper"],)
            )
        out = h.get(id=run.output_id).body
        assert out.count("STALE") == 2 and "current" not in out.split("reviews:")[1]
        assert "anchor: LOST" in out and "flags: anchor lost" in out

    def test_one_current_one_stale_on_the_same_row(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        run = self._measured(store, w)
        store.record_target_review(
            "measure", run.output_id, actor="old", verdict="approved"
        )
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (w["paper"],)
            )
        store.record_target_review(
            "measure", run.output_id, actor="new", verdict="approved"
        )
        out = h.get(id=run.output_id).body
        assert (
            "approved by new, version 0" in out and "approved by old, version 0" in out
        )
        lines = {
            ln.split(" by ")[1].split(",")[0]: ln
            for ln in out.splitlines()
            if "version 0" in ln
        }
        assert lines["new"].rstrip().endswith("current")
        assert "STALE" in lines["old"]

    def test_the_supersession_chain(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        a = _put(store, w, "bond", "1.4", unit="Å")
        b = _put(store, w, "bond", "1.5", unit="Å", supersedes=a.output_id)
        c = _put(store, w, "bond", "1.6", unit="Å", supersedes=b.output_id)
        out = h.get(id=b.output_id).body
        assert (
            f"supersession: {a.output_id} (superseded) -> [{b.output_id}] (superseded)"
            f" -> {c.output_id}" in out
        )
        assert "flags: superseded by" in out

    def test_missing_and_malformed_ids(self, store: Store, h: MeasureHandler) -> None:
        from precis.errors import NotFound

        with pytest.raises(NotFound, match="measure 999999 not found"):
            h.get(id=999999)
        with pytest.raises(BadInput, match="integer or an mx handle"):
            h.get(id="abc")
        with pytest.raises(BadInput, match="needs id"):
            h.get()
        with pytest.raises(BadInput, match="no view"):
            h.get(id=1, view="bogus")

    def test_escalation_is_shown(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        run = _put(store, w, "fe", "99", unit="%")
        out = h.get(id=run.output_id).body
        assert "flags: escalated: required_condition_missing" in out
        assert "escalation: {'rule': 'required_condition_missing'" in out


# ── the quest view ─────────────────────────────────────────────────────────


class TestQuestMeasuresView:
    def _view(self, store: Store, quest: int) -> str:
        return QuestHandler(hub=Hub(store=store)).get(id=quest, view="measures").body

    def test_best_per_group_over_everything_serving(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        """AC 8: the quest table is a plain select over measures."""
        _fe_run(store, w, "61", "-0.9", subject=w["paper"])
        best = _fe_run(store, w, "95", "-0.5", subject=w["paper2"])
        _fe_run(store, w, "99", "-0.5", subject=w["stray"])
        _put(store, w, "u_l", "-0.30", unit="V", reference="RHE")
        _put(store, w, "u_l", "-0.80", unit="V", reference="SHE")
        _put(store, w, "unranked", "3", unit="nm")
        out = self._view(store, w["quest"])
        lines = out.splitlines()
        assert lines[0].startswith(f"# measures — quest {w['quest']}: NO to N")
        fe = [ln for ln in lines if ln.startswith("Faradaic efficiency")]
        assert len(fe) == 1
        assert "95 %" in fe[0] and "99 %" not in fe[0] and "61 %" not in fe[0]
        assert f"mx{best.output_id}" in fe[0]
        assert "product=NH3" in fe[0] and "potential=-0.5 V vs RHE" in fe[0]
        assert "higher is better, n=2" in fe[0]
        # two references are two lines, never compared
        ul = [ln for ln in lines if ln.startswith("limiting potential")]
        assert len(ul) == 2
        assert any("vs RHE: -0.3 V" in ln for ln in ul)
        assert any("vs SHE: -0.8 V" in ln for ln in ul)
        # a measurand with no direction says so
        assert any(ln.startswith("particle size: 1 row(s), no best") for ln in lines)

    def test_a_quest_with_nothing_says_so(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        assert "no live measures" in self._view(store, w["quest"])

    def test_view_is_listed_among_the_quest_views(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        with pytest.raises(Unsupported) as ei:
            QuestHandler(hub=Hub(store=store)).get(id=w["quest"], view="deeds")
        assert "measures" in str(ei.value) or "measures" in " ".join(
            getattr(ei.value, "options", []) or []
        )


# ── Build C: the write verbs ───────────────────────────────────────────────


class TestPutRun:
    @staticmethod
    def _meta(w: dict[str, Any], **over: Any) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "measurand": "measurand/faradaic-efficiency",
            "subject": f"pa{w['paper']}",
            "subject_label": "Cu NWA",
            "subject_group": "fi1",
            "reported_unit": "%",
            "reference": "RHE",
            "tier": "measured",
            "anchor": {
                "chunk": f"pc{w['chunk']}",
                "anchor_scheme": "sentence",
                "span": "s1",
            },
        }
        meta.update(over)
        return meta

    @staticmethod
    def _items() -> list[dict[str, Any]]:
        return [
            {
                "measurand": "measurand/potential",
                "literal": "-0.5",
                "reported_unit": "V",
                "reference": "RHE",
            },
            {
                "measurand": "measurand/reaction-product",
                "text": "NH3",
                "condition": "product",
            },
        ]

    def test_round_trip_output_and_two_inputs(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        body = h.put(text="95", meta=self._meta(w), items=self._items()).body
        ids = [int(m) for m in re.findall(r"^mx(\d+) ", body, re.M)]
        assert len(ids) == 3, body
        assert body.splitlines()[0].startswith("run run:")
        rows = [store.measure_detail(i) for i in ids]
        assert len({r["run_key"] for r in rows}) == 1
        out, pot, prod = rows
        assert (out["direction"], pot["direction"], prod["direction"]) == (
            "output",
            "input",
            "input",
        )
        assert out["value_num"] == pytest.approx(0.95)
        assert pot["value_num"] == pytest.approx(-0.5)
        assert (out["subject"], out["subject_group"]) == ("Cu NWA", "fi1")
        assert pot["subject_group"] == "fi1"  # inherited from the output
        assert prod["literal"] == "NH3" and prod["meta"]["condition"] == "product"
        assert out["extraction_status"] == "anchor_matched"
        assert out["anchor"]["chunk_id"] == w["chunk"]
        assert out["actor"] == "agent"
        # the required conditions were met by the inputs: no escalation flag
        assert "escalation" not in (out["meta"] or {})
        # the product 'NH3' is not in the chunk: the inherited anchor flags it
        assert f"mx{prod['id']} anchor_mismatch" in body and "flagged:" in body

    def test_an_explicit_run_key_and_model_are_kept(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        body = h.put(text="95", meta=self._meta(w, run_key="fi1#1", model="mid-1")).body
        assert body.startswith("run fi1#1:")
        (mid,) = [int(m) for m in re.findall(r"^mx(\d+) ", body, re.M)]
        assert store.measure_detail(mid)["model"] == "mid-1"

    def test_missing_required_condition_is_flagged_in_the_answer(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        body = h.put(text="95", meta=self._meta(w)).body
        assert "escalation required_condition_missing" in body
        assert "condition=potential" in body

    def test_a_missing_measurand_is_refused_naming_the_field(
        self, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        meta = self._meta(w)
        del meta["measurand"]
        with pytest.raises(BadInput, match=r"meta\.measurand") as ei:
            h.put(text="95", meta=meta)
        assert "put(kind='measure'" in (ei.value.next or "")

    def test_an_input_error_names_its_index(
        self, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        with pytest.raises(BadInput, match=r"items\[1\]\.measurand"):
            h.put(
                text="95",
                meta=self._meta(w),
                items=[
                    {"measurand": "measurand/potential", "literal": "-0.5"},
                    {"literal": "NH3"},
                ],
            )
        with pytest.raises(BadInput, match=r"items\[0\]: unknown field"):
            h.put(
                text="95",
                meta=self._meta(w),
                items=[{"measurand": "measurand/potential", "literal": "1", "x": 1}],
            )

    def test_other_field_errors_are_named(
        self, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        with pytest.raises(BadInput, match=r"meta\.value_num"):
            h.put(text="95", meta=self._meta(w, value_num="x"))
        with pytest.raises(BadInput, match=r"meta\.subject"):
            h.put(text="95", meta=self._meta(w, subject="nonsense"))
        with pytest.raises(BadInput, match=r"meta\.anchor\.chunk"):
            h.put(
                text="95",
                meta=self._meta(
                    w, anchor={"chunk": "zz9", "anchor_scheme": "x", "span": "s"}
                ),
            )
        with pytest.raises(BadInput, match="unknown field"):
            h.put(text="95", meta=self._meta(w, bogus=1))
        with pytest.raises(BadInput, match="literal"):
            h.put(text="  ", meta=self._meta(w))
        with pytest.raises(BadInput, match="append-only"):
            h.put(text="95", meta=self._meta(w), id=3)

    def test_measured_without_an_anchor_is_refused(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        meta = self._meta(w)
        del meta["anchor"]
        with pytest.raises(BadInput, match="anchored primary edge"):
            h.put(text="95", meta=meta)
        # a refusal wrote nothing
        assert store.search_measures(w["fe"], include_all=True).rows == []

    def test_supersedes_through_put(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        first = h.put(text="95", meta=self._meta(w)).body
        found = re.search(r"^mx(\d+) ", first, re.M)
        assert found is not None
        old = int(found.group(1))
        h.put(text="96", meta=self._meta(w, supersedes=f"mx{old}"))
        assert store.measure_detail(old)["superseded_by"] is not None


def _output_id(body: str) -> int:
    found = re.search(r"^mx(\d+) output", body, re.M)
    assert found is not None, body
    return int(found.group(1))


class TestPutEditThroughTheVerbs:
    @pytest.fixture
    def verbs(self, runtime_with_store: Any) -> Any:
        from precis.tools import core

        core._runtime = runtime_with_store
        try:
            yield core
        finally:
            core._runtime = None

    @staticmethod
    def _text(out: Any) -> str:
        content = getattr(out, "content", None)
        return content[0].text if content else str(out)

    def _put(self, verbs: Any, w: dict[str, Any], **kw: Any) -> str:
        return self._text(
            verbs.put(
                kind="measure",
                text="95",
                meta=TestPutRun._meta(w),
                items=[
                    {
                        "measurand": "measurand/potential",
                        "literal": "-0.5",
                        "reported_unit": "V",
                    }
                ],
                **kw,
            )
        )

    def test_put_reason_reaches_the_revision_context(
        self,
        store: Store,
        w: dict[str, Any],
        verbs: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from precis.store.revision_context import current_revision_context

        seen: dict[str, Any] = {}
        real = Store.insert_measure

        def spy(self: Store, *a: Any, **kw: Any) -> Any:
            ctx = current_revision_context()
            seen["ctx"] = ctx and (ctx.reason, ctx.actor)
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT current_setting('precis.reason', true)"
                ).fetchone()
                seen["guc"] = row[0] if row else None
            return real(self, *a, **kw)

        monkeypatch.setattr(Store, "insert_measure", spy)
        out = self._put(verbs, w, reason="qu202467 pilot extraction")
        assert out.startswith("run run:"), out
        assert seen["ctx"] == ("qu202467 pilot extraction", "agent")
        assert seen["guc"] == "qu202467 pilot extraction"
        # no reason=: the verb names itself, as for edit
        self._put(verbs, w)
        assert seen["ctx"] == ("put(kind='measure')", "agent")

    def test_model_review_lands_current_and_shows_in_get(
        self, store: Store, w: dict[str, Any], verbs: Any
    ) -> None:
        mid = _output_id(self._put(verbs, w))
        out = self._text(
            verbs.edit(
                kind="measure",
                id=f"mx{mid}",
                review="model",
                verdict="approved",
                text="95 is printed in the anchor",
                meta={"model": "opus-5"},
            )
        )
        assert out.startswith(f"reviewed mx{mid}: review ") and "approved" in out
        (rv,) = store.reviews_for("measure", mid)
        assert (rv.verdict, rv.actor, rv.model, rv.current) == (
            "approved",
            "agent",
            "opus-5",
            True,
        )
        assert rv.content_sha[:12] in out
        shown = self._text(verbs.get(kind="measure", id=mid))
        assert "approved by agent / opus-5" in shown and "current" in shown
        assert "95 is printed in the anchor" in shown

    def test_two_reviews_append(
        self, store: Store, w: dict[str, Any], verbs: Any
    ) -> None:
        mid = _output_id(self._put(verbs, w))
        for verdict in ("rejected", "approved"):
            verbs.edit(
                kind="measure",
                id=mid,
                review="model",
                verdict=verdict,
                meta={"model": "opus-5"},
            )
        assert [r.verdict for r in store.reviews_for("measure", mid)] == [
            "approved",
            "rejected",
        ]

    def test_a_model_review_without_a_model_is_refused(
        self, store: Store, w: dict[str, Any], verbs: Any
    ) -> None:
        mid = _output_id(self._put(verbs, w))
        out = self._text(verbs.edit(kind="measure", id=mid, review="model"))
        assert "error" in out.lower(), out
        assert "a model review must name its model" in out
        assert store.reviews_for("measure", mid) == []

    def test_a_human_review_over_the_verb_names_the_person(
        self, store: Store, w: dict[str, Any], verbs: Any
    ) -> None:
        mid = _output_id(self._put(verbs, w))
        out = self._text(
            verbs.edit(
                kind="measure",
                id=mid,
                review="human",
                verdict="approved",
                meta={"actor": "reto"},
            )
        )
        assert f"reviewed mx{mid}" in out and "by reto" in out, out
        (rv,) = store.reviews_for("measure", mid)
        assert (rv.actor, rv.model, rv.current) == ("reto", None, True)
        # no meta.actor: the single human identity, as for draft
        verbs.edit(kind="measure", id=mid, review="human")
        assert store.reviews_for("measure", mid)[0].actor == "human"
        out = self._text(
            verbs.edit(
                kind="measure",
                id=mid,
                review="human",
                meta={"actor": "reto", "model": "opus-5"},
            )
        )
        assert "a human review has no model" in out

    def test_a_non_review_edit_is_refused(
        self, store: Store, w: dict[str, Any], verbs: Any
    ) -> None:
        mid = _output_id(self._put(verbs, w))
        for kw in (
            {"mode": "replace", "text": "96"},
            {"meta": {"trusted": True}},
            {"find": "95", "text": "96"},
        ):
            out = self._text(verbs.edit(kind="measure", id=mid, **kw))
            assert "error" in out.lower() and "append-only" in out, out
            assert "supersedes" in out
        assert store.measure_detail(mid)["literal"] == "95"


class TestReviewHandler:
    @pytest.fixture
    def mid(self, store: Store, w: dict[str, Any], h: MeasureHandler) -> int:
        return _output_id(h.put(text="95", meta=TestPutRun._meta(w)).body)

    def test_a_person_may_review_without_a_model(
        self, store: Store, h: MeasureHandler, mid: int
    ) -> None:
        from precis.store.revision_context import revision_context

        with revision_context("manual", actor="reto"):
            out = h.edit(id=mid, review="human", verdict="rejected", text="why").body
        (rv,) = store.reviews_for("measure", mid)
        assert (rv.actor, rv.model, rv.verdict, rv.note) == (
            "reto",
            None,
            "rejected",
            "why",
        )
        assert f"review {rv.review_id} rejected by reto" in out

    def test_a_person_names_no_model(self, h: MeasureHandler, mid: int) -> None:
        from precis.store.revision_context import revision_context

        with revision_context("manual", actor="reto"):
            with pytest.raises(BadInput, match="a human review has no model"):
                h.edit(id=mid, review="human", meta={"model": "x"})
            with pytest.raises(BadInput, match="a model review must name its model"):
                h.edit(id=mid, review="model")
            with pytest.raises(BadInput, match="meta.actor names the person"):
                h.edit(id=mid, review="model", meta={"model": "m", "actor": "reto"})

    def test_bad_verdicts_and_ids(self, h: MeasureHandler, mid: int) -> None:
        with pytest.raises(BadInput, match="approved or rejected"):
            h.edit(id=mid, review="model", verdict="retract", meta={"model": "m"})
        with pytest.raises(BadInput, match="'human' or 'model'"):
            h.edit(id=mid, review="cites", meta={"model": "m"})
        with pytest.raises(BadInput, match="needs id"):
            h.edit(review="model", meta={"model": "m"})
        with pytest.raises(NotFound, match="not found"):
            h.edit(id=999999, review="model", meta={"model": "m"})
