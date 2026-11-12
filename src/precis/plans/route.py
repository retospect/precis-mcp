"""Mint-time coordinator routing for the dark factory (gr645163).

Ruling (Reto 2026-11-12, "escalate to next model"): work no builder lane
may write — a brief naming a deny path, or a fix that is prod-graph only
— skips the builders entirely and becomes one coordinator ask with the
brief attached, instead of burning a build that can't land and stalling
until the coordinator lands it by hand (td643078/gr626879, td638980).
This module is the shared classifier: ``gripe_dispatch`` asks it of each
candidate gripe's report before minting a fix chain, and
``plan_dispatch._step_dispatch`` asks it of each run_slice-bound todo's
body and newest brief/handoff before minting a job. A builder's ordinary
can't-finish (a ``no_commit`` run with no verdict) is NOT here — that
climbs the model ladder in ``_step_dispatch`` and only the top rung's
failure files the coordinator ask.

Two signals:

- ``deny-path named`` — a path-shaped token in the text matches the
  resolved deny set (:func:`precis.plans.lanes.deny_paths_for`,
  ``STRICT_DENY`` by default). Any path-shaped token counts, backticks
  and code spans included: a plain-prose mention of ``scripts/ship`` is
  the td643078 shape. Case folds like the lane's own write check
  (``human_only_hits``: the slice hosts' filesystems fold case).
- ``graph-only`` — every non-empty token is a well-formed graph handle
  (``td<id>``/``gr<id>``/``me<id>``/…) or bare punctuation, and at least
  one handle appears. A report reduced to handles alone names no repo
  target: the fix lives in the prod graph, which a builder's dev
  worktree cannot reach (gr630346/gr630611's class of no-commit runs).

The instruction boilerplate must not classify: the ``gripe-fix-direct``
head names ``scripts/ship --quick`` as the land's own mechanism, and a
brief pasted whole into a body carries the "Paths you may not write" and
"When done" sections quoting the same harness paths. Both are stripped
before the text is judged.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable

from precis.plans import templates as _templates
from precis.utils import handle_registry

#: A token that could name a repo path: carries a ``/`` (``scripts/ship``,
#: ``src/precis/plans/lanes.py``), a bare dotted name (``CLAUDE.md``,
#: ``ansible.cfg``), or a dotfile (``.mcp.json``). Glob metachars are kept
#: so a brief quoting the glob itself (``deploy/inventory*/**``) still hits.
_PATH_TOKEN = re.compile(
    r"[A-Za-z0-9_.$~*+-]*(?:/[A-Za-z0-9_.$~*+-]+)+"
    r"|[A-Za-z0-9_-]+\.[A-Za-z]{1,5}"
    r"|\.[A-Za-z0-9][A-Za-z0-9_.-]*"
)

#: Markdown H2 sections of the slice brief whose boilerplate names the
#: harness (the deny-path list itself, the land-lint command block) — a
#: body quoting the rendered brief would classify on its own instructions.
_BOILER_SECTION = re.compile(r"^##\s+(Paths you may not write|When done)\b")

#: Factory-minted bodies name deny paths only as the land's own
#: mechanism — the gripe chain's ``the land (land_slice, `scripts/ship
#: --quick`)``, the release-fixer's ``land with scripts/ship --quick``
#: and the revert task's ``land it as a normal slice (scripts/ship)``.
#: Stripping the whole stock phrase keeps the land mechanics from
#: reading as the work's target (a genuine "fix scripts/ship" report
#: never matches these).
_LAND_MECHANISM = (
    re.compile(r"\bthe land \([^)]*\)", re.IGNORECASE),
    re.compile(r"\bwith `?scripts/ship`?\s+--\w+", re.IGNORECASE),
    re.compile(r"\bas a normal slice \([^)]*\)", re.IGNORECASE),
)

#: Characters a handle may be wrapped in inside prose.
_HANDLE_WRAP = ".,;:()[]{}`'\"*_#<>|"

_LETTER = re.compile(r"[a-zA-Z]")


def strip_direct_head(text: str) -> str:
    """The text minus the ``gripe-fix-direct`` instruction head (everything
    through :data:`templates.DIRECT_HEAD_END`), minus the slice brief's
    deny-path / land-lint boilerplate sections, and minus the factory's
    stock land-mechanism phrases — only the report and any real
    instructions remain for classification."""
    idx = text.find(_templates.DIRECT_HEAD_END)
    if idx >= 0:
        text = text[idx + len(_templates.DIRECT_HEAD_END) :]
    out: list[str] = []
    skip = False
    for line in text.split("\n"):
        if line.startswith("## "):
            skip = _BOILER_SECTION.match(line) is not None
        if not skip:
            out.append(line)
    stripped = "\n".join(out)
    for pattern in _LAND_MECHANISM:
        stripped = pattern.sub(" ", stripped)
    return stripped


def deny_path_hit(text: str, deny_paths: Iterable[str]) -> tuple[str, str] | None:
    """``(token, glob)`` of the first path-shaped token matching a deny
    glob, ``None`` when none does — case-folded like the lane's write
    check. Tokens are judged as whole paths, so ``gr645163`` or
    ``plan_dispatch`` prose never reads as a ``scripts/*`` hit."""
    globs = tuple(dict.fromkeys(g.lower() for g in deny_paths))
    for m in _PATH_TOKEN.finditer(text):
        token = m.group(0).lower()
        if token.startswith("./"):
            token = token[2:]
        hit = next(
            (
                g
                for g in globs
                if fnmatch.fnmatchcase(token, g)
                or ("/" in token and fnmatch.fnmatchcase(token, "*/" + g))
            ),
            None,
        )
        if hit is not None:
            return m.group(0), hit
    return None


def graph_only(text: str) -> bool:
    """True when every non-empty token is a well-formed graph handle or
    carries no letter, and at least one handle appears — the report names
    graph rows, never a repo path (gr630346's shape)."""
    real = [t.strip(_HANDLE_WRAP) for line in text.splitlines() for t in line.split()]
    real = [t for t in real if t]
    if not real:
        return False
    handles = [t for t in real if handle_registry.is_well_formed(t)]
    return bool(handles) and all(
        handle_registry.is_well_formed(t) or not _LETTER.search(t) for t in real
    )


def coordinator_route(text: str | None, deny_paths: Iterable[str]) -> str | None:
    """The short reason the work routes straight to the coordinator, or
    ``None`` when a builder lane may take it. The reason is written into
    the ask body and context, so it names the signal, not advice."""
    if not text or not text.strip():
        return None
    body = strip_direct_head(text)
    hit = deny_path_hit(body, deny_paths)
    if hit is not None:
        token, glob = hit
        return f"names deny path `{token}` (matched `{glob}`)"
    if graph_only(body):
        return "prod-graph only — no repo diff exists for a builder to write"
    return None
