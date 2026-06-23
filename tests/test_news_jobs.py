"""Tests for the news_poll / briefing job_types and registry wiring.

The schedule-spawner propagation of job_type/params is exercised by the
DB-backed schedule suite; here we lock the offline pieces: registry
lookup, executor compatibility, and the plugin dispatchers' happy/empty/
failure paths against a fake DispatchContext.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.workers import briefing as briefing_pass
from precis.workers import news_poll as news_poll_pass
from precis.workers.job_types import briefing as briefing_jt
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import news_poll as news_poll_jt


class _FakeCtx:
    """Minimal DispatchContext stand-in capturing dispatcher side effects."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.store = object()
        self.meta = {"params": params or {}}
        self.chunks: list[tuple[str, str]] = []
        self.meta_set: dict[str, Any] = {}
        self.failure: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, msg: str) -> None:
        self.failure = msg


# ── registry wiring ────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["news_poll", "briefing"])
def test_job_type_registered(name: str) -> None:
    spec = get_job_type(name)
    assert spec is not None
    assert spec.name == name
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert spec.requires == frozenset()
    assert callable(spec.dispatch)
    assert name in known_job_types()


@pytest.mark.parametrize("mod", [news_poll_jt, briefing_jt])
def test_run_stub_raises(mod: Any) -> None:
    with pytest.raises(NotImplementedError):
        mod._run()


# ── news_poll dispatcher ───────────────────────────────────────────────


def test_news_poll_dispatch_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        news_poll_pass,
        "run_news_pass",
        lambda store, limit_sources=None: {"claimed": 5, "ok": 12, "failed": 1},
    )
    ctx = _FakeCtx()
    news_poll_jt._dispatch(ctx, news_poll_jt.SPEC)
    assert ctx.failure is None
    assert ctx.meta_set == {"feeds": 5, "new_articles": 12, "failed": 1}
    assert any("12 new articles" in t for _, t in ctx.chunks)


def test_news_poll_dispatch_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(store: Any, limit_sources: Any = None) -> dict:
        raise RuntimeError("feed down")

    monkeypatch.setattr(news_poll_pass, "run_news_pass", _boom)
    ctx = _FakeCtx()
    news_poll_jt._dispatch(ctx, news_poll_jt.SPEC)
    assert ctx.failure is not None and "feed down" in ctx.failure


# ── briefing dispatcher ────────────────────────────────────────────────


def test_briefing_dispatch_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        briefing_pass,
        "run_briefing",
        lambda store, **kw: {"articles": 8, "brief_chars": 900, "ref_id": 42},
    )
    ctx = _FakeCtx()
    briefing_jt._dispatch(ctx, briefing_jt.SPEC)
    assert ctx.failure is None
    assert ctx.meta_set == {"articles": 8, "brief_ref_id": 42}


def test_briefing_dispatch_empty_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        briefing_pass,
        "run_briefing",
        lambda store, **kw: {"articles": 0, "brief_chars": 0, "ref_id": None},
    )
    ctx = _FakeCtx()
    briefing_jt._dispatch(ctx, briefing_jt.SPEC)
    assert ctx.failure is None
    assert ctx.meta_set == {}  # nothing minted
    assert any("nothing to brief" in t for _, t in ctx.chunks)


def test_briefing_dispatch_passes_lookback(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _capture(store: Any, **kw: Any) -> dict:
        seen.update(kw)
        return {"articles": 1, "brief_chars": 10, "ref_id": 1}

    monkeypatch.setattr(briefing_pass, "run_briefing", _capture)
    briefing_jt._dispatch(_FakeCtx(params={"lookback_hours": 48}), briefing_jt.SPEC)
    assert seen == {"lookback_hours": 48}


def test_briefing_dispatch_passes_deliver_to(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _capture(store: Any, **kw: Any) -> dict:
        seen.update(kw)
        return {"articles": 1, "brief_chars": 10, "ref_id": 1}

    monkeypatch.setattr(briefing_pass, "run_briefing", _capture)
    briefing_jt._dispatch(
        _FakeCtx(params={"deliver_to": "conv:discord/g/c/t"}), briefing_jt.SPEC
    )
    assert seen == {"deliver_to": "conv:discord/g/c/t"}
