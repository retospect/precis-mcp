"""Index-first skill reads and the skill section eye (gr568475).

Skill reads were long-first: a default ``get(kind='skill', id=<slug>)``
dumped the body to the 24 kB transport cap and put the section outline
at the bottom, so roughly one skill in five truncated on its default
read and the map the reader actually needed came last. This slice makes
skill reads index-first and graduates the skill eye from its two-rung
(bookmark-or-body) special case to the tree-kind ladder:

- a whole-skill read over ``_SKILL_INDEX_FIRST_BYTES`` (~6 kB) serves the
  INDEX — one row per H2 section (``~N`` handle, size, heading, the
  ``answers:`` questions targeting it) — with the body behind
  ``extent='verbatim'`` (``full=true`` is the deprecated spelling,
  gr568808); under the threshold the body serves verbatim;
- ``sk:<slug>~N`` focuses one section through the shared spatial
  fisheye span (verbatim center, graduated summary/kwd neighbours), and a
  whole-skill ``sk:<slug>`` at ``fisheye`` renders the section map, never
  the body.

Synthetic corpus throughout (``tests._skill_corpus``) — no DB, no
embedder.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import _SKILL_INDEX_FIRST_BYTES, SkillHandler
from precis.utils.eye_render import render_eye
from tests._skill_corpus import _install_corpus, _md

_DEEP_0 = "the zeroth section's buried payload sentence"
_DEEP_2 = "the second section's buried payload sentence"


def _big_body() -> str:
    """~4 sections, ~8 kB total — over the index-first threshold."""
    filler = "filler prose line for the section body, repeated to add bulk.\n"
    return (
        "## Resetting the ledger\n"
        + _DEEP_0
        + "\n"
        + filler * 40
        + "## Citing papers\n"
        + filler * 40
        + "## Harvesting cites\n"
        + _DEEP_2
        + "\n"
        + filler * 40
    )


def _big_md() -> str:
    return _md(
        front=(
            "flavor: reference\n"
            "answers:\n"
            "  - how do I reset the ledger?\n"
            "  - what is a totally unrelated question zzqq\n"
        ),
        body=_big_body(),
    )


@pytest.fixture
def skill() -> SkillHandler:
    return SkillHandler(hub=Hub())


# ── index-first whole-skill reads ────────────────────────────────────────


def test_big_default_read_serves_index_not_body(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _big_md()})
    text = _big_md()
    assert len(text.encode("utf-8")) > _SKILL_INDEX_FIRST_BYTES
    out = skill.get(id="a").body
    assert "sections (index)" in out
    assert "a~0" in out and "a~2" in out
    assert "Resetting the ledger" in out
    # The map, not the prose: buried body sentences stay unread.
    assert _DEEP_0 not in out
    assert _DEEP_2 not in out
    # The body stays one call away — the canonical extent spelling
    # (gr568808), not the deprecated full=true — and ranges are taught.
    assert "get(kind='skill', id='a', extent='verbatim')" in out
    assert "get(kind='skill', id='a~0..2')" in out
    assert "a/toc" in out
    # The rev contract (gr477798) is unchanged.
    assert out.rstrip().splitlines()[-1].startswith("skill rev ")


def test_full_true_over_threshold_serves_body(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _big_md()})
    out = skill.get(id="a", full=True).body
    assert "sections (index)" not in out
    assert _DEEP_0 in out
    assert _DEEP_2 in out


def test_full_view_batch_serves_full_body_per_element(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr680237 review (td706444): ``id=[...], view='full'`` used to
    drop ``full`` — the interception recursed with ``view=None`` and
    ``_get_batch`` forwarded only ``view``/``q``, so every element got
    its index-first serve under a false ``interpreted:`` banner. The
    alias now resolves per element: the shown block is the complete
    body, and the paged tail re-issues the same spelling."""
    _install_corpus(monkeypatch, {"a": _big_md(), "b": _big_md()})
    out = skill.get(id=["a", "b"], view="full").body
    assert out.startswith("# 2 skill ids (batch, view='full')")
    assert "sections (index)" not in out
    assert _DEEP_0 in out
    assert _DEEP_2 in out
    assert out.count("interpreted: view='full' read as full=True") == 1
    # The unread tail names the same spelling, not the dropped alias.
    assert "id=['b'], view='full'" in out


def test_under_threshold_serves_verbatim(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _md(body="small body\n")})
    out = skill.get(id="a").body
    assert "sections (index)" not in out
    assert "small body" in out


