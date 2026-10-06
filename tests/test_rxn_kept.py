"""Kept equation inputs use public rxn verbs without SMILES or measured values."""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from precis.errors import BadInput
from precis.handlers.rxn import RxnHandler

EQUATION = "NO + H2 -> NH3 + H2O"
IDENTITY_METADATA = [
    ("rxn_smiles_raw", "[H][H]>>[H][H]"),
    ("rxn_smiles", "[H][H]>>[H][H]"),
    ("uid_transform", "unsupported-transform"),
    ("uid_strict", "unsupported-strict"),
    ("desired_product", "[H][H]"),
]


def _error_text(result: Any) -> str:
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    return "\n".join(
        block.text for block in result.content if isinstance(block, TextContent)
    )


def _stored_row(store: Any, ref_id: int) -> Any:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT * FROM refs WHERE ref_id = %s", (ref_id,)).fetchone()
    assert row is not None
    return row


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


@pytest.mark.parametrize(("key", "value"), IDENTITY_METADATA)
def test_public_keep_rejects_incoming_identity_before_create(
    monkeypatch: Any, runtime_with_store: Any, key: str, value: str
) -> None:
    from precis.tools import core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    response = core.put(
        kind="rxn",
        id="mixed-ledger",
        meta={"energetics": {"q": EQUATION}, key: value},
    )
    error = _error_text(response)
    assert "[error:BadInput]" in error
    assert "separately from a SMILES reaction" in error
    assert runtime_with_store.store.get_ref(kind="rxn", id="mixed-ledger") is None


@pytest.mark.parametrize(("key", "value"), IDENTITY_METADATA)
@pytest.mark.parametrize("include_equations", [False, True])
def test_public_identity_update_refused_without_row_mutation(
    monkeypatch: Any,
    runtime_with_store: Any,
    key: str,
    value: str,
    include_equations: bool,
) -> None:
    from precis.tools import core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    core.put(
        kind="rxn",
        id="unchanged-ledger",
        title="Original ledger",
        reaction_class="RXNO:0000024",
        meta={"energetics": {"q": EQUATION, "T": 300}},
    )
    store = runtime_with_store.store
    ref = store.get_ref(kind="rxn", id="unchanged-ledger")
    assert ref is not None
    before = _stored_row(store, ref.id)
    meta: dict[str, Any] = {key: value}
    if include_equations:
        meta["energetics"] = {"q": EQUATION, "T": 400}
    refused = core.put(
        kind="rxn",
        id="unchanged-ledger",
        title="Must not replace title",
        reaction_class="RXNO:0000001",
        meta=meta,
    )
    error = _error_text(refused)
    assert "[error:BadInput]" in error
    assert "separately from a SMILES reaction" in error
    assert _stored_row(store, ref.id) == before


def test_existing_mixed_metadata_refuses_metadata_only_update(
    monkeypatch: Any, runtime_with_store: Any
) -> None:
    from precis.tools import core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    store = runtime_with_store.store
    # Simulate an externally authored pre-fix row; public put cannot create it.
    ref, _ = store.rxn_entity_upsert(
        slug="legacy-mixed",
        title="Original mixed row",
        meta_patch={
            "energetics": {"q": EQUATION, "T": 300},
            "uid_transform": "unsupported-transform",
        },
    )
    before = _stored_row(store, ref.id)
    refused = core.put(
        kind="rxn",
        id="legacy-mixed",
        title="Must not replace title",
        meta={"notes": "new"},
    )
    assert "[error:BadInput]" in _error_text(refused)
    assert _stored_row(store, ref.id) == before


def test_public_kept_class_normalization_on_create_and_update(
    monkeypatch: Any, runtime_with_store: Any
) -> None:
    from precis.tools import core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)
    created = core.put(
        kind="rxn",
        id="classified-ledger",
        reaction_class="  RXNO:0000024  ",
        meta={
            "energetics": {"q": EQUATION, "T": 300},
            "reaction_class": "meta-fallback",
        },
    )
    assert "created rxn classified-ledger" in created
    ref = runtime_with_store.store.get_ref(kind="rxn", id="classified-ledger")
    assert ref is not None
    assert ref.meta["reaction_class"] == "RXNO:0000024"
    updated = core.put(
        kind="rxn",
        id="classified-ledger",
        reaction_class="  RXNO:0000001  ",
        meta={
            "energetics": {"q": EQUATION, "T": 400},
            "reaction_class": "meta-fallback",
        },
    )
    assert "updated rxn classified-ledger" in updated
    after = runtime_with_store.store.get_ref(kind="rxn", id="classified-ledger")
    assert after is not None
    assert after.meta["reaction_class"] == "RXNO:0000001"
    assert after.meta["energetics"]["T"] == 400.0
    assert "class: RXNO:0000001" in core.get(kind="rxn", id="classified-ledger")
