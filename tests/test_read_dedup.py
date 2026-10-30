"""Tests for ``scripts/hooks/read-dedup.py`` (gr499044).

The hook stops verbatim re-serves: a repeat Read of a file whose mtime+size
are unchanged since this session's read of the same range is denied with a
one-line stub, and any Read with no ``limit`` of a >20KB file earns an
offset/limit hint — every time. On the Bash side a whole-file ``cat`` /
``sed -n`` / ``head`` / ``tail`` / ``git show`` serve joins the same ledger
(cross-tool dedup), an unbounded serve of a >20KB file is denied with a
Read redirect, and a dumper feeding a pipe is denied only when the file is
large. Every test drives the real script over stdin with fake hook-input
JSON (``PRECIS_READ_DEDUP_DIR`` isolates the per-session state file),
because the contract that matters is the wire one: exit 0 always,
deny/hint as JSON on stdout, silence otherwise. A pure-function check of
``evaluate`` covers the same-size-different-mtime edge.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "read-dedup.py"
_TU = itertools.count(1)  # each fake tool call gets a fresh tool_use_id


def _load():
    spec = importlib.util.spec_from_file_location("read_dedup", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load()


def _run(
    tmp_path: Path, payload: dict | str, argv: tuple[str, ...] = ()
) -> dict | None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PRECIS_READ_DEDUP_DIR": str(tmp_path),
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK), *argv],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert proc.returncode == 0
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _read(
    path: Path, session: str = "s1", tool_use_id: str | None = None, **tool_input
) -> dict:
    return {
        "session_id": session,
        "tool_name": "Read",
        "tool_use_id": tool_use_id or f"tu-{next(_TU)}",
        "tool_input": {"file_path": str(path), **tool_input},
    }


def _bash(
    command: str,
    session: str = "s1",
    cwd: str | None = None,
    tool_use_id: str | None = None,
) -> dict:
    return {
        "session_id": session,
        "tool_name": "Bash",
        "cwd": cwd,
        "tool_use_id": tool_use_id or f"tu-{next(_TU)}",
        "tool_input": {"command": command},
    }


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_first_read_is_allowed(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _read(f)) is None


def test_unchanged_repeat_is_denied(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _read(f)) is None
    out = _run(tmp_path, _read(f))
    assert out is not None
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "unchanged since it was served at" in reason
    assert "offset/limit" in reason


def test_read_after_change_is_allowed(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "v1\n")
    assert _run(tmp_path, _read(f)) is None
    _write(f, "v2 — changed\n")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert _run(tmp_path, _read(f)) is None


def test_same_size_change_is_allowed(tmp_path: Path) -> None:
    state: dict = {}
    deny, _ = hook.evaluate(state, "/x", ":", mtime_ns=1, size=10, now=100.0)
    assert deny is None
    deny, _ = hook.evaluate(state, "/x", ":", mtime_ns=2, size=10, now=200.0)
    assert deny is None  # mtime moved → allowed and re-recorded
    deny, _ = hook.evaluate(state, "/x", ":", mtime_ns=2, size=10, now=300.0)
    assert deny is not None  # now it IS an unchanged repeat


def test_new_range_is_allowed_same_range_is_denied(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "line\n" * 100)
    assert _run(tmp_path, _read(f)) is None  # full read recorded
    assert _run(tmp_path, _read(f, offset=1, limit=50)) is None  # new range
    assert _run(tmp_path, _read(f, offset=50, limit=50)) is None  # another new range
    out = _run(tmp_path, _read(f, offset=1, limit=50))  # verbatim range repeat
    assert out is not None and out["permissionDecision"] == "deny"


def test_large_file_first_read_hints_not_blocks(tmp_path: Path) -> None:
    f = tmp_path / "big.md"
    _write(f, "x" * (hook.WARN_BYTES + 1))
    out = _run(tmp_path, _read(f))
    assert out is not None
    assert "additionalContext" in out
    assert "offset/limit" in out["additionalContext"]
    assert "permissionDecision" not in out
    # …and the unchanged repeat still denies afterwards
    out = _run(tmp_path, _read(f))
    assert out is not None and out["permissionDecision"] == "deny"


def test_large_file_hints_on_every_unbounded_read(tmp_path: Path) -> None:
    """gr499044 review: warn on ANY >20KB read with no limit — a re-read
    after a change earns the hint again, and offset-without-limit counts."""
    f = tmp_path / "big.md"
    _write(f, "x" * (hook.WARN_BYTES + 1))
    out = _run(tmp_path, _read(f))
    assert out is not None and "additionalContext" in out
    _write(f, "y" * (hook.WARN_BYTES + 2))  # changed → allowed, hinted again
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    out = _run(tmp_path, _read(f))
    assert out is not None and "additionalContext" in out
    out = _run(tmp_path, _read(f, offset=5))  # offset but no limit → tail
    assert out is not None and "additionalContext" in out


def test_large_file_ranged_read_gets_no_hint(tmp_path: Path) -> None:
    f = tmp_path / "big.md"
    _write(f, "x" * (hook.WARN_BYTES + 1))
    assert _run(tmp_path, _read(f, offset=1, limit=10)) is None


def test_image_and_pdf_reads_never_block(tmp_path: Path) -> None:
    for name, head in (("i.png", b"\x89PNG\r\n\x1a\n"), ("d.pdf", b"%PDF-1.7\n")):
        f = tmp_path / name
        f.write_bytes(head + b"\0" * 20)
        assert _run(tmp_path, _read(f)) is None
        assert _run(tmp_path, _read(f)) is None  # repeat: still allowed


def test_unsniffable_file_is_allowed_and_not_recorded(tmp_path: Path) -> None:
    """A file we cannot open fails the Read anyway — no phantom record."""
    missing = tmp_path / "gone.txt"
    assert _run(tmp_path, _read(missing)) is None  # stat fails → allow
    _write(missing, "now it exists\n")
    assert _run(tmp_path, _read(missing)) is None  # real first read, not denied


def test_same_tool_use_id_twice_is_allowed(tmp_path: Path) -> None:
    """Registered twice (global + project), the hook fires twice per Read —
    the second fire of the SAME call must not deny the first read."""
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _read(f, tool_use_id="tu-1")) is None
    assert _run(tmp_path, _read(f, tool_use_id="tu-1")) is None  # second fire
    out = _run(tmp_path, _read(f, tool_use_id="tu-2"))  # a real repeat
    assert out is not None and out["permissionDecision"] == "deny"


def test_compact_clear_resume_reset_state(tmp_path: Path) -> None:
    """After compaction the body is gone from context — a re-read serves."""
    for source in ("compact", "clear", "resume"):
        sid = f"s-{source}"
        f = tmp_path / "a.txt"
        _write(f, "hello\n")
        assert _run(tmp_path, _read(f, session=sid)) is None
        payload = {"session_id": sid, "source": source}
        assert _run(tmp_path, payload, argv=("--session-start",)) is None
        assert _run(tmp_path, _read(f, session=sid)) is None  # allowed again


def test_startup_source_keeps_state(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _read(f)) is None
    payload = {"session_id": "s1", "source": "startup"}
    assert _run(tmp_path, payload, argv=("--session-start",)) is None
    out = _run(tmp_path, _read(f))
    assert out is not None and out["permissionDecision"] == "deny"


def test_state_is_keyed_by_session(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _read(f, session="s1")) is None
    assert _run(tmp_path, _read(f, session="s2")) is None  # other session: first
    out = _run(tmp_path, _read(f, session="s1"))
    assert out is not None and out["permissionDecision"] == "deny"


def test_bad_input_is_allowed(tmp_path: Path) -> None:
    for payload in (
        "not json at all",
        {},
        {"tool_name": "Bash", "tool_input": {"command": "cat /x"}},  # no session_id
        {"tool_name": "Read", "tool_input": {}},  # no file_path
        {"tool_name": "Read", "tool_input": {"file_path": "/x"}},  # no session_id
        {"tool_name": "Bash", "tool_input": {"command": ""}, "session_id": "s1"},
    ):
        assert _run(tmp_path, payload) is None


def test_non_read_tool_is_ignored(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    payload = _read(f)
    payload["tool_name"] = "Edit"
    assert _run(tmp_path, payload) is None


# ── Bash mode (gr499044 surface-review: cat/sed/git-show dodges) ────────────


def test_cat_repeat_is_denied(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _bash(f"cat {f}")) is None
    out = _run(tmp_path, _bash(f"cat {f}"))
    assert out is not None and out["permissionDecision"] == "deny"
    assert "unchanged since it was served" in out["permissionDecisionReason"]


def test_cat_then_read_and_read_then_cat_dedup(tmp_path: Path) -> None:
    """The ledger is shared: a full serve by EITHER tool dedups the other."""
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _bash(f"cat {f}")) is None
    out = _run(tmp_path, _read(f))
    assert out is not None and out["permissionDecision"] == "deny"
    g = tmp_path / "b.txt"
    _write(g, "world\n")
    assert _run(tmp_path, _read(g)) is None
    out = _run(tmp_path, _bash(f"cat {g}"))
    assert out is not None and out["permissionDecision"] == "deny"


def test_cat_after_change_is_allowed(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "v1\n")
    assert _run(tmp_path, _bash(f"cat {f}")) is None
    _write(f, "v2 — changed\n")
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert _run(tmp_path, _bash(f"cat {f}")) is None


def test_big_cat_is_redirected_to_read(tmp_path: Path) -> None:
    """An unbounded Bash serve over WARN_BYTES is denied outright — Read is
    a strict superset, so the first serve is already the redirect."""
    f = tmp_path / "big.md"
    _write(f, "x" * (hook.WARN_BYTES + 1))
    out = _run(tmp_path, _bash(f"cat {f}"))
    assert out is not None and out["permissionDecision"] == "deny"
    assert "Read" in out["permissionDecisionReason"]
    # and the redirected Read is itself allowed (with the size hint)
    out = _run(tmp_path, _read(f))
    assert out is not None and "additionalContext" in out
    # …after which a repeat cat gets the unchanged-stub, not the redirect
    out = _run(tmp_path, _bash(f"cat {f}"))
    assert out is not None
    assert "unchanged since it was served" in out["permissionDecisionReason"]


def test_sed_slice_dedups_against_read_range(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "line\n" * 100)
    assert _run(tmp_path, _bash(f"sed -n '1,50p' {f}")) is None
    # the same slice via Read maps to the same range key
    out = _run(tmp_path, _read(f, offset=1, limit=50))
    assert out is not None and out["permissionDecision"] == "deny"
    assert _run(tmp_path, _bash(f"sed -n '51,60p' {f}")) is None  # new range
    out = _run(tmp_path, _bash(f"sed -n '51,60p' {f}"))
    assert out is not None and out["permissionDecision"] == "deny"


def test_sed_whole_file_big_is_redirected(tmp_path: Path) -> None:
    f = tmp_path / "big.md"
    _write(f, "x" * (hook.WARN_BYTES + 1))
    for cmd in (f"sed -n '1,$p' {f}", f"sed -n p {f}", f"tail -n +1 {f}"):
        out = _run(tmp_path, _bash(cmd))
        assert out is not None and out["permissionDecision"] == "deny", cmd


def test_head_and_tail_serves_dedup(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "line\n" * 100)
    assert _run(tmp_path, _bash(f"head -n 20 {f}")) is None
    out = _run(tmp_path, _bash(f"head -n 20 {f}"))
    assert out is not None and out["permissionDecision"] == "deny"
    # head -n 20 ≡ Read offset=1 limit=20 — same range key
    out = _run(tmp_path, _read(f, offset=1, limit=20))
    assert out is not None and out["permissionDecision"] == "deny"
    assert _run(tmp_path, _bash(f"tail -n 5 {f}")) is None
    out = _run(tmp_path, _bash(f"tail -n 5 {f}"))
    assert out is not None and out["permissionDecision"] == "deny"


def test_tail_follow_and_unknown_flags_are_silent(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    for cmd in (
        f"tail -f {f}",
        f"tail --follow=name {f}",
        f"head --bogus-flag {f}",
        f"sed -n '1,5p' -e '3d' {f}",
        f"sed -i 's/x/y/' {f}",
        "cat *.txt",
        f"cat {tmp_path}/missing.txt",
        f"cat {f} > {tmp_path}/out.txt",
        f"echo hi > {f}",
    ):
        assert _run(tmp_path, _bash(cmd)) is None, cmd
    # and none of them recorded a serve — a real first serve is allowed
    assert _run(tmp_path, _bash(f"cat {f}")) is None


def test_piped_cat_small_allowed_unrecorded(tmp_path: Path) -> None:
    """`cat f | grep x` serves the filtered output only — nothing to dedup,
    and no phantom record may deny a later real serve."""
    f = tmp_path / "a.txt"
    _write(f, "alpha\nbeta\n")
    assert _run(tmp_path, _bash(f"cat {f} | grep alpha")) is None
    assert _run(tmp_path, _read(f)) is None  # still a first read, not denied


def test_piped_cat_big_is_denied(tmp_path: Path) -> None:
    f = tmp_path / "big.log"
    _write(f, "x\n" * (hook.WARN_BYTES))
    out = _run(tmp_path, _bash(f"cat {f} | grep err"))
    assert out is not None and out["permissionDecision"] == "deny"
    assert "rg" in out["permissionDecisionReason"]


def test_piped_bounded_serve_on_big_file_is_allowed(tmp_path: Path) -> None:
    """gr515526: a bounded stage feeds only its slice into the pipe —
    ``tail -n 20 big | cut`` does not pull the file through, so there is
    nothing to redirect to ``rg``. The whole-file deny is for unbounded
    serves only."""
    f = tmp_path / "big.log"
    _write(f, "x\n" * (hook.WARN_BYTES))
    for cmd in (
        f"tail -n 20 {f} | cut -d' ' -f1",
        f"head -n 5 {f} | wc -l",
        f"sed -n '1,10p' {f} | grep x",
    ):
        assert _run(tmp_path, _bash(cmd)) is None, cmd
    # …but an unbounded serve feeding a pipe is still denied.
    out = _run(tmp_path, _bash(f"tail -n +1 {f} | wc -l"))
    assert out is not None and out["permissionDecision"] == "deny"


def test_stdout_redirected_cat_is_not_a_serve(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _bash(f"cat {f} > /tmp/dedup-copy.txt")) is None
    # `2>` keeps stdout in the transcript — that one IS a serve
    assert _run(tmp_path, _bash(f"cat {f} 2>/dev/null")) is None
    out = _run(tmp_path, _bash(f"cat {f} 2>/dev/null"))
    assert out is not None and out["permissionDecision"] == "deny"


def test_cd_segment_rebases_relative_paths(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    _write(sub / "a.txt", "hello\n")
    cwd = str(tmp_path)
    assert _run(tmp_path, _bash("cd sub && cat a.txt", cwd=cwd)) is None
    out = _run(tmp_path, _bash("cd sub && cat a.txt", cwd=cwd))
    assert out is not None and out["permissionDecision"] == "deny"


def test_subagent_serves_never_reach_the_ledger(tmp_path: Path) -> None:
    """gr514450: a payload carrying ``agent_id`` is a subagent call — its
    serve lands in the subagent's context, so it must neither be denied nor
    recorded. Otherwise the parent's first read of the file is refused with
    a phantom "still in your context"."""
    f = tmp_path / "check.py"
    _write(f, "x = 1\n")
    for tool_call in (_bash(f"cat {f}"), _read(f), _bash(f"cat {f}")):
        tool_call["agent_id"] = "a-sub"
        assert _run(tmp_path, tool_call) is None
    # nothing was recorded: the parent's real first read serves, not denies
    assert _run(tmp_path, _read(f)) is None
    out = _run(tmp_path, _read(f))
    assert out is not None and out["permissionDecision"] == "deny"


def test_subagent_detected_without_agent_id(tmp_path: Path) -> None:
    """gr531241: a ``/subagents/`` transcript marks a subagent even without
    ``agent_id`` — never denied, never recorded."""
    f = tmp_path / "brief.md"
    _write(f, "brief\n")
    assert _run(tmp_path, _read(f)) is None  # the parent's own serve
    for tool_call in (_read(f), _bash(f"cat {f}")):
        tool_call["transcript_path"] = "/x/sess/subagents/agent-1.jsonl"
        assert _run(tmp_path, tool_call) is None


def test_agent_type_alone_keeps_dedup_on(tmp_path: Path) -> None:
    """`claude --agent X` mains carry ``agent_type`` with no ``agent_id``."""
    f = tmp_path / "brief.md"
    _write(f, "brief\n")
    first = _read(f)
    first["agent_type"] = "X"
    assert _run(tmp_path, first) is None
    again = _read(f)
    again["agent_type"] = "X"
    out = _run(tmp_path, again)
    assert out is not None and out["permissionDecision"] == "deny"


def test_same_bash_call_twice_on_tool_use_id_is_allowed(tmp_path: Path) -> None:
    """Registered twice (global + project / nudge-delegate + direct), the
    second fire of the same Bash call must pass."""
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    assert _run(tmp_path, _bash(f"cat {f}", tool_use_id="tu-9")) is None
    assert _run(tmp_path, _bash(f"cat {f}", tool_use_id="tu-9")) is None
    out = _run(tmp_path, _bash(f"cat {f}", tool_use_id="tu-10"))
    assert out is not None and out["permissionDecision"] == "deny"


@pytest.mark.skipif(shutil.which("git") is None, reason="no git binary")
def test_git_show_repeat_is_denied(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    git("init", "-q")
    _write(tmp_path / "a.txt", "hello\n")
    git("add", "a.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    cwd = str(tmp_path)
    for spec in ("HEAD:a.txt", "HEAD"):
        assert _run(tmp_path, _bash(f"git show {spec}", cwd=cwd)) is None
        out = _run(tmp_path, _bash(f"git show {spec}", cwd=cwd))
        assert out is not None and out["permissionDecision"] == "deny", spec


@pytest.mark.skipif(shutil.which("git") is None, reason="no git binary")
def test_git_show_unresolvable_is_silent(tmp_path: Path) -> None:
    cwd = str(tmp_path)
    # not a repo: rev-parse fails → allowed, nothing recorded
    assert _run(tmp_path, _bash("git show HEAD:a.txt", cwd=cwd)) is None
    assert _run(tmp_path, _bash("git show HEAD:a.txt", cwd=cwd)) is None
