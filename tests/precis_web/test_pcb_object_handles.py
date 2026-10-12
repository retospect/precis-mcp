"""Board-object handles (via / silk / outline / fiducial) through the note
route and the notes ledger (gr479844 follow-up). Fixture boards only; the
LLM router is stubbed."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.precis_web.test_pcb import _fake_router, pcb_client  # noqa: F401
from tests.test_pcb_selection_objects import catalogue, seed


def _object_board(runtime_with_store, slug: str):
    store = runtime_with_store.hub.store
    _, ref = seed(store, slug=slug)
    _, objects = catalogue(store, ref)
    return store, ref, {o["key"]: o for o in objects}


@pytest.mark.parametrize("kind", ["via", "silk", "outline", "fiducial"])
def test_note_accepts_each_object_kind_handle(
    pcb_client,  # noqa: F811
    runtime_with_store,
    monkeypatch,
    kind,
) -> None:
    store, ref, objs = _object_board(runtime_with_store, f"web_obj_{kind}")
    key = next(k for k, o in objs.items() if o["kind"] == kind)
    seen = _fake_router(monkeypatch)
    r = pcb_client.post(
        f"/pcb/web_obj_{kind}/note",
        json={"text": f"what is {key} for", "handles": [key]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["about"] == [key]
    assert store.pcb_notes_list(ref.id)[0]["about"] == [key]
    # The model got the identity dict as the handle's context.
    assert f"- {key} (object): " in seen[0].prompt
    assert repr(objs[key]["key"]) in seen[0].prompt


def test_note_rejects_unknown_object_key(
    pcb_client,  # noqa: F811
    runtime_with_store,
    monkeypatch,
) -> None:
    store, ref, _ = _object_board(runtime_with_store, "web_obj_bad")
    seen = _fake_router(monkeypatch)
    bad = "via:99.0000:99.0000"
    r = pcb_client.post(
        "/pcb/web_obj_bad/note", json={"text": f"{bad} is wrong", "handles": [bad]}
    )
    assert r.status_code == 400
    assert r.json()["unknown"] == [bad]
    assert "via:20.0000:5.0000" in r.json()["valid"]["objects"]
    assert store.pcb_notes_list(ref.id) == [] and seen == []


def test_object_note_is_not_dangling_while_the_object_exists(
    runtime_with_store,
) -> None:
    from precis_web.routes.pcb import _notes_ctx

    store, ref, objs = _object_board(runtime_with_store, "web_obj_read")
    key = next(k for k, o in objs.items() if o["kind"] == "via")
    store.pcb_note_insert(
        ref.id,
        name="q-a",
        kind="question",
        body="x",
        about=[key, "via:0.0000:0.0000"],
    )
    (thread,) = _notes_ctx(store, ref)["threads"]
    assert thread["note"]["dangling"] == {"via:0.0000:0.0000"}


def test_selection_and_argue_scripts_share_the_attach_event() -> None:
    static = Path(__file__).parents[2] / "src/precis_web/static"
    argue_js = (static / "pcb-argue.js").read_text(encoding="utf-8")
    sel_js = (static / "pcb-selection.js").read_text(encoding="utf-8")
    assert "'pcb-selection-attach'" in sel_js and "dispatchEvent" in sel_js
    assert '"pcb-selection-attach"' in argue_js
    assert "__pcbSelectionWired" in argue_js
