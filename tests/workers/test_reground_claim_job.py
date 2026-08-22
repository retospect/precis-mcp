"""``reground_claim`` job_type glue — registration, scope resolution, and
the two modes' checkpoint/summary contract.

The mechanism itself is tested in ``test_hub_refine_reground.py``; this
module only asserts that the glue stays *thin*: it resolves a scope,
loops, checkpoints, isolates a hub failure, and surfaces the applier's
partial-failure counts in the summary a caller actually reads.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types.reground_claim import SPEC, _dispatch


class _Ctx:
    """A minimal ``DispatchContext`` stand-in — the dataclass is frozen and
    all-callable, so a duck-typed double keeps this test off the executor."""

    def __init__(self, store: Any, params: dict[str, Any], **meta: Any) -> None:
        self.store = store
        self.ref_id = 1
        self.title = "job"
        self.meta = {"params": params, **meta}
        self.events: list[tuple[str, str]] = []
        self.set_meta_calls: list[dict[str, Any]] = []
        self.failure: str | None = None
        self.cancelled = False

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.set_meta_calls.append(fields)
        self.meta.update(fields)

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason

    def is_cancel_requested(self) -> bool:
        return self.cancelled

    @property
    def summary(self) -> str:
        return next(t for k, t in self.events if k == "job_summary")


def test_registered_in_the_job_type_registry() -> None:
    spec = get_job_type("reground_claim")
    assert spec is not None and spec is SPEC
    assert "reground_claim" in known_job_types()
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    # Thin glue: a plugin ``dispatch``, no claude subprocess.
    assert spec.dispatch is not None


def test_params_schema_gates_are_all_optional_and_default_off() -> None:
    props = SPEC.params_schema["properties"]
    for gate in ("prune", "external", "authorize_retire", "repair"):
        assert props[gate]["type"] == "boolean"
    assert SPEC.params_schema["required"] == []
    assert SPEC.params_schema["additionalProperties"] is False


def test_empty_scope_is_a_clean_failure() -> None:
    ctx = _Ctx(store=None, params={})
    _dispatch(ctx, SPEC)
    assert ctx.failure is not None and "no hubs in scope" in ctx.failure


def test_hub_ids_override_drives_the_loop_and_checkpoints() -> None:
    ctx = _Ctx(store=object(), params={"hub_ids": [11, 22, 33]})

    class _Res:
        def __init__(self, hub: int) -> None:
            self.hub_ref_id = hub
            self.confirmed_adds = 1
            self.pruned = 1
            self.withheld = 0
            self.clean = True

        def as_dict(self) -> dict[str, Any]:
            return {"hub": self.hub_ref_id}

    with (
        patch(
            "precis.workers.job_types.reground_claim._build_embedder",
            return_value=object(),
        ),
        patch(
            "precis.workers.hub_refine.reground_one_hub",
            side_effect=lambda _s, h, **_kw: _Res(h),
        ) as mock_run,
    ):
        _dispatch(ctx, SPEC)

    assert mock_run.call_count == 3
    # Checkpointed after every hub, so a re-claim resumes.
    assert ctx.set_meta_calls[0] == {"done_hub_ids": [11]}
    assert ctx.meta["done_hub_ids"] == [11, 22, 33]
    assert "3 hub(s) regrounded" in ctx.summary
    assert "prune stage OFF" in ctx.summary


def test_done_hub_ids_are_skipped_on_resume() -> None:
    ctx = _Ctx(store=object(), params={"hub_ids": [11, 22]}, done_hub_ids=[11])
    with (
        patch(
            "precis.workers.job_types.reground_claim._build_embedder",
            return_value=object(),
        ),
        patch("precis.workers.hub_refine.reground_one_hub") as mock_run,
    ):
        _dispatch(ctx, SPEC)
    assert [c.args[1] for c in mock_run.call_args_list] == [22]


def test_one_hubs_failure_is_isolated_and_surfaced() -> None:
    """A hub that raises is recorded and skipped — and the hub AFTER it
    still runs (isolation, not abort-on-first-error)."""
    ctx = _Ctx(store=object(), params={"hub_ids": [11, 22]})

    class _Res:
        hub_ref_id = 22
        confirmed_adds = 0
        pruned = 0
        withheld = 0
        clean = True

        def as_dict(self) -> dict[str, Any]:
            return {"hub": 22}

    def _run(_store: Any, hub: int, **_kw: Any) -> Any:
        if hub == 11:
            raise RuntimeError("judge exploded")
        return _Res()

    with (
        patch(
            "precis.workers.job_types.reground_claim._build_embedder",
            return_value=object(),
        ),
        patch("precis.workers.hub_refine.reground_one_hub", side_effect=_run),
    ):
        _dispatch(ctx, SPEC)

    assert any("fi11: FAILED" in t for _k, t in ctx.events)
    assert "1 hub(s) regrounded, 1 failed" in ctx.summary
    assert ctx.failure is None  # one bad hub never fails the scope
    # Both are checkpointed — the failed one too, so a resume doesn't
    # re-pay for a hub that will just fail again.
    assert ctx.meta["done_hub_ids"] == [11, 22]


def test_prune_param_cannot_route_around_the_eval_interlock(
    monkeypatch: Any,
) -> None:
    """``params={'prune': true}`` is an opt-IN, not an override: the
    rubric-eval blocker is checked in code on this path too, and a
    requested-but-blocked prune is announced rather than silently
    downgraded."""
    monkeypatch.delenv("PRECIS_TAPROOT_REGROUND_PRUNE", raising=False)
    ctx = _Ctx(store=object(), params={"hub_ids": [11], "prune": True})
    seen: list[Any] = []

    with (
        patch(
            "precis.workers.job_types.reground_claim._build_embedder",
            return_value=object(),
        ),
        patch(
            "precis.workers.hub_refine.reground_one_hub",
            side_effect=_capture(seen),
        ),
    ):
        _dispatch(ctx, SPEC)

    assert seen and seen[0].prune is False
    assert any("interlock is closed" in t for _k, t in ctx.events)
    assert "prune stage OFF" in ctx.summary

    # With the interlock open the param takes effect.
    from precis.workers.hub_refine import PRUNE_INTERLOCK_TOKEN

    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_PRUNE", PRUNE_INTERLOCK_TOKEN)
    ctx2 = _Ctx(store=object(), params={"hub_ids": [11], "prune": True})
    seen.clear()
    with (
        patch(
            "precis.workers.job_types.reground_claim._build_embedder",
            return_value=object(),
        ),
        patch(
            "precis.workers.hub_refine.reground_one_hub",
            side_effect=_capture(seen),
        ),
    ):
        _dispatch(ctx2, SPEC)
    assert seen and seen[0].prune is True


class _NoopRes:
    hub_ref_id = 11
    confirmed_adds = 0
    pruned = 0
    withheld = 0
    clean = True

    def as_dict(self) -> dict[str, Any]:
        return {"hub": 11}


def _capture(sink: list[Any]) -> Any:
    """A ``reground_one_hub`` stand-in that records the ``RegroundConfig``
    it was handed — the only thing the gate assertions care about."""

    def _run(_store: Any, _hub: int, **kwargs: Any) -> _NoopRes:
        sink.append(kwargs["cfg"])
        return _NoopRes()

    return _run


def test_verify_mode_pays_no_llm_and_needs_no_embedder() -> None:
    ctx = _Ctx(store=object(), params={"hub_ids": [11, 22], "mode": "verify"})

    class _Diff:
        def __init__(self, clean: bool) -> None:
            self.has_intent = True
            self.clean = clean
            self.missing_adds = () if clean else (1,)
            self.stale_edges = ()

    with (
        patch(
            "precis.workers.job_types.reground_claim._build_embedder",
            side_effect=AssertionError("verify mode must not build an embedder"),
        ),
        patch(
            "precis.workers.hub_refine.verify_hub_intent",
            side_effect=lambda _s, h: _Diff(h == 11),
        ),
    ):
        _dispatch(ctx, SPEC)

    assert "1 clean, 1 with residue" in ctx.summary


def test_verify_repair_cannot_route_around_the_eval_interlock(
    monkeypatch: Any,
) -> None:
    """``mode='verify', repair=true`` is the same opt-IN as reground-mode
    ``prune``: ``repair_hub_intent``'s ``apply=True`` is a hard DELETE
    through ``taproot.hub.remove_evidence``, so the rubric-eval interlock
    is checked in code on this path too. With the interlock closed the
    repair still runs (reporting the diff) but with ``apply=False`` — no
    evidence edge is removed — and a job_event announces the block."""
    monkeypatch.delenv("PRECIS_TAPROOT_REGROUND_PRUNE", raising=False)
    ctx = _Ctx(
        store=object(),
        params={"hub_ids": [11], "mode": "verify", "repair": True},
    )

    class _Diff:
        has_intent = True
        clean = False
        missing_adds = (1,)
        stale_edges = (2,)

    calls: list[dict[str, Any]] = []

    def _fake_repair(_store: Any, hub_id: int, *, apply: bool) -> Any:
        calls.append({"hub_id": hub_id, "apply": apply})
        return _Diff()

    with patch("precis.workers.hub_refine.repair_hub_intent", side_effect=_fake_repair):
        _dispatch(ctx, SPEC)

    # apply=False — no edge removed — even though repair was requested.
    assert calls == [{"hub_id": 11, "apply": False}]
    assert any("interlock is closed" in t and "repair" in t for _k, t in ctx.events)
    assert ctx.failure is None  # the job still succeeds, reporting the diff
    assert "1 with residue" in ctx.summary
    assert "repair applied" not in ctx.summary

    # With the interlock open the param takes effect for real.
    from precis.workers.hub_refine import PRUNE_INTERLOCK_TOKEN

    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_PRUNE", PRUNE_INTERLOCK_TOKEN)
    ctx2 = _Ctx(
        store=object(),
        params={"hub_ids": [11], "mode": "verify", "repair": True},
    )
    calls.clear()
    with patch("precis.workers.hub_refine.repair_hub_intent", side_effect=_fake_repair):
        _dispatch(ctx2, SPEC)

    assert calls == [{"hub_id": 11, "apply": True}]
    assert not any("interlock is closed" in t for _k, t in ctx2.events)
    assert "repair applied" in ctx2.summary
