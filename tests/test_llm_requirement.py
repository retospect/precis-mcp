"""Tests for the task→requirement judge — the agent surface (llm-catalog slice 5,
``llm-catalog`` (git-only)).

Covers: strict classifier validation, cache isolation and expiry,
tolerant JSON extraction, subscription refusal, and the
`choose_model` loop (judge → requirement → deterministic policy). The judge is
injected so no real LLM call is made. Real PG for the policy half.
"""

from __future__ import annotations

from typing import Any

import pytest


def output(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "axis": "code",
        "min_ordinal": 4,
        "needs_tools": True,
        "needs_structured": False,
        "max_input": 120000,
        "difficulty": 3,
        "domain": "code",
        "consequence": "medium",
    }
    return data | changes


@pytest.mark.parametrize(
    "transport", ["claude_p", "claude_agent", "codex_exec", "openai_compat"]
)
@pytest.mark.parametrize("op_chain", [False, True])
def test_judge_refuses_subscription_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    op_chain: bool,
) -> None:
    from types import SimpleNamespace

    from precis.utils.llm import operations, router

    rung = router.Rung(
        router.Transport(transport),
        profile="subscription" if transport == "openai_compat" else None,
    )
    monkeypatch.setattr(router, "resolve_backend", lambda: router.Backend.ANTHROPIC)
    monkeypatch.setattr(router, "resolve_model", lambda *a, **kw: "fallback")
    monkeypatch.setattr(router._keyword_route, "match_request", lambda req: None)
    monkeypatch.setattr(operations, "resolve_op", lambda source: None)
    monkeypatch.setattr(
        operations, "resolve_op_chain", lambda *a, **kw: [rung] if op_chain else None
    )
    monkeypatch.setattr(router, "resolve_chain", lambda *a, **kw: [rung])
    monkeypatch.setattr(
        router, "resolve_profile", lambda name: SimpleNamespace(kind="subscription")
    )

    def forbidden_provider(*args: Any, **kwargs: Any) -> None:
        pytest.fail("subscription provider must never run")

    monkeypatch.setattr(router, "provider_for", forbidden_provider)
    result = router.route(
        router.LlmRequest(
            tier=router.Tier.MEDIUM,
            prompt="classify",
            source="llm:requirement-judge",
            tools_needed=False,
            log_call=False,
        )
    )
    assert result.reason_class == "subscription_op_refused"
    assert result.error is not None and "refuses subscription" in result.error


@pytest.mark.parametrize("local_down", [False, True])
def test_judge_operation_chain_local_then_cloud(
    monkeypatch: pytest.MonkeyPatch,
    local_down: bool,
) -> None:
    from precis.utils.llm import live_config, local_serving, operations, router

    chain: list[router.Rung] = [
        router.Rung(
            router.Transport.LOCAL,
            model="gpt-oss-120b",
            label="local",
            max_tokens=2048,
            reasoning_effort="low",
            timeout_s=300,
        ),
        router.Rung(
            router.Transport.OPENAI_COMPAT, model="z-ai/glm-4.7-flash", label="cloud"
        ),
    ]
    monkeypatch.setattr(router, "resolve_backend", lambda: router.Backend.ANTHROPIC)
    monkeypatch.setattr(router, "resolve_model", lambda *a, **kw: "unused")
    monkeypatch.setattr(operations, "resolve_op", lambda source: None)
    monkeypatch.setattr(operations, "resolve_op_chain", lambda *a, **kw: chain)
    monkeypatch.setattr(
        router, "_drop_unavailable_rungs", lambda ladder, model: (ladder, [])
    )
    monkeypatch.setattr(
        router, "_skip_unserved_local_rung", lambda ladder, model: ladder
    )
    monkeypatch.setattr(live_config, "cloud_enabled", lambda: True)
    monkeypatch.setattr(local_serving, "acquire", lambda model: None)
    monkeypatch.setattr("precis.budget.breaker.gate_tier", lambda *a, **kw: None)
    monkeypatch.setattr("precis.utils.llm.admit.check_dispatch", lambda *a, **kw: None)
    calls: list[str] = []

    class Provider:
        def run(self, req: router.LlmRequest, *, model: str) -> router.LlmResult:
            calls.append(model)
            if model == "gpt-oss-120b":
                assert req.max_tokens == 2048
                assert req.timeout_s == 300
                assert req.local_reasoning_effort == "low"
            return router.LlmResult(
                text="{}",
                cost_usd=0.0,
                turns_used=1,
                model=model,
                tier=req.tier,
                error="connection failed" if local_down and len(calls) == 1 else None,
            )

    monkeypatch.setattr(router, "provider_for", lambda *a, **kw: Provider())
    result = router.route(
        router.LlmRequest(
            tier=router.Tier.MEDIUM,
            source="llm:requirement-judge",
            prompt="classify",
            tools_needed=False,
            log_call=False,
        )
    )
    assert result.error is None
    assert calls == (
        ["gpt-oss-120b", "z-ai/glm-4.7-flash"] if local_down else ["gpt-oss-120b"]
    )
    assert result.placement == ("cloud" if local_down else "local")


