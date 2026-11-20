"""Authoritative journal record lookup, cache lifetime, and failure semantics."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from precis.ingest import crossref, journal_record
from precis.store import Store
from precis.utils.rate_limit import RateLimited


def test_fetch_uses_journals_not_works(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    message = {"title": "Energy & Environmental Materials", "ISSN": ["2575-0356"]}
    client.journals.return_value = {"message": message}
    monkeypatch.setattr(crossref, "Crossref", lambda **kwargs: client)
    monkeypatch.setattr("precis.utils.rate_limit.require", lambda *args, **kwargs: None)
    assert crossref.fetch_journal("2575-0356") == message
    client.journals.assert_called_once_with(ids="2575-0356")
    client.works.assert_not_called()


def test_fetch_miss_and_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(crossref, "Crossref", lambda **kwargs: client)
    # Keep the provider retry policy out of this error-mapping unit test.
    monkeypatch.setattr(crossref, "call_with_backoff", lambda fn, **kwargs: fn())
    response = requests.Response()
    response.status_code = 404
    client.journals.side_effect = requests.HTTPError(response=response)
    assert crossref.fetch_journal("2575-0356") is None
    client.journals.side_effect = TimeoutError("registry unavailable")
    with pytest.raises(TimeoutError):
        crossref.fetch_journal("2575-0356")
    client.journals.side_effect = RateLimited("crossref")
    with pytest.raises(RateLimited):
        crossref.fetch_journal("2575-0356")


def test_cache_refresh_and_linked_issns(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = MagicMock(
        return_value={
            "ISSN": ["2575-0356", "2575-0348"],
            "title": "Energy & Environmental Materials",
        }
    )
    monkeypatch.setattr(journal_record, "fetch_journal", fetch)
    assert (
        journal_record.title_of_record(store, "2575-0356")
        == "Energy & Environmental Materials"
    )
    assert (
        journal_record.title_of_record(store, "2575-0348")
        == "Energy & Environmental Materials"
    )
    fetch.assert_called_once()
    key = "journal-record:crossref:2575-0356"
    cached = store.get_setting(key)
    assert cached is not None
    record = json.loads(cached)
    record["resolved_at"] = "2000-01-01T00:00:00+00:00"
    store.set_setting(key, json.dumps(record))
    journal_record.title_of_record(store, "2575-0356")
    assert fetch.call_count == 2


def test_venue_key_settles_escaped_all_caps_container_title() -> None:
    """gr685099: Crossref works serves the container title escaped and
    ALL CAPS; the /journals record does not. Same spelling, one key."""
    raw = "ENERGY &amp; ENVIRONMENTAL MATERIALS"
    record = "Energy & Environmental Materials"
    assert journal_record.venue_key(raw) == journal_record.venue_key(record)
    # Numeric entities decode the same way.
    assert journal_record.venue_key("ENERGY &#38; ENVIRONMENTAL MATERIALS") == (
        journal_record.venue_key(record)
    )


def test_missing_record_is_cached_without_guessing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetch = MagicMock(return_value=None)
    monkeypatch.setattr(journal_record, "fetch_journal", fetch)
    assert journal_record.title_of_record(store, "2575-0356") is None
    assert journal_record.title_of_record(store, "2575-0356") is None
    fetch.assert_called_once()
    assert journal_record.title_of_record(store, "not-an-issn") is None
    fetch.assert_called_once()
