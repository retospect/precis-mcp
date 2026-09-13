"""Kind-scoped skill breadcrumb — the graph consumed outside the skill
handler (docs/backlog/skill-graph.md slice 4).

Two call sites want the same one-line pointer: a kind's help/landing
read (``get(kind='se')`` with no ``id=``, ``runtime.dispatch``) and a
kind-shaped error (``BadInput``/``Unsupported`` naming the failing
``kind=``, ``runtime.hints``). Both live outside ``handlers.skill``, so
this module builds and caches its own :class:`~precis.skill_index.graph.
SkillGraph` from :func:`precis.handlers.skill.skill_corpus_texts` rather
than reaching for that module's private ``_get_skill_graph`` cache (see
that function's own docstring on why it's private) — one extra
in-process pass over a corpus that's static for the process lifetime is
cheap, and keeps this consumer decoupled from ``handlers.skill``'s
internals.

:func:`kind_skill_hint` never raises: a corpus-scan bug here must
degrade to "no hint" everywhere it's consulted, never break the kind
read or mask the real error it would have annotated (both callers treat
this as strictly additive).
"""

from __future__ import annotations

from precis.handlers._skill_common import parse_frontmatter
from precis.skill_index.graph import SkillGraph, build_skill_graph

#: Cap on how many skills the one-line breadcrumb names before it stops
#: — a hub kind with a dozen skills would otherwise blow past a "short
#: line" (docs/backlog/skill-graph.md slice 4).
_HINT_CAP = 4

_GRAPH_CACHE: SkillGraph | None = None
_PERSONA_SLUGS_CACHE: frozenset[str] | None = None


def _cache_clear() -> None:
    """Drop the cached graph + persona set. Tests only — mirrors
    ``handlers.skill._load_skills_map_cache_clear``."""
    global _GRAPH_CACHE, _PERSONA_SLUGS_CACHE
    _GRAPH_CACHE = None
    _PERSONA_SLUGS_CACHE = None


def _graph() -> tuple[SkillGraph, frozenset[str]]:
    """Lazily build + cache ``(graph, persona_slugs)`` for this process.

    Raises on a genuine build failure — callers (:func:`kind_skill_hint`)
    are the ones responsible for degrading; kept unguarded here so a
    test can assert the raise propagates before the wrapper swallows it.
    """
    global _GRAPH_CACHE, _PERSONA_SLUGS_CACHE
    if _GRAPH_CACHE is None:
        from precis.handlers.skill import skill_corpus_texts

        texts = skill_corpus_texts()
        graph = build_skill_graph(texts)
        personas: set[str] = set()
        for slug, text in texts.items():
            fm = parse_frontmatter(text)
            if fm.flavor == "persona":
                personas.add(slug)
        _GRAPH_CACHE = graph
        _PERSONA_SLUGS_CACHE = frozenset(personas)
    assert _PERSONA_SLUGS_CACHE is not None  # set alongside _GRAPH_CACHE above
    return _GRAPH_CACHE, _PERSONA_SLUGS_CACHE


def kind_skill_hint(kind: str, *, cap: int = _HINT_CAP) -> str | None:
    """One-line ``skills for kind='<kind>': get(...) · get(...)``
    breadcrumb, or ``None`` when there's nothing to say — an unwired
    kind, a kind with no ``kinds:``-tagged skills, or a graph-build
    failure (logged nowhere on purpose: this is a best-effort hint
    layer, not a diagnostic surface).

    Personas (``flavor: persona``) are dropped even if they carry
    ``kinds:`` — they stay out of every surfaced list (docs/backlog/
    skill-graph.md slice 2). Order is :meth:`SkillGraph.by_kind`'s
    sorted order, capped at ``cap``.
    """
    try:
        graph, personas = _graph()
        slugs = [s for s in graph.by_kind(kind) if s not in personas]
    except Exception:
        return None
    if not slugs:
        return None
    calls = " · ".join(f"get(kind='skill', id='{s}')" for s in slugs[:cap])
    return f"skills for kind={kind!r}: {calls}"
