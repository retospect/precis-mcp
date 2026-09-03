"""PCB browse tab — FakeStore degradation + real-store integration.

The board pane needs a placed+routed design (fab films); the schematic pane
must work on a freshly-authored netlist with no placement at all — that
asymmetry is asserted here.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig

# ── FakeStore degradation ────────────────────────────────────────────────


def test_pcb_list_empty(client: TestClient) -> None:
    r = client.get("/pcb")
    assert r.status_code == 200
    assert "No pcb designs yet" in r.text


def test_pcb_detail_404(client: TestClient) -> None:
    r = client.get("/pcb/nope")
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_pcb_svg_routes_404(client: TestClient) -> None:
    assert client.get("/pcb/nope/board.svg").status_code == 404
    assert client.get("/pcb/nope/schematic.svg").status_code == 404


# ── real-store integration ───────────────────────────────────────────────


@pytest.fixture
def pcb_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed(runtime_with_store, slug: str = "web_pcb") -> None:
    from precis.handlers.pcb import PcbHandler

    PcbHandler(hub=runtime_with_store.hub).put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": "U1",
                    "label": "MCU-TINY",
                    "pins": [{"name": "1"}, {"name": "2"}, {"name": "3"}],
                },
                {
                    "refdes": "R1",
                    "label": "RES-0402-10k",
                    "pins": [{"name": "1"}, {"name": "2"}],
                },
            ],
            "nets": [
                {"name": "GND", "class": "ground"},
                {"name": "SIG"},
            ],
            "connections": [
                {"net": "GND", "refdes": "U1", "pin": "2"},
                {"net": "GND", "refdes": "R1", "pin": "2"},
                {"net": "SIG", "refdes": "U1", "pin": "1"},
                {"net": "SIG", "refdes": "R1", "pin": "1"},
            ],
        },
    )


def test_list_shows_seeded_design(pcb_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_listed_pcb")
    r = pcb_client.get("/pcb")
    assert r.status_code == 200
    assert "web_listed_pcb" in r.text


def test_detail_drc_tally_counts_only_error_severity_not_warn(
    pcb_client, runtime_with_store
) -> None:
    """The vitals' DRC tally is meant to surface fab-blocking problems, not
    every finding on the board — a warn-severity finding must never inflate
    the count (a `==` -> `!=` flip on the severity filter would count the
    warning and, on this fixture, also drop the real error)."""
    store = runtime_with_store.hub.store
    _seed(runtime_with_store, slug="web_mixed_severity")
    ref = store.get_ref(kind="pcb", id="web_mixed_severity")
    board_id = store.pcb_ensure_board(ref.id)
    store.pcb_write_drc_findings(
        board_id,
        "run1",
        [
            {"rule": "clearance", "severity": "error", "objects": [], "detail": "…"},
            {"rule": "trace_width", "severity": "warn", "objects": [], "detail": "…"},
        ],
    )
    r = pcb_client.get("/pcb/web_mixed_severity")
    assert r.status_code == 200
    assert "clearance" in r.text
    assert "trace_width" not in r.text


def test_detail_shows_vitals_and_both_panes(pcb_client, runtime_with_store) -> None:
    _seed(runtime_with_store)
    r = pcb_client.get("/pcb/web_pcb")
    assert r.status_code == 200
    assert "2 part(s)" in r.text
    assert "2 net(s)" in r.text
    assert "/pcb/web_pcb/board.svg" in r.text
    assert "/pcb/web_pcb/schematic.svg" in r.text


def test_schematic_renders_before_any_placement(pcb_client, runtime_with_store) -> None:
    _seed(runtime_with_store, slug="web_schem")
    r = pcb_client.get("/pcb/web_schem/schematic.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert ">U1</text>" in r.text
    assert ">SIG</text>" in r.text  # signal net label
    assert ">GND</text>" not in r.text  # ground draws as the glyph


def test_board_endpoint_renders_even_before_placement(
    pcb_client, runtime_with_store
) -> None:
    """The fab renderer degrades gracefully on an unplaced design (empty
    films, not an error) — the endpoint serves whatever it produces."""
    _seed(runtime_with_store, slug="web_bare")
    r = pcb_client.get("/pcb/web_bare/board.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")


def test_board_endpoint_422_when_render_raises(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    """A render failure answers with the 422 explanation, never a 500."""
    _seed(runtime_with_store, slug="web_boom")

    def boom(self, **kw):
        raise RuntimeError("films exploded")

    monkeypatch.setattr("precis.handlers.pcb.PcbHandler.get", boom)
    r = pcb_client.get("/pcb/web_boom/board.svg")
    assert r.status_code == 422
    assert "place + route" in r.text
