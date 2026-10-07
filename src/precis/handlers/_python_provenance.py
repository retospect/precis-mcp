"""Python-only read provenance; checkout observations never certify indexed bytes.

A call-local collector follows existing cache reads, including cross-root
views. It adds prose to Response.body without changing the shared response
contract. File labels key the index snapshot so aliases of a mutable root
cannot mix versions. Inventory performs no indexing. Git is separately observed once per
root per response with one total time budget; no deployment identity fallback.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from precis.python_index.types import ModuleIndex, RepoIndex
from precis.response import Response


@dataclass
class _Reads:
    indexes: dict[str, RepoIndex] = field(default_factory=dict)
    files: dict[int, dict[str, ModuleIndex]] = field(default_factory=dict)
    external: list[str] = field(default_factory=list)


_reads: ContextVar[_Reads | None] = ContextVar("python_provenance", default=None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def note_index(alias: str, idx: RepoIndex) -> None:
    if (reads := _reads.get()) is not None:
        reads.indexes[alias] = idx


def indexed_file(alias: str, path: str) -> tuple[RepoIndex, ModuleIndex] | None:
    reads = _reads.get()
    if reads is not None and (idx := reads.indexes.get(alias)) is not None:
        if (mod := idx.file(path)) is not None:
            return idx, mod
    return None


def note_file(idx: RepoIndex, mod: ModuleIndex) -> None:
    if (reads := _reads.get()) is not None:
        reads.files.setdefault(id(idx), {})[mod.file] = mod


def note_external(path: Path, raw: bytes | None, state: str) -> None:
    if (reads := _reads.get()) is not None:
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else "unknown"
        reads.external.append(
            f"Non-Python observation: {str(path)!r}; bytes_sha256={digest}; "
            f"state={state}; observed_at={_utc_now()}; "
            "excluded from Python corpus fingerprint"
        )


def corpus_fingerprint(idx: RepoIndex) -> str:
    """SHA256 of version tag, then length-prefixed UTF-8 path + raw digest.

    Uses the represented file map (not just module names, which can collide).
    Unknown module digests explicitly invalidate the aggregate digest.
    """
    digest = hashlib.sha256(b"precis-python-corpus-v1\0")
    for path, mod in sorted(idx._by_file.items()):
        if mod.bytes_sha256 is None:
            return "unknown"
        encoded = path.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(mod.bytes_sha256))
    return digest.hexdigest()


def _observe_git(root: Path) -> str:
    started = _utc_now()
    deadline = time.monotonic() + 2.0
    values = {
        "worktree": "unknown",
        "head": "unknown",
        "branch": "unknown",
        "dirty": "unknown",
    }
    state = "observed"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 2.0)
        return subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=remaining,
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key
                    not in {
                        "GIT_DIR",
                        "GIT_WORK_TREE",
                        "GIT_COMMON_DIR",
                        "GIT_INDEX_FILE",
                    }
                },
                "GIT_OPTIONAL_LOCKS": "0",
            },
            check=False,
        )

    try:
        top = run("rev-parse", "--show-toplevel")
        if top.returncode:
            state = "unavailable (non-Git or inaccessible)"
        else:
            values["worktree"] = top.stdout.strip()
            head = run("rev-parse", "--verify", "HEAD")
            if head.returncode == 0:
                values["head"] = head.stdout.strip()
            branch = run("symbolic-ref", "--quiet", "--short", "HEAD")
            if branch.returncode == 0:
                values["branch"] = branch.stdout.strip()
            elif branch.returncode == 1:
                values["branch"] = "detached"
            # Status may invoke clean/process filters. Decline that observation
            # rather than executing configured repository commands on a read.
            filters = run("config", "--get-regexp", r"^filter\..*\.(clean|process)$")
            if filters.returncode != 1:
                state = "partial observation (dirty unknown: executable filters or config unavailable)"
            else:
                status = run(
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                    "--",
                    ".",
                )
                if status.returncode == 0:
                    values["dirty"] = "yes" if status.stdout else "no"
                if status.returncode:
                    state = "partial observation"
            if head.returncode or branch.returncode > 1:
                state = "partial observation"
    except (OSError, subprocess.TimeoutExpired) as exc:
        state = f"unavailable/partial ({type(exc).__name__})"
    return (
        f"Git separately observed: state={state}; "
        + "; ".join(f"{key}={value!r}" for key, value in values.items())
        + f"; dirty_scope={str(root)!r} (all Git-visible files, including untracked; "
        "ignored files and submodule contents excluded); indexed bytes are not attested as HEAD; "
        f"observation={started}..{_utc_now()} (non-atomic, <=2s budget)"
    )


def _render_root(
    alias: str, root: Path, idx: RepoIndex | None, reads: _Reads, git: str
) -> str:
    lines = [
        f"Python provenance: alias={alias!r}; root={str(root)!r}; available={root.is_dir()}"
    ]
    if idx is None:
        lines.append(
            "Index freshness: not indexed by this call; indexed fingerprint=unknown"
        )
    else:
        observation = idx.observation
        lines.append(
            f"Indexed Python corpus: bytes_sha256={corpus_fingerprint(idx)}; "
            f"files={len(idx._by_file)}; framing=precis-python-corpus-v1/uint64-path-length/UTF8-path/raw-SHA256; "
            "excludes symlinks, hidden/skip directories and non-Python files; not a Git tree hash"
        )
        if observation is None:
            lines.append("Index freshness: unknown (no cache observation)")
        else:
            state = "partial/unstable" if observation.issues else "stat-checked"
            lines.append(
                f"Index freshness: {state}; walk={observation.started_at}..{observation.finished_at}; "
                f"reparsed={observation.reparsed}; reused={observation.reused}; "
                "reused content-not-revalidated; non-atomic tree observation"
            )
            lines.extend(f"Index limitation: {issue}" for issue in observation.issues)
        errors = sum(mod.parse_error is not None for mod in idx._by_file.values())
        lines.append(f"Parse-error files represented with limited symbols: {errors}")
        for path, mod in reads.files.get(id(idx), {}).items():
            lines.append(
                f"Indexed file: {path!r}; bytes_sha256={mod.bytes_sha256 or 'unknown'}; "
                f"indexed_at={mod.indexed_at or 'unknown'}"
            )
    lines.append(git)
    return "\n".join(lines)


def with_provenance(method: Callable[..., Response]) -> Callable[..., Response]:
    """Collect only roots consulted by this read, preserving response fields."""

    @wraps(method)
    def wrapped(self: Any, **kwargs: Any) -> Response:
        reads = _Reads()
        token = _reads.set(reads)
        try:
            response = method(self, **kwargs)
            roots = {alias: idx.root for alias, idx in reads.indexes.items()}
            if method.__name__ == "get" and kwargs.get("id") in (None, "", "/"):
                roots = self.roots
            git = {root: _observe_git(root) for root in dict.fromkeys(roots.values())}
            blocks = [
                _render_root(alias, root, reads.indexes.get(alias), reads, git[root])
                for alias, root in roots.items()
            ]
            blocks.extend(reads.external)
            if kwargs.get("view") == "runtrace":
                blocks.append(
                    "Runtrace: indexed provenance does not attest executed bytes or imported source."
                )
            if not blocks:
                return response
            headline, _separator, content = response.body.partition("\n")
            # Runtime pagination keeps the head. Preserve the existing headline,
            # but put identity before potentially large source/search payloads.
            body = (
                headline
                + "\n\n"
                + "\n\n".join(blocks)
                + "\n\nPython content:\n"
                + content
            )
            return replace(response, body=body)
        finally:
            _reads.reset(token)

    return wrapped
