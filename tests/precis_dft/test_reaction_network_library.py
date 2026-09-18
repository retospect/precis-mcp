"""The built-in reaction-network YAML library loads + validates.

Verifies:
- All 13 expected networks are present.
- Each carries the required top-level fields (id, name, species,
  steps, references).
- Each step carries the required fields (reactants, products, n_e,
  n_H).
- IDs are unique.
"""

from __future__ import annotations

import importlib.resources

import yaml

_LIB = "precis_dft.data.reaction_networks"

_EXPECTED_IDS = {
    "reaction:oer_4step_acid",
    "reaction:oer_4step_alkaline",
    "reaction:oer_lom",
    "reaction:oer_dual_site",
    "reaction:her_volmer_heyrovsky",
    "reaction:her_volmer_tafel",
    "reaction:orr_4step_associative",
    "reaction:orr_4step_dissociative",
    "reaction:co2rr_to_co",
    "reaction:co2rr_to_methanol",
    "reaction:co2rr_to_ethylene",
    "reaction:nrr_alternating",
    "reaction:nrr_distal",
}


def _load_library() -> dict[str, dict]:
    out: dict[str, dict] = {}
    package = importlib.resources.files(_LIB)
    for entry in package.iterdir():
        if entry.is_file() and entry.name.endswith(".yaml"):
            with entry.open("r", encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            assert isinstance(doc, dict), f"{entry.name} is not a YAML mapping"
            assert "id" in doc, f"{entry.name} missing 'id'"
            out[doc["id"]] = doc
    return out


def test_all_expected_networks_present() -> None:
    library = _load_library()
    missing = _EXPECTED_IDS - set(library)
    assert not missing, f"missing networks: {sorted(missing)}"


def test_no_duplicate_ids() -> None:
    library = _load_library()
    # Loader already de-dups by overwriting; cross-check the count.
    package = importlib.resources.files(_LIB)
    file_count = sum(
        1 for e in package.iterdir() if e.is_file() and e.name.endswith(".yaml")
    )
    assert len(library) == file_count, "duplicate ids across YAML files"


def test_each_network_has_required_fields() -> None:
    library = _load_library()
    for net_id, doc in library.items():
        for field in ("id", "name", "species", "steps", "references"):
            assert field in doc, f"{net_id} missing top-level field {field!r}"
        assert isinstance(doc["species"], list) and doc["species"], (
            f"{net_id}: species must be a non-empty list"
        )
        assert isinstance(doc["steps"], list) and doc["steps"], (
            f"{net_id}: steps must be a non-empty list"
        )


def test_each_step_has_required_fields() -> None:
    library = _load_library()
    for net_id, doc in library.items():
        for step in doc["steps"]:
            for field in ("id", "reactants", "products", "n_e", "n_H"):
                assert field in step, (
                    f"{net_id} step {step.get('id', '?')} missing {field!r}"
                )
