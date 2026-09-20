"""The design-chat panel — design-workbench build, slice 3, the route +
page half (the engine's own tests are ``tests/test_design_turn.py``).
The model is a stub: ``precis_web.design_turn._router_call`` is
monkeypatched, so the router is never reached.

* ``POST /se/{slug}/chat`` with two valid pure se ops → 303 → the page
  shows the transcript block and a link to ``?rev=<new>``; one new
  ``design_revisions`` row carrying the turn handle;
* a ``bind_structure`` reply → the page shows a proposal with Apply, no
  new revision; ``POST /se/{slug}/chat/apply`` → the new revision;
* chat or apply on ``?rev=1`` of a 2-revision design → 409, no writes,
  the stub never called; the page at ``?rev=1`` hides the panel;
* ``POST /structure/{slug}/chat`` with an atom op → proposal (hover
  ``data-atoms``) → apply → ``meta.version`` +1 on the same ref, no
  ``derived-from`` link;
* an empty message → 400; a rejected reply (after its one repair round)
  → 303 to the rejected transcript block, no design write.

Real Postgres (the ``store`` fixture) — the handlers write the rows the
page reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_se
from precis.design import history
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis_se.handler import SeHandler
from precis_web import design_turn
from precis_web.app import create_app
from precis_web.config import WebConfig

_SE_MIGRATIONS = Path(precis_se.__file__).parent / "migrations"

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
        ],
    }
)

_CASTER_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "fork", "envelope": "box:w0.04d0.02h0.08"},
    {
        "op": "add_block",
        "name": "hub",
        "parent": "fork",
        "pose": [0, 0, -0.05],
        "envelope": "cyl:r0.008h0.03",
    },
]
_CAP_OPS: list[dict[str, Any]] = [
    {
        "op": "add_block",
        "name": "cap",
        "parent": "hub",
        "pose": [0, 0, -0.07],
        "envelope": "sphere:r0.005",
    }
]

#: Two pure (auto-apply) se ops.
_AXLE_OPS: list[dict[str, Any]] = [
    {"op": "add_block", "name": "axle", "parent": "hub", "envelope": "cyl:r0.002h0.02"},
    {"op": "set_desc", "block": "axle", "desc": "the wheel axle"},
]


def _reply(ops: list[dict[str, Any]], rationale: str = "because") -> str:
    return json.dumps({"ops": ops, "rationale": rationale})


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the router with a canned reply; ``stub.reply`` is what the
    model says, ``stub.prompts`` what it was asked."""

    class _Stub:
        reply: str = _reply([])
        prompts: list[str] = []

        def __call__(self, ref_id: int) -> Any:
            def call(prompt: str) -> str:
                self.prompts.append(prompt)
                return self.reply

            return call

    s = _Stub()
    monkeypatch.setattr(design_turn, "_router_call", s)
    return s


@pytest.fixture
def client(store, runtime_with_store, tmp_path) -> TestClient:
    with store.pool.connection() as c:
        for sql in sorted(_SE_MIGRATIONS.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        ),
        follow_redirects=False,
    )


def _ref_id(store: Any, kind: str, slug: str) -> int:
    ref = store.get_ref(kind=kind, id=slug)
    assert ref is not None
    return int(ref.id)


def _revision_rows(store: Any) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT count(*) FROM design_revisions").fetchone()
    return int(row[0])


def _seed_se(runtime_with_store, store, slug: str = "caster_chat") -> int:
    handler = SeHandler(hub=runtime_with_store.hub)
    handler.put(id=slug, text=json.dumps({"ops": _CASTER_OPS}))
    handler.edit(id=slug, ops=_CAP_OPS)
    ref_id = _ref_id(store, "se", slug)
    assert [r.rev for r in history.list_revisions(store, ref_id)] == [1, 2]
    return ref_id


