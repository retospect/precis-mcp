"""Keyword-triggered model routing — :mod:`precis.utils.llm.keyword_route`.

The rule seam is :mod:`precis.utils.llm.keyword_route` (ordered
``(pattern → target)`` rules, first match wins, whole-word +
case-insensitive) applied inside :func:`precis.utils.llm.router.route`.
Tests stub the DB snapshot (``_refresh_snapshot``) so no store is needed;
providers are monkeypatched exactly as ``test_llm_router`` does — a stub
``claude`` binary on PATH plus a credential keeps claude rungs "usable".
"""

from __future__ import annotations

import logging
import stat
from types import SimpleNamespace
from typing import Any

import pytest

from precis.utils.claude_agent import AgentResult
from precis.utils.claude_p import ClaudePResult
from precis.utils.llm import keyword_route, router
from precis.utils.llm.keyword_route import KeywordRule
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    route,
)


def _rule(
    rule_id: str,
    pattern: str,
    target: dict[str, Any],
    *,
    is_regex: bool = False,
    sources: set[str] | None = None,
    tiers: set[str] | None = None,
    enabled: bool = True,
) -> KeywordRule:
    return KeywordRule(
        rule_id=rule_id,
        pattern=pattern,
        is_regex=is_regex,
        target=target,
        sources=frozenset(sources) if sources else None,
        tiers=frozenset(tiers) if tiers else None,
        enabled=enabled,
        compiled=keyword_route._compile(pattern, is_regex, rule_id),
    )


@pytest.fixture
def kw_rules(monkeypatch: pytest.MonkeyPatch):
    """Inject the rule list without a store: the snapshot source returns the
    given rows and the cache is reset on teardown."""
    holder: list[KeywordRule] = []

    def _set(rules: list[KeywordRule]) -> None:
        holder[:] = rules
        keyword_route.reset_cache()

    monkeypatch.setattr(keyword_route, "_refresh_snapshot", lambda: list(holder))
    yield _set
    keyword_route.reset_cache()


