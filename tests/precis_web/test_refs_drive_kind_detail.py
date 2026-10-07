"""Drive click-through for the design/supply kinds whose browse surface is
Drive but whose detail link falls back to ``/refs/<kind>/<id>``.

Before this, a Drive row for ``pcb`` / ``component`` / ``material`` linked a
route that 400'd ("no browse tab for kind=…") — the kinds were missing from
``_REFS_BROWSABLE_KINDS``. ``pcb`` now 303s to its workbench (like
``structure``); ``component`` / ``material`` have no dedicated reader, so
they render the handler's own card through the generic detail template.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.component import ComponentHandler
from precis.handlers.material import MaterialHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.item_view import ItemPresenter
from precis_web.routes.refs import _CONSOLIDATED_KINDS


@pytest.fixture
def kind_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def test_drive_row_links_pcb_workbench_not_generic_refs() -> None:
    """The Drive row itself points straight at ``/pcb/<slug>`` — no hop."""

    class _Ref:
        kind = "pcb"
        id = 345846
        slug = "unicycle-c1"
        title = "a board"

    assert ItemPresenter("pcb").open_url(_Ref()) == "/pcb/unicycle-c1"


def test_refs_pcb_detail_redirects_to_workbench(kind_client, runtime_with_store):
    """Every other surface's generic ``/refs/pcb/<id>`` fallback lands on
    the workbench too, rather than 400ing."""
    from precis.handlers.pcb import PcbHandler

    store = runtime_with_store.hub.store
    PcbHandler(hub=runtime_with_store.hub).put(
        id="refs_redir_board",
        args={
            "components": [
                {"refdes": "R1", "label": "RES-0402-10k", "pins": [{"name": "1"}]}
            ],
            "nets": [{"name": "SIG"}],
            "connections": [{"net": "SIG", "refdes": "R1", "pin": "1"}],
        },
    )
    ref = store.get_ref(kind="pcb", id="refs_redir_board")
    r = kind_client.get(f"/refs/pcb/{ref.id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/pcb/refs_redir_board"


def test_refs_component_detail_renders_handler_card(kind_client, runtime_with_store):
    store = runtime_with_store.hub.store
    ComponentHandler(hub=runtime_with_store.hub).put(
        id="m6-a2-bolt",
        title="M6x20 A2 socket cap",
        category="fastener",
    )
    ref = store.get_ref(kind="component", id="m6-a2-bolt")
    r = kind_client.get(f"/refs/component/{ref.id}")
    assert r.status_code == 200
    assert "M6x20 A2 socket cap" in r.text
    assert "fastener" in r.text


def test_refs_material_detail_renders_handler_card(kind_client, runtime_with_store):
    store = runtime_with_store.hub.store
    MaterialHandler(hub=runtime_with_store.hub).put(
        id="6061-t6",
        title="Aluminum 6061-T6",
        meta={"material_class": "metal"},
    )
    ref = store.get_ref(kind="material", id="6061-t6")
    r = kind_client.get(f"/refs/material/{ref.id}")
    assert r.status_code == 200
    assert "Aluminum 6061-T6" in r.text


def test_refs_list_route_renders_for_the_newly_browsable_kinds(kind_client):
    """Making the kinds browsable also opens their ``/refs/<kind>`` list —
    the quest dashboard's servers-lite chips link it (``_quest_detail``), so
    it must render rather than 500."""
    for kind in ("pcb", "component", "material"):
        assert kind_client.get(f"/refs/{kind}").status_code == 200


def test_drive_kinds_stay_out_of_the_consolidated_browser() -> None:
    """Detail-only: making the three kinds browsable must not add a second
    browse grid beside Drive's own kind facet."""
    for kind in ("pcb", "component", "material"):
        assert kind not in _CONSOLIDATED_KINDS


def test_component_supplier_block_is_lazy_and_never_cached(
    kind_client, runtime_with_store, monkeypatch
):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from precis.supply.live import identity_link

    store = runtime_with_store.hub.store
    ComponentHandler(hub=runtime_with_store.hub).put(
        id="dogfood-web-live", category="electronic"
    )
    ref = store.get_ref(kind="component", id="dogfood-web-live")
    record = {
        "supplier_part_number": "SKU1",
        "mpn": "MPN1",
        "manufacturer": "Maker",
        "product_url": "https://www.digikey.com/part/SKU1",
        "datasheet_url": None,
        "match_confidence": "exact_mpn",
        "description": "LIVE-MARKER <script>alert(1)</script>",
    }
    store.component_supplier_link(
        ref.id, identity_link("digikey", record, datetime.now(UTC))
    )
    calls = []

    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return record

    monkeypatch.setattr(
        "precis.supply.live.supplier_adapter",
        lambda name: SimpleNamespace(configured=lambda: None, record=fetch),
    )
    initial = kind_client.get(f"/refs/component/{ref.id}")
    assert initial.status_code == 200 and calls == []
    assert (
        "Load live supplier data" in initial.text and "LIVE-MARKER" not in initial.text
    )
    live = kind_client.get(f"/refs/component/{ref.id}/supplier-live")
    assert live.status_code == 200 and len(calls) == 1
    assert live.headers["cache-control"] == "no-store"
    assert "LIVE-MARKER" in live.text and "not stored" in live.text
    assert "<script>alert(1)</script>" not in live.text
    assert kind_client.get("/refs/component/999999999/supplier-live").status_code == 404
