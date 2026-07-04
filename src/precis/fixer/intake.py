"""Fixer intake: what's ready to build, and which one is next.

Two risky small bits the ADR flagged live here:

* **Proposal-ready convention.** A proposal is a transient ADR-shaped
  file under ``docs/proposals/*.md`` with a YAML-ish front-matter
  block; it is *pickable* only when ``status: ready`` (a human ran
  ``/ready`` in tandem, both keys turned). ``TEMPLATE.md`` and any
  ``status: draft`` file are ignored.
* **Idempotent pick.** The loop re-fires every interval, so it must
  skip an item it has already branched — otherwise it re-clones and
  re-builds the same thing forever. Skip is a ``branch_exists``
  predicate (local branch / worktree / remote head), injected so the
  pure pick logic stays unit-testable.

Gripe intake exists but is **off by default** at the MVP (ADR 0048:
gripes surface for human promotion until the ``ready``-on-gripes dial
is turned up); it is included here so the queue is one normalized
list once enabled.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

#: Front-matter fence: a leading ``---`` line, body, closing ``---``.
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

#: Filenames under docs/proposals/ that are never work items.
_NON_PROPOSAL_STEMS = frozenset({"template", "readme"})


@dataclass(frozen=True)
class WorkItem:
    """One pickable unit of repo-dev work.

    ``branch`` is the deterministic branch name the fixer uses; the
    idempotent-pick check keys on it. ``spec_text`` is the brief fed
    to the builder (the proposal body, or a gripe's timeline).
    """

    kind: str  # "proposal" | "gripe"
    slug: str  # proposal slug (file stem) or gripe id as str
    title: str
    branch: str
    spec_text: str
    source_path: Path | None = None


def parse_front_matter(text: str) -> dict[str, str]:
    """Parse a leading ``---`` front-matter block into a flat dict.

    Deliberately minimal — flat ``key: value`` lines only, values
    lower-cased-key'd but value-preserved, ``#`` comments and blank
    lines skipped. We only need ``status`` / ``title``; anything
    richer belongs in a real YAML load, which the pick path does not
    warrant. Returns ``{}`` when there is no front-matter block.
    """
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for raw in m.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key:
            out[key] = value.strip().strip("'\"")
    return out


def _slugify(stem: str) -> str:
    """Normalise a file stem into a branch-safe slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    return slug or "proposal"


def _title_from_body(text: str, fallback: str) -> str:
    """First ``# heading`` after the front-matter, else the fallback."""
    body = _FRONT_MATTER_RE.sub("", text, count=1)
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    return fallback


def ready_proposals(proposals_dir: Path) -> list[WorkItem]:
    """All ``status: ready`` proposals, sorted by filename (stable).

    A missing directory yields ``[]`` (the MVP may run before any
    proposal exists). ``TEMPLATE.md`` / ``README.md`` are skipped.
    """
    if not proposals_dir.is_dir():
        return []
    items: list[WorkItem] = []
    for path in sorted(proposals_dir.glob("*.md")):
        if path.stem.lower() in _NON_PROPOSAL_STEMS:
            continue
        text = path.read_text(encoding="utf-8")
        fm = parse_front_matter(text)
        if fm.get("status", "").lower() != "ready":
            continue
        slug = _slugify(path.stem)
        title = fm.get("title") or _title_from_body(text, slug)
        items.append(
            WorkItem(
                kind="proposal",
                slug=slug,
                title=title,
                branch=f"fix/{slug}",
                spec_text=text,
                source_path=path,
            )
        )
    return items


def pick_next(
    items: Iterable[WorkItem],
    branch_exists: Callable[[str], bool],
) -> WorkItem | None:
    """First item whose branch does not already exist (idempotent).

    The re-firing loop must not re-pick something it already branched;
    ``branch_exists`` encapsulates the git check (local/worktree/remote)
    so this stays pure and testable.
    """
    for item in items:
        if not branch_exists(item.branch):
            return item
    return None
