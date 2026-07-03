"""SkillHandler — markdown skill docs served from the data directory.

Slug-addressed, file-backed (not DB-backed). Each ``.md`` file under
``src/precis/data/skills/`` is one skill; the file's stem (without
the ``.md`` suffix) is the slug.

This is the agent's manual: ``get(kind='skill', id='precis-overview')``
returns the overview, ``get(kind='skill', id='precis-paper-help')``
returns the paper-handler docs, etc.

A bare ``get(kind='skill')`` lists every **available** skill — i.e.
every skill whose ``status:`` front-matter is ``active`` (or absent)
*and* whose subject kind is registered in the live runtime. Skills
documenting unregistered kinds (``precis-markdown-help`` when the
markdown handler isn't wired) and skills tagged ``status: planned``
or ``status: aspirational`` are filtered out — they remain
retrievable by exact slug, but with a banner warning the agent that
the recipes inside don't all execute on this build.

This filtering closes the MCP critic's CRITICAL #3 finding
("tools/list advertises skills for kinds the registry rejects")
and MAJOR #6 ("``precis-density`` documents three views that don't
exist") — the index now agrees with the runtime.

Front-matter ``title:`` is surfaced in the index; a one-line
description (the first non-front-matter line) acts as a synopsis.

Read-only. Skills ship with the package; agents can't write them at
runtime — that's by design (skills are versioned with code).
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import re
import socket
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.protocol import _ALL_VERBS, Handler, KindSpec
from precis.response import Response
from precis.skill_index import FileCorpusIndex, SearchHit
from precis.utils.next_block import render_next_section
from precis.utils.rake import keyword_summary
from precis.utils.search_header import format_search_headline

log = logging.getLogger(__name__)


# Slugs are conservative: lowercase ASCII alphanumerics + hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

# ``slug~N`` / ``slug~A..B`` / ``slug/toc`` parse pattern. Mirrors
# the paper handler's ``_parse_paper_id`` shape so the address
# grammar is identical across kinds. Phase B integration 2026-05-31.
_SKILL_ID_RE = re.compile(
    r"^(?P<slug>[a-z0-9][a-z0-9\-]*)"
    r"(?:~(?P<lo>\d+)(?:\.\.(?P<hi>\d+))?)?"
    r"(?:/(?P<view>[a-z]+))?$"
)


def _parse_skill_id(
    raw: str,
) -> tuple[str, tuple[int, int] | None, str | None]:
    """Split ``raw`` into ``(slug, chunk_spec, path_view)``.

    Returns:
        slug: lowercase slug (always present; ``BadInput`` if missing).
        chunk_spec: ``(lo, hi)`` inclusive for ``~N`` (lo==hi) or
            ``~A..B``. ``None`` when no chunk selector is given.
        path_view: trailing ``/view`` suffix as a string (today only
            ``"toc"`` is meaningful). ``None`` when absent.
    """
    m = _SKILL_ID_RE.match(raw)
    if m is None:
        raise BadInput(
            f"unparseable skill id: {raw!r}",
            next=(
                "skill ids are slug, slug~N, slug~A..B, "
                "or slug/toc — letters/digits/hyphens only"
            ),
        )
    slug = m.group("slug")
    lo = m.group("lo")
    hi = m.group("hi")
    chunk_spec: tuple[int, int] | None = None
    if lo is not None:
        lo_i = int(lo)
        hi_i = int(hi) if hi is not None else lo_i
        if hi_i < lo_i:
            raise BadInput(
                f"inverted skill chunk range: ~{lo_i}..{hi_i}",
                next=(f"use the smaller bound first: ~{hi_i}..{lo_i}"),
            )
        chunk_spec = (lo_i, hi_i)
    path_view = m.group("view")
    if path_view is not None and path_view not in _SKILL_PATH_VIEWS:
        raise BadInput(
            f"unknown skill view {path_view!r}",
            options=sorted(_SKILL_PATH_VIEWS),
            next=f"supported path views: {sorted(_SKILL_PATH_VIEWS)}",
        )
    return slug, chunk_spec, path_view


#: Path-view suffixes accepted on skill ids (``slug/toc``). Mirrors
#: the paper handler's path-view set; today only ``toc`` is wired.
_SKILL_PATH_VIEWS: frozenset[str] = frozenset({"toc"})


# Skill catalogue grouped by purpose. Order matters — the categories
# render top-to-bottom in this order so the "start here" buckets are
# what an agent sees first. Slugs not listed below land in the
# "Other" trailing bucket so we never silently drop a new skill from
# the index. Updated 2026-05-30 per maintainer's top-layer sketch:
# Orientation → Core verbs → Content types → Research & validation →
# Workflow tools.
#
# Notes
# -----
# * ``toc`` is a synth alias of ``precis-toc`` and is omitted here
#   (folded into ``precis-toc``'s row with an "(alias: toc)" suffix
#   so the index doesn't list it twice).
# * Skills documenting unwired kinds still get filtered into the
#   "Hidden" section via ``_availability_gap``; the category they
#   belong to only matters for the active listing.
_SKILL_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Orientation",
        (
            "precis-overview",
            "precis-help",
            "precis-toc",
            "precis-toc-help",
            "precis-status",
            "precis-status-help",
            "precis-startup-skills-help",
            "precis-session-context-help",
            "precis-kinds-disabled-help",
        ),
    ),
    (
        "Core verbs",
        (
            "precis-get-help",
            "precis-put-help",
            "precis-edit-help",
            "precis-search-help",
            "precis-delete-help",
            "precis-tag-help",
            "precis-tags",
            "precis-link-help",
            "precis-relations",
        ),
    ),
    (
        "Content types",
        (
            "precis-files-help",
            "precis-markdown-help",
            "precis-plaintext-help",
            "precis-tex-help",
            "precis-python-help",
            "precis-paper-help",
            "precis-paper-tag-axes",
            "precis-patent-help",
            "precis-patent-search-help",
            "precis-patent-power",
            "precis-web-help",
            "precis-youtube-help",
            "precis-conv-help",
        ),
    ),
    (
        "Research & validation",
        (
            "precis-cite-paper-help",
            "precis-citation-help",
            "precis-check-source-help",
            "precis-finding-help",
            "precis-provenance-help",
            "precis-preflight",
            "precis-doi-resolution",
            "precis-orcid-help",
            "precis-author-discovery-help",
            "precis-math-help",
            "precis-perplexity-help",
            "precis-oracle-help",
        ),
    ),
    (
        "Workflow tools",
        (
            "precis-memory-help",
            "precis-todo-help",
            "precis-flashcard-help",
            "precis-cache",
            "precis-random-help",
            "precis-gripe-help",
            "precis-toon",
        ),
    ),
)


# Inline aliases shown next to the canonical slug — keeps the index
# from listing two rows for the same skill. ``toc`` is the only one
# today; new aliases land here.
_SKILL_ALIASES_INLINE: dict[str, tuple[str, ...]] = {
    "precis-toc": ("toc",),
}


def _slug_with_aliases(slug: str) -> str:
    """Return ``slug`` with any registered aliases noted inline."""
    aliases = _SKILL_ALIASES_INLINE.get(slug)
    if not aliases:
        return slug
    return f"{slug} (alias: {', '.join(aliases)})"


def _categorise_skills(
    slugs: list[str],
) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Group ``slugs`` into the top-layer categories.

    Returns ``(groups, uncategorised)``:

    * ``groups`` — ``[(category_name, [slug, slug, ...]), ...]`` in
      the order defined by ``_SKILL_CATEGORIES``. Categories with
      zero matching slugs are dropped from the output entirely;
      they'd be visual noise.
    * ``uncategorised`` — slugs not listed under any category,
      preserving input order. Render these into a trailing "Other"
      bucket so new skills don't silently disappear from the
      catalogue.

    ``slugs`` may contain any slugs — synth, file-backed, or aliases.
    Skills aliased via :data:`_SKILL_ALIASES_INLINE` are dropped from
    the input set on the assumption their canonical slug is rendered
    elsewhere with the alias noted inline (see ``_slug_with_aliases``).
    """
    aliases_to_drop: set[str] = {
        alias for aliases in _SKILL_ALIASES_INLINE.values() for alias in aliases
    }
    remaining: list[str] = [s for s in slugs if s not in aliases_to_drop]
    remaining_set: set[str] = set(remaining)

    groups: list[tuple[str, list[str]]] = []
    placed: set[str] = set()
    for category, members in _SKILL_CATEGORIES:
        in_category = [s for s in members if s in remaining_set]
        if in_category:
            groups.append((category, in_category))
            placed.update(in_category)

    uncategorised = [s for s in remaining if s not in placed]
    return groups, uncategorised


#: Score pinned on a skill whose title / H1 contains the full query
#: phrase — above any cosine similarity so the obvious intent match
#: ("how do I cite a paper" → precis-cite-paper-help) leads the results.
_TITLE_MATCH_SCORE = 1.5


