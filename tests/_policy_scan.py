"""Tree walking for the policy gates: an aborted scan is never a verdict.

The policy tests (secret scanner, env-var policy, ``type: ignore`` ratchet)
walk the tree inside the assertion. On 2026-09-29 the secret scanner died
mid-walk with ``OSError: [Errno 23] Too many open files in system`` — ENFILE,
the *system-wide* file-table limit, caused by whatever else shares the Docker
VM — and the red rendered as ``FAILED test_repo_carries_no_cluster_addresses``,
the same line a real tailnet address produces. A session read it as a leak and
told a peer so. Worse, a walker that swallows ``OSError`` and ``continue``\\s
reaches ``assert not hits`` with a partial list and passes.

Every tree-walking gate therefore goes through these two helpers. An
``OSError`` anywhere in the walk or a read becomes :class:`ScanIncomplete`,
whose message names the errno and says it is not a finding about the diff.
It deliberately does not subclass ``OSError`` or ``AssertionError``: a stray
``except OSError`` must not swallow it, and the summary line must not read
as a violation.
"""

from __future__ import annotations

import errno
from collections.abc import Collection
from pathlib import Path

NOT_A_VERDICT = (
    "the scan did not complete, so this is NOT a verdict about your diff "
    "(nothing was found and nothing was ruled out) — re-run in isolation"
)


class ScanIncomplete(Exception):
    """A policy scan aborted on an ``OSError`` before reaching a verdict."""

    def __init__(self, exc: OSError, doing: str) -> None:
        code = exc.errno
        name = errno.errorcode.get(code, "?") if code is not None else "?"
        super().__init__(
            f"policy scan ABORTED while {doing}: {name} (errno {code}) "
            f"{exc.strerror or exc}; {NOT_A_VERDICT}"
        )


def walk_files(
    root: Path, pattern: str = "*", skip_dirs: Collection[str] = ()
) -> list[Path]:
    """Every regular file under ``root`` matching ``pattern``, minus any whose
    path (relative to ``root``) has a component in ``skip_dirs``. Sorted.

    A missing ``root`` is an empty list (the caller decides whether that is
    vacuous); an ``OSError`` mid-walk is :class:`ScanIncomplete`.
    """
    try:
        if not root.is_dir():
            return []
        return sorted(
            p
            for p in root.rglob(pattern)
            if p.is_file()
            and not any(part in skip_dirs for part in p.relative_to(root).parts)
        )
    except OSError as exc:
        raise ScanIncomplete(exc, f"walking {root}") from exc


def read_text(path: Path, *, errors: str = "strict") -> str:
    """``path.read_text(encoding="utf-8")`` with an ``OSError`` turned into
    :class:`ScanIncomplete`. ``UnicodeDecodeError`` propagates: it says the
    file is not text, which the caller may legitimately skip."""
    try:
        return path.read_text(encoding="utf-8", errors=errors)
    except OSError as exc:
        raise ScanIncomplete(exc, f"reading {path}") from exc
