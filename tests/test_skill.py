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

from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.skill import _REPO_DEV_SKILLS, SkillHandler
from precis.runtime.core import PrecisRuntime


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


# ── registered served aliases (_SKILL_SLUG_ALIASES, gr524267) ──────────


def test_get_registered_alias_serves_canonical_skill(
    skill: SkillHandler,
) -> None:
    """gr524267: ``precis-part-help`` is the ``precis-<kind>-help``
    convention's known exception — the part skill is task-named, and the
    guessed slug resolves to it with the one-hop's banner."""
    out = skill.get(id="precis-part-help")
    assert "(aliased from 'precis-part-help' → precis-part-select-help)" in out.body
    assert "JLCPCB" in out.body


def test_registered_alias_resolves_toc_and_chunk_selectors(
    skill: SkillHandler,
) -> None:
    """Unlike the bare-kind one-hop, a registered alias carries the
    selectors too — ``/toc`` and ``~N`` land on the canonical skill's
    handles."""
    out = skill.get(id="precis-part-help/toc")
    assert "precis-part-select-help" in out.body
    out = skill.get(id="precis-part-help~0")
    assert "precis-part-select-help~0" in out.body


def test_registered_alias_find_in_ref(skill: SkillHandler) -> None:
    """``get(id=<alias>, q=…)`` scans the canonical skill's body."""
    out = skill.get(id="precis-part-help", q="Basic")
    assert "Basic" in out.body


def test_registered_alias_is_noted_on_the_canonical_row(
    skill: SkillHandler,
) -> None:
    """The catalogue annotates the canonical slug with its alias rather
    than listing ``precis-part-help`` as a second row."""
    out = skill.get(id="/")
    assert "precis-part-select-help (alias: precis-part-help)" in out.body


def test_get_skill_body_sets_result_budget_hint(skill: SkillHandler) -> None:
    """A whole-skill-body ``get`` names its targeted TOC and section reads."""
    out = skill.get(id="precis-overview")
    assert out.result_budget_hint is not None
    assert "precis-overview/toc" in out.result_budget_hint
    assert "precis-overview~N" in out.result_budget_hint


def test_get_skill_toc_omits_result_budget_hint(skill: SkillHandler) -> None:
    """The ``/toc`` view is already the targeted-access affordance."""
    out = skill.get(id="precis-overview/toc")
    assert out.result_budget_hint is None


def test_get_skill_chunk_omits_result_budget_hint(skill: SkillHandler) -> None:
    out = skill.get(id="precis-overview~0")
    assert out.result_budget_hint is None


def test_skill_chunk_next_lifts_a_referenced_section(skill: SkillHandler) -> None:
    """gr626104: a lone ``~N`` serve has no "below" — a sibling-section
    handle the chunk names rides the ``Next:`` trailer as a direct call,
    and the prose names it too (``precis-paper-help~5`` → ``~6``)."""
    out = skill.get(id="precis-paper-help~5")
    assert "`precis-paper-help~6`" in out.body
    assert "get(kind='skill', id='precis-paper-help~6')" in out.body


def test_skill_chunk_next_skips_handles_inside_the_served_range(
    skill: SkillHandler,
) -> None:
    """A range serve already includes a self-referenced chunk — the
    trailer doesn't re-advertise it."""
    out = skill.get(id="precis-paper-help~5..6")
    assert "get(kind='skill', id='precis-paper-help~6')" not in out.body


def test_skill_result_budget_hint_recovers_oversize_read(skill: SkillHandler) -> None:
    from precis._result_budget import fit_result_body

    out = skill.get(id="precis-overview")
    fitted = fit_result_body(
        out.body,
        verb="get",
        kind="skill",
        context_limited=True,
        targeted_hint=out.result_budget_hint,
        read="single",
    )
    assert out.result_budget_hint is not None
    assert out.result_budget_hint in fitted
    assert "… truncated: showing" in fitted


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


def _next_hints(err: BadInput | NotFound) -> list[str]:
    nxt = err.next
    return nxt if isinstance(nxt, list) else [nxt or ""]


