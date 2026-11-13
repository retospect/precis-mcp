"""Python-only read provenance; checkout observations never certify indexed bytes.

A call-local collector follows existing cache reads, including cross-root
views. It adds prose to Response.body without changing the shared response
contract. File labels key the index snapshot so aliases of a mutable root
cannot mix versions. Inventory performs no indexing. Git is separately observed once per
root per response with one total time budget; no deployment identity fallback.

Reads carry one summary line per consulted root (placed before the content so
runtime pagination keeps it on page one); the full per-file detail block is
the drill-down ``view='provenance'``.

When the root names a package (nearest ``pyproject.toml`` ``project.name``)
and the server's own pyproject pins that package to a git source
(``[tool.uv.sources]`` or a direct ``pkg @ git+…@ref`` requirement), the
observed HEAD is compared to the pin and a mismatch is printed on the
provenance line (gr569538 — an indexed checkout at a stale rev silently
read as the pinned engine).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
import tomllib
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from precis.errors import NotFound
from precis.python_index.types import IndexObservation, ModuleIndex, RepoIndex
from precis.response import Response


@dataclass
class _Reads:
    indexes: dict[str, RepoIndex] = field(default_factory=dict)
    files: dict[int, dict[str, ModuleIndex]] = field(default_factory=dict)
    external: list[str] = field(default_factory=list)


_reads: ContextVar[_Reads | None] = ContextVar("python_provenance", default=None)

WORKTREE_NOTE = (
    "note: your worktree is not indexed by this server; grep uncommitted code."
)


def _missing_caller_tree(handler: Any, kwargs: dict[str, Any]) -> bool:
    """Check registered aliases without discovering roots or warming indexes."""
    registry = getattr(handler, "worktrees", None)
    registered = registry.snapshot() if registry is not None else {}
    available = set(handler.roots)
    available.update(registered)
    target = str(kwargs.get("scope") or kwargs.get("id") or "")
    alias = target.split("::", 1)[0].split("/", 1)[0].split("~", 1)[0]
    if alias.startswith("wt-"):
        return alias not in available
    # The server cannot infer the caller's cwd from its own process cwd.
    return not registered and not any(alias.startswith("wt-") for alias in available)


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


def _git_env(root: Path, git_dir: Path | None) -> dict[str, str]:
    """The scrubbed env every provenance git call runs under.

    Caller ``GIT_*`` vars are stripped so an ambient checkout cannot steer
    the observation; optional locks stay off so a read never writes.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
        }
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if git_dir is not None:
        env["GIT_DIR"] = str(git_dir)
        env["GIT_WORK_TREE"] = str(root)
    return env


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
            env=_git_env(root, git_dir),
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


# ── dependency-pin comparison (gr569538) ────────────────────────────────
#
# A mounted root can drift from the engine the server actually pins: the
# catpath alias indexed a 0.35.0 checkout while pyproject pinned 0.50.0,
# so ``catpath::`` verdicts silently described code the deployment does
# not run. When the root's nearest pyproject names a package and the
# server's pyproject git-pins that name, the observed HEAD is compared
# to the pin and a mismatch is printed on the provenance line.

#: Env var pointing at the pyproject whose ``[tool.uv.sources]``/direct
#: ``git+`` requirements pin this server's dependencies. Overrides the
#: default discovery (the ``pyproject.toml`` nearest the running
#: ``precis`` package) for installs where that file is not an ancestor —
#: and for tests.
PIN_PYPROJECT_ENV = "PRECIS_PYTHON_PIN_PYPROJECT"

#: See ``_python_entries``: ``src/<pkg>/``-layout aliases can sit below
#: the file that names the project.
_PYPROJECT_SEARCH_DEPTH = 4

_HEX_RE = re.compile(r"[0-9a-fA-F]{7,40}")
#: ``pkg @ git+<url>[ @<ref>][ ; markers]`` direct-URL requirement.
_GIT_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*@\s*git\+(?P<url>[^@\s]+?)"
    r"(?:@(?P<ref>[^\s;]+))?\s*(?:;.*)?$"
)


def _find_pyproject(start: Path) -> Path | None:
    """Walk up at most `_PYPROJECT_SEARCH_DEPTH` levels looking for
    `pyproject.toml`. Returns the first match or None."""
    cur = start.resolve()
    for _ in range(_PYPROJECT_SEARCH_DEPTH):
        candidate = cur / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if cur == cur.parent:
            return None
        cur = cur.parent
    return None


