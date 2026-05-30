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
import re
from importlib import resources
from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.protocol import _ALL_VERBS, Handler, KindSpec
from precis.response import Response
from precis.skill_index import FileCorpusIndex, SearchHit
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline

log = logging.getLogger(__name__)


# Slugs are conservative: lowercase ASCII alphanumerics + hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")


class _SkillSearchRow:
    """Per-slug merged hit for the search response.

    Pure presentation glue — the search method builds these from
    semantic + lexical streams, deduplicates by slug (keeping the
    higher-scoring source), then renders them in score order. Kept
    as a plain class instead of a dataclass to avoid pulling extra
    decorators in the hot path of a search call.
    """

    __slots__ = ("preview", "score", "slug", "source")

    def __init__(self, *, slug: str, score: float, source: str, preview: str) -> None:
        self.slug = slug
        self.score = score
        self.source = source
        self.preview = preview


def _format_semantic_preview(hit: SearchHit) -> str:
    """Render a semantic hit as ``score · heading\\n  snippet``."""
    head = hit.heading or "—"
    snippet = hit.snippet or ""
    score_str = f"{hit.score:.2f}"
    if snippet:
        return f"{score_str} · {head}\n  {snippet}"
    return f"{score_str} · {head}"


def _format_lexical_preview(preview: str, count: int) -> str:
    """Render a substring-match preview, truncating long lines."""
    short = (preview[:120] + "…") if len(preview) > 120 else preview
    word = "hit" if count == 1 else "hits"
    return f"{count} {word}\n  {short}" if short else f"{count} {word}"


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
        "precis-status": ("optional dependencies + runtime health probe"),
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

    # ── get ────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.startswith("/")):
            return self._render_index()

        slug = str(id).strip()
        if not _SLUG_RE.match(slug):
            raise BadInput(
                f"invalid skill slug: {slug!r}",
                next="skill slugs are lowercase letters/digits/hyphens",
            )

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
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='skill', q='your query')",
            )

        # Two-stream search: cosine over chunk embeddings (best at
        # natural phrasing) merged with substring matches (best at
        # literal slugs / kind names / verbatim quotes). Each stream
        # contributes hits; per-slug we keep the better-scoring one
        # so ranking stays sharp. The index is silently unavailable
        # on builds without an embedder — substring carries on alone.
        semantic_hits = self._semantic_hits(q, top_k=top_k * 2)
        lexical_hits = self._lexical_hits(q)

        merged: dict[str, _SkillSearchRow] = {}
        for hit in semantic_hits:
            row = _SkillSearchRow(
                slug=hit.slug,
                score=hit.score,
                source="semantic",
                preview=_format_semantic_preview(hit),
            )
            existing = merged.get(hit.slug)
            if existing is None or row.score > existing.score:
                merged[hit.slug] = row
        for slug, count, preview in lexical_hits:
            # Substring counts are coerced to a [0.0, 0.5) score so
            # they rank below any genuine semantic hit but still
            # show up when the index missed (or returned zero).
            lex_score = min(0.49, count / 20.0)
            existing = merged.get(slug)
            if existing is None:
                merged[slug] = _SkillSearchRow(
                    slug=slug,
                    score=lex_score,
                    source="lexical",
                    preview=_format_lexical_preview(preview, count),
                )

        if not merged:
            return Response(body=f"no skills mention {q!r}")

        rows = sorted(merged.values(), key=lambda r: r.score, reverse=True)
        total = len(rows)
        rows = rows[:top_k]

        lines = [
            format_search_headline(
                n_returned=len(rows),
                total=total,
                noun="skill match",
                query=q,
            )
        ]
        for row in rows:
            # Mark skills whose subject kind isn't in the live
            # registry (or whose status is planned/aspirational).
            # The index view already hides them; search has to do
            # the same honesty work or 7B callers will quote the
            # title and invoke an [error:NotFound] kind. (MCP critic
            # MINOR — search surfaces unwired skills without marker.)
            gap = _availability_gap(row.slug, hub=self.hub)
            marker = " [unwired]" if gap is not None else ""
            lines.append(f"\n## {row.slug}{marker}  ({row.source})\n{row.preview}")
        return Response(body="\n".join(lines))

    # ── search helpers ─────────────────────────────────────────────

    def _semantic_hits(self, q: str, *, top_k: int) -> list[SearchHit]:
        """Return cosine-ranked chunk hits, or ``[]`` when no embedder."""
        index = self._get_index()
        if index is None:
            return []
        return index.search(q, top_k=top_k)

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

    # ── helpers ────────────────────────────────────────────────────

    def _render_index(self) -> Response:
        skills = sorted(_list_skills())
        # Surface every synthesised meta-skill at the top so they're
        # the first things the agent sees, in registration order.
        index_entries: list[tuple[str, str]] = [
            (slug, desc) for slug, desc in self._SYNTHESIZED_SKILLS.items()
        ]
        hidden_slugs: list[str] = []
        for slug in skills:
            # Filter skills whose subject kind isn't in the registry
            # or whose front-matter status isn't ``active``. They
            # remain reachable via direct slug get(), but they don't
            # clutter the index that agents use for discovery.
            if _availability_gap(slug, hub=self.hub) is not None:
                hidden_slugs.append(slug)
                continue
            title = _skill_title(slug)
            index_entries.append((slug, title))

        # Grammatical pluralisation in the headline — the MCP critic
        # flagged ``# 9 oracle(s)`` and ``# 22 skill(s)`` as
        # ungrammatical 2026-05-02; resolve here so the headline
        # reads naturally at any cardinality.
        skill_word = "skill" if len(index_entries) == 1 else "skills"
        lines = [f"# {len(index_entries)} {skill_word} available"]
        for slug, title in index_entries:
            if title:
                lines.append(f"  {slug:<32}  {title}")
            else:
                lines.append(f"  {slug}")
        if hidden_slugs:
            lines.append("")
            lines.append(
                f"_(+ {len(hidden_slugs)} non-active skills hidden - "
                "documenting kinds not wired in this build, or marked "
                "status: planned. Reach them by exact slug if you need "
                "to.)_"
            )
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    "get(kind='skill', id='toc')",
                    "table of contents - every skill with a one-liner",
                ),
                (
                    "get(kind='skill', id='precis-help')",
                    "what this server can do (active kinds)",
                ),
                (
                    "get(kind='skill', id='precis-status')",
                    "optional-deps + runtime health",
                ),
                ("get(kind='skill', id='precis-overview')", "start here"),
                (
                    "get(kind='skill', id='precis-tags')",
                    "tag axes + validation rules",
                ),
                (
                    "search(kind='skill', q='...')",
                    "search across all skills",
                ),
            ]
        )
        return Response(body=body)

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
        lines = ["# precis-toc — every skill at a glance", ""]
        skills = sorted(_list_skills())

        active: list[tuple[str, str, str]] = []  # slug, title, synopsis
        hidden: list[tuple[str, str, str]] = []
        for slug in skills:
            title = _skill_title(slug) or slug
            synopsis = _skill_synopsis(slug)
            row = (slug, title, synopsis)
            if _availability_gap(slug, hub=self.hub) is not None:
                hidden.append(row)
            else:
                active.append(row)

        # Synth meta-skills go first — they're the discovery
        # primitives an agent uses to navigate the TOC itself.
        if self._SYNTHESIZED_SKILLS:
            lines.append("## Meta-skills (synthesised)")
            lines.append("")
            for slug, desc in self._SYNTHESIZED_SKILLS.items():
                lines.append(f"- **{slug}** — {desc}")
            lines.append("")

        lines.append(f"## Skills ({len(active)})")
        lines.append("")
        for slug, title, synopsis in active:
            if synopsis:
                lines.append(f"- **{slug}** — {title}")
                lines.append(f"  {synopsis}")
            else:
                lines.append(f"- **{slug}** — {title}")
        lines.append("")

        if hidden:
            lines.append(
                f"## Hidden ({len(hidden)} — kind not wired or status: planned)"
            )
            lines.append("")
            for slug, title, _synopsis in hidden:
                lines.append(f"- {slug} — {title}")
            lines.append("")

        lines.append("---")
        lines.append(
            "**Discover:** `search(kind='skill', q='your goal')` or "
            "`get(kind='skill', id='<slug>')`."
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

        kind_w = max(len(r[0]) for r in rows)
        verb_w = max(len(r[1]) for r in rows)
        for kind, verbs, desc in rows:
            lines.append(f"  {kind:<{kind_w}}  {verbs:<{verb_w}}  {desc}")

        lines.append("")
        lines.append(
            f"**{len(rows)} kinds active.** "
            "For deeper docs on any kind, try "
            "`get(kind='skill', id='precis-<kind>-help')`."
        )
        return "\n".join(lines)

    def _render_status(self) -> str:
        """Render the synthesised ``precis-status`` skill.

        Probes optional Python dependencies and reports installed /
        missing per backing module.  The MCP critic's April 2026
        re-probe noted that the previous CRITICAL (sentence-
        transformers missing from `[paper]`) would have been caught
        by a health tool surfacing optional-deps state — this skill
        is that tool.

        Pure introspection, no DB or network.  Each probe is a
        ``(label, module, kind it backs, install-hint)`` row; the
        method imports each module lazily and tags the line OK /
        MISSING / ERROR.  Adding a new probe is one row in
        ``_OPTIONAL_DEP_PROBES``.
        """
        lines = [
            "# precis-status",
            "",
            "Optional-dependency health probe.  Each row tags the",
            "Python module that backs a precis kind or affordance,",
            "and reports whether it imports cleanly in this venv.",
            "",
        ]

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

        label_w = max(len(r[0]) for r in rows)
        status_w = max(len(r[1]) for r in rows)
        for label, status, backs, hint in rows:
            line = f"  {label:<{label_w}}  {status:<{status_w}}  {backs}"
            lines.append(line)
            if hint and not status.startswith("OK"):
                lines.append(f"    └─ install: {hint}")

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
        "paper / markdown / patent / quest semantic search",
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
        "web / perplexity (websearch / think / research)",
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


# ---------------------------------------------------------------------------
# File access (importlib.resources keeps this working from a wheel)
# ---------------------------------------------------------------------------


def _list_skills() -> list[str]:
    """Return all available skill slugs (without the ``.md`` suffix)."""
    try:
        files = resources.files("precis.data.skills")
    except (ModuleNotFoundError, FileNotFoundError):
        log.warning("precis.data.skills package missing")
        return []
    out: list[str] = []
    for entry in files.iterdir():  # type: ignore[union-attr]
        name = entry.name
        if name.endswith(".md"):
            stem = name[:-3]
            if _SLUG_RE.match(stem):
                out.append(stem)
    return out


def _load_skill(slug: str) -> str | None:
    """Return the raw markdown for a skill, or None if missing."""
    try:
        files = resources.files("precis.data.skills")
        path = files / f"{slug}.md"
        if not path.is_file():  # type: ignore[union-attr]
            return None
        return path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    except (ModuleNotFoundError, FileNotFoundError):
        return None


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

    if hub is not None:
        for kind in _kinds_referenced_by_skill(slug, fm):
            if not _hub_has_kind(hub, kind):
                return (
                    f"this skill documents kind={kind!r} which is **not "
                    "wired** in this build - its examples will return "
                    "[error:NotFound] unknown kind."
                )

    return None


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
