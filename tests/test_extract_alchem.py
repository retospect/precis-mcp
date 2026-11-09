"""Pure tests for ``precis.extract.alchem`` — the ALCHEM-graph agreement
diff behind ``scripts/alchem-diff`` (gr606461).

The TriG fixture is hand-written to the schema the published
``data.trig`` uses: one named graph per paper, ``alchem:doi`` /
``alchem:title`` in a separate metadata graph, ``PropertyValue`` nodes
with term/property/value(-min/-max)/unit/value_text/material/origin/
evidence_locator, and ``MaterialSystem`` labels.
"""

from __future__ import annotations

import pytest

from precis.extract.alchem import (
    AlchemValue,
    OursValue,
    _subject_key,
    alchem_unit,
    diff_paper,
    ours_from_measure_row,
    parse_alchem_trig,
    render_report,
    their_key,
)

TRIG = """\
@prefix alchem: <https://w3id.org/alchem/> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<https://w3id.org/alchem/graph/paper/1> {
    <https://w3id.org/alchem/paper/1> a alchem:Paper ;
        alchem:reports <https://w3id.org/alchem/paper/1/V1>,
            <https://w3id.org/alchem/paper/1/V2>,
            <https://w3id.org/alchem/paper/1/V3>,
            <https://w3id.org/alchem/paper/1/V4> .

    <https://w3id.org/alchem/paper/1/M1> a alchem:MaterialSystem ;
        rdfs:label "Ni foam" .

    <https://w3id.org/alchem/paper/1/M2> a alchem:MaterialSystem ;
        rdfs:label "Cu_2O" .

    <https://w3id.org/alchem/paper/1/V1> a alchem:PropertyValue,
            <https://w3id.org/alchem/term/overpotential> ;
        rdfs:label "overpotential" ;
        alchem:evidence_locator "Fig. 3a" ;
        alchem:material <https://w3id.org/alchem/paper/1/M1> ;
        alchem:origin alchem:experiment ;
        alchem:property "overpotential" ;
        alchem:term <https://w3id.org/alchem/term/overpotential> ;
        alchem:term_value 4.08e+01 ;
        alchem:unit "mV" ;
        alchem:value 4.08e+01 ;
        alchem:value_text "40.8 mV" .

    <https://w3id.org/alchem/paper/1/V2> a alchem:PropertyValue,
            <https://w3id.org/alchem/term/particle_size> ;
        alchem:evidence_locator "Section 2.1" ;
        alchem:material <https://w3id.org/alchem/paper/1/M1> ;
        alchem:origin alchem:experiment ;
        alchem:property "particle_size" ;
        alchem:term <https://w3id.org/alchem/term/particle_size> ;
        alchem:unit "nm" ;
        alchem:value_max 9e+00 ;
        alchem:value_min 7e+00 ;
        alchem:value_text "7–9 nm" .

    <https://w3id.org/alchem/paper/1/V3> a alchem:PropertyValue ;
        alchem:evidence_locator "Fig. 4" ;
        alchem:material <https://w3id.org/alchem/paper/1/M2> ;
        alchem:origin alchem:computation ;
        alchem:property "crystal_system" ;
        alchem:value_text "cubic" .

    <https://w3id.org/alchem/paper/1/V4> a alchem:PropertyValue ;
        alchem:evidence_locator "Fig. 5" ;
        alchem:material <https://w3id.org/alchem/paper/1/M1> ;
        alchem:origin alchem:experiment ;
        alchem:property "onset_potential" ;
        alchem:term <https://w3id.org/alchem/term/onset_potential> ;
        alchem:unit "V_RHE" ;
        alchem:value 4.1e-01 ;
        alchem:value_text "0.41 V vs RHE" .
}

<https://w3id.org/alchem/graph/meta> {
    <https://w3id.org/alchem/paper/1> a alchem:Paper ;
        rdfs:seeAlso <https://doi.org/10.1000/XYZ.123> ;
        alchem:doi "10.1000/XYZ.123" ;
        alchem:title "A Paper" ;
        alchem:year 2026 .
}
"""