def _read_toml(path: Path) -> dict[str, Any] | None:
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None


#: An installed package sits under ``<venv>/lib/pythonX/site-packages``;
#: the venv root (holding ``pyvenv.cfg``) is deeper than
#: ``_PYPROJECT_SEARCH_DEPTH`` reaches from the package dir.
_VENV_SEARCH_DEPTH = 6


def _venv_root(start: Path) -> Path | None:
    """The enclosing virtualenv root — the directory holding ``pyvenv.cfg``."""
    cur = start.resolve()
    for _ in range(_VENV_SEARCH_DEPTH):
        if (cur / "pyvenv.cfg").is_file():
            return cur
        if cur == cur.parent:
            return None
        cur = cur.parent
    return None


def _pinning_pyproject() -> Path | None:
    """The pyproject whose dependency pins this server runs against.

    ``PIN_PYPROJECT_ENV`` wins; otherwise the ``pyproject.toml`` nearest
    the running ``precis`` package — the project checkout in a source
    install, or the lock project a ``.venv`` lives under (a wheel
    install sits below ``_PYPROJECT_SEARCH_DEPTH`` inside
    ``<venv>/lib/pythonX/site-packages``, so the venv root is located
    first and the search continues above it). ``None`` when no
    candidate exists (wheel-only install outside any project).
    """
    override = os.environ.get(PIN_PYPROJECT_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    try:
        import precis
    except ImportError:  # pragma: no cover — provenance serves precis
        return None
    start = Path(precis.__file__).resolve().parent
    if (direct := _find_pyproject(start)) is not None:
        return direct
    if (venv := _venv_root(start)) is not None:
        return _find_pyproject(venv.parent)
    return None


def _norm_name(name: str) -> str:
    """PEP 503 normalisation for comparing project/requirement names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _root_package_name(root: Path) -> str | None:
    """The ``project.name`` the root provides, from its nearest pyproject."""
    pyproject = _find_pyproject(root)
    if pyproject is None:
        return None
    data = _read_toml(pyproject)
    if data is None:
        return None
    name = (data.get("project") or {}).get("name")
    return name if isinstance(name, str) and name else None


def _git_pin(package: str, data: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(kind, value)`` git pin for ``package``, or ``None``.

    ``[tool.uv.sources]`` first: ``rev``/``tag``/``branch`` in that order
    (``rev`` is the only one naming an immutable commit). A git source
    with none of the three is a moving remote-HEAD pin — reported as
    ``('remote', '')`` so the line can say it cannot be verified offline.
    Then direct ``pkg @ git+url@ref`` requirements.
    """
    wanted = _norm_name(package)
    sources = ((data.get("tool") or {}).get("uv") or {}).get("sources") or {}
    for name, spec in sources.items():
        if not isinstance(name, str) or _norm_name(name) != wanted:
            continue
        if not isinstance(spec, dict) or not isinstance(spec.get("git"), str):
            continue
        for kind in ("rev", "tag", "branch"):
            value = spec.get(kind)
            if isinstance(value, str) and value:
                return kind, value
        return "remote", ""
    project = data.get("project") or {}
    deps: list[Any] = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        deps.extend(group or [])
    for dep in deps:
        if not isinstance(dep, str):
            continue
        match = _GIT_REQ_RE.match(dep)
        if match is not None and _norm_name(match["name"]) == wanted:
            return "ref", match["ref"] or ""
    return None


def _remote_tracking_refs(
    root: Path, git_dir: Path | None, branch: str
) -> dict[str, str]:
    """``refs/remotes/<remote>/<branch>`` → commit, as the clone last fetched.

    A branch pin names remote lineage; the only local commit identity
    for it is the remote-tracking refs this checkout fetched. A local
    branch tip is self-evidence (on-branch HEAD always equals it), so
    it cannot verify the pin.

    The remote is the single path component after ``refs/remotes/``;
    the pin must equal the *complete* branch path that follows, so a
    ``release`` pin cannot borrow ``origin/feature/release``'s head
    (review td663785 — suffix matching reported a false match) while a
    slashed pin like ``feature/release`` still resolves.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                "refs/remotes",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            env=_git_env(root, git_dir),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    prefix = "refs/remotes/"
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, name = line.partition(" ")
        if not name.startswith(prefix) or not _HEX_RE.fullmatch(sha):
            continue
        _remote, sep, remote_branch = name[len(prefix) :].partition("/")
        if sep and remote_branch == branch:
            refs[name] = sha.lower()
    return refs


def _resolve_ref(root: Path, git_dir: Path | None, ref: str) -> str | None:
    """Resolve ``ref`` to a commit sha inside the observed checkout.

    One extra git call with its own small budget — a pin a local clone
    cannot resolve (a tag never fetched) is UNVERIFIED, never an error.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{ref}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            env=_git_env(root, git_dir),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


@dataclass(frozen=True)
class PinFacts:
    """Indexed HEAD vs the server's dependency pin for the root's package.

    ``state``: ``match`` (pin verified against the observed checkout),
    ``mismatch`` (checked out somewhere else), ``unverified`` (a pin
    exists but could not be compared — HEAD unobserved, ref not in the
    local clone, a moving remote pin, or a branch pin without a
    remote-tracking ref), ``unpinned`` (package named, no git pin),
    ``unchecked`` (no package name or no pinning pyproject — the check
    never ran).
    """

    state: str
    package: str = ""
    pin_kind: str = ""
    pin: str = ""
    observed: str = ""
    source: str = ""
    why: str = ""

    def summary(self) -> str | None:
        """The loud fragment for the one-line checkout summary, or None."""
        if self.state == "mismatch":
            pin = self.pin[:12] if self.pin_kind in ("rev", "ref") else self.pin
            observed = (
                self.observed[:8] if _HEX_RE.fullmatch(self.observed) else self.observed
            )
            return (
                f"PIN MISMATCH {self.package}: "
                f"pinned {self.pin_kind} {pin} indexed {observed}"
            )
        if self.state == "unverified":
            return f"pin {self.package} UNVERIFIED ({self.why})"
        return None

    def detail(self) -> str:
        """The ``Pin:`` line of the ``view='provenance'`` drill-down."""
        if self.state in ("match", "mismatch"):
            verdict = "match" if self.state == "match" else "MISMATCH"
            tail = (
                ""
                if self.state == "match"
                else " — index does not reflect the pinned engine"
            )
            basis = f"; {self.why}" if self.why else ""
            return (
                f"Pin: package={self.package!r}; pinned {self.pin_kind} "
                f"{self.pin!r} (from {self.source!r}); indexed {self.observed!r}; "
                f"verdict={verdict}{basis}{tail}"
            )
        if self.state == "unverified":
            return (
                f"Pin: package={self.package!r}; pinned {self.pin_kind} "
                f"{self.pin!r} (from {self.source!r}); "
                f"verdict=UNVERIFIED — {self.why}"
            )
        if self.state == "unpinned":
            return f"Pin: package={self.package!r}; no git pin in {self.source!r}"
        return f"Pin: {self.why or 'unchecked'}"


def _lazy_pin_context() -> Callable[[], tuple[Path, dict[str, Any]] | None]:
    """Memoised ``(pyproject, data)`` for the server's pinning pyproject.

    Located and parsed at most once per response, and only once some
    consulted root actually names a package — a bare file tree never
    pays for the read.
    """
    cache: list[tuple[Path, dict[str, Any]] | None] = []

    def get() -> tuple[Path, dict[str, Any]] | None:
        if not cache:
            pyproject = _pinning_pyproject()
            data = _read_toml(pyproject) if pyproject is not None else None
            cache.append(
                (pyproject, data)
                if pyproject is not None and data is not None
                else None
            )
        return cache[0]

    return get


def _pin_facts(
    root: Path,
    git: GitFacts,
    git_dir: Path | None,
    pin_context: Callable[[], tuple[Path, dict[str, Any]] | None],
) -> PinFacts:
    """Compare the observed checkout to the server's pin for its package."""
    package = _root_package_name(root)
    if package is None:
        return PinFacts(
            state="unchecked",
            why="unchecked — no pyproject.toml names a package at this root",
        )
    context = pin_context()
    if context is None:
        return PinFacts(
            state="unchecked",
            package=package,
            why=f"unchecked — no pinning pyproject found (set {PIN_PYPROJECT_ENV})",
        )
    pyproject, data = context
    spec = _git_pin(package, data)
    if spec is None:
        return PinFacts(state="unpinned", package=package, source=str(pyproject))
    kind, pin = spec
    if kind == "remote":
        return PinFacts(
            state="unverified",
            package=package,
            pin_kind="remote",
            source=str(pyproject),
            why="git source names no rev/tag/branch; remote HEAD unverifiable offline",
        )
    if kind == "branch":
        head = git.head
        if head == "unknown" or not _HEX_RE.fullmatch(head):
            return PinFacts(
                state="unverified",
                package=package,
                pin_kind=kind,
                pin=pin,
                source=str(pyproject),
                why="indexed HEAD unobserved",
            )
        tips = _remote_tracking_refs(root, git_dir, pin)
        commits = set(tips.values())
        if len(commits) == 1:
            return PinFacts(
                state="match" if commits.pop() == head.lower() else "mismatch",
                package=package,
                pin_kind=kind,
                pin=pin,
                observed=head,
                source=str(pyproject),
                why=f"commit identity via {next(iter(tips))} "
                "(remote head at last fetch; remote may have moved)",
            )
        if commits:
            why = f"remote-tracking refs for {pin!r} disagree: " + ", ".join(
                sorted(tips)
            )
        else:
            why = (
                f"no remote-tracking ref for branch {pin!r}; "
                "remote head unverifiable offline"
            )
        return PinFacts(
            state="unverified",
            package=package,
            pin_kind=kind,
            pin=pin,
            source=str(pyproject),
            why=why,
        )
    head = git.head
    if head == "unknown" or not _HEX_RE.fullmatch(head):
        return PinFacts(
            state="unverified",
            package=package,
            pin_kind=kind,
            pin=pin,
            source=str(pyproject),
            why="indexed HEAD unobserved",
        )
    if _HEX_RE.fullmatch(pin):
        return PinFacts(
            state="match" if head.lower().startswith(pin.lower()) else "mismatch",
            package=package,
            pin_kind=kind,
            pin=pin,
            observed=head,
            source=str(pyproject),
        )
    resolved = _resolve_ref(root, git_dir, pin)
    if resolved is None:
        return PinFacts(
            state="unverified",
            package=package,
            pin_kind=kind,
            pin=pin,
            source=str(pyproject),
            why="pin not resolvable in the local checkout",
        )
    return PinFacts(
        state="match" if resolved == head else "mismatch",
        package=package,
        pin_kind=kind,
        pin=pin,
        observed=head,
        source=str(pyproject),
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
    pin: PinFacts,
    *,
    expected_root_ok: bool,
) -> str:
    """One line: checkout, corpus, git, pin; loud on every non-default state."""
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
    if (pinned := pin.summary()) is not None:
        parts.append(pinned)
    return (
        " · ".join(parts)
        + f" — details: get(kind='python', id={drill_id!r}, view='provenance')"
    )


def _render_root(
    alias: str,
    root: Path,
    idx: RepoIndex | None,
    reads: _Reads,
    git: GitFacts,
    pin: PinFacts,
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
    lines.append(pin.detail())
    return "\n".join(lines)


def with_provenance(method: Callable[..., Response]) -> Callable[..., Response]:
    """Collect only roots consulted by this read, preserving response fields."""

    @wraps(method)
    def wrapped(self: Any, **kwargs: Any) -> Response:
        reads = _Reads()
        token = _reads.set(reads)
        try:
            try:
                response = method(self, **kwargs)
            except NotFound as exc:
                if (
                    method.__name__ in ("get", "search", "eye")
                    and _missing_caller_tree(self, kwargs)
                    and WORKTREE_NOTE not in exc.cause
                ):
                    exc.cause += "\n" + WORKTREE_NOTE
                    exc.args = (exc.cause,)
                raise
            roots = {alias: idx.root for alias, idx in reads.indexes.items()}
            if method.__name__ == "get" and kwargs.get("id") in (None, "", "/"):
                roots = self.roots
            gdir = getattr(self, "git_dir_for", lambda _r: None)
            git = {
                root: _observe_git(root, g) if (g := gdir(root)) else _observe_git(root)
                for root in dict.fromkeys(roots.values())
            }
            pin_context = _lazy_pin_context()
            pin = {
                root: _pin_facts(root, git[root], gdir(root), pin_context)
                for root in dict.fromkeys(roots.values())
            }
            detail = kwargs.get("view") == "provenance"
            if detail:
                blocks = [
                    _render_root(
                        alias,
                        root,
                        reads.indexes.get(alias),
                        reads,
                        git[root],
                        pin[root],
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
                        pin[root],
                        expected_root_ok=ok,
                    )
                    for alias, root in roots.items()
                ]
            blocks.extend(reads.external)
            if (
                method.__name__ in ("get", "search", "eye")
                and _missing_caller_tree(self, kwargs)
                and WORKTREE_NOTE not in response.body
            ):
                blocks.append(WORKTREE_NOTE)
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
