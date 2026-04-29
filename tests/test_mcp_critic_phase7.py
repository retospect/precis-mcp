"""Regressions for the second MCP critic pass (phase-7 build).

Covers the CRITICAL / MAJOR / MINOR findings the critic flagged
against the live ``precis2`` MCP build:

  CRITICAL #1  ``rel=`` / ``unlink=`` / ``untags=`` flow through dispatch
  CRITICAL #2  unknown ``mode=`` values reject loudly, not silently
  CRITICAL #3  skill listing filters skills documenting unregistered kinds
  MAJOR  #6   ``precis-density`` filtered (status: planned)
  MAJOR  #7   ``precis-navigation`` filtered (status: aspirational)
  MAJOR  #8   unregistered ``UPPERCASE:`` tag prefixes are rejected
  MAJOR  #9   paper default overview strips ``<jats:*>`` like view='abstract'
  MAJOR  #10  ``top_k`` capped at 100, larger values rejected
  MAJOR  #11  search skips blocks shorter than 4 chars
  MAJOR  #12  cross-kind error options filtered to verb-supporting kinds
  MAJOR  #13  ``mode='note'`` removed from advertised list
  MINOR  m1   strip drops ``<jats:title>Abstract</jats:title>`` outright
  MINOR  m2   empty-list responses on oracle/conv/quest carry Next: trailers
  MINOR  m3   underscore-in-paper-slug names the offending char
  MINOR  m4   calc rejects expressions that simplify to themselves
  MINOR  m6   single-block trailers render ``~N`` not ``~N..N``

Tests addressing items already covered by earlier suites
(``test_link_crud``, ``test_search_tag_filter``, etc.) are not
duplicated here — this file is the post-critic regression net,
not a comprehensive surface re-test.
"""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.handlers.calc import CalcHandler
from precis.handlers.memory import MemoryHandler
from precis.handlers.paper import PaperHandler
from precis.handlers.skill import (
    SkillHandler,
    _availability_gap,
    _parse_frontmatter,
)
from precis.runtime import PrecisRuntime
from precis.store import BlockInsert, Store, Tag

# ── helpers ────────────────────────────────────────────────────────


def _seed_paper(store: Store, slug: str = "wang2020state", n_blocks: int = 4) -> int:
    cid = store.ensure_corpus("default")
    ref = store.insert_ref(
        corpus_id=cid,
        kind="paper",
        slug=slug,
        title="Test paper",
        provider="manual",
        meta={},
    )
    if n_blocks:
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(pos=i, text=f"block {i} content text", slug=f"b{i}")
                for i in range(n_blocks)
            ],
        )
    return ref.id


# ── CRITICAL #2: mode= validation ─────────────────────────────────


class TestUnknownModeRejected:
    """``mode='untag'`` / ``'unlink'`` / ``'note'`` no longer silently
    no-op. The numeric-ref handlers (memory, todo, gripe, fc, …)
    accept only ``mode='delete'`` (or absence)."""

    def test_mode_untag_rejected(self, store: Store) -> None:
        h = MemoryHandler(store=store)
        m = h.put(text="m")
        rid = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(BadInput, match="unknown mode 'untag'"):
            h.put(id=rid, mode="untag", tags=["topic-x"])

    def test_mode_unlink_rejected(self, store: Store) -> None:
        h = MemoryHandler(store=store)
        m = h.put(text="m")
        rid = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(BadInput, match="unknown mode 'unlink'"):
            h.put(id=rid, mode="unlink")

    def test_mode_note_rejected(self, store: Store) -> None:
        h = MemoryHandler(store=store)
        m = h.put(text="m")
        rid = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(BadInput, match="unknown mode 'note'"):
            h.put(id=rid, mode="note", text="annotation")

    def test_mode_typo_rejected(self, store: Store) -> None:
        """Even a typo of the supported mode is caught."""
        h = MemoryHandler(store=store)
        with pytest.raises(BadInput, match="unknown mode"):
            h.put(text="m", mode="deelete")

    def test_mode_delete_still_works(self, store: Store) -> None:
        h = MemoryHandler(store=store)
        m = h.put(text="m")
        rid = int(m.body.split("=")[-1].strip().split()[0])
        out = h.put(id=rid, mode="delete")
        assert "deleted" in out.body


