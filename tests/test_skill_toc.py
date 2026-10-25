"""SkillHandler toc — listing, categories menu, tag/kind/family filters and bucketing (split out of test_skill.py, gr515135)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.skill import SkillHandler
from tests._skill_corpus import _install_corpus, _md


@pytest.fixture
def skill() -> SkillHandler:
    """SkillHandler doesn't need a store; an empty hub is enough."""
    return SkillHandler(hub=Hub())


# ── synthesized precis-toc skill ─────────────────────────────────────


def test_toc_alias_dispatches_to_precis_toc(skill: SkillHandler) -> None:
    """Both ``id='toc'`` and ``id='precis-toc'`` render the same body.

    The alias is registered in ``_SYNTH_ALIASES`` so a 7B caller
    typing the natural ``toc`` lands on the same TOC view as the
    canonical slug.
    """
    short = skill.get(id="toc").body
    full = skill.get(id="precis-toc").body
    assert short == full


def test_toc_lists_every_skill_with_synopsis(skill: SkillHandler) -> None:
    """The TOC names every shipping skill grouped by category.

    Round-2 picky 2026-05-30: the layout was restructured from a
    flat ``## Meta-skills`` + ``## Skills`` two-bucket shape into the
    five-category top layer (Orientation / Core verbs / Content
    types / Research & validation / Workflow tools). Synth
    meta-skills now live inside Orientation; file-backed skills land
    in whichever category their slug is registered under
    (``_SKILL_CATEGORIES``). Anything unmapped surfaces in
    ``## Other`` so new slugs never silently disappear.
    """
    out = skill.get(id="toc")
    body = out.body
    # Top-layer category headings — at least the first two must show
    # up; the rest depend on which kinds are wired in the fixture
    # hub and could legitimately drop out.
    assert "## Orientation" in body
    assert "## Core verbs" in body
    # Every synth meta-skill appears under Orientation.
    assert "precis-help" in body
    assert "precis-status" in body
    assert "precis-toc" in body
    # The canonical entry-point skill is listed.
    assert "precis-overview" in body
    # Discovery footer ("Suggested starting commands") and at least
    # one search recipe inside it.
    assert "Suggested starting commands" in body
    assert "search(kind='skill'" in body


def test_toc_suggested_commands_paste_verbatim(skill: SkillHandler) -> None:
    """hint-audit item 5: the TOC's "Suggested starting commands" table
    promises "paste verbatim to land somewhere useful", but included
    ``get(kind='skill', id='<slug>')`` — rejected by ``_SKILL_ID_RE``
    (``<``/``>`` aren't valid slug chars). Replaced with a real,
    always-valid slug (``toc``) that actually dispatches."""
    out = skill.get(id="toc")
    assert "id='<slug>'" not in out.body
    assert "get(kind='skill', id='toc')" in out.body
    # And it genuinely dispatches (the "paste verbatim" promise held).
    assert skill.get(id="toc").body


def test_scoped_toc_hint_uses_scope_lower_bound_not_hardcoded_zero(
    skill: SkillHandler,
) -> None:
    """hint-audit item 5: the scoped-TOC chunk hint hardcoded ``~0``
    claiming it was "from above" even when the scoped listing doesn't
    start at 0. Discover a real non-zero position from the unscoped
    TOC, scope to it, and confirm the hint anchors on THAT position."""
    unscoped = skill.get(id="precis-overview/toc").body
    import re

    positions = sorted({int(m) for m in re.findall(r"precis-overview~(\d+)", unscoped)})
    nonzero = [p for p in positions if p > 0]
    assert nonzero, "fixture skill needs a chunk position > 0 for this test"
    lo = nonzero[0]

    scoped = skill.get(id=f"precis-overview~{lo}..{lo}/toc").body
    assert f"id='precis-overview~{lo}')" in scoped
    assert "id='precis-overview~0')" not in scoped


def test_toc_listed_in_bare_index_hint(skill: SkillHandler) -> None:
    """``get(kind='skill')`` (no id) hints at ``id='toc'`` in the
    Next-Up trailer so an agent navigating the index lands on the
    TOC immediately rather than scrolling the raw slug list."""
    out = skill.get()
    assert "id='toc'" in out.body


def test_toc_opens_with_categories_menu(skill: SkillHandler) -> None:
    """gr514221 — the unfiltered toc is ~34 KB, so a folded/paginated
    first page never reached the rows an agent wanted. The body now
    opens with a compact Categories menu naming each group and its
    drill arg, so page 1 alone carries the whole decision surface."""
    body = skill.get(id="toc").body
    head = body[:3000]
    assert "## Categories" in head
    for name in ("Orientation", "Core verbs", "Drafting & figures"):
        assert name in head
    assert "args={'category': 'drafting'}" in head


def test_toc_category_drill_serves_one_group(skill: SkillHandler) -> None:
    """``args={'category': …}`` (flattened to the ``category=`` kwarg
    by dispatch) re-serves only that group's table — the targeted
    read the compact menu advertises."""
    body = skill.get(id="toc", category="drafting").body
    assert "## Drafting & figures" in body
    assert "precis-draft-help" in body
    # Other groups' sections are not rendered.
    assert "## Orientation" not in body


