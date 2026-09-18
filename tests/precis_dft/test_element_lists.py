"""Element lists YAML is well-formed."""

from __future__ import annotations

import importlib.resources

import yaml


def _load() -> dict:
    with (
        importlib.resources.files("precis_dft.data")
        .joinpath("element_lists.yaml")
        .open("r", encoding="utf-8") as f
    ):
        return yaml.safe_load(f)


def test_required_keys_present() -> None:
    doc = _load()
    for key in (
        "magnetic_elements",
        "soc_recommended",
        "soc_required",
        "vdw_strongly_recommended",
        "adsorbate_species",
    ):
        assert key in doc, f"missing top-level key {key!r}"


def test_adsorbate_ids_start_with_star() -> None:
    doc = _load()
    for adsorbate_id in doc["adsorbate_species"]:
        assert adsorbate_id.startswith("*"), (
            f"adsorbate id {adsorbate_id!r} must start with '*'"
        )


def test_magnetic_elements_are_symbols() -> None:
    doc = _load()
    for element in doc["magnetic_elements"]:
        assert isinstance(element, str), element
        assert 1 <= len(element) <= 2, element
        assert element[0].isupper(), element
