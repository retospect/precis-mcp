#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Kimi Code hook adapter: run this repo's Claude hooks under Kimi.

Kimi Code (`kimi`) reads hooks only from the user-level
`~/.kimi-code/config.toml`; `scripts/kimi-setup` installs one `[[hooks]]`
entry per event that calls this file with the event name. The adapter then
dispatches to the hooks `.claude/settings.json` lists for that event, so
the Claude config stays the one list of guards — no second copy to drift.

Kimi's payload is Claude-shaped (`hook_event_name`, `tool_name`,
`tool_input`, `cwd`) and its tool names match Claude's (Bash, Edit, Write,
Read, Grep, Agent, `mcp__precis__*`). Differences bridged here:

- File tools key the path as `path`; the hooks read `file_path`, so the
  adapter adds `file_path` (and maps it back when a hook rewrites input).
- No `$CLAUDE_PROJECT_DIR`: set from the payload `cwd` (Kimi runs hooks in
  the session's project dir).
- Kimi blocks on exit 2 + stderr. A hook's `permissionDecision: deny` or
  `ask` becomes exit 2 with its reason (an `ask` passes once the command
  carries ASK_ACK); an `updatedInput` rewrite (Kimi has no input
  rewriting) becomes a block naming the corrected arguments.
- Hooks bound to Claude's own harness state (its transcript, subagents,
  process tree) are skipped: SKIP below. kimi-tree holds the worktree lock
  that session-start-lock.sh takes for Claude.

Tier deny rules: Kimi reads no project permission file, so on PreToolUse
the adapter itself applies the nearest `.claude/settings.local.json`
`permissions.deny` list it finds walking up from the payload cwd
(_local_deny), before the repo-hook dispatch. The adapter ships inside
the tree (tracked at scripts/hooks/kimi-adapt.py), so only trees cut
after this landed carry the rule — launch refuses an older one. A
missing or unreadable settings file fails closed on a built-in deny of
the write tools and landing scripts, and the pane's own enforcement
files (settings, this adapter, the fleet authority file, ~/.kimi-code/)
are denied whatever the file says. Every decision is one line on
`<tree>/.claude/kimi-adapt.log` (stamp, tool, deny|allow, rule) so the
tier-R gate can verify a refusal landed. The Bash checks stop mistakes,
not an adversary: `$(…)`, `&`, `python -c` and absolute paths are not
normalised. Real enforcement is the server-side scope (G1
`mcp.slice_scope` enforce, td574161 grant) and tier R's rule that only
the coordinator lands.

Fail-open like Claude: a hook that crashes or times out is ignored.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

SKIP = (
    "session-start-lock.sh",
    "session-end-reap.sh",
    "read-dedup.py",
    "session-size-nudge.py",
    "subagent-task-state.py",
    "edit-delegation-nudge.py",
    "agent-tier-nudge.py",
    "precompact-persist.sh",
)
FILE_TOOLS = ("Edit", "Write", "Read")
TIMEOUT_S = 60
# Kimi hooks cannot prompt. A hook's `ask` blocks; after the user approves,
# the agent re-issues the command with this prefix (the same escape hatch
# guard-prod-psql.py reads from its environment).
ASK_ACK = "ALLOW_PROD_WRITE=1"


def _matches(matcher: str, target: str) -> bool:
    if matcher in ("", "*"):
        return True
    return re.fullmatch(matcher, target) is not None


def _commands(settings: Path, event: str, target: str) -> list[str]:
    try:
        hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError):
        return []
    out = []
    for group in hooks.get(event, []):
        if not _matches(group.get("matcher", ""), target):
            continue
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if h.get("type") == "command" and not any(s in cmd for s in SKIP):
                out.append(cmd)
    return out


def _block(reason: str) -> None:
    sys.stderr.write(reason.strip() + "\n")
    sys.exit(2)


# --- local tier deny rules ---------------------------------------------------
# Kimi Code has no project permission file; the tree's
# .claude/settings.local.json (written by fleet-authority apply) is
# enforced here instead. Rule shapes honoured: a bare tool name
# (mcp__precis__put), Bash(x) exact, Bash(x:*) prefix, Edit(glob) /
# Write(glob) against the call's file_path — an Edit rule also covers
# Kimi's Write. Anything else is ignored.
#
# Fail CLOSED: a missing, unparsable or "deny": null file enforces
# _FALLBACK_DENY and logs the rule as settings-unreadable — a pane whose
# deny list cannot be read is contained, not trusted. The pane's own
# enforcement files (_PROTECTED_PATHS, anything under ~/.kimi-code/) are
# denied whatever the file says, Edit/Write and shell writes alike.

