"""Shared ``alias:/abs/path,...`` env-var parser for DB-free, alias-rooted kinds.

Extracted from ``precis.handlers.python``'s original
``parse_python_roots`` so the ``python`` and ``md`` kinds — and any
future in-memory kind gated behind a dark switch — share one parser instance
rather than drifting copies. See the :mod:`precis.md_index` package
docstring for the ``md`` kind's design.

``precis.handlers.python.parse_python_roots`` stays importable (a
thin wrapper around :func:`parse_alias_roots`) so existing call sites
and tests are unaffected.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


def parse_alias_roots(raw: str | None, *, env_var: str) -> dict[str, Path]:
    """Parse an ``alias1:/abs/path1,alias2:/abs/path2`` env value.

    ``env_var`` names the source env var — purely for log messages —
    so one parser serves multiple env vars (``PRECIS_PYTHON_ROOTS``,
    ``PRECIS_MD_ROOTS``, ...) without losing which one a warning is
    about. Whitespace around each component is stripped. Entries with
    the following problems are skipped with a warning, and the rest
    of the entries are kept:

    - missing ``:`` separator
    - empty alias or empty path
    - non-existent or non-directory path
    - duplicate alias (first wins)

    A ``None`` or empty string yields ``{}``. The returned paths are
    resolved absolute paths (``~`` expanded). Callers should validate
    these again at handler-construction time, so a transient race
    between parse and construct still produces a clean error.
    """
    if not raw:
        return {}

    out: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            log.warning("%s: skipping %r - missing ':' separator", env_var, entry)
            continue
        alias, _, path_str = entry.partition(":")
        alias = alias.strip()
        path_str = path_str.strip()
        if not alias or not path_str:
            log.warning("%s: skipping %r - empty alias or path", env_var, entry)
            continue
        if alias in out:
            log.warning("%s: duplicate alias %r - first wins", env_var, alias)
            continue
        path = Path(path_str).expanduser().resolve()
        if not path.is_dir():
            log.warning(
                "%s: skipping %r - not a directory: %s",
                env_var,
                alias,
                path,
            )
            continue
        out[alias] = path

    return out


@dataclass(frozen=True, slots=True)
class MirrorRoot:
    """One ``PRECIS_ROOTS`` entry: a directory the file kinds mirror.

    ``ro`` roots refuse every file write (put/edit/delete); reads, tags
    and links still work because those live in the DB, not the file.
    """

    path: Path
    ro: bool = False


_ALIAS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")


def parse_mirror_roots(
    raw: str | None, *, env_var: str = "PRECIS_ROOTS"
) -> dict[str, MirrorRoot]:
    """Parse ``alias:/abs/path[:ro],...`` into :class:`MirrorRoot` entries.

    Same grammar and skip rules as :func:`parse_alias_roots`, plus an
    optional trailing ``:ro`` flag. The alias becomes the slug prefix
    (``skills/precis-relations``), so it must itself be a slug segment
    (lowercase a-z 0-9 ``_`` ``-``); other aliases are skipped.
    """
    if not raw:
        return {}
    out: dict[str, MirrorRoot] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        ro = False
        head, sep, flag = entry.rpartition(":")
        if sep and flag.strip().lower() == "ro" and ":" in head:
            entry, ro = head, True
        alias = entry.partition(":")[0].strip()
        if not _ALIAS_RE.match(alias):
            log.warning("%s: skipping %r - alias is not a slug segment", env_var, alias)
            continue
        if alias in out:
            log.warning("%s: duplicate alias %r - first wins", env_var, alias)
            continue
        # One entry at a time, so the ro flag stays with the path it was
        # written next to even when an earlier same-alias entry is dropped.
        for path in parse_alias_roots(entry, env_var=env_var).values():
            out[alias] = MirrorRoot(path=path, ro=ro)
    return out
