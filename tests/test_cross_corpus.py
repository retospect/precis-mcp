"""Tests for cross-corpus semantic search dispatch.

Covers the three pieces that together realise ``search(type='all')``
and ``search(type='paper,memory,web')``:

1. **Request detection** — :func:`is_cross_corpus_request` returns
   True only for ``'all'`` or inputs containing commas.
2. **Expansion** — :func:`expand_type_to_corpora` resolves kind lists
   to corpus ids, rejects non-ref-backed kinds (``websearch`` etc.)
   and unknown kinds with structured :class:`PrecisError`.
3. **Dispatch + rendering** — :func:`search_across_corpora` calls
   :meth:`acatome_store.store.Store.search_text(corpora=...)` once
   and renders the hits grouped by kind.

The dispatch tests use a stubbed store so they're fast and don't
require a live DB or an embedder.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from precis.cross_corpus import (
    corpus_id_to_kind,
    expand_type_to_corpora,
    is_cross_corpus_request,
    kind_to_corpus_id,
    search_across_corpora,
)
from precis.protocol import ErrorCode, PrecisError


# ===========================================================================
# Request detection
# ===========================================================================


class TestIsCrossCorpusRequest:
    def test_all_keyword(self):
        assert is_cross_corpus_request("all") is True

    def test_all_with_whitespace(self):
        assert is_cross_corpus_request("  all  ") is True

    def test_comma_list(self):
        assert is_cross_corpus_request("paper,memory") is True

    def test_comma_list_with_spaces(self):
        assert is_cross_corpus_request("paper, memory, web") is True

    def test_single_kind_not_cross_corpus(self):
        assert is_cross_corpus_request("paper") is False
        assert is_cross_corpus_request("memory") is False

    def test_empty_not_cross_corpus(self):
        assert is_cross_corpus_request("") is False
        assert is_cross_corpus_request("   ") is False

    def test_single_comma_is_list(self):
        """Even ``,`` is a list, just an empty one — the expansion
        step rejects it with a specific error.  Detection only asks
        'does this look like a list?' — yes."""
        assert is_cross_corpus_request(",") is True


# ===========================================================================
# Kind ↔ corpus_id helpers
# ===========================================================================


class TestKindCorpusMapping:
    def test_paper_kind_maps_to_papers_corpus(self):
        assert kind_to_corpus_id("paper") == "papers"

    def test_memory_kind_maps_to_memories_corpus(self):
        assert kind_to_corpus_id("memory") == "memories"

    def test_web_kind_maps_to_websites_corpus(self):
        assert kind_to_corpus_id("web") == "websites"

    def test_book_kind_maps_to_books_corpus(self):
        assert kind_to_corpus_id("book") == "books"

    def test_todo_kind_maps_to_todos_corpus(self):
        assert kind_to_corpus_id("todo") == "todos"

    def test_websearch_has_no_corpus(self):
        """External services (Perplexity) don't live in a corpus."""
        assert kind_to_corpus_id("websearch") is None

    def test_calc_has_no_corpus(self):
        """Pure-compute plugins don't live in a corpus."""
        assert kind_to_corpus_id("calc") is None

    def test_skill_has_no_corpus(self):
        """Skills are filesystem-backed, not store-backed."""
        assert kind_to_corpus_id("skill") is None

    def test_unknown_kind_returns_none(self):
        assert kind_to_corpus_id("nonexistent") is None

    def test_corpus_id_to_kind_roundtrip(self):
        """Every corpus_id we expose should reverse-map to a kind."""
        assert corpus_id_to_kind("papers") == "paper"
        assert corpus_id_to_kind("memories") == "memory"
        assert corpus_id_to_kind("websites") == "web"
        assert corpus_id_to_kind("books") == "book"

    def test_corpus_id_to_kind_unknown_returns_none(self):
        assert corpus_id_to_kind("nonexistent") is None

    def test_plural_corpus_id_accepted_as_kind(self):
        """Users reach for both singular (``paper``) and plural
        (``papers``) — accept both shapes interchangeably so
        ``type='papers,books'`` and ``type='paper,book'`` both work."""
        assert kind_to_corpus_id("papers") == "papers"
        assert kind_to_corpus_id("books") == "books"
        assert kind_to_corpus_id("memories") == "memories"
        assert kind_to_corpus_id("websites") == "websites"
        assert kind_to_corpus_id("todos") == "todos"
        assert kind_to_corpus_id("flashcards") == "flashcards"
        assert kind_to_corpus_id("conversations") == "conversations"


