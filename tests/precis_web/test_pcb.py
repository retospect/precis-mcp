"""PCB browse tab — FakeStore degradation + real-store integration.

The board pane needs a placed+routed design (fab films); the schematic pane
must work on a freshly-authored netlist with no placement at all — that
asymmetry is asserted here.
"""

from __future__ import annotations

import pathlib

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


def test_detail_renders_the_two_pane_workbench(pcb_client, runtime_with_store) -> None:
    """The workbench layout (docs/backlog/pcb-workbench-layout.md slice 1):
    schematic left, layout right, a divider between them, the info strip
    below carrying the cursor readout, and the flip control — with
    viewport.js and the workbench glue loaded."""
    _seed(runtime_with_store)
    r = pcb_client.get("/pcb/web_pcb")
    assert r.status_code == 200
    for marker in (
        'id="pcb-workbench"',
        'id="pcb-pane-schematic"',
        'id="pcb-pane-layout"',
        'id="pcb-divider"',
        'id="pcb-height"',
        'aria-orientation="horizontal"',
        'id="pcb-info-strip"',
        'id="pcb-cursor-readout"',
        'id="pcb-flip"',
        'id="pcb-schematic-object"',
        'id="pcb-board-object"',
        "/static/viewport.js",
        "/static/pcb-workbench.js",
    ):
        assert marker in r.text, marker
    # schematic is the left pane; the strip sits below both panes
    assert (
        r.text.index('id="pcb-pane-schematic"')
        < r.text.index('id="pcb-pane-layout"')
        < r.text.index('id="pcb-info-strip"')
    )
    # the row-resize handle (gr496860) sits between the workbench and the
    # strip; the page overrides base's main_class to full width (gr496857)
    assert (
        r.text.index('id="pcb-workbench"')
        < r.text.index('id="pcb-height"')
        < r.text.index('id="pcb-info-strip"')
    )
    assert '<main class="px-4 py-6">' in r.text
    # both panes are <object> documents (each svg keeps its own scripts)
    assert 'object id="pcb-schematic-object"' in r.text
    assert "board.svg?shown=" in r.text


def test_board_svg_flip_renders_the_bottom_view(pcb_client, runtime_with_store) -> None:
    """``board.svg?flip=1`` is the mirrored bottom view (gerber_view's
    ``_FLIPPED_STACK``): B.* layers paint on top, ``data-side`` marks it."""
    from precis_web import pcb_design

    _seed(runtime_with_store)
    store = runtime_with_store.hub.store
    ref = store.get_ref(kind="pcb", id="web_pcb")
    with store.pcb_view_tx() as conn:
        token = pcb_design.capture_view(store, ref, conn=conn).token
    top = pcb_client.get("/pcb/web_pcb/board.svg", params={"shown": token})
    bottom = pcb_client.get(
        "/pcb/web_pcb/board.svg", params={"shown": token, "flip": "1"}
    )
    assert top.status_code == 200 and bottom.status_code == 200
    assert 'data-side="bottom"' in bottom.text
    assert "data-side" not in top.text
    assert bottom.text.index('id="layer-B_Cu"') > bottom.text.index('id="layer-F_Cu"')


