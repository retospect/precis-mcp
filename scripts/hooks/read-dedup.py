#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook (matchers: Read and Bash) — refuse to re-serve an
unchanged file.

Evidence (gr499044, 7d across 1,245 sessions): 1,668 re-read events re-served
6.7 MB verbatim — one /private/tmp scratch file read 104x in a single session,
pcb/realize.py 38x, pyproject.toml (54 KB) read whole in several sessions. The
body of an unchanged file is already in context; re-serving it is pure spend.
The 2026-10-22 12 h surface review found the dodge: ~5 MB/12 h of whole-file
``cat``/``sed -n '1,Np'``/``git show <sha>:<path>`` through Bash, which no
Read-matcher hook can see (~2.5M tokens/day).

What this does, per session (state: one JSON file under the temp dir keyed by
``session_id`` — ``PRECIS_READ_DEDUP_DIR`` overrides the dir, tests use that;
a call carrying ``agent_id`` is skipped outright — a subagent's serves land
in its own context, never the parent's transcript):

* A repeat Read of the same file with the same ``offset``/``limit`` whose
  mtime+size are unchanged since the recorded read is DENIED with a one-line
  stub ("unchanged since it was served at <time> — still in your context").
* An allowed Read with no ``limit`` of a file over ``WARN_BYTES`` (20 KB)
  passes through with a non-blocking ``additionalContext`` hint — every
  time, not just the first: fetch a part with offset/limit, or query the
  python/md kind for code/docs.
* A Bash command that serves a file to the transcript is matched on the
  same ledger, so a ``cat`` after a ``Read`` (and vice versa) dedups too:
  ``cat <path>``, ``sed -n '<a>,<b>p' <path>`` (mapped onto Read's
  offset/limit range key — ``sed -n p``/``'N,$p'`` are unbounded),
  ``head``/``tail -n N`` (``tail -n +K`` ≡ Read ``offset=K``; ``tail -f``
  is a stream, never a serve), and ``git show <spec>`` — resolved to the
  blob/commit sha via ``git rev-parse`` so a repeat is denied across
  spellings (``main:x`` ≡ ``<sha>:x``). An unbounded serve of a file over
  ``WARN_BYTES`` is DENIED outright — Read is a strict superset, and the
  denial names it. A dumper feeding a pipe (``cat log | grep err``) is
  never recorded — its bytes were filtered, not served — but is denied
  when the file is over ``WARN_BYTES``: ``rg <pat> <file>`` filters in
  place without pulling the file through the pipe.

Two traps the first cut hit (witty review, 2026-10-21):

* ``tool_use_id``. Registered globally AND per-project, the hook fires twice
  per Read on the same state file — the second run then denied the FIRST
  read. Each record carries the call's ``tool_use_id``; a repeat with the
  same id is the same call seen twice, not a re-read, so it allows.
* Compaction. ``/compact``, ``/clear`` and resume drop file bodies from
  context while the session's state file survives — the stub's "still in
  your context" becomes a lie. Run with ``--session-start`` (wired as a
  SessionStart hook) the script deletes the session's state file when the
  payload's ``source`` is ``compact``/``clear``/``resume``.

Never blocks: a first read, a read after the file changed (mtime or size
moved), a ranged read of a new range, or an image/PDF/binary read (suffix or
magic-byte sniff — those render, they don't re-serve text). Missing
``session_id``, an unreadable/vanished file, a directory, a ``cd`` chain we
can't resolve, a flag/glob/substitution we can't parse, a ``git show``
``rev-parse`` can't pin, malformed input or any internal error all allow: a
dedup hook that errs closed would corrupt work, so every doubt resolves to
allow. ``PRECIS_READ_DEDUP_OFF=1`` disables it outright.

Self-contained (stdlib only): the same file runs from the repo at
``scripts/hooks/read-dedup.py`` and globally from ``~/.claude/hooks/``.
Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Read``;
SessionStart, ``--session-start``). The Bash side reaches sessions two ways:
``bash-reflex-nudge.py`` — already on the Bash matcher — delegates every
payload to :func:`hook_output`, and the script can also be listed directly
under the ``Bash`` matcher (the ``tool_use_id`` record makes a double
registration safe).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime

