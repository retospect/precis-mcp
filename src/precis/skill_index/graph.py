"""Skill graph — wikilink edges, tag groups, kind groups.

Pure, text-derived lateral structure over the shipped skill corpus
(docs/backlog/skill-graph.md slice 1): every ``[[slug]]`` wikilink in a
skill body is an edge, symmetrized so a target skill knows who links to
it even though only the source file wrote the link; ``tags:``/``kinds:``
frontmatter group skills for the ``tag=``/``kind=`` toc filters and the
served-skill footer (``handlers/skill.py``, slice 1 surfacing).

No embedder, no DB — this is deliberately decoupled from
:class:`~precis.skill_index.index.FileCorpusIndex`, which builds
alongside it from the same ``{slug: raw_text}`` map. A graph build never
touches the embedder and never blocks on it; a cold/absent embedder
still gets a fully-populated graph.

Exposed at the server seam (``server.py``) rather than reached for
through ``handlers.skill`` privates — slice 4 (kind-help / error-path
injection) is the reason this lives in its own module: it's a consumer
outside the skill handler, so the API can't be a `_private` of
``skill.py``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from precis.handlers._skill_common import extract_wikilinks, parse_frontmatter


@dataclass(frozen=True)
class SkillGraph:
    """Derived lateral structure over a fixed ``{slug: raw_text}`` map.

    - ``outbound`` / ``inbound`` — wikilink edges as declared (outbound)
      and their reverse (inbound). Each maps a slug to a tuple of
      distinct slugs, order-preserving on the outbound side (frontmatter
      link order), ``()`` when none. A dangling link (target not a real
      slug in the corpus) still appears in ``outbound`` — the ingest
      gate (``ingest/skill_ingest.py``) is what flags it as a finding —
      but never in any ``inbound``, since there's no real skill to
      attribute the edge to.
    - ``tags`` / ``kinds`` — axis value -> sorted tuple of slugs
      carrying it. Built from frontmatter, independent of the wikilink
      edges above.
    """

    outbound: dict[str, tuple[str, ...]] = field(default_factory=dict)
    inbound: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tags: dict[str, tuple[str, ...]] = field(default_factory=dict)
    kinds: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def linked(self, slug: str, *, cap: int | None = None) -> tuple[str, ...]:
        """Symmetrized neighbours of ``slug`` — outbound ∪ inbound,
        deduplicated, sorted for a stable render order.

        ``cap`` truncates the *returned* tuple to its first ``cap``
        entries (the footer's inbound cap, docs/backlog/skill-graph.md
        slice 1) — a caller that also needs the true neighbour count
        (to render "+N more, search for the rest") must take
        ``len(graph.linked(slug))`` *before* capping, not after.
        """
        merged = set(self.outbound.get(slug, ())) | set(self.inbound.get(slug, ()))
        merged.discard(slug)  # a self-link is not a neighbour
        out = tuple(sorted(merged))
        return out[:cap] if cap is not None else out

    def by_tag(self, tag: str) -> tuple[str, ...]:
        """Slugs carrying ``tag``, sorted. ``()`` if the tag is unused."""
        return self.tags.get(tag, ())

    def by_kind(self, kind: str) -> tuple[str, ...]:
        """Slugs whose ``kinds:`` includes ``kind``, sorted. ``()`` if none."""
        return self.kinds.get(kind, ())


def build_skill_graph(files: dict[str, str]) -> SkillGraph:
    """Build a :class:`SkillGraph` from ``{slug: raw_text}``.

    Pure function of the corpus text — every call re-derives from
    scratch (skills are static for the life of the process, same
    assumption as ``handlers.skill._SKILLS_MAP_CACHE``, so callers
    typically build this once and hold onto it). ``files`` is expected
    to already be the corpus a caller wants graphed — the server seam
    builds it from every shipped + plugin-contributed skill.
    """
    outbound: dict[str, tuple[str, ...]] = {}
    inbound_acc: dict[str, list[str]] = defaultdict(list)
    tags_acc: dict[str, list[str]] = defaultdict(list)
    kinds_acc: dict[str, list[str]] = defaultdict(list)

    for slug in sorted(files):
        text = files[slug]
        links = tuple(t for t in extract_wikilinks(text) if t != slug)
        outbound[slug] = links
        for target in links:
            if target in files:  # only real slugs become inbound edges
                inbound_acc[target].append(slug)

        fm = parse_frontmatter(text)
        for tag in fm.tags:
            tags_acc[tag].append(slug)
        for kind in fm.kinds or ():
            kinds_acc[kind].append(slug)

    inbound = {slug: tuple(srcs) for slug, srcs in inbound_acc.items()}
    tags = {tag: tuple(slugs) for tag, slugs in tags_acc.items()}
    kinds = {kind: tuple(slugs) for kind, slugs in kinds_acc.items()}
    return SkillGraph(outbound=outbound, inbound=inbound, tags=tags, kinds=kinds)
