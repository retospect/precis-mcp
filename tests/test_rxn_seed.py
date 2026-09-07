"""The ``rxn`` core property seed must survive the per-test fixture.

Regression guard for a real gap found while building this kind: a
migration-seeded registry table needs BOTH halves of the harness contract that
``material_properties`` / ``component_specs`` already have — an entry in
``conftest._PRESERVE_TABLES`` (so the per-test ``TRUNCATE`` skips it) and an
``_ensure_*_seed`` reseed helper (so an emptied shared template repairs
itself). Missing the first, migration 0157's ``core`` rows were wiped before
every test and `property='yield'` silently took the *mint a proposed property*
path instead of resolving the real seeded row — a false-green shape, since
nothing errors.
"""

from __future__ import annotations

_EXPECTED_CORE = {
    "yield",
    "temperature",
    "time",
    "pressure",
    "catalyst_loading",
    "scale",
    "ee",
    "atom_economy",
    "solvent",
    "price_per_gram",
}


def test_core_seed_is_visible_to_a_test(store) -> None:
    core = {r["prop_id"] for r in store.rxn_properties_list() if r["status"] == "core"}
    missing = _EXPECTED_CORE - core
    assert not missing, (
        f"migration 0157 core properties missing from a test DB: {sorted(missing)} "
        "— check rxn_properties is in conftest._PRESERVE_TABLES"
    )


def test_yield_resolves_as_core_with_its_canonical_unit(store) -> None:
    """The specific row every yield write resolves against. If the seed were
    wiped this would be None, and the handler would mint a `proposed` row with
    whatever unit the first caller happened to pass."""
    prop = store.rxn_property_get("yield")
    assert prop is not None
    assert prop["status"] == "core"
    assert prop["canonical_unit"] == "%"
    assert prop["value_type"] == "ratio"