# ===========================================================================
# Expansion — type= → list of corpus_ids
# ===========================================================================


class TestExpandTypeToCorpora:
    def test_all_includes_every_ref_backed_plugin(self):
        corpora = expand_type_to_corpora("all")
        # At minimum these must be present given the plugins shipped
        # in this package.  Others may appear via entry-points.
        assert "papers" in corpora
        assert "memories" in corpora
        assert "websites" in corpora
        assert "books" in corpora
        assert "todos" in corpora
        assert "flashcards" in corpora
        assert "conversations" in corpora

    def test_all_excludes_external_services(self):
        """websearch, research, think, calc, math shouldn't be in 'all'."""
        corpora = expand_type_to_corpora("all")
        # None of the Perplexity or compute kinds have a corpus to add.
        for external in [
            "websearch",
            "research",
            "think",
            "calc",
            "math",
            "youtube",
            "skill",
            "quest",
        ]:
            assert external not in corpora

    def test_comma_list_resolves_to_corpora(self):
        corpora = expand_type_to_corpora("paper,memory,web")
        assert corpora == ["papers", "memories", "websites"]

    def test_comma_list_tolerates_whitespace(self):
        corpora = expand_type_to_corpora("  paper , memory ,  web  ")
        assert corpora == ["papers", "memories", "websites"]

    def test_dedupes_repeated_kinds(self):
        """If the caller lists the same kind twice, only emit the
        corpus once — silent dedup is less surprising than erroring."""
        corpora = expand_type_to_corpora("paper,paper,memory")
        assert corpora == ["papers", "memories"]

    def test_unknown_kind_raises(self):
        with pytest.raises(PrecisError) as exc_info:
            expand_type_to_corpora("nonexistent")
        assert exc_info.value.code == ErrorCode.KIND_UNKNOWN
        assert "unknown kind" in exc_info.value.cause.lower()

    def test_non_ref_backed_kind_raises(self):
        """websearch is known but has no corpus — clear error."""
        with pytest.raises(PrecisError) as exc_info:
            expand_type_to_corpora("websearch")
        assert exc_info.value.code == ErrorCode.KIND_UNKNOWN
        assert "not ref-backed" in exc_info.value.cause.lower()

    def test_mixed_valid_and_invalid_raises_on_first_invalid(self):
        with pytest.raises(PrecisError):
            expand_type_to_corpora("paper,websearch,memory")

    def test_only_commas_raises(self):
        with pytest.raises(PrecisError) as exc_info:
            expand_type_to_corpora(",,,")
        assert "empty kind list" in exc_info.value.cause.lower()

    def test_single_kind_in_list_works(self):
        """A single kind in list form (``'paper,'``) should still resolve."""
        # Trailing comma → one non-empty kind after split.
        corpora = expand_type_to_corpora("paper,")
        assert corpora == ["papers"]

    def test_all_respects_precis_kinds_mask(self):
        """When PRECIS_KINDS hides kinds, ``type='all'`` must not
        silently bypass the mask — operators who've narrowed agent
        visibility shouldn't see their restrictions circumvented by
        the cross-corpus convenience."""
        from precis.registry import (
            clear_kinds_mask,
            set_kinds_mask,
        )

        try:
            # Mask limits search to papers + memories only.
            set_kinds_mask(
                {
                    "paper": frozenset({"search", "get"}),
                    "memory": frozenset({"search", "get"}),
                }
            )
            corpora = expand_type_to_corpora("all")
            # Only the masked-in kinds' corpora should appear.
            assert set(corpora) == {"papers", "memories"}
            # Hidden corpora explicitly absent.
            assert "websites" not in corpora
            assert "books" not in corpora
            assert "todos" not in corpora
        finally:
            clear_kinds_mask()

    def test_all_mask_with_no_corpus_backed_kinds_raises(self):
        """If PRECIS_KINDS hides every corpus-backed kind,
        ``type='all'`` must raise with a clear error instead of
        running a cross-corpus search over nothing."""
        from precis.registry import (
            clear_kinds_mask,
            set_kinds_mask,
        )

        try:
            # Mask only includes non-ref-backed kinds.
            set_kinds_mask({"calc": frozenset({"search"})})
            with pytest.raises(PrecisError) as exc_info:
                expand_type_to_corpora("all")
            assert exc_info.value.code == ErrorCode.KIND_UNAVAILABLE
            assert "PRECIS_KINDS" in exc_info.value.cause
        finally:
            clear_kinds_mask()


