"""Per-operation LLM model routing — Phase 1 (registry + resolver).

``docs/proposals/llm-operation-routing.md``: ``operations.py`` owns the
steerable allow-list (:data:`LLM_OPERATIONS`) and the deliberately-excluded
set (:data:`EXCLUDED_OPERATIONS`); :func:`resolve_op` layers a runtime
``app_settings`` override (``live_config.op_override``) over a legacy
``PRECIS_*_MODEL`` env hatch over the registry literal, but ONLY for a
*registered* source — an unregistered source (a router-bypasser or a
functional pin like ``classify``) is untouched, so today's call-site
behavior survives byte-identical.

Covered here, mapped to the proposal's acceptance criteria:

- AC1 — ships dark (no override row → registry default; unregistered → None)
- AC2 — model pin via override
- AC3 — tier remap via override
- AC4/AC8 — classify-not-clobbered (the load-bearing non-registered case)
- AC6 — registry well-formedness / drift guard
- AC7 — env hatch precedence + cast call sites migrated off ``model=``

Plus one ``dispatch()``-level integration test guarding the *ordering* of
the op-resolution block inside ``dispatch()`` — the tier remap must land
before ``_tier_gen_defaults``, ``resolve_model``, ``resolve_chain``, and the
breaker's ``gate_tier(req.tier)`` all read ``req.tier``, a property
``resolve_op()``-level unit tests can't see (they never touch ``dispatch()``).

DB-free: the store is faked (mirrors ``tests/test_llm_live_config.py``'s
``_bind`` idiom) and the TTL cache is busted around every test so no
override leaks between cases.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis.utils.claude_agent import AgentResult
from precis.utils.llm import live_config, operations
from precis.utils.llm import router as llm_router
from precis.utils.llm.operations import EXCLUDED_OPERATIONS, LLM_OPERATIONS
from precis.utils.llm.router import LlmRequest, Tier, dispatch


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    # live_config's TTL cache is a module global — clear it around every
    # test so a cached (or negative-cached) override can't leak between
    # cases, matching test_llm_live_config.py's isolation.
    live_config.bust_cache()
    yield
    live_config.bust_cache()


class _Store:
    """Opaque sentinel — ``get_setting`` is stubbed, so it's never queried."""


def _bind(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, str] | None
) -> dict[str, int]:
    """Point live_config at a fake store returning ``rows`` (a ``None`` store
    when ``rows is None``). Returns a dict whose ``n`` counts get_setting hits."""
    calls = {"n": 0}
    store = _Store() if rows is not None else None
    monkeypatch.setattr(meter, "active_store", lambda: store)

    def fake_get(_store: object, key: str) -> str | None:
        calls["n"] += 1
        return (rows or {}).get(key)

    monkeypatch.setattr(budget_settings, "get_setting", fake_get)
    return calls


def _set_op_override(monkeypatch: pytest.MonkeyPatch, source: str, value: dict) -> None:
    """Seed a single ``llm.op.<source>`` override row (JSON-encoded)."""
    import json

    _bind(monkeypatch, {live_config.op_key(source): json.dumps(value)})


# ── AC1: ships dark ────────────────────────────────────────────────────


def test_reading_brief_default_with_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `llm.op.reading_brief` row → the exact (tier, model) the call site
    pinned before migration."""
    _bind(monkeypatch, {})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-sonnet-5")


def test_meditation_default_with_no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `llm.op.meditation` row → the exact (tier, model) the call site
    pinned before migration."""
    _bind(monkeypatch, {})
    assert operations.resolve_op("meditation") == (Tier.FRONTIER, "claude-sonnet-5")


def test_dark_without_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """No store bound at all (DB-free) still resolves the registry default —
    op_override degrades to None, never raises."""
    _bind(monkeypatch, None)
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-sonnet-5")


@pytest.mark.parametrize("source", ["dream", "classify", "figure", None, ""])
def test_unregistered_source_returns_none(
    monkeypatch: pytest.MonkeyPatch, source: str | None
) -> None:
    """Any non-registered source — the caller keeps its own path untouched."""
    _bind(monkeypatch, {})
    assert operations.resolve_op(source) is None


# ── AC2: model pin via override ────────────────────────────────────────


def test_model_pin_via_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_op_override(monkeypatch, "reading_brief", {"model": "claude-opus-4-8"})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-opus-4-8")


def test_model_pin_cleared_reverts_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_op_override(monkeypatch, "reading_brief", {"model": "claude-opus-4-8"})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-opus-4-8")

    # Clear the row and bust the cache — the next read re-queries and finds
    # nothing, so the resolver falls back to the registry default.
    _bind(monkeypatch, {})
    live_config.bust_cache()
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-sonnet-5")


# ── AC3: tier remap via override ───────────────────────────────────────