WARN_BYTES = 20 * 1024

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
    tool_use_id: str | None = None,
    *,
    unbounded: bool = False,
    stub_tail: str = "use Read with offset/limit for a part",
) -> tuple[str | None, str | None]:
    """Update ``state`` for this serve; return (deny_reason, hint). Pure.

    ``state`` maps ``key -> {range_key -> {"m", "s", "at", "tu"}}`` where
    ``key`` is an abspath or a ``gitshow:<sha>`` marker. An unchanged repeat
    of a recorded (key, range) denies — unless the record carries THIS
    call's ``tool_use_id``: the hook can be registered twice (global +
    project), and the second fire of the same call must pass. Anything else
    is recorded and allowed, with a hint when an UNBOUNDED serve is over
    ``WARN_BYTES`` (every time — the 2026-10-22 review asked for the warn
    on any >20 KB read with no limit, not just the first).
    """
    per_file = state.setdefault(path, {})
    prev = per_file.get(range_key)
    if prev and prev.get("m") == mtime_ns and prev.get("s") == size:
        if tool_use_id and prev.get("tu") == tool_use_id:
            return None, None  # same call, second registration firing
        at = datetime.fromtimestamp(prev.get("at", now), UTC).strftime("%H:%MZ")
        return (
            f"unchanged since it was served at {at}; it is still in your "
            f"context — {stub_tail}",
            None,
        )
    per_file[range_key] = {"m": mtime_ns, "s": size, "at": now, "tu": tool_use_id}
    if unbounded and size > WARN_BYTES:
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


def _deny(reason: str) -> dict:
    return {"permissionDecision": "deny", "permissionDecisionReason": reason}


def _hint(note: str) -> dict:
    return {"additionalContext": note}


def _run_read(
    session: str, path: str, tool_input: dict, tool_use_id: str | None
) -> dict | None:
    if path.lower().endswith(_BINARY_SUFFIXES):
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None  # gone/unstat-able — Read reports that itself
    if not stat.S_ISREG(st.st_mode):
        return None
    if _is_rendered_binary(path) is not False:
        return None

    state_file = _state_path(session)
    state = _load(state_file)
    deny, hint = evaluate(
        state,
        os.path.abspath(path),
        _range_key(tool_input),
        st.st_mtime_ns,
        st.st_size,
        time.time(),
        tool_use_id,
        unbounded=tool_input.get("limit") is None,
    )
    if deny:
        return _deny(deny)
    _save(state_file, state)
    return _hint(hint) if hint else None