def ours(
    id: int,
    key: str,
    *,
    subject: str = "Ni foam",
    interval: tuple[float, float] | None = None,
    text: str | None = None,
    canonical_unit: str | None = "V",
    literal: str = "",
    reported_unit: str | None = None,
    normalization: str | None = None,
    reference: str | None = None,
    tier: str = "measured",
) -> OursValue:
    return OursValue(
        id=id,
        measurand_key=key,
        measurand=key,
        keys=frozenset({key}),
        subject_keys=frozenset({_subject_key(subject)} - {""}),
        subject=subject,
        interval=interval,
        text=text,
        canonical_unit=canonical_unit,
        literal=literal,
        reported_unit=reported_unit,
        normalization=normalization,
        reference=reference,
        attribution="own_work",
        tier=tier,
    )


def alv(
    vid: str,
    term: str,
    *,
    value: float | None = None,
    lo: float | None = None,
    hi: float | None = None,
    unit: str | None = None,
    text: str | None = None,
    material: str | None = "Ni foam",
    prop: str | None = None,
    origin: str = "experiment",
) -> AlchemValue:
    return AlchemValue(
        vid=vid,
        paper="1",
        term=term,
        prop=prop if prop is not None else term,
        value=value,
        value_min=lo,
        value_max=hi,
        unit=unit,
        value_text=text,
        material=material,
        origin=origin,
        locator="Fig. 1",
    )


def kinds(diff):
    return sorted(p.kind for p in diff.pairs)


class TestParse:
    def test_paper_keyed_by_doi(self):
        papers = parse_alchem_trig(TRIG)
        assert list(papers) == ["10.1000/xyz.123"]
        p = papers["10.1000/xyz.123"]
        assert p.doi == "10.1000/xyz.123"
        assert p.title == "A Paper"
        assert len(p.values) == 4

    def test_value_fields(self):
        (v1,) = [
            v
            for v in parse_alchem_trig(TRIG)["10.1000/xyz.123"].values
            if v.vid == "V1"
        ]
        assert v1.term == "overpotential"
        assert v1.prop == "overpotential"
        assert v1.value == 40.8
        assert v1.unit == "mV"
        assert v1.value_text == "40.8 mV"
        assert v1.material == "Ni foam"
        assert v1.origin == "experiment"
        assert v1.locator == "Fig. 3a"

    def test_range_and_categorical(self):
        values = {v.vid: v for v in parse_alchem_trig(TRIG)["10.1000/xyz.123"].values}
        assert values["V2"].value is None
        assert (values["V2"].value_min, values["V2"].value_max) == (7.0, 9.0)
        assert values["V3"].value is None
        assert values["V3"].value_text == "cubic"
        assert values["V3"].term == ""  # untyped PropertyValue
        assert values["V3"].origin == "computation"
        assert values["V4"].unit == "V_RHE"


class TestUnits:
    @pytest.mark.parametrize(
        ("raw", "text", "scale"),
        [
            ("mV", "mV", 1.0),
            ("cm-1", "cm^-1", 1.0),
            ("cm4.F-2.V-1", "cm^4*F^-2*V^-1", 1.0),
            ("mA/cm2_ECSA", "mA/cm^2", 1.0),
            ("g/g_MOF", "g/g", 1.0),
            ("mol.rad2", "mol*rad^2", 1.0),
            ("W/(m.K)", "W/(m*K)", 1.0),
            ("Ohm", "ohm", 1.0),
            ("wt%", "%", 1.0),
            ("µB", "bohr_magneton", 1.0),
            ("ML", "1", 1.0),
            ("e", "count", 1.0),
            ("ppb", "1", 1e-9),
            ("10^-3 Å2", "Å^2", 1e-3),
            ("×10^25 cm3", "cm^3", 1e25),
            ("mol/(g.h)", "mol/(g*h)", 1.0),
            ("meV/atom", "meV/count", 1.0),
        ],
    )
    def test_normalised(self, raw, text, scale):
        nu = alchem_unit(raw)
        assert nu.text == text
        assert nu.scale == scale

    def test_reference_electrode(self):
        assert alchem_unit("V_RHE").reference == "RHE"
        assert alchem_unit("V_RHE").text == "V"
        assert alchem_unit("V_Ag/AgCl").reference == "AG/AGCL"

    def test_basis_label(self):
        assert alchem_unit("mA/cm2_ECSA").basis == "ECSA"
        assert alchem_unit("g/g_MOF").basis == "MOF"
        assert alchem_unit("A/mgPt").basis == "Pt"

    def test_missing(self):
        assert alchem_unit(None).text is None
        assert alchem_unit("").text is None