def test_detail_title_uses_the_captured_read_transaction(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    from precis_web.routes import pcb

    slug = "web_title_snapshot"
    _seed(runtime_with_store, slug=slug)
    store = runtime_with_store.hub.store
    resolve = pcb.resolve_live_slug_ref

    def resolve_then_change_title(*args, **kwargs):
        ref = resolve(*args, **kwargs)
        with store.tx() as conn:
            conn.execute(
                "UPDATE refs SET title=%s WHERE ref_id=%s",
                ("Title committed before the view snapshot", ref.id),
            )
        return ref

    monkeypatch.setattr(pcb, "resolve_live_slug_ref", resolve_then_change_title)
    response = pcb_client.get(f"/pcb/{slug}")
    assert response.status_code == 200
    assert "Title committed before the view snapshot" in response.text


def test_bare_pcb_reader_stays_read_only_and_refuses_chat_before_model(
    pcb_client, runtime_with_store, monkeypatch
) -> None:
    import json

    from precis_web import design_turn, pcb_design

    store = runtime_with_store.hub.store
    ref = store.insert_ref(kind="pcb", slug="web_empty_ref", title="Empty PCB")
    monkeypatch.setattr(
        design_turn,
        "_router_call",
        lambda _: pytest.fail("empty reader must refuse before model generation"),
    )
    assert pcb_client.get("/pcb/web_empty_ref").status_code == 200
    assert pcb_client.get("/pcb/web_empty_ref/board.svg").status_code == 200
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
    assert view.instances == []
    assert (
        pcb_client.get(
            "/pcb/web_empty_ref/board.svg", params={"shown": view.token}
        ).status_code
        == 200
    )
    assert (
        pcb_client.post(
            "/pcb/web_empty_ref/chat",
            data={"message": "move", "context": json.dumps(view.context)},
        ).status_code
        == 409
    )
    with store.tx() as conn:
        assert conn.execute(
            "SELECT count(*) FROM pcb_boards WHERE ref_id=%s", (ref.id,)
        ).fetchone() == (0,)


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


def test_pcb_selection_chat_explicit_apply_and_readonly_history(
    pcb_client, runtime_with_store, monkeypatch
):
    import json
    from pathlib import Path

    from precis.design import history
    from precis.dispatch import Hub
    from precis.handlers.pcb import PcbHandler
    from precis_web import design_turn, pcb_design

    store = runtime_with_store.hub.live_store
    hub = Hub(store=store)
    slug = "browser_selection_fixture"
    PcbHandler(hub=hub).put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": rd,
                    "label": "resistor",
                    "footprint": "local-R",
                    "pins": [{"name": "1", "pad": "1"}],
                }
                for rd in ("R1", "R2")
            ],
            "nets": [{"name": "SIG"}],
            "connections": [],
            "footprints": [
                {
                    "name": "local-R",
                    "pads": [
                        {
                            "number": "1",
                            "x": 0,
                            "y": 0,
                            "w": 0.6,
                            "h": 0.6,
                            "shape": "rect",
                        }
                    ],
                }
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id=slug)
    store.pcb_move_instance(ref.id, "R1", x=10, y=10, rot=0)
    store.pcb_move_instance(ref.id, "R2", x=20, y=10, rot=0)
    with store.pcb_view_tx() as conn:
        view = pcb_design.capture_view(store, ref, conn=conn)
    context = {**view.context, "selected": [view.instances[0]]}
    calls = []

    def model(_):
        calls.append(True)
        return json.dumps(
            {
                "ops": [{"op": "move", "refdes": "R1", "fixed": "xy"}],
                "rationale": "lock",
            }
        )

    monkeypatch.setattr(design_turn, "_router_call", lambda _: model)
    html = pcb_client.get(f"/pcb/{slug}")
    assert html.status_code == 200
    assert "pcb-selection-manifest" in html.text
    assert "PCB changes always require explicit Apply" in html.text
    svg = pcb_client.get(f"/pcb/{slug}/board.svg", params={"shown": view.token})
    assert svg.status_code == 200
    assert "data-pcb-instance" in svg.text
    assert 'data-pcb-object="pad:' in svg.text and 'id="pcb-objects"' in svg.text
    assert 'id="pcb-object-choice"' in html.text
    assert 'id="pcb-select-component"' in html.text
    assert pcb_client.get(f"/pcb/{slug}/board.svg?shown=stale").status_code == 409
    assert calls == []
    out = pcb_client.post(
        f"/pcb/{slug}/chat",
        data={"message": "lock resistor", "context": json.dumps(context)},
        follow_redirects=False,
    )
    assert out.status_code == 303
    assert len(calls) == 1
    assert history.list_revisions(store, ref.id) == []
    proposed = pcb_client.get(f"/pcb/{slug}")
    assert "Apply as a new revision" in proposed.text
    assert 'name="ops"' not in proposed.text
    turn = design_turn.pending_proposal(store, kind="pcb", slug=slug)
    assert turn is not None
    # Save only fixture browser inputs outside the shipping payload.
    artifacts = Path(".scratch/selection.local/browser-fixture")
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "index.html").write_text(proposed.text, encoding="utf-8")
    (artifacts / "board.svg").write_text(svg.text, encoding="utf-8")
    (artifacts / "context.json").write_text(json.dumps(context), encoding="utf-8")
    bad = pcb_client.post(
        f"/pcb/{slug}/chat/apply",
        data={"turn": turn.handle, "shown": view.token, "ops": "[]"},
    )
    assert bad.status_code == 400
    applied = pcb_client.post(
        f"/pcb/{slug}/chat/apply",
        data={"turn": turn.handle, "shown": view.token},
        follow_redirects=False,
    )
    assert applied.status_code == 303
    duplicate = pcb_client.post(
        f"/pcb/{slug}/chat/apply", data={"turn": turn.handle, "shown": view.token}
    )
    assert duplicate.status_code == 409
    assert len(history.list_revisions(store, ref.id)) == 1
    with store.pcb_view_tx() as conn:
        updated = pcb_design.capture_view(store, ref, conn=conn)
    ctx2 = {**updated.context, "selected": [updated.instances[0]]}
    monkeypatch.setattr(
        design_turn,
        "_router_call",
        lambda _: (
            lambda _: json.dumps(
                {
                    "ops": [{"op": "move", "refdes": "R1", "fixed": "both"}],
                    "rationale": "all",
                }
            )
        ),
    )
    second = pcb_client.post(
        f"/pcb/{slug}/chat",
        data={"message": "lock rotation too", "context": json.dumps(ctx2)},
    )
    assert second.status_code == 200
    turn2 = design_turn.pending_proposal(store, kind="pcb", slug=slug)
    assert turn2 is not None
    assert (
        pcb_client.post(
            f"/pcb/{slug}/chat/apply",
            data={"turn": turn2.handle, "shown": updated.token},
        ).status_code
        == 200
    )
    old = pcb_client.get(f"/pcb/{slug}?rev=1")
    assert old.status_code == 200
    assert "read-only summary" in old.text
    assert "pcb-board-object" not in old.text
    assert "design-chat-form" not in old.text
    stale_send = pcb_client.post(
        f"/pcb/{slug}/chat", data={"message": "lock", "context": json.dumps(context)}
    )
    assert stale_send.status_code == 409


