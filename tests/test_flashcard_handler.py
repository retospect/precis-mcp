"""Tests for FlashcardHandler — creation, review, due, stats."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from precis.handlers.flashcard import (
    FlashcardHandler,
    _last_review_note,
    _now,
    _parse_meta,
    _relative_due,
    _slugify,
)
from precis.handlers.sm2 import DEFAULT_EASINESS
from precis.protocol import PrecisError

_PATCH_STORE_REF = "precis.handlers._ref_base._get_store"
_PATCH_STORE_FC = "precis.handlers.flashcard._get_store"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_handler():
    return FlashcardHandler()


def _mock_store(refs=None, blocks=None):
    """Create a mock store with flashcard-aware helpers."""
    store = MagicMock()

    _refs = {r["slug"]: r for r in (refs or [])}

    def _get(ident):
        if ident in _refs:
            return _refs[ident]
        for r in _refs.values():
            if r.get("ref_id") == ident or r.get("id") == ident:
                return r
        return None

    store.get.side_effect = _get
    store.list_papers.return_value = refs or []
    store.get_blocks.return_value = blocks or []
    store.get_toc.return_value = []
    store.get_links.return_value = []
    store.get_link_count.return_value = {}
    store.search_text.return_value = []
    store.create_ref.return_value = 42
    store.update_ref_metadata.return_value = None
    store.update_block_text.return_value = None

    return store


def _fc_ref(
    slug="fc:paris-capital",
    title="Paris is the capital of France",
    easiness=DEFAULT_EASINESS,
    interval=6,
    reps=2,
    next_review=None,
    last_reviewed=None,
    review_log=None,
    ref_id=1,
):
    now = _now()
    return {
        "slug": slug,
        "title": title,
        "ref_id": ref_id,
        "id": ref_id,
        "corpus_id": "flashcards",
        "meta": {
            "easiness": easiness,
            "interval": interval,
            "reps": reps,
            "next_review": (next_review or now).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_reviewed": (last_reviewed or now).strftime("%Y-%m-%dT%H:%M:%SZ") if last_reviewed else None,
            "review_log": review_log or [],
        },
    }


def _due_ref(slug="fc:due-item", title="A due item", days_overdue=1, **kwargs):
    """Create a ref that is overdue by N days."""
    now = _now()
    due = now - timedelta(days=days_overdue)
    return _fc_ref(slug=slug, title=title, next_review=due, **kwargs)


def _future_ref(slug="fc:future-item", title="A future item", days_ahead=5, **kwargs):
    """Create a ref that is due in N days."""
    now = _now()
    due = now + timedelta(days=days_ahead)
    return _fc_ref(slug=slug, title=title, next_review=due, **kwargs)


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic(self):
        assert _slugify("Paris is the capital") == "fc:paris-is-the-capital"

    def test_special_chars(self):
        result = _slugify("Bragg's law: nλ = 2d sin θ")
        assert result.startswith("fc:")
        assert " " not in result

    def test_empty(self):
        assert _slugify("") == ""

    def test_truncation(self):
        long = "a" * 100
        result = _slugify(long)
        # fc: prefix + 60 chars max
        assert len(result) <= 63


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------


class TestMetaHelpers:
    def test_parse_meta_dict(self):
        ref = {"meta": {"easiness": 2.5}}
        assert _parse_meta(ref) == {"easiness": 2.5}

    def test_parse_meta_string(self):
        ref = {"meta": '{"easiness": 2.5}'}
        assert _parse_meta(ref)["easiness"] == 2.5

    def test_parse_meta_empty(self):
        assert _parse_meta({}) == {}

    def test_parse_meta_metadata_key(self):
        ref = {"metadata": {"easiness": 1.8}}
        assert _parse_meta(ref)["easiness"] == 1.8

    def test_relative_due_overdue(self):
        meta = {"next_review": (_now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        result = _relative_due(meta)
        assert "overdue" in result

    def test_relative_due_today(self):
        meta = {"next_review": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}
        result = _relative_due(meta)
        assert "today" in result

    def test_relative_due_future(self):
        meta = {"next_review": (_now() + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        result = _relative_due(meta)
        assert "due in" in result

    def test_last_review_note(self):
        meta = {"review_log": [
            {"date": "2026-04-01", "quality": 4, "note": "first"},
            {"date": "2026-04-08", "quality": 2, "note": "confused with Lyon"},
        ]}
        assert _last_review_note(meta) == "confused with Lyon"

    def test_last_review_note_empty(self):
        assert _last_review_note({}) is None
        assert _last_review_note({"review_log": []}) is None


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


class TestCreate:
    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_create_item(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        result = h.put("", None, "Paris is the capital of France", "append")

        assert "Created" in result
        assert "fc:" in result
        store.create_ref.assert_called_once()
        call_kwargs = store.create_ref.call_args
        assert call_kwargs.kwargs["corpus_id"] == "flashcards"
        meta = call_kwargs.kwargs["metadata"]
        assert meta["easiness"] == DEFAULT_EASINESS
        assert meta["reps"] == 0

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_create_empty_text_raises(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        with pytest.raises(PrecisError, match="text required"):
            h.put("", None, "", "append")

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_create_disambiguates_slug(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        # First call fails (collision), second succeeds
        store.create_ref.side_effect = [ValueError("already exists"), 42]

        h = _make_handler()
        result = h.put("", None, "Paris is the capital", "append")
        assert "Created" in result
        assert store.create_ref.call_count == 2


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


class TestReview:
    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_review_updates_sm2(self, mock_ref_store, mock_fc_store):
        ref = _fc_ref(easiness=2.5, interval=6, reps=2)
        store = _mock_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        result = h.put("fc:paris-capital", None, "4", "review", note="got it")

        assert "reviewed" in result
        assert "quality 4" in result
        store.update_ref_metadata.assert_called_once()
        call_args = store.update_ref_metadata.call_args
        new_meta = call_args.args[1]
        assert new_meta["reps"] == 3
        assert new_meta["easiness"] >= 2.0
        assert new_meta["interval"] > 6
        # Review log should have the entry
        assert len(new_meta["review_log"]) == 1
        assert new_meta["review_log"][0]["quality"] == 4
        assert new_meta["review_log"][0]["note"] == "got it"

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_review_failed(self, mock_ref_store, mock_fc_store):
        ref = _fc_ref(easiness=2.5, interval=30, reps=5)
        store = _mock_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        result = h.put("fc:paris-capital", None, "1", "review")

        assert "reviewed" in result
        new_meta = store.update_ref_metadata.call_args.args[1]
        assert new_meta["reps"] == 0
        assert new_meta["interval"] == 1

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_review_invalid_quality(self, mock_ref_store, mock_fc_store):
        ref = _fc_ref()
        store = _mock_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        with pytest.raises(PrecisError, match="quality must be 0-5"):
            h.put("fc:paris-capital", None, "7", "review")

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_review_non_numeric(self, mock_ref_store, mock_fc_store):
        ref = _fc_ref()
        store = _mock_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        with pytest.raises(PrecisError, match="quality must be 0-5"):
            h.put("fc:paris-capital", None, "great", "review")

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_review_appends_to_existing_log(self, mock_ref_store, mock_fc_store):
        ref = _fc_ref(
            review_log=[
                {"date": "2026-04-01", "quality": 3, "note": "first review"},
            ]
        )
        store = _mock_store(refs=[ref])
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        h.put("fc:paris-capital", None, "5", "review", note="perfect")

        new_meta = store.update_ref_metadata.call_args.args[1]
        assert len(new_meta["review_log"]) == 2
        assert new_meta["review_log"][0]["note"] == "first review"
        assert new_meta["review_log"][1]["note"] == "perfect"

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_review_no_slug_raises(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        with pytest.raises(PrecisError, match="slug required"):
            h.put("", None, "4", "review")


# ---------------------------------------------------------------------------
# Due view
# ---------------------------------------------------------------------------


class TestDueView:
    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_due_shows_overdue_items(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        # Patch _query_corpus_refs to return test data
        due_item = _due_ref(days_overdue=2)
        future_item = _future_ref(days_ahead=10)
        h._query_corpus_refs = MagicMock(return_value=[due_item, future_item])

        result = h.read("", None, "due", None, "", False, 0, 0)
        assert "1 items due" in result
        assert "fc:due-item" in result
        assert "overdue" in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_due_empty(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        h._query_corpus_refs = MagicMock(return_value=[_future_ref(days_ahead=10)])

        result = h.read("", None, "due", None, "", False, 0, 0)
        assert "No items due" in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_due_shows_review_notes(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        item = _due_ref(
            review_log=[{"date": "2026-04-06", "quality": 2, "note": "confused with Lyon"}]
        )
        h._query_corpus_refs = MagicMock(return_value=[item])

        result = h.read("", None, "due", None, "", False, 0, 0)
        assert "confused with Lyon" in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_due_shows_nearby_almost_due(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        due_item = _due_ref()
        almost = _future_ref(slug="fc:almost-due", title="Almost due", days_ahead=2)
        far = _future_ref(slug="fc:far-away", title="Far away", days_ahead=30)
        h._query_corpus_refs = MagicMock(return_value=[due_item, almost, far])

        result = h.read("", None, "due", None, "", False, 0, 0)
        assert "almost due" in result.lower() or "Nearby" in result
        assert "fc:almost-due" in result
        assert "fc:far-away" not in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_due_includes_review_tips(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        h._query_corpus_refs = MagicMock(return_value=[_due_ref()])

        result = h.read("", None, "due", None, "", False, 0, 0)
        assert "Review tips:" in result
        assert "mode='review'" in result


# ---------------------------------------------------------------------------
# Stats view
# ---------------------------------------------------------------------------


class TestStatsView:
    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_stats_basic(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        refs = [
            _fc_ref(slug="fc:a", reps=0, ref_id=1),  # new
            _fc_ref(slug="fc:b", reps=3, interval=25, ref_id=2),  # mature
            _fc_ref(slug="fc:c", reps=1, interval=3, ref_id=3),  # young
        ]
        h._query_corpus_refs = MagicMock(return_value=refs)

        result = h.read("", None, "stats", None, "", False, 0, 0)
        assert "Total items:" in result
        assert "3" in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_stats_shows_struggle_spots(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        hard_item = _fc_ref(
            slug="fc:hard",
            easiness=1.4,
            review_log=[{"date": "2026-04-08", "quality": 1, "note": "always wrong"}],
            ref_id=1,
        )
        easy_item = _fc_ref(slug="fc:easy", easiness=2.8, ref_id=2)
        h._query_corpus_refs = MagicMock(return_value=[hard_item, easy_item])

        result = h.read("", None, "stats", None, "", False, 0, 0)
        assert "Struggle spots" in result
        assert "fc:hard" in result
        assert "always wrong" in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_stats_empty(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        h._query_corpus_refs = MagicMock(return_value=[])

        result = h.read("", None, "stats", None, "", False, 0, 0)
        assert "No flashcard items" in result


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestOverview:
    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_overview_shows_entry_points(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        h._query_corpus_refs = MagicMock(return_value=[_fc_ref()])

        result = h.read("", None, None, None, "", False, 0, 0)
        assert "fc:/due" in result
        assert "fc:/stats" in result
        assert "mode='append'" in result

    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_item_overview(self, mock_ref_store, mock_fc_store):
        ref = _fc_ref(
            review_log=[{"date": "2026-04-08", "quality": 2, "note": "said Lyon"}]
        )
        store = _mock_store(refs=[ref], blocks=[{"text": "Paris is the capital of France"}])
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        result = h.read("fc:paris-capital", None, None, None, "", False, 0, 0)
        assert "fc:paris-capital" in result
        assert "said Lyon" in result
        assert "mode='review'" in result


# ---------------------------------------------------------------------------
# Unsupported mode
# ---------------------------------------------------------------------------


class TestUnsupportedMode:
    @patch(_PATCH_STORE_FC)
    @patch(_PATCH_STORE_REF)
    def test_bad_mode_raises(self, mock_ref_store, mock_fc_store):
        store = _mock_store()
        mock_ref_store.return_value = store
        mock_fc_store.return_value = store

        h = _make_handler()
        with pytest.raises(PrecisError, match="Unsupported mode"):
            h.put("", None, "test", "badmode")
