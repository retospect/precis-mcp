"""Speaker attribution for asa's writes (gr478548, :mod:`precis.acting_user`).

A stdio ``precis serve`` spawned for one Discord turn writes as the speaker's
account (``user:<abbrev>`` in ``revisions.actor``); an unmapped speaker, an
unknown abbrev and every network transport stay ``agent``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from precis.acting_user import ENV_VAR, actor_for, install_from_env
from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import (
    current_revision_context,
    process_actor,
    set_process_actor,
)
from precis.store.types import ChunkInsert
from precis.users import hash_password


@pytest.fixture(autouse=True)
def _clear_process_actor() -> Iterator[None]:
    set_process_actor(None)
    yield
    set_process_actor(None)


def _account(store: Store, abbrev: str = "rs") -> None:
    store.create_web_user(login="reto", abbrev=abbrev, password=hash_password("pw"))


def _memory(store: Store) -> int:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title="a memory", meta={}, conn=conn
        )
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text="old", meta={"chunk_kind": "memory_body"})],
            conn=conn,
        )
    return ref.id


def _edit(runtime: PrecisRuntime, ref_id: int) -> None:
    out = runtime.dispatch(
        "edit", {"kind": "memory", "id": ref_id, "mode": "replace", "text": "new"}
    )
    assert "replaced body" in out, out


def _put_actor(runtime: PrecisRuntime, monkeypatch: pytest.MonkeyPatch) -> str:
    """The actor the put's writes run under.

    A creation writes no ``revisions`` row (0185: a ref created in the
    transaction is not a revision), so read the actor the store would
    send as ``precis.actor`` at the moment the handler creates the ref.
    """
    from precis.handlers.memory import MemoryHandler

    seen: list[str | None] = []
    real = MemoryHandler._create

    def spy(self: Any, **kw: Any) -> Any:
        ctx = current_revision_context()
        seen.append(ctx.actor if ctx else None)
        return real(self, **kw)

    monkeypatch.setattr(MemoryHandler, "_create", spy)
    out = runtime.dispatch("put", {"kind": "memory", "text": "chickens lay in March"})
    assert re.search(r"created memory me\d+", out), out
    assert len(seen) == 1, seen
    return str(seen[0])


# ── the stdio server's write actor ──────────────────────────────────────────


def test_mapped_speaker_put_and_edit_are_attributed(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account(store)
    installed = install_from_env(store, transport="stdio", environ={ENV_VAR: " RS "})
    assert installed == "user:rs"
    mid = _memory(store)
    _edit(runtime_with_store, mid)
    (rev,) = store.revisions_for("ref", mid)
    assert rev.actor == "user:rs"
    assert _put_actor(runtime_with_store, monkeypatch) == "user:rs"


def test_unmapped_speaker_writes_as_agent(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _account(store)
    assert install_from_env(store, transport="stdio", environ={ENV_VAR: ""}) is None
    mid = _memory(store)
    _edit(runtime_with_store, mid)
    (rev,) = store.revisions_for("ref", mid)
    assert rev.actor == "agent"
    assert _put_actor(runtime_with_store, monkeypatch) == "agent"


def test_unknown_abbrev_writes_as_agent(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    _account(store)
    assert install_from_env(store, transport="stdio", environ={ENV_VAR: "zz"}) is None
    mid = _memory(store)
    _edit(runtime_with_store, mid)
    (rev,) = store.revisions_for("ref", mid)
    assert rev.actor == "agent"


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_network_transport_never_impersonates(
    runtime_with_store: PrecisRuntime, store: Store, transport: str
) -> None:
    """The shared server ignores the variable, and clears a stale value."""
    _account(store)
    set_process_actor("user:stale")
    assert install_from_env(store, transport=transport, environ={ENV_VAR: "rs"}) is None
    assert process_actor() is None
    mid = _memory(store)
    _edit(runtime_with_store, mid)
    (rev,) = store.revisions_for("ref", mid)
    assert rev.actor == "agent"


def test_enclosing_context_still_wins_over_process_actor(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    from precis.store.revision_context import revision_context

    set_process_actor("user:rs")
    mid = _memory(store)
    with revision_context(actor="hub-refine"):
        _edit(runtime_with_store, mid)
    (rev,) = store.revisions_for("ref", mid)
    assert rev.actor == "hub-refine"


def test_storeless_server_stays_agent() -> None:
    assert install_from_env(None, transport="stdio", environ={ENV_VAR: "rs"}) is None
    assert process_actor() is None


def test_server_main_passes_its_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """``precis serve`` hands the transport through, so only stdio honours it."""
    import precis.acting_user as acting_user
    import precis.install_watchdog as watchdog
    import precis.server as server
    import precis.workers.heartbeat as heartbeat

    seen: list[tuple[Any, str]] = []
    rt = SimpleNamespace(store=object())
    monkeypatch.setattr(server, "_init_runtime", lambda: rt)
    monkeypatch.setattr(server, "_log_version_banner", lambda: None)
    for name in (
        "_warm_embedder_background",
        "_warm_md_index_background",
        "_warm_python_semantic_background",
    ):
        monkeypatch.setattr(server, name, lambda runtime: None)
    monkeypatch.setattr(watchdog, "start_install_watchdog", lambda: None)
    monkeypatch.setattr(watchdog, "start_checkout_watchdog", lambda: None)
    monkeypatch.setattr(heartbeat, "start_nas_attest_thread", lambda *a, **k: None)
    monkeypatch.setattr(
        acting_user,
        "install_from_env",
        lambda store, *, transport: seen.append((store, transport)),
    )
    monkeypatch.setattr(server.mcp, "run", lambda transport: None)
    server.main(transport="stdio")
    assert seen == [(rt.store, "stdio")]


# ── asa hands the speaker to the turn ───────────────────────────────────────


@pytest.fixture
def _hermetic_oauth(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)


def _claude_cfg() -> Any:
    from asa_bot.config import LLMConfig

    return dataclasses.replace(
        LLMConfig(), command=["claude", "-p", "--model", "claude-opus-4-8"]
    )


def _claude_turn_env(monkeypatch: pytest.MonkeyPatch, acting_user: str | None) -> dict:
    import precis.utils.claude_agent as claude_agent
    from asa_bot.claude_invoke import invoke

    env: dict[str, str] = {}

    async def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        env.update(kwargs.get("env") or {})
        return SimpleNamespace(stdout="", stderr="")

    monkeypatch.setattr(claude_agent, "run_claude_async", fake_run)
    asyncio.run(
        invoke(_claude_cfg(), "sys", "hi", conv_slug="c-1", acting_user=acting_user)
    )
    return env


@pytest.mark.usefixtures("_hermetic_oauth")
def test_claude_lane_carries_the_speaker_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _claude_turn_env(monkeypatch, "rs")[ENV_VAR] == "rs"


@pytest.mark.usefixtures("_hermetic_oauth")
def test_claude_lane_unmapped_blanks_an_inherited_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_VAR, "someone-else")
    assert _claude_turn_env(monkeypatch, None)[ENV_VAR] == ""


@pytest.mark.usefixtures("_hermetic_oauth")
@pytest.mark.parametrize(("speaker", "actor"), [("rs", "user:rs"), (None, None)])
def test_chain_lane_wraps_the_tool_loop(
    monkeypatch: pytest.MonkeyPatch, speaker: str | None, actor: str | None
) -> None:
    from asa_bot.claude_invoke import invoke
    from asa_bot.config import LLMConfig
    from precis.utils.llm.router import LlmResult, Tier

    seen: dict[str, Any] = {}

    def fake_route(req: Any) -> LlmResult:
        ctx = current_revision_context()
        seen["actor"] = ctx.actor if ctx else None
        seen["env"] = req.env_overlay
        return LlmResult(
            text="ok", cost_usd=0.0, turns_used=1, model="m", tier=Tier.BIG
        )

    monkeypatch.setattr("asa_bot.claude_invoke.warm_runtime", lambda: None)
    monkeypatch.setattr("asa_bot.claude_invoke.route", fake_route)
    asyncio.run(invoke(LLMConfig(), "sys", "hi", conv_slug="c-2", acting_user=speaker))
    assert seen["actor"] == actor
    assert seen["env"][ENV_VAR] == (speaker or "")


def test_actor_for_lowercases() -> None:
    assert actor_for(" RS ") == "user:rs"


# ── the deployed mcp.json passes the variable through ───────────────────────


def test_mcp_json_passes_the_speaker_through() -> None:
    import json
    from pathlib import Path

    import jinja2

    root = Path(__file__).resolve().parent.parent
    src = root / "deploy" / "roles" / "asa_bot" / "templates" / "claude_mcp.json.j2"
    rendered = (
        jinja2.Environment(undefined=jinja2.ChainableUndefined)
        .from_string(src.read_text(encoding="utf-8"))
        .render(
            postgres_host="pg",
            hostvars={"pg": {"ansible_host": "203.0.113.10"}},
            pgbouncer_port=6432,
            claude_mcp_pgpass_file="/home/deploy/.pgpass",
        )
    )
    env = json.loads(rendered)["mcpServers"]["precis"]["env"]
    assert env[ENV_VAR] == "${PRECIS_ACTING_USER:-}"
