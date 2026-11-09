"""revisions.writer records who stood behind a write (migration 0279)."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from precis import agentlog, serve_ledger
from precis.filing_origin import WRITER_MODEL_ENV_VAR, writer_label
from precis.runtime import PrecisRuntime
from precis.runtime.trust_verb import WRITER_NUDGE
from precis.store import Store
from precis.store.revision_context import current_revision_context, revision_context
from precis.utils.inproc_context import TickContext, tick_context


class _Session:
    def __init__(self, model: str | None = None) -> None:
        self.client_params = SimpleNamespace(
            clientInfo=SimpleNamespace(name="writer-test", version="1"),
            meta=SimpleNamespace(model=model),
        )


@pytest.fixture(autouse=True)
def _clean_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(agentlog.ENV_VAR, raising=False)
    monkeypatch.delenv(agentlog.SOURCE_ENV_VAR, raising=False)
    monkeypatch.delenv(WRITER_MODEL_ENV_VAR, raising=False)
    with tick_context(TickContext()), serve_ledger.session_scope(None):
        yield


def _put(runtime: PrecisRuntime, **extra: Any) -> tuple[int, str]:
    title = f"writer {uuid4().hex}"
    body, error = runtime.dispatch_with_status(
        "put", {"kind": "memory", "text": title, **extra}
    )
    assert not error, body
    store = runtime.store
    assert store is not None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'memory' AND title = %s", (title,)
        ).fetchone()
    assert row is not None, body
    return int(row[0]), body


def _writers(store: Store, ref_id: int) -> list[dict[str, Any] | None]:
    return [r.writer for r in store.revisions_for("ref", ref_id)]


def _claim(**writer: str) -> dict[str, Any]:
    return {"__extras__": {"writer": writer}}


def test_session_write_stamps_client_and_session(
    runtime_with_store: PrecisRuntime,
) -> None:
    with serve_ledger.session_scope(_Session()):
        ref_id, _ = _put(runtime_with_store)
    store = runtime_with_store.store
    assert store is not None
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert writer["client"] == "writer-test/1"
    assert writer["actor"] == "agent"
    assert "session_id" in writer and "model" not in writer


def test_claimed_model_and_human_are_labelled(
    runtime_with_store: PrecisRuntime,
) -> None:
    ref_id, _ = _put(
        runtime_with_store, **_claim(model="claude-opus-5-5", human="reto")
    )
    store = runtime_with_store.store
    assert store is not None
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert writer["model"] == "claude-opus-5-5"
    assert writer["model_source"] == "self"
    assert writer["human"] == "reto"
    assert writer["human_via"] == "mediated"
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    assert ref.meta["filed_by"]["model"] == "claude-opus-5-5"


def test_declared_model_beats_a_claim(runtime_with_store: PrecisRuntime) -> None:
    with serve_ledger.session_scope(_Session(model="declared-model")):
        ref_id, _ = _put(runtime_with_store, **_claim(model="claimed-model"))
    store = runtime_with_store.store
    assert store is not None
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert (writer["model"], writer["model_source"]) == ("declared-model", "declared")


def test_env_model_is_the_lowest_precedence_source(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scripted write with no session or claim stamps its model from
    ``PRECIS_WRITER_MODEL`` (gr572670); a per-call claim or a client's
    declaration still wins over the ambient var."""
    monkeypatch.setenv(WRITER_MODEL_ENV_VAR, "script-model")
    store = runtime_with_store.store
    assert store is not None
    ref_id, _ = _put(runtime_with_store)
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert (writer["model"], writer["model_source"]) == ("script-model", "env")
    ref = store.get_ref(kind="memory", id=ref_id)
    assert ref is not None
    assert ref.meta["filed_by"]["model"] == "script-model"

    ref_id, _ = _put(runtime_with_store, **_claim(model="claimed-model"))
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert (writer["model"], writer["model_source"]) == ("claimed-model", "self")

    with serve_ledger.session_scope(_Session(model="declared-model")):
        ref_id, _ = _put(runtime_with_store)
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert (writer["model"], writer["model_source"]) == ("declared-model", "declared")


