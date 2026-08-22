"""Local hardware is a sunk cost — the dollar caps must not count it.

`llm_call_log.cost_usd` mixes real money (claude / OpenRouter) with a *priced*
estimate for calls served by the cluster's own GPUs
(:mod:`precis.budget.pricing`). Nothing leaves an account for the latter, but
`PRECIS_DAILY_COST_CEILING` summed both — so as passes moved onto local hardware
the ceiling kept filling (~$0.35 a planner tick), and when it tripped it stopped
the *local* work too. A budget gate that idles a machine you already paid for is
doing harm, not nothing.

Migration 0112 records `placement`, and the caps exclude `'local'`. The capability tiers + placement chains breaker already drew this line via `_rung_is_cloud`; these tests pin that the
two never drift apart again.
"""

from __future__ import annotations

from typing import Any

from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Rung,
    Tier,
    Transport,
    _fallback_placement,
    _placement_of,
    _rung_is_cloud,
)

# ── the classifier ────────────────────────────────────────────────────────


def test_placement_string_tracks_the_breaker_classifier() -> None:
    """One definition. `_placement_of` is the string form of `_rung_is_cloud` —
    a second, independent notion of "is this local" is exactly how the caps
    drifted into never firing before."""
    for rung in (
        Rung(transport=Transport.OPENAI_TOOLS, model="qwen3-235b", label="local"),
        Rung(transport=Transport.OPENAI_TOOLS, model="z-ai/glm-5.2", label="cloud"),
        Rung(transport=Transport.CLAUDE_AGENT),
        Rung(transport=Transport.LOCAL),
    ):
        expected = "cloud" if _rung_is_cloud(rung) else "local"
        assert _placement_of(rung) == expected


def test_operator_local_label_wins_over_transport() -> None:
    """`llm.chain.big`'s rungs are BOTH `openai_tools` — the local qwen primary
    and the cloud glm-5.2 fallback. Transport alone cannot tell them apart, so
    the operator's `placement` label is the only real discriminator."""
    local = Rung(transport=Transport.OPENAI_TOOLS, model="qwen3-235b", label="local")
    cloud = Rung(transport=Transport.OPENAI_TOOLS, model="z-ai/glm-5.2", label="cloud")

    assert _placement_of(local) == "local"
    assert _placement_of(cloud) == "cloud"
    assert local.transport is cloud.transport  # the point


def test_claude_transports_are_always_cloud() -> None:
    """A claude rung spends real money (or subscription quota) whatever it is
    labelled — it must never be excludable as 'local'."""
    for t in (Transport.CLAUDE_AGENT, Transport.CLAUDE_P):
        assert _placement_of(Rung(transport=t)) == "cloud"


# ── _record_dispatch's chokepoint fallback ─────────────────────────────────


def test_fallback_placement_claude_transports_are_cloud() -> None:
    for t in (Transport.CLAUDE_AGENT, Transport.CLAUDE_P):
        assert _fallback_placement(t) == "cloud"


def test_fallback_placement_local_transport_is_local() -> None:
    assert _fallback_placement(Transport.LOCAL) == "local"


def test_fallback_placement_oss_transport_follows_base_url(monkeypatch: Any) -> None:
    # No hosted endpoint configured ⇒ the only way an OSS transport ran is a
    # local served_by slot.
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    assert _fallback_placement(Transport.OPENAI_TOOLS) == "local"
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.example/v1")
    assert _fallback_placement(Transport.OPENAI_TOOLS) == "cloud"


# ── the failover stamp: follow the money, not the intent ──────────────────


def test_failover_to_cloud_is_recorded_as_cloud(monkeypatch: Any) -> None:
    """The load-bearing case. A local primary that FAILS OVER to a cloud rung
    really did spend money; stamping rung 0's 'local' would hide that spend from
    the caps permanently — the exact failure direction that costs money.
    """
    from precis.utils.llm import router as R

    calls: list[str] = []

    class _Provider:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def run(self, req: Any, *, model: str) -> LlmResult:
            calls.append(model)
            return LlmResult(
                text="",
                cost_usd=0.5,
                turns_used=None,
                model=model,
                tier=Tier.BIG,
                error="endpoint down" if self.fail else None,
            )

    def _provider_for(transport: Transport, *, bare: bool = False) -> Any:
        # rung 0 (local) fails, rung 1 (cloud) succeeds
        return _Provider(fail=len(calls) == 0)

    monkeypatch.setattr(R, "provider_for", _provider_for, raising=True)

    ladder = [
        Rung(transport=Transport.OPENAI_TOOLS, model="qwen3-235b", label="local"),
        Rung(transport=Transport.OPENAI_TOOLS, model="z-ai/glm-5.2", label="cloud"),
    ]
    res = R.FailoverProvider(ladder).run(
        LlmRequest(tier=Tier.BIG, prompt="x"), model="qwen3-235b"
    )

    assert res.error is None
    assert res.placement == "cloud", "a cloud fallback must not be excluded as local"


def test_local_primary_that_succeeds_is_recorded_as_local(monkeypatch: Any) -> None:
    from precis.utils.llm import router as R

    class _Ok:
        def run(self, req: Any, *, model: str) -> LlmResult:
            return LlmResult(
                text="", cost_usd=0.35, turns_used=None, model=model, tier=Tier.BIG
            )

    monkeypatch.setattr(R, "provider_for", lambda t, *, bare=False: _Ok(), raising=True)

    ladder = [Rung(transport=Transport.OPENAI_TOOLS, model="qwen3-235b", label="local")]
    res = R.FailoverProvider(ladder).run(
        LlmRequest(tier=Tier.BIG, prompt="x"), model="qwen3-235b"
    )

    assert res.placement == "local"


# ── the caps' SQL ─────────────────────────────────────────────────────────


def test_every_dollar_cap_excludes_local() -> None:
    """All three money caps, not just the daily ceiling.

    The per-todo and per-tree caps sum the same column; leaving either counting
    local would halt a todo for spending nothing. Asserted against the SQL text
    because these are raw queries with no seam to inject.
    """
    import inspect

    from precis.workers import planner_guardrails as pg

    for fn in (pg._read_cost_usd, pg._read_tree_cost_usd, pg._read_daily_cost):
        src = inspect.getsource(fn)
        assert "placement IS DISTINCT FROM 'local'" in src, (
            f"{fn.__name__} still counts local rows as spend — a tripped cap "
            "would idle the GPUs we are trying to keep busy"
        )


def test_tick_cap_is_not_placement_aware() -> None:
    """The convergence guard must keep firing on local work.

    Excluding local from the *dollar* caps is right (it is not money); doing the
    same to the tick cap would let a non-converging local planner spin forever.
    Cheap hardware is a reason to run more work, not a reason to stop noticing a
    task that will never finish.
    """
    import inspect

    from precis.workers import planner_guardrails as pg

    assert "placement" not in inspect.getsource(pg._read_tick_count)
