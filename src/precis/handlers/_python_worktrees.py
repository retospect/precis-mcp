"""Git-worktree discovery for the python kind (``PRECIS_PYTHON_WORKTREES``).

Source of truth is the main checkout's worktree registry:
``<main>/.git/worktrees/<id>/gitdir`` holds ``<host worktree path>/.git``
(the data behind ``git worktree list --porcelain``; read directly, no git
binary). ``PRECIS_PYTHON_GITDIR_MAP=<hostprefix>:<containerprefix>``
translates host paths to container paths; a worktree is a root only when its
translated path exists here. Aliases are ``<prefix>-<basename>``.

The listing is re-read at most every ``ttl`` seconds. Worktree indexes build
lazily on first query; at most ``max_indexes`` stay cached (LRU), the rest are
forgotten through ``RepoCache.drop``. Worktree roots are never part of the
default search fan-out and never writable.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


@dataclass(frozen=True, slots=True)
class Worktree:
    path: Path  # container path of the checkout
    git_dir: Path  # container path of <main>/.git/worktrees/<id>


def parse_gitdir_map(raw: str | None) -> tuple[str, str] | None:
    """``hostprefix:containerprefix`` -> pair, or None when unset/malformed."""
    if not raw or ":" not in raw:
        return None
    host, _, ctr = raw.strip().partition(":")
    host, ctr = host.rstrip("/"), ctr.rstrip("/")
    if not host or not ctr:
        return None
    return host, ctr


def parse_worktrees_spec(raw: str | None) -> tuple[str, Path] | None:
    """``prefix:/container/main`` -> (prefix, main), first valid entry only."""
    if not raw:
        return None
    for entry in raw.split(","):
        prefix, _, path = entry.strip().partition(":")
        prefix, path = prefix.strip(), path.strip()
        if prefix and path and (Path(path) / ".git").is_dir():
            return prefix, Path(path).resolve()
        log.warning("PRECIS_PYTHON_WORKTREES: skipping %r", entry)
    return None


class WorktreeRegistry:
    def __init__(
        self,
        prefix: str,
        main: Path,
        *,
        gitdir_map: tuple[str, str] | None = None,
        max_indexes: int = 4,
        ttl: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.prefix = prefix
        self.main = Path(main).resolve()
        self.gitdir_map = gitdir_map
        self.max_indexes = max(1, max_indexes)
        self.ttl = ttl
        self._clock = clock
        self._listing: dict[str, Worktree] = {}
        self._listed_at: float | None = None
        self._lru: OrderedDict[str, Path] = OrderedDict()

    def owns(self, alias: str) -> bool:
        return alias.startswith(self.prefix + "-")

    def _translate(self, host: str) -> Path:
        if self.gitdir_map:
            h, c = self.gitdir_map
            if host == h or host.startswith(h + "/"):
                return Path(c + host[len(h) :])
        return Path(host)

    def _scan(self) -> dict[str, Worktree]:
        out: dict[str, Worktree] = {}
        reg = self.main / ".git" / "worktrees"
        try:
            entries = sorted(reg.iterdir())
        except OSError:
            return out
        for ent in entries:
            try:
                target = (ent / "gitdir").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not target.endswith("/.git"):
                continue
            path = self._translate(target[: -len("/.git")])
            try:
                if not (path / ".git").exists() or not path.is_dir():
                    continue
                path = path.resolve()
            except OSError:
                continue
            if path == self.main or not _NAME_RE.match(path.name):
                continue
            out.setdefault(f"{self.prefix}-{path.name}", Worktree(path, ent))
        return out

    def listing(self) -> tuple[dict[str, Worktree], list[Path]]:
        """Current ``{alias: Worktree}`` plus paths of worktrees that vanished."""
        now = self._clock()
        if self._listed_at is not None and now - self._listed_at < self.ttl:
            return self._listing, []
        self._listing = self._scan()
        self._listed_at = now
        gone_aliases = [a for a in self._lru if a not in self._listing]
        gone = [self._lru.pop(a) for a in gone_aliases]
        return self._listing, gone

    def touch(self, alias: str, path: Path) -> list[Path]:
        """Mark ``alias`` used; return paths evicted to stay within the bound."""
        self._lru[alias] = path
        self._lru.move_to_end(alias)
        evicted: list[Path] = []
        while len(self._lru) > self.max_indexes:
            _, p = self._lru.popitem(last=False)
            evicted.append(p)
        return evicted

    def git_dir_for(self, root: Path) -> Path | None:
        for wt in self._listing.values():
            if wt.path == root:
                return wt.git_dir
        return None
