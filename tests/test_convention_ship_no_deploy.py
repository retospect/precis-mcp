"""Enforcer for container-ops/deploy-is-opt-in (docs/conventions/container-ops.md).

Deploy bounces every daemon fleet-wide; ``scripts/ship`` only *surfaces*
shipped-but-undeployed lag. This pins that ``scripts/ship`` never executes
``scripts/deploy`` / ``scripts/release``: a mention is fine inside a comment
or a message (printf/echo/say/warn/die), an invocation is not.
"""

from __future__ import annotations

import re
from pathlib import Path

_SHIP = Path(__file__).resolve().parent.parent / "scripts" / "ship"

_EMITTERS = ("printf", "echo", "say", "warn", "die", "log", "info", "err", "fail")


def _segments(line: str) -> list[str]:
    """Split on ``;`` ``&&`` ``||`` ``|`` outside single/double quotes."""
    segs, cur, quote, i = [], "", "", 0
    while i < len(line):
        c = line[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in ";|" or line.startswith("&&", i):
            i += 2 if line.startswith(("&&", "||"), i) else 1
            segs.append(cur)
            cur = ""
            continue
        cur += c
        i += 1
    return [*segs, cur]


def deploy_invocations(script: str) -> list[int]:
    """Line numbers that run ``.../scripts/deploy|release`` as a command."""
    hits: list[int] = []
    for n, raw in enumerate(script.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for seg in _segments(line):
            words = seg.split(None, 1)
            if not words or words[0] in _EMITTERS:
                continue
            if re.search(r"scripts/(deploy|release)\b(?![-._])", seg):
                hits.append(n)
                break
    return hits


def test_checker_flags_synthetic_invocation() -> None:
    assert deploy_invocations(
        'echo hi\n"$WORKTREE/scripts/deploy" "$sha" --pinned\n'
    ) == [2]
    assert deploy_invocations("bash scripts/release --cut\n") == [1]
    assert deploy_invocations("say x; scripts/deploy\n") == [1]
    assert deploy_invocations("echo 'a; scripts/deploy'\n") == []
    assert deploy_invocations("true && echo y | scripts/release\n") == [1]
    assert (
        deploy_invocations('# scripts/deploy later\nsay "run scripts/deploy"\n') == []
    )


def test_ship_never_invokes_deploy() -> None:
    hits = deploy_invocations(_SHIP.read_text(encoding="utf-8"))
    assert not hits, (
        f"scripts/ship invokes deploy/release at lines {hits}: deploy is an "
        "explicit step (scripts/deploy or /go), never a side effect of ship"
    )