class _SkillSearchRow:
    """Per-slug merged hit for the search response.

    Pure presentation glue — the search method builds these from
    semantic + lexical streams, deduplicates by slug (keeping the
    higher-scoring source), then renders them in score order. Kept
    as a plain class instead of a dataclass to avoid pulling extra
    decorators in the hot path of a search call.

    The ``section`` and ``snippet`` columns surface separately so the
    TOON render lets an agent see which H2 a hit lives under and the
    matched text in independent columns — round-2 picky reviewer
    flagged the previous ``score · heading\\n  snippet`` blob as
    monolithic.
    """

    __slots__ = ("score", "section", "slug", "snippet", "source")

    def __init__(
        self,
        *,
        slug: str,
        score: float,
        source: str,
        section: str,
        snippet: str,
    ) -> None:
        self.slug = slug
        self.score = score
        self.source = source
        self.section = section
        self.snippet = snippet


def _semantic_row(hit: SearchHit) -> _SkillSearchRow:
    """Build a row from a semantic hit, normalising heading + snippet."""
    return _SkillSearchRow(
        slug=hit.slug,
        score=hit.score,
        source="semantic",
        section=hit.heading or "",
        snippet=(hit.snippet or "").strip(),
    )


def _lexical_row(slug: str, count: int, preview: str) -> _SkillSearchRow:
    """Build a row from a substring-match hit.

    Substring hits don't carry a section; we record the match count
    in ``section`` (``"3 substring hits"``) so the TOON column stays
    populated and informative.
    """
    # Substring counts map to a [0.0, 0.5) score so they rank below
    # any genuine semantic hit but still surface when the index missed.
    lex_score = min(0.49, count / 20.0)
    short = (preview[:160] + "…") if len(preview) > 160 else preview
    word = "hit" if count == 1 else "hits"
    return _SkillSearchRow(
        slug=slug,
        score=lex_score,
        source="lexical",
        section=f"{count} substring {word}",
        snippet=short.strip(),
    )


class SkillHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="skill",
        title="Skill",
        description=(
            "Agent skill - markdown documentation served from the "
            "package data dir. Read-only; ships with code."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=False,
        is_numeric=False,
        id_required=False,
        role="system",
    )

    #: Special slugs synthesised at runtime rather than served from a
    #: markdown file.  Maps slug → one-line description used in the
    #: index view.  The actual rendering dispatches via ``_render_<slug
    #: stem>``: ``precis-help`` → ``_render_help``, ``precis-status``
    #: → ``_render_status``.
    #:
    #: Adding a synthesised skill is one entry here plus one render
    #: method.  All other gates (availability-gap, index, prompt
    #: enumeration) discover the slug through this dict.
    _SYNTHESIZED_SKILLS: ClassVar[dict[str, str]] = {
        "precis-help": ("active kinds + verbs (auto-generated from this server)"),
        "precis-status": ("build + runtime + DB + optional-dependency probe"),
        "precis-toc": ("table of contents - every skill with a one-line summary"),
        "toc": ("alias for precis-toc"),
    }

    #: Synth slugs that map to the same renderer as another synth slug.
    #: Avoids duplicating ``_render_*`` methods for short aliases like
    #: ``toc`` → ``precis-toc``. Keys and values are slugs from
    #: :data:`_SYNTHESIZED_SKILLS`.
    _SYNTH_ALIASES: ClassVar[dict[str, str]] = {
        "toc": "precis-toc",
    }

    #: Backwards-compat alias for the original single-slug attribute.
    #: Tests and the old availability-gap path reference this name; the
    #: new code paths consult ``_SYNTHESIZED_SKILLS`` directly.
    _SYNTHESIZED_SLUG: ClassVar[str] = "precis-help"

    def __init__(self, *, hub: Hub) -> None:
        # The skill handler is file-backed: markdown under
        # ``precis.data.skills`` plus synthesised meta-skills. The
        # hub itself is only needed for ``precis-help`` / ``precis-
        # status`` rendering, and that reference is planted on
        # ``self.hub`` by :meth:`Handler._register_with` immediately
        # after this ``__init__`` returns — so we intentionally do
        # no work here. Accepting ``hub=`` keeps the boot-loop
        # kw-args shape uniform across every handler.
        _ = hub

        # Lazy-built embedded index over ``precis.data.skills/*.md``
        # — populated on the first ``search()`` call so cold start
        # stays cheap. Falls back to substring search when no
        # embedder is wired (see :meth:`_get_index`).
        self._index: FileCorpusIndex | None = None

        # Memoised view='toc' render per (slug, scope). Skill files
        # are static disk content for the life of the process, so
        # one DP+KeyBERT pass per (slug, scope) is enough — the
        # per-request renderer in ``utils/toc.py`` is expensive
        # (~hundreds of ms on a dozen-chunk skill) and was being
        # re-run on every ``get(kind='skill', id='X/toc')``. The
        # paper handler already cut over to the db-backed renderer
        # (ADR 0018-superseding F20) but the skill handler still
        # uses the on-demand path; caching avoids the worst-case
        # cost without a schema change for skills.
        self._toc_cache: dict[tuple[str, tuple[int, int] | None], str] = {}

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Round-2 picky 2026-05-30: ``get(kind='skill', q='reading a
        # paper')`` previously dropped ``q=`` and returned the grouped
        # index — agents searching by topic via ``get`` got a flat
        # category list when they wanted ranked matches. Delegate to
        # ``search`` so the verbs converge on the obvious intent.
        if id is None and q is not None and q.strip():
            return self.search(q=q)
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_index()

        raw_id = str(id).strip()
        # Parse the id for skill-chunk selector syntax: ``slug~N``,
        # ``slug~A..B``, ``slug/toc``. Same shape as the paper
        # handler's address grammar so the agent's mental model
        # carries across kinds.
        slug, chunk_spec, path_view = _parse_skill_id(raw_id)

        if not _SLUG_RE.match(slug):
            raise BadInput(
                f"invalid skill slug: {slug!r}",
                next="skill slugs are lowercase letters/digits/hyphens",
            )

        # Path view (``slug/toc``) and explicit ``view='toc'`` both
        # render the TOC. Chunk specs ``slug~N`` / ``slug~A..B`` go
        # through the chunk-resolver below.
        effective_view = path_view or view
        # Phase F 2026-05-31: unknown views error with the per-kind
        # accepted list so the agent learns the enum from one
        # round-trip rather than guessing.
        if effective_view is not None:
            accepted = self.accepted_views(id=slug)
            if effective_view not in accepted:
                raise BadInput(
                    f"unknown skill view {effective_view!r}",
                    options=accepted,
                    next=(
                        f"view= for kind='skill' accepts: {accepted}; "
                        f"omit view= for the markdown body"
                    ),
                )
        if effective_view == "toc":
            return self._render_skill_toc(slug, scope=chunk_spec)
        if chunk_spec is not None:
            return self._render_skill_chunks(slug, chunk_spec)

        # Synthesised meta-skills: enumerate active kinds, probe
        # optional deps, …  Each entry in ``_SYNTHESIZED_SKILLS``
        # dispatches to ``_render_<slug-stem>``.
        if slug in self._SYNTHESIZED_SKILLS:
            # Resolve aliases first so e.g. ``toc`` dispatches to the
            # same renderer as ``precis-toc``.
            target = self._SYNTH_ALIASES.get(slug, slug)
            # Strip the ``precis-`` prefix when present; bare slugs
            # (``toc``) dispatch via their whole name. The stem
            # produces ``_render_<stem>`` with hyphens folded to
            # underscores so multi-word stems work too.
            stem = target.split("-", 1)[1] if "-" in target else target
            method_name = "_render_" + stem.replace("-", "_")
            renderer = getattr(self, method_name, None)
            if renderer is None:  # pragma: no cover — registry typo
                raise NotFound(
                    f"synthesised skill {slug!r} has no renderer",
                    next="see SkillHandler._SYNTHESIZED_SKILLS",
                )
            return Response(body=renderer())

        text = _load_skill(slug)
        if text is None:
            available = sorted(_list_skills())
            available.extend(self._SYNTHESIZED_SKILLS)
            raise NotFound(
                f"skill {slug!r} not found",
                options=sorted(available),
                next="get(kind='skill') to list every skill",
            )
        # Banner if this skill is filtered from the index — the agent
        # asked for it explicitly, so we serve it, but we want them
        # to know the recipes inside may not all run on this build.
        # See CRITICAL #3 and MAJOR #6 in the MCP critique.
        gap = _availability_gap(slug, hub=self.hub)
        if gap is not None:
            text = f"> **Heads up:** {gap}\n\n" + text
        # Append a live-registry footer so cross-cutting skills
        # (precis-overview, precis-files-help) that mention kinds in
        # tables can't drift against the active build. Each skill
        # gets a one-line summary of the registered kinds so the
        # reader can cross-check their plan against reality.
        # MCP critic MAJOR-C 2026-05-02.
        text = text.rstrip() + "\n\n" + self._live_registry_footer()
        return Response(body=text)

    def _live_registry_footer(self) -> str:
        """Markdown footer listing active kinds on this build.

        Rendered from ``self.hub.kinds`` at serve time so it can't
        drift against the registry. Appended to every non-synth
        skill response; the synth ``precis-help`` already renders
        its own kind table so it doesn't get a redundant footer
        (synth skills don't flow through this code path).
        """
        if self.hub is None:
            return ""
        try:
            kinds = sorted(self.hub.kinds)
        except (AttributeError, TypeError):
            return ""
        if not kinds:
            return ""
        return (
            "---\n"
            f"**Active kinds on this build:** {', '.join(kinds)}. "
            "See `get(kind='skill', id='precis-help')` for a verb "
            "table generated from the live registry."
        )

    # ── search ─────────────────────────────────────────────────────

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        page_size: int = 10,
        **_kw: Any,
    ) -> Response:
        # ``q=`` is optional — round-2 picky N4/F-6, 2026-05-30. The
        # rest of the surface (``search(kind='memory', tags=[...])``,
        # for instance) degrades on empty q to a list view; doing the
        # same here keeps the verb consistent across kinds and gives
        # the agent a runnable second-step option that mirrors
        # ``get(kind='skill')``'s index.
        if q is None or not q.strip():
            return self.get()

        # Two-stream search: cosine over chunk embeddings (best at
        # natural phrasing) merged with substring matches (best at
        # literal slugs / kind names / verbatim quotes). Each stream
        # contributes hits; per-slug we keep the better-scoring one
        # so ranking stays sharp. The index is silently unavailable
        # on builds without an embedder — substring carries on alone.
        #
        # 2026-06-06: over-fetch 5× page_size so that (a) after
        # dropping unwired skills there are still page_size wired
        # survivors to show, and (b) the per-slug semantic-hit count
        # used for the ``more`` column is a closer approximation of
        # "how many H2 sections of this skill matched."
        over_fetch = page_size * 5
        semantic_hits = self._semantic_hits(q, page_size=over_fetch)
        lexical_hits = self._lexical_hits(q)

        # Count semantic chunk hits per slug BEFORE dedup so the
        # ``more`` column can reflect "this skill has N additional
        # matching sections" — same signal as the paper-mode
        # ``more`` design in backlog-search-unique-per-paper.md.
        # Exclude body-only twins (v3): they re-embed a section that
        # already has a structural chunk, so counting them would
        # inflate "additional matching sections."
        sem_hits_per_slug: Counter[str] = Counter(
            h.slug for h in semantic_hits if not h.body_only
        )

        merged: dict[str, _SkillSearchRow] = {}
        for hit in semantic_hits:
            row = _semantic_row(hit)
            existing = merged.get(hit.slug)
            if existing is None or row.score > existing.score:
                merged[hit.slug] = row
        for slug, count, preview in lexical_hits:
            if slug in merged:
                continue
            merged[slug] = _lexical_row(slug, count, preview)

        # Exact title / H1 phrase boost. A query that *is* a substring of
        # a skill's title or H1 ("how do I cite a paper" →
        # precis-cite-paper-help) is an almost-certain intent match — but
        # the lexical leg caps such a hit below 0.5 and the merge above
        # drops it whenever any (possibly weak) semantic hit shares the
        # slug, so the obvious doc sinks below the page_size cut. Pin a
        # title/H1 phrase match to the top regardless of stream. Guarded
        # on a 4+ char needle so a degenerate 1-3 char query can't promote
        # half the catalogue.
        needle = _norm_for_substr(q)
        if len(needle) >= 4:
            for slug in _list_skills():
                title = _skill_title_text(slug)
                if not title or needle not in _norm_for_substr(title):
                    continue
                row = merged.get(slug)
                if row is None:
                    merged[slug] = _SkillSearchRow(
                        slug=slug,
                        score=_TITLE_MATCH_SCORE,
                        source="title",
                        section="title match",
                        snippet=title.strip(),
                    )
                elif row.score < _TITLE_MATCH_SCORE:
                    row.score = _TITLE_MATCH_SCORE
                    row.source = "title"

        if not merged:
            # Distinguish "genuinely no match" from "lexical drew blank
            # while semantic was unavailable" — the latter is recoverable
            # by retrying once the embedder warms (or by pointing the MCP
            # at the always-hot precis serve-embeddings via
            # PRECIS_EMBEDDER=remote). Broad-pass finding #1: the
            # previous unconditional "no skills mention" was over-
            # confident during the bge-m3 cold-start window.
            semantic_available = self._semantic_available()
            if semantic_available:
                headline = f"no skills mention {q!r}"
                tip = "Try a different phrasing, or fall back to the index:"
            else:
                headline = (
                    f"no lexical matches for {q!r}; semantic search is "
                    "warming and didn't contribute this turn"
                )
                tip = (
                    "Retry in ~30s once the embedder warms, or fall back to the index:"
                )
            return Response(
                body=(
                    f"{headline}\n\n"
                    f"{tip}"
                    + render_next_section(
                        [
                            (
                                "get(kind='skill')",
                                "browse the grouped catalogue",
                            ),
                            (
                                "get(kind='skill', id='toc')",
                                "table of contents with synopses",
                            ),
                        ]
                    )
                )
            )

        all_rows = sorted(merged.values(), key=lambda r: r.score, reverse=True)

        # 2026-06-06: partition by availability. Unwired skills are
        # filtered from the result rows (recipes won't all run on
        # this build, and an LLM with no cross-session memory gains
        # nothing from reading them) and surfaced instead as a
        # single escalation line below the table so the agent can
        # still suggest "spin up a build with kind X wired."
        wired_rows: list[_SkillSearchRow] = []
        unwired_rows: list[_SkillSearchRow] = []
        for row in all_rows:
            if _availability_gap(row.slug, hub=self.hub) is not None:
                unwired_rows.append(row)
            else:
                wired_rows.append(row)

        total_wired = len(wired_rows)
        visible = wired_rows[:page_size]

        # Round-2 picky 2026-05-31: dropped low-signal columns
        # (status/source/score) that the maintainer flagged as
        # uninformative for a top-K skill search. Replaced the raw
        # snippet with RAKE-extracted key phrases — agents scanning
        # results want the *topic* of the matched section, not its
        # first 140 characters of prose.
        #
        # 2026-06-06: added a ``more`` column counting additional
        # semantic H2 hits in the same skill (``+3`` / ``.``) —
        # mirrors the paper-mode ``more`` design. Informational
        # only: ``get(kind='skill', id=…)`` returns the whole file,
        # so unlike papers there's no per-section drill verb to
        # spend the signal on, but it still distinguishes "broadly
        # relevant skill" from "one paragraph happens to match."
        table_rows: list[dict[str, str]] = []
        for row in visible:
            extra = max(0, sem_hits_per_slug.get(row.slug, 0) - 1)
            more = f"+{extra}" if extra > 0 else "."
            table_rows.append(
                {
                    "slug": row.slug,
                    "section": row.section,
                    "more": more,
                    "keywords": keyword_summary(row.snippet, top_k=5),
                }
            )

        if visible:
            head = format_search_headline(
                n_returned=len(visible),
                total=total_wired,
                noun="skill match",
                query=q,
            )
            body = (
                head
                + "\n\n"
                + render_agent_table(
                    table_rows,
                    schema=["slug", "section", "more", "keywords"],
                )
            )
        else:
            body = f"# no actionable skill matches for {q!r}"

        if unwired_rows:
            _ESC_CAP = 5
            shown = [r.slug for r in unwired_rows[:_ESC_CAP]]
            overflow = len(unwired_rows) - len(shown)
            tail = f" (+{overflow} more)" if overflow > 0 else ""
            body += (
                "\n\n"
                f"Also matched in unwired skills: {', '.join(shown)}{tail}. "
                "These need a build with their kind enabled to run; "
                "use `get(kind='skill', id='<slug>')` only to read for "
                "context, not to invoke recipes."
            )

        body += render_next_section(
            [
                (
                    "get(kind='skill', id='<slug-from-above>')",
                    "read the full skill (paste any slug from the table)",
                ),
                (
                    f"search(kind='skill', q={q!r}, page_size=25)",
                    "widen to more hits",
                ),
            ]
        )
        return Response(body=body)

    # ── search helpers ─────────────────────────────────────────────

    def _semantic_hits(self, q: str, *, page_size: int) -> list[SearchHit]:
        """Return cosine-ranked chunk hits, or ``[]`` when no embedder."""
        index = self._get_index()
        if index is None:
            return []
        return index.search(q, page_size=page_size)

    def _semantic_available(self) -> bool:
        """True when the semantic side of skill search can answer.

        Used by the empty-result path to distinguish "lexical empty AND
        semantic genuinely returned nothing" from "lexical empty AND
        semantic couldn't run because the embedder is cold." The latter
        deserves a "retry soon" hint instead of an overconfident "no
        skills mention." Broad-pass finding #1.
        """
        index = self._get_index()
        if index is None:
            return False
        embedder = getattr(index, "_embedder", None)
        if embedder is None:
            return False
        is_ready = getattr(embedder, "is_ready", None)
        if callable(is_ready):
            return bool(is_ready())
        # Backends without is_ready() (RemoteEmbedder, MockEmbedder)
        # are always-ready by construction.
        return True

    def _lexical_hits(self, q: str) -> list[tuple[str, int, str]]:
        """Substring-match every skill body against ``q``.

        Hyphens and whitespace are normalised on both sides so a
        natural-language query (``spaced repetition``) finds the
        corpus's hyphenated form (``spaced-repetition``) and vice
        versa. (MCP critic MAJOR-C 2026-05-02.)
        """
        needle = _norm_for_substr(q)
        out: list[tuple[str, int, str]] = []
        for slug in _list_skills():
            text = _load_skill(slug)
            if text is None:
                continue
            count = _norm_for_substr(text).count(needle)
            if count == 0:
                continue
            preview = ""
            for line in text.splitlines():
                if needle in _norm_for_substr(line):
                    preview = line.strip()
                    break
            out.append((slug, count, preview))
        return out

    def _get_index(self) -> FileCorpusIndex | None:
        """Lazily build and return the skill embedding index.

        Returns ``None`` when no embedder is wired. The instance is
        cached on the handler so subsequent searches reuse the
        in-memory chunk vectors.
        """
        if self._index is not None:
            return self._index if self._index.is_available() else None
        # Use ``is not None`` explicitly: ``Hub`` defines ``__len__``
        # (= number of live kinds), so a hub with no kinds registered
        # yet — like the one used in unit tests that bypass
        # ``_register_with`` — would be falsy under a bare truthy
        # check and we'd silently skip the embedder lookup.
        embedder = getattr(self.hub, "embedder", None) if self.hub is not None else None
        if embedder is None:
            return None
        files: dict[str, str] = {}
        for slug in _list_skills():
            text = _load_skill(slug)
            if text is not None:
                files[slug] = text
        self._index = FileCorpusIndex(
            files=files,
            embedder=embedder,
            cache_namespace="skill_embeddings",
        )
        return self._index if self._index.is_available() else None

    def accepted_views(self, *, id: Any = None) -> list[str]:
        # Phase F 2026-05-31: per-kind view enum for the BadInput
        # envelope on unknown / empty ``view=`` values. Skills only
        # use ``view='toc'`` today (and only when paired with a
        # specific slug); the bare-slug get returns the markdown
        # body, no view kwarg required.
        return ["toc"]

    # ── smart-TOC + chunk rendering (Phase B integration) ─────────

    def _render_skill_toc(
        self, slug: str, *, scope: tuple[int, int] | None
    ) -> Response:
        """Render the smart-TOC for a skill, optionally scoped."""
        cache_key = (slug, scope)
        cached = self._toc_cache.get(cache_key)
        if cached is not None:
            return Response(body=cached)

        from precis.utils.toc import render_for_ref

        adapter = self.chunks_for_toc(slug)
        body = render_for_ref(
            ref_id=slug,
            slug=slug,
            kind="skill",
            adapter=adapter,
            scope=scope,
        )
        body += render_next_section(
            [
                (
                    f"get(kind='skill', id={slug!r})",
                    "read the skill's card + chunk overview",
                ),
                (
                    f"get(kind='skill', id='{slug}~0')",
                    "read a specific chunk (use any handle from above)",
                ),
            ]
        )
        # Memoise the fully-rendered body. Skill files are static
        # for the life of the process; subsequent calls return the
        # cached body without re-running DP+KeyBERT.
        self._toc_cache[cache_key] = body
        return Response(body=body)

    def _render_skill_chunks(self, slug: str, chunk_spec: tuple[int, int]) -> Response:
        """Render one or more H2 chunks of a skill (selector ``~N`` /
        ``~A..B``). Single-chunk requests show the H2 heading + the
        chunk body verbatim; range requests concatenate the body of
        every chunk in the range with H2 markers preserved."""
        from precis.skill_index.chunker import chunk_by_h2

        text = _load_skill(slug)
        if text is None:
            raise NotFound(
                f"skill {slug!r} not found",
                next="get(kind='skill') for the catalogue",
            )
        chunks = chunk_by_h2(text)
        lo, hi = chunk_spec
        if hi >= len(chunks) or lo < 0 or lo > hi:
            raise BadInput(
                f"skill {slug!r} has {len(chunks)} chunks; "
                f"range ~{lo}..{hi} is out of bounds",
                next=(f"get(kind='skill', id='{slug}/toc') for valid handles"),
            )

        # Single chunk: heading + body. Range: concatenate.
        if lo == hi:
            chunk = chunks[lo]
            header = f"# {slug}~{lo}"
            if chunk.heading:
                header += f" — {chunk.heading}"
            body = f"{header}\n\n{chunk.text}"
        else:
            parts = [f"# {slug}~{lo}..{hi}\n"]
            for i in range(lo, hi + 1):
                parts.append(chunks[i].text)
            body = "\n\n".join(parts)
        body += render_next_section(
            [
                (
                    f"get(kind='skill', id='{slug}/toc')",
                    "table of contents for this skill",
                ),
                (
                    f"get(kind='skill', id={slug!r})",
                    "skill card + chunk overview",
                ),
            ]
        )
        return Response(body=body)

    # ── chunks_for_toc adapter ─────────────────────────────────────

    def chunks_for_toc(self, ref: Any) -> Any:
        """Adapter for the generic TOC renderer.

        ``ref`` here is the skill slug (string) rather than a Ref
        object — skills are file-backed, not store-backed. Returns
        a :class:`ChunksForToc` whose chunks/H2 boundaries come
        from :func:`chunk_by_h2`; embeddings come from the embedded
        index when available, else ``None`` (the renderer falls
        back to H2 boundaries alone in that case).
        """
        from precis.skill_index.chunker import CHUNKER_VERSION, chunk_by_h2
        from precis.utils.toc import ChunksForToc

        slug = str(ref)
        text = _load_skill(slug)
        if text is None:
            raise NotFound(f"skill {slug!r} not found")

        chunks = chunk_by_h2(text)
        chunks_text = tuple(c.text for c in chunks)
        # Every chunk is its own H2 section; boundaries are 1:1
        # with chunk indices. Empty heading marks the head chunk
        # (content before the first H2) — we skip those entries
        # since the renderer's H2-coverage threshold would gate
        # them out anyway.
        h2_boundaries = tuple(
            (i, i, c.heading) for i, c in enumerate(chunks) if c.heading
        )

        # Try the embedded index for per-chunk vectors. When the
        # embedder isn't wired, fall through with ``embeddings=None``
        # and the renderer handles H2 fallback.
        embeddings: tuple[tuple[float, ...], ...] | None = None
        embedder_name = "none"
        index = self._get_index()
        if index is not None:
            try:
                index._build()
                entry = index._entries.get(slug) if index._entries else None
                if entry is not None:
                    # The index embeds body-only twins (v3) after the
                    # structural chunks; drop them so what's left aligns
                    # 1:1 with the structural-only ``chunk_by_h2`` above.
                    structural = [c for c in entry.chunks if not c.body_only]
                    if len(structural) == len(chunks):
                        embeddings = tuple(tuple(c.embedding) for c in structural)
                        embedder_name = entry.embedder_model
            except Exception:  # pragma: no cover — defensive
                embeddings = None

        return ChunksForToc(
            chunks_text=chunks_text,
            embeddings=embeddings,
            h2_boundaries=h2_boundaries,
            chunker_version=str(CHUNKER_VERSION),
            embedder_name=embedder_name,
            embedder=getattr(self.hub, "embedder", None) if self.hub else None,
        )

    # ── helpers ────────────────────────────────────────────────────

    def _render_index(self) -> Response:
        # Build the candidate set: synth meta-skills + every file-
        # backed skill that's currently available (i.e. its subject
        # kind is wired in this build). Filtered-out skills accumulate
        # into the trailing "Hidden" section.
        synth = list(self._SYNTHESIZED_SKILLS.keys())
        file_slugs = sorted(_list_skills())
        active: list[str] = list(synth)
        hidden_slugs: list[str] = []
        for slug in file_slugs:
            if _availability_gap(slug, hub=self.hub) is not None:
                hidden_slugs.append(slug)
                continue
            active.append(slug)

        groups, uncategorised = _categorise_skills(active)

        # Grammatical pluralisation in the headline — the MCP critic
        # flagged ``# 9 oracle(s)`` and ``# 22 skill(s)`` as
        # ungrammatical 2026-05-02; resolve here so the headline
        # reads naturally at any cardinality.
        total_active = sum(len(members) for _, members in groups) + len(uncategorised)
        skill_word = "skill" if total_active == 1 else "skills"
        lines = [f"# {total_active} {skill_word} (grouped by purpose)"]

        for category, slugs in groups:
            lines.append("")
            lines.append(f"## {category}")
            lines.append(
                render_agent_table(
                    [
                        {
                            "slug": _slug_with_aliases(slug),
                            "title": self._index_title_for(slug),
                        }
                        for slug in slugs
                    ],
                    schema=["slug", "title"],
                )
            )

        if uncategorised:
            lines.append("")
            lines.append(f"## Other ({len(uncategorised)})")
            lines.append(
                render_agent_table(
                    [
                        {
                            "slug": _slug_with_aliases(slug),
                            "title": self._index_title_for(slug),
                        }
                        for slug in uncategorised
                    ],
                    schema=["slug", "title"],
                )
            )

        # F17: the "Hidden" section (skills whose subject kind isn't
        # wired in this build) was useful as a developer / operator
        # audit but pure noise for the agent — those skills are
        # unreachable until configuration changes, which is not the
        # agent's concern. Dropped here; ``hidden_slugs`` stays
        # computed in case a future operator view wants to expose
        # it under a separate path.
        del hidden_slugs

        # Suggested starting commands — explicitly labelled as
        # examples rather than reusing the generic "Next:" trailer.
        # The picky reviewer (round 2) flagged the old phrasing as
        # ambiguous (the agent couldn't tell if these were new skills
        # or recipe shortcuts). Now they're named for what they are.
        lines.append("")
        lines.append("## Suggested starting commands")
        lines.append("")
        lines.append(
            "These are example invocations — paste verbatim to land somewhere useful."
        )
        lines.append("")
        lines.append(
            render_agent_table(
                [
                    {
                        "command": "get(kind='skill', id='precis-overview')",
                        "purpose": "orientation: seven verbs, one address scheme",
                    },
                    {
                        "command": "get(kind='skill', id='precis-help')",
                        "purpose": "active kinds + verbs on this server",
                    },
                    {
                        "command": "get(kind='skill', id='toc')",
                        "purpose": "table of contents with synopses",
                    },
                    {
                        "command": "search(kind='skill', q='your goal in plain language')",
                        "purpose": "fuzzy lookup by topic",
                    },
                ],
                schema=["command", "purpose"],
            )
        )
        return Response(body="\n".join(lines))

    def _index_title_for(self, slug: str) -> str:
        """Title to render next to ``slug`` in the index.

        Prefers the synth-skill description when ``slug`` is a synth;
        falls back to the markdown front-matter ``title:`` for file
        skills. Empty string when both miss so the TOON column stays
        well-formed.
        """
        synth_desc = self._SYNTHESIZED_SKILLS.get(slug)
        if synth_desc is not None:
            return synth_desc
        return _skill_title(slug) or ""

    def _render_toc(self) -> str:
        """Render the synthesised ``precis-toc`` (alias: ``toc``) skill.

        Lists every available skill with its title and a one-line
        synopsis pulled from the first paragraph after front-matter.
        Filtered the same way the index is — skills whose subject
        kind isn't wired or whose status is ``planned`` are listed
        in a separate "Hidden" section so an agent scanning the TOC
        sees the live set first.

        This is the embedding-search-poor-cousin: substring match
        on titles + summaries gets close enough for a 25-skill
        corpus, and the user can always
        ``search(kind='skill', q=...)`` for fuzzier lookup.
        """
        # Same category-driven layout as the index, but with the
        # synopsis column added so a skim-reading agent can decide
        # which slug to fetch in full.
        synth = list(self._SYNTHESIZED_SKILLS.keys())
        file_slugs = sorted(_list_skills())
        active: list[str] = list(synth)
        hidden: list[tuple[str, str]] = []
        for slug in file_slugs:
            if _availability_gap(slug, hub=self.hub) is not None:
                hidden.append((slug, _skill_title(slug) or slug))
            else:
                active.append(slug)

        groups, uncategorised = _categorise_skills(active)
        total_active = sum(len(members) for _, members in groups) + len(uncategorised)

        lines = [
            f"# precis-toc — {total_active} skills grouped by purpose",
            "",
        ]

        def _row_for(slug: str) -> dict[str, str]:
            synth_desc = self._SYNTHESIZED_SKILLS.get(slug)
            if synth_desc is not None:
                title = synth_desc
                synopsis = ""
            else:
                title = _skill_title(slug) or slug
                synopsis = _skill_synopsis(slug)
            return {
                "slug": _slug_with_aliases(slug),
                "title": title,
                "synopsis": synopsis,
            }

        for category, slugs in groups:
            lines.append(f"## {category} ({len(slugs)})")
            lines.append("")
            lines.append(
                render_agent_table(
                    [_row_for(s) for s in slugs],
                    schema=["slug", "title", "synopsis"],
                )
            )
            lines.append("")

        if uncategorised:
            lines.append(f"## Other ({len(uncategorised)})")
            lines.append("")
            lines.append(
                render_agent_table(
                    [_row_for(s) for s in uncategorised],
                    schema=["slug", "title", "synopsis"],
                )
            )
            lines.append("")

        # F17: hidden skills (subject kind not wired in this build)
        # dropped from the agent-facing list — unreachable from the
        # current configuration, so showing them just adds noise.
        del hidden

        # Same explicit "these are examples" framing as the index —
        # avoids the picky reviewer's complaint that the Next: trailer
        # blurred the line between commands and skills.
        lines.append("## Suggested starting commands")
        lines.append("")
        lines.append(
            "These are example invocations — paste verbatim to land somewhere useful."
        )
        lines.append("")
        lines.append(
            render_agent_table(
                [
                    {
                        "command": "search(kind='skill', q='your goal in plain language')",
                        "purpose": "fuzzy lookup by topic",
                    },
                    {
                        "command": "get(kind='skill', id='<slug>')",
                        "purpose": "fetch any skill from the table above",
                    },
                ],
                schema=["command", "purpose"],
            )
        )
        return "\n".join(lines)

    def _render_help(self) -> str:
        """Render the synthesized ``precis-help`` skill.

        Lists every kind currently active in this server's registry,
        grouped by class (state / cache / file / paid), with the verbs
        each supports. When the registry isn't wired (e.g. unit tests
        that build a SkillHandler directly), falls back to a static
        introduction.
        """
        lines = [
            "# precis-help",
            "",
            "What this server can do **right now** - active kinds",
            "and supported verbs, generated from the live hub.",
            "",
        ]
        if self.hub is None:
            lines.append(
                "_(hub not wired; see precis-overview for the canonical list.)_"
            )
            return "\n".join(lines)

        # Pull the canonical verb order from the protocol so this
        # renderer never goes stale when a new verb is added — every
        # kind's row reflects exactly what the live dispatch table
        # advertises via ``Hub.verbs_for(kind)``.
        rows: list[tuple[str, str, str]] = []  # (kind, verbs, desc)
        for kind in sorted(self.hub.kinds):
            handler = self.hub.handler_for(kind)
            spec = handler.spec
            live_verbs = self.hub.verbs_for(kind)
            ordered = [v for v in _ALL_VERBS if v in live_verbs]
            verb_str = " / ".join(ordered)
            desc = (spec.description or "").splitlines()[0] if spec.description else ""
            if len(desc) > 90:
                desc = desc[:87] + "…"
            rows.append((kind, verb_str, desc))

        if not rows:
            lines.append("_(no kinds available)_")
            return "\n".join(lines)

        lines.append(
            render_agent_table(
                [
                    {"kind": kind, "verbs": verbs, "description": desc}
                    for kind, verbs, desc in rows
                ],
                schema=["kind", "verbs", "description"],
            )
        )
        lines.append("")
        lines.append(
            f"**{len(rows)} kinds active.** "
            "For deeper docs on any kind, try "
            "`get(kind='skill', id='precis-<kind>-help')`."
        )
        return "\n".join(lines)

    def _render_status(self) -> str:
        """Render the synthesised ``precis-status`` skill.

        Four sections:

        1. **Build** — version + git/build metadata baked into the
           image at ``docker build`` time via env vars from
           ``scripts/build-image``. Surfaces ``"unknown"`` when the
           image was built without the build-args (so the response
           still answers "what is this build" honestly).
        2. **Runtime** — live process facts: container hostname, OS
           platform, python version, pid, uptime.
        3. **Database** — connected DB host/port/name/user, server
           version, and the highest applied migration. Renders an
           ``unreachable`` line instead of crashing when the DB is
           down — this is the surface you hit *because* something is
           wrong.
        4. **Optional dependencies** — original import probe table.

        Pure introspection. The DB read is a single round-trip;
        everything else is in-process.
        """
        lines = [
            "# precis-status",
            "",
            "Build + runtime + DB + optional-dependency health probe.",
            "",
            "**Build**",
            "",
            render_agent_table(
                [
                    {"field": field, "value": value}
                    for field, value in _collect_build_info()
                ],
                schema=["field", "value"],
            ),
            "",
            "**Runtime**",
            "",
            render_agent_table(
                [
                    {"field": field, "value": value}
                    for field, value in _collect_runtime_info()
                ],
                schema=["field", "value"],
            ),
            "",
            "**Database**",
            "",
        ]
        store = getattr(self.hub, "store", None) if self.hub is not None else None
        db_info = _collect_database_info(store)
        if isinstance(db_info, str):
            lines.append(f"_{db_info}_")
        else:
            lines.append(
                render_agent_table(
                    [{"field": field, "value": value} for field, value in db_info],
                    schema=["field", "value"],
                )
            )
        lines.extend(
            [
                "",
                "**Optional dependencies**",
                "",
                "Each row tags the Python module that backs a precis "
                "kind or affordance, and reports whether it imports "
                "cleanly in this venv.",
                "",
            ]
        )

        rows: list[tuple[str, str, str, str]] = []  # (label, status, kind, hint)
        worst = "OK"
        for module, label, backs, hint in _OPTIONAL_DEP_PROBES:
            try:
                mod = importlib.import_module(module)
            except ImportError:
                rows.append((label, "MISSING", backs, hint))
                worst = "DEGRADED"
                continue
            except Exception as exc:  # pragma: no cover — import-side bug
                rows.append((label, f"ERROR: {exc}", backs, hint))
                worst = "DEGRADED"
                continue
            version = getattr(mod, "__version__", None) or "(unknown)"
            rows.append((label, f"OK {version}", backs, ""))

        lines.append(
            render_agent_table(
                [
                    {
                        "module": label,
                        "status": status,
                        "backs": backs,
                        "install_hint": hint if not status.startswith("OK") else "",
                    }
                    for label, status, backs, hint in rows
                ],
                schema=["module", "status", "backs", "install_hint"],
            )
        )
        lines.append("")
        lines.append(f"**Overall: {worst}**")
        if worst == "DEGRADED":
            lines.append(
                "\nMissing entries above mean the listed kinds will "
                "raise at runtime even though `tools/list` advertises "
                "them.  Install the missing extra and restart the MCP."
            )

        # Embedder + store are deeper than a bare import probe, but
        # cheap to surface from the bound hub when wired.
        if self.hub is not None:
            try:
                kinds = sorted(self.hub.kinds)
            except Exception:  # pragma: no cover
                kinds = []
            if kinds:
                lines.append("")
                lines.append(
                    f"**Registered kinds ({len(kinds)}):** " + ", ".join(kinds)
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Build / runtime / DB collectors — used by ``precis-status``
# ---------------------------------------------------------------------------


#: Process start time. Captured at module import so ``uptime_seconds``
#: in ``precis-status`` reflects how long the server has been live.
_STARTED_AT: datetime = datetime.now(UTC)


def _live_git_info() -> dict[str, str]:
    """Read the git state of the *source tree the running code loaded from*.

    Shells ``git`` against the directory holding ``precis/__init__.py``
    (``Path(precis.__file__).parent``). Returns ``{}`` when that dir is
    not inside a git checkout — an installed wheel in ``site-packages``,
    or a host without the ``git`` binary — so a baked Docker image (which
    answers via :data:`_BUILD_ENV_KEYS` env vars instead) is unaffected.

    Complements the build-time env vars: those describe the *image*, this
    describes a *live checkout* (local dev, an editable install, or the
    cluster's ``uv``/``pip``-from-git checkout — none of which run
    ``scripts/build-image``). Keys match the :data:`_BUILD_ENV_KEYS`
    labels so :func:`_collect_build_info` can fall back field-by-field.

    Every ``git`` call is wrapped: this must never raise at import time.
    """
    import precis

    src = Path(precis.__file__).resolve().parent

    def _git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(src), *args],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    top = _git("rev-parse", "--show-toplevel")
    if top is None:
        # Not a git checkout (installed wheel) or no git binary — let the
        # baked env vars answer, or render "unknown" honestly.
        return {}

    info: dict[str, str] = {"source_path": top}
    if (sha := _git("rev-parse", "HEAD")) is not None:
        info["git_sha"] = sha
        info["git_sha_short"] = sha[:12]
    if (branch := _git("rev-parse", "--abbrev-ref", "HEAD")) is not None:
        info["git_branch"] = branch
    if (describe := _git("describe", "--tags", "--always", "--dirty")) is not None:
        info["git_describe"] = describe
    if (last_tag := _git("describe", "--tags", "--abbrev=0")) is not None:
        info["git_last_tag"] = last_tag
    # ``rev-parse --show-toplevel`` already succeeded, so git works here:
    # an empty ``status --porcelain`` means a genuinely clean tree.
    info["git_dirty"] = "true" if _git("status", "--porcelain") else "false"
    return info