def _judge_chain(monkeypatch: pytest.MonkeyPatch, chain: list[Any]) -> None:
    """Make ``chain`` the judge's resolved ladder. The keyword route is left
    alone: it skips ``llm:requirement-judge`` itself."""
    from precis.utils.llm import operations, router

    monkeypatch.setattr(router, "resolve_backend", lambda: router.Backend.ANTHROPIC)
    monkeypatch.setattr(router, "resolve_model", lambda *a, **kw: "unused")
    monkeypatch.setattr(operations, "resolve_op", lambda source: None)
    monkeypatch.setattr(operations, "resolve_op_chain", lambda *a, **kw: chain)
    monkeypatch.setattr(router, "resolve_chain", lambda *a, **kw: chain)


def test_judge_refuses_a_chain_that_mixes_local_and_claude_p(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.utils.llm import router

    _judge_chain(
        monkeypatch,
        [
            router.Rung(router.Transport.LOCAL, model="gpt-oss-120b", label="local"),
            router.Rung(router.Transport.CLAUDE_P, label="claude"),
        ],
    )

    def forbidden_provider(*args: Any, **kwargs: Any) -> None:
        pytest.fail("a refused chain runs no rung — not even its local one")

    monkeypatch.setattr(router, "provider_for", forbidden_provider)
    result = router.route(
        router.LlmRequest(
            tier=router.Tier.MEDIUM,
            prompt="classify",
            source="llm:requirement-judge",
            tools_needed=False,
            log_call=False,
        )
    )
    assert result.reason_class == "subscription_op_refused"
    assert result.error is not None and "refuses subscription" in result.error


def test_judge_metered_profile_rung_passes_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.utils.llm import live_config, local_serving, router

    profile = router.ProviderProfile(
        name="metered-cloud",
        base_url="https://llm.example.test/v1",
        api_key_ref=None,
        kind="metered",
        cap=None,
        adapter=None,
        gate=None,
        ops=None,
        thinking_required=None,
    )
    _judge_chain(
        monkeypatch,
        [
            router.Rung(router.Transport.LOCAL, model="gpt-oss-120b", label="local"),
            router.Rung(
                router.Transport.OPENAI_COMPAT,
                model="z-ai/glm-4.7-flash",
                label="cloud",
                profile=profile.name,
            ),
        ],
    )
    monkeypatch.setattr(
        router,
        "resolve_profile",
        lambda name: profile if name == profile.name else None,
    )
    monkeypatch.setattr(
        router, "_drop_unavailable_rungs", lambda ladder, model: (ladder, [])
    )
    monkeypatch.setattr(
        router, "_skip_unserved_local_rung", lambda ladder, model: ladder
    )
    monkeypatch.setattr(live_config, "cloud_enabled", lambda: True)
    monkeypatch.setattr(local_serving, "acquire", lambda model: None)
    monkeypatch.setattr("precis.budget.breaker.gate_tier", lambda *a, **kw: None)
    monkeypatch.setattr("precis.utils.llm.admit.check_dispatch", lambda *a, **kw: None)
    calls: list[str] = []

    class Provider:
        def run(self, req: router.LlmRequest, *, model: str) -> router.LlmResult:
            calls.append(model)
            return router.LlmResult(
                text="{}",
                cost_usd=0.0,
                turns_used=1,
                model=model,
                tier=req.tier,
                error="connection failed" if len(calls) == 1 else None,
            )

    monkeypatch.setattr(router, "provider_for", lambda *a, **kw: Provider())
    result = router.route(
        router.LlmRequest(
            tier=router.Tier.MEDIUM,
            source="llm:requirement-judge",
            prompt="classify",
            tools_needed=False,
            log_call=False,
        )
    )
    # A metered profile is not a subscription: the guard lets the chain walk on
    # to that rung once the local one has failed.
    assert result.reason_class != "subscription_op_refused"
    assert result.error is None
    assert calls == ["gpt-oss-120b", "z-ai/glm-4.7-flash"]
    assert result.placement == "cloud"


@pytest.fixture
def clean_catalog(store: Any) -> Any:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() "
            "WHERE kind = 'llm' AND retired_at IS NULL"
        )
    return store


