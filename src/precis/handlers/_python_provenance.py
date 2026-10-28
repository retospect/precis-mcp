"""Python-only read provenance; checkout observations never certify indexed bytes.

A call-local collector follows existing cache reads, including cross-root
views. It adds prose to Response.body without changing the shared response
contract. File labels key the index snapshot so aliases of a mutable root
cannot mix versions. Inventory performs no indexing. Git is separately observed once per
root per response with one total time budget; no deployment identity fallback.

Reads carry one summary line per consulted root (placed before the content so
runtime pagination keeps it on page one); the full per-file detail block is
the drill-down ``view='provenance'``.
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

from precis.python_index.types import IndexObservation, ModuleIndex, RepoIndex
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


@dataclass(frozen=True)
class GitFacts:
    """One non-atomic Git observation of a root; never attests indexed bytes."""

    state: str = "unobserved"
    worktree: str = "unknown"
    head: str = "unknown"
    branch: str = "unknown"
    dirty: str = "unknown"
    root: str = ""
    started: str = ""
    finished: str = ""

    def detail(self) -> str:
        values = {
            "worktree": self.worktree,
            "head": self.head,
            "branch": self.branch,
            "dirty": self.dirty,
        }
        return (
            f"Git separately observed: state={self.state}; "
            + "; ".join(f"{key}={value!r}" for key, value in values.items())
            + f"; dirty_scope={self.root!r} (all Git-visible files, including untracked; "
            "ignored files and submodule contents excluded); indexed bytes are not attested as HEAD; "
            f"observation={self.started}..{self.finished} (non-atomic, <=2s budget)"
        )

    def summary(self) -> str:
        unavailable = self.state.startswith("unavailable")
        if unavailable and self.head == "unknown":
            return "git UNAVAILABLE"
        head = self.head[:8] if self.head != "unknown" else "head-unknown"
        verdict = {
            "no": "clean (observed)",
            "yes": "DIRTY (observed)",
        }.get(self.dirty, "dirty=UNKNOWN")
        flag = " PARTIAL" if self.state != "observed" else ""
        return f"git {head} {verdict}{flag}"


def _observe_git(root: Path, git_dir: Path | None = None) -> GitFacts:
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
                **(
                    {"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(root)}
                    if git_dir is not None
                    else {}
                ),
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
    return GitFacts(
        state=state,
        worktree=values["worktree"],
        head=values["head"],
        branch=values["branch"],
        dirty=values["dirty"],
        root=str(root),
        started=started,
        finished=_utc_now(),
    )


def _parse_errors(idx: RepoIndex) -> int:
    return sum(mod.parse_error is not None for mod in idx._by_file.values())


#: A partial refresh gets a cost note only when it actually reparsed
#: the majority of the corpus or took long enough to feel (gr525769).
_REFRESH_NOTE_MIN_S = 1.0


def _obs_seconds(observation: IndexObservation) -> float:
    """Wall seconds of an index observation, parsed from its ISO stamps."""
    try:
        t0 = datetime.fromisoformat(observation.started_at)
        t1 = datetime.fromisoformat(observation.finished_at)
    except ValueError:
        return 0.0
    return max(0.0, (t1 - t0).total_seconds())


def _corpus_cost_note(idx: RepoIndex) -> str | None:
    """``cold corpus: built in N s`` / refresh note, or None (gr525769).

    The first read against a worktree corpus pays a full walk+parse
    (~17 s on a 4k-file tree) while later calls stat-check in <1 s —
    the header says which one this call was so the agent pays the
    cold build once and stays on the kind. A refresh that reparsed
    names the count (``reparsed 1 file``); one whose time went into
    the stat-walk instead says ``checked N files``.
    """
    obs = idx.observation
    if obs is None:
        return None
    elapsed = _obs_seconds(obs)
    if obs.reused == 0:
        if not obs.reparsed:
            return None
        return f"cold corpus: built in {elapsed:.1f} s; later calls reuse it"
    if obs.reparsed >= obs.reused or elapsed >= _REFRESH_NOTE_MIN_S:
        if obs.reparsed:
            noun = "file" if obs.reparsed == 1 else "files"
            detail = f"reparsed {obs.reparsed} {noun}"
        else:
            noun = "file" if obs.reused == 1 else "files"
            detail = f"checked {obs.reused} {noun}"
        return f"corpus refresh: {detail} in {elapsed:.1f} s; later calls reuse it"
    return None


def empty_root_reason(root: Path) -> str:
    """Why an existing root can index zero modules (gr520147).

    The dir exists (construction rejects non-dirs) yet holds nothing an
    agent can see: either literally no entries — the signature of a host
    path bind-mounted into a container VM that does not share it — or
    files but not a single ``.py``.
    """
    try:
        next(root.iterdir())
    except StopIteration:
        return (
            "directory is empty (a host path the container VM does not "
            "share bind-mounts as an empty dir)"
        )
    except OSError:
        return "directory cannot be listed"
    return "no .py files under it"


def _render_summary(
    alias: str,
    root: Path,
    idx: RepoIndex | None,
    reads: _Reads,
    git: GitFacts,
    *,
    expected_root_ok: bool,
) -> str:
    """One line: checkout, corpus, git; loud on every non-default state."""
    parts = [f"checkout: {alias}@{root}"]
    if expected_root_ok:
        parts[0] += " (expected_root ok)"
    touched = list(reads.files.get(id(idx), {}).items()) if idx is not None else []
    drill_id = f"{alias}/{touched[0][0]}" if len(touched) == 1 else alias
    if not root.is_dir():
        parts.append("root UNAVAILABLE")
    if idx is None:
        parts.append("corpus not indexed by this call")
    else:
        fingerprint = corpus_fingerprint(idx)
        observation = idx.observation
        if observation is None:
            freshness = "freshness UNKNOWN"
        elif observation.issues:
            freshness = f"PARTIAL WALK ({len(observation.issues)} issues)"
        else:
            freshness = "stat-checked"
        errors = _parse_errors(idx)
        errtxt = f"PARSE ERRORS {errors}" if errors else "0 parse errors"
        if not idx._by_file:
            corpus = f"corpus EMPTY (0 files, {freshness}: {empty_root_reason(root)})"
        else:
            corpus = (
                f"corpus {fingerprint[:8] if fingerprint != 'unknown' else 'UNKNOWN'} "
                f"({len(idx._by_file)} files, {freshness}, {errtxt})"
            )
        if len(touched) == 1:
            path, mod = touched[0]
            corpus += f" file {path} {(mod.bytes_sha256 or 'UNKNOWN')[:8]}"
        parts.append(corpus)
        if (note := _corpus_cost_note(idx)) is not None:
            parts.append(note)
    parts.append(git.summary())
    return (
        " · ".join(parts)
        + f" — details: get(kind='python', id={drill_id!r}, view='provenance')"
    )


def _render_root(
    alias: str, root: Path, idx: RepoIndex | None, reads: _Reads, git: GitFacts
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
        if not idx._by_file:
            lines.append(f"Root EMPTY: {empty_root_reason(root)}")
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
        errors = _parse_errors(idx)
        lines.append(f"Parse-error files represented with limited symbols: {errors}")
        for path, mod in reads.files.get(id(idx), {}).items():
            lines.append(
                f"Indexed file: {path!r}; bytes_sha256={mod.bytes_sha256 or 'unknown'}; "
                f"indexed_at={mod.indexed_at or 'unknown'}"
            )
    lines.append(git.detail())
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
            gdir = getattr(self, "git_dir_for", lambda _r: None)
            git = {
                root: _observe_git(root, g) if (g := gdir(root)) else _observe_git(root)
                for root in dict.fromkeys(roots.values())
            }
            detail = kwargs.get("view") == "provenance"
            if detail:
                blocks = [
                    _render_root(
                        alias, root, reads.indexes.get(alias), reads, git[root]
                    )
                    for alias, root in roots.items()
                ]
            else:
                ok = kwargs.get("expected_root") is not None
                blocks = [
                    _render_summary(
                        alias,
                        root,
                        reads.indexes.get(alias),
                        reads,
                        git[root],
                        expected_root_ok=ok,
                    )
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
            # Runtime pagination keeps the head: identity goes before the
            # potentially large source/search payload, after the headline.
            body = headline + "\n\n" + "\n\n".join(blocks)
            if not detail:
                body += "\n\nPython content:\n" + content
            elif content:
                body += "\n\n" + content
            return replace(response, body=body)
        finally:
            _reads.reset(token)

    return wrapped