class TestDiff:
    def test_match_unit_converted(self):
        d = diff_paper(
            [ours(1, "overpotential", interval=(0.0408, 0.0408), literal="40.8 mV")],
            [alv("V1", "overpotential", value=40.8, unit="mV")],
        )
        assert kinds(d) == ["match"]

    def test_match_percent_to_fraction(self):
        d = diff_paper(
            [
                ours(
                    1,
                    "faradaic-efficiency",
                    interval=(0.95, 0.95),
                    canonical_unit="1",
                    literal="95 %",
                )
            ],
            [alv("V1", "faradaic_efficiency", value=95.0, unit="%")],
        )
        assert kinds(d) == ["match"]

    def test_interval_overlap_matches(self):
        d = diff_paper(
            [
                ours(
                    1,
                    "particle-size",
                    interval=(7e-9, 9e-9),
                    canonical_unit="m",
                    literal="7–9 nm",
                )
            ],
            [alv("V1", "particle_size", lo=7.0, hi=9.0, unit="nm")],
        )
        assert kinds(d) == ["match"]

    def test_value_disagreement(self):
        d = diff_paper(
            [ours(1, "overpotential", interval=(0.10, 0.10))],
            [alv("V1", "overpotential", value=40.8, unit="mV")],
        )
        assert kinds(d) == ["value"]

    def test_unit_disagreement_dimension(self):
        d = diff_paper(
            [ours(1, "overpotential", interval=(0.04, 0.04), canonical_unit="V")],
            [alv("V1", "overpotential", value=40.0, unit="A")],
        )
        assert kinds(d) == ["unit"]

    def test_unit_disagreement_unparseable(self):
        d = diff_paper(
            [ours(1, "permeability", interval=(1.0, 1.0), canonical_unit="m^2")],
            [alv("V1", "permeability", value=3.0, unit="Barrer")],
        )
        assert kinds(d) == ["unit"]

    def test_reference_disagreement(self):
        d = diff_paper(
            [ours(1, "onset-potential", interval=(0.41, 0.41))],
            [alv("V1", "onset_potential", value=0.41, unit="V_RHE")],
        )
        assert kinds(d) == ["unit"]
        assert "RHE" in d.pairs[0].note

    def test_reference_agreement(self):
        d = diff_paper(
            [ours(1, "onset-potential", interval=(0.41, 0.41), reference="RHE")],
            [alv("V1", "onset_potential", value=0.41, unit="V_RHE")],
        )
        assert kinds(d) == ["match"]

    def test_subject_mismatch_keeps_both(self):
        d = diff_paper(
            [ours(1, "overpotential", subject="Cu_2O", interval=(0.04, 0.04))],
            [alv("V1", "overpotential", value=40.8, unit="mV", material="Ni foam")],
        )
        assert kinds(d) == ["ours_only", "theirs_only"]

    def test_categorical(self):
        d = diff_paper(
            [
                ours(
                    1,
                    "crystal-system",
                    interval=None,
                    text="cubic",
                    canonical_unit=None,
                    literal="cubic",
                )
            ],
            [alv("V1", "crystal_system", text="cubic", material="Ni foam")],
        )
        assert kinds(d) == ["match"]

    def test_unmapped_is_theirs_only(self):
        d = diff_paper(
            [ours(1, "overpotential", interval=(0.0408, 0.0408))],
            [
                alv("V1", "overpotential", value=40.8, unit="mV"),
                alv("V2", "ftir_band_position", value=1630.0, unit="cm-1"),
            ],
        )
        assert kinds(d) == ["match", "theirs_only"]
        assert [v.vid for v in d.unmapped] == ["V2"]

    def test_measurand_map(self):
        d = diff_paper(
            [
                ours(
                    1, "stability-duration", interval=(150.0, 150.0), canonical_unit="s"
                )
            ],
            [
                alv(
                    "V1",
                    "plating_lifespan",
                    value=150.0,
                    unit="s",
                    prop="stability_duration",
                )
            ],
        )
        assert kinds(d) == ["match"]
        assert d.pairs[0].theirs is not None
        assert their_key(d.pairs[0].theirs) == "stability-duration"

    def test_greedy_picks_closest(self):
        o = ours(1, "overpotential", interval=(0.0408, 0.0408))
        d = diff_paper(
            [o],
            [
                alv("V1", "overpotential", value=40.8, unit="mV"),
                alv("V2", "overpotential", value=400.0, unit="mV"),
            ],
        )
        assert kinds(d) == ["match", "theirs_only"]
        match = next(p for p in d.pairs if p.kind == "match")
        assert match.theirs is not None
        assert match.theirs.vid == "V1"


