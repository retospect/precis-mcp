"""Where mined artefacts go — **outside the repo tree, deliberately**.

Every stage writes here by default. It is a cache directory under
``$XDG_CACHE_HOME`` (or ``~/.cache``), not ``scripts/mine-sessions/out/``,
for one reason that is not negotiable:

``tests/test_deploy_tree_no_secrets.py`` scans the **working tree**, not
git's index — so a gitignored ``out/`` is still scanned. Mined transcripts
are the single most likely content in this project to contain a tailnet
address, a prod DSN or a pasted credential, because a session that debugged
the cluster quotes the cluster. Writing them inside the repo means running
the miner reddens the secret gate, which is both a broken workflow and
exactly the wrong lesson to teach (the answer to "the scanner found a
secret" must never become "add an exemption").

``redact.scrub`` still runs on every byte written. This is the second layer,
not a replacement: scrubbing is a filter over shapes we know, and the README
says so. Keeping the artefacts out of the public tree means a scrub miss is
a local-disk problem rather than a commit.

Override with ``MINE_OUT`` (``run.sh`` passes ``--out``), including back
into the tree if you really want it — ``scripts/mine-sessions/.gitignore``
still covers that case.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def default_out_dir() -> Path:
    """Cache dir for this worktree's mined artefacts.

    Keyed by worktree name so parallel sessions on the same machine do not
    overwrite each other's scoreboards — comparing pass N with pass N-1 is
    the point of keeping them.
    """
    env = os.environ.get("MINE_OUT")
    if env:
        return Path(env).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    worktree = Path(__file__).resolve().parents[2]
    slug = re.sub(r"[^\w.-]", "-", worktree.name) or "repo"
    return Path(cache) / "precis-mine-sessions" / slug


def out_path(*parts: str) -> Path:
    return default_out_dir().joinpath(*parts)
