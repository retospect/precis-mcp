"""JLCPCB capability rule table — the accessor over the packaged data file,
never Python constants (see :mod:`precis.pcb.capabilities`)."""

from __future__ import annotations

import pytest

from precis.pcb import capabilities as caps


def test_loads_both_tiers_for_every_row():
    rows = caps.load_capabilities()
    assert rows  # non-empty
    for row in rows:
        assert row.jlc_min and row.house_default
        assert set(caps.FIELDS) <= set(row.jlc_min)
        assert set(caps.FIELDS) <= set(row.house_default)


#: Fields the flat house_default margin rule deliberately does NOT apply
#: to, each with the reason the JSON ``_note`` and
#: :mod:`precis.pcb.capabilities`' own docstring record. These are not
#: exempt from checking — :func:`test_not_margined_fields_are_equal_not_lower`
#: below pins them to EQUALITY, which is a stricter statement than "at
#: least 10% above", not a hole in the ladder.
_NOT_MARGINED = {
    # A DESIGN value, not a minimum to exceed: it is the swell
    # `gerber.soldermask_gerber` actually draws each mask opening with, so
    # margining it up would enlarge every opening on every board while
    # calling itself safety. Both tiers carry JLC's applied default.
    "soldermask_expansion_mm",
}


def test_house_default_has_genuine_margin_over_jlc_min():
    # The entire point of the two-tier structure: house_default sits
    # *comfortably above* JLC's published minimum, for every field of every
    # row — mere equality defeats the purpose (and was exactly the bug this
    # test now catches). None means "not applicable to this process" (e.g.
    # vias on a single-layer aluminum board) and is skipped, not a violation.
    # Require a strict, non-trivial margin: at least 10% headroom.
    violations = []
    for row in caps.load_capabilities():
        for field in caps.FIELDS:
            if field in _NOT_MARGINED:
                continue
            lo = row.jlc_min.get(field)
            hi = row.house_default.get(field)
            if lo is None or hi is None:
                continue
            if hi <= lo or hi < lo * 1.1:
                violations.append((row.process, field, lo, hi))
    assert violations == []


@pytest.mark.parametrize("field", sorted(_NOT_MARGINED))
def test_not_margined_fields_are_equal_not_lower(field: str):
    """A field off the margin ladder still has a shape to hold: the two
    tiers must be the SAME number. "Not margined" is a statement about the
    kind of quantity it is, and it must never become cover for a
    house_default that quietly drifted BELOW what the fab publishes."""
    for row in caps.load_capabilities():
        lo, hi = row.jlc_min.get(field), row.house_default.get(field)
        assert (lo is None) == (hi is None), (
            f"{row.process}: {field} is published at one tier and null at "
            "the other — a null is 'not applicable to this process', which "
            "cannot be true of one tier only"
        )
        if lo is not None:
            assert hi == lo, f"{row.process}: {field} {hi} != {lo}"


def test_every_row_has_source_and_retrieved_date():
    for row in caps.load_capabilities():
        assert row.source
        assert row.retrieved  # dated — an undated capability table is a trap


def test_capability_for_known_process():
    row = caps.capability_for("4layer")
    assert row.process == "4layer"
    lo = row.jlc_min["trace_width_mm"]
    hi = row.house_default["trace_width_mm"]
    assert lo is not None and hi is not None  # published for 4-layer
    assert lo <= hi


def test_capability_for_unknown_process_raises_with_known_list():
    with pytest.raises(KeyError, match="2layer"):
        caps.capability_for("6layer-rigid-flex")


def test_headroom_is_margin_above_jlc_min():
    row = caps.capability_for("2layer")
    hi = row.house_default["trace_width_mm"]
    lo = row.jlc_min["trace_width_mm"]
    # Narrowing doubles as a real assertion: unlike the aluminum row's
    # unpublished fields, 2-layer trace width IS published, so a None here
    # is a data regression rather than an expected "not applicable".
    assert hi is not None and lo is not None
    h = caps.headroom(row, "trace_width_mm", hi)
    assert h == pytest.approx(hi - lo)
    assert h > 0


def test_headroom_raises_for_not_applicable_field():
    row = caps.capability_for("aluminum")
    assert row.jlc_min["via_diameter_mm"] is None
    with pytest.raises(ValueError, match="via_diameter_mm"):
        caps.headroom(row, "via_diameter_mm", 0.5)


