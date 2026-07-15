"""Tests for the cast job_types (coordinator dispatch returns Done)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.workers.executors._yield import Done
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import meditation as md_jt
from precis.workers.job_types import reading_brief as rb_jt


def test_specs_registered_on_coordinator() -> None:
    for name in ("reading_brief", "meditation"):
        spec = get_job_type(name)
        assert spec is not None
        assert spec.compatible_executors == frozenset({"coordinator"})
        assert spec.requires == frozenset()
        assert spec.dispatch is not None
        assert name in known_job_types()


class TestReadingBriefDispatch:
    def test_success_returns_done_with_ref(self, monkeypatch: Any) -> None:
        import precis.reading.briefing_cast as bc

        monkeypatch.setattr(bc, "build_reading_briefing", lambda store, **k: 42)
        ctx = SimpleNamespace(store=object(), meta={})
        out = rb_jt._dispatch(ctx, rb_jt.SPEC)
        assert isinstance(out, Done) and out.success
        assert out.summary_meta["draft_ref_id"] == 42

    def test_none_returns_done_nothing(self, monkeypatch: Any) -> None:
        import precis.reading.briefing_cast as bc

        monkeypatch.setattr(bc, "build_reading_briefing", lambda store, **k: None)
        out = rb_jt._dispatch(SimpleNamespace(store=object(), meta={}), rb_jt.SPEC)
        assert isinstance(out, Done) and out.success
        assert "nothing" in out.summary.lower()

    def test_raise_returns_failed_done(self, monkeypatch: Any) -> None:
        import precis.reading.briefing_cast as bc

        def boom(store: Any, **k: Any) -> Any:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(bc, "build_reading_briefing", boom)
        out = rb_jt._dispatch(SimpleNamespace(store=object(), meta={}), rb_jt.SPEC)
        assert isinstance(out, Done) and out.success is False


class TestMeditationDispatch:
    def test_passes_params_through(self, monkeypatch: Any) -> None:
        import precis.reading.meditation as m

        seen: dict[str, Any] = {}

        def fake_build(store: Any, **k: Any) -> int:
            seen.update(k)
            return 7

        monkeypatch.setattr(m, "build_meditation", fake_build)
        ctx = SimpleNamespace(
            store=object(), meta={"params": {"cohort": "waves", "target_minutes": 30}}
        )
        out = md_jt._dispatch(ctx, md_jt.SPEC)
        assert isinstance(out, Done) and out.success
        assert seen == {"cohort": "waves", "target_minutes": 30}
        assert out.summary_meta["draft_ref_id"] == 7
