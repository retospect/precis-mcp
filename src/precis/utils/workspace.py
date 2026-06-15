"""Workspace abstraction — project-scoped layout + git + auto-init.

A *workspace* is a project's home directory under ``PRECIS_ROOT``. The
LLM never sees physical paths; it works in slug-space. The MCP layer
handles infrastructure: workspace init, layout routing, git commits,
.gitignore, main.tex skeleton, refs.bib regeneration.

Two carriers of workspace context coexist:

* **``meta.workspace`` on a todo ref** — durable, inherited from parent
  at ``put`` time. Owner sets it once on the strategic root; cascade
  flows it down to every leaf.
* **``PRECIS_WORKSPACE`` env var** — ambient, per-call. The planner
  runner sets it on the ``claude -p`` subprocess matching the parent
  todo's ``meta.workspace.path``. The MCP server reads it; file-kind
  handlers route accordingly.

The two MUST agree. The runner is responsible for setting the env
from the meta.

Shape (frozen dataclass for type-safety + json-serializable for meta
column storage)::

    Workspace(
        path="projects/nanotrans_auto",   # relative to PRECIS_ROOT
        format="tex",                     # "tex" or "md"
        entrypoint="main.tex",            # root document name
        style="ieee-numeric",             # citation style (informational)
    )

The :func:`ensure_initialized` helper does **lazy** init: when the MCP
layer sees a workspace path that lacks ``.git/`` or the entrypoint
file, it copies templates from
``src/precis/data/workspace_templates/<format>/`` and runs ``git init``.
LLM never calls ``init`` explicitly.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


SUPPORTED_FORMATS: frozenset[str] = frozenset({"tex", "md"})


@dataclass(frozen=True, slots=True)
class Workspace:
    """A project workspace — path + format + light metadata.

    Stored as ``meta.workspace`` on todo refs (JSON-serializable; this
    class is the typed view). Pass through the cascade by reading the
    parent's meta and injecting the same dict into the child's meta at
    ``put`` time.
    """

    path: str  # relative to PRECIS_ROOT
    format: str  # "tex" | "md"
    entrypoint: str  # e.g. "main.tex" or "main.md"
    style: str = ""  # citation style; informational
    # Forward-compatible storage for extra workspace metadata
    # (e.g., author, title, build_command overrides).
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.format not in SUPPORTED_FORMATS:
            raise ValueError(
                f"unsupported workspace format {self.format!r}; "
                f"supported: {sorted(SUPPORTED_FORMATS)}"
            )
        if "/" in self.entrypoint or self.entrypoint.startswith("."):
            raise ValueError(
                f"workspace entrypoint must be a plain filename, got "
                f"{self.entrypoint!r}"
            )
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError(
                f"workspace path must be relative + no traversal, got "
                f"{self.path!r}"
            )

    @classmethod
    def from_meta(cls, meta: dict[str, Any] | None) -> Workspace | None:
        """Parse a ``meta.workspace`` block into a Workspace, or None."""
        if not meta:
            return None
        ws = meta.get("workspace")
        if not ws or not isinstance(ws, dict):
            return None
        known = {"path", "format", "entrypoint", "style"}
        extra = {k: v for k, v in ws.items() if k not in known}
        try:
            return cls(
                path=str(ws["path"]),
                format=str(ws.get("format", "tex")),
                entrypoint=str(ws.get("entrypoint", "main.tex")),
                style=str(ws.get("style", "")),
                extra=extra,
            )
        except (KeyError, ValueError) as exc:
            log.warning("workspace.from_meta: invalid workspace block: %s", exc)
            return None

    def to_meta(self) -> dict[str, Any]:
        """Render as a dict suitable for storing in ``refs.meta.workspace``."""
        out: dict[str, Any] = {
            "path": self.path,
            "format": self.format,
            "entrypoint": self.entrypoint,
        }
        if self.style:
            out["style"] = self.style
        out.update(self.extra)
        return out

    def absolute_root(self, precis_root: Path) -> Path:
        """Resolve to an absolute filesystem path under PRECIS_ROOT."""
        return (precis_root / self.path).resolve()


def current_from_env() -> str | None:
    """Return the workspace path from ``PRECIS_WORKSPACE``, or None.

    Returns the relative path the env var carries (e.g.
    ``projects/nanotrans_auto``). Caller resolves against PRECIS_ROOT.
    Empty / unset env → None.
    """
    raw = os.environ.get("PRECIS_WORKSPACE")
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("/") or ".." in raw.split("/"):
        log.warning(
            "PRECIS_WORKSPACE rejected (must be relative, no traversal): %r",
            raw,
        )
        return None
    return raw


def current_project_tag_from_env() -> str | None:
    """Return the ``project:<slug>`` tag derived from the workspace path.

    Cross-cutting query handle: every ref the LLM mints under this
    workspace (todos, citations, findings, file refs) gets this tag
    auto-injected so ``search(tags=['project:nanotrans_auto'])``
    returns the full project surface regardless of kind. Bib
    generation, project archive, cleanup operations all use the same
    tag. The LLM never thinks about which project it's working on —
    the env propagates the context.

    Derived from the basename of ``PRECIS_WORKSPACE``:
    ``projects/nanotrans_auto`` → ``project:nanotrans_auto``.
    """
    ws_path = current_from_env()
    if not ws_path:
        return None
    slug = ws_path.rstrip("/").split("/")[-1]
    if not slug:
        return None
    return f"project:{slug}"


def current_model_from_env() -> str | None:
    """Return the model the current tick is running on (opus / sonnet / haiku).

    Planner runner sets ``PRECIS_CURRENT_MODEL`` on the ``claude -p``
    subprocess so the LLM knows its own tier — and can make
    degradation/escalation decisions:

    * Too hard for haiku? Mint a child with ``LLM:opus``.
    * Sonnet on a topic needing external state? Mint
      ``LLM:sonnet`` child with ``executor:fetch`` to pull missing
      papers, OR call ``get(kind='perplexity-research', q='...')`` inline for
      a perplexity research dive.
    * Opus on a topic that's clear and obvious? Do it yourself
      without a model upgrade.

    Returns ``None`` when unset (operator CLI session, tests).
    """
    raw = os.environ.get("PRECIS_CURRENT_MODEL")
    if not raw:
        return None
    raw = raw.strip().lower()
    if raw not in ("opus", "sonnet", "haiku"):
        return None
    return raw


def current_todo_from_env() -> int | None:
    """Return the parent todo ref id from ``PRECIS_CURRENT_TODO``, or None.

    The planner runner sets this on the ``claude -p`` subprocess env
    to the parent todo's ref_id. The MCP server reads it from
    ``os.environ``. ``TodoHandler.put`` auto-injects ``parent_id=`` from
    here when the caller doesn't pass one explicitly — so the LLM can
    mint subtasks without remembering its own id every call.

    Pattern parallel to :func:`current_from_env` for ``PRECIS_WORKSPACE``:
    runtime context flows via env vars set by the runner, not via
    every MCP call carrying redundant identity.

    Returns ``None`` on unset / non-integer / negative values
    (the LLM should still be able to mint a root todo explicitly when
    ``parent_id=None`` is passed; this helper only handles defaulting).
    """
    raw = os.environ.get("PRECIS_CURRENT_TODO")
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        parent_id = int(raw)
    except ValueError:
        log.warning(
            "PRECIS_CURRENT_TODO rejected (must be int): %r", raw
        )
        return None
    if parent_id <= 0:
        return None
    return parent_id


# ── lazy init ────────────────────────────────────────────────────


def ensure_initialized(workspace: Workspace, precis_root: Path) -> Path:
    """Initialize the workspace dir on disk if it isn't yet.

    Idempotent. Each missing piece is created independently:

    * Directory tree (workspace root + ``tex/``, ``pics/``, ``data/``,
      ``build/`` for ``format='tex'``; less for ``format='md'``).
    * ``.gitignore`` copied from the format template.
    * Entrypoint (``main.tex`` / ``main.md``) copied from the format
      template if missing — preamble + ``\\input{}`` stub list.
    * ``refs.bib`` empty placeholder (tex format only).
    * ``git init`` if no ``.git/`` exists; initial commit stages the
      templates so the first per-put commit has a parent.

    Returns the absolute workspace root path.
    """
    root = workspace.absolute_root(precis_root)
    root.mkdir(parents=True, exist_ok=True)

    # Subdirs per layout convention.
    layout_dirs = _layout_subdirs(workspace.format)
    for sub in layout_dirs:
        (root / sub).mkdir(parents=True, exist_ok=True)

    # Templates: .gitignore + entrypoint + (tex) refs.bib.
    _copy_template_if_missing(workspace.format, ".gitignore", root / ".gitignore")
    _copy_template_if_missing(
        workspace.format, workspace.entrypoint, root / workspace.entrypoint
    )
    if workspace.format == "tex":
        bib = root / "refs.bib"
        if not bib.exists():
            bib.write_text("% Generated by precis from kind='citation' refs.\n")

    # Git init (idempotent: skip if .git exists). Defensive: each
    # subprocess step has its own try/except so a failure at the
    # ``git add`` stage (NFS uid translation, .gitignore-rejected
    # paths, etc.) doesn't leave the repo in a state where ``.git``
    # exists but HEAD doesn't — which would make every subsequent
    # ``commit_put`` call fail with the same NFS error forever.
    if not (root / ".git").exists():
        try:
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            log.warning(
                "workspace.ensure_initialized: git init failed at %s: %s",
                root,
                exc,
            )
            return root
    # Initial commit so per-put commits have a parent. Idempotent:
    # skip if HEAD already points somewhere (a prior init landed).
    head = root / ".git" / "HEAD"
    has_commits = False
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        has_commits = rev.returncode == 0
    except FileNotFoundError:
        pass
    if not has_commits:
        try:
            subprocess.run(
                ["git", "add", "-A"], cwd=root,
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=precis@localhost",
                 "-c", "user.name=precis",
                 "commit", "-q", "-m",
                 f"workspace init (format={workspace.format})"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            # Most common cause on NFS: git uses the file owner for
            # commit metadata and the mount maps the user to a uid
            # the git tools don't expect. Log + continue; per-put
            # commits will retry with their own error surface.
            log.warning(
                "workspace.ensure_initialized: initial commit skipped "
                "at %s: %s (per-put commits will retry independently)",
                root,
                exc,
            )

    return root


def _layout_subdirs(format: str) -> tuple[str, ...]:
    """Per-format subdirectory layout."""
    if format == "tex":
        return ("tex", "pics", "data", "build")
    if format == "md":
        return ("sections", "pics", "data", "build")
    return ()


def _copy_template_if_missing(format: str, name: str, dest: Path) -> None:
    """Copy a workspace template file from package data if dest is missing."""
    if dest.exists():
        return
    try:
        template_root = resources.files("precis.data.workspace_templates")
        candidate = template_root / format / name  # type: ignore[union-attr]
        if not candidate.is_file():
            log.debug(
                "workspace template missing: %s/%s (skipping)", format, name
            )
            return
        text = candidate.read_text()
        dest.write_text(text)
    except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
        log.warning(
            "workspace.copy_template: %s/%s → %s failed: %s",
            format,
            name,
            dest,
            exc,
        )


# ── git commit per put ───────────────────────────────────────────


def commit_put(
    workspace_root: Path,
    *,
    summary: str,
    body: str = "",
) -> str | None:
    """Stage everything under workspace_root and commit with the message.

    Returns the new commit SHA, or None on failure (logged). Always
    runs ``git add -A`` first so any auto-generated companion files
    (refs.bib, main.tex `\\input{}` updates) land in the same commit.

    Templated message — caller composes summary + body from the
    Result chunk's structured data. No LLM in the loop.
    """
    if not (workspace_root / ".git").exists():
        log.warning(
            "workspace.commit_put: no .git in %s; skipping commit",
            workspace_root,
        )
        return None
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
        )
        # No-op-if-nothing-to-commit guard: `git diff --cached --quiet`
        # returns 1 when there ARE staged changes, 0 when clean.
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace_root,
            capture_output=True,
        )
        if diff.returncode == 0:
            log.debug("workspace.commit_put: no changes to commit in %s", workspace_root)
            return None
        message = summary if not body else f"{summary}\n\n{body}"
        subprocess.run(
            ["git",
             "-c", "user.email=precis@localhost",
             "-c", "user.name=precis",
             "commit", "-q", "-m", message],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return sha
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning(
            "workspace.commit_put: failed at %s: %s",
            workspace_root,
            exc,
        )
        return None


__all__ = [
    "SUPPORTED_FORMATS",
    "Workspace",
    "commit_put",
    "current_from_env",
    "ensure_initialized",
]
