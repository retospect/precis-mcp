"""``GET /se/<slug>/pick`` — the 3D viewer's pick panel reads the same
level list as ``get(kind='se', view='pick')``, as JSON rows innermost
first (``docs/backlog/se-pick-hierarchy.md``, the render half)."""

from __future__ import annotations

import re
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store import Store
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.test_se_chain_realize import (
    _atoms,
    _hairpin_ops,
    _loaded,
    _put,
    _realize,
)
from tests.test_se_pick import _ordinal, _rows


@pytest.fixture
def pick_client(store: Store, runtime_with_store: Any, tmp_path: Any) -> TestClient:
    handler = SeHandler(hub=runtime_with_store.hub)
    _put(
        handler,
        "pkw",
        [*_hairpin_ops(), {"op": "relax_chain"}, _realize("stem", 0, 4, loops=True)],
    )
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_an_atom_pick_returns_the_same_rows_as_view_pick(
    pick_client: TestClient, store: Store, runtime_with_store: Any
) -> None:
    tree = _loaded(store, "pkw")
    uid = int(tree.blocks["stem.s0"].uid or 0)
    _coords, meta = _atoms(store, "pkw-stem.s0")
    ordinal = _ordinal(meta, "O3'", 4)

    r = pick_client.get("/se/pkw/pick", params={"block": f"#{uid}", "atom": ordinal})
    assert r.status_code == 200, r.text
    body = r.json()
    expected = _rows(
        SeHandler(hub=runtime_with_store.hub)
        .get(id="pkw", view="pick", args={"block": "stem.s0", "atom": ordinal})
        .body
    )
    assert [(x["level"], x["label"], x["token"]) for x in body["levels"]] == expected
    assert body["levels"][0]["token"] == f"<se:{uid}#{ordinal}>"
    assert "stem.s0" in body["subject"]


def test_a_block_token_pick_lists_the_block_and_its_ancestors(
    pick_client: TestClient, store: Store
) -> None:
    tree = _loaded(store, "pkw")
    uid = int(tree.blocks["stem.s0"].uid or 0)
    r = pick_client.get("/se/pkw/pick", params={"token": f"<se:{uid}>"})
    assert r.status_code == 200, r.text
    levels = r.json()["levels"]
    assert levels[0] == {"level": "segment", "label": "stem.s0", "token": f"<se:{uid}>"}
    assert [x["label"] for x in levels[1:]] == ["stem"]


def test_pick_refusals_are_json_with_the_resolving_message(
    pick_client: TestClient, store: Store
) -> None:
    tree = _loaded(store, "pkw")
    uid = int(tree.blocks["stem.s0"].uid or 0)
    # Neither form → 400 naming both.
    r = pick_client.get("/se/pkw/pick")
    assert r.status_code == 400
    assert "token" in r.json()["error"]
    # An ordinal past the end → 400 naming the range.
    r = pick_client.get("/se/pkw/pick", params={"block": f"#{uid}", "atom": 10**6})
    assert r.status_code == 400
    assert "ordinals 0.." in r.json()["error"]
    # An unknown block → 404; an unknown design → 404.
    assert (
        pick_client.get("/se/pkw/pick", params={"block": "nope", "atom": 0}).status_code
        == 404
    )
    assert (
        pick_client.get("/se/no-such/pick", params={"token": f"<se:{uid}>"}).status_code
        == 404
    )


def test_the_3d_page_wires_the_pick_panel(pick_client: TestClient) -> None:
    html = pick_client.get("/se/pkw").text
    assert 'id="bt3d-pick-panel"' in html
    assert '"/se/pkw/pick"' in html


def test_atomic3d_carries_a_hover_row_set_per_atom(pick_client: TestClient) -> None:
    blocks = pick_client.get("/se/pkw/atomic3d.json").json()["blocks"]
    assert blocks
    for b in blocks:
        hover = b["hover"]
        # A realize_chain structure sends atom names plus residue rows, each
        # residue once, not the scene label.
        assert len(hover["atom"]) == len(hover["residue"]) == len(b["elements"])
        assert all(0 <= r < len(hover["residues"]) for r in hover["residue"])
        for label, chain, strand in hover["residues"]:
            assert re.fullmatch(r"D[ACGT] \d+ = deoxy\w+", label)
            assert chain and strand


def test_the_3d_page_wires_the_view_export(pick_client: TestClient) -> None:
    html = pick_client.get("/se/pkw").text
    assert 'id="bt3d-export-png"' in html
    assert 'id="bt3d-export-svg"' in html
    assert 'exportName: "pkw"' in html