#: Live git state of the running source tree, **frozen at process start**
#: (module import). Freezing is deliberate: ``precis-status`` answers "which
#: commit is *this process* running", not "what does the checkout say now".
#: If the checkout is later moved ahead (``git pull`` / a ship reset) without
#: restarting, this stays at the loaded sha — so the drift is *visible* and
#: the reader knows to restart, rather than a fresh on-demand read falsely
#: reporting "current". Mirrors how the baked env vars freeze at build time.
_SOURCE_GIT_INFO: dict[str, str] = _live_git_info()


#: Build-metadata env vars baked into the image by ``scripts/build-image``.
#: Each entry is ``(env name, display label)``. Order is the render order.
#: Unset vars fall through to the literal string ``"unknown"`` — the
#: status response surfaces the gap honestly rather than pretending.
_BUILD_ENV_KEYS: tuple[tuple[str, str], ...] = (
    ("PRECIS_GIT_LAST_TAG", "git_last_tag"),
    ("PRECIS_GIT_SHA", "git_sha"),
    ("PRECIS_GIT_SHA_SHORT", "git_sha_short"),
    ("PRECIS_GIT_DIRTY", "git_dirty"),
    ("PRECIS_GIT_DESCRIBE", "git_describe"),
    ("PRECIS_GIT_BRANCH", "git_branch"),
    ("PRECIS_BUILD_TIME", "build_time"),
    ("PRECIS_BUILD_HOST", "build_host"),
    ("PRECIS_BUILD_USER", "build_user"),
)


