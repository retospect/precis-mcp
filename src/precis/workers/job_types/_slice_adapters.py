"""Headless vendor protocols for ``run_slice``.

Only Claude receives write-capable precis MCP credentials. Codex and
Devin can carry a *scoped* grant (``read_mcp`` — reader/triage,
gr574082) when ``PRECIS_SLICE_READ_MCP`` arms it, and otherwise use
their host account login and return their handoff through output; the worker
checks every vendor's branch against the lane's deny paths before land.
Codex's ``workspace-write`` sandbox needs ``--add-dir`` for anything the
session must write outside its tree — ``run_slice`` passes the shared
git dir (worktree index/refs, objects, the gate queue + slot locks) and
the Docker socket's parent so a slice can ``git commit`` and run
``scripts/test`` (gr570533).
Devin's model allowlist is supplied by the selected lane, never inferred
from a vendor-wide default: unattended runs must not buy overage.

Observed with installed ``codex exec --json`` on 2026-11-04 (a trivial
``ADAPTER_OK`` prompt): ``{"type":"item.completed","item":{"id":"item_2",
"type":"agent_message","text":"ADAPTER_OK"}}``, followed by
``turn.completed`` with usage. ``codex exec --json --help`` confirms
``-`` reads stdin and ``-o`` writes the last message.

Kimi's headless mode (``kimi -p``, installed 2.3.3, verified 2026-11-07
against a fake provider) takes the prompt in argv and emits JSONL on
``--output-format stream-json`` — the last ``{"role":"assistant"}``
line is the final message. The CLI authenticates through a synthesized
provider env (``KIMI_MODEL_*``) the worker fills from the vault key at
launch — the only env channel for it — and answers the folder-trust
prompt with ``KIMI_CODE_TRUST_WORKSPACE=1``. It is exec'd directly,
like devin, never through scripts/kimi-tree (its ``-p`` refusal is for
interactive launches). No ``-m`` — it would rebind to the config's
managed provider — and no UA or identity override: the subscription
terms forbid tampering (td573681; autonomous lane sanctioned
2026-11-07).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PERMISSION_MODE = "bypassPermissions"
_DENY_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def claude_bin() -> str:
    """``PRECIS_CLAUDE_BIN`` or ``claude`` on PATH."""
    return os.environ.get("PRECIS_CLAUDE_BIN") or "claude"


def codex_bin() -> str:
    """``PRECIS_CODEX_BIN`` or ``codex`` on PATH."""
    return os.environ.get("PRECIS_CODEX_BIN") or "codex"


def devin_bin() -> str:
    """``PRECIS_DEVIN_BIN`` or ``devin`` on PATH — resolved on the worker
    at submit, for argv and the preflight alike (gr570688)."""
    return os.environ.get("PRECIS_DEVIN_BIN") or "devin"


def kimi_bin() -> str:
    """``PRECIS_KIMI_BIN``, else ``kimi`` on ``PATH``, else the user
    install at ``~/.kimi-code/bin/kimi`` — resolved on the worker at
    submit, for argv and the preflight alike (mirrors scripts/kimi-tree)."""
    return (
        os.environ.get("PRECIS_KIMI_BIN")
        or shutil.which("kimi")
        or str(Path.home() / ".kimi-code" / "bin" / "kimi")
    )


_ADAPTER_BINS: dict[str, Callable[[], str]] = {
    "claude_p": claude_bin,
    "codex_exec": codex_bin,
    "devin_tree": devin_bin,
    "kimi_tree": kimi_bin,
}


def adapter_bin(name: str) -> str:
    """The adapter's own binary, resolved exactly as its argv does —
    the name a ``cli-missing`` verdict must see on the process's own
    last line (``KeyError`` on an unknown adapter)."""
    return _ADAPTER_BINS[name]()


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


def parse_kimi_result(
    log_text: str, *, exit_code: int | None = 0
) -> tuple[str | None, bool]:
    """The last ``{"role":"assistant"}`` stream-json line's content —
    the session's final message. ``content`` is a string or a list of
    text parts. No usable assistant line, or a non-zero exit, is an
    error."""
    text: str | None = None
    for line in log_text.strip().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get("role") != "assistant":
            continue
        content = obj.get("content")
        if isinstance(content, str):
            candidate = content
        elif isinstance(content, list):
            candidate = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            continue
        if candidate.strip():
            text = candidate
    if text is None:
        return None, True
    return text, exit_code != 0


@dataclass(frozen=True)
class Adapter:
    """One vendor's launch/output protocol; ``models`` in ``argv`` is the
    selected lane's allowlist (required for any explicit Devin model).
    ``mcp`` means write-capable precis MCP (it gates generators and the
    reviewer wording); ``read_mcp`` means the lane can take a scoped
    grant (reader/triage, gr574082) — reads plus the triage write set —
    when ``PRECIS_SLICE_READ_MCP`` names a scope."""

    name: str
    mcp: bool
    read_mcp: bool = False

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
        writable_roots: tuple[Path, ...] = (),
        read_mcp_url: str | None = None,
    ) -> list[str]:
        """Build argv; effort remains the worker's exported metadata.
        ``writable_roots`` are the sandbox's ``--add-dir`` grants (codex
        only — the other vendors run unsandboxed). ``read_mcp_url`` is
        the checkout's precis MCP URL for a scoped-grant lane: codex
        takes it as ``-c`` dotted overrides (``codex exec -c
        mcp_servers.<name>.url=…``); devin's server entry lives in the
        per-tree config file ``run_slice._devin_mcp_config`` writes, not
        argv."""
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
                codex_bin(),
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                str(tree),
                "-o",
                str(tree / "scratch" / "last-message.md"),
            ]
            for root in writable_roots:
                argv += ["--add-dir", str(root)]
            if read_mcp_url:
                argv += [
                    "-c",
                    f"mcp_servers.precis.url={read_mcp_url}",
                    "-c",
                    "mcp_servers.precis.bearer_token_env_var=PRECIS_MCP_TOKEN",
                ]
            if model is not None:
                argv += ["-m", model]
            return argv + ["--sandbox", "workspace-write", "-"]
        if self.name == "devin_tree":
            if model is not None and model not in models:
                raise ValueError(f"Devin model {model!r} is not in the lane's models")
            argv = [
                devin_bin(),
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
        if self.name == "kimi_tree":
            # The prompt rides argv, not stdin (``kimi -p``). The model
            # never becomes ``-m`` — it would rebind to the config's
            # managed/OAuth provider; ``KIMI_MODEL_NAME`` carries it.
            return [
                kimi_bin(),
                "-p",
                brief_path.read_text(encoding="utf-8"),
                "--output-format",
                "stream-json",
            ]
        raise KeyError(self.name)

    def stdin(self, brief_path: Path) -> Path | None:
        """Devin and kimi carry the prompt in argv; the other CLIs
        consume stdin."""
        return None if self.name in ("devin_tree", "kimi_tree") else brief_path

    def parse_result(
        self, log_text: str, tree: Path, *, exit_code: int | None = 0
    ) -> tuple[str | None, bool]:
        """Return the final message and vendor error bit; poll also checks
        the process exit code independently for every vendor."""
        if self.name == "claude_p":
            return parse_claude_result(log_text)
        if self.name == "devin_tree":
            return log_text.strip(), exit_code != 0
        if self.name == "kimi_tree":
            return parse_kimi_result(log_text, exit_code=exit_code)
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
    "codex_exec": Adapter("codex_exec", mcp=False, read_mcp=True),
    "devin_tree": Adapter("devin_tree", mcp=False, read_mcp=True),
    "kimi_tree": Adapter("kimi_tree", mcp=False),
}


def get_adapter(name: str) -> Adapter:
    """Resolve a lane adapter; unknown names raise ``KeyError``."""
    return ADAPTERS[name]