def test_index_answers_column_targets_sections(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``answers:`` question lands in the row of the section whose
    heading it overlaps; a question no heading claims heads the serve."""
    _install_corpus(monkeypatch, {"a": _big_md()})
    out = skill.get(id="a").body
    assert "how do I reset the ledger?" in out
    # The unclaimed question is listed above the table, not padded onto
    # a wrong row.
    assert "what is a totally unrelated question zzqq" in out


def test_index_alias_h2_collapses_into_the_question_column(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two H2 lines over one body stay ONE row: first alias is the
    heading cell, the second collapses into the answers column."""
    body = "## Writing skills\n## How do I write a skill?\n" + (
        "shared body prose, repeated to add bulk.\n" * 160
    )
    _install_corpus(monkeypatch, {"a": _md(body=body)})
    out = skill.get(id="a").body
    assert "sections (index)" in out
    # The two alias H2s over one body stay ONE row (~0 is the ``# T``
    # head chunk, ~1 the merged alias group) — no ~2 exists.
    assert "a~1" in out
    assert "Writing skills" in out
    assert "How do I write a skill?" in out
    assert "a~2" not in out


def test_have_matched_after_index_read_returns_stub(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _big_md()})
    rev = skill.get(id="a").body.rstrip().splitlines()[-1].removeprefix("skill rev ")
    out = skill.get(id="a", have=rev).body
    assert "unchanged since you read it" in out
    assert _DEEP_0 not in out