def _collect_build_info() -> list[tuple[str, str]]:
    """Return ``(field, value)`` rows for the **Build** section.

    Sources, in precedence order per git field:

    1. The build-time env vars in :data:`_BUILD_ENV_KEYS` (baked into a
       Docker image by ``scripts/build-image``) — the identity of a
       *built image*.
    2. Otherwise :data:`_SOURCE_GIT_INFO` — the git state of the *live
       checkout* the code loaded from, frozen at process start. Covers
       local dev, editable installs, and the cluster's from-git checkouts,
       none of which run ``scripts/build-image``.
    3. Otherwise the literal ``"unknown"`` — a bare source tree with no
       git and no build-args still produces a well-formed response.

    Also emits ``source_path`` (the on-disk checkout the process is
    running, when known) and ``git_source`` — ``image-build`` /
    ``working-tree`` / ``unknown`` — so the reader knows which lane the
    git facts came from and can tell a live checkout from a frozen image.
    """
    from precis import __version__

    rows: list[tuple[str, str]] = [("version", __version__)]
    for env_name, label in _BUILD_ENV_KEYS:
        baked = os.environ.get(env_name)
        if baked:
            rows.append((label, baked))
        else:
            rows.append((label, _SOURCE_GIT_INFO.get(label, "unknown")))

    if os.environ.get("PRECIS_GIT_SHA"):
        git_source = "image-build"
    elif _SOURCE_GIT_INFO:
        git_source = "working-tree"
    else:
        git_source = "unknown"
    rows.append(("git_source", git_source))
    rows.append(("source_path", _SOURCE_GIT_INFO.get("source_path", "unknown")))
    return rows


