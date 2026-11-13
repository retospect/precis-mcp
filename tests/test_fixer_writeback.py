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

        def tags_for(self, ref_id: int) -> list[Any]:
            return []

        def close(self) -> None:
            self.closed = True

    fake = _FakeStore()

    import precis.store.store as store_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: fake)

    def _fake_append_chunk(
        store: Any,
        ref_id: int,
        kind: str,
        text: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        calls["append_chunk"] = (ref_id, kind, text, meta)

    import precis.workers.executors._common as common_mod

    monkeypatch.setattr(common_mod, "append_chunk", _fake_append_chunk)

    ok = writeback_mod.gripe_writeback(
        "postgresql://example/db", 42, "FIXER (auto): built it", status="in_review"
    )

    assert ok is True
    assert fake.closed is True
    assert calls["append_chunk"] == (
        42,
        "gripe_comment",
        "FIXER (auto): built it",
        {"stage": "code"},
    )
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
        common_mod, "append_chunk", lambda store, ref_id, kind, text, **kw: None
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


# ── skill-gripe escalation ──────────────────────────────────────────


def _run_writeback(
    monkeypatch: pytest.MonkeyPatch,
    prior: list[str],
    comment: str,
    *,
    status: str | None,
    escalate_skill: bool,
    prior_status: str | None = None,
) -> dict[str, Any]:
    calls: dict[str, Any] = {"appended": [], "tags": []}

    class _Chunk:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Chunks:
        def list_chunks_for_ref(self, ref_id: int) -> list[_Chunk]:
            return [_Chunk(t) for t in prior]

    class _FakeStore:
        chunks = _Chunks()

        def add_tag(self, ref_id: int, tag: Any, **kw: Any) -> None:
            calls["tags"].append(
                tag.value if tag.prefix is None else f"{tag.prefix}:{tag.value}"
            )

        def tags_for(self, ref_id: int) -> list[Any]:
            from precis.store.types import Tag

            return [Tag.closed("STATUS", prior_status)] if prior_status else []

        def close(self) -> None:
            pass

    import precis.store.store as store_mod
    import precis.workers.executors._common as common_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(
        common_mod,
        "append_chunk",
        lambda store, ref_id, kind, text, **kw: calls["appended"].append(text),
    )
    assert writeback_mod.gripe_writeback(
        "postgresql://x/db", 7, comment, status, escalate_skill=escalate_skill
    )
    return calls


FAIL = "FIXER (auto): build attempt did not land — boom"


def test_second_failure_on_a_skill_gripe_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _run_writeback(monkeypatch, [FAIL], FAIL, status=None, escalate_skill=True)
    assert "waiting-for:reto" in calls["tags"]
    assert any("escalated to reto" in t and "tried: " in t for t in calls["appended"])


def test_first_failure_and_non_skill_gripes_do_not_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _run_writeback(monkeypatch, [], FAIL, status=None, escalate_skill=True)
    assert first["tags"] == [] and len(first["appended"]) == 1
    other = _run_writeback(monkeypatch, [FAIL], FAIL, status=None, escalate_skill=False)
    assert other["tags"] == [] and len(other["appended"]) == 1
    landed = _run_writeback(
        monkeypatch,
        [FAIL],
        "FIXER (auto): candidate fix built",
        status="in_review",
        escalate_skill=True,
    )
    assert "waiting-for:reto" not in landed["tags"]


def test_writeback_does_not_downgrade_a_verify_gripe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pipeline v2: a ``STATUS:verify`` gripe (fix deployed, probe
    pending) keeps its status when a late write-back lands — the comment
    still appends, only the flip is skipped."""
    calls = _run_writeback(
        monkeypatch,
        [],
        "FIXER (auto): fix shipped to main (branch fix/gr7)",
        status="in_review",
        escalate_skill=False,
        prior_status="verify",
    )
    assert len(calls["appended"]) == 1
    assert calls["tags"] == []


def test_work_item_carries_linked_skills() -> None:
    from precis.fixer.intake import _TimelineEntry, _work_item_from_gripe

    item = _work_item_from_gripe(
        5,
        "t",
        None,
        [_TimelineEntry("gripe_comment", 0, "DIAGNOSIS: x")],
        ["precis-gripe-help"],
    )
    assert item is not None and item.skills == ("precis-gripe-help",)


def test_gripe_outcome_composes_the_head_only() -> None:
    """The pure helper composes the outcome head only — the code stage's
    ``touched files:`` / ``changed symbols:`` evidence lines belong to
    ``gripe_writeback``, which needs a store to mint the ``an<N>``
    anchors the ledger's anchor-only contract requires (gr595551
    review)."""
    report = Report(ReportStatus.OK, "t", "branch fix/gr42 built")
    comment, _ = _gripe_outcome(report, Autonomy.REPORT, _item(), shipped=False)
    assert comment.startswith("FIXER (auto):")
    assert "touched files" not in comment
    assert "changed symbols" not in comment


def test_writeback_touched_drops_evidence_when_mint_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-soft: a mint failure (the fake store has no pool) drops the
    evidence lines but never the outcome comment — raw paths/symbols
    are never written as a fallback."""
    calls: dict[str, Any] = {}

    class _FakeStore:
        def close(self) -> None:
            pass

    import precis.store.store as store_mod
    import precis.workers.executors._common as common_mod

    monkeypatch.setattr(store_mod.Store, "connect", lambda *a, **k: _FakeStore())
    monkeypatch.setattr(
        common_mod,
        "append_chunk",
        lambda store, ref_id, kind, text, **kw: calls.setdefault("text", text),
    )

    ok = writeback_mod.gripe_writeback(
        "postgresql://example/db",
        42,
        "FIXER (auto): built it",
        touched=(["src/a.py"], ["src/a.py::do_thing"]),
    )

    assert ok is True
    assert calls["text"] == "FIXER (auto): built it"


