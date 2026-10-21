"""Tests for ``scripts/hooks/read-dedup.py`` (gr499044).

The hook stops verbatim re-serves: a repeat Read of a file whose mtime+size
are unchanged since this session's read of the same range is denied with a
one-line stub, and the first full read of a >30KB file earns an offset/limit
hint. Every test drives the real script over stdin with fake hook-input JSON
(``PRECIS_READ_DEDUP_DIR`` isolates the per-session state file), because the
contract that matters is the wire one: exit 0 always, deny/hint as JSON on
stdout, silence otherwise. A pure-function check of ``evaluate`` covers the
same-size-different-mtime edge.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

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
    assert "unchanged since your read at" in reason
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
    _write(f, "x" * (hook.LARGE_BYTES + 1))
    out = _run(tmp_path, _read(f))
    assert out is not None
    assert "additionalContext" in out
    assert "offset/limit" in out["additionalContext"]
    assert "permissionDecision" not in out
    # …and the unchanged repeat still denies afterwards
    out = _run(tmp_path, _read(f))
    assert out is not None and out["permissionDecision"] == "deny"


def test_large_file_ranged_read_gets_no_hint(tmp_path: Path) -> None:
    f = tmp_path / "big.md"
    _write(f, "x" * (hook.LARGE_BYTES + 1))
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
        {"tool_name": "Bash", "tool_input": {"command": "cat /x"}},
        {"tool_name": "Read", "tool_input": {}},  # no file_path
        {"tool_name": "Read", "tool_input": {"file_path": "/x"}},  # no session_id
    ):
        assert _run(tmp_path, payload) is None


def test_non_read_tool_is_ignored(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    _write(f, "hello\n")
    payload = _read(f)
    payload["tool_name"] = "Edit"
    assert _run(tmp_path, payload) is None
