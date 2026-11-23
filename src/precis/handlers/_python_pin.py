"""origin/main-pinned read indexes for volatile git checkouts (gr762343).

A static python root can name a builder checkout whose HEAD moves between
branches (``PRECIS_FIX_REPO_DIR`` under plan_tick, where the working tree
is whatever slice a builder last checked out).  The regular index follows
the working tree — the per-file stat check re-indexes changed files, but
"changed" means "changed since this server's last read", so a read
between checkouts answered from whatever branch happened to be on disk.

Aliases listed in ``PRECIS_PYTHON_PIN_ROOTS`` instead answer from a
materialized copy of ``refs/remotes/origin/main``.  On access the pinner
fetches (TTL-bounded), resolves the remote-tracking sha, and — when the
sha moved or no copy exists yet — archives the tree into the pin cache,
where the ordinary :class:`precis.python_index.RepoCache` indexes it
(content-addressed trees stat-check warm on later reads).  Every git
step is subprocess-bounded; a step that overruns — or a clone without
``origin/main`` — degrades to the newest pinned tree already on disk, or
finally to the working tree, and the note carried on the read says which.

Anchors survive a pin move the same way they survive any reindex: by
re-resolving the symbol/snippet, never by path:line (gr595549).  Writes
never go through the pinner: put/edit/delete keep targeting the
configured root's working tree (``_resolve_writable`` bypasses
``PythonHandler._index``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from precis.config import cache_root
from precis.handlers._python_provenance import _git_env

log = logging.getLogger(__name__)

#: Comma-separated aliases whose reads answer ``refs/remotes/origin/main``.
PIN_ROOTS_ENV = "PRECIS_PYTHON_PIN_ROOTS"
#: Per-alias override for the on-disk pin cache (``off`` disables pinning:
#: reads fall back to the working tree with a note).
PIN_CACHE_ENV = "PRECIS_PYTHON_PIN_CACHE"
#: Minimum seconds between fetches of the same root (reads otherwise use
#: the remote-tracking ref as of the last fetch).
PIN_FETCH_TTL_ENV = "PRECIS_PYTHON_PIN_FETCH_TTL_S"
#: Subprocess budget for one ``git fetch``.
PIN_FETCH_TIMEOUT_ENV = "PRECIS_PYTHON_PIN_FETCH_TIMEOUT_S"
#: Subprocess budget for one ``git archive`` + extract.
PIN_ARCHIVE_TIMEOUT_ENV = "PRECIS_PYTHON_PIN_ARCHIVE_TIMEOUT_S"

_DEFAULT_FETCH_TTL_S = 120.0
_DEFAULT_FETCH_TIMEOUT_S = 15.0
_DEFAULT_ARCHIVE_TIMEOUT_S = 60.0
#: Materialized trees kept per root; older ones are pruned after a fresh
#: materialization so the cache cannot grow without bound.
_KEEP_TREES = 2

#: The only ref a pinned root answers from.
PIN_REF = "refs/remotes/origin/main"

_MARKER = ".precis-pin-sha"


def parse_pin_roots(raw: str | None) -> set[str]:
    """Parse ``PRECIS_PYTHON_PIN_ROOTS`` (comma-separated aliases)."""
    if not raw:
        return set()
    return {entry.strip() for entry in raw.split(",") if entry.strip()}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("%s: invalid value %r; using %s", name, raw, default)
        return default


def default_pin_cache_dir() -> Path | None:
    """Pin cache root for a handler-constructed :class:`RootPinner`.

    Same trust domain as the corpus snapshot (``cache_root('python-pin')``
    under ``~/.cache/precis``); ``PRECIS_PYTHON_PIN_CACHE`` overrides and
    ``off`` disables pinning (working-tree reads with a note).
    """
    raw = os.environ.get(PIN_CACHE_ENV)
    if raw is not None:
        raw = raw.strip()
        if not raw or raw.lower() == "off":
            return None
        return Path(raw).expanduser()
    return cache_root("python-pin")


@dataclass(frozen=True, slots=True)
class PinDecision:
    """Where one read of a pinned alias answers from, and what to say.

    ``index_root`` is what the ``RepoCache`` indexes — a materialized
    ``origin/main`` tree when ``pinned``, else the working tree.
    ``sha`` is the pinned commit (None on working-tree fallback).
    ``note`` is a complete fragment for the read's summary line, or None
    when the caller should compose it from the index observation (a
    fresh materialization names the reparsed count and seconds there).
    """

    index_root: Path
    sha: str | None
    note: str | None
    pinned: bool


@dataclass(slots=True)
class _RootPinState:
    last_fetch_monotonic: float = 0.0
    last_good: tuple[str, Path] | None = None


def _fetched_fragment(sha: str, fetch_note: str | None) -> str | None:
    """The steady-state pinned fragment, naming a failed fetch when one."""
    base = f"pinned to origin/main@{sha[:8]}"
    if fetch_note:
        return f"{base} ({fetch_note}; remote-tracking ref as of the last fetch)"
    return None


class RootPinner:
    """Per-root resolution of ``origin/main`` pin decisions.

    Serializes on a per-root lock like ``RepoCache``: a fetch plus
    materialization is one unit of work, and a second reader waits and
    reuses the result.  Git runs under the scrubbed provenance env with
    hard subprocess timeouts — a slow remote degrades to the last good
    tree instead of stalling reads.
    """

    def __init__(
        self,
        cache_dir: Path | None,
        *,
        fetch_ttl_s: float = _DEFAULT_FETCH_TTL_S,
        fetch_timeout_s: float = _DEFAULT_FETCH_TIMEOUT_S,
        archive_timeout_s: float = _DEFAULT_ARCHIVE_TIMEOUT_S,
        keep: int = _KEEP_TREES,
    ) -> None:
        self._cache_dir = cache_dir
        self._fetch_ttl_s = fetch_ttl_s
        self._fetch_timeout_s = fetch_timeout_s
        self._archive_timeout_s = archive_timeout_s
        self._keep = max(1, keep)
        self._locks: dict[Path, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._state: dict[Path, _RootPinState] = {}

    @classmethod
    def from_env(cls) -> RootPinner:
        """The handler's default pinner (env-tuned budgets, default cache)."""
        return cls(
            default_pin_cache_dir(),
            fetch_ttl_s=_env_float(PIN_FETCH_TTL_ENV, _DEFAULT_FETCH_TTL_S),
            fetch_timeout_s=_env_float(PIN_FETCH_TIMEOUT_ENV, _DEFAULT_FETCH_TIMEOUT_S),
            archive_timeout_s=_env_float(
                PIN_ARCHIVE_TIMEOUT_ENV, _DEFAULT_ARCHIVE_TIMEOUT_S
            ),
        )

    def _lock_for(self, root: Path) -> threading.Lock:
        with self._locks_lock:
            return self._locks.setdefault(root, threading.Lock())

    # ── git helpers ────────────────────────────────────────────────

    def _git(self, root: Path, *args: str, timeout: float) -> tuple[bool, str]:
        """One bounded git call; (ok, stdout)."""
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
                    *args,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=_git_env(root, None),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("pin git %s failed for %s: %s", args[0], root, exc)
            return False, ""
        return proc.returncode == 0, proc.stdout.strip()

    # ── resolution ─────────────────────────────────────────────────

    def resolve(self, root: Path) -> PinDecision:
        root = root.resolve()
        with self._lock_for(root):
            return self._resolve(root)

    def _resolve(self, root: Path) -> PinDecision:
        if self._cache_dir is None:
            return PinDecision(
                root,
                None,
                "pin requested but the pin cache is disabled "
                f"({PIN_CACHE_ENV}=off); answered from the working tree",
                False,
            )
        ok, _ = self._git(root, "rev-parse", "--git-dir", timeout=2.0)
        if not ok:
            return PinDecision(
                root,
                None,
                "pin requested but the root is not a git checkout; "
                "answered from the working tree",
                False,
            )
        state = self._state.setdefault(root, _RootPinState())

        fetch_note: str | None = None
        if time.monotonic() - state.last_fetch_monotonic >= self._fetch_ttl_s:
            state.last_fetch_monotonic = time.monotonic()
            fetched, _ = self._git(
                root, "fetch", "origin", "main", timeout=self._fetch_timeout_s
            )
            if not fetched:
                fetch_note = "fetch failed or timed out"

        ok, sha = self._git(
            root, "rev-parse", "--verify", f"{PIN_REF}^{{commit}}", timeout=2.0
        )
        if not ok or not sha:
            return self._fallback(
                state, root, f"{PIN_REF} is not available in this clone"
            )

        if state.last_good is not None and self._complete(state.last_good[1], sha):
            return PinDecision(
                state.last_good[1], sha, _fetched_fragment(sha, fetch_note), True
            )

        reason = fetch_note or self._materialize(root, sha)
        if reason is None:
            tree = self._tree_dir(root, sha)
            state.last_good = (sha, tree)
            return PinDecision(tree, sha, _fetched_fragment(sha, fetch_note), True)
        return self._fallback(
            state, root, f"{reason}; could not index origin/main@{sha[:8]}"
        )

    @staticmethod
    def _complete(tree: Path, sha: str) -> bool:
        """The materialized tree exists and holds exactly this commit."""
        marker = tree / _MARKER
        if not tree.is_dir() or not marker.is_file():
            return False
        try:
            return marker.read_text(encoding="utf-8").strip() == sha
        except OSError:
            return False

    def _fallback(self, state: _RootPinState, root: Path, reason: str) -> PinDecision:
        """Degrade loudly: newest pinned tree on disk, else the working tree."""
        if state.last_good is not None and self._complete(
            state.last_good[1], state.last_good[0]
        ):
            sha, path = state.last_good
            return PinDecision(
                path,
                sha,
                f"pin refresh CUT SHORT: {reason}; pinned index is behind "
                f"origin/main (at {sha[:8]})",
                True,
            )
        return PinDecision(
            root,
            None,
            f"pin refresh CUT SHORT: {reason}; answered from the working tree",
            False,
        )

    # ── materialization ────────────────────────────────────────────

    def _tree_dir(self, root: Path, sha: str) -> Path:
        assert self._cache_dir is not None
        key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
        return self._cache_dir / f"{root.name}-{key}-{sha[:12]}"

    def _materialize(self, root: Path, sha: str) -> str | None:
        """Archive ``sha`` into the pin cache; None on success, else why not."""
        target = self._tree_dir(root, sha)
        if self._complete(target, sha):
            return None  # a previous process left a complete tree
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        reason = self._extract(root, sha, tmp)
        if reason is None:
            (tmp / _MARKER).write_text(sha + "\n", encoding="utf-8")
            try:
                tmp.replace(target)
            except OSError as exc:
                shutil.rmtree(tmp, ignore_errors=True)
                return f"install failed ({type(exc).__name__})"
            self._prune(root)
            return None
        shutil.rmtree(tmp, ignore_errors=True)
        return reason

    def _extract(self, root: Path, sha: str, dest: Path) -> str | None:
        """``git archive <sha>`` to a temp file, then extract; None on success.

        Extraction is :mod:`tarfile` with the ``data`` filter (no ``tar``
        subprocess — the launch-admission ratchet stays untouched), which
        also strips the path tricks a plain extract would honour.
        """
        archive = dest.with_name(f"{dest.name}.{os.getpid()}.archive")
        try:
            written, _ = self._git(
                root,
                "archive",
                "--output",
                str(archive),
                sha,
                timeout=self._archive_timeout_s,
            )
            if not written:
                return "git archive failed"
            try:
                with tarfile.open(archive) as tar:
                    tar.extractall(dest, filter="data")
            except (OSError, tarfile.TarError) as exc:
                return f"archive extract failed ({type(exc).__name__})"
            return None
        finally:
            archive.unlink(missing_ok=True)

    def _prune(self, root: Path) -> None:
        """Keep the newest ``keep`` complete trees of this root, best-effort."""
        assert self._cache_dir is not None
        key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
        prefix = f"{root.name}-{key}-"
        trees = [
            marker.parent
            for marker in self._cache_dir.glob(f"{prefix}*/{_MARKER}")
            if marker.parent.is_dir()
        ]
        try:
            trees.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        for tree in trees[self._keep :]:
            shutil.rmtree(tree, ignore_errors=True)
