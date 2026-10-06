"""Actual runtime/DB coverage for the reaction ledger and explicit keep."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers._rxn_energetics import render_energetics
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.linkify import linkify_toon

EQUATIONS = (
    "NO + 1/2 H2 -> HNO; HNO + 1/2 H2 -> H2NO; "
    "H2NO + 1/2 H2 -> NH2OH; NH2OH + H2 -> NH3 + H2O"
)


@pytest.fixture
def rxn_client(runtime_with_store: Any, tmp_path: Any) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_form_defaults_and_four_step_handler_ledger(rxn_client: TestClient) -> None:
    landing = rxn_client.get("/rxn")
    assert landing.status_code == 200
    assert 'value="298.15"' in landing.text
    assert 'href="/rxn"' in landing.text
    assert "No barriers and no electrode reference" in landing.text

    response = rxn_client.get("/rxn", params={"q": EQUATIONS, "T": "300"})
    assert response.status_code == 200
    expected = render_energetics(EQUATIONS, T=300).body
    for table in re.findall(r"<table\b.*?</table>", str(linkify_toon(expected)), re.S):
        assert table in response.text
    assert response.text.count("<table") >= 5
    assert "cum_dG" in response.text
    assert response.text.count("yes (ΔG)") == 1
    assert "no explicit author licence grant" in response.text
    assert "original reference pressure unverified" in response.text
    assert 'formaction="/rxn/keep"' in response.text


def test_keep_round_trip_reuses_ref_and_reader_recomputes(
    rxn_client: TestClient, runtime_with_store: Any
) -> None:
    data = {"q": EQUATIONS, "T": "300", "n_electrons": ""}
    first = rxn_client.post("/rxn/keep", data=data, follow_redirects=False)
    assert first.status_code == 303
    url = first.headers["location"]
    assert url.startswith("/refs/rxn/")
    ref = runtime_with_store.store.get_ref(kind="rxn", id=int(url.rsplit("/", 1)[1]))
    assert ref is not None
    assert ref.kind == "rxn"
    assert ref.meta["energetics"] == {
        "q": EQUATIONS,
        "T": 300.0,
        "n_electrons": None,
    }
    assert "rxn_smiles" not in ref.meta
    assert ref.meta["energetics_snapshot"] == render_energetics(EQUATIONS, T=300).body
    second = rxn_client.post("/rxn/keep", data=data, follow_redirects=False)
    assert second.headers["location"] == url
    # Corrupting an archival snapshot must not affect the derived reader.
    runtime_with_store.store.rxn_entity_upsert(
        slug=ref.slug,
        title=ref.title,
        meta_patch={"energetics_snapshot": "STALE SNAPSHOT"},
    )
    reader = rxn_client.get(url)
    assert reader.status_code == 200
    assert "Derived ledger recomputed" in reader.text
    assert "STALE SNAPSHOT" not in reader.text
    for table in re.findall(
        r"<table\b.*?</table>",
        str(linkify_toon(render_energetics(EQUATIONS, T=300).body)),
        re.S,
    ):
        assert table in reader.text


@pytest.mark.parametrize(
    ("q", "T", "error"),
    [
        (EQUATIONS, "0", "finite and > 0"),
        (EQUATIONS, "6000.001", "T=6000.001 is out of range"),
        (EQUATIONS, "not-a-temperature", "must be a number"),
        ("H2 -> O2", "300", "no valid balance"),
    ],
)
def test_refusals_preserve_handler_text_and_do_not_store(
    rxn_client: TestClient, runtime_with_store: Any, q: str, T: str, error: str
) -> None:
    before = runtime_with_store.store.list_refs(kind="rxn")
    data = {"q": q, "T": T}
    for response in (
        rxn_client.get("/rxn", params=data),
        rxn_client.post("/rxn/keep", data=data),
    ):
        assert response.status_code == 400
        assert error in unescape(response.text)
        assert 'role="alert"' in response.text
        assert "[error:BadInput]" in response.text
    assert runtime_with_store.store.list_refs(kind="rxn") == before


def test_optional_n_electrons_is_used_and_pathway_refused(
    rxn_client: TestClient,
) -> None:
    response = rxn_client.get(
        "/rxn",
        params={"q": "NO + H2 -> NH3 + H2O", "T": "298.15", "n_electrons": "5"},
    )
    assert response.status_code == 200
    assert "E° = 0.69 V" in response.text
    kept = rxn_client.post(
        "/rxn/keep",
        data={"q": "NO + H2 -> NH3 + H2O", "T": "298.15", "n_electrons": "5"},
    )
    assert kept.status_code == 200
    assert "E° = 0.69 V" in kept.text
    refused = rxn_client.post(
        "/rxn/keep", data={"q": EQUATIONS, "T": "300", "n_electrons": "5"}
    )
    assert refused.status_code == 400
    assert "n_electrons is supported for a single reaction only" in refused.text