# ===========================================================================
# Dispatch — search_across_corpora calls the store correctly
# ===========================================================================


class TestSearchAcrossCorpora:
    @pytest.fixture
    def stub_store(self, monkeypatch):
        """Install a stub store that records search_text calls."""
        import precis._store as store_mod

        mock_store = MagicMock()
        mock_store.search_text.return_value = []
        monkeypatch.setattr(store_mod, "_store_singleton", mock_store)
        yield mock_store
        monkeypatch.setattr(store_mod, "_store_singleton", None)

    def test_calls_store_with_corpora_kwarg(self, stub_store):
        search_across_corpora(
            query="MOFs",
            corpora=["papers", "memories"],
            top_k=5,
        )
        args, kwargs = stub_store.search_text.call_args
        assert args == ("MOFs",)
        assert kwargs["corpora"] == ["papers", "memories"]
        assert kwargs["top_k"] == 5

    def test_rejects_scope_combination(self, stub_store):
        """scope= + cross-corpus is nonsensical — must error explicitly."""
        with pytest.raises(PrecisError) as exc_info:
            search_across_corpora(
                query="q",
                corpora=["papers", "memories"],
                top_k=5,
                scope="wang2020state",
            )
        assert "scope" in exc_info.value.cause.lower()
        # Store must NOT be called — we bail out early.
        assert stub_store.search_text.call_count == 0

    def test_handles_empty_hits(self, stub_store):
        stub_store.search_text.return_value = []
        out = search_across_corpora(
            query="nothing-matches",
            corpora=["papers", "memories"],
            top_k=5,
        )
        assert "No hits" in out
        # Both corpora surfaced in the "try these" hint.
        assert "papers" in out
        assert "memories" in out

    def test_renders_grouped_by_kind(self, stub_store):
        """Each hit's corpus_id determines its group; groups are
        rendered in caller-requested order with kind-specific
        badges."""
        stub_store.search_text.return_value = [
            {
                "text": "Paper hit content",
                "distance": 0.12,
                "metadata": {
                    "corpus_id": "papers",
                    "slug": "wang2020state",
                    "ref_title": "Wang 2020",
                    "block_index": 3,
                },
            },
            {
                "text": "Memory hit content",
                "distance": 0.45,
                "metadata": {
                    "corpus_id": "memories",
                    "slug": "meeting-notes",
                    "ref_title": "Oct meeting",
                },
            },
        ]
        out = search_across_corpora(
            query="MOFs",
            corpora=["papers", "memories"],
            top_k=5,
        )
        # Header shows the corpus count
        assert "2 corpora" in out
        # Both kinds' groups present with their slugs
        assert "wang2020state" in out
        assert "meeting-notes" in out
        # Papers badge + memories badge both appear
        assert "📄" in out
        assert "💭" in out

    def test_sorts_within_group_by_distance(self, stub_store):
        """A closer hit (lower distance) comes first within its group."""
        stub_store.search_text.return_value = [
            {
                "text": "far",
                "distance": 0.9,
                "metadata": {
                    "corpus_id": "papers",
                    "slug": "paper-far",
                    "ref_title": "Far",
                },
            },
            {
                "text": "near",
                "distance": 0.1,
                "metadata": {
                    "corpus_id": "papers",
                    "slug": "paper-near",
                    "ref_title": "Near",
                },
            },
        ]
        out = search_across_corpora(
            query="q",
            corpora=["papers"],
            top_k=5,
        )
        near_pos = out.index("paper-near")
        far_pos = out.index("paper-far")
        assert near_pos < far_pos, "closer hit must render first"

    def test_unexpected_corpus_in_hits_is_not_dropped(self, stub_store):
        """If the store returns a hit from a corpus we didn't ask for
        (shouldn't happen, but defensive), it must still surface
        rather than silently disappear."""
        stub_store.search_text.return_value = [
            {
                "text": "surprise",
                "distance": 0.5,
                "metadata": {
                    "corpus_id": "surprise_corpus",
                    "slug": "x",
                    "ref_title": "X",
                },
            },
        ]
        out = search_across_corpora(
            query="q",
            corpora=["papers"],
            top_k=5,
        )
        assert "x" in out  # slug surfaces

    def test_missing_corpus_id_on_hit_is_bucketed_as_other(self, stub_store):
        """A hit with no corpus_id metadata (Chroma-legacy) must still
        render — it's bucketed under '_other'."""
        stub_store.search_text.return_value = [
            {
                "text": "legacy",
                "distance": 0.5,
                "metadata": {
                    "slug": "legacy-hit",
                    "ref_title": "Legacy",
                    # corpus_id intentionally absent
                },
            },
        ]
        out = search_across_corpora(
            query="q",
            corpora=["papers"],
            top_k=5,
        )
        assert "legacy-hit" in out