class TestAdapter:
    def test_ours_from_measure_row(self):
        row = {
            "id": 7,
            "measurand_slug": "overpotential",
            "measurand": "Overpotential",
            "measurand_aliases": ["η"],
            "subject": "Ni foam",
            "subject_title": "Ni foam electrode",
            "value_num": 0.0408,
            "value_low": None,
            "value_high": None,
            "value_form": "point",
            "literal": "40.8 mV",
            "canonical_unit": "V",
            "reported_unit": "mV",
            "normalization": None,
            "reference": "RHE",
            "source_attribution": "own_work",
            "tier": "measured",
        }
        o = ours_from_measure_row(row)
        assert o.interval == (0.0408, 0.0408)
        assert o.measurand_key == "overpotential"
        assert "ni-foam-electrode" in o.subject_keys
        assert o.reference == "RHE"
        assert o.text == "40.8 mV"

    def test_paper_subject_title_is_not_a_material_key(self):
        row = {
            "id": 9,
            "measurand_slug": "overpotential",
            "measurand": "Overpotential",
            "subject": None,
            "subject_title": "A Paper About Catalysts",
            "subject_kind": "paper",
            "value_num": 0.0408,
            "value_form": "point",
            "literal": "40.8 mV",
            "canonical_unit": "V",
        }
        o = ours_from_measure_row(row)
        assert o.subject_keys == frozenset()
        assert o.subject == ""
        d = diff_paper(
            [o],
            [alv("V1", "overpotential", value=40.8, unit="mV", material="Ni foam")],
        )
        assert kinds(d) == ["match"]

    def test_paper_subject_free_label_still_keys(self):
        row = {
            "id": 10,
            "measurand_slug": "overpotential",
            "measurand": "Overpotential",
            "subject": "Ni foam",
            "subject_title": "A Paper About Catalysts",
            "subject_kind": "paper",
            "value_num": 0.0408,
            "value_form": "point",
            "literal": "40.8 mV",
            "canonical_unit": "V",
        }
        o = ours_from_measure_row(row)
        assert o.subject_keys == frozenset({"ni-foam"})
        d = diff_paper(
            [o],
            [alv("V1", "overpotential", value=40.8, unit="mV", material="Cu_2O")],
        )
        assert kinds(d) == ["ours_only", "theirs_only"]


def test_render_report_smoke():
    d = diff_paper(
        [ours(1, "overpotential", interval=(0.0408, 0.0408), literal="40.8 mV")],
        [
            alv("V1", "overpotential", value=40.8, unit="mV"),
            alv("V2", "ftir_band_position", value=1630.0, unit="cm-1"),
        ],
        label="pa1",
        doi="10.1000/xyz.123",
    )
    report = render_report([d])
    assert "| pa1 | 10.1000/xyz.123 | 1 | 0 | 0 | 0 | 1 |" in report
    assert "ftir_band_position" in report
