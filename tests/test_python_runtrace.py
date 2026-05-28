"""Tests for ``view='runtrace'`` — dynamic call-graph capture.

These tests actually spawn subprocesses (the runner script is the
whole point of the slice). They monkeypatch ``PRECIS_PYTHON_ALLOW_EXEC=1``
so they always run; the handler-level gate test runs without the env
to verify the off-by-default behaviour.

Per-test cost is ~150ms (subprocess startup dominates). Kept count
moderate; deeper unit tests on the harness/tree-builder use
synthesised events rather than real subprocesses.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _python_runtrace as rtrace
from precis.handlers.python import PythonHandler

# The runner subprocess fails on Python 3.12 with a partially-
# initialised ``urllib.parse`` import error when ``sys.setprofile``
# intercepts the import machinery during a test entry that uses
# ``argparse`` (which lazy-imports ``urllib.parse`` for help-text
# fallbacks). First seen on macOS framework Python 3.12; now also
# reproduces in the Linux ``precis-dev`` container's Python 3.12.
# 3.11 and 3.13 both work. Tracked in ``OPEN-ITEMS.md`` under
# "Platform-specific test bugs (Windows + Python 3.12 setprofile)".
#
# ``strict=False`` because some 3.12 builds (e.g. Homebrew) do work,
# so an XPASS in CI on a different runtime is fine. We still want
# pytest to *attempt* the test so we notice when the bug is fixed.
_PY312_SETPROFILE_BUG = sys.version_info[:2] == (3, 12)
_RUNTRACE_XFAIL_REASON = (
    "Python 3.12 sys.setprofile + urllib.parse circular import; "
    "see OPEN-ITEMS.md §Platform-specific test bugs."
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Tiny package whose `main()` calls `loop()` (which calls
    `helper()` 3×) plus one direct `helper()` call.

    Layout:
      <tmp_path>/
        demopkg/
          __init__.py
          m.py        — main, loop, helper, dead_code
    """
    pkg = tmp_path / "demopkg"
    _write(pkg / "__init__.py", "")
    _write(
        pkg / "m.py",
        '''
        def helper() -> int:
            return 1


        def loop() -> int:
            total = 0
            for _ in range(3):
                total += helper()
            return total


        def main() -> int:
            x = loop()
            y = helper()
            return x + y


        def dead_code() -> int:
            """Statically reachable from main? No. Lives here so the
            'Static-only' diff has nothing to surface for main."""
            return 42
        ''',
    )
    return tmp_path


@pytest.fixture
def handler(repo: Path) -> PythonHandler:
    return PythonHandler(hub=Hub(), roots={"r": repo / "demopkg"})


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the runtrace gate for the duration of the test."""
    monkeypatch.setenv("PRECIS_PYTHON_ALLOW_EXEC", "1")


# ---------------------------------------------------------------------------
# Env gate (no subprocess)
# ---------------------------------------------------------------------------


def test_runtrace_gated_off_by_default(
    handler: PythonHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``PRECIS_PYTHON_ALLOW_EXEC=1`` the handler refuses,
    pointing the agent at the env var. No subprocess is spawned."""
    monkeypatch.delenv("PRECIS_PYTHON_ALLOW_EXEC", raising=False)
    with pytest.raises(BadInput, match="PRECIS_PYTHON_ALLOW_EXEC=1"):
        handler.get(id="r", view="runtrace", entry="demopkg.m:main")


