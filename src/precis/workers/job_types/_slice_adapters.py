"""Headless vendor protocols for ``run_slice``.

Only Claude receives precis MCP credentials. Codex and Devin use their
host account login and return their handoff through output; the worker
checks every vendor's branch against the lane's deny paths before land.
Devin's model allowlist is supplied by the selected lane, never inferred
from a vendor-wide default: unattended runs must not buy overage.

Observed with installed ``codex exec --json`` on 2026-11-04 (a trivial
``ADAPTER_OK`` prompt): ``{"type":"item.completed","item":{"id":"item_2",
"type":"agent_message","text":"ADAPTER_OK"}}``, followed by
``turn.completed`` with usage. ``codex exec --json --help`` confirms
``-`` reads stdin and ``-o`` writes the last message.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PERMISSION_MODE = "bypassPermissions"
_DENY_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def claude_bin() -> str:
    """``PRECIS_CLAUDE_BIN`` or ``claude`` on PATH."""
    return os.environ.get("PRECIS_CLAUDE_BIN") or "claude"


def deny_settings(deny_paths: tuple[str, ...], tree: Path) -> str:
    """Claude deny rules in cwd-relative and absolute forms, plus trust
    for the service account checkout's project MCP servers."""
    rules: list[str] = []
    for path in deny_paths:
        for spec in (f"./{path}", f"/{tree.as_posix()}/{path}"):
            rules += [f"{tool}({spec})" for tool in _DENY_TOOLS]
    return json.dumps(
        {"permissions": {"deny": rules}, "enableAllProjectMcpServers": True}
    )


def parse_claude_result(log_text: str) -> tuple[str | None, bool]:
    """The last Claude JSON object carrying ``result``, unchanged."""
    for line in reversed(log_text.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "result" in obj:
            return str(obj.get("result") or ""), bool(obj.get("is_error"))
    return None, True


@dataclass(frozen=True)
class Adapter:
    """One vendor's launch/output protocol; ``models`` in ``argv`` is the
    selected lane's allowlist (required for any explicit Devin model)."""

    name: str
    mcp: bool

    def argv(
        self,
        *,
        model: str | None,
        effort: str,
        tree: Path,
        brief_path: Path,
        deny_paths: tuple[str, ...],
        max_turns: int,
        models: tuple[str, ...] = (),
    ) -> list[str]:
        """Build argv; effort remains the worker's exported metadata."""
        if self.name == "claude_p":
            argv = [
                claude_bin(),
                "-p",
                "--output-format",
                "json",
                "--max-turns",
                str(max_turns),
            ]
            if model is not None:
                argv += ["--model", model]
            return argv + [
                "--permission-mode",
                PERMISSION_MODE,
                "--settings",
                deny_settings(deny_paths, tree),
            ]
        if self.name == "codex_exec":
            argv = [
                os.environ.get("PRECIS_CODEX_BIN") or "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                str(tree),
                "-o",
                str(tree / "scratch" / "last-message.md"),
            ]
            if model is not None:
                argv += ["-m", model]
            return argv + ["--sandbox", "workspace-write", "-"]
        if self.name == "devin_tree":
            if model is not None and model not in models:
                raise ValueError(f"Devin model {model!r} is not in the lane's models")
            argv = [
                os.environ.get("PRECIS_DEVIN_BIN") or "devin",
                "-p",
                "--prompt-file",
                str(brief_path),
                "--permission-mode",
                "dangerous",
                "--respect-workspace-trust",
                "false",
                "--verbosity",
                "1",
            ]
            if model is not None:
                argv += ["--model", model]
            return argv
        raise KeyError(self.name)

    def stdin(self, brief_path: Path) -> Path | None:
        """Devin reads its prompt file; the other CLIs consume stdin."""
        return None if self.name == "devin_tree" else brief_path

    def parse_result(
        self, log_text: str, tree: Path, *, exit_code: int | None = 0
    ) -> tuple[str | None, bool]:
        """Return the final message and vendor error bit; poll also checks
        the process exit code independently for every vendor."""
        if self.name == "claude_p":
            return parse_claude_result(log_text)
        if self.name == "devin_tree":
            return log_text.strip(), exit_code != 0
        if self.name == "codex_exec":
            try:
                message = (
                    (tree / "scratch" / "last-message.md")
                    .read_text(encoding="utf-8", errors="replace")
                    .strip()
                )
            except OSError:
                message = ""
            if message:
                return message, False
            for line in reversed(log_text.strip().splitlines()):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "item.completed":
                    continue
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        return text, False
            return None, True
        raise KeyError(self.name)


ADAPTERS: dict[str, Adapter] = {
    "claude_p": Adapter("claude_p", mcp=True),
    "codex_exec": Adapter("codex_exec", mcp=False),
    "devin_tree": Adapter("devin_tree", mcp=False),
}


def get_adapter(name: str) -> Adapter:
    """Resolve a lane adapter; unknown names raise ``KeyError``."""
    return ADAPTERS[name]