# ── CRITICAL #3 + MAJOR #6/#7: skill index filtering ──────────────


class TestSkillIndexFiltering:
    def test_density_marked_planned(self) -> None:
        """``precis-density`` documents three views the runtime
        rejects; front-matter must say so."""
        from importlib import resources

        text = (resources.files("precis.data.skills") / "precis-density.md").read_text(
            encoding="utf-8"
        )
        fm = _parse_frontmatter(text)
        assert fm.get("status") == "planned"

    def test_navigation_marked_aspirational(self) -> None:
        from importlib import resources

        text = (
            resources.files("precis.data.skills") / "precis-navigation.md"
        ).read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        assert fm.get("status") == "aspirational"

    def test_files_help_marked_planned(self) -> None:
        """``precis-files-help`` documents kinds (markdown/python/…) not
        currently in the runtime registry."""
        from importlib import resources

        text = (
            resources.files("precis.data.skills") / "precis-files-help.md"
        ).read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        assert fm.get("status") == "planned"

    def test_availability_gap_filters_status_planned(
        self, runtime: PrecisRuntime
    ) -> None:
        gap = _availability_gap("precis-density", registry=runtime.registry)
        assert gap is not None
        assert "planned" in gap.lower()

    def test_availability_gap_filters_unregistered_kind_help(
        self, runtime: PrecisRuntime
    ) -> None:
        # ``markdown`` kind is not in the test runtime's registry.
        gap = _availability_gap("precis-markdown-help", registry=runtime.registry)
        assert gap is not None
        assert "not" in gap.lower() and "wired" in gap.lower()

    def test_availability_gap_passes_active_skill(self, runtime: PrecisRuntime) -> None:
        # ``precis-overview`` is cross-cutting and active.
        gap = _availability_gap("precis-overview", registry=runtime.registry)
        assert gap is None

    def test_availability_gap_passes_active_help(self, runtime: PrecisRuntime) -> None:
        # ``memory`` is in every test fixture's registry.
        gap = _availability_gap("precis-memory-help", registry=runtime.registry)
        assert gap is None

    def test_index_omits_filtered_skills(self, runtime: PrecisRuntime) -> None:
        """The bare ``get(kind='skill')`` index doesn't list
        ``precis-density`` or ``precis-navigation``."""
        h = next(
            x for x in runtime.registry._by_kind.values() if isinstance(x, SkillHandler)
        )
        out = h.get()
        assert "precis-density" not in out.body
        assert "precis-navigation" not in out.body
        # But the hidden-count footer notes them.
        assert "non-active skills hidden" in out.body

    def test_filtered_skill_still_retrievable_with_banner(
        self, runtime: PrecisRuntime
    ) -> None:
        """Direct slug fetch still works and prepends a heads-up banner."""
        h = next(
            x for x in runtime.registry._by_kind.values() if isinstance(x, SkillHandler)
        )
        out = h.get(id="precis-density")
        assert "Heads up" in out.body
        # The original content is still there.
        assert "DENSITY" in out.body or "representatives" in out.body


# ── MAJOR #8: unregistered UPPERCASE: prefixes rejected ──────────