def test_runtrace_gate_off_message_mentions_callgraph_fallback(
    handler: PythonHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_PYTHON_ALLOW_EXEC", raising=False)
    with pytest.raises(BadInput) as excinfo:
        handler.get(id="r", view="runtrace", entry="demopkg.m:main")
    # Recovery hint lives in the .next field, not the message body.
    assert excinfo.value.next is not None
    assert "callgraph" in excinfo.value.next


def test_runtrace_gate_zero_value_blocks(
    handler: PythonHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the literal '1' enables — '0', 'true', 'yes' all block."""
    monkeypatch.setenv("PRECIS_PYTHON_ALLOW_EXEC", "0")
    with pytest.raises(BadInput, match="PRECIS_PYTHON_ALLOW_EXEC=1"):
        handler.get(id="r", view="runtrace", entry="demopkg.m:main")


# ---------------------------------------------------------------------------
# Argument validation (gate on, but errors before subprocess)
# ---------------------------------------------------------------------------


def test_runtrace_requires_entry(handler: PythonHandler, gate_on: None) -> None:
    with pytest.raises(BadInput, match="requires entry="):
        handler.get(id="r", view="runtrace")


def test_runtrace_rejects_file_id(handler: PythonHandler, gate_on: None) -> None:
    with pytest.raises(BadInput, match="bare alias"):
        handler.get(id="r/demopkg/m.py", view="runtrace", entry="demopkg.m:main")


def test_runtrace_rejects_qualname_id(handler: PythonHandler, gate_on: None) -> None:
    with pytest.raises(BadInput, match="bare alias"):
        handler.get(id="r::demopkg.m.main", view="runtrace", entry="demopkg.m:main")


def test_runtrace_rejects_bad_timeout(handler: PythonHandler, gate_on: None) -> None:
    with pytest.raises(BadInput, match="timeout must be"):
        handler.get(id="r", view="runtrace", entry="demopkg.m:main", timeout=0)
    with pytest.raises(BadInput, match="timeout must be"):
        handler.get(id="r", view="runtrace", entry="demopkg.m:main", timeout=999)


# ---------------------------------------------------------------------------
# Real subprocess execution
# ---------------------------------------------------------------------------


@pytest.mark.xfail(_PY312_SETPROFILE_BUG, reason=_RUNTRACE_XFAIL_REASON, strict=False)
def test_runtrace_captures_call_tree(handler: PythonHandler, gate_on: None) -> None:
    """End-to-end: actual subprocess, real setprofile, real events.
    The result should show main calling loop calling helper 3× plus
    a direct helper call."""
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.m:main",
        timeout=5,
    )
    body = out.body
    assert "Runtime trace of r::demopkg.m:main" in body
    assert "demopkg.m.main" in body
    assert "demopkg.m.loop" in body
    assert "demopkg.m.helper" in body
    # The inner-loop helper call coalesces into 3×.
    assert "3×" in body


def test_runtrace_produces_static_only_diff(
    handler: PythonHandler, gate_on: None
) -> None:
    """`dead_code` is statically defined but never called from main —
    it's a no-op for the diff (not reachable statically from main).
    But `main`'s static reach IS just {loop, helper}, all of which
    fire at runtime. So the diff section should be empty for this
    fixture."""
    out = handler.get(id="r", view="runtrace", entry="demopkg.m:main", timeout=5)
    body = out.body
    # The 'Static-only' header may or may not appear; either way,
    # `dead_code` is unreachable from main, so it should NOT appear.
    assert "demopkg.m.dead_code" not in body


@pytest.mark.xfail(_PY312_SETPROFILE_BUG, reason=_RUNTRACE_XFAIL_REASON, strict=False)
def test_runtrace_argv_is_forwarded(repo: Path, gate_on: None) -> None:
    """Adding a script that inspects sys.argv proves argv= is forwarded
    through the runner into the entry's process."""
    pkg = repo / "demopkg"
    _write(
        pkg / "argv_check.py",
        """
        import sys

        def report() -> None:
            print(f"GOT_ARGV={sys.argv[1:]}")
        """,
    )
    handler = PythonHandler(hub=Hub(), roots={"r": pkg})
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.argv_check:report",
        argv=["--flag", "hello"],
        timeout=5,
    )
    # The runner captures stdout but doesn't surface it on success;
    # call run_trace directly to verify argv reached the subprocess.
    result = rtrace.run_trace(
        entry="demopkg.argv_check:report",
        argv=["--flag", "hello"],
        cwd=pkg,
        syspath=[pkg, repo],
        timeout=5,
    )
    assert "GOT_ARGV=['--flag', 'hello']" in result.stdout


def test_runtrace_handles_entry_import_failure(
    handler: PythonHandler, gate_on: None
) -> None:
    """A missing entry doesn't crash the harness — failure surfaces
    as a structured error in the response body."""
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.does_not_exist:main",
        timeout=5,
    )
    body = out.body
    assert "Runtime trace" in body  # header still rendered
    assert "failed:" in body or "could not import" in body