@pytest.mark.parametrize(
    "path", ["/pcb/fixture/chat", "/pcb/fixture/chat/apply", "/pcb/import"]
)
def test_pcb_post_auth_and_cross_origin_refused_before_handler(
    runtime_with_store, tmp_path, path, monkeypatch
):
    secured = TestClient(
        create_app(
            runtime=runtime_with_store,
            web_config=WebConfig(corpus_dir=tmp_path, auth_required=True),
        )
    )
    # No user/model/board writer is involved: the shared middleware owns refusal.
    assert (
        secured.post(
            path, headers={"Origin": "https://foreign.example"}, data={}
        ).status_code
        == 403
    )
    # The fresh fixture has no enabled roster: this is intentionally503.
    assert secured.post(path, data={}).status_code == 503
    monkeypatch.setattr(
        runtime_with_store.hub.live_store, "count_web_users", lambda **_: 1
    )
    assert secured.post(path, data={}).status_code == 401


# ── GET /pcb/{slug}/export.epro2 (gr495289, Reto's ruling 1a) ────────────

_EPRO_PADS = {
    "pads": [
        {"number": n, "shape": "RECT", "x": x, "y": 0.0, "w": 0.4, "h": 0.4}
        for n, x in (("1", -0.5), ("2", 0.5))
    ],
    "pin_map": {"1": {"name": "1", "tags": []}, "2": {"name": "2", "tags": []}},
}


def _seed_placed(runtime_with_store, slug: str) -> None:
    from precis.handlers.pcb import PcbHandler

    store = runtime_with_store.hub.store
    PcbHandler(hub=runtime_with_store.hub).put(
        id=slug,
        args={
            "components": [
                {
                    "refdes": f"R{k}",
                    "label": "10k",
                    "part": "C25744",
                    "x": 5.0 + 6.0 * k,
                    "y": 8.0,
                    "pins": [{"name": "1"}, {"name": "2"}],
                }
                for k in (1, 2)
            ],
            "nets": [{"name": "SIG"}, {"name": "GND", "class": "ground"}],
            "connections": [
                {"net": "SIG", "refdes": "R1", "pin": "1"},
                {"net": "SIG", "refdes": "R2", "pin": "1"},
                {"net": "GND", "refdes": "R1", "pin": "2"},
                {"net": "GND", "refdes": "R2", "pin": "2"},
            ],
            "features": [
                {
                    "ftype": "outline",
                    "geom": {"path": [[0, 0], [24, 0], [24, 16], [0, 16]]},
                }
            ],
        },
    )
    store.part_footprint_put("C25744", _EPRO_PADS)