def _stub_claude_host(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host where claude rungs pass the availability check: a stub binary
    on PATH plus a resolvable OAuth token."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("PRECIS_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")


def _fake_p(calls: dict[str, Any] | None = None):
    def _run(prompt: str, **kwargs: Any) -> ClaudePResult:
        if calls is not None:
            calls["model"] = kwargs.get("model")
        return ClaudePResult(
            data={"ok": True}, raw_stdout='{"ok": true}', cost_usd=0.01
        )

    return _run


def _fake_agent(calls: dict[str, Any] | None = None):
    def _run(prompt: str, **kwargs: Any) -> AgentResult:
        if calls is not None:
            calls["model"] = kwargs.get("model")
        return AgentResult(
            final_text="agent out", cost_usd=0.5, duration_s=1.0, turns_used=2
        )

    return _run


# ── matching (keyword_route.match) ─────────────────────────────────────


def test_match_keyword_whole_word_case(kw_rules) -> None:
    kw_rules([_rule("1", "opus", {"model": "claude-opus-4-8"})])
    assert keyword_route.match("please ask Opus about this", source=None, tier=None)
    assert (
        keyword_route.match("the opuses were catalogued", source=None, tier=None)
        is None
    )


def test_match_first_wins(kw_rules) -> None:
    kw_rules(
        [
            _rule("1", "opus", {"model": "claude-opus-4-8"}),
            _rule("2", "opus", {"model": "claude-fable-5"}),
        ]
    )
    hit = keyword_route.match("route this to opus", source=None, tier=None)
    assert hit is not None and hit.rule_id == "1"


def test_match_scope_source_and_tier(kw_rules) -> None:
    kw_rules(
        [
            _rule(
                "1",
                "deep",
                {"model": "claude-opus-4-8"},
                sources={"dream"},
                tiers={"big"},
            ),
            _rule("2", "deep", {"model": "claude-fable-5"}),
        ]
    )
    # Out of scope for rule 1 → rule 2 fires.
    hit = keyword_route.match("go deep", source="briefing", tier="big")
    assert hit is not None and hit.rule_id == "2"
    # In scope for rule 1 → first match.
    hit = keyword_route.match("go deep", source="dream", tier="big")
    assert hit is not None and hit.rule_id == "1"
    # Tier out of scope → rule 2.
    hit = keyword_route.match("go deep", source="dream", tier="small")
    assert hit is not None and hit.rule_id == "2"


def test_match_disabled_and_bad_pattern_skipped(kw_rules) -> None:
    bad = _rule("1", "(((((", {"model": "claude-opus-4-8"}, is_regex=True)
    assert bad.compiled is None  # rejected at compile, not at match time
    kw_rules(
        [
            _rule("2", "opus", {"model": "x"}, enabled=False),
            bad,
            _rule("3", "opus", {"model": "claude-fable-5"}),
        ]
    )
    hit = keyword_route.match("ask opus", source=None, tier=None)
    assert hit is not None and hit.rule_id == "3"


def test_match_regex_rule(kw_rules) -> None:
    kw_rules(
        [
            _rule(
                "1", r"run on (opus|fable)", {"model": "claude-opus-4-8"}, is_regex=True
            )
        ]
    )
    hit = keyword_route.match("please run on fable", source=None, tier=None)
    assert hit is not None and hit.rule_id == "1"


def test_match_never_raises_on_matcher_fault(kw_rules, monkeypatch) -> None:
    """A fault inside the matcher degrades to no-match — dispatch must not
    inherit a routing-layer error."""
    kw_rules([_rule("1", "opus", {"model": "x"})])
    monkeypatch.setattr(
        keyword_route, "rules", lambda: (_ for _ in ()).throw(RuntimeError("db"))
    )
    assert keyword_route.match("ask opus", source=None, tier=None) is None


def test_request_text_covers_system_and_messages() -> None:
    req = LlmRequest(
        tier=Tier.BIG,
        prompt="user prompt",
        system_prompt="the system words",
        messages=[{"role": "user", "content": "msg text"}],
    )
    text = keyword_route.request_text(req)
    assert "user prompt" in text
    assert "the system words" in text
    assert "msg text" in text


# ── route() seam ───────────────────────────────────────────────────────


def test_route_keyword_retires_to_rule_model(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules, caplog
) -> None:
    """A matched bare-model target dispatches to that model on the claude
    wire, and the rule id lands on the logged request."""
    _stub_claude_host(tmp_path, monkeypatch)
    kw_rules([_rule("7", "opus", {"model": "claude-opus-4-8"})])
    calls: dict[str, Any] = {}
    monkeypatch.setattr(router, "call_claude_p", _fake_p(calls))

    seen_reqs: list[LlmRequest] = []

    def _capture(req: LlmRequest, result: Any, **kw: Any) -> None:
        seen_reqs.append(req)

    monkeypatch.setattr(router, "_record_dispatch", _capture)

    out = route(LlmRequest(tier=Tier.MEDIUM, prompt="please ask opus"))

    assert out.error is None
    assert calls["model"] == "claude-opus-4-8"
    assert seen_reqs and seen_reqs[0].keyword_rule == "7"


def test_route_keyword_no_match_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules
) -> None:
    """No rule hit ⇒ today's resolution, byte-for-byte: the MEDIUM tier's
    claude_p model."""
    _stub_claude_host(tmp_path, monkeypatch)
    kw_rules([_rule("7", "zzz-never", {"model": "claude-opus-4-8"})])
    calls: dict[str, Any] = {}
    monkeypatch.setattr(router, "call_claude_p", _fake_p(calls))
    monkeypatch.delenv("PRECIS_MODEL_HAIKU", raising=False)

    out = route(LlmRequest(tier=Tier.MEDIUM, prompt="judge this"))

    assert out.error is None
    assert calls["model"] == "claude-haiku-4-5-20251001"


def test_route_keyword_chain_target(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules
) -> None:
    """A chain target rides its own rungs — here a single local rung."""
    kw_rules(
        [
            _rule(
                "9",
                "localize",
                {
                    "chain": [
                        {
                            "model": "qwen-local",
                            "transport": "local",
                            "placement": "local",
                        }
                    ]
                },
            )
        ]
    )

    class _FakeClient:
        def __init__(self, config: object) -> None:
            self.model = getattr(config, "model", None)

        def complete(self, messages: list[dict[str, str]]) -> Any:
            return SimpleNamespace(text="local answered", total_tokens=3)

    monkeypatch.setattr("precis.workers.llm_summarize.LlmClient", _FakeClient)

    out = route(LlmRequest(tier=Tier.BIG, prompt="localize this"))

    assert out.error is None
    assert out.text == "local answered"
    assert out.placement == "local"