# ===========================================================================
# End-to-end via server.search()
# ===========================================================================


class TestServerSearchIntegration:
    """Exercise the type='all' dispatch through the MCP search() tool
    itself, so we catch any wiring regressions between the tool and
    the cross-corpus dispatcher."""

    @pytest.fixture
    def stub_store(self, monkeypatch):
        import precis._store as store_mod

        mock_store = MagicMock()
        mock_store.search_text.return_value = [
            {
                "text": "A relevant paper chunk",
                "distance": 0.2,
                "metadata": {
                    "corpus_id": "papers",
                    "slug": "wang2020state",
                    "ref_title": "Wang 2020",
                },
            }
        ]
        monkeypatch.setattr(store_mod, "_store_singleton", mock_store)
        yield mock_store
        monkeypatch.setattr(store_mod, "_store_singleton", None)

    def test_search_type_all(self, stub_store):
        from precis.server import search

        out = search(query="MOFs", type="all", top_k=5)
        assert "Cross-corpus" in out
        assert "wang2020state" in out
        # Store was called with all ref-backed corpora.
        _, kwargs = stub_store.search_text.call_args
        assert "papers" in kwargs["corpora"]
        assert "memories" in kwargs["corpora"]
        assert "books" in kwargs["corpora"]

    def test_search_type_comma_list(self, stub_store):
        from precis.server import search

        out = search(query="MOFs", type="paper,memory", top_k=5)
        assert "Cross-corpus" in out
        _, kwargs = stub_store.search_text.call_args
        assert kwargs["corpora"] == ["papers", "memories"]

    def test_search_type_all_with_scope_is_error(self, stub_store):
        """Cross-corpus + scope= must return an ERROR envelope."""
        from precis.server import search

        out = search(query="MOFs", type="all", scope="wang2020state", top_k=5)
        assert "ERROR" in out
        assert "scope" in out.lower()
        # Store was never called.
        assert stub_store.search_text.call_count == 0

    def test_search_unknown_kind_in_list_is_error(self, stub_store):
        from precis.server import search

        out = search(query="q", type="paper,nonexistent", top_k=5)
        assert "ERROR" in out
        assert "nonexistent" in out
        assert stub_store.search_text.call_count == 0

    def test_search_websearch_in_list_is_error(self, stub_store):
        """External services can't join cross-corpus — clear error."""
        from precis.server import search

        out = search(query="q", type="paper,websearch", top_k=5)
        assert "ERROR" in out
        assert "websearch" in out
        assert stub_store.search_text.call_count == 0

    def test_search_single_kind_uses_single_corpus_filter(self, stub_store):
        """Single-kind search routes through the per-kind handler,
        which now also passes ``corpora=[corpus_id]`` — but as a
        one-item list, not a cross-corpus expansion.  Verify the
        handler is not mistakenly passing 'all' corpora."""
        from precis.server import search

        search(query="q", type="paper", top_k=5)
        # If search_text was called, it should be scoped to exactly
        # one corpus (the handler's own) — never a multi-corpus list
        # and never the cross-corpus union.
        for call in stub_store.search_text.call_args_list:
            _, kwargs = call
            corpora = kwargs.get("corpora")
            if corpora is not None:
                assert corpora == ["papers"], (
                    "single-kind search must scope to its own corpus only, "
                    f"not {corpora}"
                )

    def test_search_empty_query_rejected_before_cross_corpus(
        self, stub_store
    ):
        """Empty query validation runs first — even for type='all'."""
        from precis.server import search

        out = search(query="", type="all", top_k=5)
        assert "ERROR" in out or "required" in out.lower()
        assert stub_store.search_text.call_count == 0