#: Enforced when the settings file cannot be read: the prod write tools
#: and the scripts that land, deploy or kill — reads and ordinary file
#: edits still pass.
_FALLBACK_DENY = (
    "Bash(scripts/ship:*)",
    "Bash(scripts/release:*)",
    "Bash(scripts/deploy:*)",
    "Bash(scripts/round:*)",
    "Bash(scripts/host-ops/play:*)",
    "Bash(pkill -f:*)",
    "mcp__precis__put",
    "mcp__precis__edit",
    "mcp__precis__delete",
    "mcp__precis__tag",
    "mcp__precis__link",
)

#: A pane may never rewrite its own enforcement, whatever the deny list
#: says. Matched on the resolved path and on the raw tail, so an absolute
#: path into another tree counts too.
_PROTECTED_PATHS = (
    ".claude/settings.local.json",
    ".claude/settings.json",
    ".claude/fleet/authority.md",
    "scripts/hooks/kimi-adapt.py",
)
_KIMI_HOME = os.path.normpath(os.path.expanduser("~/.kimi-code"))
_BASH_WRITE_VERBS = ("tee", "sed", "cp", "mv")
# ``x>file`` redirects with no whitespace either side, like ``x > file``.
_REDIRECT_RE = re.compile(r"(?:\d+|&)?>{1,2}\s*([^\s;&|<>]+)")
# The quoted argument of ``bash -c``/``sh -c`` — unquoted so its operators
# split like an unwrapped command's.
_C_QUOTE_RE = re.compile(r"(\b(?:ba)?sh\s+-c\b\s*)(['\"])(.*?)\2")


def _deny_file(cwd: Path) -> Path | None:
    """The nearest .claude/settings.local.json at or above ``cwd``
    (at most 6 levels up), else None."""
    base = cwd
    for _ in range(7):
        f = base / ".claude" / "settings.local.json"
        if f.is_file():
            return f
        if base.parent == base:
            return None
        base = base.parent
    return None


def _claude_base(cwd: Path) -> Path:
    """The nearest ancestor of ``cwd`` holding a ``.claude`` dir (≤6 up),
    else ``cwd`` — where the decision log lands when no settings file
    anchored it."""
    base = cwd
    for _ in range(7):
        if (base / ".claude").is_dir():
            return base
        if base.parent == base:
            return cwd
        base = base.parent
    return cwd


def _strip_env(command: str) -> str:
    """Leading ``VAR=val`` words (bare or quoted) dropped from a command."""
    cmd = command.strip()
    while True:
        m = re.match(r"^[A-Za-z_]\w*=(?:'[^']*'|\"[^\"]*\"|\S+)(?:\s+|$)", cmd)
        if not m:
            return cmd
        cmd = cmd[m.end() :].lstrip()


def _path_rule_hit(spec: str, file_path: str, base: Path) -> bool:
    """An Edit/Write glob vs the call's path: ``~`` expanded, relative
    patterns resolved against the settings file's tree, ``*``/``**``
    crossing ``/`` (fnmatch)."""
    if not file_path:
        return False
    cand = os.path.expanduser(file_path)
    if not os.path.isabs(cand):
        cand = str(base / cand)
    pat = os.path.expanduser(spec)
    if os.path.isabs(pat):
        return fnmatch.fnmatch(cand, pat)
    return fnmatch.fnmatch(os.path.relpath(cand, base), pat)


