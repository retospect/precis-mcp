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
import re
from collections.abc import Callable, Collection
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
    vacuous); an ``OSError`` mid-walk is :class:`ScanIncomplete`. One
    exception: a ``FileNotFoundError`` mid-walk means a sibling test's
    scratch dir (``test_fleet_codex`` scratches under ``.claude/``) was
    deleted while ``rglob`` was inside it — not evidence, so the walk
    retries once before reporting the abort.
    """
    for attempt in (1, 2):
        try:
            if not root.is_dir():
                return []
            return sorted(
                p
                for p in root.rglob(pattern)
                if p.is_file()
                and not any(part in skip_dirs for part in p.relative_to(root).parts)
            )
        except FileNotFoundError as exc:
            if attempt == 2:
                raise ScanIncomplete(exc, f"walking {root}") from exc
        except OSError as exc:
            raise ScanIncomplete(exc, f"walking {root}") from exc
    raise AssertionError("unreachable")


def read_text(path: Path, *, errors: str = "strict") -> str:
    """``path.read_text(encoding="utf-8")`` with an ``OSError`` turned into
    :class:`ScanIncomplete`. ``UnicodeDecodeError`` propagates: it says the
    file is not text, which the caller may legitimately skip. A walked file
    that vanished before its read holds no lines to scan — reads as empty."""
    try:
        return path.read_text(encoding="utf-8", errors=errors)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ScanIncomplete(exc, f"reading {path}") from exc


def _ignore_glob(pat: str) -> str:
    """gitignore glob → regex body: ``*``/``?``/``[...]`` never cross ``/``;
    ``**/`` matches any run of directories (including none) and a trailing
    ``**`` everything below."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**/", i):
                out.append("(?:[^/]+/)*")
                i += 3
            elif pat.startswith("**", i) and i + 2 == n:
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] == "!":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j < n:
                inner = pat[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
            else:
                out.append(re.escape(c))
                i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def gitignore_matcher(anchor: Path) -> Callable[[str], bool]:
    """Predicate ``ignored(relpath)`` parsed from ``anchor/.gitignore`` —
    always ``False`` when the file is absent.

    The fallback filesystem walks (no ``.git``) use this to drop paths a
    checkout would never track: a ``git archive`` tree carries tracked
    files plus whatever the run then generated — ``.test-hang-stacks/``
    faulthandler dumps holding the test DSN tripped the credential gate
    as ``url with inline password`` (gr523994).

    Grammar covered: comments, ``!`` negations (last match wins), trailing
    ``/`` dir rules, ``*``/``?``/``[...]``/``**`` globs, and ``/``-anchored
    vs any-level patterns (a non-dir pattern matching an ancestor dir
    excludes everything under it, as git does). Two deliberate
    simplifications vs git, both erring toward scanning MORE: nested
    ``.gitignore`` files are not read, and a negation may re-include a
    path under an ignored dir (git refuses that).

    Scope: the matcher applies only on the no-``.git`` fallback walk. Git
    mode scans tracked files, so a ``.gitignore`` entry cannot hide a
    committed secret there. In the fallback (a ``git archive`` tree) a
    force-added tracked file matching a ``.gitignore`` pattern is skipped —
    a known narrow gap.
    """
    path = anchor / ".gitignore"
    if not path.is_file():
        return lambda rel: False
    rules: list[tuple[bool, bool, bool, re.Pattern[str]]] = []
    for raw in read_text(path, errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        rules.append((neg, dir_only, anchored, re.compile(_ignore_glob(line))))

    def ignored(rel: str) -> bool:
        parts = rel.split("/")
        hit = False
        for neg, dir_only, anchored, rx in rules:
            if anchored:
                # match the full relpath or an ancestor dir prefix
                for i in range(1, len(parts) + (0 if dir_only else 1)):
                    if rx.fullmatch("/".join(parts[:i])):
                        hit = not neg
            else:
                # match any component; dir rules only ancestor components
                stop = len(parts) - 1 if dir_only else len(parts)
                if any(rx.fullmatch(part) for part in parts[:stop]):
                    hit = not neg
        return hit

    return ignored
