"""``precis.utils.devin_trust`` — add a path to the Devin CLI's trusted
workspaces, atomically.

The Devin CLI trusts subfolders of a listed ``trusted_paths`` entry, so
registering ``<repo>/.claude/worktrees`` once covers every tree under it:
``scripts/devin-tree`` does it for interactive trees, and ``run_slice``'s
devin lane (:func:`precis.workers.job_types.run_slice._devin_preflight`)
does it for the slice checkout it is about to launch under — a headless
session cannot answer the trust prompt (gr570688).

UNDER must be a ``<repo>/.claude/worktrees`` directory that exists and is
not a symlink (nor is its ``.claude`` component, the only parts below the
repo root), inside a git repo root (``.git`` present). PATH must equal
UNDER or sit inside it; both are canonicalised first. Anything else is
refused.

The read-modify-write holds an ``flock`` on ``<file>.lock``; the write
itself is a temp file + ``os.replace`` in the target directory. The file
is created when missing, every existing key and list entry is preserved,
an already-listed path changes nothing, and malformed JSON is refused
without overwriting. The CLI (:func:`main`, reached through
``scripts/lib/devin_trust.py``) exits 2 on refusal; the library side
raises :class:`TrustError`.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import time

DEFAULT_FILE = "~/.local/share/devin/cli/trusted_workspaces.json"

#: How long ``add`` polls a held ``<file>.lock`` before refusing — a
#: blocking ``LOCK_EX`` would hang a worker pass forever (outbound
#: wait-bound invariant; review of gr570688). Tests monkeypatch it down.
LOCK_TIMEOUT_S = 10.0


class TrustError(Exception):
    """A refused trust write; the message names the failed check."""


def resolve_file(cli: str | None) -> str:
    """``--file``, else ``$DEVIN_TRUSTED_WORKSPACES``, else
    :data:`DEFAULT_FILE`, expanded."""
    return os.path.expanduser(
        cli or os.environ.get("DEVIN_TRUSTED_WORKSPACES") or DEFAULT_FILE
    )


def valid_under(arg: str) -> str:
    """Canonical UNDER — a real, non-symlink ``<repo>/.claude/worktrees``
    inside a git repo root. Raises :class:`TrustError` naming the failed
    check."""
    lex = os.path.normpath(os.path.abspath(arg))
    if (
        os.path.basename(lex) != "worktrees"
        or os.path.basename(os.path.dirname(lex)) != ".claude"
    ):
        raise TrustError(f"{arg} is not a <repo>/.claude/worktrees path")
    repo = os.path.dirname(os.path.dirname(lex))
    if not os.path.exists(os.path.join(repo, ".git")):
        raise TrustError(f"{repo} is not a git repo root (no .git)")
    if not os.path.isdir(lex):
        raise TrustError(f"{arg} is not a directory")
    for comp in (lex, os.path.dirname(lex)):
        if os.path.islink(comp):
            raise TrustError(f"{comp} is a symlink")
    return os.path.realpath(lex)


def load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("root is not an object")
    if "trusted_paths" in data and not isinstance(data["trusted_paths"], list):
        raise ValueError("trusted_paths is not a list")
    return data


def atomic_dump(path: str, data: dict) -> None:
    """Temp-file + ``os.replace`` in ``path``'s directory, preserving the
    existing file's mode."""
    parent = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".trusted_workspaces.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(path):
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def add(file: str, target: str) -> bool:
    """Append ``target`` to ``file``'s ``trusted_paths`` under the
    ``<file>.lock`` flock, written atomically. ``False`` when an entry
    already resolves to it. The lock is polled with ``LOCK_NB`` for at
    most :data:`LOCK_TIMEOUT_S`; a still-held lock raises
    :class:`TrustError` instead of hanging the caller."""
    import fcntl

    parent = os.path.dirname(file) or "."
    os.makedirs(parent, exist_ok=True)
    with open(file + ".lock", "a", encoding="utf-8") as lock:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TrustError(
                        "trust config locked by another process; next: "
                        "retry, or check for a stuck scripts/devin-tree"
                    ) from None
                time.sleep(0.1)
        data = load(file)
        paths = data.setdefault("trusted_paths", [])
        if any(isinstance(e, str) and os.path.realpath(e) == target for e in paths):
            return False
        paths.append(target)
        atomic_dump(file, data)
    return True


def trust(
    under: str, path: str | None = None, file: str | None = None
) -> tuple[str, bool]:
    """Validate ``under``/``path`` and record the trust; ``(target,
    added)``. Every refusal — a bad UNDER, a PATH outside UNDER, a file
    that cannot be read or written — raises :class:`TrustError`."""
    real_under = valid_under(under)
    target = os.path.realpath(path) if path else real_under
    if target != real_under and os.path.commonpath([real_under, target]) != real_under:
        raise TrustError(f"{target} is outside {real_under}")
    resolved = resolve_file(file)
    try:
        added = add(resolved, target)
    except (OSError, ValueError) as e:
        raise TrustError(f"not updating {resolved}: {e}") from None
    return target, added


def _refuse(msg: str) -> int:
    print(f"devin_trust: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    """``devin_trust.py [--file FILE] UNDER [PATH]`` — 0 on success, 2 on
    refusal."""
    p = argparse.ArgumentParser(prog="devin_trust.py")
    p.add_argument("--file", default=None, help="trusted_workspaces.json path")
    p.add_argument("under", help="the repo's .claude/worktrees directory")
    p.add_argument(
        "path", nargs="?", default=None, help="path to trust (default: UNDER)"
    )
    a = p.parse_args(argv)
    try:
        target, added = trust(a.under, a.path, a.file)
    except TrustError as e:
        return _refuse(str(e))
    if not added:
        print(f"devin_trust: already trusted: {target}")
    else:
        print(f"devin_trust: trusted {target} in {resolve_file(a.file)}")
    return 0
