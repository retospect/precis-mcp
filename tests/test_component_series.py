"""The `component` standards-series registry and its mint path
(``docs/backlog/se-off-the-shelf-fabrication.md`` engine 1, rung 2).

Two halves, deliberately separated: :mod:`precis.component_series` is pure
(a data file plus a resolver, no store, no network), so it is tested
directly; the handler half is tested through ``put``/``get`` because the
contract that matters is what an agent can call.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from precis import component_series as cseries
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.component import ComponentHandler
from precis.tools import core as tools_core


def _handler(store: Any) -> ComponentHandler:
    return ComponentHandler(hub=Hub(store=store))


# ── the data file ───────────────────────────────────────────────────


class TestRegistryData:
    def test_the_seeded_series_load(self) -> None:
        ids = {s.series_id for s in cseries.load_series()}
        assert {"iso-4762", "iso-4032", "iso-7089", "en-10255-medium"} <= ids

    def test_every_series_has_sizes_and_provenance(self) -> None:
        for s in cseries.load_series():
            assert s.sizes, f"{s.series_id} has no size table"
            assert s.source, f"{s.series_id} has no source"
            assert s.retrieved, f"{s.series_id} has no retrieved date"

    def test_a_length_series_stocks_lengths_and_a_lengthless_one_does_not(
        self,
    ) -> None:
        screw = cseries.find_series("iso-4762")
        nut = cseries.find_series("iso-4032")
        assert screw is not None and nut is not None
        assert screw.length_spec == "length"
        assert all(s.lengths for s in screw.sizes)
        assert nut.length_spec is None
        assert all(s.lengths == () for s in nut.sizes)

    def test_acrylic_sheet_declares_no_designation(self) -> None:
        """A stock range is not a standard, and the file says so rather
        than inventing a designation for it."""
        sheet = cseries.find_series("acrylic-sheet-cast")
        assert sheet is not None
        assert sheet.designation is None
        assert "not a standard" in sheet.source

    def test_every_spec_id_in_the_file_is_a_registered_spec(self, store: Any) -> None:
        """The file is curated data — an unknown spec_id there is a data
        bug that would otherwise surface only as a silently-skipped
        dimension at mint time."""
        unknown: set[str] = set()
        for s in cseries.load_series():
            spec_ids = set(s.specs)
            for row in s.sizes:
                spec_ids |= set(row.specs)
            if s.length_spec:
                spec_ids.add(s.length_spec)
            for spec_id in spec_ids:
                if store.component_spec_get(spec_id) is None:
                    unknown.add(f"{s.series_id}:{spec_id}")
        assert not unknown

    def test_every_series_category_is_registered(self, store: Any) -> None:
        for s in cseries.load_series():
            assert store.component_category_get(s.category) is not None, s.series_id


# ── pure helpers ────────────────────────────────────────────────────


class TestNormalize:
    @pytest.mark.parametrize("raw", ["M6x30", "m6 x 30", "M6×30", "M6_X_30"])
    def test_separators_and_case_fold_away(self, raw: str) -> None:
        assert cseries.normalize(raw) == "m630"


class TestSplitDesignation:
    @pytest.mark.parametrize(
        ("raw", "key", "length"),
        [
            ("M6x30", "M6", 30.0),
            ("M6 x 30", "M6", 30.0),
            ("M6×30", "M6", 30.0),
            ("M6", "M6", None),
            ("DN25x1000", "DN25", 1000.0),
            ("3mm", "3mm", None),
            ("DN15", "DN15", None),
            ("M8x12.5", "M8", 12.5),
        ],
    )
    def test_only_a_trailing_bare_number_is_a_length(
        self, raw: str, key: str, length: float | None
    ) -> None:
        assert cseries.split_designation(raw) == (key, length)


class TestFindSeries:
    def test_by_id(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None and s.series_id == "iso-4762"

    def test_by_designation(self) -> None:
        s = cseries.find_series("ISO 4762")
        assert s is not None and s.series_id == "iso-4762"

    def test_unknown_returns_none_rather_than_raising(self) -> None:
        assert cseries.find_series("iso-9999") is None

    def test_size_lookup_is_separator_insensitive(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None
        assert s.size("m6") is not None
        assert s.size("M6") is not None
        assert s.size("M99") is None


class TestMintSpecsAndNaming:
    def test_size_specs_win_over_series_specs_and_length_is_added(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        specs = cseries.mint_specs(s, row, 30.0)
        assert specs["drive_type"] == "socket"  # series level
        assert specs["head_diameter"] == 10.0  # size level
        assert specs["length"] == 30.0  # under length_spec

    def test_a_lengthless_series_records_no_length(self) -> None:
        s = cseries.find_series("iso-4032")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        assert "length" not in cseries.mint_specs(s, row, 30.0)

    def test_slug_and_title_are_deterministic(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        assert cseries.suggest_slug(s, row, 30.0) == "iso-4762-m6x30"
        title = cseries.title_for(s, row, 30.0)
        assert title.startswith("M6x30 ")
        assert "(ISO 4762)" in title

    def test_a_lengthless_size_omits_the_length_from_slug_and_title(self) -> None:
        s = cseries.find_series("iso-4032")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        assert cseries.suggest_slug(s, row, None) == "iso-4032-m6"
        assert cseries.title_for(s, row, None).startswith("M6 ")

    def test_a_series_without_a_designation_gets_no_suffix(self) -> None:
        s = cseries.find_series("acrylic-sheet-cast")
        assert s is not None
        row = s.size("3mm")
        assert row is not None
        assert "(" not in cseries.title_for(s, row, None)


class TestToCanonical:
    def test_a_mm_spec_passes_through(self) -> None:
        assert cseries.to_canonical(30.0, canonical_unit="mm", dimension="length") == (
            30.0,
            None,
        )

    def test_a_metres_spec_is_converted(self) -> None:
        assert cseries.to_canonical(2000.0, canonical_unit="m", dimension="length") == (
            2.0,
            None,
        )

    def test_a_non_length_passes_through_untouched(self) -> None:
        """The file states mm for lengths and nothing for anything else —
        so a mass or a load rating is not something it can be wrong
        about."""
        assert cseries.to_canonical(5.0, canonical_unit="N", dimension="force") == (
            5.0,
            None,
        )

    def test_a_unitless_length_passes_through(self) -> None:
        assert cseries.to_canonical(5.0, canonical_unit=None, dimension="length") == (
            5.0,
            None,
        )

    def test_an_unreachable_length_unit_yields_no_number(self) -> None:
        """Better to skip the value and say so than to write an
        unconverted figure into the wrong column."""
        value, complaint = cseries.to_canonical(
            30.0, canonical_unit="inch", dimension="length"
        )
        assert value is None
        assert complaint is not None and "cannot convert" in complaint


class TestCheckLength:
    def test_stocked_length_is_silent(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        assert cseries.check_length(s, row, 30.0) is None

    def test_off_list_length_warns_with_the_nearest_stocked_one(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        note = cseries.check_length(s, row, 31.0)
        assert note is not None
        assert "not a stocked length" in note
        assert "nearest 30" in note

    def test_missing_length_on_a_length_series_complains(self) -> None:
        s = cseries.find_series("iso-4762")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        note = cseries.check_length(s, row, None)
        assert note is not None and "needs a length" in note

    def test_a_length_on_a_lengthless_series_is_reported_ignored(self) -> None:
        s = cseries.find_series("iso-4032")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        note = cseries.check_length(s, row, 30.0)
        assert note is not None and "no length axis" in note

    def test_a_lengthless_series_with_no_length_is_silent(self) -> None:
        s = cseries.find_series("iso-4032")
        assert s is not None
        row = s.size("M6")
        assert row is not None
        assert cseries.check_length(s, row, None) is None


# ── the resolver ────────────────────────────────────────────────────


class TestResolve:
    def test_colloquial_query_ranks_the_right_series_and_size_first(self) -> None:
        hits = cseries.resolve("M6x30 socket cap")
        assert hits
        assert hits[0].series.series_id == "iso-4762"
        assert hits[0].size.key == "M6"
        assert hits[0].length == 30.0

    def test_a_nut_query_does_not_acquire_a_length(self) -> None:
        hits = cseries.resolve("M8 hex nut")
        assert hits
        assert hits[0].series.series_id == "iso-4032"
        assert hits[0].length is None

    def test_plexiglass_reaches_the_sheet_series_by_alias(self) -> None:
        hits = cseries.resolve("3mm plexiglass")
        assert hits
        assert hits[0].series.series_id == "acrylic-sheet-cast"
        assert hits[0].size.key == "3mm"

    def test_an_off_list_length_ranks_below_a_stocked_one(self) -> None:
        stocked = cseries.resolve("M6x30 socket cap")
        odd = cseries.resolve("M6x31 socket cap")
        assert stocked[0].score > odd[0].score

    def test_no_match_returns_empty_rather_than_guessing(self) -> None:
        assert cseries.resolve("titanium unobtainium widget") == []

    def test_blank_query_returns_empty(self) -> None:
        assert cseries.resolve("   ") == []

    def test_why_names_the_tokens_that_matched(self) -> None:
        hits = cseries.resolve("M6x30 socket cap")
        assert "M6" in hits[0].why

    def test_limit_is_honoured(self) -> None:
        hits = cseries.resolve("M6", limit=2)
        assert len(hits) <= 2

    def test_a_size_only_mentioned_in_passing_still_ranks_below_the_subject(
        self,
    ) -> None:
        """ "M6 washers for the M8 frame" must offer M6 first without
        hiding M8 — the low-confidence hit is demoted, not dropped."""
        hits = cseries.resolve("M6 washer")
        washers = [h for h in hits if h.series.series_id == "iso-7089"]
        assert washers and washers[0].size.key == "M6"


# ── view='series' ───────────────────────────────────────────────────


class TestSeriesViews:
    def test_index_lists_every_series(self, store: Any) -> None:
        body = _handler(store).get(view="series").body
        assert "iso-4762" in body
        assert "en-10255-medium" in body
        assert "ISO 4762" in body

    def test_one_series_renders_its_size_table(self, store: Any) -> None:
        body = _handler(store).get(id="iso-4762", view="series").body
        assert "head_diameter" in body
        assert "M6" in body
        assert "lengths" in body
        assert "ISO 4762:2004" in body

    def test_a_lengthless_series_has_no_lengths_column(self, store: Any) -> None:
        body = _handler(store).get(id="iso-4032", view="series").body
        assert "across_flats" in body
        assert "lengths" not in body

    def test_a_size_without_a_spec_renders_an_absence_not_a_zero(
        self, store: Any
    ) -> None:
        body = _handler(store).get(id="iso-4017", view="series").body
        assert "—" in body  # M5..M20 carry no head_diameter (hex head)

    def test_unknown_series_raises_naming_the_known_ones(self, store: Any) -> None:
        with pytest.raises(NotFound) as exc:
            _handler(store).get(id="iso-9999", view="series")
        assert "iso-4762" in str(exc.value)

    def test_q_resolves_a_colloquial_name_to_candidates(self, store: Any) -> None:
        body = _handler(store).get(view="series", q="M6x30 socket cap").body
        assert "iso-4762" in body
        assert "M6x30" in body
        assert "Ranked, not picked" in body

    def test_q_with_no_match_says_so_without_offering_a_part(self, store: Any) -> None:
        body = _handler(store).get(view="series", q="unobtainium widget").body
        assert "no series matches" in body
        assert "iso-4762" not in body

    def test_q_flags_an_off_list_length_in_the_note_column(self, store: Any) -> None:
        body = _handler(store).get(view="series", q="M6x31 socket cap").body
        assert "not a stocked length" in body


# ── minting ─────────────────────────────────────────────────────────


class TestMint:
    def test_mint_creates_the_entity_with_a_derived_slug_and_dimensions(
        self, store: Any
    ) -> None:
        h = _handler(store)
        resp = h.put(series="iso-4762", size="M6x30")
        assert "iso-4762-m6x30" in resp.body
        ref = store.get_ref(kind="component", id="iso-4762-m6x30")
        assert ref is not None
        assert ref.meta["category"] == "fastener"
        assert ref.meta["series"] == "iso-4762"
        assert ref.meta["size"] == "M6x30"
        assert ref.meta["designation"] == "ISO 4762"
        assert ref.meta["uom"] == "each"
        head = store.component_current_spec_value(ref.id, "head_diameter")
        assert head is not None and head["value_num"] == 10.0
        length = store.component_current_spec_value(ref.id, "length")
        assert length is not None and length["value_num"] == 30.0
        drive = store.component_current_spec_value(ref.id, "drive_type")
        assert drive is not None and drive["value_text"] == "socket"

    def test_minted_values_carry_standard_provenance(self, store: Any) -> None:
        h = _handler(store)
        h.put(series="iso-4762", size="M8x20")
        ref = store.get_ref(kind="component", id="iso-4762-m8x20")
        assert ref is not None
        row = store.component_current_spec_value(ref.id, "head_diameter")
        assert row is not None
        assert row["method"] == "standard"
        assert row["maturity"] == "commercial"
        assert "ISO 4762" in (row["notes"] or "")

    def test_re_minting_appends_nothing(self, store: Any) -> None:
        """The fact table is append-only and a BOM rollup reads one
        current value out of it — a second mint must not grow it."""
        h = _handler(store)
        h.put(series="iso-4762", size="M5x16")
        ref = store.get_ref(kind="component", id="iso-4762-m5x16")
        assert ref is not None
        before = len(store.component_values_for_ref(ref.id))
        resp = h.put(series="iso-4762", size="M5x16")
        after = len(store.component_values_for_ref(ref.id))
        assert after == before
        assert "0 written" in resp.body
        assert "already current" in resp.body

    def test_explicit_id_overrides_the_derived_slug(self, store: Any) -> None:
        h = _handler(store)
        h.put(id="frame-bolt", series="iso-4762", size="M10x40")
        assert store.get_ref(kind="component", id="frame-bolt") is not None
        assert store.get_ref(kind="component", id="iso-4762-m10x40") is None

    def test_explicit_title_and_meta_survive(self, store: Any) -> None:
        h = _handler(store)
        h.put(
            id="titled-bolt",
            series="iso-4762",
            size="M4x12",
            title="the long one",
            meta={"mpn": "X-1"},
        )
        ref = store.get_ref(kind="component", id="titled-bolt")
        assert ref is not None
        assert ref.title == "the long one"
        assert ref.meta["mpn"] == "X-1"
        assert ref.meta["series"] == "iso-4762"

    def test_a_lengthless_series_mints_without_one(self, store: Any) -> None:
        h = _handler(store)
        h.put(series="iso-4032", size="M6")
        ref = store.get_ref(kind="component", id="iso-4032-m6")
        assert ref is not None
        af = store.component_current_spec_value(ref.id, "across_flats")
        assert af is not None and af["value_num"] == 10.0

    def test_a_pipe_mints_from_its_nominal_bore(self, store: Any) -> None:
        h = _handler(store)
        h.put(series="en-10255-medium", size="DN25x2000")
        ref = store.get_ref(kind="component", id="en-10255-medium-dn25x2000")
        assert ref is not None
        assert ref.meta["category"] == "pipe"
        od = store.component_current_spec_value(ref.id, "outer_diameter")
        assert od is not None and od["value_num"] == 33.7  # mm spec, verbatim
        # tube length writes to length_overall, not the fastener 'length'
        assert store.component_current_spec_value(ref.id, "length") is None
        overall = store.component_current_spec_value(ref.id, "length_overall")
        # length_overall is the METRES outlier — 2000 mm must not land as
        # 2000 m. This is the whole reason mint converts rather than
        # writing the file's number through.
        assert overall is not None and overall["value_num"] == 2.0

    def test_an_off_list_length_still_mints_but_warns(self, store: Any) -> None:
        """The size table is what suppliers hold, not what physics allows
        — an odd length is a cut charge, not an error."""
        h = _handler(store)
        resp = h.put(series="iso-4762", size="M6x31")
        assert "⚠" in resp.body
        assert "not a stocked length" in resp.body
        ref = store.get_ref(kind="component", id="iso-4762-m6x31")
        assert ref is not None
        length = store.component_current_spec_value(ref.id, "length")
        assert length is not None and length["value_num"] == 31.0

    def test_size_without_series_is_rejected(self, store: Any) -> None:
        with pytest.raises(BadInput) as exc:
            _handler(store).put(id="x", size="M6x30")
        assert "series=" in str(exc.value)

    def test_unknown_series_is_rejected_naming_the_known_ones(self, store: Any) -> None:
        with pytest.raises(BadInput) as exc:
            _handler(store).put(series="iso-9999", size="M6")
        assert "iso-4762" in str(exc.value)

    def test_series_without_size_is_rejected(self, store: Any) -> None:
        with pytest.raises(BadInput) as exc:
            _handler(store).put(series="iso-4762")
        assert "needs size=" in str(exc.value)

    def test_unknown_size_is_rejected_naming_the_size_table(self, store: Any) -> None:
        with pytest.raises(BadInput) as exc:
            _handler(store).put(series="iso-4762", size="M7x30")
        assert "M6" in str(exc.value)

    def test_a_missing_length_on_a_length_series_is_a_hard_miss(
        self, store: Any
    ) -> None:
        """Dimensionless is not a degraded part, it is no part — unlike an
        off-list length, this one refuses."""
        with pytest.raises(BadInput) as exc:
            _handler(store).put(series="iso-4762", size="M6")
        assert "needs a length" in str(exc.value)

    def test_a_spec_whose_unit_cannot_be_reached_is_skipped_not_guessed(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            cseries,
            "to_canonical",
            lambda value, **_kw: (None, "cannot convert mm to 'furlong'"),
        )
        resp = _handler(store).put(series="iso-4032", size="M4")
        assert "skipped" in resp.body
        assert "cannot convert" in resp.body
        ref = store.get_ref(kind="component", id="iso-4032-m4")
        assert ref is not None
        assert store.component_current_spec_value(ref.id, "across_flats") is None
        # the categorical spec doesn't route through the converter at all
        thread = store.component_current_spec_value(ref.id, "thread_size")
        assert thread is not None and thread["value_text"] == "M4"

    def test_an_unregistered_spec_in_the_file_is_reported_not_minted(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A curated data file with a typo must say so — silently minting
        a `proposed` spec would bury the bug."""
        bogus = cseries.Series(
            series_id="test-bogus",
            name="Bogus test series",
            category="fastener",
            source="a fixture, not a standard",
            retrieved="2026-09-05",
            sizes=(
                cseries.SeriesSize(
                    key="B1", specs={"across_flats": 9.0, "not_a_real_spec": 1.0}
                ),
            ),
        )
        monkeypatch.setattr(cseries, "load_series", lambda: (bogus,))
        resp = _handler(store).put(series="test-bogus", size="B1")
        assert "skipped" in resp.body
        assert "not_a_real_spec" in resp.body
        ref = store.get_ref(kind="component", id="test-bogus-b1")
        assert ref is not None
        af = store.component_current_spec_value(ref.id, "across_flats")
        assert af is not None and af["value_num"] == 9.0
        assert store.component_spec_get("not_a_real_spec") is None


# ── the MCP tool boundary ───────────────────────────────────────────


class TestCoreParams:
    """The verb signature IS the advertised MCP schema: an undeclared
    kwarg is stripped by strict-schema clients and silently dropped by the
    dispatcher, so the mint path would be unreachable over MCP while every
    handler test above kept passing."""

    def test_put_signature_advertises_series_and_size(self) -> None:
        sig = inspect.signature(tools_core.put)
        assert "series" in sig.parameters
        assert "size" in sig.parameters

    def test_put_forwards_series_and_size_into_the_dispatch_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
            captured["payload"] = payload
            return "ok"

        monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)
        tools_core.put(kind="component", series="iso-4762", size="M6x30")
        assert captured["payload"]["series"] == "iso-4762"
        assert captured["payload"]["size"] == "M6x30"

    def test_get_signature_advertises_q_for_the_resolver(self) -> None:
        assert "q" in inspect.signature(tools_core.get).parameters
