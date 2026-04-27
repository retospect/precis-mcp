"""Write helpers for `precis.handlers.python.PythonHandler`.

Three independent concerns split out so the handler stays readable:

1. **Atomic write** (`atomic_write`) — tmpfile + fsync + os.replace +
   directory fsync. Concurrent readers see old XOR new bytes, never a
   torn mix; on crash the original file is intact (the tmpfile may
   linger, cleaned up at startup).

2. **Ruff** (`run_ruff`) — runs ``ruff check --fix --exit-zero`` then
   ``ruff format``, both via stdin so we never write the unfixed
   buffer to disk. Returns the canonical text + a `RuffChanges`
   summary the response renderer surfaces. If the ruff binary is
   missing, returns the input unchanged with `ok=False` so the caller
   can warn but not block.

3. **Gates** (`gate_ast`, `gate_qualnames`) — pure functions over the
   pre/post buffers; no I/O. The handler calls them in order and
   reverts on failure.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` atomically and durably.

    Steps (per spec § Atomic-immediate write semantics):

    1. Create a tmpfile in the same directory (so ``os.replace`` is
       guaranteed atomic on the same filesystem).
    2. ``fsync`` the tmpfile so its bytes hit physical storage.
    3. ``os.replace`` — single inode swap; readers see exactly old or
       exactly new, never a partial mix.
    4. ``fsync`` the parent directory so the rename itself is durable
       on crash. (Skipped silently on platforms where directory fds
       can't be opened — e.g. Windows.)

    Creates parent directories as needed. Encoding is UTF-8.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_str = tempfile.mkstemp(
        dir=str(parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:  # pragma: no cover — non-fatal
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    # fsync the parent directory so the rename is durable.
    try:
        dir_fd = os.open(str(parent), os.O_DIRECTORY)
    except OSError:  # pragma: no cover — Windows / unusual FS
        return
    try:
        os.fsync(dir_fd)
    except OSError:  # pragma: no cover
        pass
    finally:
        os.close(dir_fd)


# ---------------------------------------------------------------------------
# Ruff
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuffChanges:
    """Summary of what ruff did to a buffer.

    `ok` is False only when ruff itself failed (missing binary, crash).
    Lint findings that ruff couldn't fix are surfaced via
    `unfixable_findings` but do NOT block the write — they're a hint
    to the agent.
    """

    ok: bool
    fix_changed: bool  # check --fix produced different bytes
    format_changed: bool  # format produced different bytes
    unfixable_findings: tuple[str, ...] = ()
    error: str | None = None  # populated when ok=False

    @property
    def changed(self) -> bool:
        return self.fix_changed or self.format_changed


def run_ruff(content: str, target_path: Path) -> tuple[str, RuffChanges]:
    """Run ``ruff check --fix --exit-zero`` then ``ruff format`` on `content`.

    Both invocations stream through stdin with ``--stdin-filename
    target_path`` so ruff picks up the project's config (pyproject /
    ruff.toml) walking up from `target_path`. Two subprocess starts;
    ~70 ms total for a typical file.

    Returns ``(canonical_text, RuffChanges)``. The canonical text is
    what should hit disk. If ruff is missing or crashes,
    ``RuffChanges.ok`` is False and the input is returned verbatim.
    """
    ruff = _find_ruff()
    if ruff is None:
        return content, RuffChanges(
            ok=False,
            fix_changed=False,
            format_changed=False,
            error="ruff binary not on PATH",
        )

    target_str = str(target_path)
    try:
        # 1) check --fix --exit-zero — apply safe autofixes; stderr
        #    carries unfixable findings (one per line in default fmt).
        fix_result = subprocess.run(
            [
                ruff,
                "check",
                "--fix",
                "--exit-zero",
                "--stdin-filename",
                target_str,
                "-",
            ],
            input=content,
            capture_output=True,
            text=True,
            check=False,
        )
        fixed = fix_result.stdout
        unfixable = _parse_ruff_findings(fix_result.stderr)
        fix_changed = fixed != content

        # 2) format — pure layout pass. Should never fail in practice
        #    on AST-valid input; gate 1 already validated.
        fmt_result = subprocess.run(
            [ruff, "format", "--stdin-filename", target_str, "-"],
            input=fixed,
            capture_output=True,
            text=True,
            check=False,
        )
        if fmt_result.returncode != 0:
            return content, RuffChanges(
                ok=False,
                fix_changed=fix_changed,
                format_changed=False,
                unfixable_findings=unfixable,
                error=f"ruff format exit {fmt_result.returncode}: "
                f"{fmt_result.stderr.strip()[:200]}",
            )
        formatted = fmt_result.stdout
        format_changed = formatted != fixed

        return formatted, RuffChanges(
            ok=True,
            fix_changed=fix_changed,
            format_changed=format_changed,
            unfixable_findings=unfixable,
        )
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover
        return content, RuffChanges(
            ok=False,
            fix_changed=False,
            format_changed=False,
            error=f"ruff invocation failed: {e}",
        )


def _find_ruff() -> str | None:
    """Locate the `ruff` binary.

    Search order:
    1. ``shutil.which('ruff')`` — system PATH.
    2. The ``bin``/``Scripts`` dir next to ``sys.executable`` — covers
       venv-installed `ruff` even when the venv isn't activated for the
       precis process.

    Returns the absolute path or None.
    """
    found = shutil.which("ruff")
    if found:
        return found
    exe_dir = Path(sys.executable).parent
    for candidate in (exe_dir / "ruff", exe_dir / "ruff.exe"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


_RUFF_FINDING_RE = re.compile(r"^[^:]+:\d+:\d+: (\S+) (.+)$")


def _parse_ruff_findings(stderr: str) -> tuple[str, ...]:
    """Extract `RULE message` lines from ruff stderr.

    ``ruff check`` emits findings as ``path:line:col: RULE message`` on
    stdout when not `--fix`-ing, and on stderr in `--fix` mode for the
    items it could not auto-correct. Best-effort regex; if format
    drifts we just lose the summary, not block the write.
    """
    findings: list[str] = []
    for line in stderr.splitlines():
        m = _RUFF_FINDING_RE.match(line.strip())
        if m:
            findings.append(f"{m.group(1)} {m.group(2)}")
    return tuple(findings[:5])  # cap so the response stays short


# ---------------------------------------------------------------------------
# Gate 1 — AST parse
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    detail: str = ""
    dropped: tuple[str, ...] = field(default_factory=tuple)


def gate_ast(content: str, *, filename: str = "<post-edit>") -> GateResult:
    """Pass iff `ast.parse(content)` succeeds. Failure carries the
    SyntaxError message."""
    try:
        ast.parse(content, filename=filename)
    except SyntaxError as e:
        return GateResult(
            ok=False, detail=f"{type(e).__name__}: {e.msg} (line {e.lineno})"
        )
    return GateResult(ok=True)


# ---------------------------------------------------------------------------
# Static qualname extraction (pure-text variant; no file I/O)
# ---------------------------------------------------------------------------


def qualnames_in_text(text: str, *, module_qualname: str) -> set[str]:
    """Static qualnames for every class / function / method in `text`,
    parsed as if it were the module named `module_qualname`.

    Walks class bodies (so methods + nested classes are seen) but NOT
    function bodies — locally-defined helpers are not symbols. Mirrors
    the indexer's `_SymbolVisitor` discipline; the two should agree on
    what counts as a top-level qualname.

    Returns ``set()`` on `SyntaxError` (caller's gate 1 will catch
    that separately and produce a sharper error).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    found: set[str] = set()

    def _walk(node: ast.AST, parent_qn: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                qn = f"{parent_qn}.{child.name}"
                found.add(qn)
                if isinstance(child, ast.ClassDef):
                    _walk(child, qn)
                # Function bodies: not walked — locals are not symbols.

    _walk(tree, module_qualname)
    return found


# ---------------------------------------------------------------------------
# Gate 2 — no-qualname-drop
# ---------------------------------------------------------------------------


def gate_qualnames(
    *,
    pre_in_region: set[str],
    post_in_file: set[str],
    allow_rename: bool,
) -> GateResult:
    """Pass iff every qualname that was in the addressed region before
    is still somewhere in the file after, OR `allow_rename=True`.

    This catches:
    - **Accidental rename** — replacing a method with one named
      differently leaves the old qualname unreachable.
    - **Accidental drop** — replacing a class with a body that forgets
      one of its methods.

    A qualname *moving* within the file is OK (the post-set is
    file-wide).
    """
    if allow_rename:
        return GateResult(ok=True, detail="allow_rename=True")
    dropped = pre_in_region - post_in_file
    if dropped:
        return GateResult(
            ok=False,
            detail=(
                f"{len(dropped)} qualname{'s' if len(dropped) != 1 else ''} "
                f"would disappear from the file"
            ),
            dropped=tuple(sorted(dropped)),
        )
    return GateResult(ok=True)


# ---------------------------------------------------------------------------
# Region splice helpers
# ---------------------------------------------------------------------------


def splice_lines(
    original: str,
    *,
    start_line: int,
    end_line: int,
    replacement: str,
) -> str:
    """Replace lines [start_line, end_line] (1-indexed inclusive) with
    `replacement` and return the new content.

    ``replacement=''`` deletes the range. Trailing newline behavior:
    we preserve the file's trailing newline if it had one. Replacement
    text is appended verbatim — caller controls indentation per spec
    § Indentation strict.
    """
    if start_line < 1 or end_line < start_line:
        raise ValueError(
            f"invalid line range {start_line}..{end_line} (must be 1-indexed, end >= start)"
        )

    lines = original.splitlines(keepends=True)
    n = len(lines)
    # Clamp so we don't index past EOF (callers may have stale ranges).
    lo = min(start_line - 1, n)
    hi = min(end_line, n)

    # Make sure the replacement, if non-empty, ends with a newline so
    # the line count stays sane after splice. Empty string is fine.
    if replacement and not replacement.endswith("\n"):
        replacement = replacement + "\n"

    new = "".join(lines[:lo]) + replacement + "".join(lines[hi:])
    return new