def test_runtrace_timeout_kills_runaway(repo: Path, gate_on: None) -> None:
    """A loop that never returns is killed at the timeout and the
    response surfaces the timeout, not a hang."""
    pkg = repo / "demopkg"
    _write(
        pkg / "spinner.py",
        """
        import time

        def spin() -> None:
            while True:
                time.sleep(0.01)
        """,
    )
    handler = PythonHandler(hub=Hub(), roots={"r": pkg})
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.spinner:spin",
        timeout=1,
    )
    body = out.body
    assert "timeout" in body.lower() or "failed" in body.lower()


# ---------------------------------------------------------------------------
# Lower-level tree builder unit tests (no subprocess)
# ---------------------------------------------------------------------------


def _ev(event: str, qn: str, t: float) -> rtrace.TraceEvent:
    return rtrace.TraceEvent(event=event, qn=qn, t=t)


def test_build_tree_empty_returns_none() -> None:
    assert rtrace.build_tree(()) is None


def test_build_tree_simple_call_return() -> None:
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "a.helper", 0.001),
        _ev("return", "a.helper", 0.002),
        _ev("return", "a.main", 0.003),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    assert tree.qualname == "a.main"
    assert len(tree.children) == 1
    assert tree.children[0].qualname == "a.helper"
    # Total time is 1ms = 1_000_000ns.
    assert 0.5e6 < tree.children[0].total_ns < 2.0e6


def test_build_tree_coalesces_consecutive_siblings() -> None:
    """Three back-to-back calls to helper() collapse into one node
    with multiplicity=3."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "a.helper", 0.001),
        _ev("return", "a.helper", 0.002),
        _ev("call", "a.helper", 0.003),
        _ev("return", "a.helper", 0.004),
        _ev("call", "a.helper", 0.005),
        _ev("return", "a.helper", 0.006),
        _ev("return", "a.main", 0.007),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    assert len(tree.children) == 1
    assert tree.children[0].multiplicity == 3
    # Total ns is sum of 3 × 1ms = 3ms.
    assert 2.5e6 < tree.children[0].total_ns < 3.5e6


def test_build_tree_does_not_coalesce_alternating() -> None:
    """A B A B should produce 4 separate child nodes, not 2."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "a.A", 0.001),
        _ev("return", "a.A", 0.002),
        _ev("call", "a.B", 0.003),
        _ev("return", "a.B", 0.004),
        _ev("call", "a.A", 0.005),
        _ev("return", "a.A", 0.006),
        _ev("call", "a.B", 0.007),
        _ev("return", "a.B", 0.008),
        _ev("return", "a.main", 0.009),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    labels = [c.qualname for c in tree.children]
    # 4 separate nodes — no coalescing across non-consecutive siblings.
    assert labels == ["a.A", "a.B", "a.A", "a.B"]


def test_build_tree_tags_c_calls() -> None:
    events = (
        _ev("call", "a.main", 0.0),
        _ev("c_call", "builtins.len", 0.001),
        _ev("c_return", "builtins.len", 0.002),
        _ev("return", "a.main", 0.003),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    assert tree.children[0].is_c is True


def test_collect_runtime_qualnames() -> None:
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "a.helper", 0.001),
        _ev("return", "a.helper", 0.002),
        _ev("return", "a.main", 0.003),
    )
    tree = rtrace.build_tree(events)
    qns = rtrace.collect_runtime_qualnames(tree)
    assert qns == {"a.main", "a.helper"}


# ---------------------------------------------------------------------------
# Entry-spec parsing (used by the runner script)
# ---------------------------------------------------------------------------