def test_toc_category_unknown_lists_options(skill: SkillHandler) -> None:
    """An unmatched category raises BadInput naming the real category
    list — the agent learns the enum from one round-trip."""
    with pytest.raises(BadInput) as excinfo:
        skill.get(id="toc", category="zzz-no-such")
    assert excinfo.value.options is not None
    assert "Orientation" in excinfo.value.options


def test_toc_category_ambiguous_is_refused(skill: SkillHandler) -> None:
    """A substring matching several categories refuses rather than
    guessing — 'e' appears inside most category names."""
    with pytest.raises(BadInput):
        skill.get(id="toc", category="e")


def test_toc_category_names_cover_every_skill(skill: SkillHandler) -> None:
    """Ratchet: the union of every category's drilled table still
    lists every available skill — the Categories menu must not strand
    a slug on no page."""
    from precis.handlers.skill import _list_skills

    body = skill.get(id="toc").body
    assert "## Categories" in body
    for slug in _list_skills():
        assert slug in body, f"{slug} missing from the toc"


def test_toc_decision_surface_fits_page_one(skill: SkillHandler) -> None:
    """gr514221 — the shipped design keeps the unfiltered toc ~35 KB
    but puts the whole decision surface on page 1. Two budgets pin
    that: the Categories menu must end inside the fold head (the
    write-time fold keeps ~¾ of ``DEFAULT_FOLD_BODY_BYTES`` as head —
    a menu past it truncates mid-table, the original symptom), and
    every drill arg the menu advertises must re-serve within one
    frame page (``DEFAULT_MAX_BODY_BYTES``)."""
    import re

    from precis._pagination import (
        DEFAULT_FOLD_BODY_BYTES,
        DEFAULT_MAX_BODY_BYTES,
    )

    body = skill.get(id="toc").body
    menu_start = body.find("## Categories")
    menu_end = body.find("\n## ", menu_start)
    assert menu_start != -1 and menu_end != -1
    head_budget = DEFAULT_FOLD_BODY_BYTES * 3 // 4
    assert menu_end <= head_budget, (
        f"Categories menu ends at byte {menu_end}, past the fold head "
        f"({head_budget}) — page 1 would cut the drill table mid-menu"
    )
    drills = re.findall(r"args=\{'category': '([^']+)'\}", body[:menu_end])
    assert drills, "Categories menu advertises no drill args"
    for drill in drills:
        page = skill.get(id="toc", category=drill).body
        size = len(page.encode("utf-8"))
        assert size <= DEFAULT_MAX_BODY_BYTES, (
            f"category drill {drill!r} serves {size} bytes — over one "
            f"frame page ({DEFAULT_MAX_BODY_BYTES})"
        )


def test_toc_filter_by_tag_actually_filters(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "zzq-orient": _md(
                front="flavor: reference\ntags:\n  - orientation", body="body"
            ),
            "zzq-workflow": _md(
                front="flavor: reference\ntags:\n  - workflow", body="body"
            ),
        },
    )
    out = skill.get(id="toc", tag="orientation")
    assert "zzq-orient" in out.body
    assert "zzq-workflow" not in out.body
    # negative check the other direction too
    out2 = skill.get(id="toc", tag="workflow")
    assert "zzq-workflow" in out2.body
    assert "zzq-orient" not in out2.body


def test_toc_filter_by_kinds_actually_filters(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "zzq-paper": _md(front="flavor: reference\nkinds:\n  - paper", body="body"),
            "zzq-patent": _md(
                front="flavor: reference\nkinds:\n  - patent", body="body"
            ),
        },
    )
    out = skill.get(id="toc", kinds="paper")
    assert "zzq-paper" in out.body
    assert "zzq-patent" not in out.body
    out2 = skill.get(id="toc", kinds="patent")
    assert "zzq-patent" in out2.body
    assert "zzq-paper" not in out2.body


def test_toc_filter_by_family_actually_filters(skill: SkillHandler) -> None:
    """The kwarg spelling that exercises what ``args={'family': …}``
    means: the toc narrows to one family (gr514727's intended test)."""
    out = skill.get(id="toc", family="se")
    assert "precis-se-help" in out.body
    assert "precis-paper-help" not in out.body


def test_toc_filter_rejects_unknown_tag(skill: SkillHandler) -> None:
    """An unknown ``tag=`` raises listing the valid vocabulary instead
    of returning a silent "0 skills" toc (gr338978) — a typo'd tag must
    be distinguishable from a valid-but-empty slice."""
    with pytest.raises(BadInput) as excinfo:
        skill.get(id="toc", tag="bogus-tag")
    msg = str(excinfo.value)
    assert "unknown tag 'bogus-tag'" in msg
    assert "orientation" in msg and "workflow" in msg


