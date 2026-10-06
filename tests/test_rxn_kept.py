"""Kept equation inputs use public rxn verbs without SMILES or measured values."""

from __future__ import annotations

from typing import Any

import pytest

from precis.errors import BadInput
from precis.handlers.rxn import RxnHandler

EQUATION = "NO + H2 -> NH3 + H2O"


def test_public_put_and_derived_get_retain_inputs_and_temperature_override(
    monkeypatch: Any, runtime_with_store: Any
) -> None:
    from precis.tools import core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    created = core.put(
        kind="rxn",
        id="kept-no-nh3",
        title="NO reduction",
        meta={"energetics": {"q": EQUATION, "T": 300, "n_electrons": 5}},
    )
    assert "created rxn kept-no-nh3" in created
    ref = runtime_with_store.store.get_ref(kind="rxn", id="kept-no-nh3")
    assert ref is not None
    assert ref.meta["energetics"]["T"] == 300.0
    assert ref.meta["energetics"]["n_electrons"] == 5.0
    assert "NO + 5/2 H2 -> NH3 + H2O" in ref.meta["energetics_snapshot"]
    assert not runtime_with_store.store.rxn_values_for_ref(ref.id)
    page = core.get(kind="rxn", id="kept-no-nh3")
    assert "Derived ledger recomputed" in page
    assert "at 300 K" in page
    assert "rdkit was unavailable" not in page
    overridden = core.get(
        kind="rxn", id="kept-no-nh3", view="energetics", args={"T": 400}
    )
    assert "at 400 K" in overridden
    assert runtime_with_store.store.get_ref(kind="rxn", id=ref.slug).meta == ref.meta


@pytest.mark.parametrize(
    "inputs",
    [None, {"T": 300}, {"q": EQUATION, "extra": 1}, {"q": "H2 -> O2"}],
)
def test_invalid_keeps_write_nothing(hub: Any, inputs: Any) -> None:
    handler = RxnHandler(hub=hub)
    with pytest.raises(BadInput):
        handler.put(id="invalid-keep", meta={"energetics": inputs})
    assert hub.store.get_ref(kind="rxn", id="invalid-keep") is None


def test_kept_equations_and_smiles_do_not_overwrite_each_other(hub: Any) -> None:
    handler = RxnHandler(hub=hub)
    result = handler.put(id="equations", meta={"energetics": {"q": EQUATION}})
    assert result.ref_id is not None
    assert result.reused is False
    repeated = handler.put(id="equations", meta={"energetics": {"q": EQUATION}})
    assert repeated.ref_id == result.ref_id
    assert repeated.reused is True
    with pytest.raises(BadInput, match="separately"):
        handler.put(id="equations", rxn_smiles="[H][H]>>[H][H]")
    handler.put(id="smiles", rxn_smiles="[H][H]>>[H][H]")
    with pytest.raises(BadInput, match="separately"):
        handler.put(id="smiles", meta={"energetics": {"q": EQUATION}})


def test_sourced_values_on_kept_ref_remain_visible(hub: Any) -> None:
    handler = RxnHandler(hub=hub)
    handler.put(id="kept-with-fact", meta={"energetics": {"q": EQUATION}})
    handler.put(id="kept-with-fact", property="yield", value=83, unit="%")
    body = handler.get(id="kept-with-fact").body
    assert "Derived ledger recomputed" in body
    assert "83" in body
    assert "yield" in body
