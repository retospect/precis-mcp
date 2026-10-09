"""``conflict_sweep`` job_type glue — registration and the dispatch
contract over :func:`precis.workers.conflict_search.sweep_one_hub`.

The mechanism is tested in ``test_conflict_search.py``; this module only
asserts the glue stays thin: it reads ``params``, calls the one-hub door
once, and turns its outcome into the summary / meta / failure a caller
reads. The LLM seams are monkeypatched module globals (never a live
model); the embedder is the deterministic mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import mint_hub
from precis.workers import conflict_search
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types.conflict_sweep import SPEC, _dispatch
from tests.workers._helpers import make_mock_bge_m3


class _Ctx:
    """A minimal ``DispatchContext`` stand-in (frozen, all-callable)."""

    def __init__(self, store: Any, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = 1
        self.title = "job"
        self.meta: dict[str, Any] = {"params": params}
        self.events: list[tuple[str, str]] = []
        self.failure: str | None = None
        self.failure_class: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.events.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta.update(fields)

    def record_failure(self, reason: str, **kw: Any) -> None:
        self.failure = reason
        self.failure_class = kw.get("failure_class")

    def is_cancel_requested(self) -> bool:
        return False

    @property
    def summary(self) -> str:
        return next(t for k, t in self.events if k == "job_summary")


@pytest.fixture
def mock_embedder() -> Any:
    embedder = make_mock_bge_m3()
    with patch(
        "precis.workers.job_types.conflict_sweep._build_embedder",
        return_value=embedder,
    ):
        yield embedder


def test_registered_in_the_job_type_registry() -> None:
    spec = get_job_type("conflict_sweep")
    assert spec is not None and spec is SPEC
    assert "conflict_sweep" in known_job_types()
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert spec.dispatch is not None
    assert SPEC.params_schema["required"] == ["hub_id"]
    assert SPEC.params_schema["additionalProperties"] is False


def test_missing_hub_id_is_a_clean_failure() -> None:
    ctx = _Ctx(store=None, params={})
    _dispatch(ctx, SPEC)
    assert ctx.failure is not None and "hub_id" in ctx.failure


def test_dispatch_sweeps_the_named_hub_and_summarises(
    store: Any, mock_embedder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="Job-swept claim.", scope={}))
    monkeypatch.setattr(
        conflict_search, "negate_claim", lambda s, sc: {"paraphrases": []}
    )
    monkeypatch.setattr(
        conflict_search,
        "_verify_support_with_caveats",
        lambda **kw: {"supports": "no", "contradicts": False, "caveats": []},
    )
    ctx = _Ctx(store=store, params={"hub_id": hub})
    _dispatch(ctx, SPEC)
    assert ctx.failure is None
    assert ctx.meta["swept"] is True
    assert "candidate(s) verified" in ctx.summary
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'conflict_search'->>'version' FROM refs WHERE ref_id = %s",
            (hub,),
        ).fetchone()
    assert row is not None and int(row[0]) == conflict_search.CONFLICT_SEARCH_VERSION

    # Already swept at the current version: a second job is a no-op success.
    ctx2 = _Ctx(store=store, params={"hub_id": hub})
    _dispatch(ctx2, SPEC)
    assert ctx2.failure is None
    assert ctx2.meta == {"params": {"hub_id": hub}, "swept": False, "skipped": True}
    assert "not claimable" in ctx2.summary

    # ``refresh`` sweeps anyway and says so.
    ctx3 = _Ctx(store=store, params={"hub_id": hub, "refresh": True})
    _dispatch(ctx3, SPEC)
    assert ctx3.failure is None and ctx3.meta["swept"] is True
    assert ctx3.summary.endswith("[refresh]")


def test_negate_outage_is_an_infra_failure_with_nothing_stamped(
    store: Any, mock_embedder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence="Outage claim.", scope={}))
    monkeypatch.setattr(conflict_search, "negate_claim", lambda s, sc: None)
    ctx = _Ctx(store=store, params={"hub_id": hub})
    _dispatch(ctx, SPEC)
    assert ctx.failure is not None and "negate LLM unavailable" in ctx.failure
    assert ctx.failure_class == "infra"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta ? 'conflict_search' FROM refs WHERE ref_id = %s", (hub,)
        ).fetchone()
    assert row is not None and row[0] is False


def test_no_embedder_is_an_infra_failure(store: Any) -> None:
    ctx = _Ctx(store=store, params={"hub_id": 1})
    with patch(
        "precis.workers.job_types.conflict_sweep._build_embedder",
        side_effect=ValueError("no PRECIS_EMBEDDER_URL"),
    ):
        _dispatch(ctx, SPEC)
    assert ctx.failure is not None and "no embedder" in ctx.failure
    assert ctx.failure_class == "infra"