def _bash_segments(command: str, base: Path) -> list[tuple[str, Path]]:
    """One ``(command, cwd)`` per ``&&``/``||``/``;``/``|`` segment.

    ``cd`` segments update the cwd later segments resolve against;
    ``VAR=val`` words, a leading ``./``, ``bash``/``sh`` (+``-c``)/
    ``uv run`` wrappers and one whole-segment quote layer are normalised
    away before a rule is compared. The quoted argument of a ``-c`` is
    unquoted first, so its operators split like an unwrapped command's."""
    command = _C_QUOTE_RE.sub(lambda m: m.group(1) + m.group(3), command)
    cwd = base
    out = []
    for raw in re.split(r"&&|\|\||[;|\n]", command):
        seg = _strip_env(raw)
        while True:
            s = seg.lstrip()
            if s.startswith("./") and len(s) > 2:
                seg = s[2:]
                continue
            m = re.match(r"(?:(?:ba)?sh(?:\s+-c\b)?|uv\s+run)\s+", s)
            if m:
                seg = s[m.end() :]
                continue
            if len(s) > 1 and s[0] in "'\"" and s.endswith(s[0]):
                seg = s[1:-1]
                continue
            seg = s
            break
        if not seg:
            continue
        if seg == "cd" or seg.startswith("cd "):
            arg = seg[2:].strip().strip("'\"")
            d = os.path.expanduser(arg) if arg else os.path.expanduser("~")
            cwd = Path(d) if os.path.isabs(d) else cwd / d
            continue
        out.append((seg, cwd))
    return out


def _protected_hit(path_str: str, base: Path) -> str | None:
    """The protected name ``path_str`` resolves to, else None: the
    settings/hook/authority files under ``base`` or any other root (raw
    tail match), or anything under ``~/.kimi-code/``."""
    if not path_str:
        return None
    cand = os.path.normpath(os.path.expanduser(path_str.strip().strip("'\"")))
    full = cand if os.path.isabs(cand) else os.path.normpath(str(base / cand))
    if full == _KIMI_HOME or full.startswith(_KIMI_HOME + os.sep):
        return "~/.kimi-code"
    for rel in _PROTECTED_PATHS:
        if full == os.path.normpath(str(base / rel)) or full.endswith(os.sep + rel):
            return rel
    return None


def _bash_protected_hit(command: str, base: Path) -> str | None:
    """A protected name the command would write through a ``>``/``>>``
    redirect or a tee/sed -i/cp/mv argument, else None."""
    for seg, seg_base in _bash_segments(command, base):
        for m in _REDIRECT_RE.finditer(seg):
            if hit := _protected_hit(m.group(1), seg_base):
                return hit
        words = seg.split()
        if not words or os.path.basename(words[0]) not in _BASH_WRITE_VERBS:
            continue
        if words[0].endswith("sed") and not any(
            w.startswith("-") and "i" in w for w in words[1:]
        ):
            continue
        for w in words[1:]:
            if w.startswith("-"):
                continue
            if hit := _protected_hit(w, seg_base):
                return hit
    return None


def _hard_deny(tool: str, tool_input: dict, base: Path) -> str | None:
    """``protected:<name>`` when the call would rewrite the pane's own
    enforcement — denied whatever the settings file says."""
    if tool in ("Edit", "Write"):
        fp = str(tool_input.get("path") or tool_input.get("file_path") or "")
        hit = _protected_hit(fp, base)
        return f"protected:{hit}" if hit else None
    if tool == "Bash":
        hit = _bash_protected_hit(str(tool_input.get("command", "")), base)
        return f"protected:{hit}" if hit else None
    return None


def _deny_match(rule: str, tool: str, tool_input: dict, base: Path) -> bool:
    m = re.match(r"^(\w+)\((.*)\)$", rule)
    rtool, spec = (m.group(1), m.group(2)) if m else (rule, "")
    # An Edit rule also covers Kimi's Write tool.
    covered = tool == rtool or (rtool == "Edit" and tool == "Write")
    if not spec:
        return covered
    if not covered:
        return False
    if rtool == "Bash":
        cmd = str(tool_input.get("command", ""))
        for seg, _cwd in _bash_segments(cmd, base):
            if spec.endswith(":*"):
                prefix = spec[:-2]
                if seg == prefix or seg.startswith(prefix + " "):
                    return True
            elif seg == spec:
                return True
        return False
    if rtool in ("Edit", "Write"):
        fp = str(tool_input.get("file_path") or tool_input.get("path") or "")
        return _path_rule_hit(spec, fp, base)
    return False