class TestUnregisteredPrefixesRejected:
    def test_density_prefix_rejected(self) -> None:
        with pytest.raises(BadInput, match="unknown closed-prefix axis"):
            Tag.parse_strict("DENSITY:sparse")

    def test_confidence_prefix_rejected(self) -> None:
        with pytest.raises(BadInput, match="unknown closed-prefix axis"):
            Tag.parse_strict("CONFIDENCE:moderate")

    def test_typo_prefix_rejected(self) -> None:
        """``STATSU:`` (typo of STATUS) is a different prefix and
        must reject — that's the whole point of the strict check."""
        with pytest.raises(BadInput, match="unknown closed-prefix axis"):
            Tag.parse_strict("STATSU:open")

    def test_registered_prefix_still_validates(self) -> None:
        # STATUS: with bad value still rejects (existing contract).
        with pytest.raises(BadInput, match="invalid STATUS value"):
            Tag.parse_strict("STATUS:bogus")
        # STATUS: with good value still accepts.
        t = Tag.parse_strict("STATUS:done")
        assert t.namespace == "closed"

    def test_lowercase_open_tag_unaffected(self) -> None:
        """``confidence-strong`` (lowercase, hyphen-separated) is a
        plain open tag and always accepted."""
        t = Tag.parse_strict("confidence-strong")
        assert t.namespace == "open"


# ── MAJOR #9 / MINOR m1: paper overview strips JATS ──────────────


class TestPaperOverviewStripsJats:
    def test_overview_no_jats_in_body(self, store: Store) -> None:
        h = PaperHandler(store=store)
        cid = store.ensure_corpus("default")
        store.insert_ref(
            corpus_id=cid,
            kind="paper",
            slug="jats-test",
            title="Test",
            provider="manual",
            meta={
                "abstract": (
                    "<jats:title>Abstract</jats:title>"
                    "<jats:p>Metal-organic frameworks (MOFs) represent…</jats:p>"
                ),
            },
        )
        out = h.get(id="jats-test")
        assert "<jats:" not in out.body
        assert "</jats:" not in out.body
        # And the heading-word doesn't fuse with the body's first word.
        assert not any(
            line.startswith("AbstractMetal") for line in out.body.splitlines()
        )


# ── MAJOR #11: search noise floor (skip <4-char blocks) ─────────


class TestSearchNoiseFloor:
    def test_short_blocks_excluded_from_lexical(self, store: Store) -> None:
        cid = store.ensure_corpus("default")
        ref = store.insert_ref(corpus_id=cid, kind="paper", slug="p", title="P")
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(pos=0, text="."),
                BlockInsert(pos=1, text=","),
                BlockInsert(pos=2, text="abc"),
                BlockInsert(pos=3, text="real content with words"),
            ],
        )
        hits = store.search_blocks_lexical(q="content", kind="paper")
        for block, _ref, _rank in hits:
            assert len(block.text.strip()) >= 4


# ── MAJOR #12: cross-kind error options filtered ────────────────


class TestCrossKindErrorOptionsFiltered:
    def test_unknown_kind_on_search_lists_only_search_kinds(
        self, runtime: PrecisRuntime
    ) -> None:
        """The MCP critic flagged that ``search(kind='all', q='…')``
        returned options including kinds (calc, math, web, …) that
        don't actually support search. The retry then double-failed."""
        rendered = runtime.dispatch("search", {"kind": "all", "q": "x"})
        assert "[error:NotFound]" in rendered
        # The options line must exist and must NOT mention search-
        # incapable kinds.
        opt_line = next(
            (line for line in rendered.splitlines() if "options:" in line), ""
        )
        assert opt_line, "expected an options: line in the error reply"
        assert "calc" not in opt_line
        assert "math" not in opt_line
        assert "web" not in opt_line
        assert "websearch" not in opt_line
        assert "youtube" not in opt_line


# ── MINOR m3: paper-id underscore error names the rule ──────────


class TestPaperSlugUnderscoreError:
    def test_underscore_message_names_rule(self, store: Store) -> None:
        h = PaperHandler(store=store)
        # Even with no live ref by that slug, parsing should reject
        # *before* the lookup with a clear "underscore is illegal"
        # message that names the offending char (rather than the
        # generic "unparseable paper id" the critic flagged).
        with pytest.raises(BadInput, match=r"contains '_'"):
            h.get(id="nonexistent_paper_xyz")


