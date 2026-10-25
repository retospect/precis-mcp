"""Tests for :mod:`precis.skill_index.graph` — wikilink edges, tag/kind
groups (docs/backlog/skill-graph.md slice 1).

Pure text-in, structure-out — no filesystem, no embedder.
"""

from __future__ import annotations

import dataclasses

import pytest

from precis.skill_index.graph import SkillGraph, build_skill_graph

# ── outbound / inbound edges ────────────────────────────────────────────


def _skill(body: str, *, front: str = "flavor: reference") -> str:
    return f"---\n{front}\n---\n# T\n## op\n{body}\n"


def test_build_empty_corpus() -> None:
    g = build_skill_graph({})
    assert g == SkillGraph()


def test_outbound_edge_recorded() -> None:
    files = {"a": _skill("see [[b]]"), "b": _skill("body")}
    g = build_skill_graph(files)
    assert g.outbound["a"] == ("b",)
    assert g.outbound["b"] == ()


def test_inbound_is_the_reverse_of_outbound() -> None:
    files = {"a": _skill("see [[b]]"), "b": _skill("body")}
    g = build_skill_graph(files)
    assert g.inbound["b"] == ("a",)
    assert "a" not in g.inbound  # nobody links to a


def test_linked_symmetrizes_outbound_and_inbound() -> None:
    files = {"a": _skill("see [[b]]"), "b": _skill("body")}
    g = build_skill_graph(files)
    # From b's side, a is a neighbour purely via the inbound edge.
    assert g.linked("b") == ("a",)
    assert g.linked("a") == ("b",)


def test_linked_deduplicates_mutual_links() -> None:
    files = {"a": _skill("see [[b]]"), "b": _skill("see [[a]]")}
    g = build_skill_graph(files)
    assert g.linked("a") == ("b",)
    assert g.linked("b") == ("a",)


def test_linked_excludes_self() -> None:
    files = {"a": _skill("see [[a]]")}
    g = build_skill_graph(files)
    assert g.outbound["a"] == ()  # self-links are dropped at build time
    assert g.linked("a") == ()


def test_dangling_link_appears_outbound_not_inbound() -> None:
    files = {"a": _skill("see [[ghost]]")}
    g = build_skill_graph(files)
    assert g.outbound["a"] == ("ghost",)
    assert "ghost" not in g.inbound
    # Not a real slug, so it never shows up as a's own neighbour list
    # either — linked() only knows about slugs actually in the corpus
    # via inbound; outbound alone would surface it, so check directly.
    assert "ghost" in g.linked("a")  # still visible from a's own page


def test_linked_unknown_slug_returns_empty() -> None:
    g = build_skill_graph({"a": _skill("body")})
    assert g.linked("nope") == ()


def test_linked_cap_truncates() -> None:
    files = {
        "hub": _skill("[[a]] [[b]] [[c]]"),
        "a": _skill("body"),
        "b": _skill("body"),
        "c": _skill("body"),
    }
    g = build_skill_graph(files)
    full = g.linked("hub")
    assert len(full) == 3
    capped = g.linked("hub", cap=2)
    assert capped == full[:2]
    assert len(capped) == 2


# ── tag / kind groups ────────────────────────────────────────────────────


def test_tag_groups_built_from_frontmatter() -> None:
    files = {
        "a": _skill("body", front="flavor: reference\ntags:\n  - orientation"),
        "b": _skill("body", front="flavor: reference\ntags:\n  - orientation"),
        "c": _skill("body", front="flavor: reference\ntags:\n  - workflow"),
    }
    g = build_skill_graph(files)
    assert g.by_tag("orientation") == ("a", "b")
    assert g.by_tag("workflow") == ("c",)
    assert g.by_tag("no-such-tag") == ()


def test_kind_groups_built_from_frontmatter() -> None:
    files = {
        "a": _skill("body", front="flavor: reference\nkinds:\n  - paper"),
        "b": _skill("body", front="flavor: reference\nkinds:\n  - paper\n  - patent"),
    }
    g = build_skill_graph(files)
    assert g.by_kind("paper") == ("a", "b")
    assert g.by_kind("patent") == ("b",)
    assert g.by_kind("no-such-kind") == ()


def test_absent_kinds_contributes_no_kind_group_membership() -> None:
    files = {"a": _skill("body", front="flavor: reference")}
    g = build_skill_graph(files)
    assert g.kinds == {}


def test_tag_and_kind_groups_are_sorted() -> None:
    files = {
        "zzz": _skill("body", front="flavor: reference\ntags:\n  - orientation"),
        "aaa": _skill("body", front="flavor: reference\ntags:\n  - orientation"),
    }
    g = build_skill_graph(files)
    assert g.by_tag("orientation") == ("aaa", "zzz")


# ── immutability / shape ─────────────────────────────────────────────────


def test_skill_graph_is_frozen_dataclass() -> None:
    g = build_skill_graph({})
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.outbound = {}  # type: ignore[misc]
