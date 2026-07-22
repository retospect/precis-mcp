"""Tests for the cast job_types (claude_inproc plugin dispatch → ctx reports)."""

from __future__ import annotations

from typing import Any

from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import meditation as md_jt
from precis.workers.job_types import reading_brief as rb_jt


class _FakeCtx:
    """Records the claude_inproc dispatch-context calls."""

    def __init__(self, meta: dict[str, Any] | None = None) -> None:
        self.store = object()
        self.meta = meta or {}
        self.chunks: list[tuple[str, str]] = []
        self.metas: dict[str, Any] = {}
        self.failure: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.metas.update(kw)

    def record_failure(self, reason: str) -> None:
        self.failure = reason


def test_specs_registered_on_claude_inproc() -> None:
    for name in ("reading_brief", "meditation", "card_forge"):
        spec = get_job_type(name)
        assert spec is not None
        assert spec.compatible_executors == frozenset({"claude_inproc"})
        assert spec.requires == frozenset()
        assert spec.dispatch is not None
        assert name in known_job_types()


class TestReadingBriefDispatch:
    def test_success_reports_ref(self, monkeypatch: Any) -> None:
        import precis.reading.briefing_cast as bc

        monkeypatch.setattr(bc, "build_reading_briefing", lambda store, **k: 42)
        ctx = _FakeCtx()
        rb_jt._dispatch(ctx, rb_jt.SPEC)
        assert ctx.failure is None
        assert ctx.metas["draft_ref_id"] == 42
        assert any("42" in t for _, t in ctx.chunks)

    def test_none_reports_nothing_composed(self, monkeypatch: Any) -> None:
        import precis.reading.briefing_cast as bc

        monkeypatch.setattr(bc, "build_reading_briefing", lambda store, **k: None)
        ctx = _FakeCtx()
        rb_jt._dispatch(ctx, rb_jt.SPEC)
        assert ctx.failure is None
        assert any("nothing" in t.lower() for _, t in ctx.chunks)

    def test_raise_records_failure(self, monkeypatch: Any) -> None:
        import precis.reading.briefing_cast as bc

        def boom(store: Any, **k: Any) -> Any:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(bc, "build_reading_briefing", boom)
        ctx = _FakeCtx()
        rb_jt._dispatch(ctx, rb_jt.SPEC)
        assert ctx.failure is not None and "kaboom" in ctx.failure


class TestMeditationDispatch:
    def test_passes_params_through(self, monkeypatch: Any) -> None:
        import precis.reading.meditation as m

        seen: dict[str, Any] = {}

        def fake_build(store: Any, **k: Any) -> int:
            seen.update(k)
            return 7

        monkeypatch.setattr(m, "build_meditation", fake_build)
        ctx = _FakeCtx(meta={"params": {"cohort": "waves", "target_minutes": 30}})
        md_jt._dispatch(ctx, md_jt.SPEC)
        assert seen["cohort"] == "waves"
        assert seen["target_minutes"] == 30
        # nidra gets the voice-craft preamble but not the numbers/formulas rule
        # (it's deliberately soft, not numerically precise) — see cast_common.
        assert "skill_preamble" in seen
        assert "Numbers: spell" not in seen["skill_preamble"]
        assert ctx.metas["draft_ref_id"] == 7


class TestCardForgeDispatch:
    def test_passes_params_and_reports(self, monkeypatch: Any) -> None:
        import precis.reading.cards as cards
        from precis.reading.cards import CardDecision, ForgeReport
        from precis.workers.job_types import card_forge as cf_jt

        seen: dict[str, Any] = {}

        def fake_run(store: Any, **k: Any) -> ForgeReport:
            seen.update(k)
            return ForgeReport(
                minted=[(11, [21, 22])],
                decisions=[CardDecision(31, 11, "rewrite", "wording at fault")],
            )

        monkeypatch.setattr(cards, "run_card_forge", fake_run)
        ctx = _FakeCtx(meta={"params": {"cohort": "waves", "per_day": 3}})
        cf_jt._dispatch(ctx, cf_jt.SPEC)
        assert seen == {"cohort": "waves", "per_day": 3}
        assert ctx.failure is None
        assert ctx.metas == {"minted_concepts": 1, "rework_decisions": 1}
        summary = ctx.chunks[0][1]
        assert "ak21" in summary and "would rewrite ak31" in summary

    def test_raise_records_failure(self, monkeypatch: Any) -> None:
        import precis.reading.cards as cards
        from precis.workers.job_types import card_forge as cf_jt

        def boom(store: Any, **k: Any) -> Any:
            raise RuntimeError("forge cold")

        monkeypatch.setattr(cards, "run_card_forge", boom)
        ctx = _FakeCtx()
        cf_jt._dispatch(ctx, cf_jt.SPEC)
        assert ctx.failure is not None and "forge cold" in ctx.failure
