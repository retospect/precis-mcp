"""Tests for stage 1 of `docs/backlog/taxonomy-bootstrap.md` — the
deterministic number+unit census (`precis.taxonomy.census`).

Plain DB-free functions, no ``store`` fixture, no markers: stage 1 is a pure
text scan and every fixture here is inline text, except the two tests that
touch the real ``norr-her-meta`` snapshot dump, which live outside the repo
(`~/.claude/projects/-Users-reto-precis-mcp/norr-her-meta/hubs.clean.jsonl`)
and are skipped when it is absent so CI stays green.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from precis.taxonomy.census import (
    build_registry,
    census_digest,
    load_snapshot_rows,
    scan_snapshot,
    scan_text,
    verify_snapshot,
)
from precis.taxonomy.config import CampaignConfig, PhraseRule, load_campaign
from precis.taxonomy.types import Snapshot, Thresholds

# ── fixtures ─────────────────────────────────────────────────────────

_NORR_HER_META_SNAPSHOT = Path(
    "~/.claude/projects/-Users-reto-precis-mcp/norr-her-meta/hubs.clean.jsonl"
).expanduser()

_REAL_SNAPSHOT_ROW_COUNT = 1664
_REAL_SNAPSHOT_SHA256 = (
    "e4fa6046172e5e59aa6f763b612e221265ea743891563a153f80523856575264"
)


def _snapshot(**overrides: Any) -> Snapshot:
    defaults: dict[str, Any] = {
        "source": "test fixture",
        "row_count": 0,
        "sha256": "0" * 64,
        "pulled_at": "2026-09-27T00:00:00Z",
        "text_field": "title",
        "ref_field": "ref_id",
    }
    defaults.update(overrides)
    return Snapshot(**defaults)


def _config(**overrides: Any) -> CampaignConfig:
    """A minimal, constructible-without-YAML campaign config. Every stage-1
    test that does not specifically need the shipped ``norr-her-meta``
    campaign (its reference-electrode/normalisation-basis vocabulary) uses
    this instead, which is also what proves the number+unit grammar itself
    carries no chemistry (AC7).
    """
    defaults: dict[str, Any] = {
        "campaign": "test",
        "config_version": 1,
        "snapshot": _snapshot(),
        "snapshot_path": Path("unused"),
        "thresholds": Thresholds(),
    }
    defaults.update(overrides)
    return CampaignConfig(**defaults)


def _mention_texts(text: str, config: CampaignConfig | None = None) -> tuple:
    cfg = config or _config()
    registry = build_registry(cfg)
    return scan_text(text, ref_id=1, config=cfg, registry=registry)


# A campaign config carrying exactly the grammar features the "accepted
# units" table below needs: the ``decade`` unit (AC3's "120 mV dec−1"),
# nothing else campaign-specific.
_UNIT_CONFIG = _config(unit_definitions=("decade = [decade] = dec",))


# ── AC1: determinism ─────────────────────────────────────────────────


def test_scan_text_is_deterministic() -> None:
    text = "Overpotential of −0.35 V vs RHE at 10 mA cm−2, tested for 3 h."
    cfg = load_campaign("norr-her-meta")
    registry = build_registry(cfg)
    first = scan_text(text, ref_id=42, config=cfg, registry=registry)
    second = scan_text(text, ref_id=42, config=cfg, registry=registry)
    assert first == second
    assert census_digest(first) == census_digest(second)


def test_scan_snapshot_is_deterministic() -> None:
    rows = [
        {"ref_id": 1, "title": "Yield of 50 % at 25 °C."},
        {"ref_id": 2, "title": "Overpotential −0.35 V vs RHE."},
    ]
    cfg = _config()
    first = scan_snapshot(rows, cfg)
    second = scan_snapshot(rows, cfg)
    assert first == second
    assert census_digest(first) == census_digest(second)
    # Order of the input rows must not matter — the sort key covers the
    # whole result, not just within one row (`scan_snapshot`'s docstring).
    reversed_result = scan_snapshot(list(reversed(rows)), cfg)
    assert reversed_result == first


# ── verify_snapshot ──────────────────────────────────────────────────


def test_verify_snapshot_raises_on_row_count_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "snap.jsonl"
    path.write_text('{"ref_id": 1, "title": "x"}\n', encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = _snapshot(row_count=2, sha256=sha)
    with pytest.raises(ValueError, match="row_count"):
        verify_snapshot(path, snapshot)


def test_verify_snapshot_raises_on_sha_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "snap.jsonl"
    path.write_text('{"ref_id": 1, "title": "x"}\n', encoding="utf-8")
    snapshot = _snapshot(row_count=1, sha256="f" * 64)
    with pytest.raises(ValueError, match="sha256"):
        verify_snapshot(path, snapshot)


def test_verify_snapshot_passes_on_matching_file(tmp_path: Path) -> None:
    path = tmp_path / "snap.jsonl"
    path.write_text('{"ref_id": 1, "title": "x"}\n', encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = _snapshot(row_count=1, sha256=sha)
    verify_snapshot(path, snapshot)  # must not raise


@pytest.mark.skipif(
    not _NORR_HER_META_SNAPSHOT.exists(),
    reason="norr-her-meta hub snapshot lives outside the repo",
)
def test_verify_snapshot_pins_norr_her_meta() -> None:
    snapshot = _snapshot(
        row_count=_REAL_SNAPSHOT_ROW_COUNT, sha256=_REAL_SNAPSHOT_SHA256
    )
    verify_snapshot(_NORR_HER_META_SNAPSHOT, snapshot)  # must not raise


@pytest.mark.skipif(
    not _NORR_HER_META_SNAPSHOT.exists(),
    reason="norr-her-meta hub snapshot lives outside the repo",
)
def test_scan_snapshot_on_real_dump_is_deterministic() -> None:
    cfg = load_campaign("norr-her-meta")
    rows = load_snapshot_rows(cfg.snapshot_path)
    assert len(rows) == _REAL_SNAPSHOT_ROW_COUNT
    registry = build_registry(cfg)
    first = scan_snapshot(rows, cfg, registry=registry)
    second = scan_snapshot(rows, cfg, registry=registry)
    assert first == second
    assert census_digest(first) == census_digest(second)


# ── units accepted ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_literal"),
    [
        ("10 mA cm−2", "10"),
        ("50 %", "50"),
        ("25 °C", "25"),
        ("0.5 V", "0.5"),
        ("3 h", "3"),
        ("120 mV dec−1", "120"),
        ("1.2 × 10−3 s−1", "1.2 × 10−3"),
        ("2 M KOH", "2"),
    ],
)
def test_units_accepted(text: str, expected_literal: str) -> None:
    mentions = _mention_texts(text, _UNIT_CONFIG)
    values = [m for m in mentions if m.kind == "value"]
    assert len(values) == 1, values
    assert values[0].literal == expected_literal
    assert values[0].raw_unit is not None


def test_units_accepted_2_m_koh_binds_only_m_not_m_koh() -> None:
    # "M KOH" is a value followed by a solute name, not a two-word unit —
    # the campaign electrolyte isn't a unit, and pint correctly stops
    # shrinking at "M" (molar) rather than failing on "M KOH" as a whole.
    mentions = _mention_texts("2 M KOH", _UNIT_CONFIG)
    values = [m for m in mentions if m.kind == "value"]
    assert len(values) == 1
    assert values[0].raw_unit == "M"


# ── shared units (blocker 3b) ────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "current densities of 10 and 30 mA cm−2",
            [("10", "mA cm^-2"), ("30", "mA cm^-2")],
        ),
        (
            "at 10, 30 and 100 mA cm−2",
            [("10", "mA cm^-2"), ("30", "mA cm^-2"), ("100", "mA cm^-2")],
        ),
        ("10 or 20 %", [("10", "%"), ("20", "%")]),
    ],
)
def test_shared_unit_list_borrows_the_trailing_unit(
    text: str, expected: list[tuple[str, str]]
) -> None:
    values = [m for m in _mention_texts(text, _UNIT_CONFIG) if m.kind == "value"]
    assert [(m.literal, m.raw_unit) for m in values] == expected
    assert [m.marker for m in values][:-1] == ["shared-unit"] * (len(expected) - 1)
    assert values[-1].marker is None, "the unit's own number is not a borrower"


@pytest.mark.parametrize(
    "text",
    [
        "2 electrons and 3 h",  # the gap is not a bare connective
        "150 hubs, 3 h",  # a unit-less count before a comma keeps no unit
        "10 and 30 catalysts",  # nothing to borrow
        "3 h and 25",  # the borrower must come first
    ],
)
def test_shared_unit_does_not_fire_across_prose_or_without_a_unit(text: str) -> None:
    values = [m for m in _mention_texts(text, _UNIT_CONFIG) if m.kind == "value"]
    assert all(m.marker is None for m in values), values
    first = values[0]
    assert first.raw_unit is None or first.literal == "3"


def test_shared_unit_keeps_a_number_with_its_own_unit() -> None:
    values = [
        m for m in _mention_texts("25 °C and 1 atm", _UNIT_CONFIG) if m.kind == "value"
    ]
    assert [(m.literal, m.raw_unit, m.marker) for m in values] == [
        ("25", "°C", None),
        ("1", "atm", None),
    ]


# ── units refused ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["150 hubs", "3 in the cell", "1664 papers", "20 catalysts"],
)
def test_units_refused(text: str) -> None:
    mentions = _mention_texts(text)
    values = [m for m in mentions if m.kind == "value"]
    assert len(values) == 1, values
    assert values[0].raw_unit is None


def test_ambiguous_stopword_mid_run_does_not_form_a_bogus_compound() -> None:
    # "at" is a real pint unit (technical_atmosphere) once campaign
    # definitions widen the registry, but "h at" is not a unit anyone
    # meant — a common corpus phrasing ("tested for 3 h at 25 °C") must
    # not resolve "3" to "hour * atmosphere".
    mentions = _mention_texts("tested for 3 h at 25 °C", _UNIT_CONFIG)
    values = {m.literal: m.raw_unit for m in mentions if m.kind == "value"}
    assert values["3"] == "h"
    assert values["25"] == "°C"


# ── letter/digit boundary: a number glued onto a preceding token ─────
#
# pint's SI-prefix machinery manufactures plausible units out of ordinary
# formula/identifier fragments (`has` -> hectare, `RR` -> ronnamolar gas
# constant, `nanoparticles` -> nanoparticle, `D` -> debye) with no parse
# failure to catch — this is a real, measured finding against the
# norr-her-meta snapshot (RR 61 hits, N 18, C 16 before the fix), not a
# hypothetical. A number is not a value mention at all when the character
# immediately before it is a letter or digit — it is part of a larger
# token (a formula subscript, a material code), never a measurement.


@pytest.mark.parametrize(
    "text",
    [
        "NO3RR catalysts were screened",
        "the Ti3C2 MXene shows",
        "H2 evolution was observed",
        "grown on g-C3N4 supports",
        "an NZT-1 electrode",
        "computed at the HSE06 level",
    ],
)
def test_digit_glued_to_a_preceding_letter_yields_no_value_mention(
    text: str,
) -> None:
    mentions = _mention_texts(text, _UNIT_CONFIG)
    assert [m for m in mentions if m.kind == "value"] == []


# ── parenthesised identifiers: "(111)" is a Miller index, not a value ─
#
# Measured on the first norr-her-meta probe: 21 of 204 discovered rows were
# "crystallographic facet" measurands minted from surface labels such as
# Pd(111), Mo₂C(0001), Ni₂P(001). A parenthesised group whose entire
# content is a bare digit run of three or more names something; it does
# not measure it. "(204 kJ/mol)" keeps its value mention — the digits are
# not the whole content of the group.


@pytest.mark.parametrize(
    "text",
    [
        "NO3− adsorbs more favorably on Cu(111) than on Ag(111)",
        "Hydrogen evolution on Mo₂C(0001) surfaces proceeds",
        "on Ni₂P(001) surfaces",
        "the (100) facet dominates",
    ],
)
def test_parenthesised_digit_run_yields_no_value_mention(text: str) -> None:
    mentions = _mention_texts(text, _UNIT_CONFIG)
    assert [m for m in mentions if m.kind == "value"] == []


@pytest.mark.parametrize(
    ("text", "expected_literal"),
    [
        ("the N-O bond (204 kJ/mol) is weak", "204"),
        ("a 111 mV shift", "111"),
        ("only (12) sites", "12"),
        ("at (2.5) V", "2.5"),
    ],
)
def test_parenthesised_rule_leaves_real_values_alone(
    text: str, expected_literal: str
) -> None:
    mentions = _mention_texts(text, _UNIT_CONFIG)
    assert [m.literal for m in mentions if m.kind == "value"] == [expected_literal]


def test_glued_denylisted_letter_is_refused_but_spaced_form_resolves() -> None:
    cfg = _config(glued_unit_denylist=frozenset({"D"}))
    glued = {
        m.literal: m.raw_unit
        for m in _mention_texts("a 2D Cu/Fe MOF", cfg)
        if m.kind == "value"
    }
    assert glued == {"2": None}  # not debye

    spaced = {
        m.literal: m.raw_unit
        for m in _mention_texts("a dipole moment of 0.5 D", cfg)
        if m.kind == "value"
    }
    assert spaced == {"0.5": "D"}  # the denylist only gates the glued form


@pytest.mark.parametrize(
    ("text", "expected_literal", "expected_unit"),
    [
        ("pH 7", "7", None),
        ("the Faradaic efficiency (FE) of 95 % was reached", "95", "%"),
    ],
)
def test_spaced_value_after_a_letter_boundary_still_resolves(
    text: str, expected_literal: str, expected_unit: str | None
) -> None:
    # The boundary rule only rejects a number *glued* to the preceding
    # token — a space, as in ordinary prose, is unaffected.
    values = [m for m in _mention_texts(text) if m.kind == "value"]
    assert len(values) == 1
    assert values[0].literal == expected_literal
    assert values[0].raw_unit == expected_unit


# ── ranges and tolerances ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected_literal"),
    [
        ("1.2–3.4 V", "1.2–3.4"),
        ("10-20 %", "10-20"),
        ("1.2 to 3.4 V", "1.2 to 3.4"),
        ("1.2 ± 0.3 V", "1.2 ± 0.3"),
        ("1.2 +/- 0.3 V", "1.2 +/- 0.3"),
    ],
)
def test_ranges_and_tolerances_are_one_mention(
    text: str, expected_literal: str
) -> None:
    mentions = _mention_texts(text)
    values = [m for m in mentions if m.kind == "value"]
    assert len(values) == 1, values
    assert values[0].literal == expected_literal


# ── reference states ─────────────────────────────────────────────────


def test_value_with_reference_state_yields_both_mentions() -> None:
    # This pair is the census finding the design doc cites: 150 hubs state
    # a potential with no reference electrode, 185 state one with RHE
    # (`taxonomy-bootstrap.md` motivation section).
    cfg = load_campaign("norr-her-meta")
    mentions = _mention_texts("−0.35 V vs RHE", cfg)
    kinds = [(m.kind, m.marker) for m in mentions]
    assert ("value", None) in kinds
    assert ("reference_state", "rhe") in kinds
    assert len(mentions) == 2


def test_value_without_reference_state_yields_no_reference_mention() -> None:
    cfg = load_campaign("norr-her-meta")
    mentions = _mention_texts("−0.35 V", cfg)
    assert len(mentions) == 1
    assert mentions[0].kind == "value"


def test_unlisted_electrode_still_yields_generic_reference_state() -> None:
    cfg = load_campaign("norr-her-meta")
    mentions = _mention_texts("measured vs Zn/Zn2+ electrode", cfg)
    ref_mentions = [m for m in mentions if m.kind == "reference_state"]
    assert len(ref_mentions) == 1
    assert ref_mentions[0].marker is None
    assert "Zn/Zn2+" in ref_mentions[0].literal


def test_campaign_rule_suppresses_the_generic_fallback_at_the_same_span() -> None:
    # "vs RHE" must not ALSO produce a marker=None generic hit alongside
    # the campaign's marker="rhe" one.
    cfg = load_campaign("norr-her-meta")
    mentions = _mention_texts("−0.35 V vs RHE", cfg)
    ref_mentions = [m for m in mentions if m.kind == "reference_state"]
    assert len(ref_mentions) == 1
    assert ref_mentions[0].marker == "rhe"


# ── AC7: non-chemistry ───────────────────────────────────────────────


def _sociology_config() -> CampaignConfig:
    """No chemistry anywhere — proves the grammar has no domain dependency.
    Only a currency code list, which is itself domain-neutral (a sociology
    campaign needs it as much as a chemistry one).
    """
    return _config(currency_codes=frozenset({"USD"}))


def test_non_chemistry_unemployment_rate() -> None:
    mentions = _mention_texts(
        "the unemployment rate of 5.2 % rose sharply", _sociology_config()
    )
    values = [m for m in mentions if m.kind == "value"]
    assert len(values) == 1
    assert values[0].literal == "5.2"
    assert values[0].raw_unit == "%"


def test_non_chemistry_currency_with_base_year() -> None:
    mentions = _mention_texts(
        "GDP per capita was measured in 2015 USD per capita", _sociology_config()
    )
    currency_values = [m for m in mentions if m.kind == "value" and m.raw_unit == "USD"]
    assert len(currency_values) == 1
    assert currency_values[0].literal == "2015"
    base_year = [
        m
        for m in mentions
        if m.kind == "reference_state" and m.marker == "price-base-year"
    ]
    assert len(base_year) == 1
    assert base_year[0].literal == "2015"


# ── context ──────────────────────────────────────────────────────────


def test_context_has_no_newline_and_is_bounded() -> None:
    text = "a" * 200 + "\n" + "10 mA cm−2" + "\n" + "b" * 200
    mentions = _mention_texts(text, _UNIT_CONFIG)
    values = [m for m in mentions if m.kind == "value"]
    assert len(values) == 1
    context = values[0].context
    assert "\n" not in context
    assert len(context) <= 130  # ~121 target + slack for the number itself


# ── PhraseRule / config plumbing used only from this module ─────────


def test_normalisation_basis_mention_carries_rule_id() -> None:
    cfg = _config(
        normalisation_bases=(PhraseRule(id="per-mass", pattern=re.compile(r"g\^?-?1")),)
    )
    mentions = _mention_texts("10 A g-1", cfg)
    basis = [m for m in mentions if m.kind == "normalisation_basis"]
    assert len(basis) == 1
    assert basis[0].marker == "per-mass"