def test_toc_filter_tag_naming_a_kind_remaps_to_kinds_axis(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tag='<kind>'`` is a predictable slip (gr504487 — agents write
    ``tag='se'`` for "the se skills"): the value is honored on the
    ``kinds=`` axis rather than refused with the tag vocabulary."""
    _install_corpus(
        monkeypatch,
        {
            "zzq-paper": _md(front="flavor: reference\nkinds:\n  - paper", body="body"),
            "zzq-patent": _md(
                front="flavor: reference\nkinds:\n  - patent", body="body"
            ),
        },
    )
    out = skill.get(id="toc", tag="paper")
    assert "zzq-paper" in out.body
    assert "zzq-patent" not in out.body


def test_toc_filter_tag_naming_a_kind_code_remaps_to_its_kind(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tag='me'`` (a handle code, not a kind name) resolves to its long
    kind the same way the ``kinds=`` code slip does."""
    _install_corpus(
        monkeypatch,
        {
            "zzq-mem": _md(front="flavor: reference\nkinds:\n  - memory", body="body"),
            "zzq-paper": _md(front="flavor: reference\nkinds:\n  - paper", body="body"),
        },
    )
    out = skill.get(id="toc", tag="me")
    assert "zzq-mem" in out.body
    assert "zzq-paper" not in out.body


def test_toc_filter_rejects_unknown_kind(skill: SkillHandler) -> None:
    with pytest.raises(BadInput) as excinfo:
        skill.get(id="toc", kinds="nonexistent-kind")
    assert "unknown kind 'nonexistent-kind'" in str(excinfo.value)


def test_toc_filter_short_code_redirects_to_long_name(
    skill: SkillHandler,
) -> None:
    """``kinds='me'`` (a handle code) names the long kind it stands for
    rather than a bare rejection — the footer prints ``memory (me)``, so
    the code is a predictable wrong guess."""
    with pytest.raises(BadInput) as excinfo:
        skill.get(id="toc", kinds="me")
    assert "'me' is the code for 'memory'" in str(excinfo.value)
    assert "kinds='memory'" in str(excinfo.value.next)


def test_toc_filter_known_tag_with_no_skills_is_empty_not_error(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A *known* tag whose slice is empty still renders the honest
    0-skill toc — only out-of-vocabulary values are rejected."""
    _install_corpus(
        monkeypatch,
        {
            "zzq-orient": _md(
                front="flavor: reference\ntags:\n  - orientation", body="b"
            )
        },
    )
    out = skill.get(id="toc", tag="design")
    assert "0 skills" in out.body


def test_index_filter_by_tag_actually_filters(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(
        monkeypatch,
        {
            "aaa-orient": _md(
                front="flavor: reference\ntags:\n  - orientation", body="body"
            ),
            "bbb-workflow": _md(
                front="flavor: reference\ntags:\n  - workflow", body="body"
            ),
        },
    )
    out = skill.get(tag="orientation")
    assert "aaa-orient" in out.body
    assert "bbb-workflow" not in out.body


def test_categorise_buckets_the_rest_by_tag_not_into_other() -> None:
    """gr334773: ~120 skills sat in one 'Other' bucket. Everything the
    curated list doesn't name is grouped by its first recognised
    ``tags:`` value; a tag-group sharing a curated name folds into it;
    only untagged skills stay in Other."""
    from precis.handlers.skill import _categorise_skills

    tags = {
        "precis-overview": ["orientation"],
        "precis-se-help": ["design"],
        "precis-hexfold-help": ["workflow", "design"],
        "precis-figure-help": ["drafting"],
        "precis-doctor-help": ["troubleshooting", "workflow"],
        "precis-cache": [],
        "precis-mystery": [],
    }
    groups, other = _categorise_skills(list(tags), tags_for=lambda s: tags[s])
    as_dict = dict(groups)
    assert as_dict["Orientation"] == ["precis-overview"]  # curated wins
    assert as_dict["Design & engineering"] == ["precis-hexfold-help", "precis-se-help"]
    assert as_dict["Drafting & figures"] == ["precis-figure-help"]
    assert as_dict["Troubleshooting"] == ["precis-doctor-help"]
    assert as_dict["Workflow tools"] == ["precis-cache"]  # curated, untagged
    assert other == ["precis-mystery"]
    # Derived groups come after the curated ones, in _TAG_GROUPS order.
    names = [n for n, _ in groups]
    assert names.index("Workflow tools") < names.index("Design & engineering")
    assert names.index("Design & engineering") < names.index("Drafting & figures")


def test_toc_other_bucket_is_small(skill: SkillHandler) -> None:
    """The live catalogue: with tag-derived groups, 'Other' holds only
    skills lacking a recognised tag — a handful, not a hundred."""
    import re

    body = skill.get(id="toc").body
    assert "## Design & engineering" in body
    m = re.search(r"## Other \((\d+)\)", body)
    assert m is None or int(m.group(1)) <= 15, m.group(0) if m else None


def test_chunks_for_toc_unknown_skill_names_the_toc(skill: SkillHandler) -> None:
    """M3 burn-down (gr494831): an unknown skill slug carries next= to toc."""

    with pytest.raises(NotFound) as exc:
        skill.chunks_for_toc("no-such-skill-xyz")
    assert exc.value.next is not None
    assert "get(kind='skill', id='toc')" in str(exc.value.next)
