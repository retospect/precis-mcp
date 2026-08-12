"""asa_bot.slash's ``/model`` command — model-alias resolution.

``_resolve_model_alias`` used to consult a private, stale ``MODEL_ALIASES``
dict pinning vendor model ids (gr193672-adjacent model-vocabulary-drift
fix). It now resolves a capability-tier alias through the router
(``PLANNER_TIER_BY_ALIAS`` + ``resolve_model``) at command time, so
``/model opus`` always names the router's current FRONTIER model rather
than whatever id was hardcoded here at the time this file was last
touched. A target that isn't one of those alias keys still passes
through unchanged, so a one-off custom model id keeps working.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# asa_bot.slash imports discord.py (the `[asa]` extra). Skip cleanly where
# it isn't installed — mirrors the importorskip pattern in
# test_asa_split_for_discord.py (CI's `--extra all` omits `[asa]`).
pytest.importorskip("discord")

from asa_bot.slash import (
    Runtime,
    SlashContext,
    _resolve_model_alias,
    cmd_model,
)
from precis.utils.llm.router import (
    PLANNER_TIER_BY_ALIAS,
    Tier,
    resolve_model,
)


def test_resolve_model_alias_opus_matches_router_frontier() -> None:
    assert _resolve_model_alias("opus") == resolve_model(Tier.FRONTIER)


def test_resolve_model_alias_every_planner_alias_matches_router() -> None:
    for alias, tier in PLANNER_TIER_BY_ALIAS.items():
        if alias == "local":
            continue  # the chain sentinel — deliberately NOT resolved, below
        assert _resolve_model_alias(alias) == resolve_model(tier)


def test_resolve_model_alias_local_is_passed_through_unresolved() -> None:
    """``local`` must survive as the sentinel claude_invoke maps to the BIG
    placement chain — eagerly resolving it would collapse it to the BIG
    tier's claude default and route the turn back onto ``claude -p``."""
    assert _resolve_model_alias("local") == "local"
    assert _resolve_model_alias("LOCAL") == "local"


def test_resolve_model_alias_custom_id_passes_through() -> None:
    assert _resolve_model_alias("some-custom-model-id") == "some-custom-model-id"


def test_resolve_model_alias_custom_id_keeps_original_casing() -> None:
    # Some OpenAI-compatible endpoints are case-sensitive: a non-alias custom
    # id must survive verbatim, while alias matching stays case-insensitive.
    assert _resolve_model_alias("MyProxy-Model-ID") == "MyProxy-Model-ID"
    assert _resolve_model_alias("OPUS") == resolve_model(Tier.FRONTIER)


class _FakeSendTarget:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    async def send(self, content: str = "", *, file: Any = None) -> None:
        self._sink.append(content)


def _make_ctx(positional: list[str], sink: list[str]) -> SlashContext:
    # message/precis/config are unused placeholders here: cmd_model only
    # touches positional/runtime/send (and config, but only on the
    # empty-positional branch, which these tests don't exercise).
    return SlashContext(
        message=object(),  # type: ignore[arg-type]
        positional=positional,
        kwargs={},
        precis=object(),  # type: ignore[arg-type]
        config=object(),  # type: ignore[arg-type]
        runtime=Runtime(),
        soul="",
        tool_hints="",
        ctx_from_message=lambda m: m,
        reply_target=lambda m: _FakeSendTarget(sink),
    )


def test_cmd_model_opus_sets_router_resolved_override() -> None:
    sink: list[str] = []
    ctx = _make_ctx(["opus"], sink)
    asyncio.run(cmd_model(ctx))
    assert ctx.runtime.model_override == resolve_model(Tier.FRONTIER)
    assert sink and resolve_model(Tier.FRONTIER) in sink[0]


def test_cmd_model_custom_id_passes_through() -> None:
    sink: list[str] = []
    ctx = _make_ctx(["some-custom-model-id"], sink)
    asyncio.run(cmd_model(ctx))
    assert ctx.runtime.model_override == "some-custom-model-id"