def test_epro_download_is_the_mcp_views_bytes(
    pcb_client, runtime_with_store, tmp_path
) -> None:
    import hashlib

    from precis.handlers.pcb import PcbHandler

    _seed_placed(runtime_with_store, "web_epro")
    handler = PcbHandler(hub=runtime_with_store.hub)
    body = handler.get(id="web_epro", view="epro", args={"dir": str(tmp_path)}).body
    path = next(
        line.split(": ", 1)[1]
        for line in body.splitlines()
        if line.startswith("download")
    )
    r = pcb_client.get(path)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == 'attachment; filename="web_epro.epro2"'
    assert f"sha256: {hashlib.sha256(r.content).hexdigest()}" in body
    # the same bytes without the sha guard, and the board page links it
    assert pcb_client.get("/pcb/web_epro/export.epro2").content == r.content
    assert "/pcb/web_epro/export.epro2" in pcb_client.get("/pcb/web_epro").text


def test_epro_download_refuses_a_link_to_an_edited_board(
    pcb_client, runtime_with_store
) -> None:
    from precis.handlers.pcb import PcbHandler

    _seed_placed(runtime_with_store, "web_epro_edit")
    handler = PcbHandler(hub=runtime_with_store.hub)
    ref = runtime_with_store.hub.store.get_ref(kind="pcb", id="web_epro_edit")
    before = handler.epro_build(ref.id)
    assert before is not None
    assert pcb_client.get(before.download_path()).status_code == 200
    # locking a part changes the COMPONENT record, so the bytes change
    handler.put(
        id="web_epro_edit", args={"op": "move", "refdes": "R1", "fixed": "both"}
    )
    after = handler.epro_build(ref.id)
    assert after is not None and after.sha256 != before.sha256
    assert pcb_client.get(before.download_path()).status_code == 409
    assert pcb_client.get(after.download_path()).status_code == 200


def test_epro_download_refuses_a_stale_sha(pcb_client, runtime_with_store) -> None:
    _seed_placed(runtime_with_store, "web_epro_stale")
    r = pcb_client.get("/pcb/web_epro_stale/export.epro2?sha=<b>0000</b>")
    assert r.status_code == 409
    assert "run view='epro' again" in r.text
    assert "<b>" not in r.text
    assert r.headers["content-type"].startswith("text/plain")


def test_epro_download_404(pcb_client) -> None:
    assert pcb_client.get("/pcb/nope/export.epro2").status_code == 404


# ── GET /pcb/{slug}/fab.zip + the export toolbar (gr496699) ─────────────


def test_fab_zip_downloads_the_full_production_package(
    pcb_client, runtime_with_store
) -> None:
    import io
    import zipfile

    _seed_placed(runtime_with_store, "web_fab")
    r = pcb_client.get("/pcb/web_fab/fab.zip")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == (
        'attachment; filename="web_fab-fab-package.zip"'
    )
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == [
        "web_fab-bom.csv",
        "web_fab-cpl.csv",
        "web_fab-fab.zip",
    ]
    # the nested zip is the same gerber+drill bundle view='fab' writes
    inner = zipfile.ZipFile(io.BytesIO(zf.read("web_fab-fab.zip")))
    names = inner.namelist()
    assert "web_fab-Edge_Cuts.gbr" in names
    assert "web_fab-F_Cu.gbr" in names
    bom = zf.read("web_fab-bom.csv").decode(encoding="utf-8")
    assert "R1" in bom and "C25744" in bom
    assert "R1" in zf.read("web_fab-cpl.csv").decode(encoding="utf-8")


def test_detail_export_toolbar_links_both_downloads(
    pcb_client, runtime_with_store
) -> None:
    _seed_placed(runtime_with_store, "web_toolbar")
    r = pcb_client.get("/pcb/web_toolbar")
    assert r.status_code == 200
    assert 'href="/pcb/web_toolbar/fab.zip"' in r.text
    assert 'href="/pcb/web_toolbar/export.epro2"' in r.text
    assert 'href="/pcb/new"' in r.text
    assert "production files" in r.text


def test_fab_zip_404_and_no_board(pcb_client, runtime_with_store) -> None:
    assert pcb_client.get("/pcb/nope/fab.zip").status_code == 404
    store = runtime_with_store.hub.store
    store.insert_ref(kind="pcb", slug="web_no_board", title="bare")
    r = pcb_client.get("/pcb/web_no_board/fab.zip")
    assert r.status_code == 422
    assert "no board" in r.text


# ── GET /pcb/new + POST /pcb/import (gr496699 slice 2) ──────────────────

_EPRO_TINY = pathlib.Path(__file__).parent.parent / "fixtures" / "pcb_epro_tiny"