def test_field_confidence_present_and_flags_uncertain_numbers():
    # Every row must self-report which numbers are memory-confident vs.
    # unverified — an un-dated, unflagged capability table is a trap.
    for row in caps.load_capabilities():
        assert set(row.field_confidence) >= set(caps.FIELDS)
    # Aluminum figures JLC does not publish are null and flagged low/n/a;
    # figures JLC does publish for aluminum (trace width/spacing, drill) are
    # verified and flagged high, so this checks per-field, not row-wide.
    aluminum = caps.capability_for("aluminum")
    for field in caps.FIELDS:
        confidence = aluminum.field_confidence[field]
        if aluminum.jlc_min[field] is None:
            assert confidence.startswith(("low", "n/a")), (field, confidence)


def test_every_field_has_a_source_and_a_retrieved_date():
    # Row-level source/retrieved cover every field in that row — verify the
    # row-level fields are non-empty for every row that defines FIELDS
    # entries (a table with numbers but no provenance is a trap).
    for row in caps.load_capabilities():
        assert row.source.strip()
        assert row.retrieved.strip()
        for field in caps.FIELDS:
            assert field in row.jlc_min
            assert field in row.house_default
            assert field in row.field_confidence


def test_aluminum_drill_minimum_is_manufacturable():
    # The critical fix: JLC's real aluminum drill minimum is 0.65mm — the
    # previous jlc_min (0.30) and house_default (0.35) were both below it,
    # making the house default itself unmanufacturable.
    row = caps.capability_for("aluminum")
    lo = row.jlc_min["drill_mm"]
    hi = row.house_default["drill_mm"]
    assert lo is not None and hi is not None
    assert lo == pytest.approx(0.65)
    assert hi > lo


def test_via_diameter_never_undercuts_drill_plus_annular_ring():
    # The 2026-08-28 fix, made durable: via_diameter_mm, drill_mm and
    # annular_ring_mm are geometrically coupled (diameter >= drill +
    # 2 x annular_ring) — the bug this test guards against is exactly
    # having a NUMBER right today but drifting out of sync again later,
    # since the three used to be margined as three independent tunables.
    # Every row, both tiers; a None field (not applicable to that
    # process, e.g. aluminum has no vias) is skipped, not a violation.
    violations = []
    for row in caps.load_capabilities():
        for tier_name, tier in (
            ("jlc_min", row.jlc_min),
            ("house_default", row.house_default),
        ):
            dia = tier.get("via_diameter_mm")
            drill = tier.get("drill_mm")
            ring = tier.get("annular_ring_mm")
            if dia is None or drill is None or ring is None:
                continue
            required = drill + 2.0 * ring
            if dia < required - 1e-9:
                violations.append((row.process, tier_name, dia, required))
    assert violations == []


def test_via_diameter_is_derived_not_the_raw_json_floor():
    # capabilities.py must not just pass the JSON's stored via_diameter_mm
    # (the published floor) straight through -- it has to win the max()
    # against the geometric requirement whenever the ring is a real ring,
    # which it is for both FR-4 rows.
    for process in ("2layer", "4layer"):
        row = caps.capability_for(process)
        for tier in (row.jlc_min, row.house_default):
            dia = tier["via_diameter_mm"]
            drill = tier["drill_mm"]
            ring = tier["annular_ring_mm"]
            assert dia is not None and drill is not None and ring is not None
            assert dia == pytest.approx(drill + 2.0 * ring)


def test_npth_annular_ring_has_no_in_table_coupling():
    # Audited alongside via_diameter_mm (2026-08-28): npth_annular_ring_mm
    # is measured against a per-design NPTH hole diameter, never a value
    # stored in this table, so unlike via_diameter_mm it has nothing to
    # derive here -- this test just pins that it stays a plain, present
    # figure for both FR-4 rows (a future coupling, if one is added, would
    # need its own invariant like the via one above).
    for process in ("2layer", "4layer"):
        row = caps.capability_for(process)
        assert row.jlc_min["npth_annular_ring_mm"] is not None
        assert row.house_default["npth_annular_ring_mm"] is not None


def test_no_low_confidence_field_carries_a_bare_unexplained_number():
    # A field flagged "low" confidence must never carry a non-null number
    # without an explanatory note attached — a bare "low" tag on a real
    # number is exactly the kind of unauditable guess this table forbids.
    for row in caps.load_capabilities():
        for field in caps.FIELDS:
            confidence = row.field_confidence.get(field, "")
            value = row.jlc_min.get(field)
            if confidence.startswith("low") and value is not None:
                # Must be more than the bare tag — a real explanation.
                assert len(confidence) > len("low"), (row.process, field)
                assert "—" in confidence or "-" in confidence, (row.process, field)
