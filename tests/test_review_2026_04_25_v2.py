"""Regression tests for the 2026-04-25 mcp-critic review (v2 pass).

Twelve fixes covered in this pass — all individually documented inline
with the rule code (B4 / B5 / B7 / C5 / D5 / D7 / D12 / E2 / E3 / F1)
the critic raised against:

1. **B4** — ``/abstract`` no longer leaks JATS XML markup
   (``<jats:title>``, ``<jats:sub>``, ``<jats:p>``).
2. **B5/D4** — ``/fig/0``, ``/fig/-1`` and ``/fig/abc`` all return the
   *same* structured ``ERROR [<code>]`` envelope, with the available-
   figures list folded into ``next:``.
3. **B7** — ``/fig/<N>`` rescues captions from adjacent text blocks
   when the figure-extractor missed them; ``/fig/<N>/image`` carries
   the caption inline above the base64 blob.
4. **C5/E3** — ``rng:int(1,100)`` (commas inside parens on a kind that
   normally batches on ``,``) is preserved verbatim instead of being
   chopped into two failed dispatches.
5. **D5** — ``~38..200`` on an 87-block paper clamps to the actual
   block count and emits an "End of paper" trailer instead of an
   aspirational ``Next: ~200..`` lie.
6. **D7** (a) — multi-id batch responses dedupe per-chunk
   ``[cost: …]`` footers down to one trailing footer.
7. **D7** (b) — ``flashcard:/due`` no longer carries the ~120-token
   "Review tips:" block; the pedagogy lives in ``skill:sm2-basics``.
8. **D12** (a) — ``doi:10.x/y`` and ``arxiv:2207.09327`` carry the
   ``[cost: free]`` footer just like ``paper:slug``.
9. **D12** (b) — ``oracle:/recent`` works (returns the tradition list
   with a one-line note) instead of failing with PARAM_INVALID.
10. **E2** — kind name is ``docx`` (renamed from ``word``).  No alias.
11. **E3** — visually-similar separators (``–`` U+2013, ``—`` U+2014,
    ``‐`` U+2010, ``‑`` U+2011, ``−`` U+2212) are rejected with a
    structured error pointing at the canonical ``~``.
12. **F1** — ``get(type='docx', id='/recent')`` returns a structured
    ``PARAM_INVALID`` envelope instead of leaking
    ``FileNotFoundError: [Errno 2] No such file or directory:
    '/[Content_Types].xml'``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from precis import server
from precis.handlers.docx import DocxHandler
from precis.handlers.paper import (
    PaperHandler,
    _clean_jats,
)
from precis.protocol import ErrorCode, PrecisError
from precis.registry import (
    ALIASES,
    KINDS,
    _discover,
)

# ---------------------------------------------------------------------------
# B4 — JATS XML stripping
# ---------------------------------------------------------------------------


class TestJatsCleanup:
    """``_clean_jats`` is the single helper every abstract path runs
    through (mcp-critic finding B4)."""

    def test_strips_jats_title_and_p(self):
        raw = (
            "<jats:title>Abstract</jats:title>"
            "<jats:p>Hello world.</jats:p>"
        )
        out = _clean_jats(raw)
        assert "<jats:" not in out
        assert "</jats:" not in out
        assert "Hello world." in out
        assert "Abstract" in out

    def test_jats_sub_becomes_unicode(self):
        raw = "NO<jats:sub>3</jats:sub>"
        # ``₃`` is U+2083
        assert _clean_jats(raw) == "NO\u2083"

    def test_jats_sup_becomes_unicode(self):
        raw = "H<jats:sup>+</jats:sup>"
        # ``⁺`` is U+207A
        assert _clean_jats(raw) == "H\u207a"

    def test_jats_sub_then_sup_combined(self):
        # Real CrossRef shape from the critic's ``ni2024atomic`` probe.
        raw = "NO<jats:sub>3</jats:sub><jats:sup>−</jats:sup>"
        out = _clean_jats(raw)
        # Subscript ₃ + Unicode minus ⁻ in superscript.
        assert out == "NO\u2083\u207b"

    def test_non_digit_sub_falls_back_to_underscore(self):
        # ``x`` isn't in the digit/operator translation table, so we
        # wrap with markdown italics rather than silently drop it.
        raw = "y<jats:sub>x</jats:sub>"
        out = _clean_jats(raw)
        assert "_x_" in out

    def test_empty_input(self):
        assert _clean_jats("") == ""
        assert _clean_jats(None) is None  # type: ignore[arg-type]


class TestPaperAbstractView:
    """``/abstract`` view runs the cleaned text through ``_clean_jats``."""

    @staticmethod
    def _handler_with_abstract(raw_text: str):
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {
            "slug": "ni2024atomic",
            "title": "T",
            "ref_id": 1,
        }

        def get_blocks(slug, block_type=None):
            if block_type == "abstract":
                return [{"text": raw_text}]
            return []

        store.get_blocks.side_effect = get_blocks
        return h, store

    def test_abstract_view_strips_jats(self):
        h, store = self._handler_with_abstract(
            "<jats:title>Abstract</jats:title>"
            "<jats:p>Reducing nitrate (NO<jats:sub>3</jats:sub>"
            "<jats:sup>−</jats:sup>) releases H<jats:sup>+</jats:sup>.</jats:p>"
        )
        ref = store.get.return_value
        out = h._read_abstract(store, ref)
        assert "<jats:" not in out
        assert "</jats:" not in out
        assert "NO\u2083\u207b" in out  # NO₃⁻
        assert "H\u207a" in out  # H⁺

    def test_overview_strips_jats_from_preview(self):
        # The overview pulls the first abstract block and snips it to
        # 500 chars; the snip must run *after* JATS cleanup.  Without
        # this, an abstract starting with ``<jats:title>...`` was
        # rendered verbatim into the agent's overview.
        h, store = self._handler_with_abstract(
            "<jats:title>Abstract</jats:title>"
            "<jats:p>Body here.</jats:p>"
        )
        # Patch link-count helper and figures so overview doesn't hit
        # un-mocked branches.
        store.get_link_count.return_value = {}
        store.get_figures.return_value = []
        ref = store.get.return_value
        out = h._read_overview(store, ref)
        assert "<jats:" not in out
        assert "Body here." in out


# ---------------------------------------------------------------------------
# B5/D4 — figure error envelope is consistent
# ---------------------------------------------------------------------------


class TestFigureErrorShape:
    """All three flavours of bad figure number (``0``, ``-1``, ``abc``)
    raise the same structured ``PrecisError`` via the central
    ``_figure_not_found`` helper.  Review 2026-04-25 finding B5/D4.
    """

    @staticmethod
    def _handler():
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {
            "slug": "wu2008first",
            "title": "T",
            "ref_id": 1,
        }
        store.get_figures.return_value = [
            {"fig_num": 1, "caption": "", "page": 1},
            {"fig_num": 2, "caption": "", "page": 2},
            {"fig_num": 3, "caption": "", "page": 3},
        ]
        store.get_blocks.return_value = []
        return h, store

    def test_figure_zero_raises_structured_id_not_found(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "0")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert "available: 1, 2, 3" in exc.next
            assert "wu2008first/fig" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_figure_negative_raises_structured_id_not_found(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "-1")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert "available: 1, 2, 3" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_figure_non_numeric_raises_structured_id_malformed(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "abc")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_MALFORMED
            assert "abc" in exc.cause
            assert "available: 1, 2, 3" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_figure_out_of_range_raises_structured_id_not_found(self):
        h, store = self._handler()
        ref = store.get.return_value
        try:
            h._read_figures(store, ref, "99")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert "99" in exc.cause
            assert "available: 1, 2, 3" in exc.next
        else:
            raise AssertionError("expected PrecisError")


# ---------------------------------------------------------------------------
# B7 — figure caption pairing rescue
# ---------------------------------------------------------------------------


class TestFigureCaptionRescue:
    """When ``store.get_figures`` returns ``caption=""`` but a body
    block contains the literal ``Figure N. ...`` text, the handler
    pairs them on read.  Review 2026-04-25 finding B7.
    """

    @staticmethod
    def _handler_with_orphan_caption():
        h = PaperHandler()
        store = MagicMock()
        store.get.return_value = {
            "slug": "wu2008first",
            "title": "T",
            "ref_id": 1,
        }
        store.get_figures.return_value = [
            # Caption deliberately empty — the rescue must find it.
            {"fig_num": 3, "caption": "", "page": 1},
        ]
        store.get_blocks.return_value = [
            {
                "block_index": 38,
                "page": 1,
                "block_type": "text",
                "text": "Some preceding paragraph.",
            },
            {
                "block_index": 40,
                "page": 1,
                "block_type": "text",
                "text": (
                    "Figure 3. (a) Electronic band structure of pristine "
                    "zigzag (10,0) SWCNT and (b) corresponding density of "
                    "states."
                ),
            },
        ]
        store.get_figure_image.return_value = {
            "image_bytes": b"fake-png-bytes",
            "image_ext": ".png",
            "fig_num": 3,
            "caption": "",
        }
        return h, store

    def test_overview_pairs_caption_from_body(self):
        h, store = self._handler_with_orphan_caption()
        out = h._read_figures(store, store.get.return_value, "3")
        assert "[no caption]" not in out
        assert "Electronic band structure" in out
        # Bold marker + figure number prefix lands ahead of the caption
        assert "**Figure 3.**" in out

    def test_legend_returns_rescued_caption(self):
        h, store = self._handler_with_orphan_caption()
        out = h._read_figures(store, store.get.return_value, "3/legend")
        assert out.startswith("Figure 3.")
        assert "Electronic band structure" in out

    def test_image_view_carries_caption_inline(self):
        h, store = self._handler_with_orphan_caption()
        out = h._read_figures(store, store.get.return_value, "3/image")
        # Caption appears above the base64 blob
        idx_caption = out.find("**Figure 3.**")
        idx_b64 = out.find("data:image/png;base64,")
        assert idx_caption >= 0, "caption should be present in /image view"
        assert idx_b64 >= 0
        assert idx_caption < idx_b64

    def test_rescue_prefers_same_page_match(self):
        h = PaperHandler()
        store = MagicMock()
        store.get_blocks.return_value = [
            {
                "block_index": 5,
                "page": 7,
                "block_type": "text",
                "text": "Figure 2. Caption from page 7.",
            },
            {
                "block_index": 10,
                "page": 9,
                "block_type": "text",
                "text": "Figure 2. Caption from page 9.",
            },
        ]
        rescued = h._rescue_caption(store, "x", 2, page=9)
        assert rescued == "Figure 2. Caption from page 9."


# ---------------------------------------------------------------------------
# C5/E3 — comma-in-parens
# ---------------------------------------------------------------------------


class TestCommaInParensSplit:
    """``server._split_top_level_commas`` keeps commas inside
    ``()``/``[]``/``{}`` together.  Review 2026-04-25 finding C5/E3.
    """

    def test_no_parens_splits_normally(self):
        assert server._split_top_level_commas("a,b,c") == ["a", "b", "c"]

    def test_comma_inside_parens_is_preserved(self):
        assert server._split_top_level_commas("int(1,100)") == ["int(1,100)"]

    def test_mixed_top_and_inner_commas(self):
        # Top-level split lands between ``a(b,c)`` and ``d``; the inner
        # comma is part of the function argument list and survives.
        assert server._split_top_level_commas("a(b,c),d") == ["a(b,c)", "d"]

    def test_nested_parens(self):
        out = server._split_top_level_commas("Matrix([[1,2],[3,4]]),5")
        assert out == ["Matrix([[1,2],[3,4]])", "5"]

    def test_brackets_and_braces_count(self):
        assert server._split_top_level_commas("f({a,b}),g([c,d])") == [
            "f({a,b})",
            "g([c,d])",
        ]

    def test_whitespace_stripped_and_empty_dropped(self):
        assert server._split_top_level_commas(" a , , b ") == ["a", "b"]

    def test_unbalanced_parens_dont_raise(self):
        # Trailing unbalanced paren is treated as content; the URI
        # parser will surface the error downstream with its own
        # structured envelope.  We just don't crash here.
        out = server._split_top_level_commas("a(b,c")
        assert out == ["a(b,c"]


# ---------------------------------------------------------------------------
# D5 — pagination clamping at end of paper
# ---------------------------------------------------------------------------


class TestRangeClamping:
    """``~38..200`` on an 87-block paper used to deliver blocks
    38..85 then advertise a fake ``Next: ~200.. for more`` hint that
    paginated into an empty range.  Review 2026-04-25 finding D5.
    """

    @staticmethod
    def _handler_with_blocks(n_blocks: int):
        h = PaperHandler()
        store = MagicMock()
        ref = {"slug": "wu2008first", "ref_id": 1}
        store.get.return_value = ref
        store.get_blocks.return_value = [
            {
                "block_index": i,
                "page": i // 5 + 1,
                "block_type": "text",
                "text": f"block {i}",
                "node_id": f"n{i}",
            }
            for i in range(n_blocks)
        ]
        store.get_links.return_value = []
        return h, store, ref

    def test_range_extending_past_end_clamps(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "38..200")
        # Clamp notice surfaces up front
        assert "Range clamped to" in out
        assert "~38..86" in out
        # End-of-paper marker present, no aspirational Next: hint
        assert "End of paper" in out
        assert "Next: get(id='wu2008first~200" not in out
        # The last delivered block is the actual last block
        assert ">> wu2008first ~86" in out

    def test_open_range_still_works_at_end(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "80..")
        assert "End of paper" in out
        # No off-the-end Next: line
        assert "Next: get(" not in out

    def test_range_inside_paper_keeps_next_hint(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "10..30")
        # Mid-paper range gets a Next: hint pointing at the next chunk.
        assert "End of paper" not in out
        assert "Next: get(id='wu2008first~30..')" in out

    def test_range_starting_past_end_explains(self):
        h, store, ref = self._handler_with_blocks(87)
        out = h._read_chunks(store, ref, "200..300")
        assert "paper has 87 blocks" in out
        assert "~0..86" in out


# ---------------------------------------------------------------------------
# D7 — flashcard /due trailer slimmed
# ---------------------------------------------------------------------------


# Coverage already in tests/test_flashcard_handler.py (the existing
# ``test_due_points_at_sm2_skill_not_inline_tips`` was rewritten to
# guard the new behaviour).  No additional case needed here.


# ---------------------------------------------------------------------------
# D7 — multi-id batch footer dedupe
# ---------------------------------------------------------------------------


class TestMultiIdFooterDedupe:
    """``_strip_inner_cost_footers`` collapses N per-chunk
    ``[cost: …]`` lines down to one trailing footer.  Review
    2026-04-25 finding D7.
    """

    def test_three_free_chunks_emit_one_trailing_footer(self):
        parts = [
            "chunk one body\n\n[cost: free]",
            "chunk two body\n\n[cost: free]",
            "chunk three body\n\n[cost: free]",
        ]
        out = server._strip_inner_cost_footers(parts)
        assert out.count("[cost: free]") == 1
        assert out.endswith("[cost: free]")
        # All three bodies survive
        assert "chunk one body" in out
        assert "chunk two body" in out
        assert "chunk three body" in out

    def test_paid_footer_wins_over_free(self):
        parts = [
            "free chunk\n\n[cost: free]",
            "paid chunk\n\n[cost: ~$0.005/call]",
        ]
        out = server._strip_inner_cost_footers(parts)
        assert "[cost: free]" not in out
        assert out.endswith("[cost: ~$0.005/call]")

    def test_no_inner_footers_passes_through(self):
        parts = ["body one", "body two"]
        out = server._strip_inner_cost_footers(parts)
        # Nothing to merge — separator + bodies, no synthetic footer.
        assert "[cost:" not in out
        assert "body one" in out
        assert "body two" in out

    def test_separator_preserved(self):
        parts = ["a\n\n[cost: free]", "b\n\n[cost: free]"]
        out = server._strip_inner_cost_footers(parts)
        assert "\n---\n" in out


# ---------------------------------------------------------------------------
# D12 — cost-footer parity for scheme aliases
# ---------------------------------------------------------------------------


class TestSchemeAliasCostFooterParity:
    """Non-canonical scheme names of a multi-scheme single-kind plugin
    (``doi``, ``arxiv``, ``pmid``, ``pmcid``, ``isbn``, ``issn`` for
    the paper plugin) route through the same ``KINDS``-keyed
    ``Result``-wrapping path in ``_dispatch`` as the canonical
    ``paper:`` scheme, so every URI-form picks up the
    ``[cost: free]`` footer.  Review 2026-04-25 finding D12.

    Implementation: ``server._kind_from_uri`` resolves a URI scheme
    that's neither a kind nor an alias to the owning plugin's first
    ``KindSpec`` name.  This deliberately does **NOT** also rebind
    ``type='doi'`` \u2014 ``type=`` is the agent-facing kind enum and
    keeps its strict canonical-only policy (per Apr 2026 cleanup,
    locked in by ``test_server_phase1.TestToUriKindHint``).
    """

    def setup_method(self):
        _discover()

    def test_kind_from_doi_uri_returns_paper(self):
        assert server._kind_from_uri("doi:10.1021/nn800256d") == "paper"

    def test_kind_from_arxiv_uri_returns_paper(self):
        assert server._kind_from_uri("arxiv:2207.09327") == "paper"

    def test_kind_from_pmid_pmcid_isbn_issn_uris_returns_paper(self):
        # ``isbn:`` is a scheme on *both* the paper plugin (via
        # PaperHandler.schemes) and the book plugin.  The lookup
        # returns whichever plugin's handler matches the SCHEMES
        # mapping, which by registration order is the paper plugin.
        # Books still resolve via their own ``book:`` scheme.
        for scheme, fixture in (
            ("pmid", "pmid:12345678"),
            ("pmcid", "pmcid:PMC1234567"),
            ("issn", "issn:2049-3630"),
        ):
            assert server._kind_from_uri(fixture) == "paper", (
                f"{scheme}: should route to canonical paper kind"
            )

    def test_type_doi_is_still_rejected_as_kind(self):
        # The agent enum is canonical-only.  ``type='doi'`` must NOT
        # silently rewrite to ``type='paper'`` \u2014 that's the explicit
        # invariant in test_server_phase1.TestToUriKindHint.
        assert "doi" not in ALIASES
        assert "doi" not in KINDS

    def test_canonical_paper_kind_is_not_an_alias(self):
        assert "paper" in KINDS
        assert "paper" not in ALIASES


# ---------------------------------------------------------------------------
# D12 — oracle /recent parity
# ---------------------------------------------------------------------------


class TestOracleRecentParity:
    """``oracle:/recent`` works.  Review 2026-04-25 finding D12.

    Every other state-backed kind (memory, todo, quest, skill,
    flashcard) accepts ``/recent``.  Oracle is stateless — it has no
    draw history — but rejecting the input was harsher than helpful.
    The handler now treats ``/recent`` as a tradition-listing alias
    with a one-line note explaining that draws aren't tracked.
    """

    def setup_method(self):
        _discover()

    def test_recent_returns_tradition_list(self):
        from precis.handlers.oracle import OracleHandler

        h = OracleHandler()
        store = MagicMock()
        store.list_refs_by_corpus.return_value = [
            {
                "slug": "oracle:iching",
                "title": "I-Ching",
                "tags": '["oracle","built-in","i-ching"]',
                "meta": {},
            }
        ]
        store.get_blocks.return_value = []
        with patch(
            "precis.handlers.oracle._get_store", return_value=store
        ):
            out = h.read("/recent", None, None, None, "", False, 0, 0)
        assert "aren't tracked" in out
        assert "Oracle" in out
        # Drops down into the tradition listing
        assert "iching" in out

    def test_recent_view_arg_also_works(self):
        # If the URI parser routes ``oracle:/recent`` to ``view='recent'``
        # rather than ``path='/recent'``, the same fallback fires.
        from precis.handlers.oracle import OracleHandler

        h = OracleHandler()
        store = MagicMock()
        store.list_refs_by_corpus.return_value = []
        with patch(
            "precis.handlers.oracle._get_store", return_value=store
        ):
            out = h.read("", None, "recent", None, "", False, 0, 0)
        assert "aren't tracked" in out


# ---------------------------------------------------------------------------
# E2 — kind name is ``docx`` not ``word``
# ---------------------------------------------------------------------------


class TestDocxKindRename:
    """The DOCX file kind is registered under the canonical name
    ``docx``.  No back-compat ``word`` alias is registered (review
    2026-04-25 finding E2 — small models routed dictionary-lookup
    queries to ``word`` thinking it was a definition kind).
    """

    def setup_method(self):
        _discover()

    def test_docx_kind_registered(self):
        assert "docx" in KINDS
        assert KINDS["docx"].handler_cls is DocxHandler

    def test_word_is_not_a_kind_anymore(self):
        # Hard rename — ``word`` should not resolve as either a kind
        # or an alias.  Callers that hard-coded ``type='word'`` must
        # update.
        assert "word" not in KINDS
        assert "word" not in ALIASES

    def test_docx_handler_class_renamed(self):
        from precis.handlers import docx as docx_module

        # The class is ``DocxHandler``; the legacy ``WordHandler``
        # name no longer exists.
        assert hasattr(docx_module, "DocxHandler")
        assert not hasattr(docx_module, "WordHandler")


# ---------------------------------------------------------------------------
# E3 — visually-similar separator rejection
# ---------------------------------------------------------------------------


class TestLookalikeSeparatorRejection:
    """``server._check_lookalike_sep`` catches en-dashes, em-dashes,
    Unicode hyphens, etc. and points the agent at canonical ``~``.
    Review 2026-04-25 finding E3.
    """

    def test_endash_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u201338")
        assert out is not None
        assert "ERROR [id_malformed]" in out
        assert "U+2013" in out  # the offending char is named
        assert "wu2008first~38" in out  # canonical fix in next:
        assert "[cost: free]" in out  # cost-footer parity

    def test_emdash_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u201438")
        assert out is not None
        assert "U+2014" in out
        assert "wu2008first~38" in out

    def test_unicode_hyphen_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u201038")
        assert out is not None
        assert "U+2010" in out

    def test_unicode_minus_rejected(self):
        out = server._check_lookalike_sep("wu2008first\u221238")
        assert out is not None
        assert "U+2212" in out

    def test_ascii_tilde_passes_through(self):
        # Canonical separator is fine.
        assert server._check_lookalike_sep("wu2008first~38") is None

    def test_no_separator_passes_through(self):
        # Bare slug, no separator at all.
        assert server._check_lookalike_sep("wu2008first") is None

    def test_legacy_u203a_still_silently_accepted(self):
        # ``›`` (U+203A) is the v5.x legacy separator and remains
        # accepted on input for back-compat (see
        # ``test_review_2026_04_25.TestSeparatorFlip``).  The
        # lookalike check must NOT flag it.
        assert server._check_lookalike_sep("wu2008first\u203a38") is None


# ---------------------------------------------------------------------------
# F1 — file kind validates path before opening
# ---------------------------------------------------------------------------


class TestFileKindRejectsNonPaths:
    """``DocxHandler`` (and every other ``FileHandlerBase`` subclass)
    rejects ``/recent``, empty paths, and paths without an extension
    *before* python-docx can leak ``FileNotFoundError`` and the
    ``/[Content_Types].xml`` zip-member name to the agent.  Review
    2026-04-25 finding F1.
    """

    def test_docx_rejects_slash_recent(self):
        h = DocxHandler()
        try:
            h._resolve_path("/recent")
        except PrecisError as exc:
            assert exc.code is ErrorCode.PARAM_INVALID
            assert "is not a file path" in exc.cause
            assert "Content_Types" not in str(exc)
            # Recovery hint points at the right shape.
            assert ".docx" in exc.next
        else:
            raise AssertionError("expected PrecisError")

    def test_docx_rejects_empty_path(self):
        h = DocxHandler()
        try:
            h._resolve_path("")
        except PrecisError as exc:
            assert exc.code is ErrorCode.PARAM_INVALID
            assert "filename" in exc.cause
        else:
            raise AssertionError("expected PrecisError")

    def test_docx_rejects_dot_path(self):
        # ``Path('.').exists()`` is True — the cwd — which used to slip
        # past the existence check and fall through to python-docx.
        h = DocxHandler()
        try:
            h._resolve_path(".")
        except PrecisError as exc:
            assert exc.code is ErrorCode.PARAM_INVALID
        else:
            raise AssertionError("expected PrecisError")

    def test_full_dispatch_returns_structured_error_for_recent(self):
        # End-to-end: ``server.get(type='docx', id='/recent')`` must
        # return a structured ``ERROR [<code>]: \u2026 / next: \u2026``
        # envelope, not a Python stack trace.
        out = server.get(type="docx", id="/recent")
        assert "ERROR [" in out
        assert "FileNotFoundError" not in out
        assert "Content_Types" not in out
        # Cost footer parity \u2014 every error envelope in the server
        # carries a cost line so wrappers parsing for budgets see it.
        assert "[cost: free]" in out

    def test_unknown_extension_returns_clean_error(self):
        h = DocxHandler()
        try:
            h._resolve_path("/tmp/nope.xyz")
        except PrecisError as exc:
            assert exc.code is ErrorCode.ID_NOT_FOUND
            assert ".docx" in exc.next
            assert "FileNotFoundError" not in str(exc)
        else:
            raise AssertionError("expected PrecisError")


# ---------------------------------------------------------------------------
# Cross-fix smoke: end-to-end scheme alias goes through Result wrapping
# ---------------------------------------------------------------------------


class TestSchemeAliasEndToEnd:
    """Belt-and-braces: ``get(id='doi:10.x/y')`` reaches
    ``_dispatch('paper', \u2026)`` (via the auto-alias) and renders
    through ``Result.render()``, which means the cost footer is
    present.  Review 2026-04-25 finding D12.
    """

    def setup_method(self):
        _discover()

    def test_doi_get_dispatches_to_paper_kind(self):
        # Capture the kind that ``_dispatch`` is called with.
        seen: list[str] = []

        def fake_dispatch(kind, verb, call, args=None):
            seen.append(kind)
            # Mimic the success path of invoke_handler so the test
            # doesn't need a live store.
            return "OK\n\n[cost: free]"

        with patch.object(server, "_dispatch", side_effect=fake_dispatch):
            out = server.get(id="doi:10.1021/nn800256d")
        assert seen == ["paper"]
        assert "[cost: free]" in out

    def test_arxiv_get_dispatches_to_paper_kind(self):
        seen: list[str] = []

        def fake_dispatch(kind, verb, call, args=None):
            seen.append(kind)
            return "OK\n\n[cost: free]"

        with patch.object(server, "_dispatch", side_effect=fake_dispatch):
            server.get(id="arxiv:2207.09327")
        assert seen == ["paper"]
