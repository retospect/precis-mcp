"""Tests for SkillHandler — markdown docs served from data/skills/.

The skills are real package data (`src/precis/data/skills/*.md`), so
these tests assert against the actual files shipped with the package.
That's intentional: they double as a packaging smoke test.

Split into per-concern files (gr515135):

- tests/test_skill_search.py — terms, gap-cut, verbatim, axis filters
- tests/test_skill_toc.py — toc listing, categories, tag/kind filters
- tests/test_skill_rank.py — title boost, family roots, content tokens
- tests/test_skill_semantic.py — semantic rows, chunk-id round-trips
- tests/test_skill_corpus.py — corpus, personas, includes, overview
- tests/test_skill_graph.py — graph footers, wikilinks
- tests/test_skill_gates.py — loadability, requires, availability gates
- tests/test_skill_state.py — rev freshness, status, breadcrumbs, fleet
- tests/test_skill_index_graph.py — precis.skill_index.graph builder
  tests (pre-existing file, kept under a module-qualified name — the
  split took test_skill_graph.py for the handler's graph surface)
- tests/_skill_corpus.py — shared synthetic-corpus installer
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.skill import SkillHandler


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── single fetch ──────────────────────────────────────────────────────


def test_get_existing_skill(skill: SkillHandler) -> None:
    out = skill.get(id="precis-overview")
    # The first H1 should match the canonical skill name.
    assert "precis-overview" in out.body or "Precis" in out.body
    assert len(out.body) > 100  # not empty


def test_get_bare_kind_aliases_to_help_skill(skill: SkillHandler) -> None:
    """gr338442: ``id='se'`` serves ``precis-se-help`` with a banner
    naming the redirect instead of NotFound."""
    out = skill.get(id="se")
    assert "(aliased from 'se' → precis-se-help)" in out.body
    assert len(out.body) > 100


def test_get_bare_verb_aliases_to_help_skill(skill: SkillHandler) -> None:
    out = skill.get(id="search")
    assert "(aliased from 'search' → precis-search-help)" in out.body


def test_get_unknown_id_without_help_alias_still_not_found(
    skill: SkillHandler,
) -> None:
    with pytest.raises(NotFound):
        skill.get(id="zz-no-such-skill")


def test_get_skill_body_sets_pagination_alt_hint(skill: SkillHandler) -> None:
    """A whole-skill-body ``get`` names the concrete slug in the
    footer alt_hint so a paginated read is pointed at the cheaper
    ``/toc`` + ``~N`` targeted access instead of blindly draining
    every page."""
    out = skill.get(id="precis-overview")
    assert out.pagination_alt_hint is not None
    assert "precis-overview/toc" in out.pagination_alt_hint
    assert "precis-overview~N" in out.pagination_alt_hint


def test_get_skill_toc_omits_pagination_alt_hint(skill: SkillHandler) -> None:
    """The ``/toc`` view is already the targeted-access affordance —
    it doesn't need to point at itself."""
    out = skill.get(id="precis-overview/toc")
    assert out.pagination_alt_hint is None


def test_get_skill_chunk_omits_pagination_alt_hint(skill: SkillHandler) -> None:
    out = skill.get(id="precis-overview~0")
    assert out.pagination_alt_hint is None


def test_skill_pagination_alt_hint_fits_footer_reserve(skill: SkillHandler) -> None:
    """The concrete hint text the skill handler emits must round-trip
    through :meth:`PaginationCache.split` without the head overflowing
    the configured cap — the reserve calculation has to account for the
    hint's actual length, not just the base footer."""
    from precis._pagination import PaginationCache

    out = skill.get(id="precis-overview")
    assert out.pagination_alt_hint is not None

    cache = PaginationCache()
    big_body = out.body + ("\n\n## padding\n" + ("x" * 2000)) * 20
    head, cursor = cache.split(big_body, alt_hint=out.pagination_alt_hint)
    assert cursor is not None
    assert "If you only need part of this document:" in head
    assert out.pagination_alt_hint in head
    from precis._pagination import _max_body_bytes

    assert len(head.encode("utf-8")) <= _max_body_bytes()