# ===========================================================================
# Per-kind semantic search (formerly grep-only)
#
# Before the corpus_id filter landed, non-paper kinds (memory, web,
# book, todo, flashcard, conversation) fell back to keyword grep
# because ``search_text`` would otherwise return paper hits mixed in.
# Now that ``search_text(corpora=[...])`` filters properly, every
# ref-backed kind actually uses the vector index.  These tests lock
# in the new wiring so a regression would be caught immediately.
# ===========================================================================


class TestPerKindSemanticSearch:
    """Each ref-backed handler's ``_search`` should call
    ``store.search_text(query, top_k=..., corpora=[own_corpus])``."""

    @pytest.fixture
    def stub_store(self, monkeypatch):
        import precis._store as store_mod

        mock_store = MagicMock()
        mock_store.search_text.return_value = []
        # list_papers is consulted on the paper path by the grep-merge
        # code; return an empty list so any fallback path still works.
        mock_store.list_papers.return_value = []
        monkeypatch.setattr(store_mod, "_store_singleton", mock_store)
        yield mock_store
        monkeypatch.setattr(store_mod, "_store_singleton", None)

    def test_memory_search_uses_memories_corpus(self, stub_store):
        from precis.server import search

        search(query="meeting", type="memory", top_k=3)
        # The memory handler eventually calls store.search_text with a
        # 'corpora' filter — assert it's present and scopes to 'memories'.
        found_memory_call = False
        for call in stub_store.search_text.call_args_list:
            _, kwargs = call
            if kwargs.get("corpora") == ["memories"]:
                found_memory_call = True
                break
        assert found_memory_call, (
            "memory search must filter to the 'memories' corpus — "
            f"calls were: {stub_store.search_text.call_args_list}"
        )

    def test_web_search_uses_websites_corpus(self, stub_store):
        from precis.server import search

        search(query="github", type="web", top_k=3)
        found = any(
            call.kwargs.get("corpora") == ["websites"]
            for call in stub_store.search_text.call_args_list
        )
        assert found, (
            "web search must filter to the 'websites' corpus — "
            f"calls were: {stub_store.search_text.call_args_list}"
        )

    def test_book_search_uses_books_corpus(self, stub_store):
        from precis.server import search

        search(query="Feynman", type="book", top_k=3)
        found = any(
            call.kwargs.get("corpora") == ["books"]
            for call in stub_store.search_text.call_args_list
        )
        assert found, (
            "book search must filter to the 'books' corpus — "
            f"calls were: {stub_store.search_text.call_args_list}"
        )

    def test_paper_search_scopes_to_papers_corpus(self, stub_store):
        from precis.server import search

        search(query="MOFs", type="paper", top_k=3)
        # Paper may hit search_text multiple times (e.g. grep-prefilter
        # path) — at least one call should scope to papers.
        found = any(
            call.kwargs.get("corpora") == ["papers"]
            for call in stub_store.search_text.call_args_list
        )
        assert found, (
            "paper search must scope to the 'papers' corpus — "
            f"calls were: {stub_store.search_text.call_args_list}"
        )
