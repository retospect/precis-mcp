"""Bounded neighbour-link audit gates (gr626719).

Pins :func:`precis.skill_neighbours.skill_neighbour_audit` — see that
module's docstring for the report's sections. The fast legs run on the
regular gate; the live ``search()`` sweep is ``nightly`` — one real
search per linked shipped skill (~0.5 s × ~220 ≈ 2 min) clears the 30 s
gate bar.
"""

from __future__ import annotations

import re
import warnings

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import (
    _FOOTER_LINK_CAP,
    SkillHandler,
    _get_skill_graph,
    _list_skills,
    skill_corpus_texts,
)
from precis.skill_neighbours import (
    _DETAIL_CAP,
    _RENDER_CAP,
    SkillNeighbourReport,
    skill_neighbour_audit,
)
from precis.utils.skill_common import parse_frontmatter
from tests._skill_corpus import _install_corpus, _md

#: The isolated-skill exceptions the audit reports on the shipped corpus
#: (measured 2026-11-11): no wikilink edge in either direction and no
#: curated ``_SKILL_CATEGORIES`` siblings — reachable only via
#: ``family=``/tag/kind facets or a direct search. Pinned both ways so a
#: NEW isolated skill fails here (give it a real neighbour — a wikilink
#: or a category — or record the deliberate exception) and a skill that
#: gains a neighbour must leave the list.
_KNOWN_ISOLATED: tuple[str, ...] = (
    "components",
    "patent-abstract",
    "patent-claim",
    "patent-description",
    "patent-image-part",
    "patent-prior-art",
    "precis-chem-help",
    "precis-draft-reviewer",
    "precis-draft-section-insertion",
    "precis-learning-help",
    "precis-loose-files-help",
    "precis-mermaid",
    "precis-mermaid-class",
    "precis-mermaid-er",
    "precis-mermaid-flowchart",
    "precis-mermaid-gitgraph",
    "precis-mermaid-journey",
    "precis-mermaid-mindmap",
    "precis-mermaid-quadrant",
    "precis-mermaid-requirement",
    "precis-mermaid-sequence",
    "precis-mermaid-state",
    "precis-mermaid-timeline",
    "precis-mermaid-unsupported",
    "precis-mermaid-xychart",
    "precis-news-help",
    "precis-oauth-help",
    "precis-proposal-help",
    "precis-protein-help",
    "precis-se-flow-help",
    "precis-structsolve-help",
    "precis-structure-import-help",
    "precis-structure-slab-help",
    "precis-time-help",
    "precis-user-queue-help",
    "sci-abstract",
    "sci-conclusion",
    "sci-discussion",
    "sci-introduction",
    "sci-methods",
    "sci-related-work",
    "sci-results",
    "sci-survey-section",
)


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


@pytest.fixture(scope="module")
def shipped_report() -> SkillNeighbourReport:
    """One audit of the shipped corpus for all the shipped-corpus gates —
    the audit is a pure function of the corpus and ~40 s a run."""
    return skill_neighbour_audit()


# ── the audit itself (synthetic corpus) ──────────────────────────────


def test_audit_report_shape_on_synthetic_corpus() -> None:
    links = " ".join(f"[[n{i}]]" for i in range(_FOOTER_LINK_CAP + 2))
    corpus = {
        "a": _md(body="see [[b]] and [[ghost]]"),
        "b": _md(body="body"),
        "hub": _md(body=f"the mesh: {links}"),
        "iso": _md(body="alone — no links"),
        "p": _md(front="flavor: persona", body="no links either"),
        "w": _md(body="see [[wonly]]"),
        "wonly": _md(front="scope: write", body="body"),
    }
    corpus.update({f"n{i}": _md(body="body") for i in range(_FOOTER_LINK_CAP + 2)})

    report = skill_neighbour_audit(corpus=corpus)

    assert report.skills == len(corpus)
    assert report.dangling == (("a", "ghost"),)
    # 'iso' and the edge-less persona 'p' have no lateral surface; every
    # other slug is linked (in or out) or a curated sibling would rescue
    # it — synthetic slugs never sit in _SKILL_CATEGORIES.
    assert report.isolated == ("iso", "p")
    # hub's neighbours sort n0..n7; the footer names the first six and
    # defers n6/n7 to ``search(q='hub')``.
    assert report.overflow == (("hub", ("n6", "n7")),)
    # 'w' links only 'wonly', which is scope:write — a read connection
    # sees no neighbour for 'w'.
    assert report.scope_hidden == ("w",)


def test_curated_sibling_edges_are_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_SKILL_CATEGORIES`` members are navigation edges too: a member
    resolving to no served slug is a dangling ``see also:`` name on
    every bucket-mate's footer — reported per edge, like a wikilink."""
    monkeypatch.setattr(
        "precis.handlers.skill._SKILL_CATEGORIES",
        (
            ("Bucket", ("a", "b", "ghost-cat")),
            ("Thin", ("solo", "ghost-only")),
        ),
    )
    corpus = {
        "a": _md(body="body"),
        "b": _md(body="body"),
        "solo": _md(body="body"),
    }
    report = skill_neighbour_audit(corpus=corpus)
    assert report.dangling_siblings == (
        ("a", "ghost-cat"),
        ("b", "ghost-cat"),
        ("solo", "ghost-only"),
    )
    # A dangling edge is still a declared neighbour — 'solo' isn't
    # isolated, exactly as a lone 'a -> [[ghost]]' wikilink wouldn't be.
    assert report.isolated == ()