def test_tier_remap_keeps_registry_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `{"tier": ...}` override with no `model` remaps the tier but keeps
    the registry's default model."""
    _set_op_override(monkeypatch, "reading_brief", {"tier": "big"})
    assert operations.resolve_op("reading_brief") == (Tier.BIG, "claude-sonnet-5")


def test_tier_remap_with_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_op_override(
        monkeypatch, "reading_brief", {"tier": "big", "model": "z-ai/glm-5.2"}
    )
    assert operations.resolve_op("reading_brief") == (Tier.BIG, "z-ai/glm-5.2")


# ── AC7: env hatch precedence ───────────────────────────────────────────


def test_env_hatch_beats_registry_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DB override → the legacy PRECIS_READING_BRIEF_MODEL env hatch beats
    the registry literal."""
    _bind(monkeypatch, {})
    monkeypatch.setenv("PRECIS_READING_BRIEF_MODEL", "some-env-model")
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "some-env-model")


def test_db_override_beats_env_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB override > env hatch > registry literal — the full precedence
    ladder in one test."""
    monkeypatch.setenv("PRECIS_READING_BRIEF_MODEL", "some-env-model")
    _set_op_override(monkeypatch, "reading_brief", {"model": "db-model"})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "db-model")


# ── AC4/AC8: classify-not-clobbered ─────────────────────────────────────


def test_classify_untouched_even_with_a_stray_override_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`classify` is not in the registry, so `resolve_op` returns None even
    if an `llm.op.classify` row somehow exists — the override layer must
    never reach a non-registered source, or it would silently reopen the
    `classify` -> `"summarizer"` functional pin's empty-response bug."""
    _set_op_override(monkeypatch, "classify", {"model": "some-other-model"})
    assert operations.resolve_op("classify") is None


def test_classify_topics_untouched_even_with_a_stray_override_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_op_override(monkeypatch, "classify_topics", {"tier": "big"})
    assert operations.resolve_op("classify_topics") is None


def test_classify_is_excluded_with_reason() -> None:
    assert "classify" in EXCLUDED_OPERATIONS
    reason = operations.excluded_reason("classify")
    assert reason is not None
    assert "summarizer" in reason or "pinned" in reason


def test_classify_topics_is_excluded_with_reason() -> None:
    assert "classify_topics" in EXCLUDED_OPERATIONS
    reason = operations.excluded_reason("classify_topics")
    assert reason is not None


def test_fix_gripe_is_excluded_with_reason() -> None:
    """The other excluded class — a router-bypasser, not a functional pin."""
    assert "fix_gripe" in EXCLUDED_OPERATIONS
    reason = operations.excluded_reason("fix_gripe")
    assert reason is not None
    assert "dispatch" in reason or "bypass" in reason


# ── Bad override degrades safe ──────────────────────────────────────────


def test_bad_tier_string_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid tier value in the override is logged + ignored, not raised
    — the resolver keeps the default tier."""
    _set_op_override(monkeypatch, "reading_brief", {"tier": "nonsense-tier"})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-sonnet-5")


def test_empty_override_dict_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_op_override(monkeypatch, "reading_brief", {})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-sonnet-5")


def test_blank_model_override_does_not_clobber_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_op_override(monkeypatch, "reading_brief", {"model": ""})
    assert operations.resolve_op("reading_brief") == (Tier.FRONTIER, "claude-sonnet-5")


# ── AC6: registry well-formedness / drift guard ─────────────────────────


def test_registry_and_excluded_keys_are_disjoint() -> None:
    """A source can't be both steerable and excluded."""
    assert set(LLM_OPERATIONS) & set(EXCLUDED_OPERATIONS) == set()


def test_every_op_default_has_a_valid_tier() -> None:
    for source, default in LLM_OPERATIONS.items():
        assert isinstance(default.tier, Tier), f"{source}: tier {default.tier!r}"


def test_every_op_default_has_nonempty_label_and_description() -> None:
    for source, default in LLM_OPERATIONS.items():
        assert isinstance(default.label, str) and default.label.strip(), source
        assert isinstance(default.description, str) and default.description.strip(), (
            source
        )


def test_is_steerable_agrees_with_registry_membership() -> None:
    for source in LLM_OPERATIONS:
        assert operations.is_steerable(source) is True
    for source in EXCLUDED_OPERATIONS:
        assert operations.is_steerable(source) is False
    assert operations.is_steerable("dream") is False
    assert operations.is_steerable(None) is False
    assert operations.is_steerable("") is False


# ── AC7: cast call sites migrated off model= ────────────────────────────


def test_reading_brief_registry_carries_default_and_env_hatch() -> None:
    """The migrated call site's former `model=` literal and its
    `PRECIS_READING_BRIEF_MODEL` env hatch now live in the registry, not at
    the `briefing_cast.py` call site."""
    default = LLM_OPERATIONS["reading_brief"]
    assert default.model == "claude-sonnet-5"
    assert default.tier is Tier.FRONTIER
    assert default.env == "PRECIS_READING_BRIEF_MODEL"


