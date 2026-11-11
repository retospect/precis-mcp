"""Bounded neighbour-link audit over the shipped skill corpus (gr626719).

The serve surface already exposes skill neighbours on several axes —
``[[slug]]`` wikilinks (symmetrised by :class:`~precis.skill_index.SkillGraph`),
curated ``_SKILL_CATEGORIES`` siblings (the footer's ``see also:`` row),
``tags:``/``kinds:`` groupings, the ``family:`` header's ``siblings:``
pointer — plus the ``related:`` line a search renders on its top hit.
This module does not rebuild a mesh: it validates the existing surfaces
and reports exceptions. It never invents neighbours — an entry in the
report is a fact about the corpus for a human to act on (or accept).

:func:`skill_neighbour_audit` returns a :class:`SkillNeighbourReport`
with four sections:

- ``dangling`` — ``(slug, target)`` wikilink edges whose target resolves
  to no served slug (corpus ∪ the synthesised meta-skills
  ``precis-help``/``precis-status``/``precis-toc``/``toc``). The ingest
  gate (``ingest/skill_ingest.py``) hard-fails these at scan time; this
  re-verifies at the served-corpus layer — same resolution set the
  graph footer and ``_expand_wikilinks`` rely on.
- ``isolated`` — skills no lateral neighbour surface touches: no
  wikilink edge in either direction AND no curated
  ``_SKILL_CATEGORIES`` siblings. Family grouping still reaches them
  (``family=`` and the header's ``siblings:`` line), so "isolated" here
  means "reachable only by facet or direct search", not unreachable.
  Personas count too — an edge-less persona is as mesh-orphaned as any
  other file (``invokes_personas`` lives outside the wikilink graph).
- ``overflow`` — skills whose ``linked()`` neighbourhood exceeds the
  footer's ``_FOOTER_LINK_CAP``. The footer prints ``+N more`` and
  redirects the agent to ``search(kind='skill', q='<slug>')``; each
  entry pairs the slug with the beyond-cap neighbours that suggested
  search must carry. Persona skills are excluded — their footer never
  renders by design (docs/backlog/skill-graph.md slice 2).
- ``scope_hidden`` — non-persona skills with neighbours whose *every*
  neighbour is ``scope: write``: a read-only connection's footer and
  ``related:`` line filter them all out, so on that scope the skill
  shows no neighbour at all. Empty today; a finding means the skill's
  whole neighbourhood is fenced.

Pure text + graph — no embedder, no DB, no live ``search()`` calls.
The live-search half of the validation (does ``search(q=slug)`` actually
render a neighbour?) is a render-time property and lives in
``tests/test_skill_neighbours.py``, which gates this report.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.handlers.skill import (
    _FOOTER_LINK_CAP,
    _skill_is_write_only,
    _tree_siblings,
    skill_corpus_texts,
)
from precis.skill_index import build_skill_graph
from precis.utils.skill_common import SYNTH_SKILL_SLUGS, parse_frontmatter

#: Cap on per-section rows in :meth:`SkillNeighbourReport.render` — the
#: report stays bounded even if a section balloons (a corpus that lost
#: half its wikilinks would still print a page, not a wall).
_RENDER_CAP = 20


@dataclass(frozen=True)
class SkillNeighbourReport:
    """The audit's findings. Every field is sorted for stable output."""

    #: Corpus size audited (file-backed + plugin-contributed skills).
    skills: int = 0
    #: ``(slug, unresolvable wikilink target)`` edges, sorted by slug.
    dangling: tuple[tuple[str, str], ...] = ()
    #: Slugs with no wikilink edges and no curated siblings, sorted.
    isolated: tuple[str, ...] = ()
    #: ``(slug, beyond-cap neighbours)`` for footers that print ``+N
    #: more``, sorted by slug; neighbours sorted within each entry.
    overflow: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Slugs whose every neighbour is ``scope: write``, sorted.
    scope_hidden: tuple[str, ...] = ()

    def render(self) -> str:
        """One line per finding, each section capped at ``_RENDER_CAP``
        rows — the shape a test failure or an operator wants to see."""
        lines = [f"skill neighbour audit — {self.skills} skills"]

        def _section(
            title: str, rows: tuple[Any, ...], fmt: Callable[[Any], str]
        ) -> None:
            lines.append(f"{title}: {len(rows)}")
            for row in rows[:_RENDER_CAP]:
                lines.append(f"  {fmt(row)}")
            if len(rows) > _RENDER_CAP:
                lines.append(f"  … +{len(rows) - _RENDER_CAP} more")

        _section(
            "dangling wikilinks",
            self.dangling,
            lambda r: f"{r[0]} -> [[{r[1]}]]",
        )
        _section("isolated skills (no links, no curated siblings)", self.isolated, str)
        _section(
            "linked: overflow (+N more — search must carry these)",
            self.overflow,
            lambda r: f"{r[0]}: {', '.join(r[1])}",
        )
        _section(
            "read-scope-hidden neighbourhoods (all neighbours scope: write)",
            self.scope_hidden,
            str,
        )
        return "\n".join(lines)


def skill_neighbour_audit(
    *,
    corpus: dict[str, str] | None = None,
) -> SkillNeighbourReport:
    """Audit the served skill corpus's neighbour surfaces.

    ``corpus`` substitutes the packaged ``{slug: raw}`` map (tests);
    ``None`` audits what a live server would serve —
    :func:`~precis.handlers.skill.skill_corpus_texts`. The graph is
    rebuilt from the corpus rather than read from the handler's cache
    so the report is a pure function of the input map.
    """
    files = skill_corpus_texts() if corpus is None else corpus
    graph = build_skill_graph(files)
    servable = set(files) | SYNTH_SKILL_SLUGS

    dangling: list[tuple[str, str]] = []
    isolated: list[str] = []
    overflow: list[tuple[str, tuple[str, ...]]] = []
    scope_hidden: list[str] = []
    for slug in sorted(files):
        persona = parse_frontmatter(files[slug]).flavor == "persona"
        for target in graph.outbound.get(slug, ()):
            if target not in servable:
                dangling.append((slug, target))
        linked = graph.linked(slug)
        if not linked and not _tree_siblings(slug):
            isolated.append(slug)
        if persona or not linked:
            continue
        if len(linked) > _FOOTER_LINK_CAP:
            overflow.append((slug, linked[_FOOTER_LINK_CAP:]))
        if all(_skill_is_write_only(s, corpus=files) for s in linked):
            scope_hidden.append(slug)
    return SkillNeighbourReport(
        skills=len(files),
        dangling=tuple(dangling),
        isolated=tuple(isolated),
        overflow=tuple(overflow),
        scope_hidden=tuple(scope_hidden),
    )
