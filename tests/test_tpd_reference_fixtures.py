"""Provenance check for the kinetics-v2 TPD reference dataset (spec D11).

Every value in ``tests/fixtures/kinetics/tpd_reference.json`` cites a paper
chunk handle (``pc…``). The committed snapshot
(``tpd_reference_chunks.json``, exported read-only from prod so CI needs
no prod access) carries no journal text — the repo is public. Each entry
is ``{sha256, printeds}``: the sha256 of the full chunk text (checkable
against prod) and the verbatim ``printed`` snippets (a number and its
label) cited from that chunk.

Beyond presence, a parametrized check asserts that the leading number in
``printed`` equals ``value``; rows where the interpretation legitimately
differs (OCR-garbled digits, dropped ``±``, sign conventions, orders
printed as words) sit in ``_EXEMPTIONS`` with a per-row reason, and a
stale exemption fails the suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "kinetics"

_MINUS_VARIANTS = dict.fromkeys(map(ord, "−–—‑‒‐"), "-")
_NUMBER_RE = re.compile(r"[+-]?\s*\d+(?:\.\d+)?")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PRINTED_MAX = 120
_SECOND_ORDER_UNITS = {"cm2/(particle s)", "ML-1 s-1"}
_FIRST_ORDER_UNITS = {"s-1"}

# Rows where the leading number in `printed` legitimately differs from
# `value`, each with the reason. An entry that stops mismatching fails
# the suite so the list cannot rot.
_EXEMPTIONS = {
    ("co-pd110-pa498699", "e_des_second_state_offset"): (
        "sign interpreted: printed '5 kcal/mole lower' means -5"
    ),
    ("h2-pd110-pa498700", "theta0"): (
        "theta=1/2 interpreted from OCR '8=&(24.7x lOi atoms~cm2)'"
    ),
    ("h2-pd110-pa498700", "order"): "order printed as a word, no numeral",
    ("h2-pd110-pa498700", "nu_prefactor"): ("8e-2 interpreted from OCR 'v=8 x 10e2'"),
    ("h2-pd111-pa498700", "t_p_bulk_C"): "OCR '4OO' is 400",
    ("h2-pd111-pa498701-beta", "order"): "order printed as a word, no numeral",
    ("h2-pd111-pa498701-alpha", "order"): (
        "order printed as a word ('near-zeroth-order'), no numeral"
    ),
    ("h2-pd111-pa498704", "e_ads_d2_zpe"): (
        "value is the third entry of the flattened Table-1 row '0.980.991.00'"
    ),
    ("no-pd111-pa498705", "nu_prefactor"): (
        "superscripts flattened: printed '1015.65±0.20' is 10^15.65±0.20"
    ),
    ("no-pd332-pa498705", "nu_prefactor"): (
        "superscripts flattened: printed '1015.80±0.15' is 10^15.8±0.15"
    ),
    ("no-pd111-pa498706", "nu_theta_lo"): "OCR '1Or7' is 10^17",
    ("no-pd111-pa498707-beta3", "nu_prefactor"): "OCR '1 X 1Ol6' is 10^16",
    ("no-pd111-pa498707-beta3", "order"): "order printed as a word, no numeral",
    ("no-pd111stepped-pa498708", "theta_sat_ML"): (
        "printed as 'one monolayer', no numeral"
    ),
    ("co-pd-nanoparticle-pa498809", "e_ads_8nm"): (
        "± dropped in extraction: printed '1267' is 126±7"
    ),
    ("co-pd-nanoparticle-pa498809", "e_ads_1_8nm"): (
        "± dropped in extraction: printed '1062' is 106±2"
    ),
    ("co-pd-nanoparticle-pa498809", "e_ads_steady_state"): (
        "± dropped in extraction: printed '759' is 75±9"
    ),
    ("h2-w110-pa498687", "order"): "order printed as a word, no numeral",
    ("h2-w100-pa498687", "order_beta2"): "order printed as a word, no numeral",
    ("co-ni110-pa498688", "order"): "order printed as a word, no numeral",
    ("xe-w111-pa498688", "order"): "order printed as a word, no numeral",
    ("co-co10m10-pa498689", "order"): "order printed as a word, no numeral",
    ("o2-pd111-pa498707", "order"): "order printed as a word, no numeral",
    ("o2-pt111-pa498693", "order"): (
        "order printed as a word ('secondorder'), no numeral"
    ),
    ("o2-pd111-pa498693-quoted", "nu_prefactor"): (
        "superscripts in tags: printed '10<sup>13</sup>' is 10^13"
    ),
    ("o2-pd111-pa498693-quoted", "order"): "order printed as a word, no numeral",
    ("co-pd111-pa498800", "nu_prefactor_theta0"): (
        "superscripts flattened: printed '1013.5' is 10^13.5"
    ),
}


def _load() -> tuple[dict, dict]:
    table = json.loads((FIXTURE_DIR / "tpd_reference.json").read_text(encoding="utf-8"))
    snapshot = json.loads(
        (FIXTURE_DIR / "tpd_reference_chunks.json").read_text(encoding="utf-8")
    )
    return table, snapshot["chunks"]


def _first_number(text: str) -> float | None:
    m = _NUMBER_RE.search(text.translate(_MINUS_VARIANTS))
    return None if m is None else float(m.group().replace(" ", ""))


def _value_number(v: dict) -> float | None:
    val = v["value"]
    if isinstance(val, (int, float)):
        return float(val)
    return _first_number(val)


def _rows() -> list[tuple[str, str, dict]]:
    table, _ = _load()
    return [
        (m["id"], name, v)
        for m in table["measurements"]
        for name, v in m["values"].items()
    ]


def test_every_value_cites_an_existing_chunk() -> None:
    table, chunks = _load()
    missing = [
        (m["id"], name, v["chunk"])
        for m in table["measurements"]
        for name, v in m["values"].items()
        if v["chunk"] not in chunks
    ]
    assert not missing, f"cited chunks missing from snapshot: {missing}"


def test_chunk_entries_carry_hash_and_printeds() -> None:
    table, chunks = _load()
    cited: dict[str, set[str]] = {}
    for m in table["measurements"]:
        for v in m["values"].values():
            cited.setdefault(v["chunk"], set()).add(v["printed"])
    for pc, entry in chunks.items():
        assert _SHA256_RE.fullmatch(entry["sha256"]), pc
        printeds = entry["printeds"]
        assert printeds, pc
        for p in printeds:
            # a few words of label at most — never prose
            assert len(p) <= _PRINTED_MAX, (pc, p)
        stale = set(printeds) - cited.get(pc, set())
        assert not stale, f"{pc}: printeds no longer cited: {stale}"
    assert set(chunks) == set(cited), "snapshot and table citations diverge"


def test_printed_is_recorded_under_cited_chunk() -> None:
    table, chunks = _load()
    failures = [
        (m["id"], name, v["printed"], v["chunk"])
        for m in table["measurements"]
        for name, v in m["values"].items()
        if v["printed"] not in chunks[v["chunk"]]["printeds"]
    ]
    assert not failures, f"printed strings not recorded under cited chunk: {failures}"


def test_row_shape() -> None:
    table, _ = _load()
    assert table["measurements"], "empty reference set"
    for m in table["measurements"]:
        assert re.fullmatch(r"pa\d+", m["paper"]), m["id"]
        assert m["adsorbate"] and m["surface"], m["id"]
        assert m["values"], m["id"]
        for name, v in m["values"].items():
            assert re.fullmatch(r"pc\d+", v["chunk"]), (m["id"], name)
            assert v["printed"], (m["id"], name)
            assert len(v["printed"]) <= _PRINTED_MAX, (m["id"], name)
            # a short bare number is ambiguous evidence — the printed
            # string must also carry the unit or a label
            if len(v["printed"]) < 6:
                assert re.search(r"[A-Za-z]", v["printed"]), (
                    m["id"],
                    name,
                    v["printed"],
                )
            if name.startswith("nu"):
                assert v["dimension"] in {"first_order", "second_order"}, (
                    m["id"],
                    name,
                )
                if v["unit"] in _SECOND_ORDER_UNITS:
                    assert v["dimension"] == "second_order", (m["id"], name)
                else:
                    assert v["unit"] in _FIRST_ORDER_UNITS, (m["id"], name)
                    assert v["dimension"] == "first_order", (m["id"], name)


@pytest.mark.parametrize(
    ("mid", "name", "v"),
    _rows(),
    ids=[f"{mid}::{name}" for mid, name, _ in _rows()],
)
def test_printed_leading_number_matches_value(mid: str, name: str, v: dict) -> None:
    printed_num = _first_number(v["printed"])
    value_num = _value_number(v)
    matches = (
        printed_num is not None
        and value_num is not None
        and abs(printed_num - value_num) < 1e-9
    )
    key = (mid, name)
    if key in _EXEMPTIONS:
        assert not matches, (
            f"stale exemption for {mid}::{name}: "
            f"printed leading number now matches value "
            f"({_EXEMPTIONS[key]})"
        )
        return
    assert matches, (
        f"{mid}::{name}: leading number of printed {v['printed']!r} "
        f"is {printed_num}, value is {v['value']!r}; fix the row or add "
        f"a reasoned entry to _EXEMPTIONS"
    )


def test_exemptions_are_current() -> None:
    known = {(mid, name) for mid, name, _ in _rows()}
    unknown = [key for key in _EXEMPTIONS if key not in known]
    assert not unknown, f"exemptions for removed rows: {unknown}"