def test_meditation_registry_carries_default_and_env_hatch() -> None:
    default = LLM_OPERATIONS["meditation"]
    assert default.model == "claude-sonnet-5"
    assert default.tier is Tier.FRONTIER
    assert default.env == "PRECIS_MEDITATION_MODEL"


def test_briefing_cast_call_site_has_no_hardcoded_model_kwarg() -> None:
    """`DispatchClient(..., source="reading_brief", ...)` no longer passes a
    literal `model=` — the default lives in the registry (AC7)."""
    import inspect

    from precis.reading import briefing_cast

    src = inspect.getsource(briefing_cast)
    idx = src.index('source="reading_brief"')
    # The DispatchClient(...) construction is a single statement; scan back
    # to its opening "DispatchClient(" and check no "model=" literal appears
    # between there and the matching source= kwarg's statement.
    call_start = src.rfind("DispatchClient(", 0, idx)
    stmt = src[call_start : idx + len('source="reading_brief"')]
    assert "model=" not in stmt


def test_meditation_call_site_has_no_hardcoded_model_kwarg() -> None:
    import inspect

    from precis.reading import meditation

    src = inspect.getsource(meditation)
    idx = src.index('source="meditation"')
    call_start = src.rfind("DispatchClient(", 0, idx)
    stmt = src[call_start : idx + len('source="meditation"')]
    assert "model=" not in stmt


# ── dispatch(): op-resolution ordering regression guard ─────────────────
#
# The unit tests above only exercise resolve_op() in isolation — they can't
# see whether dispatch() actually applies the tier remap *before* the
# downstream reads of req.tier (_tier_gen_defaults, resolve_model,
# resolve_chain, and the breaker's gate_tier(req.tier)). A future edit that
# reorders the op-resolution block to run after one of those would leave it
# gated/resolved on the call site's original tier — nothing at the
# resolve_op() level would catch that. Drives dispatch() end-to-end with the
# provider/breaker mocked, mirroring test_llm_router.py's
# test_dispatch_breaker_trip_is_flagged_paused (breaker.gate_tier spy) and
# test_dispatch_cloud_agent (call_claude_agent fake) idioms.


def test_dispatch_op_tier_remap_reaches_breaker_and_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`llm.op.reading_brief = {"tier": "big"}` remaps a FRONTIER call site
    request to BIG — and the remap must land before the breaker gate and the
    final result are built, or this would observe the stale FRONTIER tier."""
    _set_op_override(monkeypatch, "reading_brief", {"tier": "big"})

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        return AgentResult(
            final_text="brief text", cost_usd=0.1, duration_s=0.5, turns_used=1
        )

    monkeypatch.setattr(llm_router, "call_claude_agent", fake_agent)

    breaker_tiers: list[Tier] = []

    def spy_gate_tier(tier: Tier, **kwargs: object) -> str | None:
        breaker_tiers.append(tier)
        return None  # never trip — this test only observes what tier it saw

    monkeypatch.setattr("precis.budget.breaker.gate_tier", spy_gate_tier)

    out = dispatch(
        LlmRequest(
            tier=Tier.FRONTIER,
            source="reading_brief",
            prompt="x",
            tools_needed=True,
        )
    )

    # The tier that actually reached the breaker gate — proves the remap
    # landed before gate_tier(req.tier) read it, not after.
    assert breaker_tiers == [Tier.BIG]
    # The tier stamped into the final result is the remapped one too.
    assert out.tier is Tier.BIG
    assert out.model == "claude-sonnet-5"
    assert out.text == "brief text"


def test_dispatch_unregistered_source_keeps_call_site_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion case proving the remap branch is scoped to registered
    sources: an unregistered source (`dream`) with the same FRONTIER request
    sees no remap anywhere — the breaker gate and the result both keep
    FRONTIER, even with an (irrelevant) `llm.op.reading_brief` override also
    set."""
    _set_op_override(monkeypatch, "reading_brief", {"tier": "big"})

    def fake_agent(prompt: str, **kwargs: object) -> AgentResult:
        return AgentResult(
            final_text="dream text", cost_usd=0.1, duration_s=0.5, turns_used=1
        )

    monkeypatch.setattr(llm_router, "call_claude_agent", fake_agent)

    breaker_tiers: list[Tier] = []

    def spy_gate_tier(tier: Tier, **kwargs: object) -> str | None:
        breaker_tiers.append(tier)
        return None

    monkeypatch.setattr("precis.budget.breaker.gate_tier", spy_gate_tier)

    out = dispatch(
        LlmRequest(
            tier=Tier.FRONTIER,
            source="dream",
            prompt="x",
            tools_needed=True,
        )
    )

    assert breaker_tiers == [Tier.FRONTIER]
    assert out.tier is Tier.FRONTIER