def _epro2_bytes(*, lone_pin_net: bool = False) -> bytes:
    """The tiny fixture board as a real ``.epro2`` upload (the same zip
    ``test_pcb_epro_import.py`` feeds the importer)."""
    import io
    import zipfile

    board = (_EPRO_TINY / "board.epru").read_bytes()
    if lone_pin_net:
        stream = board.decode(encoding="utf-8")
        stream = stream.replace(
            '"id":"s2p3m"}||{"parentId":"s2p3","key":"Pin Name",'
            '"value":"GND","partId":"U.1"',
            '"id":"s2p3m"}||{"parentId":"s2p3","key":"Pin Name",'
            '"value":"LONE_PIN","partId":"U.1"',
        )
        stream = stream.replace(
            '{"type":"DOCHEAD","ticket":70}||{"docType":"FOOTPRINT"',
            '{"type":"NET","ticket":15,"id":"[\\"NET\\",\\"LONE\\"]"}||'
            '{"netType":null}|\n'
            '{"type":"PAD_NET","ticket":98,"id":"[\\"PAD_NET\\",\\"c2\\",'
            '\\"3\\",\\"f3\\"]"}||{"padNet":"LONE","padLen":null,"attrsMap":{}}|\n'
            '{"type":"DOCHEAD","ticket":70}||{"docType":"FOOTPRINT"',
        )
        board = stream.encode(encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project2.json", (_EPRO_TINY / "project2.json").read_bytes())
        zf.writestr("board.epru", board)
    return buf.getvalue()


def test_new_page_renders_the_import_form(pcb_client) -> None:
    r = pcb_client.get("/pcb/new")
    assert r.status_code == 200
    assert 'action="/pcb/import"' in r.text
    assert 'type="file"' in r.text


def test_import_upload_creates_the_board_and_shows_the_report(
    pcb_client, runtime_with_store
) -> None:
    r = pcb_client.post(
        "/pcb/import",
        files={
            "file": ("tiny.epro2", _epro2_bytes(lone_pin_net=True), "application/zip")
        },
    )
    assert r.status_code == 200, r.text
    store = runtime_with_store.hub.store
    ref = store.get_ref(kind="pcb", id="tiny")
    assert ref is not None
    assert "Imported tiny" in r.text
    assert "component(s)" in r.text
    assert "applied:" in r.text
    assert "stackup:" in r.text
    assert "net(s) with fewer than 2 pins" in r.text
    assert "LONE" in r.text
    assert "marked unused" in r.text
    assert "U1.LONE_PIN" in r.text
    assert 'href="/pcb/tiny"' in r.text
    # the imported board's own page then serves it
    assert pcb_client.get("/pcb/tiny").status_code == 200


def test_import_taken_slug_is_refused(pcb_client, runtime_with_store) -> None:
    _seed_placed(runtime_with_store, "web-taken")
    r = pcb_client.post(
        "/pcb/import",
        files={"file": ("other.epro2", _epro2_bytes(), "application/zip")},
        data={"slug": "web-taken"},
    )
    assert r.status_code == 400
    assert "already exists" in r.text or "fresh slug" in r.text


def test_import_refuses_a_non_epro2_upload_with_a_next_step(pcb_client) -> None:
    r = pcb_client.post(
        "/pcb/import",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
    )
    assert r.status_code == 400
    assert "not an .epro2" in r.text
    assert "EasyEDA Pro" in r.text


def test_import_refuses_a_zip_that_is_not_an_epro2(pcb_client) -> None:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.txt", "not a board")
    r = pcb_client.post(
        "/pcb/import",
        files={"file": ("not-board.epro2", buf.getvalue(), "application/zip")},
    )
    assert r.status_code == 400
    assert "project2.json" in r.text
    assert "EasyEDA Pro" in r.text


def test_import_refuses_slug_new(pcb_client) -> None:
    r = pcb_client.post(
        "/pcb/import",
        files={"file": ("tiny.epro2", _epro2_bytes(), "application/zip")},
        data={"slug": "new"},
    )
    assert r.status_code == 400
    assert "another slug" in r.text


def test_import_enforces_the_upload_cap(pcb_client, monkeypatch) -> None:
    from precis_web.routes import pcb

    monkeypatch.setattr(pcb, "_EPRO_UPLOAD_MAX", 4)
    r = pcb_client.post(
        "/pcb/import",
        files={"file": ("tiny.epro2", b"PK\x03\x04x", "application/zip")},
    )
    assert r.status_code == 400
    assert "larger than" in r.text
    assert "CLI" in r.text