def _collect_runtime_info() -> list[tuple[str, str]]:
    """Return ``(field, value)`` rows for the **Runtime** section.

    Pure introspection of the live process — hostname, platform,
    python version, pid, and uptime since :data:`_STARTED_AT`.
    """
    uptime = int((datetime.now(UTC) - _STARTED_AT).total_seconds())
    return [
        ("hostname", socket.gethostname()),
        ("platform", platform.platform()),
        ("python", sys.version.split()[0]),
        ("pid", str(os.getpid())),
        ("cwd", os.getcwd()),
        ("started_at", _STARTED_AT.isoformat(timespec="seconds")),
        ("uptime_seconds", str(uptime)),
    ]


def _collect_database_info(store: Any) -> list[tuple[str, str]] | str:
    """Return DB facts as rows, or a one-line error string.

    On stateless builds (``store is None``) returns a sentinel string.
    On reachable DBs returns rows: ``dsn_host`` / ``dsn_port`` (parsed
    from ``store.dsn`` — password component is never read), then
    ``current_database()``, ``current_user``, ``version()``, and the
    last applied migration version + count from ``public._migrations``.

    Wraps the DB roundtrip in a broad ``except`` — this is the *first*
    surface called when something is wrong, so it must not die when the
    DB is the thing wrong. Returns ``"unreachable: <type>: <msg>"`` so
    the operator sees the failure mode without a traceback.
    """
    if store is None:
        return "stateless build — no DB configured"

    rows: list[tuple[str, str]] = []
    if getattr(store, "dsn", None):
        try:
            parsed = urlparse(store.dsn)
            if parsed.hostname:
                rows.append(("dsn_host", parsed.hostname))
            if parsed.port:
                rows.append(("dsn_port", str(parsed.port)))
        except ValueError:
            # Malformed DSN — skip the parsed fields; the live SQL
            # round-trip below still works because psycopg parses
            # independently.
            pass

    try:
        with store.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_user, version(),"
                " (SELECT max(version) FROM public._migrations),"
                " (SELECT count(*) FROM public._migrations)"
            )
            row = cur.fetchone()
    except Exception as exc:
        # Broad except is intentional: this is the *first* surface
        # called when something is wrong. A traceback here masks the
        # real signal; render the failure mode in-band instead.
        return f"unreachable: {type(exc).__name__}: {exc}"

    if row is None:
        return "DB returned no rows"
    db_name, db_user, server_version, migration, mig_count = row
    rows.append(("name", str(db_name)))
    rows.append(("user", str(db_user)))
    # ``SELECT version()`` returns a multi-comma string ("PostgreSQL
    # 16.4 on x86_64-pc-linux-gnu, compiled by gcc …"); keep the
    # leading clause for readability.
    rows.append(("server_version", str(server_version).split(",", 1)[0]))
    rows.append(("migration", str(migration) if migration is not None else "(none)"))
    rows.append(("migration_count", str(mig_count)))
    return rows


