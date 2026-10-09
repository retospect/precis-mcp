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


def test_pcb_index_redirects_to_drive_kind_pcb(client: TestClient) -> None:
    """``/pcb`` (the list) is retired into the unified Drive surface —
    it redirects to the ``kind=pcb`` facet preset, the same target
    ``_drive_back.html.j2``'s back-link uses. The workbench
    (``/pcb/{slug}`` and its SVG endpoints) is unaffected — see
    ``test_pcb_detail_404``/``test_detail_shows_vitals_and_both_panes``
    below, which prove the ``{slug}`` route still resolves and isn't
    swallowed by this redirect."""
    r = client.get("/pcb", follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/drive?k=pcb&folder=*&sort=recency"


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


# ── argue with the design: POST /pcb/{slug}/note
#    (docs/backlog/pcb-argue-with-design.md slice 1) ────────────────────────


def _fake_router(monkeypatch, answer: str = "Move R1 next to U1.") -> list:
    """Stub the LLM router at the seam ``precis.pcb.argue.ask`` calls
    (``route``), recording every request it saw."""
    import precis.utils.llm.router as router

    seen: list = []

    def fake_route(req):
        seen.append(req)
        return router.LlmResult(
            text=answer, cost_usd=0.01, turns_used=1, model="m", tier=req.tier
        )

    monkeypatch.setattr(router, "route", fake_route)
    return seen


def test_note_resolves_clicked_handles_stores_verbatim_and_answers(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    store = runtime_with_store.hub.store
    _seed(runtime_with_store, slug="web_argue")
    seen = _fake_router(monkeypatch)
    text = "  U1.1 should not share net:SIG with R1 — move R1 closer.\n"
    r = pcb_client.post(
        "/pcb/web_argue/note",
        json={"text": text, "handles": ["U1.1", "net:SIG", "R1"]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] and not data["degraded"]
    assert data["about"] == ["U1.1", "net:SIG", "R1"]
    ref = store.get_ref(kind="pcb", id="web_argue")
    notes = store.pcb_notes_list(ref.id)
    assert [n["kind"] for n in notes] == ["question", "answer"]
    q, a = notes
    assert q["body"] == text  # byte-identical, not stripped or rewritten
    assert q["about"] == ["U1.1", "net:SIG", "R1"]
    assert q["origin"] == "user"
    assert a["re"] == q["name"] and a["origin"] == "proposed"
    assert a["body"] == "Move R1 next to U1."
    assert a["about"] == q["about"]
    # The model saw the resolved context for each handle, not just names.
    assert len(seen) == 1
    prompt = seen[0].prompt
    assert "- U1.1 (pin):" in prompt and "'net': 'SIG'" in prompt
    assert "- net:SIG (net):" in prompt and "'refdes': 'R1'" in prompt
    assert "- R1 (part):" in prompt and "RES-0402-10k" in prompt
    assert seen[0].source == "pcb-argue"
    # The response carries the re-rendered ledger with both notes.
    assert 'data-count="2"' in data["html"]
    assert "Move R1 next to U1." in data["html"]


def test_note_rejects_an_unknown_handle_with_the_valid_roster(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    store = runtime_with_store.hub.store
    _seed(runtime_with_store, slug="web_argue_bad")
    seen = _fake_router(monkeypatch)
    r = pcb_client.post(
        "/pcb/web_argue_bad/note",
        json={"text": "U7.2 looks shorted", "handles": ["U7.2", "U1"]},
    )
    assert r.status_code == 400
    data = r.json()
    assert data["unknown"] == ["U7.2"]
    assert "U7.2" in data["error"]
    assert data["valid"]["parts"] == ["R1", "U1"]
    assert "U1.3" in data["valid"]["pins"] and "R1.2" in data["valid"]["pins"]
    assert data["valid"]["nets"] == ["net:GND", "net:SIG"]
    ref = store.get_ref(kind="pcb", id="web_argue_bad")
    assert store.pcb_notes_list(ref.id) == []  # nothing stored
    assert seen == []  # and the model was never asked


def test_whole_design_argument_needs_no_handle_and_typed_handles_count(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    store = runtime_with_store.hub.store
    _seed(runtime_with_store, slug="web_argue_whole")
    _fake_router(monkeypatch, answer="Agreed.")
    r = pcb_client.post(
        "/pcb/web_argue_whole/note", json={"text": "the whole board is too dense"}
    )
    assert r.status_code == 200, r.text
    ref = store.get_ref(kind="pcb", id="web_argue_whole")
    q = store.pcb_notes_list(ref.id)[0]
    assert q["about"] == []  # "the" is prose, not a handle
    # A handle typed by hand (no click) resolves and lands in about too.
    r = pcb_client.post(
        "/pcb/web_argue_whole/note", json={"text": "and R1.2 is floating"}
    )
    assert r.status_code == 200
    assert store.pcb_notes_list(ref.id)[2]["about"] == ["R1.2"]


def test_note_degrades_to_recorded_unanswered_when_the_model_fails(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    store = runtime_with_store.hub.store
    _seed(runtime_with_store, slug="web_argue_down")

    def boom(**kw):
        raise RuntimeError("router offline")

    monkeypatch.setattr("precis.pcb.argue.ask", boom)
    r = pcb_client.post("/pcb/web_argue_down/note", json={"text": "U1 is upside down"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["degraded"] is True and data["answer"] is None
    assert "router offline" in data["detail"]
    ref = store.get_ref(kind="pcb", id="web_argue_down")
    notes = store.pcb_notes_list(ref.id)
    assert len(notes) == 1 and notes[0]["kind"] == "question"
    assert notes[0]["about"] == ["U1"]


def test_note_route_404_and_400_shapes(pcb_client, runtime_with_store) -> None:
    assert pcb_client.post("/pcb/nope/note", json={"text": "x"}).status_code == 404
    _seed(runtime_with_store, slug="web_argue_shapes")
    assert (
        pcb_client.post("/pcb/web_argue_shapes/note", json={"text": "  "}).status_code
        == 400
    )
    assert (
        pcb_client.post(
            "/pcb/web_argue_shapes/note", json={"text": "x", "handles": "U1"}
        ).status_code
        == 400
    )
    r = pcb_client.post(
        "/pcb/web_argue_shapes/note",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400


def test_detail_page_has_the_text_box_no_place_or_route_button_and_the_notes(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    store = runtime_with_store.hub.store
    _seed(runtime_with_store, slug="web_argue_page")
    _fake_router(monkeypatch, answer="Swap them.")
    pcb_client.post(
        "/pcb/web_argue_page/note",
        json={"text": "R1 and U1 are swapped", "handles": ["R1", "U1"]},
    )
    # A part retired after the argument reads as a dangling anchor, not an
    # error — on the page and in view='notes'.
    ref = store.get_ref(kind="pcb", id="web_argue_page")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_instances SET retired_at = now() "
            "WHERE ref_id = %s AND refdes = 'R1'",
            (ref.id,),
        )
    r = pcb_client.get("/pcb/web_argue_page")
    assert r.status_code == 200
    assert 'id="pcb-argue-text"' in r.text
    assert "/pcb/web_argue_page/note" in r.text
    assert "pcb-argue.js" in r.text
    assert "R1 and U1 are swapped" in r.text and "Swap them." in r.text
    assert "dangling: no longer on the board" in r.text
    lowered = r.text.lower()
    for forbidden in (">place<", ">route<", "place board", "route board"):
        assert forbidden not in lowered

    from precis.handlers.pcb import PcbHandler

    body = (
        PcbHandler(hub=runtime_with_store.hub)
        .get(id="web_argue_page", view="notes")
        .body
    )
    assert "# notes — 2" in body
    assert "about: R1 U1" in body
    assert "dangling (no longer on the board): R1" in body
    assert "R1 and U1 are swapped" in body and "Swap them." in body
    assert " · answer · proposed · " in body


def test_view_notes_empty_points_at_the_board_page(
    pcb_client, runtime_with_store
) -> None:
    from precis.handlers.pcb import PcbHandler

    _seed(runtime_with_store, slug="web_argue_empty")
    body = (
        PcbHandler(hub=runtime_with_store.hub)
        .get(id="web_argue_empty", view="notes")
        .body
    )
    assert "none yet" in body and "/pcb/<slug>" in body