def test_change_evidence_names_added_and_edited_symbols(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AST-diff of both revisions names the actually-changed symbols as
    ``path::qual`` — an added function is detected (hunk headers
    attributed it to its predecessor) and an unchanged sibling is not
    listed."""
    from precis.fixer import tick as tick_mod

    old_src = "def do_thing():\n    return 1\n\n\nclass Foo:\n    pass\n"
    new_src = (
        "def do_thing():\n    return 2\n\n\nclass Foo:\n    pass\n\n\n"
        "def brand_new():\n    return 3\n"
    )

    class _R:
        def __init__(self, out: str, rc: int = 0) -> None:
            self.stdout = out
            self.returncode = rc

    def _fake_git(repo: Any, *args: str, check: bool = True) -> _R:
        if "--name-only" in args:
            return _R("src/a.py\n")
        if "origin/main:src/a.py" in args:
            return _R(old_src)
        if "HEAD:src/a.py" in args:
            return _R(new_src)
        return _R("")

    monkeypatch.setattr(tick_mod, "_git", _fake_git)
    assert tick_mod._change_evidence(tmp_path) == (
        ["src/a.py"],
        ["src/a.py::brand_new", "src/a.py::do_thing"],
    )


def test_change_evidence_skips_unchanged_and_nonpy(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol whose span only shifted is not a change; a non-Python
    file contributes to touched files but never to symbols."""
    from precis.fixer import tick as tick_mod

    old_src = "LIMIT = 10\n\n\ndef stable():\n    return 1\n"
    new_src = "NEW_FLAG = True\n\n\nLIMIT = 10\n\n\ndef stable():\n    return 1\n"

    class _R:
        def __init__(self, out: str, rc: int = 0) -> None:
            self.stdout = out
            self.returncode = rc

    def _fake_git(repo: Any, *args: str, check: bool = True) -> _R:
        if "--name-only" in args:
            return _R("src/a.py\nscripts/run.sh\n")
        if "origin/main:src/a.py" in args:
            return _R(old_src)
        if "HEAD:src/a.py" in args:
            return _R(new_src)
        return _R("", rc=1)

    monkeypatch.setattr(tick_mod, "_git", _fake_git)
    files, syms = tick_mod._change_evidence(tmp_path)
    assert files == ["src/a.py", "scripts/run.sh"]
    assert syms == ["src/a.py::NEW_FLAG"]


def test_change_evidence_deleted_and_added_files(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deleted file's symbols read as removed; an added file's read
    as added — both sides of the diff contribute."""
    from precis.fixer import tick as tick_mod

    class _R:
        def __init__(self, out: str, rc: int = 0) -> None:
            self.stdout = out
            self.returncode = rc

    def _fake_git(repo: Any, *args: str, check: bool = True) -> _R:
        if "--name-only" in args:
            return _R("src/gone.py\nsrc/new.py\n")
        if "origin/main:src/gone.py" in args:
            return _R("def was_here():\n    pass\n")
        if "HEAD:src/new.py" in args:
            return _R("def is_here():\n    pass\n")
        return _R("", rc=1)  # the missing side of each pair

    monkeypatch.setattr(tick_mod, "_git", _fake_git)
    files, syms = tick_mod._change_evidence(tmp_path)
    assert files == ["src/gone.py", "src/new.py"]
    assert syms == ["src/gone.py::was_here", "src/new.py::is_here"]