# ---------------------------------------------------------------------------
# Optional-dep probe table — used by ``precis-status``
# ---------------------------------------------------------------------------

#: ``(import name, display label, kinds it backs, pip install hint)``.
#:
#: Adding a new probe is one row.  Keep the import name precise
#: (the exact ``importlib.import_module`` argument) and the install
#: hint copy-pasteable.
_OPTIONAL_DEP_PROBES: tuple[tuple[str, str, str, str], ...] = (
    (
        "sentence_transformers",
        "sentence-transformers",
        "paper / markdown / patent semantic search",
        "pip install 'precis-mcp[paper]'",
    ),
    (
        "sympy",
        "sympy",
        "calc",
        "pip install 'precis-mcp[calc]'",
    ),
    (
        "wolframalpha",
        "wolframalpha",
        "math",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "youtube_transcript_api",
        "youtube-transcript-api",
        "youtube",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "httpx",
        "httpx",
        "web / perplexity (websearch / perplexity-reasoning / perplexity-research)",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "trafilatura",
        "trafilatura",
        "web (page → markdown extraction)",
        "pip install 'precis-mcp[external]'",
    ),
    (
        "docx",
        "python-docx",
        "docx file kind",
        "pip install 'precis-mcp[docx]'",
    ),
    (
        "lxml",
        "lxml",
        "tex / patent / docx XML parsing",
        "pip install 'precis-mcp[tex]' or [docx] or [patent]",
    ),
    (
        "epo_ops",
        "python-epo-ops-client",
        "patent (EPO OPS biblio + claims)",
        "pip install 'precis-mcp[patent]'",
    ),
    (
        "matplotlib",
        "matplotlib",
        "plot kind (declarative renderer)",
        "pip install 'precis-mcp[plot]'",
    ),
)


# ---------------------------------------------------------------------------
# Substring normalisation (hyphen ↔ space fold)
# ---------------------------------------------------------------------------


# Pre-compiled regex for collapsing runs of whitespace + hyphens into a
# single space, so the substring match treats ``spaced repetition``,
# ``spaced-repetition``, ``spaced - repetition`` and ``spaced  repetition``
# as the same query target. Compiling at module level keeps the per-call
# allocation cost off the hot path. (MCP critic MAJOR-C 2026-05-02.)
_NORM_HYPHEN_WS_RE = re.compile(r"[\s\-]+")


def _norm_for_substr(s: str) -> str:
    """Lower-case and collapse hyphen / whitespace runs to one space.

    Used by the skill substring search so a 7B caller's natural
    phrasing finds the hyphenated form a corpus author wrote (and
    vice versa). Keeps no allocation cost when the input has no
    hyphens or runs of whitespace — the regex is a no-op then.
    """
    return _NORM_HYPHEN_WS_RE.sub(" ", s.lower()).strip()


_FM_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _skill_title_text(slug: str) -> str:
    """The skill's front-matter ``title:`` + its first ``# H1`` line.

    These carry the skill's stated purpose in near-natural phrasing
    (``precis — how do I cite a paper?``). A query that *is* a substring
    of this text is an almost-certain intent match, so the search boosts
    it (see :meth:`SkillHandler.search`). Returns ``""`` for an unknown
    slug or a skill with neither field.
    """
    text = _load_skill(slug)
    if text is None:
        return ""
    parts: list[str] = []
    fm = _FM_TITLE_RE.search(text)
    if fm:
        parts.append(fm.group(1))
    h1 = _H1_RE.search(text)
    if h1:
        parts.append(h1.group(1))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# File access (importlib.resources keeps this working from a wheel)
# ---------------------------------------------------------------------------


#: Process-wide cache of ``{slug → raw markdown body}``. First call
#: to :func:`_load_skills_map` populates it; subsequent calls reuse
#: it. Tests that mutate the on-disk skill corpus must call
#: :func:`_load_skills_map_cache_clear` to force a re-scan.
_SKILLS_MAP_CACHE: dict[str, str] | None = None


def _load_skills_map_cache_clear() -> None:
    """Drop the cached ``{slug → raw}`` map. Use in tests after edits."""
    global _SKILLS_MAP_CACHE
    _SKILLS_MAP_CACHE = None


#: Entry-point group third-party packages use to contribute skill
#: roots, mirroring ``precis.handlers`` (``dispatch._load_plugins``).
#: Each entry-point value is a package path holding ``*.md`` skill
#: files, e.g. ``precis_chain = "precis_chain.data.skills"``. Built-in
#: skills load first and win any slug collision.
SKILL_PLUGIN_GROUP = "precis.skills"


def _walk_skill_root(node: Any, out: dict[str, str]) -> None:
    """Add every ``*.md`` skill under ``node`` to ``out`` (first writer
    wins, so built-ins beat plugins on a slug clash)."""
    for entry in node.iterdir():
        name = entry.name
        if name.startswith("__"):
            continue  # skip __pycache__, __init__.py, etc.
        if entry.is_dir():
            _walk_skill_root(entry, out)
            continue
        if not name.endswith(".md"):
            continue
        stem = name[:-3]
        if not _SLUG_RE.match(stem) or stem in out:
            continue
        try:
            out[stem] = entry.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError) as exc:
            log.warning("could not read skill %s: %s", stem, exc)


def _plugin_skill_roots() -> list[Any]:
    """Resolve skill roots contributed via the ``precis.skills``
    entry-point group. Failures are logged, never raised — one broken
    plugin must not brick the skill surface (mirrors ``_load_plugins``)."""
    roots: list[Any] = []
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group=SKILL_PLUGIN_GROUP)
    except Exception as exc:  # defensive — importlib surface is stable
        log.warning("precis.skills discovery failed: %s", exc)
        return roots
    for ep in eps:
        try:
            roots.append(resources.files(ep.value))
        except Exception as exc:
            log.warning(
                "precis.skills plugin %r (%s) failed: %s", ep.name, ep.value, exc
            )
    return roots


def _load_skills_map() -> dict[str, str]:
    """Return ``{slug → raw markdown body}`` for every shipped skill.

    Walks ``src/precis/data/skills/`` recursively so subdirectory
    organisation (``personas/``, ``refs/``, …) is invisible to callers
    — every ``*.md`` whose stem matches :data:`_SLUG_RE` is reachable
    via :func:`_load_skill` regardless of its directory. Then walks any
    skill roots contributed by third-party packages via the
    ``precis.skills`` entry-point group (built-ins win slug collisions).

    Cached on first call. Process-wide; restart the server to pick
    up new files on disk in production. Tests that mutate the corpus
    call :func:`_load_skills_map_cache_clear`.
    """
    global _SKILLS_MAP_CACHE
    if _SKILLS_MAP_CACHE is not None:
        return _SKILLS_MAP_CACHE

    out: dict[str, str] = {}
    try:
        root = resources.files("precis.data.skills")
    except (ModuleNotFoundError, FileNotFoundError):
        log.warning("precis.data.skills package missing")
    else:
        _walk_skill_root(root, out)

    for proot in _plugin_skill_roots():
        _walk_skill_root(proot, out)

    _SKILLS_MAP_CACHE = out
    return out


def _list_skills() -> list[str]:
    """Return all available skill slugs (without the ``.md`` suffix).

    Includes slugs from any subdirectory under
    ``src/precis/data/skills/`` (personas/, refs/, etc.).
    """
    return list(_load_skills_map().keys())


def _load_skill(slug: str) -> str | None:
    """Return the skill body with ``{{include doc:…}}`` directives resolved.

    Returns ``None`` if the slug is unknown. Frontmatter is preserved
    through expansion (includes only live in body content), so
    callers that parse frontmatter on the result continue to work.

    Includes that fail to resolve are **logged and the raw content
    is returned** so the skill stays viewable rather than disappearing
    on a directive typo. The strict "fail ingest on broken include"
    gate (per docs-and-skills-redesign.md decision 10) lives in the
    boot-time scanner (``precis.ingest.skill_ingest``); SkillHandler
    is the lenient runtime path.
    """
    skills = _load_skills_map()
    text = skills.get(slug)
    if text is None:
        return None
    # Fast path — most skills don't carry include directives.
    if "{{include" not in text:
        return text
    # Lazy import to avoid pulling the ingest module on every
    # ``import skill`` (and to keep the import graph acyclic if the
    # ingest layer ever wants to import from handlers).
    from precis.ingest.skill_template import DocResolver, IncludeError, Includer

    includer = Includer(resolvers={"doc": DocResolver(docs=skills)})
    try:
        return includer.expand(text)
    except IncludeError as exc:
        log.warning("skill %r: include expansion failed: %s; serving raw", slug, exc)
        return text


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the YAML-style front-matter as a flat dict of strings.

    We don't pull in PyYAML for this — the front-matter we use is
    strictly key:value pairs. Lists and nested dicts aren't supported,
    which is fine because the only fields we read here are scalars
    (``status``, ``applies-to``, ``title``).
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip().lower()] = val.strip().strip("\"'")
    return out


