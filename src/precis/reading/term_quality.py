"""Non-concept term filter — the upstream guard against vacuous glossary terms
becoming ``concept`` nodes (and thence junk Anki cards).

Deterministic reflection of the ``precis-cloze`` rule-0 gate: a glossary term is
a *non-concept* when it is a topic-label / research-direction, a stock academic
phrase, or document front-matter / a section header. Such a term carries no
specific durable fact — the concept node built from it can only ever yield a
card that restates its own label (gripe 186183: "new trends", "novel
compounds", "improved properties", "extensively examined", "Dedication", …).

Name-anchored and **conservative on purpose** — a wrongly-dropped real concept
(silent knowledge loss) is far worse than a junk one slipping through to the
already-shipped card-side rule-0 gate. The two-word rule fires only when *both*
words are vacuous, and the modifier/adverb sets deliberately exclude adjectives
that head real technical noun phrases (critical, major, advanced, well, highly,
…), so genuine terms whose head happens to be generic ("expert systems",
"fatigue resistance", "critical system", "advanced materials") are kept. There
is no bare single-generic-head rule for the same reason ("field", "work"). What
this filter misses (e.g. "critical evaluation", or open-set proper nouns like
"World Heritage Site") is caught downstream by the card gate. Applied at the
glossary build (``paper_glossary._clean_clusters``) so junk never enters a fresh
glossary chunk, and again at the concept chokepoint (``promote.promote_paper``)
so glossaries already in the corpus don't mint junk concepts.
"""

from __future__ import annotations

import re

# Section headers / front-matter / scaffolding — a bare one of these as a
# "term" is never the subject being learned.
_FRONT_MATTER: frozenset[str] = frozenset(
    {
        "introduction",
        "conclusion",
        "conclusions",
        "overview",
        "summary",
        "methods",
        "method",
        "methodology",
        "results",
        "discussion",
        "references",
        "reference",
        "bibliography",
        "acknowledgement",
        "acknowledgements",
        "acknowledgment",
        "acknowledgments",
        "dedication",
        "abstract",
        "appendix",
        "appendices",
        "preface",
        "foreword",
        "glossary",
        "contents",
        "keywords",
        "abbreviations",
        "outline",
        "author",
        "authors",
        "affiliation",
        "affiliations",
        "copyright",
        "funding",
        "disclosure",
    }
)

# Vacuous leading modifiers — words that promise novelty/quantity but no
# content, so "<modifier> <anything-generic>" is always a topic label. The two-
# word rule requires one of these AND a generic head. DELIBERATELY excludes
# adjectives that are load-bearing in real technical noun phrases (critical,
# major, general, advanced, common, key, …): those collide with genuine terms
# ("critical system", "advanced materials", "major system") and a wrongly-
# dropped concept is far worse than a junk one slipping to the card-side gate.
_MODIFIERS: frozenset[str] = frozenset(
    {
        "new",
        "recent",
        "future",
        "emerging",
        "novel",
        "improved",
        "enhanced",
        "various",
        "many",
        "several",
        "different",
        "other",
        "additional",
        "further",
        "certain",
        "some",
        "more",
    }
)

# Generic heads — nouns so broad they carry no subject on their own. Safe to be
# generous here: a head only fires alongside a vacuous modifier above.
_HEADS: frozenset[str] = frozenset(
    {
        "trend",
        "trends",
        "compound",
        "compounds",
        "advance",
        "advances",
        "development",
        "developments",
        "direction",
        "directions",
        "approach",
        "approaches",
        "method",
        "methods",
        "material",
        "materials",
        "application",
        "applications",
        "result",
        "results",
        "finding",
        "findings",
        "property",
        "properties",
        "advantage",
        "advantages",
        "performance",
        "feature",
        "features",
        "characteristic",
        "characteristics",
        "aspect",
        "aspects",
        "factor",
        "factors",
        "issue",
        "issues",
        "challenge",
        "challenges",
        "opportunity",
        "opportunities",
        "system",
        "systems",
        "technique",
        "techniques",
        "strategy",
        "strategies",
        "concept",
        "concepts",
        "idea",
        "ideas",
        "topic",
        "topics",
        "area",
        "areas",
        "field",
        "fields",
        "role",
        "roles",
        "example",
        "examples",
        "case",
        "cases",
        "study",
        "studies",
        "work",
        "works",
        "review",
        "reviews",
        "evaluation",
        "evaluations",
        "analysis",
        "analyses",
        "consideration",
        "considerations",
        "insight",
        "insights",
        "phenomenon",
        "phenomena",
        "process",
        "processes",
    }
)

# Rhetorical adverbs that lead a "stock academic phrase" (adverb + past
# participle): "extensively examined", "widely studied", "critically evaluated".
# Excludes adverbs that head real technical terms (well-ordered, highly
# correlated, deeply bound, actively transported) — same false-positive caution
# as the modifier set.
_RHETORICAL_ADVERBS: frozenset[str] = frozenset(
    {
        "extensively",
        "widely",
        "thoroughly",
        "recently",
        "previously",
        "generally",
        "commonly",
        "frequently",
        "carefully",
        "briefly",
        "critically",
        "increasingly",
        "successfully",
        "originally",
        "initially",
    }
)

_PARTICIPLE_RE = re.compile(r"^[a-z]+(ed|ied)$")


def _tokens(name: str) -> list[str]:
    """Lowercased alphabetic word tokens; drops punctuation and digits."""
    return re.findall(r"[a-z]+", (name or "").lower())


def non_concept_reason(name: str, definition: str = "") -> str | None:
    """Return a short reason string if ``name`` is a non-concept term that should
    not become a ``concept`` node, else ``None``.

    Conservative by construction: only fires on the patterns that produced the
    documented junk (gripe 186183). ``definition`` is accepted for future use
    but currently unused — the name alone is decisive for these patterns.
    """
    toks = _tokens(name)
    if not toks:
        return "empty term"

    # Bare front-matter / section header.
    if len(toks) == 1 and toks[0] in _FRONT_MATTER:
        return f"front-matter/section header ('{toks[0]}')"

    # Stock academic phrase: rhetorical adverb + past participle
    # ("extensively examined", "critically evaluated").
    if (
        len(toks) == 2
        and toks[0] in _RHETORICAL_ADVERBS
        and _PARTICIPLE_RE.match(toks[1])
    ):
        return f"stock academic phrase ('{toks[0]} {toks[1]}')"

    # Vacuous modifier + generic head ("new trends", "novel compounds",
    # "improved properties", "various applications").
    if len(toks) == 2 and toks[0] in _MODIFIERS and toks[1] in _HEADS:
        return f"topic label ('{toks[0]} {toks[1]}')"

    # NOTE: no single-token generic-head rule — a bare generic head collides with
    # real standalone concepts ("field", "work", "process", "case", "role"), so
    # the head set only fires alongside a vacuous modifier (above). Section-word
    # singletons are caught by the front-matter set instead.
    return None


__all__ = ["non_concept_reason"]