def test_runner_split_entry_colon_form() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from precis.handlers._python_runtrace_runner import _split_entry

    assert _split_entry("pkg.mod:func") == ("pkg.mod", "func")


def test_runner_split_entry_dotted_form() -> None:
    from precis.handlers._python_runtrace_runner import _split_entry

    assert _split_entry("pkg.mod.func") == ("pkg.mod", "func")


def test_runner_split_entry_rejects_no_separator() -> None:
    from precis.handlers._python_runtrace_runner import _split_entry

    with pytest.raises(ValueError, match="dotted entry"):
        _split_entry("just_a_name")


def test_runner_split_entry_rejects_empty() -> None:
    from precis.handlers._python_runtrace_runner import _split_entry

    with pytest.raises(ValueError, match="empty"):
        _split_entry("")


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_includes_call_count_and_elapsed() -> None:
    events = (
        _ev("call", "a.main", 0.0),
        _ev("return", "a.main", 0.018),
    )
    result = rtrace.TraceResult(
        ok=True,
        events=events,
        truncated=False,
        exit_code=0,
        elapsed_s=0.018,
    )
    tree = rtrace.build_tree(events)
    body = rtrace.render_runtrace(
        alias="r", entry="a:main", argv=[], result=result, tree=tree
    )
    assert "1 calls" in body
    assert "18.0ms" in body
    assert "a.main" in body


def test_render_marks_truncated() -> None:
    result = rtrace.TraceResult(
        ok=True,
        events=tuple(),
        truncated=True,
        exit_code=0,
        elapsed_s=10.0,
    )
    body = rtrace.render_runtrace(
        alias="r", entry="a:main", argv=[], result=result, tree=None
    )
    assert "truncated" in body


def test_render_static_only_section() -> None:
    result = rtrace.TraceResult(
        ok=True,
        events=(_ev("call", "a.main", 0.0), _ev("return", "a.main", 0.001)),
        truncated=False,
        exit_code=0,
        elapsed_s=0.001,
    )
    tree = rtrace.build_tree(result.events)
    body = rtrace.render_runtrace(
        alias="r",
        entry="a:main",
        argv=[],
        result=result,
        tree=tree,
        static_only=["a.unused1", "a.unused2"],
    )
    assert "Static-only" in body
    assert "a.unused1" in body
    assert "a.unused2" in body


def test_render_failed_includes_stderr_tail() -> None:
    result = rtrace.TraceResult(
        ok=False,
        events=tuple(),
        truncated=False,
        exit_code=2,
        elapsed_s=0.0,
        error="entry not found",
        stderr="line1\nline2\nline3\nfatal error: nope\n",
    )
    body = rtrace.render_runtrace(
        alias="r", entry="a:main", argv=[], result=result, tree=None
    )
    assert "failed: entry not found" in body
    assert "Stderr" in body
    assert "fatal error: nope" in body


# ---------------------------------------------------------------------------
# Stdlib collapse (display-only)
# ---------------------------------------------------------------------------


def test_is_stdlib_qn_recognises_top_level_stdlib() -> None:
    """Anything whose top-level module is in `sys.stdlib_module_names`
    counts as stdlib for collapse purposes."""
    assert rtrace._is_stdlib_qn("argparse.ArgumentParser.__init__")
    assert rtrace._is_stdlib_qn("re._compile")
    assert rtrace._is_stdlib_qn("builtins.dict.setdefault")
    assert rtrace._is_stdlib_qn("posix.fspath")
    assert rtrace._is_stdlib_qn("os._Environ.__getitem__")
    assert rtrace._is_stdlib_qn("_frozen_importlib._call_with_frames_removed")


def test_is_stdlib_qn_user_code_is_not_stdlib() -> None:
    assert not rtrace._is_stdlib_qn("precis.cli.main")
    assert not rtrace._is_stdlib_qn("demopkg.m.helper")
    assert not rtrace._is_stdlib_qn("")
    assert not rtrace._is_stdlib_qn("<module>")


