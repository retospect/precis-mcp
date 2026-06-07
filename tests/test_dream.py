"""The dreaming agent loop (#8) — fully offline via a scripted transport.

The litellm client's transport seam (mirroring RemoteEmbedder) lets us
script the model's tool-calls without a live server. Pins the gate,
the turn loop, tool dispatch (dispatch verbs + handler-method tools),
the last_dreamt rotation, dream_log/dream_transcripts writes, and the
max-turns backstop.
"""

from __future__ import annotations

import json
import re
import types

import pytest

from precis.embedder import MockEmbedder
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.workers.dream import (
    DreamConfig,
    _format_hits,
    _system_prompt,
    run_dream_pass,
)

_EMB = MockEmbedder(dim=1024)


@pytest.fixture(autouse=True)
def _clean_dream_tables(store: Store) -> None:
    """The shared-DB cleanup doesn't cover the dreaming telemetry tables;
    truncate them per-test so row counts/ordering are deterministic."""
    with store.pool.connection() as conn:
        conn.execute("TRUNCATE dream_transcripts, dream_log RESTART IDENTITY")


# ── scripted transport ──────────────────────────────────────────────


class FakeLLM:
    """Returns scripted assistant messages in order; stops when drained."""

    def __init__(self, turns: list[dict]) -> None:
        self.turns = list(turns)
        self.bodies: list[dict] = []

    def __call__(self, method, url, body, timeout):
        self.bodies.append(body)
        msg = (
            self.turns.pop(0)
            if self.turns
            else {"role": "assistant", "content": "done"}
        )
        return 200, {"choices": [{"message": msg}]}


def _tool_turn(name: str, args: dict, call_id: str = "c1") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
        ],
    }


def _stop_turn(text: str = "leaving it a little better") -> dict:
    return {"role": "assistant", "content": text}


# ── corpus seeding ──────────────────────────────────────────────────


def _seed_memory(rt: PrecisRuntime, store: Store, text: str, score: float = 0.0) -> int:
    out = rt.dispatch("put", {"kind": "memory", "text": text})
    m = re.search(r"id=(\d+)", out)
    assert m, out
    rid = int(m.group(1))
    (cid,) = store.card_chunk_ids([rid])
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, 'bge-m3', %s, 'ok', 1) "
            "ON CONFLICT (chunk_id, embedder) DO UPDATE "
            "SET vector = EXCLUDED.vector, status = 'ok'",
            (cid, _EMB.embed_one(text)),
        )
        if score:
            conn.execute(
                "UPDATE chunks SET last_seen = now(), "
                "  last_dreamt = now() - make_interval(secs => %s) WHERE chunk_id = %s",
                (score, cid),
            )
    return rid


def _dream_log_rows(store: Store) -> list[tuple]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT outcome, turns, tool_calls, model FROM dream_log ORDER BY attempt_id"
        ).fetchall()


def _enabled(**kw) -> DreamConfig:
    return DreamConfig(enabled=True, sparks_n=0, **kw)


# ── prompt ──────────────────────────────────────────────────────────


def test_system_prompt_acquire_line_is_conditional() -> None:
    prompt = _system_prompt(acquire_enabled=False)
    assert "acquire(" not in prompt
    assert "acquire(" in _system_prompt(acquire_enabled=True)
    # the id-usage guard + the get-for-detail guidance are always present
    assert "never invent an id" in prompt
    assert "get(kind=..., id=...)" in prompt


def _hit(text: str, title: str, rid: int = 1, kind: str = "memory", score: float = 1.0):
    block = types.SimpleNamespace(text=text, id=rid)
    ref = types.SimpleNamespace(title=title, slug=None, id=rid, kind=kind)
    return (block, ref, score)


def test_focus_verbatim_sparks_truncated() -> None:
    long_body = "lorem ipsum dolor sit amet " * 30  # well over the 240-char excerpt
    hits = [_hit(long_body.strip(), "a long note")]
    full = _format_hits(hits, full=True)
    excerpt = _format_hits(hits, full=False)
    # focus: untruncated (no ellipsis, full length preserved)
    assert "…" not in full
    assert len(long_body.strip()) <= len(full)
    # sparks: truncated with an ellipsis
    assert "…" in excerpt
    assert len(excerpt) < len(full)


def test_focus_verbatim_preserves_newlines() -> None:
    body = "line one\nline two\nline three"
    out = _format_hits([_hit(body, "multi")], full=True)
    assert "    line one\n    line two\n    line three" in out


