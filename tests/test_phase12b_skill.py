"""Phase 12b v1 — SkillHandler (filesystem-backed SKILL.md).

Covers:

1.  Directory scan + SKILL.md parsing (standard frontmatter + precis extensions).
2.  Read surface: bare list, single-skill render, /meta / /recent / /kind / /topic
    views, unknown-view rejection with options enrichment.
3.  Search: simple grep over name + description.
4.  Write surface: append (create), replace (overwrite), delete.  Writes are
    confined to ~/.precis/skills/ — attempting to overwrite an ecosystem-
    supplied skill (e.g. one under ~/.claude/skills/) is DENIED.
5.  Kwargs validation: extract_kwargs catches typos on every view.
6.  Registration: `skill` kind is always available (no PG dep, no ImportError
    gating).

Filesystem tests use tmp_path-based scan dirs and a monkeypatched HOME so we
don't touch the user's real ~/.precis/skills/ or ~/.claude/skills/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from precis.handlers.skill import (
    SkillHandler,
    _parse_skill_md,
    _split_frontmatter,
)
from precis.protocol import ErrorCode, PrecisError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIND_PAPER = """\
---
name: find-paper
description: >
  Acquire a scientific paper given a DOI, arXiv id, or title.
  Use when user asks "can you get this paper" or mentions a DOI.
user-invocable: true
argument-hint: [doi, arxiv-id, title]
allowed-tools: [get, put, search]
applies-to: [quest, paper]
kind-onboarding: quest
tags: [papers, research]
---

## When to Use
- Triggers: "get this paper", "acquire DOI 10.x/y"

## Steps
1. Check precis-papers first
2. If absent, enqueue via quest
"""


_TODO_TRIAGE = """\
---
name: todo-triage
description: Bulk triage for accumulated todo items.
user-invocable: true
applies-to: [todo]
tags: [productivity]
---

## When to Use
When you have 5+ open todos.