#: Match ``kind='X'`` (or ``kind="X"``) inside the front-matter
#: ``applies-to:`` line. Power-user skills like ``precis-patent-power``
#: don't follow the ``precis-<kind>-help`` naming pattern but their
#: front-matter still names the kind via ``applies-to: search
#: (kind='patent', q=<CQL>)``. Parsing this lets the gate hide the
#: skill alongside its ``-help`` sibling when the kind is missing.
_APPLIES_TO_KIND_RE = re.compile(r"""kind\s*=\s*['"]([a-z][a-z0-9_-]*)['"]""")

# Slug stems that look like ``precis-<X>-help`` but X is *not* a kind.
# Without this set, ``precis-get-help`` derives ``'get'`` from its slug,
# the availability gate finds no kind ``'get'`` in the hub, and every
# verb-help skill gets a misleading "kind='get' not wired" banner —
# the same banner cascades into the TOC's Hidden section. We treat the
# seven verbs as non-kinds explicitly so the slug-derived fallback
# only fires for genuine kind-help skills (``precis-markdown-help``,
# ``precis-paper-help``, etc.) where the gate is actually meaningful.
# Broad-pass usability finding 2026-05-30 (#1 + #2).
_NON_KIND_SLUG_STEMS = frozenset(
    {"get", "search", "put", "edit", "delete", "tag", "link"}
)


def _kinds_referenced_by_skill(slug: str, fm: dict[str, str]) -> list[str]:
    """Return every kind the skill claims to apply to.

    Two sources, in priority order:
      1. Front-matter ``applies-to:`` — extract every ``kind='X'``.
      2. Slug suffix ``precis-<kind>-help`` — derived as a fallback
         so existing ``-help`` skills without explicit front-matter
         still gate correctly. Slugs whose stem names a *verb*
         (``precis-get-help`` etc.) are not treated as kind-targeted
         — see ``_NON_KIND_SLUG_STEMS``.

    Returns an empty list for cross-cutting skills (``precis-overview``,
    ``precis-tags``, …) that don't reference any specific kind.
    """
    kinds: list[str] = []
    applies = fm.get("applies-to") or fm.get("applies_to") or ""
    if applies:
        kinds.extend(_APPLIES_TO_KIND_RE.findall(applies))
    # Slug-derived fallback, only when front-matter didn't name any
    # kinds.  When the slug names an umbrella concept
    # (``precis-perplexity-help``) while the front-matter pins the
    # concrete kinds (``websearch`` / ``think`` / ``research``),
    # treating the slug-derived string as authoritative produces a
    # false "kind=perplexity not wired" banner on a valid skill.
    # (MCP critic MINOR-C — skill-availability gate false-positive.)
    if not kinds and slug.startswith("precis-") and slug.endswith("-help"):
        derived = slug[len("precis-") : -len("-help")]
        if derived and derived not in _NON_KIND_SLUG_STEMS:
            kinds.append(derived)
    return kinds


def _availability_gap(slug: str, *, hub: Any) -> str | None:
    """Return a human-readable reason why this skill is filtered, or None.

    Two gates:

    1. Subject-kind gate. The skill names a kind via the slug
       (``precis-<kind>-help``) or the ``applies-to:`` front-matter
       (``kind='X'``). When *any* referenced kind is missing from the
       hub, the skill is filtered — the recipes don't all run.
       Power-user companions like ``precis-patent-power`` flow through
       the front-matter side of this gate.

       **Verb-help skills are exempt from this gate** (slug stem in
       ``_NON_KIND_SLUG_STEMS``). They document a verb that applies
       to every kind that supports it; their ``applies-to:``
       frontmatter often lists file kinds for examples, and those
       being unwired in the current build is *exactly* the situation
       the skill is meant to teach you about — flagging it as "kind
       not wired" misclassifies the skill and hides it from the
       index. Round-2 picky F-5, 2026-05-30.

    2. Status gate. Front-matter ``status:`` of ``planned`` or
       ``aspirational`` flags the skill as "describes a future API,
       don't follow recipes blind". Filtered.

    Cross-cutting skills (``precis-overview``, ``precis-tags``, …)
    reference no kind and pass gate 1 automatically. They're still
    subject to gate 2.

    Returns ``None`` if the skill is fully available.
    """
    if slug in SkillHandler._SYNTHESIZED_SKILLS:
        return None

    text = _load_skill(slug)
    if text is None:
        return None  # caller handles 'not found'

    fm = _parse_frontmatter(text)
    status = fm.get("status", "active").lower()
    if status in ("planned", "aspirational"):
        return (
            f"this skill is marked status: {status} - its examples "
            "describe a planned API, not the live runtime. Treat as "
            "design notes, not as recipes."
        )

    # Verb-help exemption from the subject-kind gate.
    if _is_verb_help_slug(slug):
        return None

    if hub is not None:
        for kind in _kinds_referenced_by_skill(slug, fm):
            # Only fire the banner if ``kind`` is a name the registry
            # *recognises* (currently active, or known-but-disabled in
            # ``loadabilities``). Slug-derived strings that don't map
            # to any kind — like ``precis-files-help`` whose stem
            # ``files`` is an umbrella concept for markdown/plaintext/
            # tex/python rather than a real kind — would otherwise
            # produce a misleading "kind='files' not wired" banner
            # pointing at a NotFound an agent can't recover from
            # (round-2 picky R2-3, 2026-05-30).
            if not _kind_is_known(hub, kind):
                continue
            if not _hub_has_kind(hub, kind):
                return (
                    f"this skill documents kind={kind!r} which is **not "
                    "wired** in this build - its examples will return "
                    "[error:Unsupported] for missing env vars or "
                    "[error:NotFound] for genuinely unknown kinds."
                )

    return None


def _kind_is_known(hub: Any, kind: str) -> bool:
    """True if ``kind`` is a name the registry recognises in any state.

    A name is "known" if it's currently loaded (in ``hub.kinds``) or
    appears in the loadability map (registered-but-disabled). Names
    that fall in neither bucket — umbrella concepts like ``files``,
    typos, third-party plugin slugs — are unknown to the gate and
    must not trigger a kind-not-wired banner.
    """
    if _hub_has_kind(hub, kind):
        return True
    try:
        loadabilities = hub.loadabilities
    except (AttributeError, KeyError):
        return False
    return kind in loadabilities


def _is_verb_help_slug(slug: str) -> bool:
    """True if ``slug`` is ``precis-<verb>-help`` for a known verb."""
    if not (slug.startswith("precis-") and slug.endswith("-help")):
        return False
    stem = slug[len("precis-") : -len("-help")]
    return stem in _NON_KIND_SLUG_STEMS


def _hub_has_kind(hub: Any, kind: str) -> bool:
    """Best-effort hub membership check.

    We access the hub through duck-typing because the SkillHandler
    may be wired up against a fake hub in tests. Treat any
    AttributeError or KeyError as "kind not registered" so the
    filter stays conservative rather than crashing the index.
    """
    try:
        kinds = hub.kinds
        # ``dispatch.Hub`` exposes ``kinds`` as a set property;
        # older / fake shapes may use a method. Accept both.
        if callable(kinds):
            kinds = kinds()
    except (AttributeError, KeyError):
        return False
    return kind in kinds


def _skill_synopsis(slug: str) -> str:
    """Pull a one-line synopsis from a skill's body.

    Strategy:
      1. First non-blank line after the front-matter that isn't an
         H1/H2 header, blockquote, or HTML comment.
      2. Trim to ~140 chars.
      3. Strip surrounding whitespace and trailing punctuation.

    Empty string if nothing usable. Used by the ``precis-toc``
    renderer so each TOC entry carries a sentence's worth of context
    without committing to the full skill body.
    """
    text = _load_skill(slug)
    if text is None:
        return ""
    body = text
    # Skip front-matter.
    if body.startswith("---"):
        end = body.find("\n---", 3)
        if end != -1:
            body = body[end + 4 :]
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", ">", "<!--", "---", "|")):
            continue
        if len(line) > 140:
            line = line[:137].rstrip() + "…"
        return line
    return ""


def _skill_title(slug: str) -> str:
    """Pull the YAML front-matter ``title:`` field from a skill, if any.

    Falls back to the first ``# H1`` line, then to an empty string.
    """
    text = _load_skill(slug)
    if text is None:
        return ""
    # Front-matter block.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                line = line.strip()
                if line.lower().startswith("title:"):
                    return line.split(":", 1)[1].strip().strip("\"'")
    # First H1.
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""
