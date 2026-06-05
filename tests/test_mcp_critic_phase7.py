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
  MAJOR  #10  ``page_size`` capped at 100, larger values rejected
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

from precis.dispatch import Hub
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
    ref = store.insert_ref(
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


class TestPutNarrowedToCreateOnly:
    """After the seven-verb cutover, numeric-ref kinds (memory, todo,
    gripe, fc, …) accept ``put`` only for creation. ``id=`` and
    ``mode=`` are both rejected with sharp pointers at the dedicated
    verbs (tag / link / delete) so an agent stuck on the legacy
    ``put(mode='X')`` shape gets a recovery hint rather than a
    silent no-op.

    The original critic finding was CRITICAL #2: ``mode='unlink'``
    silently succeeded without removing anything. The fix migrated
    those operations to dedicated verbs; the regression here is
    that ``put`` is now noisy about every legacy invocation shape.
    """

    def test_put_with_mode_rejected(self, store: Store) -> None:
        """Any ``mode=`` on numeric-ref put is rejected — there's no
        creation mode worth carrying."""
        h = MemoryHandler(hub=Hub(store=store))
        with pytest.raises(BadInput, match="mode= is not accepted on put"):
            h.put(text="m", mode="deelete")

    def test_put_on_existing_id_rejected(self, store: Store) -> None:
        """``id=`` on put points the caller at tag/link/delete."""
        h = MemoryHandler(hub=Hub(store=store))
        m = h.put(text="m")
        rid = int(m.body.split("=")[-1].strip().split()[0])
        with pytest.raises(BadInput, match="put on existing memory"):
            h.put(id=rid)

    def test_delete_verb_works(self, store: Store) -> None:
        """The replacement for the legacy ``put(id=N, mode='delete')``
        — the dedicated ``delete`` verb soft-deletes the ref."""
        h = MemoryHandler(hub=Hub(store=store))
        m = h.put(text="m")
        rid = int(m.body.split("=")[-1].strip().split()[0])
        out = h.delete(id=rid)
        assert "deleted" in out.body


# ── CRITICAL #3 + MAJOR #6/#7: skill index filtering ──────────────


class TestSkillIndexFiltering:
    def test_density_and_navigation_deleted(self) -> None:
        """Round 3 tightening of the unwired-= unmentioned discipline:
        ``precis-density`` and ``precis-navigation`` were previously
        shipped with status: planned / aspirational banners, but both
        described features that don't exist (DENSITY tag prefix,
        ``view='representatives'``, ``kind='ask'``, ``kind='all'``,
        …). Even hidden from the default index they were still
        retrievable by explicit slug, teaching agents APIs that
        would throw. The stricter policy (per repo owner, May 2026)
        is: if we can't help mention something unwired, don't.
        These two files are deleted; re-add them when the described
        APIs land.
        """
        from importlib import resources

        for gone in ("precis-density.md", "precis-navigation.md"):
            traversable = resources.files("precis.data.skills") / gone
            # ``Traversable`` doesn't expose ``exists`` uniformly, so
            # probe via ``is_file`` (available since py3.11).
            assert not traversable.is_file(), (
                f"{gone} resurfaced — it describes unwired features "
                "and must stay deleted until those features land"
            )

    def test_files_help_marked_active(self) -> None:
        """``precis-files-help`` documents the shared address grammar
        for the shipped file kinds (markdown, plaintext, python). It
        was ``status: planned`` while those kinds were pre-wire; now
        that all three ship (each gated on its own env var) the skill
        is ``status: active``. Individual kinds are still hidden from
        the index via the availability-gap gate when their env var
        isn't set in the current runtime."""
        from importlib import resources

        text = (
            resources.files("precis.data.skills") / "precis-files-help.md"
        ).read_text(encoding="utf-8")
        fm = _parse_frontmatter(text)
        assert fm.get("status") == "active"
        # The status banner inside the skill must still warn readers
        # that file kinds are gated on env vars — that's the honesty
        # clause. After the consolidation, ``PRECIS_ROOT`` gates the
        # markdown / plaintext / tex trio; python keeps its own var.
        assert "PRECIS_ROOT" in text
        assert "PRECIS_PYTHON_ROOTS" in text

    def test_availability_gap_filters_unregistered_kind_help(
        self, runtime: PrecisRuntime
    ) -> None:
        # ``markdown`` kind is not in the test runtime's hub.
        gap = _availability_gap("precis-markdown-help", hub=runtime.hub)
        assert gap is not None
        assert "not" in gap.lower() and "wired" in gap.lower()

    def test_availability_gap_passes_active_skill(self, runtime: PrecisRuntime) -> None:
        # ``precis-overview`` is cross-cutting and active.
        gap = _availability_gap("precis-overview", hub=runtime.hub)
        assert gap is None

    def test_availability_gap_passes_active_help(self, runtime: PrecisRuntime) -> None:
        # ``memory`` is in every test fixture's hub.
        gap = _availability_gap("precis-memory-help", hub=runtime.hub)
        assert gap is None

    def test_index_omits_deleted_skills(self, runtime: PrecisRuntime) -> None:
        """Direct confirmation that the bare ``get(kind='skill')``
        index doesn't reference the deleted skills — defense in depth
        against a stale cached copy resurfacing via resource enumeration
        or FM re-parsing."""
        h = runtime.hub.handler_for("skill")
        assert isinstance(h, SkillHandler)
        out = h.get()
        assert "precis-density" not in out.body
        assert "precis-navigation" not in out.body

    def test_deleted_skill_raises_notfound(self, runtime: PrecisRuntime) -> None:
        """Direct fetch of a deleted skill must ``NotFound``, not
        return a banner-wrapped stale copy. This pins the stricter
        "unwired = unmentioned" discipline: the deleted skills are
        genuinely gone, not soft-hidden."""
        from precis.errors import NotFound

        h = runtime.hub.handler_for("skill")
        assert isinstance(h, SkillHandler)
        with pytest.raises(NotFound):
            h.get(id="precis-density")
        with pytest.raises(NotFound):
            h.get(id="precis-navigation")


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
        h = PaperHandler(hub=Hub(store=store))
        store.insert_ref(
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
        ref = store.insert_ref(kind="paper", slug="p", title="P")
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
        self, runtime_with_store: PrecisRuntime
    ) -> None:
        """The MCP critic flagged that an unknown kind on a
        cross-kind ``search`` returned options including kinds
        (calc, math, …) that don't support search. The retry
        then double-failed.

        Note: ``web`` and the Perplexity tiers (``websearch`` /
        ``think`` / ``research``) gained ``supports_search`` in the
        fetch-path-embedding consolidation — their fetched pages are
        block-parsed + embedded so search works over cached bodies.
        Only the genuinely stateless single-shot tools (``calc``,
        ``math``, ``youtube``) stay search-incapable.

        ``kind='all'`` itself is now a wildcard alias (MCP critic
        2026-05-02), so the test exercises the same option-filter
        property via a comma-list whose unknown token forces the
        BadInput path.
        """
        rendered = runtime_with_store.dispatch(
            "search", {"kind": "paper,not-a-real-kind", "q": "x"}
        )
        assert "[error:BadInput]" in rendered
        # Parse the options: line as a comma-separated kind list so
        # substring-matching doesn't false-positive on siblings
        # (``web`` vs ``websearch``).
        opt_line = next(
            (line for line in rendered.splitlines() if "options:" in line), ""
        )
        assert opt_line, "expected an options: line in the error reply"
        opts = {tok.strip() for tok in opt_line.split(":", 1)[1].split(",")}
        # Stateless single-shot tools must not appear in the cross-kind
        # search options — they have nothing to search over.
        assert "calc" not in opts
        assert "math" not in opts
        assert "youtube" not in opts


# ── MINOR m3: paper-id underscore error names the rule ──────────


class TestPaperSlugUnderscoreError:
    def test_underscore_message_names_rule(self, store: Store) -> None:
        h = PaperHandler(hub=Hub(store=store))
        # Even with no live ref by that slug, parsing should reject
        # *before* the lookup with a clear "underscore is illegal"
        # message that names the offending char (rather than the
        # generic "unparseable paper id" the critic flagged).
        with pytest.raises(BadInput, match=r"contains '_'"):
            h.get(id="nonexistent_paper_xyz")


# ── MINOR m4: calc rejects self-simplifying gibberish ───────────


class TestCalcRejectsGibberish:
    def test_malformed_expression_rejected(self) -> None:
        h = CalcHandler(hub=Hub())
        with pytest.raises(BadInput, match="simplifies to itself"):
            h.get(id="malformed**broken")

    def test_real_math_still_works(self) -> None:
        h = CalcHandler(hub=Hub())
        out = h.get(id="2+3*4")
        assert "= 14" in out.body

    def test_symbolic_math_with_operator_still_works(self) -> None:
        """Genuine symbolic computation (``integrate``, ``solve``, …)
        produces a different output shape than the input and must
        not be rejected."""
        h = CalcHandler(hub=Hub())
        out = h.get(id="integrate(sin(x), x)")
        # Result is "-cos(x)" — different from the input.
        assert "cos(x)" in out.body


# ── MAJOR #10: page_size cap (validated at MCP boundary) ─────────────


class TestTopKCap:
    def test_page_size_max_constant(self) -> None:
        """The cap lives next to the ``search`` tool implementation
        as a module constant — pin the value so changes are
        deliberate. Moved out of ``precis.server`` and into
        ``precis.tools.core`` when the seven verbs migrated to the
        shared tool registry."""
        from precis.tools.core import _SEARCH_PAGE_SIZE_MAX

        assert _SEARCH_PAGE_SIZE_MAX == 100


# ── MINOR m2: empty-list responses carry Next: trailers ─────────


class TestEmptyListTrailers:
    def test_oracle_empty_list_has_trailer(self, store: Store) -> None:
        from precis.handlers.oracle import OracleHandler

        h = OracleHandler(hub=Hub(store=store))
        out = h.get()
        assert "no oracles defined" in out.body
        assert "Next:" in out.body

    def test_conv_empty_list_has_trailer(self, store: Store) -> None:
        from precis.handlers.conversation import ConversationHandler

        h = ConversationHandler(hub=Hub(store=store))
        out = h.get()
        assert "no conversations" in out.body
        assert "Next:" in out.body

    def test_quest_empty_list_has_trailer(self, store: Store) -> None:
        from precis.handlers.quest import QuestHandler

        h = QuestHandler(hub=Hub(store=store))
        out = h.get(id="/recent")
        assert "no quests" in out.body
        assert "Next:" in out.body


# ── MINOR m6: degenerate ranges render as ~N not ~N..N ─────────


class TestDegenerateRangeTrailer:
    def test_single_block_next_is_tilde_n(self, store: Store) -> None:
        h = PaperHandler(hub=Hub(store=store))
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
    from precis.dispatch import boot
    from precis.embedder import make_embedder

    embedder = make_embedder("mock", dim=store.embedding_dim())
    return PrecisRuntime(
        config=PrecisConfig(),
        hub=boot(store=store, embedder=embedder),
    )
