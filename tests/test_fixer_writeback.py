"""Unit tests for the fixer's gripe write-back (the dark-factory sink
half — ``intake.py`` is the read side, this is the write side).

Two things load-bearing enough to pin down:

* **Fail-soft.** A write-back failure (unreachable DB, import blowing
  up, whatever) must never propagate — the tick already built, gated,
  and possibly shipped the fix by the time write-back runs, so a
  crash here must not undo reporting that success.
* **The pure outcome helper.** ``tick._gripe_outcome`` maps
  ``(report.status, autonomy, shipped)`` to a (comment, status) pair
  without touching the network — the comment never starts with
  ``DIAGNOSIS`` (that prefix is the diagnose_gripe promotion signal;
  ``intake._is_diagnosed`` would wrongly treat a write-back comment as
  a fresh diagnosis) and only an unshipped ``NEEDS_YOU`` yields a
  ``None`` status — a shipped-but-unverified ``NEEDS_YOU`` (full
  autonomy's deploy/prod-check failing after a successful ship, gr313409)
  still flips to ``in_review`` and says so honestly instead of "did not
  land".
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.fixer import writeback as writeback_mod
from precis.fixer.intake import WorkItem
from precis.fixer.report import Report, ReportStatus
from precis.fixer.tick import Autonomy, _gripe_outcome


def _item(**kwargs: Any) -> WorkItem:
    base: dict[str, Any] = dict(
        kind="gripe",
        slug="gr42",
        title="Something broke",
        branch="fix/gr42",
        spec_text="x",
        ref_id=42,
    )
    base.update(kwargs)
    return WorkItem(**base)


# ── fail-soft ────────────────────────────────────────────────────────


def test_gripe_writeback_fail_soft_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import precis.store.store as store_mod

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("store layer unreachable")

    monkeypatch.setattr(store_mod.Store, "connect", _boom)

    ok = writeback_mod.gripe_writeback(
        "postgresql://example/db", 42, "FIXER (auto): whatever"
    )
    assert ok is False


def test_gripe_writeback_success_appends_comment_and_flips_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class _FakeStore:
        def __init__(self) -> None:
            self.closed = False

        def add_tag(self, ref_id: int, tag: Any, **kwargs: Any) -> None:
            calls["add_tag"] = (ref_id, tag, kwargs)

        def close(self) -> None:
            self.closed = True

    fake = _FakeStore()

    import precis.store.store as store_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: fake)

    def _fake_append_chunk(store: Any, ref_id: int, kind: str, text: str) -> None:
        calls["append_chunk"] = (ref_id, kind, text)

    import precis.workers.executors._common as common_mod

    monkeypatch.setattr(common_mod, "append_chunk", _fake_append_chunk)

    ok = writeback_mod.gripe_writeback(
        "postgresql://example/db", 42, "FIXER (auto): built it", status="in_review"
    )

    assert ok is True
    assert fake.closed is True
    assert calls["append_chunk"] == (42, "gripe_comment", "FIXER (auto): built it")
    ref_id, tag, kwargs = calls["add_tag"]
    assert ref_id == 42
    assert (tag.prefix, tag.value) == ("STATUS", "in_review")
    assert kwargs["set_by"] == "agent"
    assert kwargs["replace_prefix"] is True


def test_gripe_writeback_no_status_skips_add_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class _FakeStore:
        def __init__(self) -> None:
            self.closed = False

        def add_tag(self, *a: Any, **k: Any) -> None:
            calls["add_tag_called"] = True

        def close(self) -> None:
            self.closed = True

    fake = _FakeStore()

    import precis.store.store as store_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: fake)

    import precis.workers.executors._common as common_mod

    monkeypatch.setattr(
        common_mod, "append_chunk", lambda store, ref_id, kind, text: None
    )

    ok = writeback_mod.gripe_writeback("postgresql://example/db", 42, "FIXER (auto): x")

    assert ok is True
    assert "add_tag_called" not in calls


# ── _gripe_outcome: pure comment/status derivation ──────────────────


def test_gripe_outcome_report_autonomy_ok_is_in_review() -> None:
    report = Report(ReportStatus.OK, "t", "branch fix/gr42 built + gate green")
    comment, status = _gripe_outcome(report, Autonomy.REPORT, _item(), shipped=False)
    assert comment.startswith("FIXER (auto):")
    assert not comment.startswith("DIAGNOSIS")
    assert "fix/gr42" in comment
    assert status == "in_review"


def test_gripe_outcome_ship_autonomy_ok_is_in_review() -> None:
    report = Report(ReportStatus.OK, "t", "shipped fix/gr42 to main")
    comment, status = _gripe_outcome(report, Autonomy.SHIP, _item(), shipped=True)
    assert comment.startswith("FIXER (auto):")
    assert not comment.startswith("DIAGNOSIS")
    assert "shipped" in comment
    assert status == "in_review"


def test_gripe_outcome_full_autonomy_ok_is_in_review() -> None:
    report = Report(ReportStatus.OK, "t", "shipped + deployed; /readyz 200")
    comment, status = _gripe_outcome(report, Autonomy.FULL, _item(), shipped=True)
    assert comment.startswith("FIXER (auto):")
    assert not comment.startswith("DIAGNOSIS")
    assert "/readyz 200" in comment
    assert status == "in_review"


def test_gripe_outcome_needs_you_has_no_status_flip() -> None:
    report = Report(
        ReportStatus.NEEDS_YOU, "t", "gate failed: mypy\nline1\nline2\nline3\nline4"
    )
    comment, status = _gripe_outcome(report, Autonomy.REPORT, _item(), shipped=False)
    assert comment.startswith("FIXER (auto):")
    assert not comment.startswith("DIAGNOSIS")
    assert "did not land" in comment
    assert status is None


def test_gripe_outcome_needs_you_truncates_detail_to_three_lines() -> None:
    detail = "\n".join(f"line{i}" for i in range(10))
    report = Report(ReportStatus.NEEDS_YOU, "t", detail)
    comment, _status = _gripe_outcome(report, Autonomy.REPORT, _item(), shipped=False)
    assert "line0" in comment
    assert "line1" in comment
    assert "line2" in comment
    assert "line9" not in comment


# ── gr313409: shipped-but-unverified NEEDS_YOU is told honestly ─────


def test_gripe_outcome_shipped_needs_you_is_honest_and_flips_in_review() -> None:
    """Full autonomy: ``scripts/ship`` succeeded (fix landed on main) but
    the post-ship deploy/prod-check failed — the write-back must NOT say
    "did not land" (it did) and must still flip the gripe to
    ``in_review`` since a human needs to look at the fix-forward, not
    re-pick the gripe from ``open``."""
    report = Report(
        ReportStatus.NEEDS_YOU,
        "t",
        "deployed but prod check failed — fix-forward needed. /readyz unreachable",
    )
    comment, status = _gripe_outcome(report, Autonomy.FULL, _item(), shipped=True)
    assert comment.startswith("FIXER (auto):")
    assert not comment.startswith("DIAGNOSIS")
    assert "did not land" not in comment
    assert "shipped" in comment.lower()
    assert "not verified" in comment.lower() or "fix-forward" in comment.lower()
    assert status == "in_review"


def test_gripe_outcome_unshipped_needs_you_wording_unchanged() -> None:
    """The pre-ship NEEDS_YOU path (build/gate failure, never reached
    ``scripts/ship``) keeps its existing "did not land" wording and
    ``None`` status — unaffected by the ``shipped`` threading."""
    report = Report(ReportStatus.NEEDS_YOU, "t", "gate failed: mypy\nboom")
    comment, status = _gripe_outcome(report, Autonomy.SHIP, _item(), shipped=False)
    assert (
        comment == "FIXER (auto): build attempt did not land — gate failed: mypy\nboom"
    )
    assert status is None