def test_get_paper_skill_documents_navigation(skill: SkillHandler) -> None:
    """The phase 3.5 navigation update added a 'Navigate' section."""
    out = skill.get(id="precis-paper-help")
    assert "Navigate" in out.body or "TOC" in out.body


def test_get_plan_skill_resolves_and_documents_create(skill: SkillHandler) -> None:
    """gr164476 — the `plan` kind now has a dedicated help skill; agents
    were reaching for `precis-plan-help` and getting NotFound. It must
    resolve, name the create-vs-add-node contract, and appear in the
    bare-list index."""
    out = skill.get(id="precis-plan-help")
    assert "precis-plan-help" in out.body
    # The headline contract: create vs. add-node.
    assert "create" in out.body and "add a node" in out.body
    assert "pe<chunk_id>" in out.body or "pe<id>" in out.body
    # Discoverable in the index the bare list returns.
    assert "precis-plan-help" in skill.get().body


def test_get_missing_raises_with_options(skill: SkillHandler) -> None:
    # A near-miss slug gets difflib suggestions naming the right skill.
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="precis-overvieww")
    err = excinfo.value
    assert err.options is not None
    assert any("precis-overview" in s for s in err.options)
    # A genuinely unknown slug gets the search redirect, not the old
    # every-skill options dump.
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="nonexistent-skill")
    err = excinfo.value
    hints = err.next if isinstance(err.next, list) else [err.next or ""]
    assert any("search(kind='skill'" in h for h in hints)


def test_missing_skill_notfound_names_deploy_gap(skill: SkillHandler) -> None:
    """gr514411 — a slug merged on main but not yet deployed raised a
    bare NotFound with no hint that the running build simply pre-dates
    it. The error now names the deploy gap and points at the
    ``precis-status`` build report so the agent can check."""
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="nonexistent-skill")
    hints = excinfo.value.next
    hints = hints if isinstance(hints, list) else [hints or ""]
    assert any("not yet deployed" in h for h in hints)
    assert any("precis-status" in h for h in hints)
    # The find-in-ref path (q= on a concrete slug) carries the same hint.
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="nonexistent-skill", q="anything")
    hints = excinfo.value.next
    hints = hints if isinstance(hints, list) else [hints or ""]
    assert any("not yet deployed" in h for h in hints)


def test_invalid_slug_raises(skill: SkillHandler) -> None:
    # The id parser is the first gate (regex-tight); the slug
    # validator the second. Both routes produce BadInput — that's
    # what the test pins. Phase B 2026-05-31: added the address
    # parser, so the specific error text shifted but the
    # error class stays the same.
    with pytest.raises(BadInput):
        skill.get(id="UPPERCASE")
    with pytest.raises(BadInput):
        skill.get(id="path/traversal")


# ── index view ────────────────────────────────────────────────────────


def test_bare_get_lists_skills(skill: SkillHandler) -> None:
    out = skill.get()
    assert "skill" in out.body.lower()
    assert "precis-overview" in out.body
    # The index trailer is now an explicit "Suggested starting
    # commands" section (round-2 picky reviewer flagged the old
    # generic "Next:" wording as ambiguous between recipe shortcuts
    # and new skills). Pin the new heading so a future rename
    # surfaces here.
    assert "Suggested starting commands" in out.body


def test_path_view_also_lists(skill: SkillHandler) -> None:
    out = skill.get(id="/index")
    assert "precis-overview" in out.body


def test_get_rejects_literal_args_dict(skill: SkillHandler) -> None:
    """gr514727: dispatch flattens ``args={'k': v}`` into kwargs before
    the handler runs, so a literal ``args=`` passed at handler level
    lands in ``**_kw`` and silently does nothing. Refuse it and name the
    kwarg spelling a handler-level test should use instead."""
    with pytest.raises(BadInput, match="args="):
        skill.get(id="toc", args={"family": "se"})