def test_route_keyword_unavailable_target_falls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules, caplog
) -> None:
    """A rule whose target can't run on this host (an OSS id with no
    base-url or serving) is a logged miss; the call runs the normal path."""
    _stub_claude_host(tmp_path, monkeypatch)
    kw_rules([_rule("7", "oss", {"model": "unhosted-oss-model"})])
    calls: dict[str, Any] = {}
    monkeypatch.setattr(router, "call_claude_p", _fake_p(calls))
    monkeypatch.delenv("PRECIS_MODEL_HAIKU", raising=False)

    with caplog.at_level(logging.WARNING):
        out = route(LlmRequest(tier=Tier.MEDIUM, prompt="use oss please"))

    assert out.error is None
    assert calls["model"] == "claude-haiku-4-5-20251001"
    assert any("rule 7 matched but is unusable" in r.message for r in caplog.records)


def test_route_keyword_over_limiter_falls_back(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules, caplog
) -> None:
    """A keyword retier onto a tripped resource is a miss — the call keeps
    its own tier's gate instead of inheriting the rule's pause."""
    _stub_claude_host(tmp_path, monkeypatch)
    kw_rules([_rule("7", "cheap", {"tier": "medium"})])
    calls: dict[str, Any] = {}
    monkeypatch.setattr(router, "call_claude_p", _fake_p(calls))
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)

    def _gate(tier: Tier, **kw: Any) -> str | None:
        return "budget: cap reached" if tier is Tier.MEDIUM else None

    monkeypatch.setattr("precis.budget.breaker.gate_tier", _gate)

    with caplog.at_level(logging.WARNING):
        out = route(LlmRequest(tier=Tier.BIG, prompt="go cheap"))

    assert out.error is None
    assert calls["model"] == "claude-sonnet-5"
    assert any("over its limiter" in r.message for r in caplog.records)


def test_route_keyword_never_bypasses_tools(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules
) -> None:
    """A tool-less keyword chain on a tools-needed call is filtered to
    empty → miss → normal routing keeps the tool-carrying transport."""
    _stub_claude_host(tmp_path, monkeypatch)
    kw_rules(
        [
            _rule(
                "7",
                "opus",
                {"chain": [{"model": "claude-opus-4-8", "transport": "claude_p"}]},
            )
        ]
    )
    calls: dict[str, Any] = {}
    monkeypatch.setattr(router, "call_claude_agent", _fake_agent(calls))
    monkeypatch.delenv("PRECIS_MODEL_SONNET", raising=False)

    out = route(LlmRequest(tier=Tier.BIG, prompt="ask opus", tools_needed=True))

    assert out.error is None
    assert calls["model"] == "claude-sonnet-5"


def test_route_keyword_does_not_steal_model_pin(
    tmp_path, monkeypatch: pytest.MonkeyPatch, kw_rules
) -> None:
    """A call-site ``req.model`` pin is a correctness constraint — a
    matching rule must not retarget it."""
    _stub_claude_host(tmp_path, monkeypatch)
    kw_rules([_rule("7", "opus", {"model": "claude-opus-4-8"})])
    calls: dict[str, Any] = {}
    monkeypatch.setattr(router, "call_claude_p", _fake_p(calls))

    out = route(LlmRequest(tier=Tier.MEDIUM, prompt="ask opus", model="pinned-model"))

    assert out.error is None
    assert calls["model"] == "pinned-model"


def test_route_features_stamp_keyword_rule() -> None:
    req = LlmRequest(tier=Tier.BIG, prompt="x", keyword_rule="7")
    feats = router._route_features(req)
    assert feats["keyword_rule"] == "7"
    assert "keyword_rule" not in router._route_features(
        LlmRequest(tier=Tier.BIG, prompt="x")
    )