def test_batch_get_uses_index_for_big_skills(
    skill: SkillHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_corpus(monkeypatch, {"a": _big_md(), "b": _md(body="small body\n")})
    out = skill.get(id=["a", "b"]).body
    assert "sections (index)" in out
    assert "small body" in out


# ── the skill section eye ────────────────────────────────────────────────


def _tree_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_corpus(
        monkeypatch,
        {
            "a": _md(
                body=(
                    "## Alpha\nalpha body text.\n"
                    "## Beta\nbeta body text.\n"
                    "## Gamma\ngamma body text.\n"
                )
            ),
            "b": _md(body="see [[a]]"),
        },
    )


def test_section_eye_fisheye_renders_graduated_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree_corpus(monkeypatch)
    # ~0 is the ``# T`` head chunk; ~2 is the Beta section.
    out = render_eye(None, "sk:a~2", "fisheye")
    assert out.startswith("sk:a~2 [skill] Beta")
    # verbatim center
    assert "▸ a~2 [section]\n## Beta\nbeta body text." in out
    # ±5 neighbours render verbatim too, each its own copy-pasteable ~N
    # handle; farther ones collapse to ``·`` summary rows.
    assert "a~1 [section]\n## Alpha\nalpha body text." in out
    assert "a~3 [section]\n## Gamma\ngamma body text." in out


def test_section_eye_content_rungs(monkeypatch: pytest.MonkeyPatch) -> None:
    _tree_corpus(monkeypatch)
    kwd = render_eye(None, "sk:a~2", "kwd")
    assert kwd.startswith("· sk:a~2")
    assert "\n" not in kwd
    summary = render_eye(None, "sk:a~2", "summary")
    assert "▸ a~2 [section]" in summary
    assert "beta body text." in summary
    verbatim = render_eye(None, "sk:a~2", "verbatim")
    assert "beta body text." in verbatim
    assert "alpha body text." not in verbatim  # the section alone


def test_section_eye_out_of_range_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _tree_corpus(monkeypatch)
    with pytest.raises(ValueError, match="no section ~9"):
        render_eye(None, "sk:a~9", "fisheye")


def test_section_eye_1hop_appends_the_wikilink_ring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _tree_corpus(monkeypatch)
    out = render_eye(None, "sk:a~2", "fisheye+1hop")
    assert "▸ a~2 [section]" in out
    assert "— linked (1 hop) —" in out
    assert "see-also-by: sk:b" in out


def test_whole_skill_eye_fisheye_is_the_section_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never a page: the whole-skill fisheye eye is one ~N row per
    section; the body shows only at the verbatim rung."""
    _install_corpus(monkeypatch, {"a": _big_md()})
    out = render_eye(None, "sk:a", "fisheye")
    assert out.startswith("sk:a [skill]")
    assert "sections (drill:" in out
    assert "a~0" in out
    assert _DEEP_0 not in out
    assert _DEEP_2 not in out
    # The map row carries the heading, and the questions with it.
    assert "Resetting the ledger" in out
    verbatim = render_eye(None, "sk:a", "verbatim")
    assert _DEEP_0 in verbatim
    # ``extent='verbatim'`` is the complete read (gr568808) — the whole
    # body, never a capped head pointing at the deprecated full=true.
    assert _DEEP_2 in verbatim
    assert "… cut:" not in verbatim
    assert "full=true" not in verbatim


def test_whole_skill_eye_summary_rung_is_the_synopsis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_corpus(
        monkeypatch,
        {"a": _md(front="flavor: reference\nsummary: the one-line gist", body="# T\n")},
    )
    out = render_eye(None, "sk:a", "summary")
    assert "the one-line gist" in out


# ── the eye honours the serve's scope fence and id resolutions ─────────
#
# gr568808 land review: the new section ladder read the raw file — a
# ``scope: write`` skill's eye leaked its recipes under a read
# connection, and ``extent='verbatim'`` — the recovery call the index
# serve advertises — failed for worktree-prefixed ids and served
# aliases because the eye only loaded packaged slugs.


_WRITE_SKILL = (
    "---\nid: w-put\ntitle: W\nsummary: write things\nscope: write\n---\n"
    "# W\n\n## Mutate\n\nhow to write a ref: put(kind='x', text='y')\n"
)
_MIXED_SKILL = (
    "---\nid: mixed\ntitle: M\n---\n"
    "# M\n\n## Read part\n\nhow to read a ref safely\n\n"
    "## Secret write part {write}\n\n"
    "how to write a ref: put(kind='x', text='y')\n"
)


def _scoped_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_corpus(monkeypatch, {"w-put": _WRITE_SKILL, "mixed": _MIXED_SKILL})


@pytest.fixture
def read_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.runtime.scope as scope_mod

    monkeypatch.setenv("PRECIS_MCP_DENY_VERBS", "put,edit,delete,tag,link")
    monkeypatch.setattr(scope_mod, "_boot_read_only", False)
    monkeypatch.setattr(scope_mod, "_serve_read_only", False)


@pytest.fixture
def write_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.runtime.scope as scope_mod

    for name in (
        scope_mod.SERVE_READ_ONLY_ENV,
        "PRECIS_MCP_DENY_VERBS",
        "PRECIS_MCP_READ_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(scope_mod, "_boot_read_only", False)
    monkeypatch.setattr(scope_mod, "_serve_read_only", False)


def test_skill_eye_read_scope_stubs_write_only_skill(
    monkeypatch: pytest.MonkeyPatch, read_scope: None
) -> None:
    """A ``scope: write`` skill's eye serves the same stub ``get``
    does, on every rung above the bookmark — the section handle can't
    drill past it either."""
    _scoped_corpus(monkeypatch)
    for rung in ("summary", "verbatim", "fisheye", "fisheye+1hop"):
        out = render_eye(None, "sk:w-put", rung)
        assert "write recipes omitted" in out, rung
        assert "put(kind='x'" not in out, rung
    out = render_eye(None, "sk:w-put~1", "verbatim")
    assert "write recipes omitted" in out
    assert "put(kind='x'" not in out
    # The bookmark rung stays a bookmark — the stub itself only ever
    # shows title + summary, so the one line leaks nothing.
    assert render_eye(None, "sk:w-put", "kwd").startswith("· sk:w-put [skill]")


def test_skill_eye_write_scope_serves_write_only_body(
    monkeypatch: pytest.MonkeyPatch, write_scope: None
) -> None:
    _scoped_corpus(monkeypatch)
    out = render_eye(None, "sk:w-put", "verbatim")
    assert "put(kind='x'" in out
    assert "write recipes omitted" not in out


def test_skill_eye_read_scope_drops_fenced_section_body(
    monkeypatch: pytest.MonkeyPatch, read_scope: None
) -> None:
    """A ``{write}`` section is a section fence, not a whole-skill one:
    the eye reads the scope-rendered text — same ``~N`` numbering,
    pointer in place of the recipe — on the verbatim body, the section
    map and the section eye itself."""
    _scoped_corpus(monkeypatch)
    body = render_eye(None, "sk:mixed", "verbatim")
    assert "how to read a ref safely" in body
    assert "write recipe omitted" in body
    assert "put(kind='x'" not in body
    # ~2 is the fenced section: heading and address survive, the body
    # is the pointer.
    section = render_eye(None, "sk:mixed~2", "verbatim")
    assert "Secret write part" in section
    assert "write recipe omitted" in section
    assert "put(kind='x'" not in section
    # The map keeps the row (address stability) without quoting it.
    fmap = render_eye(None, "sk:mixed", "fisheye")
    assert "Secret write part" in fmap
    assert "put(kind='x'" not in fmap


def test_skill_eye_write_scope_strips_markers(
    monkeypatch: pytest.MonkeyPatch, write_scope: None
) -> None:
    _scoped_corpus(monkeypatch)
    body = render_eye(None, "sk:mixed~2", "verbatim")
    assert "put(kind='x'" in body
    assert "{write}" not in body
    assert "write recipe omitted" not in body


def test_skill_eye_resolves_served_aliases(skill: SkillHandler) -> None:
    """``extent=`` takes the ids ``get`` takes: a registered served
    alias (gr524267) resolves on the eye — the packaged corpus is the
    real one here, no fixture needed."""
    out = skill.eye(id="precis-part-help", extent="verbatim").body
    assert out.startswith("sk:precis-part-help [skill]")
    # The alias's canonical target minted the section map handles.
    fmap = skill.eye(id="precis-part-help", extent="fisheye").body
    assert "precis-part-select-help~0" in fmap


def test_skill_eye_slash_without_corpus_stays_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``/`` in the handle is only a worktree root when a corpus was
    resolved for it — otherwise it stays one unresolvable slug, never a
    silent repoint at the packaged corpus."""
    _tree_corpus(monkeypatch)
    with pytest.raises(ValueError, match="no live skill"):
        render_eye(None, "sk:wt-alpha/a", "fisheye")