## Steps
1. List pending todos by priority
2. Close obvious no-longer-relevant ones
"""


_INVALID_YAML = """\
---
name: broken
description: this skill has
 bad: indentation:
  in: [the frontmatter
---
body
"""


_MISSING_NAME = """\
---
description: skill without a name
---
body
"""


def _write_skill(root: Path, slug: str, content: str) -> Path:
    """Create root/<slug>/SKILL.md with the given content."""
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME so ~/.precis/skills/ and ~/.claude/skills/ are sandboxed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    # Path.home() caches; but it uses HOME env on POSIX so the monkeypatch works.
    return fake_home


@pytest.fixture
def handler_with_skills(tmp_path, isolated_home):
    """A SkillHandler pointed at a single tmp scan path with two seed skills."""
    scan_dir = tmp_path / "skills"
    scan_dir.mkdir()
    _write_skill(scan_dir, "find-paper", _FIND_PAPER)
    _write_skill(scan_dir, "todo-triage", _TODO_TRIAGE)
    return SkillHandler(scan_paths=[scan_dir])


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestFrontmatterParsing:
    def test_split_frontmatter_extracts_yaml_and_body(self):
        text = "---\nname: foo\ndescription: bar\n---\nbody text\n"
        fm, body = _split_frontmatter(text)
        assert fm == {"name": "foo", "description": "bar"}
        assert body == "body text\n"

    def test_split_frontmatter_no_frontmatter_returns_empty_dict(self):
        fm, body = _split_frontmatter("just a body")
        assert fm == {}
        assert body == "just a body"

    def test_split_frontmatter_unterminated_returns_empty_dict(self):
        text = "---\nname: foo\n(no closing marker)\n"
        fm, body = _split_frontmatter(text)
        assert fm == {}

    def test_split_frontmatter_invalid_yaml_returns_empty_dict(self):
        # Logs warning and falls back — no crash.
        fm, body = _split_frontmatter(_INVALID_YAML)
        assert fm == {}

    def test_parse_skill_md_with_full_frontmatter(self, tmp_path):
        path = _write_skill(tmp_path, "find-paper", _FIND_PAPER)
        skill = _parse_skill_md(path)
        assert skill is not None
        assert skill.slug == "find-paper"
        assert skill.name == "find-paper"
        assert "scientific paper" in skill.description
        assert skill.user_invocable is True
        assert skill.argument_hint == ["doi", "arxiv-id", "title"]
        assert skill.allowed_tools == ["get", "put", "search"]
        assert skill.applies_to == ["quest", "paper"]
        assert skill.kind_onboarding == "quest"
        assert skill.tags == ["papers", "research"]
        assert "## When to Use" in skill.body

    def test_parse_skill_md_missing_name_falls_back_to_slug(self, tmp_path):
        # Lenient parsing: a skill without frontmatter 'name' still
        # indexes — the directory name is authoritative and becomes the
        # display name.  This keeps agent-authored skills discoverable
        # when the frontmatter is minimal (see mcp-smoke-test-plan §3.3).
        path = _write_skill(tmp_path, "broken", _MISSING_NAME)
        skill = _parse_skill_md(path)
        assert skill is not None
        assert skill.slug == "broken"
        assert skill.name == "broken"
        # description from frontmatter is still preserved when present.
        assert skill.description == "skill without a name"

    def test_parse_skill_md_missing_description_uses_body_first_line(
        self, tmp_path
    ):
        # Description falls back to the first non-blank, non-heading
        # body line so listings render something meaningful.
        text = "---\nname: foo\n---\n# ignore me\n\nFirst body line.\nSecond.\n"
        path = _write_skill(tmp_path, "foo", text)
        skill = _parse_skill_md(path)
        assert skill is not None
        assert skill.description == "First body line."

    def test_parse_skill_md_empty_frontmatter_still_indexes(self, tmp_path):
        # Absolute minimum: a SKILL.md with no frontmatter at all still
        # indexes under the directory name, with the body used for both
        # the body field and the description fallback.
        path = _write_skill(tmp_path, "bareskill", "Just a body line.\n")
        skill = _parse_skill_md(path)
        assert skill is not None
        assert skill.slug == "bareskill"
        assert skill.name == "bareskill"

    def test_parse_skill_md_treats_string_list_fields_as_single_item(self, tmp_path):
        text = (
            "---\n"
            "name: solo\n"
            "description: single-item coercion\n"
            "applies-to: quest\n"  # scalar, not list
            "---\n"
            "body\n"
        )
        path = _write_skill(tmp_path, "solo", text)
        skill = _parse_skill_md(path)
        assert skill is not None
        assert skill.applies_to == ["quest"]


# ---------------------------------------------------------------------------
# Scanning & indexing
# ---------------------------------------------------------------------------


class TestScan:
    def test_scan_finds_all_skills(self, handler_with_skills):
        handler_with_skills._ensure_fresh()
        assert set(handler_with_skills._index) == {"find-paper", "todo-triage"}

    def test_scan_skips_directories_without_skill_md(self, tmp_path, isolated_home):
        scan_dir = tmp_path / "skills"
        scan_dir.mkdir()
        _write_skill(scan_dir, "ok", _TODO_TRIAGE)
        (scan_dir / "empty").mkdir()  # no SKILL.md
        (scan_dir / "notes.txt").write_text("not a skill")  # not a dir
        handler = SkillHandler(scan_paths=[scan_dir])
        handler._ensure_fresh()
        assert set(handler._index) == {"ok"}

    def test_scan_precedence_earlier_path_wins(self, tmp_path, isolated_home):
        # Two scan dirs with the same slug; first one wins.
        first = tmp_path / "a"
        second = tmp_path / "b"
        first.mkdir()
        second.mkdir()
        _write_skill(
            first,
            "find-paper",
            _FIND_PAPER.replace("Acquire a scientific paper", "FIRST wins"),
        )
        _write_skill(
            second,
            "find-paper",
            _FIND_PAPER.replace("Acquire a scientific paper", "SECOND loses"),
        )
        handler = SkillHandler(scan_paths=[first, second])
        handler._ensure_fresh()
        assert "FIRST wins" in handler._index["find-paper"].description

    def test_invalid_yaml_logged_but_does_not_abort_scan(
        self, tmp_path, isolated_home, caplog
    ):
        scan_dir = tmp_path / "skills"
        scan_dir.mkdir()
        _write_skill(scan_dir, "broken", _INVALID_YAML)
        _write_skill(scan_dir, "ok", _TODO_TRIAGE)
        handler = SkillHandler(scan_paths=[scan_dir])
        handler._ensure_fresh()
        # The valid one indexes with its full frontmatter.
        assert "ok" in handler._index
        # Lenient parsing: the broken-YAML skill still indexes via the
        # directory-name fallback, so the user can at least see + edit
        # it rather than having it silently disappear.  The key guarantee
        # here is that the scan does not abort on one bad file.
        assert "broken" in handler._index
        assert handler._index["broken"].name == "broken"

    def test_missing_scan_path_is_tolerated(self, tmp_path, isolated_home):
        missing = tmp_path / "does-not-exist"
        handler = SkillHandler(scan_paths=[missing])
        handler._ensure_fresh()
        assert handler._index == {}


# ---------------------------------------------------------------------------
# Read surface
# ---------------------------------------------------------------------------


def _read(handler: SkillHandler, path: str = "", view=None, subview=None, **kw):
    """Thin wrapper to save on repetitive positional args."""
    return handler.read(
        path=path,
        selector=None,
        view=view,
        subview=subview,
        query=kw.pop("query", ""),
        summarize=False,
        depth=0,
        page=1,
        **kw,
    )


class TestReadSurface:
    def test_bare_call_lists_all(self, handler_with_skills):
        out = _read(handler_with_skills)
        assert "skill:find-paper" in out
        assert "skill:todo-triage" in out

    def test_render_single_skill(self, handler_with_skills):
        out = _read(handler_with_skills, path="find-paper")
        assert "skill:find-paper" in out
        assert "## When to Use" in out  # body included

    def test_render_unknown_slug_raises_id_not_found(self, handler_with_skills):
        with pytest.raises(PrecisError) as exc_info:
            _read(handler_with_skills, path="missing")
        assert exc_info.value.code is ErrorCode.ID_NOT_FOUND

    def test_meta_view_returns_frontmatter_dump(self, handler_with_skills):
        out = _read(handler_with_skills, path="find-paper", view="meta")
        assert "Frontmatter:" in out
        assert "argument-hint" in out

    def test_recent_view_lists_by_mtime(self, handler_with_skills):
        out = _read(handler_with_skills, path="/recent")
        assert "Recent skills" in out
        assert "skill:find-paper" in out

    def test_kind_view_filters_by_applies_to(self, handler_with_skills):
        out = _read(handler_with_skills, path="/kind/quest")
        assert "skill:find-paper" in out  # applies-to: [quest, paper]
        assert "skill:todo-triage" not in out  # applies-to: [todo]

    def test_kind_view_via_parsed_uri_shape(self, handler_with_skills):
        """Regression: ``skill:/kind/quest`` runs through
        :func:`precis.uri.parse`, which splits the leading ``/`` into
        ``(path='', view='kind', subview='quest')``.  The view-dispatch
        branch must NOT pass ``slug=path`` (``=''``) to
        ``_read_kind_view`` — that leaked through as
        ``PARAM_INVALID: unexpected kwarg(s) on skill/kind: slug``
        in live MCP traffic.  See commit introducing this test."""
        out = _read(handler_with_skills, path="", view="kind", subview="quest")
        assert "skill:find-paper" in out
        assert "skill:todo-triage" not in out

    def test_topic_view_via_parsed_uri_shape(self, handler_with_skills):
        """Companion regression for the ``/topic/`` path."""
        out = _read(handler_with_skills, path="", view="topic", subview="papers")
        assert "skill:find-paper" in out

    def test_recent_view_via_parsed_uri_shape(self, handler_with_skills):
        """Companion regression for the ``/recent`` path."""
        out = _read(handler_with_skills, path="", view="recent")
        assert "Recent skills" in out

    def test_kind_view_empty_match_returns_friendly_message(self, handler_with_skills):
        out = _read(handler_with_skills, path="/kind/bogus")
        assert "No skills apply to kind 'bogus'" in out

    def test_topic_view_filters_by_tag(self, handler_with_skills):
        out = _read(handler_with_skills, path="/topic/papers")
        assert "skill:find-paper" in out
        assert "skill:todo-triage" not in out

    def test_unknown_view_raises_with_options_enriched(self, handler_with_skills):
        with pytest.raises(PrecisError) as exc_info:
            _read(handler_with_skills, path="find-paper", view="wibble")
        exc = exc_info.value
        assert exc.code is ErrorCode.VIEW_UNKNOWN
        assert set(exc.options) >= {"meta", "recent", "kind", "topic"}

    def test_kwarg_typo_rejected_on_recent_view(self, handler_with_skills):
        with pytest.raises(PrecisError) as exc_info:
            _read(handler_with_skills, path="/recent", wibblez=1)
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
        assert "wibblez" in exc_info.value.cause


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_matches_description(self, handler_with_skills):
        out = _read(handler_with_skills, query="scientific paper")
        assert "skill:find-paper" in out
        assert "skill:todo-triage" not in out

    def test_search_matches_name(self, handler_with_skills):
        out = _read(handler_with_skills, query="todo-triage")
        assert "skill:todo-triage" in out

    def test_search_is_case_insensitive(self, handler_with_skills):
        out = _read(handler_with_skills, query="SCIENTIFIC")
        assert "skill:find-paper" in out

    def test_search_no_matches_returns_friendly_message(self, handler_with_skills):
        out = _read(handler_with_skills, query="zzzzzz-nope")
        assert "No skills match" in out


# ---------------------------------------------------------------------------
# Write surface
# ---------------------------------------------------------------------------


class TestWriteSurface:
    def test_append_creates_new_skill(self, tmp_path, isolated_home):
        # No seed skills; write into ~/.precis/skills/.
        handler = SkillHandler(scan_paths=[])
        out = handler.put(
            path="new-skill",
            selector=None,
            text=("---\nname: new-skill\ndescription: fresh skill\n---\nbody\n"),
            mode="append",
        )
        assert "skill:new-skill" in out
        # Re-scan and verify it shows up.
        assert "new-skill" in handler._index

    def test_append_rejects_collision(self, tmp_path, isolated_home):
        handler = SkillHandler(scan_paths=[])
        body = "---\nname: x\ndescription: y\n---\n"
        handler.put(path="dup", selector=None, text=body, mode="append")
        with pytest.raises(PrecisError) as exc_info:
            handler.put(path="dup", selector=None, text=body, mode="append")
        assert exc_info.value.code is ErrorCode.ID_AMBIGUOUS

    def test_append_derives_slug_from_frontmatter_name(
        self, tmp_path, isolated_home
    ):
        # smoke-test plan §3.3 convention: id='' with the slug living
        # inside the frontmatter.  ``name:`` (canonical Agent Skills
        # field) drives the destination directory.
        handler = SkillHandler(scan_paths=[])
        out = handler.put(
            path="",
            selector=None,
            text="---\nname: fm-name-skill\ndescription: x\n---\nbody\n",
            mode="append",
        )
        assert "skill:fm-name-skill" in out
        assert "fm-name-skill" in handler._index

    def test_append_derives_slug_from_frontmatter_slug_field(
        self, tmp_path, isolated_home
    ):
        # Backward-compat for the plan's earlier ``slug:`` authoring
        # convention.  Either field works; ``name:`` wins if both are
        # present (tested implicitly via ``_parse_skill_md`` precedence).
        handler = SkillHandler(scan_paths=[])
        out = handler.put(
            path="",
            selector=None,
            text="---\nslug: fm-slug-skill\ndescription: x\n---\nbody\n",
            mode="append",
        )
        assert "skill:fm-slug-skill" in out
        assert "fm-slug-skill" in handler._index

    def test_append_errors_when_no_slug_anywhere(
        self, tmp_path, isolated_home
    ):
        # Still strict when none of id/title/frontmatter provides a slug.
        handler = SkillHandler(scan_paths=[])
        with pytest.raises(PrecisError) as exc_info:
            handler.put(
                path="",
                selector=None,
                text="just a body, no frontmatter",
                mode="append",
            )
        assert exc_info.value.code is ErrorCode.PARAM_INVALID
        assert "slug" in exc_info.value.cause.lower()

    def test_replace_overwrites_writable_skill(self, tmp_path, isolated_home):
        handler = SkillHandler(scan_paths=[])
        body = "---\nname: editable\ndescription: v1\n---\n"
        handler.put(path="editable", selector=None, text=body, mode="append")
        body_v2 = "---\nname: editable\ndescription: v2\n---\n"
        handler.put(path="editable", selector=None, text=body_v2, mode="replace")
        assert handler._index["editable"].description == "v2"

    def test_replace_denied_for_ecosystem_skill(self, tmp_path, isolated_home):
        # Skill lives in a non-writable scan dir (simulating ~/.claude/skills/).
        scan_dir = tmp_path / "foreign"
        scan_dir.mkdir()
        _write_skill(scan_dir, "foreign-skill", _FIND_PAPER)
        handler = SkillHandler(scan_paths=[scan_dir])
        with pytest.raises(PrecisError) as exc_info:
            handler.put(
                path="foreign-skill",
                selector=None,
                text="---\nname: x\ndescription: y\n---\n",
                mode="replace",
            )
        assert exc_info.value.code is ErrorCode.DENIED

    def test_delete_removes_writable_skill(self, tmp_path, isolated_home):
        handler = SkillHandler(scan_paths=[])
        body = "---\nname: goner\ndescription: bye\n---\n"
        handler.put(path="goner", selector=None, text=body, mode="append")
        assert "goner" in handler._index
        handler.put(path="goner", selector=None, text="", mode="delete")
        assert "goner" not in handler._index

    def test_note_mode_is_deferred_to_v1_2(self, handler_with_skills):
        with pytest.raises(PrecisError) as exc_info:
            handler_with_skills.put(
                path="find-paper",
                selector=None,
                text="a note",
                mode="note",
            )
        assert exc_info.value.code is ErrorCode.MODE_UNSUPPORTED

    def test_unknown_mode_rejected(self, handler_with_skills):
        with pytest.raises(PrecisError) as exc_info:
            handler_with_skills.put(
                path="find-paper",
                selector=None,
                text="x",
                mode="wibble",
            )
        assert exc_info.value.code is ErrorCode.MODE_UNSUPPORTED

    def test_tools_put_does_not_leak_tracked_to_skill(
        self, tmp_path, isolated_home
    ):
        """Regression: the MCP ``put`` tool wrapper always sends
        ``tracked=True`` (it's a schema default, not caller-supplied).
        ``tools.put`` used to forward that unconditionally to every
        handler; SkillHandler.put rejects unexpected kwargs via
        ``extract_kwargs``, so every skill write through the MCP
        surface errored:

            PARAM_INVALID: unexpected kwarg(s) on skill put
            mode='append': tracked

        Fixed in ``precis.tools.put`` by only forwarding ``tracked``
        when the target scheme is ``file:``.  This test exercises the
        full ``tools.put`` → ``SkillHandler.put`` path with the
        MCP-default ``tracked=True`` to guard against re-regression."""
        from precis import registry, tools

        # Route the scheme through a fresh handler instance pointed at
        # tmp_path so we don't pollute real skill dirs.
        handler = SkillHandler(scan_paths=[])
        registry._reset_instance_cache()
        registry._SCHEME_INSTANCES["skill"] = handler
        try:
            out = tools.put(
                uri="skill:tracked-leak-regression",
                text=(
                    "---\nname: tracked-leak-regression\n"
                    "description: regression fixture\n---\nbody\n"
                ),
                mode="append",
                tracked=True,  # the schema default from server.py:put
            )
            assert "skill:tracked-leak-regression" in out
            assert "tracked-leak-regression" in handler._index
        finally:
            registry._reset_instance_cache()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_skill_scheme_resolvable(self):
        from precis.registry import resolve

        handler = resolve("skill", path="anything")
        assert isinstance(handler, SkillHandler)

    def test_skill_kind_in_visible_kinds(self):
        from precis.registry import _discover, visible_kinds

        _discover()
        kinds = visible_kinds("get")
        assert any(k.name == "skill" for k in kinds)


# ---------------------------------------------------------------------------
# Phase 12b v1.1: onboarding skill + /help view
# ---------------------------------------------------------------------------


class TestOnboardingSkillAttribute:
    def test_handler_base_defaults_to_none(self):
        from precis.protocol import Handler

        assert Handler.onboarding_skill is None

    def test_flashcard_declares_sm2_basics(self):
        from precis.handlers.flashcard import FlashcardHandler

        assert FlashcardHandler.onboarding_skill == "sm2-basics"

    def test_todo_declares_todo_triage(self):
        from precis.handlers.todo import TodoHandler

        assert TodoHandler.onboarding_skill == "todo-triage"

    def test_tex_declares_tex_workflow(self):
        from precis.handlers.tex import TexHandler

        assert TexHandler.onboarding_skill == "tex-workflow"


class TestBundledSeedSkills:
    """Seed skills ship with the package at src/precis/skills/."""

    def test_builtin_path_exists(self):
        from precis.handlers.skill import _builtin_skills_path

        assert _builtin_skills_path().is_dir()

    def test_all_three_seeds_are_discoverable(self):
        # Default scan paths include the bundled dir at the end.
        handler = SkillHandler()
        handler._ensure_fresh()
        assert "sm2-basics" in handler._index
        assert "todo-triage" in handler._index
        assert "tex-workflow" in handler._index

    def test_seed_skill_has_onboarding_marker(self):
        handler = SkillHandler()
        handler._ensure_fresh()
        # The bundled sm2-basics skill's kind-onboarding frontmatter
        # names the canonical ``flashcard`` kind (the short ``fc`` alias
        # was retired — see Apr 2026 registry cleanup).
        assert handler._index["sm2-basics"].kind_onboarding == "flashcard"

    def test_seed_skill_applies_to_matches_kind(self):
        handler = SkillHandler()
        handler._ensure_fresh()
        # tex-workflow applies to tex
        assert "tex" in handler._index["tex-workflow"].applies_to


class TestRefHandlerHelpView:
    """RefHandler exposes /help when onboarding_skill is declared."""

    def test_help_view_registered(self):
        from precis.handlers._ref_base import RefHandler

        assert "help" in RefHandler.views
        assert RefHandler.views["help"] == "_read_help_view"

    def test_help_view_renders_bundled_skill(self):
        """Calling FlashcardHandler._read_help_view returns the sm2-basics body."""
        from precis.handlers.flashcard import FlashcardHandler

        handler = FlashcardHandler()
        out = handler._read_help_view(store=None, ref=None, selector=None, subview=None)
        assert "skill:sm2-basics" in out
        assert "SM-2 quality scale" in out

    def test_help_view_raises_when_onboarding_unset(self):
        """A RefHandler without onboarding_skill refuses /help gracefully."""
        from precis.handlers._ref_base import RefHandler

        class _NoSkill(RefHandler):
            scheme = "noskill"

        handler = _NoSkill()
        with pytest.raises(PrecisError) as exc_info:
            handler._read_help_view(store=None, ref=None, selector=None, subview=None)
        assert exc_info.value.code is ErrorCode.VIEW_UNKNOWN
        assert "no onboarding skill" in exc_info.value.cause

    def test_help_view_raises_when_skill_file_missing(self, tmp_path, isolated_home):
        """Declared onboarding slug that has no SKILL.md on disk fails cleanly."""
        from precis.handlers._ref_base import RefHandler

        class _MissingSkill(RefHandler):
            scheme = "missing"
            onboarding_skill = "does-not-exist"

        # Sandbox: no bundled path either (monkey-patch _default_scan_paths).
        import precis.handlers.skill as skill_mod

        original = skill_mod._default_scan_paths
        skill_mod._default_scan_paths = lambda: [tmp_path / "empty"]
        try:
            handler = _MissingSkill()
            with pytest.raises(PrecisError) as exc_info:
                handler._read_help_view(
                    store=None, ref=None, selector=None, subview=None
                )
            assert exc_info.value.code is ErrorCode.ID_NOT_FOUND
            assert "does-not-exist" in exc_info.value.cause
        finally:
            skill_mod._default_scan_paths = original

    def test_help_view_rejects_unknown_kwarg(self):
        from precis.handlers.flashcard import FlashcardHandler

        handler = FlashcardHandler()
        with pytest.raises(PrecisError) as exc_info:
            handler._read_help_view(
                store=None, ref=None, selector=None, subview=None, wibblez=1
            )
        assert exc_info.value.code is ErrorCode.PARAM_INVALID


class TestEnrichErrorSkillPointer:
    """_enrich_error appends 'see skill:<slug>' on agent-confusion codes."""

    def test_pointer_appended_to_empty_next(self):
        from precis.protocol import CallContext
        from precis.registry import _enrich_error

        class _H:
            scheme = "demo"
            writable = True
            views = {"toc": "_read_toc_view"}
            allowed_modes: set = {"append"}
            onboarding_skill = "demo-workflow"

        exc = PrecisError(ErrorCode.MODE_UNSUPPORTED, cause="mode wibble")
        ctx = CallContext(kind="demo", verb="put")
        options, next_hint = _enrich_error(exc, _H(), ctx)
        assert "skill:demo-workflow" in next_hint

    def test_pointer_appended_after_existing_next(self):
        from precis.protocol import CallContext
        from precis.registry import _enrich_error

        class _H:
            scheme = "demo"
            writable = True
            views = {"toc": "_read_toc_view"}
            allowed_modes: set = {"append"}
            onboarding_skill = "demo-workflow"

        exc = PrecisError(
            ErrorCode.PARAM_INVALID,
            cause="bad arg",
            next="use --thing=foo",
        )
        ctx = CallContext(kind="demo", verb="get")
        _options, next_hint = _enrich_error(exc, _H(), ctx)
        assert next_hint.startswith("use --thing=foo")
        assert "skill:demo-workflow" in next_hint
        assert ";" in next_hint

    def test_pointer_not_appended_on_id_not_found(self):
        """ID_NOT_FOUND wants a search, not a workflow primer."""
        from precis.protocol import CallContext
        from precis.registry import _enrich_error

        class _H:
            scheme = "demo"
            writable = True
            views = {"recent": "_read_recent_view"}
            allowed_modes: set = set()
            onboarding_skill = "demo-workflow"

        exc = PrecisError(ErrorCode.ID_NOT_FOUND, cause="ref 'x' not in corpus")
        ctx = CallContext(kind="demo", verb="get")
        _options, next_hint = _enrich_error(exc, _H(), ctx)
        # Agent gets a /recent pointer (kind has a recent view) — not the skill.
        assert "skill:demo-workflow" not in next_hint
        assert "/recent" in next_hint

    def test_pointer_not_appended_when_handler_lacks_attribute(self):
        from precis.protocol import CallContext
        from precis.registry import _enrich_error

        class _H:
            scheme = "demo"
            writable = True
            views = {"toc": "_read_toc_view"}
            allowed_modes: set = {"append"}
            # no onboarding_skill

        exc = PrecisError(ErrorCode.MODE_UNSUPPORTED, cause="mode wibble")
        ctx = CallContext(kind="demo", verb="put")
        _options, next_hint = _enrich_error(exc, _H(), ctx)
        assert "skill:" not in next_hint


class TestHelpViewE2E:
    """End-to-end: get(id='fc:/help') returns the onboarding skill body."""

    def test_fc_help_via_views_dispatch(self):
        """Route through RefHandler.read() → _read_help_view."""
        from precis.handlers.flashcard import FlashcardHandler

        handler = FlashcardHandler()
        # We bypass the normal read() path (which resolves a ref) and call
        # the dispatcher directly — /help doesn't need a ref.
        method_name = handler.views["help"]
        out = getattr(handler, method_name)(
            store=None, ref=None, selector=None, subview=None
        )
        assert "skill:sm2-basics" in out


class TestFileBaseHelpView:
    """FileHandlerBase routes /help via its inline if-ladder."""

    def test_tex_help_raises_not_found_without_real_file(self, tmp_path):
        """The /help branch runs before file resolution, but _resolve_path
        fires first — check that a real .tex unlocks /help.
        """
        from precis.handlers.tex import TexHandler

        tex_file = tmp_path / "paper.tex"
        tex_file.write_text(
            "\\documentclass{article}\n\\begin{document}\nx\n\\end{document}\n"
        )
        handler = TexHandler()
        out = handler.read(
            path=str(tex_file),
            selector=None,
            view="help",
            subview=None,
            query="",
            summarize=False,
            depth=0,
            page=1,
        )
        assert "skill:tex-workflow" in out
        assert "tex:" in out or "LaTeX" in out