class TestInferRequirement:
    def test_maps_judge_json_to_requirement(self) -> None:
        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        def judge(_t: str) -> dict[str, Any]:
            return output()

        req = infer_requirement("refactor auth", tier_floor=Tier.FRONTIER, judge=judge)
        assert req.axis == "code" and req.min_ordinal == 4
        assert req.needs_tools is True and req.max_input == 120000

    @pytest.mark.parametrize(
        "changes",
        [
            {"axis": "vibes"},
            {"difficulty": 0},
            {"difficulty": 6},
            {"difficulty": True},
            {"consequence": "catastrophic"},
            {"model": "opus"},
            {"max_input": -1},
            {"needs_tools": "false"},
        ],
    )
    def test_rejects_invalid_schema(self, changes: dict[str, Any]) -> None:
        from pydantic import ValidationError

        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        def judge(_t: str) -> dict[str, Any]:
            return output(**changes)

        with pytest.raises(ValidationError):
            infer_requirement("do a thing", tier_floor=Tier.BIG, judge=judge)

    def test_empty_judge_reply_is_refused(self) -> None:
        from pydantic import ValidationError

        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        with pytest.raises(ValidationError):
            infer_requirement("x", tier_floor=Tier.BIG, judge=lambda _t: {})

    def test_domain_outside_the_fixed_set_is_rejected(self) -> None:
        from pydantic import ValidationError

        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        with pytest.raises(ValidationError, match="domain"):
            infer_requirement(
                "do a thing",
                tier_floor=Tier.BIG,
                judge=lambda _t: output(domain="software"),
            )

    def test_domain_set_is_the_fixed_eight_and_the_prompt_lists_it(self) -> None:
        from typing import get_args

        from precis.utils.llm.requirement import _PROMPT, ClassifierOutput, Domain

        eight = (
            "code",
            "chemistry",
            "geometry",
            "pcb",
            "text",
            "graph",
            "research",
            "other",
        )
        assert get_args(Domain) == eight
        for domain in eight:
            assert (
                ClassifierOutput.model_validate(output(domain=domain)).domain == domain
            )
        assert "<" + "|".join(eight) + ">" in _PROMPT

    def test_missing_domain_defaults_to_other(self) -> None:
        from precis.utils.llm.requirement import infer_requirement
        from precis.utils.llm.router import Tier

        payload = output()
        del payload["domain"]
        req = infer_requirement("x", tier_floor=Tier.BIG, judge=lambda _t: payload)
        assert req.domain == "other"

    def test_cache_hit_scope_isolation_and_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        calls: list[str] = []
        clock: list[float] = [0.0]

        def judge(task: str) -> dict[str, Any]:
            calls.append(task)
            return output()

        monkeypatch.setattr(module, "_cache", {})
        monkeypatch.setattr(module, "_default_judge", judge)
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
        first = module.infer_requirement("task", tier_floor=Tier.BIG, cache_scope="a")
        second = module.infer_requirement(
            "task", tier_floor=Tier.MEDIUM, cache_scope="a"
        )
        assert len(calls) == 1
        assert first.difficulty == second.difficulty == 3
        assert second.domain == "code" and second.consequence == "medium"
        assert second.tier_floor == Tier.MEDIUM
        module.infer_requirement("task", tier_floor=Tier.BIG, cache_scope="b")
        module.infer_requirement("changed", tier_floor=Tier.BIG, cache_scope="a")
        assert len(calls) == 3
        clock[0] = 86400.0
        module.infer_requirement("task", tier_floor=Tier.BIG, cache_scope="a")
        assert len(calls) == 4


