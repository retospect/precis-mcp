"""Scanning every object of a bare repository for a string.

The refused-push scenario and the invariant checker ask whether a
credential-shaped string reached the hub or the scratch remote *anywhere*:
not only in a reachable commit but in a loose blob, an unreferenced commit
or the old tip of a replaced ref. The objects of the seed (the repository's
whole history) are skipped by name, so a scan reads only what the run
created.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def object_names(git_dir: Path) -> set[str]:
    """Every object name of a bare repository, reachable or not."""
    proc = subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=600,
    )
    return set(proc.stdout.split())


def parse_batch(data: bytes, wants: Sequence[bytes]) -> list[str]:
    """Names of the objects in a ``cat-file --batch`` stream whose content
    holds any of ``wants``."""
    hits: list[str] = []
    pos = 0
    while pos < len(data):
        eol = data.index(b"\n", pos)
        header = data[pos:eol].split()
        if len(header) < 3:
            pos = eol + 1
            continue
        size = int(header[2])
        body = data[eol + 1 : eol + 1 + size]
        if any(w in body for w in wants):
            hits.append(header[0].decode())
        pos = eol + 1 + size + 1
    return hits


def objects_containing(
    git_dir: Path,
    needles: Sequence[str],
    *,
    skip: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Names of the objects of ``git_dir`` not in ``skip`` whose content
    (inflated) holds any of ``needles``."""
    names = sorted(object_names(git_dir) - set(skip))
    if not names or not needles:
        return []
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), "cat-file", "--batch"],
        input=("\n".join(names) + "\n").encode(),
        capture_output=True,
        check=False,
        timeout=600,
    )
    return parse_batch(proc.stdout, [n.encode() for n in needles])