# ── MINOR m4: calc rejects self-simplifying gibberish ───────────


class TestCalcRejectsGibberish:
    def test_malformed_expression_rejected(self) -> None:
        h = CalcHandler()
        with pytest.raises(BadInput, match="simplifies to itself"):
            h.get(id="malformed**broken")

    def test_real_math_still_works(self) -> None:
        h = CalcHandler()
        out = h.get(id="2+3*4")
        assert "= 14" in out.body

    def test_symbolic_math_with_operator_still_works(self) -> None:
        """Genuine symbolic computation (``integrate``, ``solve``, …)
        produces a different output shape than the input and must
        not be rejected."""
        h = CalcHandler()
        out = h.get(id="integrate(sin(x), x)")
        # Result is "-cos(x)" — different from the input.
        assert "cos(x)" in out.body


# ── MAJOR #10: top_k cap (validated at MCP boundary) ─────────────


class TestTopKCap:
    def test_top_k_max_constant(self) -> None:
        """The cap lives in server.py as a module constant — pin
        the value so changes are deliberate."""
        from precis.server import _SEARCH_TOP_K_MAX

        assert _SEARCH_TOP_K_MAX == 100


# ── MAJOR #13: mode='note' not in supported list ────────────────


class TestModeNoteRetired:
    def test_mode_note_not_in_supported_modes(self) -> None:
        """The numeric-ref handlers no longer claim to support
        ``mode='note'``."""
        from precis.handlers._numeric_ref import _SUPPORTED_PUT_MODES

        assert "note" not in _SUPPORTED_PUT_MODES


# ── MINOR m2: empty-list responses carry Next: trailers ─────────


class TestEmptyListTrailers:
    def test_oracle_empty_list_has_trailer(self, store: Store) -> None:
        from precis.handlers.oracle import OracleHandler

        h = OracleHandler(store=store)
        out = h.get()
        assert "no oracles defined" in out.body
        assert "Next:" in out.body

    def test_conv_empty_list_has_trailer(self, store: Store) -> None:
        from precis.handlers.conversation import ConversationHandler

        h = ConversationHandler(store=store)
        out = h.get()
        assert "no conversations" in out.body
        assert "Next:" in out.body

    def test_quest_empty_list_has_trailer(self, store: Store) -> None:
        from precis.handlers.quest import QuestHandler

        h = QuestHandler(store=store)
        out = h.get(id="/recent")
        assert "no quests" in out.body
        assert "Next:" in out.body


# ── MINOR m6: degenerate ranges render as ~N not ~N..N ─────────


class TestDegenerateRangeTrailer:
    def test_single_block_next_is_tilde_n(self, store: Store) -> None:
        h = PaperHandler(store=store)
        _seed_paper(store, n_blocks=4)
        # Reading ~0..2 should suggest "~3" (degenerate single-block)
        # rather than "~3..3".
        resp = h.get(id="wang2020state~0..2")
        assert "~3..3" not in resp.body
        assert "~3" in resp.body


# ── runtime fixture ─────────────────────────────────────────────


@pytest.fixture
def runtime(store: Store) -> PrecisRuntime:
    """A runtime wired with every active handler — same shape the
    real MCP server uses, but pointed at the test store."""
    from precis.config import PrecisConfig
    from precis.embedder import make_embedder
    from precis.hints import HintBus
    from precis.registry import Registry, builtins

    embedder = make_embedder("mock", dim=store.embedding_dim())
    handlers = builtins(store=store, embedder=embedder)
    registry = Registry(handlers)
    for h in handlers:
        bind = getattr(h, "bind_registry", None)
        if callable(bind):
            bind(registry)
    return PrecisRuntime(
        config=PrecisConfig(),
        registry=registry,
        hints=HintBus(),
        store=store,
    )