class TestClassificationCache:
    """The in-process cache of default-judge results (``requirement._cache``)."""

    @pytest.fixture(autouse=True)
    def fresh_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.utils.llm import requirement as module

        monkeypatch.setattr(module, "_cache", {})

    def test_injected_judge_bypasses_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        calls: list[str] = []

        def default(task: str) -> dict[str, Any]:
            calls.append("default")
            return output(difficulty=2)

        monkeypatch.setattr(module, "_default_judge", default)
        # Nothing cached yet: two different injected judges each answer for
        # themselves and leave the cache empty.
        a = module.infer_requirement(
            "task", tier_floor=Tier.BIG, judge=lambda _t: output(difficulty=4)
        )
        b = module.infer_requirement(
            "task", tier_floor=Tier.BIG, judge=lambda _t: output(difficulty=5)
        )
        assert (a.difficulty, b.difficulty) == (4, 5)
        assert module._cache == {} and calls == []
        # A default call primes the cache; an injected judge still is not served
        # from it, and what it returns does not replace the cached entry.
        assert module.infer_requirement("task", tier_floor=Tier.BIG).difficulty == 2
        c = module.infer_requirement(
            "task", tier_floor=Tier.BIG, judge=lambda _t: output(difficulty=3)
        )
        assert c.difficulty == 3
        assert module.infer_requirement("task", tier_floor=Tier.BIG).difficulty == 2
        assert calls == ["default"]

    @pytest.mark.parametrize("failure", ["judge_raises", "schema_miss"])
    def test_judge_failure_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        calls: list[str] = []

        def judge(task: str) -> dict[str, Any]:
            calls.append(task)
            if len(calls) == 1:
                if failure == "judge_raises":
                    raise ValueError("requirement judge failed: boom")
                return {}
            return output()

        monkeypatch.setattr(module, "_default_judge", judge)
        # pydantic's ValidationError is a ValueError, so one class covers both.
        with pytest.raises(ValueError):
            module.infer_requirement("task", tier_floor=Tier.BIG)
        assert module._cache == {}
        req = module.infer_requirement("task", tier_floor=Tier.BIG)
        assert len(calls) == 2 and req.axis == "code"
        assert len(module._cache) == 1

    def test_features_key_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        calls: list[str] = []

        def judge(task: str) -> dict[str, Any]:
            calls.append(task)
            return output()

        monkeypatch.setattr(module, "_default_judge", judge)
        features = {
            "prompt_chars": "1k-4k",
            "files": 3,
            "context": "32k-128k",
            "tool_steps": 5,
            "validators": ["pytest"],
        }
        module.infer_requirement(
            "refactor auth", tier_floor=Tier.BIG, features=features
        )
        # Same features, other task text and key order: a hit.
        module.infer_requirement(
            "tidy the login module",
            tier_floor=Tier.BIG,
            features=dict(reversed(features.items())),
        )
        assert calls == ["refactor auth"]
        # Other features: a miss.
        module.infer_requirement(
            "refactor auth", tier_floor=Tier.BIG, features=features | {"files": 4}
        )
        assert len(calls) == 2
        # The scope still namespaces featured entries.
        module.infer_requirement(
            "refactor auth", tier_floor=Tier.BIG, features=features, cache_scope="x"
        )
        assert len(calls) == 3
        # Without features (or with an empty mapping) the task text keys it.
        module.infer_requirement("refactor auth", tier_floor=Tier.BIG)
        module.infer_requirement("refactor auth", tier_floor=Tier.BIG, features={})
        assert len(calls) == 4
        module.infer_requirement("other text", tier_floor=Tier.BIG, features={})
        assert len(calls) == 5

    def test_cache_is_capped_and_drops_the_oldest_expiring_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        calls: list[str] = []
        clock: list[float] = [0.0]

        def judge(task: str) -> dict[str, Any]:
            calls.append(task)
            return output()

        monkeypatch.setattr(module, "_default_judge", judge)
        monkeypatch.setattr(module, "monotonic", lambda: clock[0])
        monkeypatch.setattr(module, "_CACHE_MAX", 3)
        for tick, task in enumerate(["a", "b", "c", "d"]):
            clock[0] = float(tick)
            module.infer_requirement(task, tier_floor=Tier.BIG)
        assert len(module._cache) == 3
        for task in ["b", "c", "d"]:  # all still cached ...
            module.infer_requirement(task, tier_floor=Tier.BIG)
        assert calls == ["a", "b", "c", "d"]
        module.infer_requirement("a", tier_floor=Tier.BIG)  # ... "a" made room
        assert calls == ["a", "b", "c", "d", "a"]
        assert len(module._cache) == 3

    def test_concurrent_calls_do_not_corrupt_the_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        workers, per_worker = 8, 100
        calls: list[str] = []

        def judge(task: str) -> dict[str, Any]:
            calls.append(task)
            time.sleep(0)  # hand the GIL over between the miss and the insert
            return output()

        monkeypatch.setattr(module, "_default_judge", judge)
        monkeypatch.setattr(module, "_CACHE_TTL", 0)  # every insert sweeps the rest
        # Live entries make each sweep walk a long dict, widening the race window.
        live = module.ClassifierOutput.model_validate(output())
        seeds = [("seed", str(i)) for i in range(2000)]
        module._cache.update({key: (float("inf"), live) for key in seeds})
        start = threading.Barrier(workers)

        def work(n: int) -> None:
            start.wait(timeout=30)
            for i in range(per_worker):
                module.infer_requirement(f"task {n}-{i}", tier_floor=Tier.BIG)

        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)  # interleave the threads as often as possible
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for future in [pool.submit(work, n) for n in range(workers)]:
                    future.result()  # re-raises a worker's KeyError / RuntimeError
        finally:
            sys.setswitchinterval(interval)
        assert len(calls) == workers * per_worker  # TTL 0: every call was a miss
        assert all(key in module._cache for key in seeds)  # live entries survive
        assert len(module._cache) <= module._CACHE_MAX


