"""Resolve ``PRECIS_STARTUP_SKILLS`` into a pinned-skill verdict.

A pure-functional shim over the skill loader: parse the env var,
look up each slug, drop unknowns (with a notice), and produce a
:class:`Resolution` the server renders in the cold-start banner.

Pinning is **not** limited by skill body size (gr681315): the banner
prints names only — bodies are fetched on demand via ``get`` or
``prompts/get`` — so a byte cap would limit a cost nobody pays and
silently drop operator-stated pins (the six-skill asa default exceeds
50 KB before the third slug). If a future path inlines pinned bodies
into the banner, the cap belongs there, at the inlining site.

No I/O beyond the skill loader; safe to call from
:func:`precis.server._build_instructions` on every banner refresh
(cheap in practice — skill bodies are already read elsewhere by the
skill index machinery and the underlying ``importlib.resources``
calls are O(small)).

See ``docs/backlog/mcp-cold-start-token-budget.md`` Phase 3 for the
broader design context and the skill ``precis-startup-skills-help``
for the agent-facing documentation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from precis.handlers.skill import _list_skills, _load_skill

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resolution:
    """Verdict for a parsed ``PRECIS_STARTUP_SKILLS`` value.

    Fields are tuples (not lists) so the dataclass stays hashable
    and can flow through frozen contexts (config caches, test
    parametrisations) without surprises.
    """

    #: Known slugs that pass kind gating, in the configured order.
    pinned: tuple[str, ...] = ()
    #: Slugs that didn't resolve to a known skill (typo, removed
    #: skill, third-party skill from a sibling deployment).
    unknown: tuple[str, ...] = ()
    #: Slugs whose subject kind isn't loaded on this server
    #: (prohibited via ``PRECIS_KINDS_DISABLED``, missing resources,
    #: or otherwise gated out by :mod:`precis.kind_gate`). The skill
    #: body would still load but its recipes would fail when the
    #: agent tries to call the kind, so we surface a warning notice
    #: rather than silently teaching unusable knowledge.
    kind_unavailable: tuple[str, ...] = ()


def parse(value: str | None) -> list[str]:
    """Parse a comma-separated env value into a deduped ordered list.

    Whitespace around commas is tolerated. Empty entries (``a,,b``)
    are dropped. Each slug keeps its first position in the configured order.
    """
    if not value:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in value.split(","):
        slug = raw.strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(slug)
    return out


def resolve(
    slugs: list[str],
    *,
    unavailable_kinds: frozenset[str] = frozenset(),
    loader: Callable[[str], str | None] = _load_skill,
    known: Callable[[], list[str]] = _list_skills,
) -> Resolution:
    """Look up each slug and bucket unknowns / kind-gated skills.

    ``loader`` / ``known`` are injectable so tests can stand a fake
    catalogue without touching the package data. No size accounting:
    see the module docstring for why body bytes are irrelevant here.

    ``unavailable_kinds`` is the set of kinds gated out by
    :mod:`precis.kind_gate` (typically derived from
    ``hub.loadabilities``). A pinned slug whose front-matter
    ``applies-to:`` (or ``precis-<kind>-help`` slug pattern) names
    *any* unavailable kind is moved from :attr:`Resolution.pinned`
    to :attr:`Resolution.kind_unavailable` and surfaced via a
    warning notice — the body would load fine, but its recipes
    would fail when the agent calls the kind, so we'd rather not
    teach unusable knowledge.
    """
    from precis.handlers.skill import _kinds_referenced_by_skill
    from precis.utils.skill_common import parse_frontmatter

    known_set = set(known())
    pinned: list[str] = []
    unknown: list[str] = []
    kind_unavailable: list[str] = []
    for slug in slugs:
        if slug not in known_set:
            unknown.append(slug)
            continue
        body = loader(slug) or ""
        if unavailable_kinds:
            referenced = _kinds_referenced_by_skill(slug, parse_frontmatter(body))
            if any(k in unavailable_kinds for k in referenced):
                kind_unavailable.append(slug)
                continue
        pinned.append(slug)
    if unknown:
        log.warning(
            "PRECIS_STARTUP_SKILLS skipped unknown skill ids: %s",
            ", ".join(unknown),
        )
    if kind_unavailable:
        log.warning(
            "PRECIS_STARTUP_SKILLS targets unavailable kinds: %s",
            ", ".join(kind_unavailable),
        )
    return Resolution(
        pinned=tuple(pinned),
        unknown=tuple(unknown),
        kind_unavailable=tuple(kind_unavailable),
    )


def format_banner(result: Resolution) -> str:
    """Render the banner notice for ``serverInfo.instructions``.

    Returns the empty string when nothing is pinned and no errors
    occurred (zero unconditional banner bytes — operators who don't
    set the env var pay nothing). Errors surface even with an empty
    pinned set so the operator can fix the configuration without
    digging through stderr.
    """
    lines: list[str] = []
    if result.pinned:
        lines.append("Pinned skills: " + ", ".join(result.pinned) + ".")
    if result.unknown:
        lines.append("⚠ unknown skill ids: " + ", ".join(result.unknown) + ".")
    if result.kind_unavailable:
        lines.append(
            "\u26a0 skills for unavailable kinds: "
            + ", ".join(result.kind_unavailable)
            + "."
        )
    return "\n".join(lines)