def _query(location: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


# ── se ───────────────────────────────────────────────────────────────────


def test_se_pure_ops_turn_applies_and_the_page_shows_it(
    client, runtime_with_store, store, stub
) -> None:
    ref_id = _seed_se(runtime_with_store, store)
    stub.reply = _reply(_AXLE_OPS, "an axle through the hub")
    rows_before = _revision_rows(store)

    r = client.post(
        "/se/caster_chat/chat",
        data={"message": "add an axle to @hub", "handles": "@hub, fork"},
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert urlparse(location).path == "/se/caster_chat"
    assert _query(location) == {"turn": "0"}
    assert urlparse(location).fragment == "design-chat"
    # The handles reached the prompt, '@' stripped.
    assert "# Clicked handles\nhub, fork" in stub.prompts[0]

    revs = history.list_revisions(store, ref_id)
    assert _revision_rows(store) == rows_before + 1
    assert revs[-1].rev == 3 and revs[-1].ops == _AXLE_OPS
    assert revs[-1].turn == "design-chat-caster_chat~0"

    page = client.get(location).text
    assert 'id="chat-turn-0"' in page
    assert "add an axle to @hub" in page
    assert "an axle through the hub" in page
    assert 'href="/se/caster_chat?rev=3"' in page
    assert "revision 3" in page
    assert 'id="design-chat-proposal"' not in page
    assert 'action="/se/caster_chat/chat"' in page
    # The ring marks the just-written block.
    assert "ring-1" in page.split('id="chat-turn-0"', 1)[1].split(">", 1)[0]


def test_se_store_aware_turn_proposes_then_apply_writes_the_revision(
    client, runtime_with_store, store, stub
) -> None:
    ref_id = _seed_se(runtime_with_store, store)
    StructureHandler(hub=Hub(store=store)).put(id="pd_pair", text=_PD)
    ops = [{"op": "bind_structure", "block": "cap", "design": "pd_pair"}]
    stub.reply = _reply(ops, "bind the cap")
    rows_before = _revision_rows(store)

    r = client.post("/se/caster_chat/chat", data={"message": "bind the cap"})
    assert r.status_code == 303
    assert _query(r.headers["location"]) == {"turn": "0"}
    assert _revision_rows(store) == rows_before  # a proposal writes no revision

    page = client.get(r.headers["location"]).text
    assert 'id="design-chat-proposal"' in page
    assert 'action="/se/caster_chat/chat/apply"' in page
    assert 'name="turn" value="design-chat-caster_chat~0"' in page
    assert "awaiting Apply below" in page
    assert ">valid<" in page

    r2 = client.post(
        "/se/caster_chat/chat/apply",
        data={"ops": json.dumps(ops), "turn": "design-chat-caster_chat~0"},
    )
    assert r2.status_code == 303
    assert _query(r2.headers["location"]) == {"turn": "0"}
    revs = history.list_revisions(store, ref_id)
    assert _revision_rows(store) == rows_before + 1
    assert revs[-1].ops == ops and revs[-1].turn == "design-chat-caster_chat~0"

    from precis_se import persist

    assert persist.load_tree(store, ref_id).blocks["cap"].bound == "pd_pair"
    # Applied: the proposal box is gone, the transcript still shows the turn.
    page = client.get("/se/caster_chat").text
    assert 'id="design-chat-proposal"' not in page
    assert 'id="chat-turn-0"' in page


def test_se_chat_on_a_past_revision_is_refused(
    client, runtime_with_store, store, stub
) -> None:
    _seed_se(runtime_with_store, store)
    stub.reply = _reply(_AXLE_OPS)
    rows_before = _revision_rows(store)

    r = client.post("/se/caster_chat/chat?rev=1", data={"message": "add an axle"})
    assert r.status_code == 409
    assert "past revision" in r.text
    r2 = client.post(
        "/se/caster_chat/chat/apply?rev=1",
        data={"ops": json.dumps(_AXLE_OPS), "turn": ""},
    )
    assert r2.status_code == 409

    assert stub.prompts == []
    assert _revision_rows(store) == rows_before
    assert store.get_ref(kind="conv", id="design-chat-caster_chat") is None

    # The page at ?rev=1 hides the panel behind a one-line note.
    past = client.get("/se/caster_chat?rev=1").text
    assert 'id="design-chat-readonly"' in past
    assert 'id="design-chat-form"' not in past
    # The current revision, addressed explicitly, is chattable.
    r3 = client.post("/se/caster_chat/chat?rev=2", data={"message": "add an axle"})
    assert r3.status_code == 303


def test_se_rejected_reply_stays_in_the_transcript_and_writes_no_revision(
    client, runtime_with_store, store, stub
) -> None:
    _seed_se(runtime_with_store, store)
    stub.reply = _reply([{"op": "teleport", "block": "cap"}])
    rows_before = _revision_rows(store)

    r = client.post("/se/caster_chat/chat", data={"message": "move it"})
    assert r.status_code == 303
    q = _query(r.headers["location"])
    assert q == {"turn": "0"}
    assert len(stub.prompts) == 2  # the one repair round
    assert "# Validator error" in stub.prompts[1]
    assert _revision_rows(store) == rows_before
    assert store.get_ref(kind="conv", id="design-chat-caster_chat") is not None
    page = client.get(r.headers["location"]).text
    assert 'id="design-chat-error"' not in page
    assert 'id="chat-turn-0"' in page and "move it" in page
    assert ">rejected<" in page
    assert "unknown op &#39;teleport&#39;" in page or "unknown op 'teleport'" in page
    assert "one repair round" in page
    assert 'id="design-chat-proposal"' not in page


def test_model_failure_is_flashed_and_writes_nothing(
    client, runtime_with_store, store, stub, monkeypatch
) -> None:
    _seed_se(runtime_with_store, store)

    def down(ref_id: int):
        def call(prompt: str) -> str:
            raise RuntimeError("router down")

        return call

    monkeypatch.setattr(design_turn, "_router_call", down)
    r = client.post("/se/caster_chat/chat", data={"message": "move it"})
    assert r.status_code == 303
    q = _query(r.headers["location"])
    assert "turn" not in q and "router down" in q["chat_error"]
    assert store.get_ref(kind="conv", id="design-chat-caster_chat") is None
    page = client.get(r.headers["location"]).text
    assert 'id="design-chat-error"' in page and "No turns yet." in page


def test_empty_message_is_400(client, runtime_with_store, store, stub) -> None:
    _seed_se(runtime_with_store, store)
    assert (
        client.post("/se/caster_chat/chat", data={"message": "   "}).status_code == 400
    )
    assert client.post("/se/caster_chat/chat", data={}).status_code == 400
    StructureHandler(hub=Hub(store=store)).put(id="pd_chat", text=_PD)
    assert (
        client.post("/structure/pd_chat/chat", data={"message": ""}).status_code == 400
    )
    assert stub.prompts == []


def test_chat_on_a_missing_design_is_404(client, stub) -> None:
    assert client.post("/se/nope/chat", data={"message": "x"}).status_code == 404
    assert client.post("/structure/nope/chat", data={"message": "x"}).status_code == 404


# ── structure ────────────────────────────────────────────────────────────


def test_structure_turn_proposes_then_apply_edits_in_place(client, store, stub) -> None:
    StructureHandler(hub=Hub(store=store)).put(id="pd_chat", text=_PD)
    ref_id = _ref_id(store, "structure", "pd_chat")
    assert store.structure_version(ref_id) == 1
    ops = [{"op": "displace", "atom": "aPd1", "vector": [0.5, 0.0, 0.0]}]
    stub.reply = _reply(ops, "nudge aPd1 along x")
    rows_before = _revision_rows(store)

    r = client.post(
        "/structure/pd_chat/chat", data={"message": "nudge @aPd1", "handles": "aPd1"}
    )
    assert r.status_code == 303
    assert _query(r.headers["location"]) == {"turn": "0"}
    assert "aPd1 Pd frac=" in stub.prompts[0]
    # Every structure op is propose-only: no revision, version unchanged.
    assert _revision_rows(store) == rows_before
    assert store.structure_version(ref_id) == 1

    page = client.get(r.headers["location"]).text
    assert 'id="design-chat-proposal"' in page
    assert 'action="/structure/pd_chat/chat/apply"' in page
    assert 'data-atoms="aPd1"' in page  # the per-op hover highlight hook
    assert "nudge aPd1 along x" in page

    r2 = client.post(
        "/structure/pd_chat/chat/apply",
        data={"ops": json.dumps(ops), "turn": "design-chat-pd_chat~0"},
    )
    assert r2.status_code == 303
    assert _query(r2.headers["location"]) == {"turn": "0"}
    # Same ref, version +1 (edit, not derive), no lineage link.
    assert store.structure_version(ref_id) == 2
    live = store.get_ref(kind="structure", id="pd_chat")
    assert live is not None and live.id == ref_id
    assert (live.meta or {}).get("version") == 2
    assert store.links_for(ref_id, relation="derived-from") == []
    revs = history.list_revisions(store, ref_id)
    assert _revision_rows(store) == rows_before + 1
    assert revs[-1].rev == 2 and revs[-1].ops == ops
    assert revs[-1].turn == "design-chat-pd_chat~0"

    page = client.get("/structure/pd_chat").text
    assert 'id="design-chat-proposal"' not in page
    assert 'href="/structure/pd_chat?rev=2"' in page
    # The derive-Apply surface is untouched.
    assert 'id="sv-proposal"' in page


def test_structure_chat_on_a_past_revision_is_refused(client, store, stub) -> None:
    handler = StructureHandler(hub=Hub(store=store))
    handler.put(id="pd_chat", text=_PD)
    handler.edit(
        id="pd_chat", ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}]
    )
    ref_id = _ref_id(store, "structure", "pd_chat")
    assert store.structure_version(ref_id) == 2
    stub.reply = _reply([{"op": "vacancy", "atom": "aO1"}])

    r = client.post("/structure/pd_chat/chat?rev=1", data={"message": "drop the O"})
    assert r.status_code == 409
    r2 = client.post(
        "/structure/pd_chat/chat/apply?rev=1",
        data={"ops": json.dumps([{"op": "vacancy", "atom": "aO1"}]), "turn": ""},
    )
    assert r2.status_code == 409
    assert stub.prompts == []
    assert store.structure_version(ref_id) == 2
    assert store.get_ref(kind="conv", id="design-chat-pd_chat") is None

    past = client.get("/structure/pd_chat?rev=1").text
    assert 'id="design-chat-readonly"' in past
    assert 'id="design-chat-form"' not in past
    current = client.get("/structure/pd_chat?rev=2").text
    assert 'id="design-chat-form"' in current


def test_apply_with_bad_ops_is_400(client, store, stub) -> None:
    StructureHandler(hub=Hub(store=store)).put(id="pd_chat", text=_PD)
    r = client.post(
        "/structure/pd_chat/chat/apply", data={"ops": "not json", "turn": ""}
    )
    assert r.status_code == 400
    r = client.post(
        "/structure/pd_chat/chat/apply", data={"ops": '{"op": "x"}', "turn": ""}
    )
    assert r.status_code == 400
    # A roster miss on Apply is the engine's rejection, flashed.
    r = client.post(
        "/structure/pd_chat/chat/apply",
        data={"ops": json.dumps([{"op": "teleport"}]), "turn": ""},
    )
    assert r.status_code == 303
    assert "unknown op" in _query(r.headers["location"])["chat_error"]