class TestExtractJson:
    def test_pulls_json_from_prose(self) -> None:
        from precis.utils.llm.json_reply import extract_json_object

        raw = (
            'Sure! Here is the requirement:\n{"axis": "code", "min_ordinal": 3}\nDone.'
        )
        assert extract_json_object(raw) == {"axis": "code", "min_ordinal": 3}

    def test_garbage_is_none(self) -> None:
        from precis.utils.llm.json_reply import extract_json_object

        assert extract_json_object("no json here") is None


class TestChooseModel:
    def test_judge_then_policy_picks(self, clean_catalog: Any) -> None:
        from precis import llm_catalog
        from precis.utils.llm.requirement import choose_model
        from precis.utils.llm.router import Tier

        llm_catalog.upsert_card(
            clean_catalog,
            model_id="coder",
            text="A strong coder.",
            tier_floor="big",
            offerings=[{"transport": "claude_agent", "price_in": 3.0}],
            capability={"code": 5},
        )

        def judge(_t: str) -> dict[str, Any]:
            return output()

        req, sel = choose_model(
            clean_catalog, "write a parser", tier_floor=Tier.BIG, judge=judge
        )
        assert req.axis == "code"
        assert sel.model == "coder" and sel.from_catalog is True

    def test_degrades_to_floor_when_no_card(self, clean_catalog: Any) -> None:
        from precis.utils.llm.requirement import choose_model
        from precis.utils.llm.router import Tier, resolve_model

        req, sel = choose_model(
            clean_catalog,
            "summarize this",
            tier_floor=Tier.MEDIUM,
            judge=lambda _t: output(axis="summarize-extract", min_ordinal=3),
        )
        assert sel.from_catalog is False
        assert sel.model == resolve_model(Tier.MEDIUM)

    def test_features_pass_through_to_the_cache_key(
        self, clean_catalog: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.utils.llm import requirement as module
        from precis.utils.llm.router import Tier

        calls: list[str] = []

        def judge(task: str) -> dict[str, Any]:
            calls.append(task)
            return output()

        monkeypatch.setattr(module, "_cache", {})
        monkeypatch.setattr(module, "_default_judge", judge)
        features = {"files": 2, "context": "32k-128k"}
        for task in ("write a parser", "write a lexer"):
            module.choose_model(
                clean_catalog, task, tier_floor=Tier.BIG, features=features
            )
        assert calls == ["write a parser"]


class TestChooseCommand:
    """``precis llm choose`` — the slice-5 loop's proof on the command line."""

    @staticmethod
    def run_choose(store: Any, tier: str = "big") -> None:
        import argparse

        from precis.cli.llm import _cmd_choose

        _cmd_choose(store, argparse.Namespace(task="refactor auth", tier=tier))

    def test_prints_difficulty_domain_and_consequence(
        self,
        clean_catalog: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from precis.utils.llm import requirement as module

        monkeypatch.setattr(module, "_cache", {})
        monkeypatch.setattr(module, "_default_judge", lambda _t: output())
        self.run_choose(clean_catalog)
        out = capsys.readouterr().out
        assert (
            "requirement: code≥4, tools=True, window=120000, "
            "difficulty=3, domain=code, consequence=medium"
        ) in out
        assert "model: " in out

    @pytest.mark.parametrize(
        ("failure", "fragment"),
        [("judge_raises", "requirement judge failed"), ("schema_miss", "domain")],
    )
    def test_a_refused_judge_is_a_message_not_a_traceback(
        self,
        clean_catalog: Any,
        monkeypatch: pytest.MonkeyPatch,
        failure: str,
        fragment: str,
    ) -> None:
        from precis.utils.llm import requirement as module

        def judge(_t: str) -> dict[str, Any]:
            if failure == "judge_raises":
                raise ValueError("requirement judge failed: no subscription rung")
            return output(domain="software")

        monkeypatch.setattr(module, "_cache", {})
        monkeypatch.setattr(module, "_default_judge", judge)
        with pytest.raises(SystemExit) as exc:
            self.run_choose(clean_catalog)
        message = str(exc.value.code)
        assert message.startswith("llm choose: ") and fragment in message

    def test_an_unknown_tier_is_a_message_too(self, clean_catalog: Any) -> None:
        with pytest.raises(SystemExit) as exc:
            self.run_choose(clean_catalog, tier="bogus")
        assert str(exc.value.code).startswith("llm choose: ")