def test_report_render_is_bounded() -> None:
    report = skill_neighbour_audit(
        corpus={
            "a": _md(body=" ".join(f"[[gone{i}]]" for i in range(25))),
        }
    )
    out = report.render()
    assert f"{25 - 20} more" in out
    assert out.count("\n") < 40


def test_render_caps_neighbour_names_within_a_row() -> None:
    """Row details are bounded too: a beyond-cap neighbour list names
    ``_DETAIL_CAP`` slugs then counts the rest — the finding itself
    still carries them all."""
    n = _FOOTER_LINK_CAP + _DETAIL_CAP + 3
    corpus = {"hub": _md(body=" ".join(f"[[n{i:02}]]" for i in range(n)))}
    corpus.update({f"n{i:02}": _md(body="body") for i in range(n)})

    report = skill_neighbour_audit(corpus=corpus)
    (hub_row,) = report.overflow
    assert len(hub_row[1]) == _DETAIL_CAP + 3

    row = next(
        line for line in report.render().splitlines() if line.startswith("  hub:")
    )
    hidden = n - _FOOTER_LINK_CAP - _DETAIL_CAP
    assert row.endswith(f"+{hidden} more")


# ── shipped-corpus gates (fast) ──────────────────────────────────────


def test_shipped_corpus_has_no_dangling_neighbour_links(
    shipped_report: SkillNeighbourReport,
) -> None:
    """The ingest gate already hard-fails dangling ``[[slug]]`` targets;
    this re-verifies at the served-corpus layer the graph footer and
    ``_expand_wikilinks`` resolve against."""
    assert shipped_report.dangling == (), shipped_report.render()


def test_shipped_corpus_has_no_dangling_curated_siblings(
    shipped_report: SkillNeighbourReport,
) -> None:
    """``_SKILL_CATEGORIES`` is hand-curated with no ingest gate — a
    renamed or dropped member prints a dead ``see also:`` name on every
    bucket-mate's footer."""
    assert shipped_report.dangling_siblings == (), shipped_report.render()


def test_isolated_skills_match_the_known_exceptions(
    shipped_report: SkillNeighbourReport,
) -> None:
    """The bounded exceptions list: report, never invent neighbours."""
    assert shipped_report.isolated == _KNOWN_ISOLATED, shipped_report.render()


def test_every_linked_skill_keeps_a_read_scope_neighbour_door(
    shipped_report: SkillNeighbourReport,
) -> None:
    """A skill whose every neighbour is ``scope: write`` shows none on a
    read-only connection — no ``linked:`` row, no ``related:``. Reported
    as an exception; today there are none."""
    assert shipped_report.scope_hidden == (), shipped_report.render()


# ── search exposure ──────────────────────────────────────────────────


def test_search_related_line_names_graph_neighbours(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism: ``search(q=slug)`` pins the slug as top hit and
    renders its graph neighbours as the ``related:`` line."""
    _install_corpus(
        monkeypatch,
        {"a": _md(body="see [[b]]"), "b": _md(body="body")},
    )
    body = skill.search(q="a").body
    assert "related: b" in body


@pytest.mark.nightly(
    reason="~2 min at 0.5 s/search × ~220 linked skills — over the 30 s "
    "gate bar; runs in the nightly full shape and local scripts/test"
)
def test_search_exposes_each_linked_skills_neighbours() -> None:
    """Whether searching a skill exposes its neighbours — measured.

    For every non-persona shipped skill with wikilink neighbours,
    ``search(q=slug)`` must surface at least one of them (the
    ``related:`` line or a result row): the footer's ``linked:`` and
    ``+N more`` pointers all assume the slug-search page is where the
    neighbourhood lives. Neighbours the page hides are reported as a
    warning, not a failure — page-1 real estate is bounded by design.
    """
    h = SkillHandler(hub=Hub())
    corpus = skill_corpus_texts()
    graph = _get_skill_graph()
    misses: list[str] = []
    thin: list[str] = []
    for slug in sorted(_list_skills()):
        if parse_frontmatter(corpus[slug]).flavor == "persona":
            continue
        linked = graph.linked(slug)
        if not linked:
            continue
        body = h.search(q=slug).body
        shown = [
            s for s in linked if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", body)
        ]
        if not shown:
            misses.append(slug)
        elif len(shown) < len(linked):
            thin.append(f"{slug} ({len(linked) - len(shown)} hidden)")
    assert not misses, (
        f"{len(misses)} linked skill(s) expose no neighbour on their own "
        f"slug-search page — the serve's navigation pointers dead-end "
        f"for them: {_cap(misses)}"
    )
    if thin:
        warnings.warn(
            f"{len(thin)} skill(s) hide some neighbours from the "
            f"search(q=slug) page: {_cap(thin)}",
            stacklevel=1,
        )


def _cap(items: list[str]) -> str:
    """Bounded join for failure/warning lists — the first ``_RENDER_CAP``
    names then an omitted count, same convention as the report."""
    shown = ", ".join(items[:_RENDER_CAP])
    if len(items) > _RENDER_CAP:
        shown += f" … +{len(items) - _RENDER_CAP} more"
    return shown