# ── Bash mode ─────────────────────────────────────────────────────────
# Command-separator split (pipes stay inside a segment — they change a
# serve's meaning, see ``filtered``).
_SEG_BREAK = re.compile(r"&&|\|\||[;\n]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# sed -n '<expr>' shapes that are pure slices — a substitution/script
# prints bytes a Read would not, so it is never treated as a serve.
_SED_EXPR = re.compile(r"^(?:(\d+)(?:,(\d+|\$))?)?\s*p$")
# Path tokens we cannot resolve to one file: globs, substitutions, braces.
_PATH_UNSAFE = re.compile(r"[$`{}\[\]*?]")
# ``> out`` redirects on a stage — stripped before shlex so ``cd x 2>/dev``
# still parses. Only an STDOUT redirect (``>``, ``>>``, ``&>``, ``1>``)
# keeps a dumper's bytes out of the transcript; ``2>`` still serves, and
# ``cat < f`` serves f through stdin.
_REDIR_STRIP = re.compile(r"\s*\d*>+\s*\S+")
_STDOUT_REDIR = re.compile(r"(?:^|\s)(?:&>>?|1?>+)")


def _clean_path(tok: str, base: str) -> str | None:
    """Resolve a path token against ``base``; None when it isn't a literal."""
    if _PATH_UNSAFE.search(tok):
        return None
    p = os.path.expanduser(tok)
    if not os.path.isabs(p):
        p = os.path.join(base, p)
    return os.path.abspath(p)


def _file_serve(
    desc: str, path: str, range_key: str, unbounded: bool, filtered: bool
) -> dict:
    return {
        "kind": "file",
        "desc": desc,
        "path": path,
        "range_key": range_key,
        "unbounded": unbounded,
        "filtered": filtered,
    }


def _sed_serves(verb: str, args: list[str], base: str, filtered: bool) -> list[dict]:
    """``sed -n '<a>,<b>p' <files>`` — mapped onto Read's offset/limit key."""
    if not args or not args[0].startswith("-n"):
        return []
    if args[0] == "-n":
        if len(args) < 2:
            return []
        expr, rest = args[1], args[2:]
    else:  # glued: ``sed -n1,5p f``
        expr, rest = args[0][2:], args[1:]
    m = _SED_EXPR.match(expr)
    if not m or any(t.startswith("-") for t in rest):
        # -e/-i/--expression or another flag: the output isn't the slice.
        return []
    a, b = m.group(1), m.group(2)
    if a is None:  # 'p' — the whole file
        range_key, unbounded = _FULL, True
    elif b is None:  # 'Np'
        range_key, unbounded = f"{a}:1", False
    elif b == "$":  # 'N,$p' — line N to EOF
        range_key = _FULL if a == "1" else f"{a}:"
        unbounded = True
    else:
        if int(b) < int(a):
            return []
        range_key, unbounded = f"{a}:{int(b) - int(a) + 1}", False
    return [
        _file_serve(f"{verb} -n '{expr}' {t}", p, range_key, unbounded, filtered)
        for t in rest
        if (p := _clean_path(t, base)) is not None
    ]


def _headtail_serves(
    verb: str, args: list[str], base: str, filtered: bool
) -> list[dict]:
    """``head``/``tail`` of literal paths. ``tail -f`` is a stream — skip."""
    paths: list[str] = []
    count: str | None = None  # -n / -N value
    plus: str | None = None  # tail -n +K — serves lines K..EOF
    byte_n: str | None = None  # -c value
    i = 0
    while i < len(args):
        t = args[i]
        if t in ("-f", "-F") or t.startswith("--follow"):
            return []  # a live stream, not a serve
        if t in ("-n", "-c", "--lines", "--bytes"):
            i += 1
            if i >= len(args) or not re.fullmatch(r"\+?\d+", args[i]):
                return []
            v = args[i]
            if t in ("-n", "--lines"):
                if v.startswith("+"):
                    plus = v[1:]
                else:
                    count = v
            else:
                byte_n = v
        elif t in (
            "-q",
            "-v",
            "-z",
            "--quiet",
            "--verbose",
            "--silent",
            "--zero-terminated",
        ):
            pass  # decorations that don't change which bytes are served
        elif (
            m := re.fullmatch(r"-([nc])\+?(\d+)", t)
        ) is not None:  # -n5 / -n+5 / -c500
            if m.group(1) == "n":
                if "+" in t:
                    plus = m.group(2)
                else:
                    count = m.group(2)
            else:
                byte_n = m.group(2)
        elif (m := re.fullmatch(r"-\+?(\d+)", t)) is not None:  # head -50 / tail -5
            count = m.group(1)
        elif (m := re.fullmatch(r"\+(\d+)", t)) is not None:  # tail +5 (old form)
            plus = m.group(1)
        elif (m := re.fullmatch(r"--(lines|bytes)=\+?(\d+)", t)) is not None:
            if m.group(1) == "lines":
                if "+" in t:
                    plus = m.group(2)
                else:
                    count = m.group(2)
            else:
                byte_n = m.group(2)
        elif t.startswith("-") and t != "-":
            return []  # an unknown flag: stay silent rather than guess
        else:
            paths.append(t)
        i += 1
    if not paths:
        return []
    if verb == "head":
        range_key = f"head-c:{byte_n}" if byte_n is not None else f"1:{count or '10'}"
        unbounded = False
    elif byte_n is not None:
        range_key, unbounded = f"tail-c:{byte_n}", False
    elif plus is not None:  # tail +K / tail -n +K — from line K to EOF
        range_key, unbounded = (_FULL if plus == "1" else f"{plus}:"), True
    else:
        range_key, unbounded = f"tail:{count or '10'}", False
    return [
        _file_serve(f"{verb} {t}", p, range_key, unbounded, filtered)
        for t in paths
        if (p := _clean_path(t, base)) is not None
    ]


def _git_serves(args: list[str], filtered: bool) -> list[dict]:
    """``git show <spec>`` — one object serve; flagged/multi-spec forms skip."""
    if not args or args[0] != "show" or filtered:
        return []
    rest = args[1:]
    if any(t.startswith("-") for t in rest) or len(rest) > 1:
        return []
    spec = rest[0] if rest else "HEAD"
    if _PATH_UNSAFE.search(spec):
        return []
    return [{"kind": "git", "spec": spec, "desc": f"git show {spec}"}]


def _bash_serves(command: str, cwd: str) -> list[dict]:
    """The file/object serves inside ``command`` — fail open on any doubt.

    A segment split on ``&&``/``||``/``;``/newline is split again on ``|``;
    a dumper in a non-final pipeline stage is ``filtered`` (its bytes fed
    the next stage, not the transcript), in the final stage it is a direct
    serve. A leading ``cd <dir>`` segment rebases relative paths — the
    payload's ``cwd`` anchors the first segment. A stage whose stdout is
    redirected (``>``/``1>``/``&>`` — ``2>`` still serves) and stages we
    can't parse are skipped rather than guessed.
    """
    serves: list[dict] = []
    base = cwd or os.getcwd()
    for segment in _SEG_BREAK.split(command):
        stages = segment.split("|")
        for i, raw in enumerate(stages):
            stage = _REDIR_STRIP.sub("", raw.strip())
            if not stage:
                continue
            try:
                words = shlex.split(stage)
            except ValueError:
                continue
            while words and _ENV_ASSIGN.match(words[0]):
                words = words[1:]
            if not words:
                continue
            verb = words[0].rsplit("/", 1)[-1]
            args = words[1:]
            filtered = i < len(stages) - 1
            if verb == "cd":
                # A `cd` rebases only as a standalone segment — inside a
                # pipeline it runs in a subshell and leaves cwd untouched.
                if len(args) == 1 and len(stages) == 1:
                    base = _clean_path(args[0], base) or base
                continue
            if _STDOUT_REDIR.search(raw):
                continue  # stdout went to a file/fd, not the transcript
            if verb == "cat":
                for t in args:
                    if t.startswith("-"):
                        continue  # flags and stdin '-'
                    if (p := _clean_path(t, base)) is not None:
                        serves.append(_file_serve(f"cat {t}", p, _FULL, True, filtered))
            elif verb == "sed":
                serves += _sed_serves(verb, args, base, filtered)
            elif verb in ("head", "tail"):
                serves += _headtail_serves(verb, args, base, filtered)
            elif verb == "git":
                serves += _git_serves(args, filtered)
    return serves


def _resolve_git(spec: str, cwd: str) -> str | None:
    """The object sha ``git show <spec>`` would serve, or None — fail open."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", spec],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd or None,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _run_bash(
    session: str, command: str, cwd: str, tool_use_id: str | None
) -> dict | None:
    serves = _bash_serves(command, cwd)
    if not serves:
        return None
    state_file = _state_path(session)
    state = _load(state_file)
    now = time.time()
    for s in serves:
        if s["kind"] == "git":
            sha = _resolve_git(s["spec"], cwd)
            if sha is None:
                continue
            deny, _ = evaluate(
                state,
                f"gitshow:{sha}",
                _FULL,
                0,
                0,
                now,
                tool_use_id,
                stub_tail="it's the same immutable object — narrow it with "
                "`git show --stat` or a `<rev>:<path>` you haven't served",
            )
            if deny:
                return _deny(deny)
            continue
        path = s["path"]
        try:
            st = os.stat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if _is_rendered_binary(path) is not False:
            continue
        if s["filtered"]:
            # Its bytes fed the next pipe stage — never recorded. Denied
            # only when the file is large: `rg <pat> <file>` is the same
            # filter without pulling the file through.
            if st.st_size > WARN_BYTES:
                return _deny(
                    f"[read-dedup] `{s['desc']} | …` pulls {st.st_size // 1024}KB "
                    "of the file through a pipe — filter it in place instead: "
                    f"`rg <pattern> {path}` / `grep <pattern> {path}` "
                    "(or Read it with offset/limit)."
                )
            continue
        deny, _ = evaluate(
            state, path, s["range_key"], st.st_mtime_ns, st.st_size, now, tool_use_id
        )
        if deny:
            return _deny(deny)
        if s["unbounded"] and st.st_size > WARN_BYTES:
            return _deny(
                f"[read-dedup] `{s['desc']}` would serve {st.st_size // 1024}KB "
                "whole through Bash, outside Read's tracking — use the Read "
                f"tool on {path} instead (offset/limit for a part; the "
                "python/md kind answers code/doc questions without the bytes)."
            )
    _save(state_file, state)
    return None


def _session_start(payload: dict) -> int:
    """Drop this session's dedup state after compaction/clear/resume.

    Those events evict file bodies from context, so "still in your context"
    stops being true; the next read must serve again, not be denied.
    ``startup`` keeps state (a fresh session has a fresh id anyway).
    """
    if not isinstance(payload, dict):
        return 0
    if payload.get("source") not in {"compact", "clear", "resume"}:
        return 0
    session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not session:
        return 0
    try:
        os.unlink(_state_path(str(session)))
    except OSError:
        pass
    return 0


def hook_output(payload: dict) -> dict | None:
    """The ``hookSpecificOutput`` fields for a PreToolUse call, or None.

    Exposed so ``bash-reflex-nudge.py`` — already on the Bash matcher — can
    delegate: the Bash side works without a second settings entry, and a
    later direct ``Bash`` registration stays safe via ``tool_use_id``.
    """
    if os.environ.get("PRECIS_READ_DEDUP_OFF"):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("agent_id"):
        # A subagent's serves land in the SUBAGENT's context — recording them
        # under the session ledger then denies the parent's first read with a
        # phantom "still in your context" (gr514450). Skip tracking and
        # dedup alike: the main-loop ledger only describes the main context.
        return None
    session = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID")
    if not session:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    tu = payload.get("tool_use_id")
    tool_use_id = str(tu) if tu else None
    try:
        if payload.get("tool_name") == "Read":
            path = tool_input.get("file_path")
            if not isinstance(path, str) or not path:
                return None
            return _run_read(str(session), path, tool_input, tool_use_id)
        if payload.get("tool_name") == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str) or not command:
                return None
            return _run_bash(
                str(session), command, str(payload.get("cwd") or ""), tool_use_id
            )
    except Exception:  # a dedup hook fails OPEN, never closed
        return None
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if "--session-start" in sys.argv[1:]:
        if os.environ.get("PRECIS_READ_DEDUP_OFF"):
            return 0
        try:
            return _session_start(payload)
        except Exception:  # fail open, same rule
            return 0
    out = hook_output(payload)
    if out is not None:
        print(
            json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **out}})
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
