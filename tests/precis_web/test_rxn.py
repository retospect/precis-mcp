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
from precis_web.routes.rxn import doi_link_map

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


def test_form_defaults_and_four_step_handler_ledger(
    rxn_client: TestClient, runtime_with_store: Any
) -> None:
    landing = rxn_client.get("/rxn")
    assert landing.status_code == 200
    assert 'value="298.15"' in landing.text
    assert 'href="/rxn"' in landing.text
    assert "No barriers and no electrode reference" in landing.text

    response = rxn_client.get("/rxn", params={"q": EQUATIONS, "T": "300"})
    assert response.status_code == 200
    expected = render_energetics(EQUATIONS, T=300).body
    doi_links = doi_link_map(runtime_with_store.store, expected)
    for table in re.findall(
        r"<table\b.*?</table>",
        str(linkify_toon(expected, doi_links=doi_links)),
        re.S,
    ):
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
    body = render_energetics(EQUATIONS, T=300).body
    doi_links = doi_link_map(runtime_with_store.store, body)
    for table in re.findall(
        r"<table\b.*?</table>",
        str(linkify_toon(body, doi_links=doi_links)),
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


def test_rxn_link_verb_and_put_link(
    rxn_client: TestClient, runtime_with_store: Any
) -> None:
    """gr489578: rxn takes the generic link door — ``rel='cites'`` from the
    rxn side, and ``put(link=, rel=)`` rides the entity write."""
    store = runtime_with_store.store
    paper = store.insert_ref(kind="paper", slug="g18", title="Glarborg 2018")
    body, err = runtime_with_store.dispatch_with_status(
        "put",
        {
            "kind": "rxn",
            "id": "nh3-synth",
            "meta": {"energetics": {"q": "NO + 5/2 H2 -> NH3 + H2O"}},
            "link": f"pa{paper.id}",
            "rel": "cites",
        },
    )
    assert not err, body
    ref = store.get_ref(kind="rxn", id="nh3-synth")
    assert ref is not None
    cited = [
        lk.dst_ref_id
        for lk in store.links_for(ref.id, direction="out", relation="cites")
    ]
    assert cited == [paper.id]
    body, err = runtime_with_store.dispatch_with_status(
        "link",
        {
            "kind": "rxn",
            "id": f"rx{ref.id}",
            "target": f"pa{paper.id}",
            "rel": "cites",
            "mode": "remove",
        },
    )
    assert not err, body
    assert store.links_for(ref.id, direction="out", relation="cites") == []
    body, err = runtime_with_store.dispatch_with_status(
        "link",
        {"kind": "rxn", "id": "nh3-synth", "target": f"pa{paper.id}", "rel": "cites"},
    )
    assert not err, body
    cited = [
        lk.dst_ref_id
        for lk in store.links_for(ref.id, direction="out", relation="cites")
    ]
    assert cited == [paper.id]


def test_rxn_reader_links_doi_source_cells(
    rxn_client: TestClient, runtime_with_store: Any
) -> None:
    """gr489578: a ``doi:<id>`` source cite links to the paper page when
    the DOI is held, else to the drive lookup that lists what cites it."""
    store = runtime_with_store.store
    doi = "10.1016/j.pecs.2018.01.002"
    params = {"q": EQUATIONS, "T": "298.15"}
    unheld = rxn_client.get("/rxn", params=params)
    assert unheld.status_code == 200 and f"doi:{doi}" in unheld.text
    assert f'href="/drive?q=doi%3A{doi.replace("/", "%2F")}"' in unheld.text
    paper = store.insert_ref(kind="paper", slug="glarborg18", title="G")
    store.insert_ref_identifiers(paper.id, [("doi", doi, "test")])
    held = rxn_client.get("/rxn", params=params)
    assert f'href="/papers/{paper.id}"' in held.text
    kept = rxn_client.post(
        "/rxn/keep", data={**params, "n_electrons": ""}, follow_redirects=False
    )
    page = rxn_client.get(kept.headers["location"])
    assert f'href="/papers/{paper.id}"' in page.text