def test_collapse_stdlib_drops_subtree_and_records_count() -> None:
    """A user→stdlib→deeper subtree collapses to the stdlib root with
    `collapsed_count` == number of dropped descendants (counting
    multiplicities)."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "argparse.ArgumentParser.__init__", 0.001),
        _ev("call", "argparse._ActionsContainer.__init__", 0.002),
        _ev("call", "argparse._ActionsContainer.register", 0.003),
        _ev("return", "argparse._ActionsContainer.register", 0.004),
        _ev("call", "argparse._ActionsContainer.register", 0.005),
        _ev("return", "argparse._ActionsContainer.register", 0.006),
        _ev("return", "argparse._ActionsContainer.__init__", 0.007),
        _ev("return", "argparse.ArgumentParser.__init__", 0.008),
        _ev("return", "a.main", 0.009),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    rtrace.collapse_stdlib(tree)

    # User root untouched.
    assert tree.qualname == "a.main"
    assert len(tree.children) == 1

    # Stdlib node kept, but its descendants are gone.
    stdlib_root = tree.children[0]
    assert stdlib_root.qualname == "argparse.ArgumentParser.__init__"
    assert stdlib_root.children == []
    # 3 descendants were dropped: ActionsContainer.__init__ (mult=1)
    # plus register (coalesced mult=2). Total = 3.
    assert stdlib_root.collapsed_count == 3


def test_collapse_stdlib_preserves_user_code() -> None:
    """User → user → user trees are not touched."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "a.helper", 0.001),
        _ev("call", "a.inner", 0.002),
        _ev("return", "a.inner", 0.003),
        _ev("return", "a.helper", 0.004),
        _ev("return", "a.main", 0.005),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    rtrace.collapse_stdlib(tree)
    # All three user qualnames survive.
    qns = rtrace.collect_runtime_qualnames(tree)
    assert qns == {"a.main", "a.helper", "a.inner"}


def test_collapse_stdlib_idempotent() -> None:
    """Calling twice is a no-op on the second pass."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "argparse.X", 0.001),
        _ev("call", "argparse.Y", 0.002),
        _ev("return", "argparse.Y", 0.003),
        _ev("return", "argparse.X", 0.004),
        _ev("return", "a.main", 0.005),
    )
    tree = rtrace.build_tree(events)
    assert tree is not None
    rtrace.collapse_stdlib(tree)
    once = tree.children[0].collapsed_count
    rtrace.collapse_stdlib(tree)
    assert tree.children[0].collapsed_count == once
    assert tree.children[0].children == []


def test_collapse_stdlib_handles_none_root() -> None:
    assert rtrace.collapse_stdlib(None) is None


def test_render_shows_collapsed_count_annotation() -> None:
    """The renderer surfaces `(+N stdlib)` for collapsed nodes."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("call", "argparse.X", 0.001),
        _ev("call", "argparse.Y", 0.002),
        _ev("return", "argparse.Y", 0.003),
        _ev("return", "argparse.X", 0.004),
        _ev("return", "a.main", 0.005),
    )
    result = rtrace.TraceResult(
        ok=True, events=events, truncated=False, exit_code=0, elapsed_s=0.005
    )
    tree = rtrace.build_tree(events)
    rtrace.collapse_stdlib(tree)
    body = rtrace.render_runtrace(
        alias="r", entry="a:main", argv=[], result=result, tree=tree, collapsed=True
    )
    assert "(+1 stdlib)" in body  # 1 descendant dropped
    assert "stdlib collapsed" in body  # header flag
    assert "expand_stdlib" in body  # Next: hint


def test_render_no_collapsed_annotation_when_expand_stdlib() -> None:
    """When `collapsed=False` (i.e. user passed expand_stdlib=True),
    neither the header flag nor the Next: hint mentions it."""
    events = (
        _ev("call", "a.main", 0.0),
        _ev("return", "a.main", 0.001),
    )
    result = rtrace.TraceResult(
        ok=True, events=events, truncated=False, exit_code=0, elapsed_s=0.001
    )
    tree = rtrace.build_tree(events)
    body = rtrace.render_runtrace(
        alias="r", entry="a:main", argv=[], result=result, tree=tree, collapsed=False
    )
    assert "stdlib collapsed" not in body
    assert "expand_stdlib" not in body


