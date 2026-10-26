"""SkillHandler ranking — title boost, family-root pins and content tokens (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── tokenized matcher: word-overlap title boost (discoverability audit) ──
#
# Regression tests for the 2026-07 skill-search discoverability audit.
# The old lexical leg + title boost required the *entire* query to appear
# as one contiguous substring of the body / of title+H1, so punctuated or
# reordered natural-language queries surfaced nothing even when the
# obviously-correct skill existed. These run on the no-embedder ``skill``
# fixture, so they exercise exactly the lexical + title-boost legs that
# were changed (the semantic leg is absent and contributes nothing).


def _ranked_slugs(body: str) -> list[str]:
    """Slugs of the TOON data rows, in rendered (score) order."""
    out: list[str] = []
    for ln in body.splitlines():
        if "\t" not in ln or ln.startswith("{"):
            continue
        head = ln.split("\t", 1)[0].strip()
        if head.startswith("precis-") or "-" in head:
            out.append(head)
    return out


def test_title_boost_matches_word_overlap_not_contiguous_phrase(
    skill: SkillHandler,
) -> None:
    """A query whose words overlap a skill's title/headers surfaces it,
    even though it never appears as one contiguous substring.

    Both queries returned *zero / absent* under the old atomic-substring
    matcher: `precis-toolpath-help`'s title reads "canonical call
    sequences per scenario" (the word "toolpath" lives only in the slug),
    and `precis-cite-paper-help`'s relevant text is in H2 headers
    ("## How do I cite a paper?") the old boost never looked at.
    """
    top = _ranked_slugs(skill.search(q="toolpath canonical call sequences").body)[:3]
    assert "precis-toolpath-help" in top, top

    top = _ranked_slugs(skill.search(q="how to cite a paper, add a citation").body)[:3]
    assert "precis-cite-paper-help" in top, top


def test_title_boost_requires_identity_match_not_mere_mention(
    skill: SkillHandler,
) -> None:
    """The boost pins the skill the query *names*, not every skill that
    mentions the words in some section — so "cite a paper" surfaces the
    citing skills, and a single common word doesn't promote the whole
    catalogue.
    """
    top5 = _ranked_slugs(skill.search(q="cite a paper").body)[:5]
    assert "precis-cite-paper-help" in top5, top5
    # A lone common word must not title-boost (would pin half the corpus);
    # it still returns lexical hits without crashing.
    out = skill.search(q="citation")
    assert "skill match" in out.body or "no skills mention" in out.body


def test_single_word_query_pins_named_skill(skill: SkillHandler) -> None:
    """A one-word query that names a skill's subject still pins it via the
    identity path (parity with the old single-word title substring boost).
    """
    assert "precis-memory-help" in _ranked_slugs(skill.search(q="memory").body)[:3]
    top3 = _ranked_slugs(skill.search(q="gripe").body)[:3]
    assert any("gripe" in s for s in top3), top3


# ── DF-aware rare-identity-token bypass (gr259665) ────────────────────
#
# The 70%-coverage bar starves a long, multi-topic query even when one of
# its words names a skill unambiguously (catalogue document frequency ==
# 1) — a rare identity token bypasses the bar; a common one (DF > 1)
# still needs it. Real-corpus note: the gripe's original repro query
# ("taproot help merge repoint demote claim") was diagnosed against
# ``precis-taproot-help`` as the expected top hit, but post the taproot
# skill split (a08ba404) "merge"/"repoint" are ``precis-taproot-mint-
# help``'s territory (DF(identity)['merge'] == 1, naming that skill, not
# ``precis-taproot-help`` — none of ``precis-taproot-help``'s own matched
# identity tokens {taproot, claim, help} have DF == 1: taproot names 3
# sibling skills, help names 100+). The correct top hit for that exact
# wording is the mint-help skill; ``precis-taproot-help`` gets its own
# pin test below via a token that genuinely is DF == 1 for it.


def test_title_boost_df1_token_bypasses_coverage_bar_for_taproot_family(
    skill: SkillHandler,
) -> None:
    """gr259665's repro query must surface a taproot-family skill in the
    top rows instead of sinking below the page_size cut. "merge" has
    catalogue identity-DF == 1 (only ``precis-taproot-hub-edit-help``'s
    title carries it, since the sharpen/refine/merge section split out of
    mint-help) — that's the token that should fire the bypass, so that
    skill is the one that gets pinned to the top."""
    ranked = _ranked_slugs(
        skill.search(q="taproot help merge repoint demote claim").body
    )
    assert ranked, "expected at least one ranked hit"
    assert ranked[0] == "precis-taproot-hub-edit-help", ranked[:5]


def test_title_boost_df1_token_pins_taproot_help_on_its_own_identity(
    skill: SkillHandler,
) -> None:
    """A query whose ONLY on-topic word is a genuinely DF==1 identity
    token for ``precis-taproot-help`` itself ("many", from its H1 "one
    claim, many papers, one citable hub" — unique to this skill) still
    pins it, even padded with enough unrelated words to push coverage
    to 33% — well under the 70% bar."""
    ranked = _ranked_slugs(
        skill.search(q="many detailed technical notes about taproot workflows").body
    )
    assert ranked, "expected at least one ranked hit"
    assert ranked[0] == "precis-taproot-help", ranked[:5]


def test_title_boost_ubiquitous_token_alone_does_not_bypass_coverage_bar(
    skill: SkillHandler,
) -> None:
    """A multi-word query whose only identity overlap is a ubiquitous
    word ("help" — a title/slug token on 100+ skills) must NOT get
    title-boosted just because the word is present; it's exactly the
    "documented limitation guard" the 70% bar exists to keep. (The filler
    words must stay off every skill's identity: "reference" left this
    query when ``precis-draft-reference-help`` made it a DF==1 token.)"""
    out = skill.search(q="help documentation manual guide extra words here")
    assert "title match" not in out.body, out.body


# ── boundary pins for the DF-aware bypass cluster (gr333204) ───────────
#
# The three tests below each isolate one edge of
# ``if not single and not rare_identity_hit and (len(matched) < 2 or
# coverage < 0.7): continue`` that the tests above exercise only in
# combination with other true/false legs, so a mutant on one leg can
# still pass because a different leg already forces the same outcome.


def test_title_boost_df_exactly_one_bypasses_but_df_two_does_not(
    skill: SkillHandler,
) -> None:
    """``rare_identity_hit`` must fire on catalogue identity-DF ==
    *exactly* 1, not "not 1" or "exactly 2" — a mutant on either the
    ``==`` or the ``1`` would make a DF==2 word bypass identically to a
    genuine DF==1 word. "pagination" is DF==1 (only
    ``precis-search-help``'s title carries it); "anki" is DF==2
    (``precis-anki-help`` and ``precis-cloze`` both do). Both
    queries pad the same way to well under the 70% coverage bar and
    below the 2-word-match floor, so only the DF==1 bypass can explain
    a difference in outcome.
    """
    out = skill.search(q="pagination banana orange grape mango kiwi")
    assert "title match" in out.body, out.body

    out = skill.search(q="anki banana orange grape mango kiwi")
    assert "title match" not in out.body, out.body


def test_title_boost_single_word_query_bypasses_bar_for_a_common_identity_word(
    skill: SkillHandler,
) -> None:
    """The single-word exemption (``not single`` short-circuits the
    whole bar) must hold even when the query word is *not* a rare
    (DF==1) identity token — "anki" is DF==2, so this can't pass by
    accident via the rare-identity bypass; only the single-word
    exemption explains the boost. Distinct from
    ``test_single_word_query_pins_named_skill``, which only checks the
    slug ranks somewhere and would still pass off a bare lexical hit if
    the boost never fired.
    """
    out = skill.search(q="anki")
    assert "title match" in out.body, out.body


def test_title_boost_two_word_match_at_full_coverage_beats_one_word_match(
    skill: SkillHandler,
) -> None:
    """``len(matched) < 2`` is the exact floor: two matched words at
    full coverage clears the OR (both legs false) and boosts; one
    matched word at reduced coverage trips ``matched < 2`` and is
    blocked. Both queries share the same identity tokens ("anki",
    "cloze" — both DF > 1, so the rare-identity bypass can't explain
    a difference) and the same padding shape, isolating the matched-
    count boundary from the coverage leg.
    """
    out = skill.search(q="anki cloze")
    assert "title match" in out.body, out.body

    out = skill.search(q="anki extra words")
    assert "title match" not in out.body, out.body


# ── hyphenated compounds + family-root pin (skill-search audit 2026-10) ──
#
# The third skill-search review found two under-ranking shapes: a
# hyphenated compound tokenised into generic parts ("child-failed-parked"
# → child/failed/parked, so the skill literally documenting the term lost
# to skills mentioning the parts), and a family-root word ("cad") shared
# by sibling slugs so the DF==1 relief couldn't fire and the root skill
# sank below a pile of section-word matches. The matcher now indexes the
# unsplit compound alongside its parts (weighted above them in the
# lexical leg and the title boost) and pins ``precis-<word>-help`` /
# ``precis-<word>`` when a query token is a ≥2-member slug family's root
# word.


def test_content_tokens_emit_compound_and_parts() -> None:
    """A hyphenated run yields the unsplit compound AND its parts (each
    part under the usual ≥3-char/stopword rules)."""
    from precis.handlers.skill import _content_tokens

    tokens = set(_content_tokens("why is my todo child-failed-parked today"))
    assert "child-failed-parked" in tokens
    assert {"child", "failed", "parked"} <= tokens
    # A compound survives whole even when one part would be dropped
    # (stopword or too short) — it is its own token.
    tokens = set(_content_tokens("pass-dead"))
    assert "pass-dead" in tokens and {"pass", "dead"} <= tokens


def test_family_root_slugs_only_pins_canonical_roots() -> None:
    """``_family_root_slugs`` requires ≥2 members and a canonical root
    slug — a group without ``precis-<word>-help``/``precis-<word>``
    (e.g. claim-adversary + claim-fidelity-help) pins nothing."""
    from precis.handlers.skill import _family_root_slugs, _list_skills

    roots = _family_root_slugs(_list_skills())
    assert roots["cad"] == "precis-cad-help"
    assert roots["job"] == "precis-job-help"  # the compute split's family
    assert roots["mermaid"] == "precis-mermaid-help"  # -help beats the bare slug
    for word in ("anki", "help", "claim", "review", "doi"):
        assert word not in roots, (word, roots.get(word))

    # Synthetic shapes: -help preferred, bare precis-<word> next, a
    # group with no canonical root unpinned, one member is no family.
    slugs = [
        "precis-foo-help",
        "precis-foo-build",
        "precis-bar",
        "precis-bar-x",
        "precis-baz-one",
        "precis-baz-two",
        "precis-solo-help",
        "patent-abstract",
        "patent-claim",
    ]
    roots = _family_root_slugs(slugs)
    assert roots["foo"] == "precis-foo-help"
    assert roots["bar"] == "precis-bar"
    assert "baz" not in roots  # no canonical root slug
    assert "solo" not in roots  # one member — not a family
    assert "patent" not in roots  # only precis-* slugs form families


def test_search_hyphenated_compound_finds_owning_skill(
    skill: SkillHandler,
) -> None:
    """Audit acceptance: ``search(q='child-failed-parked')`` puts
    precis-job-help in the top 2 — the skill that literally documents
    the compound, not skills matching its generic parts."""
    ranked = _ranked_slugs(skill.search(q="child-failed-parked").body)
    assert "precis-job-help" in ranked[:2], ranked[:5]


def test_search_family_root_word_pins_root_skill(skill: SkillHandler) -> None:
    """Audit acceptance: ``cad`` heads the precis-cad-* family, so the
    root skill pins top-2 even though the word is shared by sibling
    slugs (DF>1 — unreachable by the gr259665 bypass)."""
    ranked = _ranked_slugs(
        skill.search(
            q="cad units mm primitives shell hollow frame lattice pattern"
        ).body
    )
    assert "precis-cad-help" in ranked[:2], ranked[:5]


def test_family_root_pin_is_the_root_only_never_siblings(
    skill: SkillHandler,
) -> None:
    """Padding a family-root word to kill coverage still pins the root
    — and pins NOTHING for a sibling slug or a one-member stem."""
    body = skill.search(q="cad banana orange grape mango kiwi").body
    pinned = [
        ln.split("\t", 1)[0].strip()
        for ln in body.splitlines()
        if "\t" in ln and "title match" in ln
    ]
    assert pinned == ["precis-cad-help"], pinned

    out = skill.search(q="anki banana orange grape mango kiwi")
    assert "title match" not in out.body, out.body