def _local_deny(cwd: Path, tool: str, tool_input: dict) -> None:
    """Enforce the tree's deny posture on a PreToolUse call.

    The pane's own enforcement files are always denied. Otherwise the
    nearest settings.local.json ``permissions.deny`` list applies; a
    missing, unparsable or ``"deny": null`` file fails CLOSED on
    _FALLBACK_DENY and the decision is logged under rule
    ``settings-unreadable``. Each decision is one
    ``stamp⇥tool⇥deny|allow⇥rule`` line on
    ``<tree>/.claude/kimi-adapt.log`` so a refused call is visible
    outside the pane."""
    found = _deny_file(cwd)
    base = found.parent.parent if found else _claude_base(cwd)
    rules: list = []
    unreadable = True
    if found is not None:
        try:
            rules = (
                json.loads(found.read_text(encoding="utf-8"))
                .get("permissions", {})
                .get("deny", [])
            )
            unreadable = not isinstance(rules, list)
        except (OSError, ValueError, AttributeError):
            rules = []
    if unreadable:
        match = next(
            (r for r in _FALLBACK_DENY if _deny_match(r, tool, tool_input, base)),
            None,
        )
        hit = "settings-unreadable" if match else None
    else:
        hit = next(
            (
                r
                for r in rules
                if isinstance(r, str) and _deny_match(r, tool, tool_input, base)
            ),
            None,
        )
    if hit is None:
        hit = _hard_deny(tool, tool_input, base)
    try:
        (base / ".claude").mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (base / ".claude" / "kimi-adapt.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{tool}\t{'deny' if hit else 'allow'}\t{hit or '-'}\n")
    except OSError:
        pass
    if hit:
        _block(
            f"tier deny rule {hit} (.claude/settings.local.json): stop and "
            "report the exact command and this refusal to the coordinator; "
            "do not get the same effect another way"
        )


def _handle(stdout: str, tool_input: dict, context: list[str]) -> None:
    """Fold one hook's stdout into a block (exits) or extra context."""
    text = stdout.strip()
    if not text:
        return
    try:
        data = json.loads(text)
    except ValueError:
        context.append(text)
        return
    if not isinstance(data, dict):
        context.append(text)
        return
    spec = data.get("hookSpecificOutput") or {}
    decision = spec.get("permissionDecision") or data.get("decision")
    reason = spec.get("permissionDecisionReason") or data.get("reason") or ""
    if decision == "deny" or decision == "block":
        _block(reason or "blocked by a repo hook")
    if decision == "ask" and ASK_ACK not in str(tool_input.get("command", "")):
        _block(
            f"{reason}\nThis needs the user's confirmation. Ask them; once they "
            f"approve, re-issue the same command prefixed with `{ASK_ACK} `."
        )
    updated = spec.get("updatedInput")
    if isinstance(updated, dict) and updated != tool_input:
        if "file_path" in updated and "path" in tool_input:
            updated = {**updated, "path": updated["file_path"]}
            updated.pop("file_path")
        _block(
            f"{reason}\nRe-issue the call with these arguments: "
            + json.dumps(updated, ensure_ascii=False)
        )
    extra = spec.get("additionalContext") or data.get("systemMessage")
    if extra:
        context.append(str(extra))


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    event = event or payload.get("hook_event_name", "")
    root = Path(payload.get("cwd") or os.getcwd())
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    if tool in FILE_TOOLS and "path" in tool_input and "file_path" not in tool_input:
        p = tool_input["path"]
        abs_path = p if os.path.isabs(p) else str(root / p)
        payload["tool_input"] = {**tool_input, "file_path": abs_path}
    if event == "PreToolUse":
        _local_deny(root, tool, payload["tool_input"])
    target = tool if event.startswith(("PreToolUse", "PostToolUse")) else ""
    if event == "SessionStart":
        target = payload.get("source", "startup")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), "PRECIS_AGENT": "kimi"}
    if ASK_ACK in str(tool_input.get("command", "")):
        env["ALLOW_PROD_WRITE"] = "1"
    stdin = json.dumps(payload)
    context: list[str] = []
    for cmd in _commands(root / ".claude" / "settings.json", event, target):
        try:
            r = subprocess.run(
                ["bash", "-c", cmd],
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=root,
                env=env,
                timeout=TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 2:
            _block(r.stderr or r.stdout or "blocked by a repo hook")
        _handle(r.stdout, payload["tool_input"] if tool_input else {}, context)
    if context:
        print("\n".join(context))


if __name__ == "__main__":
    main()
