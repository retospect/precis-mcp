"""Nav badges mirror each page's default filter (td541981).

* Gripes badge = the ``attention`` cohort — ``open`` + ``triaged`` +
  ``auto_verify``, the not-properly-closed set (gr548577) that
  ``/gripes?status=attention`` lists. Leased ``fixing`` and shelved
  ``needs_spec``/``parked`` stay out; ``user_verify`` already rides
  the Needs-you ask todo.
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


def test_gripes_badge_counts_the_attention_cohort(store: Any) -> None:
    """gr548577: the badge sums open + triaged + auto_verify — everything
    not properly closed that still needs a pickup or the release check."""
    insert_gripe(store, "filed", status="open")
    insert_gripe(store, "pick-me", status="triaged")
    insert_gripe(store, "landed", status="auto_verify")
    for status in (
        "fixing",
        "in_review",
        "user_verify",
        "needs_spec",
        "parked",
        "verify",
        "ready_for_fix",
    ):
        insert_gripe(store, f"excluded-{status}", status=status)
    insert_gripe(store, "closed-done", status="done")
    insert_gripe(store, "closed-wontfix", status="wontfix")
    assert _gripes_count(store) == 3


def test_gripes_badge_skips_a_retired_cohort_gripe(store: Any) -> None:
    keep = insert_gripe(store, "pick-me", status="triaged")
    gone = insert_gripe(store, "merged-away", status="auto_verify")
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


def test_gripes_default_filter_is_the_pick_list(
    store: Any, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /gripes`` still opens on triaged: the triaged row is inline,
    the open row is behind its (collapsed) chip — but the nav badge now
    counts the wider ``attention`` cohort (gr548577)."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-triaged-pick", status="triaged")
    insert_gripe(store, "fixture-open-hidden", status="open")
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    page = client.get("/gripes").text
    assert '<details class="mb-3 group" data-status="triaged" open>' in page
    assert "fixture-triaged-pick" in page
    assert "fixture-open-hidden" not in page
    assert 'hx-get="/gripes/rows?status=open"' in page
    assert _gripes_count(store) == 2


def test_gripes_badge_lands_on_the_attention_cohort(
    store: Any, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The badge links to ``/gripes?status=attention``, which lists the
    same open + triaged + auto_verify set the count sums (gr548577)."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-open-listed", status="open")
    insert_gripe(store, "fixture-triaged-listed", status="triaged")
    insert_gripe(store, "fixture-autoverify-listed", status="auto_verify")
    insert_gripe(store, "fixture-fixing-hidden", status="fixing")
    insert_gripe(store, "fixture-parked-hidden", status="parked")
    insert_gripe(store, "fixture-done-hidden", status="done")
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    assert 'href="/gripes?status=attention"' in client.get("/gripes").text
    page = client.get("/gripes?status=attention").text
    assert 'data-status="attention" open' in page
    for listed in (
        "fixture-open-listed",
        "fixture-triaged-listed",
        "fixture-autoverify-listed",
    ):
        assert listed in page
    for hidden in (
        "fixture-fixing-hidden",
        "fixture-parked-hidden",
        "fixture-done-hidden",
    ):
        assert hidden not in page


@pytest.mark.parametrize("status", ["live", "all", "attention"])
def test_gripes_status_param_reaches_the_cohort_lists(
    store: Any, client: Any, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """``?status=live`` / ``all`` / ``attention`` stay reachable: the
    cohort renders inline under a synthetic leading chip carrying its
    summed count."""
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