def test_repo_dev_skill_notfound_names_the_file(skill: SkillHandler) -> None:
    """gr563570 — ``bug`` is a repo-dev skill
    (``.claude/skills/bug/SKILL.md``), not a product skill; the NotFound
    recurred ~13×/day, mostly codex clients. The refusal now says so and
    names the file, ahead of the unchanged search/deploy hints."""
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="bug")
    hints = _next_hints(excinfo.value)
    assert any(
        "repo-dev skill" in h and ".claude/skills/bug/SKILL.md" in h for h in hints
    )
    assert any("search(kind='skill'" in h for h in hints)
    assert any("not yet deployed" in h for h in hints)
    # The selector views land on the same refusal.
    for sid in ("bug/toc", "bug~0"):
        with pytest.raises(NotFound) as excinfo:
            skill.get(id=sid)
        hints = _next_hints(excinfo.value)
        assert any(".claude/skills/bug/SKILL.md" in h for h in hints), sid
    # find-in-ref too.
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="bug", q="anything")
    hints = _next_hints(excinfo.value)
    assert any(".claude/skills/bug/SKILL.md" in h for h in hints)


def test_repo_dev_skills_constant_matches_checkout() -> None:
    """``_REPO_DEV_SKILLS`` names exactly the repo's ``.claude/skills``
    dirs — the fallback for deployments with no repo mount; a new
    repo-dev skill must land here or its hint rots."""
    root = Path(__file__).resolve().parents[1]
    dirs = {
        p.name
        for p in (root / ".claude" / "skills").iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    }
    assert dirs == set(_REPO_DEV_SKILLS)


def test_non_repo_dev_slug_gets_no_file_hint(skill: SkillHandler) -> None:
    """An ordinary unknown slug keeps the plain catalogue hints."""
    with pytest.raises(NotFound) as excinfo:
        skill.get(id="nonexistent-skill")
    hints = _next_hints(excinfo.value)
    assert not any("repo-dev skill" in h for h in hints)


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


# ── search scope=/view= (gr573037) ──────────────────────────────────


def test_search_scope_routes_to_find_in_ref(skill: SkillHandler) -> None:
    """gr573037: the slim search schema advertises ``scope=`` on every
    kind, and callers used it to mean "search inside that one skill" —
    20 of 32 refused calls carried it. It now routes to find-in-ref
    (``get(id='<slug>', q=…)``) instead of dying at the kwargs gate."""
    out = skill.search(q="executor", scope="precis-job-help")
    assert "# find 'executor' in precis-job-help" in out.body
    assert "match" in out.body


def test_search_scope_needs_q(skill: SkillHandler) -> None:
    with pytest.raises(BadInput, match="needs q="):
        skill.search(scope="precis-job-help")


def test_search_scope_unknown_slug_notfound(skill: SkillHandler) -> None:
    """A dead slug keeps find_in_ref's own refusal, with the catalogue
    hints — same as ``get(id=<slug>, q=…)``."""
    with pytest.raises(NotFound) as excinfo:
        skill.search(q="anything", scope="nonexistent-skill")
    assert any("search(kind='skill'" in h for h in _next_hints(excinfo.value))


def test_search_view_index_folds_to_default(skill: SkillHandler) -> None:
    """``view='index'`` spells the default listing (the memory
    convention): q-less it is the catalogue, with q it is the hits —
    the second most common refused shape on the gripe (11 calls)."""
    assert skill.search(view="index").body == skill.get().body
    out = skill.search(q="kind", view="index")
    assert "skill match" in out.body


def test_search_view_refused_with_working_call(skill: SkillHandler) -> None:
    """Other ``view=`` values name get() reads — refused with the
    working call, the python handler's gr567275 convention."""
    with pytest.raises(BadInput) as excinfo:
        skill.search(q="kind", view="toc")
    hints = _next_hints(excinfo.value)
    assert any("get(kind='skill'" in h for h in hints)
    assert any("scope=" in h for h in hints)


def test_search_scope_through_dispatch(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The gripe's actual failure was the strictness gate rejecting
    ``scope=`` before the handler ran — pin the full dispatch path."""
    body, is_error = runtime_with_store.dispatch_with_status(
        "search",
        {"kind": "skill", "q": "executor", "scope": "precis-job-help"},
    )
    assert is_error is False
    assert "# find 'executor' in precis-job-help" in body
