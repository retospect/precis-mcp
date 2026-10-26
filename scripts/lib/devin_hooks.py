"""Generate .devin/hooks.v1.json from .claude/settings.json.

Devin CLI imports Claude-format hooks from ``.claude/settings.json``, but its
importer rejects the WHOLE file on any event name it does not know — one
Claude-only event (``SubagentStart`` landed in f1771b356) silently stripped
every project hook from every Devin session (gr515151). Devin's own
project-level hook file, ``.devin/hooks.v1.json``, is a separate provider:
it loads even while the Claude file fails, so the same hook set is kept as a
generated mirror. ``.devin/config.json`` cannot host it — that file is a
per-worktree generated authority file (scripts/lib/fleet_authority.py),
gitignored and deny-listed.

Subcommands: ``render`` (write .devin/hooks.v1.json), ``check`` /
``render --check`` (diff a fresh render against the checked-in file,
exit 1 with a ``next=`` line on drift). tests/test_devin_hooks.py and the
``scripts/ship --quick`` pre-qland lint run the check so the mirror cannot
drift past a land (gr517403).

What the transform does:

- drops events outside Devin's allow-list (the importer's error message names
  it; DEVIN_EVENTS below);
- rewrites matchers: Claude tool names (``Bash``, ``Edit|Write``…) become the
  Devin equivalents (``exec``, ``edit|write|apply_patch``…) — Devin matchers
  are regexes on the same ``tool_name`` hook stdin carries, so translations
  are anchored ``^(…)$`` to keep e.g. ``read`` from also matching
  ``read_subagent``; ``*`` becomes an omitted matcher (Devin's match-all);
- leaves commands byte-identical: Devin exports ``CLAUDE_PROJECT_DIR`` (and
  ``DEVIN_PROJECT_DIR``) pointing at the workspace root — verified against
  devin 3000.11.205 hook spawns — and parses the Claude-style
  ``hookSpecificOutput``/``permissionDecision`` hook output the scripts emit.

Known gap (out of scope): ``write_to_process``/``kill_shell`` carry no
``command`` field, so the exec matchers cannot see them.

Caveat: if ``.claude/settings.json`` ever becomes Devin-parseable again
(all events inside DEVIN_EVENTS), Devin would import it AND load the
mirror — every hook fires twice. The allow-list drift test makes that
visible; today the fix lives entirely on the Devin side.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / ".claude" / "settings.json"
OUT = REPO / ".devin" / "hooks.v1.json"

#: hooks.v1.json is parsed as STRICT json — a // header line fails the load
#: ("expected value at line 1 column 1"), so the do-not-edit note lives in
#: .gitignore's comment and this module's docstring instead of the file.

#: Event names Devin CLI accepts, verbatim from the importer's rejection
#: message (config_importers::importers::hooks). Anything else in the Claude
#: file is dropped.
DEVIN_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "UserPromptSubmit",
    "Stop",
    "PostCompaction",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SessionEnd",
    "PermissionRequest",
    "Notification",
    "SubagentStop",
}

#: Claude tool name -> Devin tool names. Devin's write side is split across
#: ``write`` (new files) and ``apply_patch`` (edits), so each edit-ish Claude
#: name maps to both. ``NotebookRead`` has no Claude matcher today but maps
#: for completeness; ``mcp__*`` names pass through untouched.
TOOL_MAP = {
    "Edit": ["edit", "apply_patch"],
    "MultiEdit": ["edit", "apply_patch"],
    "Write": ["write", "apply_patch"],
    "NotebookEdit": ["notebook_edit"],
    "Read": ["read", "notebook_read"],
    "NotebookRead": ["notebook_read"],
    "Bash": ["exec"],
    "Grep": ["grep"],
    "Glob": ["glob"],
    "Agent": ["run_subagent"],
    "Task": ["run_subagent"],
    "WebFetch": ["webfetch"],
    "TodoWrite": ["todo_write"],
}


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments outside string literals."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                i += 1
                if i < n:
                    out.append(text[i])
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated /* comment")
            i = end + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def load(path: Path = SOURCE) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(strip_jsonc(raw))
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        raise ValueError(f"{path}: expected an object with a 'hooks' object")
    return data


def translate_matcher(matcher: Any) -> str | None:
    """Claude matcher -> Devin matcher; None means 'match everything'."""
    if not isinstance(matcher, str) or matcher in ("", "*", ".*"):
        return None
    names: list[str] = []
    for token in matcher.split("|"):
        token = token.strip()
        if not token:
            continue
        for name in TOOL_MAP.get(token, [token]):
            if name not in names:
                names.append(name)
    if not names:
        return None
    return "^(" + "|".join(names) + ")$"


def convert(hooks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for event, entries in hooks.items():
        if event not in DEVIN_EVENTS:
            continue
        if not isinstance(entries, list):
            raise ValueError(f"hooks.{event}: expected a list")
        converted: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                raise ValueError(
                    f"hooks.{event}: expected matcher objects with 'hooks'"
                )
            new_entry: dict[str, Any] = {}
            matcher = translate_matcher(entry.get("matcher"))
            if matcher is not None:
                new_entry["matcher"] = matcher
            new_entry["hooks"] = entry["hooks"]
            converted.append(new_entry)
        out[event] = converted
    return out


def render(settings: dict[str, Any]) -> str:
    return json.dumps(convert(settings["hooks"]), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=("render", "check"))
    parser.add_argument(
        "--check",
        action="store_true",
        help="render in memory and diff against the committed file "
        "(same verdict as the 'check' subcommand; nothing is written)",
    )
    args = parser.parse_args(argv)
    shown = OUT.relative_to(REPO) if OUT.is_relative_to(REPO) else OUT
    fresh = render(load())
    if args.cmd == "render" and not args.check:
        OUT.write_text(fresh, encoding="utf-8")
        print(f"wrote {shown}")
        return 0
    have = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    if have == fresh:
        print(f"{shown} is up to date")
        return 0
    sys.stdout.writelines(
        difflib.unified_diff(
            have.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            str(OUT),
            "fresh render",
        )
    )
    print(
        f"stale {shown} — next=scripts/gen-devin-hooks render",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
