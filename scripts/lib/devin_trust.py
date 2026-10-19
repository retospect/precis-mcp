#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""devin_trust.py — add a path to Devin CLI's trusted workspaces, atomically.

    devin_trust.py [--file FILE] UNDER [PATH]

Ensures PATH (default: UNDER) is listed in ``trusted_paths`` of Devin's
trusted workspaces file: ``--file``, else ``$DEVIN_TRUSTED_WORKSPACES``,
else ``~/.local/share/devin/cli/trusted_workspaces.json``. The Devin CLI
trusts subfolders of a listed parent, so scripts/devin-tree registers the
repo's ``.claude/worktrees`` directory once and every tree is covered.

UNDER must be a ``<repo>/.claude/worktrees`` directory that exists and is
not a symlink (nor is its ``.claude`` component, the only parts below the
repo root), inside a git repo root (``.git`` present). PATH must equal
UNDER or sit inside it; both are canonicalised first. Anything else is
refused.

The read-modify-write holds an ``flock`` on ``<file>.lock``; the write
itself is a temp file + os.replace in the target directory. The file is
created when missing, every existing key and list entry is preserved, an
already-listed path changes nothing, and malformed JSON is refused
without overwriting. Exits 2 on refusal.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile

DEFAULT_FILE = "~/.local/share/devin/cli/trusted_workspaces.json"


def refuse(msg: str) -> int:
    print(f"devin_trust: {msg}", file=sys.stderr)
    return 2


def resolve_file(cli: str | None) -> str:
    return os.path.expanduser(
        cli or os.environ.get("DEVIN_TRUSTED_WORKSPACES") or DEFAULT_FILE
    )


def valid_under(arg: str) -> tuple[str | None, str]:
    """Canonical UNDER, or an error string naming the failed check."""
    lex = os.path.normpath(os.path.abspath(arg))
    if (
        os.path.basename(lex) != "worktrees"
        or os.path.basename(os.path.dirname(lex)) != ".claude"
    ):
        return None, f"{arg} is not a <repo>/.claude/worktrees path"
    repo = os.path.dirname(os.path.dirname(lex))
    if not os.path.exists(os.path.join(repo, ".git")):
        return None, f"{repo} is not a git repo root (no .git)"
    if not os.path.isdir(lex):
        return None, f"{arg} is not a directory"
    for comp in (lex, os.path.dirname(lex)):
        if os.path.islink(comp):
            return None, f"{comp} is a symlink"
    return os.path.realpath(lex), ""


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


def add(file: str, target: str) -> int:
    parent = os.path.dirname(file) or "."
    os.makedirs(parent, exist_ok=True)
    with open(file + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(file)
        paths = data.setdefault("trusted_paths", [])
        if any(isinstance(e, str) and os.path.realpath(e) == target for e in paths):
            print(f"devin_trust: already trusted: {target}")
            return 0
        paths.append(target)
        atomic_dump(file, data)
    print(f"devin_trust: trusted {target} in {file}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="devin_trust.py")
    p.add_argument("--file", default=None, help="trusted_workspaces.json path")
    p.add_argument("under", help="the repo's .claude/worktrees directory")
    p.add_argument(
        "path", nargs="?", default=None, help="path to trust (default: UNDER)"
    )
    a = p.parse_args(argv)

    under, err = valid_under(a.under)
    if under is None:
        return refuse(err)
    target = os.path.realpath(a.path) if a.path else under
    if target != under and os.path.commonpath([under, target]) != under:
        return refuse(f"{target} is outside {under}")

    file = resolve_file(a.file)
    try:
        return add(file, target)
    except (OSError, ValueError) as e:
        return refuse(f"not updating {file}: {e}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