# ── gate ────────────────────────────────────────────────────────────


def test_gate_off_is_noop(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    fake = FakeLLM([])
    out = run_dream_pass(
        store,
        hub=runtime_with_store.hub,
        config=DreamConfig(enabled=False),
        transport=fake,
    )
    assert out == {"claimed": 0, "ok": 0, "failed": 0}
    assert fake.bodies == []  # no LLM call
    assert _dream_log_rows(store) == []


def test_empty_corpus_logs_noop(runtime_with_store: PrecisRuntime) -> None:
    store = runtime_with_store.hub.store
    assert store is not None
    fake = FakeLLM([])
    out = run_dream_pass(
        store, hub=runtime_with_store.hub, config=_enabled(), transport=fake
    )
    assert out == {"claimed": 1, "ok": 1, "failed": 0}
    assert fake.bodies == []  # no region → no LLM call
    rows = _dream_log_rows(store)
    assert len(rows) == 1 and rows[0][0] == "noop"


# ── write run ───────────────────────────────────────────────────────


def test_put_then_stop_writes_and_records(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _seed_memory(rt, store, "copper catalyses nitrate reduction", score=100)

    fake = FakeLLM(
        [
            _tool_turn("put", {"kind": "memory", "text": "dream synthesis note"}),
            _stop_turn(),
        ]
    )
    out = run_dream_pass(store, hub=rt.hub, config=_enabled(), transport=fake)
    assert out == {"claimed": 1, "ok": 1, "failed": 0}

    rows = _dream_log_rows(store)
    assert len(rows) == 1
    outcome, turns, tool_calls, model = rows[0]
    assert outcome == "wrote"
    assert tool_calls == 1
    assert model == "qwen-heavy"

    # transcript row written 1:1
    with store.pool.connection() as conn:
        n = conn.execute("SELECT count(*) FROM dream_transcripts").fetchone()[0]
        assert n == 1
        # the dream's note exists as a live memory
        memcount = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'memory' AND deleted_at IS NULL "
            "AND title = 'dream synthesis note'"
        ).fetchone()[0]
        assert memcount == 1


def test_readonly_run_is_noop(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _seed_memory(rt, store, "alpha note", score=100)

    fake = FakeLLM([_tool_turn("search", {"view": "dreamable"}), _stop_turn()])
    run_dream_pass(store, hub=rt.hub, config=_enabled(), transport=fake)
    rows = _dream_log_rows(store)
    assert len(rows) == 1 and rows[0][0] == "noop"


def test_last_dreamt_rotation(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    a = _seed_memory(rt, store, "copper nitrate", score=100)
    b = _seed_memory(rt, store, "palladium hydrogen", score=50)

    fake = FakeLLM([_stop_turn()])  # model writes nothing
    run_dream_pass(store, hub=rt.hub, config=_enabled(), transport=fake)

    # seed a was surfaced → its score dropped → b is now the most-due seed
    seed = store.select_dream_seed()
    (cid_b,) = store.card_chunk_ids([b])
    assert seed == cid_b
    assert a != b


# ── guards ──────────────────────────────────────────────────────────


def test_acquire_disabled_message(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _seed_memory(rt, store, "needs a paper", score=100)

    fake = FakeLLM([_tool_turn("acquire", {"identifier": "doi:10.1/x"}), _stop_turn()])
    # acquire_enabled defaults False
    run_dream_pass(store, hub=rt.hub, config=_enabled(), transport=fake)
    with store.pool.connection() as conn:
        tr = conn.execute("SELECT transcript FROM dream_transcripts").fetchone()[0]
    assert "disabled" in tr[0]["result"]
    # no write happened → noop
    assert _dream_log_rows(store)[0][0] == "noop"


def test_max_turns_backstop(runtime_with_store: PrecisRuntime) -> None:
    rt = runtime_with_store
    store = rt.hub.store
    assert store is not None
    _seed_memory(rt, store, "loop bait", score=100)

    # model always asks for another tool call → only max_turns stops it
    never_stops = [
        _tool_turn("search", {"view": "dreamable"}, call_id=f"c{i}") for i in range(50)
    ]
    fake = FakeLLM(never_stops)
    out = run_dream_pass(
        store, hub=rt.hub, config=_enabled(max_turns=3), transport=fake
    )
    assert out["claimed"] == 1
    assert len(fake.bodies) == 3  # exactly max_turns LLM calls
    assert _dream_log_rows(store)[0][1] == 3  # turns recorded