# ---------------------------------------------------------------------------
# Handler integration: max_events + expand_stdlib via args=
# ---------------------------------------------------------------------------


@pytest.mark.xfail(_PY312_SETPROFILE_BUG, reason=_RUNTRACE_XFAIL_REASON, strict=False)
def test_runtrace_collapses_stdlib_by_default(repo: Path, gate_on: None) -> None:
    """End-to-end: argparse-heavy entry should NOT show every
    argparse internal in the default rendered output."""
    pkg = repo / "demopkg"
    _write(
        pkg / "argparse_user.py",
        """
        import argparse

        def main() -> None:
            p = argparse.ArgumentParser()
            p.add_argument('--flag')
            p.parse_args([])
        """,
    )
    handler = PythonHandler(hub=Hub(), roots={"r": pkg})
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.argparse_user:main",
        timeout=5,
    )
    body = out.body
    # Header flag set.
    assert "stdlib collapsed" in body
    # Collapse annotation appears at least once.
    assert "stdlib)" in body
    # Recovery hint.
    assert "expand_stdlib" in body


@pytest.mark.xfail(_PY312_SETPROFILE_BUG, reason=_RUNTRACE_XFAIL_REASON, strict=False)
def test_runtrace_expand_stdlib_keeps_full_tree(repo: Path, gate_on: None) -> None:
    """With `expand_stdlib=True`, deep argparse internals are visible
    and the collapse annotations disappear."""
    pkg = repo / "demopkg"
    _write(
        pkg / "argparse_user.py",
        """
        import argparse

        def main() -> None:
            p = argparse.ArgumentParser()
            p.add_argument('--flag')
            p.parse_args([])
        """,
    )
    handler = PythonHandler(hub=Hub(), roots={"r": pkg})
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.argparse_user:main",
        timeout=5,
        expand_stdlib=True,
    )
    body = out.body
    assert "stdlib collapsed" not in body
    assert "(+" not in body or "stdlib)" not in body  # no collapse annotation
    # An argparse-internal qualname now appears that the default run hid.
    assert "argparse._ActionsContainer" in body or "argparse.Action" in body


def test_runtrace_max_events_validation(handler: PythonHandler, gate_on: None) -> None:
    """`max_events` must be a positive int within the documented range."""
    with pytest.raises(BadInput, match="max_events must be"):
        handler.get(id="r", view="runtrace", entry="demopkg.m:main", max_events=0)
    with pytest.raises(BadInput, match="max_events must be"):
        handler.get(
            id="r", view="runtrace", entry="demopkg.m:main", max_events=2_000_000
        )


@pytest.mark.xfail(_PY312_SETPROFILE_BUG, reason=_RUNTRACE_XFAIL_REASON, strict=False)
def test_runtrace_max_events_truncates(repo: Path, gate_on: None) -> None:
    """A tight `max_events` cap surfaces as `truncated` in the output."""
    pkg = repo / "demopkg"
    _write(
        pkg / "loop_lots.py",
        """
        def helper() -> int:
            return 1

        def main() -> int:
            total = 0
            for _ in range(500):
                total += helper()
            return total
        """,
    )
    handler = PythonHandler(hub=Hub(), roots={"r": pkg})
    out = handler.get(
        id="r",
        view="runtrace",
        entry="demopkg.loop_lots:main",
        timeout=5,
        max_events=10,  # intentionally tiny
    )
    assert "truncated" in out.body


# ---------------------------------------------------------------------------
# KindSpec advertises runtrace
# ---------------------------------------------------------------------------


def test_runtrace_view_in_kind_spec() -> None:
    assert "runtrace" in PythonHandler.spec.views