def test_a_blank_env_model_stamps_nothing(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``scripts/prod-precis`` exports the var unconditionally, so blank or
    whitespace must not stamp."""
    monkeypatch.setenv(WRITER_MODEL_ENV_VAR, "  ")
    ref_id, _ = _put(runtime_with_store)
    store = runtime_with_store.store
    assert store is not None
    (writer,) = _writers(store, ref_id)
    assert writer is not None and "model" not in writer


def test_a_claim_does_not_reach_the_machine_edge_context(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming the model must leave ``ctx.model`` empty, or every agent link
    would route through the machine-edge gate."""
    seen: list[Any] = []
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler is not None
    real = type(handler).put

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(current_revision_context())
        return real(self, *args, **kwargs)

    monkeypatch.setattr(type(handler), "put", spy)
    _put(runtime_with_store, **_claim(model="claude-opus-5-5"))
    assert seen and not seen[0].model
    assert seen[0].writer["model"] == "claude-opus-5-5"


def test_trust_act_keeps_its_reason_and_gains_a_writer(
    runtime_with_store: PrecisRuntime,
) -> None:
    ref_id, _ = _put(runtime_with_store)
    store = runtime_with_store.store
    assert store is not None
    state = store.trust_state("ref", ref_id)
    assert state is not None
    body, error = runtime_with_store.dispatch_with_status(
        "edit",
        {
            "kind": "memory",
            "id": ref_id,
            "mode": "trust",
            "__extras__": {
                "trust": {
                    "confidence": "considered",
                    "reason": "re-derived it",
                    "expected_sha": state.sha,
                },
                "writer": {"model": "reviewer-model"},
            },
        },
    )
    assert not error, body
    last = store.revisions_for("ref", ref_id)[-1]
    assert last.reason == "re-derived it"
    assert last.writer is not None and last.writer["model"] == "reviewer-model"


def test_human_ingress_is_direct(runtime_with_store: PrecisRuntime) -> None:
    with revision_context(
        actor="user:rs", trust_origin="human", trust_actor_type="human"
    ):
        ref_id, _ = _put(runtime_with_store)
    store = runtime_with_store.store
    assert store is not None
    (writer,) = _writers(store, ref_id)
    assert writer is not None
    assert (writer["human"], writer["human_via"]) == ("user:rs", "direct")


def test_outer_writer_wins_over_the_verb(runtime_with_store: PrecisRuntime) -> None:
    with revision_context(actor="garden", writer={"source": "garden"}):
        ref_id, _ = _put(runtime_with_store)
    store = runtime_with_store.store
    assert store is not None
    assert _writers(store, ref_id) == [{"source": "garden"}]


def test_nested_context_merges_and_first_stamp_of_a_tx_stays(store: Store) -> None:
    with revision_context("a", writer={"source": "outer", "model": "m1"}):
        with revision_context(writer={"model": "m2"}) as ctx:
            assert ctx.writer == {"source": "outer", "model": "m2"}
    ref = store.insert_ref(
        kind="finding", slug=None, title=f"writer {uuid4().hex}", meta={"scope": "Cu"}
    )
    with revision_context("first", writer={"source": "one"}), store.tx() as conn:
        conn.execute("UPDATE refs SET title = 'first' WHERE ref_id = %s", (ref.id,))
        conn.execute(
            "SELECT set_config('precis.writer', '{\"source\": \"two\"}', true)"
        )
        conn.execute("UPDATE refs SET title = 'second' WHERE ref_id = %s", (ref.id,))
    assert _writers(store, ref.id)[-1] == {"source": "one"}


def test_statement_cannot_name_its_writer(store: Store) -> None:
    ref = store.insert_ref(
        kind="finding", slug=None, title=f"writer {uuid4().hex}", meta={"scope": "Cu"}
    )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO revisions (target_kind, target_id, event, actor, reason, "
            " prev_state, writer) VALUES ('ref', %s, 'edited', 'x', 'y', '{}', "
            ' \'{"model": "forged"}\')',
            (ref.id,),
        )
    assert _writers(store, ref.id)[-1] is None


@pytest.mark.parametrize(
    "writer",
    [
        {"model": "bad model; drop"},
        {"human": ""},
        {"model": 7},
        {"session_id": "forged"},
        {"model_source": "agentlog"},
    ],
)
def test_bad_claims_are_refused(
    runtime_with_store: PrecisRuntime, writer: dict[str, Any]
) -> None:
    body, error = runtime_with_store.dispatch_with_status(
        "put", {"kind": "memory", "text": "x", "__extras__": {"writer": writer}}
    )
    assert error and "writer" in body


def test_nudge_shows_once_and_only_when_the_model_is_unknown(
    runtime_with_store: PrecisRuntime,
) -> None:
    with serve_ledger.session_scope(_Session()):
        _, named = _put(runtime_with_store, **_claim(model="claude-opus-5-5"))
        _, first = _put(runtime_with_store)
        _, second = _put(runtime_with_store)
    assert WRITER_NUDGE not in named
    assert WRITER_NUDGE in first
    assert WRITER_NUDGE not in second


def test_sessionless_write_gets_no_nudge(runtime_with_store: PrecisRuntime) -> None:
    _, body = _put(runtime_with_store)
    assert WRITER_NUDGE not in body


def test_get_shows_first_and_last_writer(runtime_with_store: PrecisRuntime) -> None:
    ref_id, _ = _put(runtime_with_store, **_claim(model="opus-a"))
    body, error = runtime_with_store.dispatch_with_status(
        "edit",
        {
            "kind": "memory",
            "id": ref_id,
            "mode": "append",
            "text": "more",
            "__extras__": {"writer": {"model": "sonnet-b"}},
        },
    )
    assert not error, body
    read, error = runtime_with_store.dispatch_with_status(
        "get", {"kind": "memory", "id": ref_id}
    )
    assert not error
    assert "written: opus-a (self) · last: sonnet-b (self)" in read
    assert read.rstrip().splitlines()[-1].startswith("trust: ")


@pytest.mark.parametrize(
    ("writer", "label"),
    [
        ({"model": "m", "model_source": "self"}, "m (self)"),
        ({"model": "m", "model_source": "env"}, "m (env)"),
        ({"model": "m", "model_source": "agentlog", "source": "dream"}, "m, dream"),
        ({"actor": "agent", "client": "claude-code/2.1"}, "agent, claude-code"),
        (
            {
                "model": "m",
                "model_source": "self",
                "human": "reto",
                "human_via": "mediated",
            },
            "human reto via m (self)",
        ),
        (
            {"actor": "user:rs", "human": "user:rs", "human_via": "direct"},
            "human user:rs",
        ),
    ],
)
def test_writer_label(writer: dict[str, Any], label: str) -> None:
    assert writer_label(writer) == label
