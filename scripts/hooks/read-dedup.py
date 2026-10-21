#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook (matcher: Read) — refuse to re-serve an unchanged file.

Evidence (gr499044, 7d across 1,245 sessions): 1,668 re-read events re-served
6.7 MB verbatim — one /private/tmp scratch file read 104x in a single session,
pcb/realize.py 38x, pyproject.toml (54 KB) read whole in several sessions. The
body of an unchanged file is already in context; re-serving it is pure spend.

What this does, per session (state: one JSON file under the temp dir keyed by
``session_id`` — ``PRECIS_READ_DEDUP_DIR`` overrides the dir, tests use that):

* A repeat Read of the same file with the same ``offset``/``limit`` whose
  mtime+size are unchanged since the recorded read is DENIED with a one-line
  stub ("unchanged since your read at <time>; it is still in your context —
  use offset/limit for a part").
* The FIRST full Read (no offset/limit) of a file over ``LARGE_BYTES``
  (30 KB) passes through with a non-blocking ``additionalContext`` hint:
  fetch a part with offset/limit, or query the python/md kind for code/docs.

Never blocks: a first read, a read after the file changed (mtime or size
moved), a ranged read of a new range, or an image/PDF/binary read (suffix or
magic-byte sniff — those render, they don't re-serve text). Missing
``session_id``, an unreadable/vanished file, a directory, malformed input or
any internal error all allow: a dedup hook that errs closed would corrupt
work, so every doubt resolves to allow. ``PRECIS_READ_DEDUP_OFF=1`` disables
it outright.

Self-contained (stdlib only): the same file runs from the repo at
``scripts/hooks/read-dedup.py`` and globally from ``~/.claude/hooks/``.
Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Read``).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime

LARGE_BYTES = 30 * 1024

# Read renders these rather than serving text — dedup must not touch them.
_BINARY_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
    ".ico",
    ".pdf",
)
_MAGICS = (
    b"\x89PNG",
    b"\xff\xd8\xff",  # JPEG
    b"GIF8",
    b"RIFF",  # WEBP/WAV/AVI
    b"%PDF",
    b"BM",
    b"PK\x03\x04",  # zip/jar/docx
    b"\x1f\x8b",  # gzip
    b"\x7fELF",
    b"SQLite format 3",
)

_FULL = ":"  # range key when neither offset nor limit is set


def _state_path(session: str) -> str:
    safe = "".join(c for c in session if c.isalnum() or c in "-_")
    base = os.environ.get("PRECIS_READ_DEDUP_DIR") or tempfile.gettempdir()
    return os.path.join(base, f"precis-readdedup-{safe}.json")


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: str, state: dict) -> None:
    try:
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _range_key(tool_input: dict) -> str:
    return f"{tool_input.get('offset') or ''}:{tool_input.get('limit') or ''}"


def evaluate(
    state: dict,
    path: str,
    range_key: str,
    mtime_ns: int,
    size: int,
    now: float,
) -> tuple[str | None, str | None]:
    """Update ``state`` for this read; return (deny_reason, hint). Pure.

    ``state`` maps ``abspath -> {range_key -> {"m", "s", "at"}}``. An
    unchanged repeat of a recorded (path, range) denies; anything else is
    recorded and allowed, with a hint when it is the session's first full
    read of a file over ``LARGE_BYTES``.
    """
    per_file = state.setdefault(path, {})
    prev = per_file.get(range_key)
    if prev and prev.get("m") == mtime_ns and prev.get("s") == size:
        at = datetime.fromtimestamp(prev.get("at", now), UTC).strftime("%H:%MZ")
        return (
            f"unchanged since your read at {at}; it is still in your context — "
            "use offset/limit for a part",
            None,
        )
    first_full = range_key == _FULL and _FULL not in per_file
    per_file[range_key] = {"m": mtime_ns, "s": size, "at": now}
    if first_full and size > LARGE_BYTES:
        return None, (
            f"[read-dedup] {path} is {size // 1024}KB — if you only need part "
            "of it, Read with offset/limit (or query the python/md kind for "
            "code/docs) rather than serving it whole."
        )
    return None, None


def _is_rendered_binary(path: str) -> bool | None:
    """True: image/PDF/binary (skip). False: text. None: cannot tell → allow.

    A file that cannot be opened for a 512-byte sniff will fail the Read
    anyway; returning ``None`` keeps us from recording a read that never
    happened (a phantom record would deny the first REAL read).
    """
    if path.lower().endswith(_BINARY_SUFFIXES):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    return b"\0" in head or any(head.startswith(m) for m in _MAGICS)


def _deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def _hint(note: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": note,
                }
            }
        )
    )


def _run(session: str, path: str, tool_input: dict) -> int:
    if path.lower().endswith(_BINARY_SUFFIXES):
        return 0
    try:
        st = os.stat(path)
    except OSError:
        return 0  # gone/unstat-able — Read reports that itself
    if not stat.S_ISREG(st.st_mode):
        return 0
    binary = _is_rendered_binary(path)
    if binary is not False:
        return 0

    state_file = _state_path(session)
    state = _load(state_file)
    deny, hint = evaluate(
        state,
        os.path.abspath(path),
        _range_key(tool_input),
        st.st_mtime_ns,
        st.st_size,
        time.time(),
    )
    if deny:
        _deny(deny)
        return 0
    _save(state_file, state)
    if hint:
        _hint(hint)
    return 0


def main() -> int:
    if os.environ.get("PRECIS_READ_DEDUP_OFF"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Read":
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return 0
    session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not session:
        return 0
    try:
        return _run(str(session), path, tool_input)
    except Exception:  # a dedup hook fails OPEN, never closed
        return 0


if __name__ == "__main__":
    sys.exit(main())
