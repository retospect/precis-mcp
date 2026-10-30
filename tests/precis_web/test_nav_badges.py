"""Nav badges mirror each page's default filter (td541981).

* Gripes badge = ``STATUS:triaged`` (the pick list), the same set
  ``/gripes`` opens on — not the 195-strong live list.
* Needs-you badge = showstopper + ``ask-user`` decision rows from
  ``asks_model``; discussions and a bare ``waiting-for:reto`` stay off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.store.types import Tag
from precis_web.nav import _asks_count, _gripes_count
from tests._refs import insert_gripe


@pytest.fixture(autouse=True)
def _no_review_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``asks_model`` also reads the checkout's review-queue files; point it
    at an empty dir so the count sees only the rows the test seeds."""
    empty = tmp_path / "review-queue"
    empty.mkdir()
    monkeypatch.setenv("PRECIS_REVIEW_QUEUE_DIR", str(empty))


def _todo(store: Any, title: str, *tags: str) -> int:
    ref = store.insert_ref(kind="todo", slug=None, title=title, meta={})
    for t in tags:
        store.add_tag(ref.id, Tag.open(t))
    return int(ref.id)


def test_gripes_badge_counts_triaged_only(store: Any) -> None:
    insert_gripe(store, "pick-me", status="triaged")
    insert_gripe(store, "not-yet", status="open")
    insert_gripe(store, "in-flight", status="verify")
    insert_gripe(store, "finished", status="done")
    assert _gripes_count(store) == 1


def test_gripes_badge_skips_a_retired_triaged_gripe(store: Any) -> None:
    keep = insert_gripe(store, "pick-me", status="triaged")
    gone = insert_gripe(store, "merged-away", status="triaged")
    store.retire_ref(gone.id)
    assert keep.id != gone.id
    assert _gripes_count(store) == 1


def test_needs_you_counts_claimed_waiting_for_but_not_a_bare_one(store: Any) -> None:
    """Acceptance: one claimed waiting-for:reto slice plus one bare
    waiting-for todo reads 1 — the claimed one is a showstopper, the bare
    one an unclaimed decision without an ask-user."""
    _todo(store, "claimed and stopped", "waiting-for:reto", "claimed-by:slice-x")
    _todo(store, "bare waiting", "waiting-for:reto")
    assert _asks_count(store) == 1


def test_needs_you_counts_ask_user_decisions_and_showstoppers(store: Any) -> None:
    _todo(store, "which database?", "ask-user:which database?")
    _todo(store, "plain ask", "ask-user")
    _todo(store, "lane stopped", "showstopper")
    assert _asks_count(store) == 3


def test_needs_you_leaves_out_discussions_and_ruling_gripes(store: Any) -> None:
    _todo(store, "shape question", "discussion")
    gripe = insert_gripe(store, "needs a ruling", status="triaged")
    store.add_tag(gripe.id, Tag.open("needs:ruling"))
    assert _asks_count(store) == 0


def test_needs_you_drops_a_closed_ask(store: Any) -> None:
    done = _todo(store, "answered", "ask-user")
    store.add_tag(done, Tag.closed("STATUS", "done"), set_by="agent")
    _todo(store, "still open", "ask-user")
    assert _asks_count(store) == 1


def test_gripes_default_filter_is_the_badge_set(
    store: Any, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /gripes`` opens on triaged: the triaged row is inline, the
    open row is behind its (collapsed) chip, and the triaged chip's count
    equals the badge."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-triaged-pick", status="triaged")
    insert_gripe(store, "fixture-open-hidden", status="open")
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    page = client.get("/gripes").text
    assert '<details class="mb-3 group" data-status="triaged" open>' in page
    assert "fixture-triaged-pick" in page
    assert "fixture-open-hidden" not in page
    assert 'hx-get="/gripes/rows?status=open"' in page
    assert _gripes_count(store) == 1


@pytest.mark.parametrize("status", ["live", "all"])
def test_gripes_status_param_reaches_the_big_lists(
    store: Any, client: Any, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """``?status=live`` / ``all`` stay reachable: the cohort renders inline
    under a synthetic leading chip carrying its summed count."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-triaged-pick", status="triaged")
    insert_gripe(store, "fixture-open-too", status="open")
    insert_gripe(store, "fixture-done-row", status="done")
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    page = client.get(f"/gripes?status={status}").text
    assert f'data-status="{status}" open' in page
    assert "fixture-triaged-pick" in page
    assert "fixture-open-too" in page
    assert ("fixture-done-row" in page) is (status == "all")


def test_gripes_unknown_status_param_is_a_400(
    store: Any, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis_web.routes import gripes

    monkeypatch.setattr(gripes, "get_store", lambda request: store)
    resp = client.get("/gripes?status=bogus")
    assert resp.status_code == 400
    assert "triaged" in resp.text
