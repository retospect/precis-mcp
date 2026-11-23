"""``walk_cards`` — the /eye walk → ``twin:asked`` bridge (gr743232).

The daily pass queues the concepts an owner focused directly or met one
hop out via ``queue_asked_term`` (``source="walk"``): never mints a term,
skips carded/mastered/already-queued, and the forge stamps the minted
card's ``meta.source``/``meta.walk_date``. Runs against real PG (the
``store`` fixture) like the asked-queue tests.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from precis.eye_visits import RETENTION_DAYS, EyeVisitStore
from precis.reading.asked import ASKED_TAG, unqueue_asked_term
from precis.reading.flashcards import author_card, mint_daily_cards
from precis.reading.promote import create_concept
from precis.store import Tag
from precis.user_context import user_scope
from precis.users import hash_password
from precis.workers.walk_cards import run_walk_cards_pass


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._text = json.dumps(payload)
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=5)


def _user(store: Any, login: str | None = None) -> Any:
    return store.create_web_user(
        login=login or f"w{uuid.uuid4().hex[:8]}",
        abbrev=f"{(login or 'w')[:2]}{uuid.uuid4().hex[:1]}",
        password=hash_password("pw"),
    )


def _concept(store: Any, name: str | None = None, **kwargs: Any) -> int:
    return create_concept(
        store,
        name=name or f"concept-{uuid.uuid4().hex[:10]}",
        definition="a seeded test idea",
        **kwargs,
    )


def _walk(store: Any, user: Any, handle: str, kind: str = "concept") -> None:
    with user_scope(user.id):
        EyeVisitStore(store).record(handle=handle, kind=kind)


def _tags(store: Any, ref_id: int) -> set[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s",
            (ref_id,),
        ).fetchall()
    return {r[0] for r in rows}


def _subs(store: Any, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="concept", id=ref_id)
    subs = (ref.meta or {}).get("asked_submissions") or {}
    return dict(subs)


def _concept_count(store: Any) -> int:
    with store.pool.connection() as conn:
        return int(
            conn.execute(
                "SELECT count(*) FROM refs WHERE kind='concept' AND retired_at IS NULL"
            ).fetchone()[0]
        )


@pytest.mark.db
def test_focused_concept_is_queued_for_its_owner(store: Any) -> None:
    user = _user(store)
    cid = _concept(store)
    _walk(store, user, f"cn{cid}")

    result = run_walk_cards_pass(store)

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "owners": 1}
    assert ASKED_TAG in _tags(store, cid)
    ref = store.get_ref(kind="concept", id=cid)
    assert ref.meta["asked_count"] == 1
    assert ref.meta["asked_by"] == user.login
    [(_token, entry)] = _subs(store, cid).items()
    assert entry["source"] == "walk"
    assert entry["owner"] == user.login
    assert entry["at"]


@pytest.mark.db
def test_one_hop_concept_is_queued_but_two_hops_is_not(store: Any) -> None:
    user = _user(store)
    anchor = store.insert_ref(kind="memory", slug=None, title="walked node")
    near = _concept(store)
    far = _concept(store)
    other = store.insert_ref(kind="memory", slug=None, title="unwalked node")
    store.add_link(src_ref_id=anchor.id, dst_ref_id=near, relation="related-to")
    store.add_link(src_ref_id=other.id, dst_ref_id=far, relation="related-to")
    _walk(store, user, f"me{anchor.id}", kind="memory")

    run_walk_cards_pass(store)

    assert ASKED_TAG in _tags(store, near)
    assert ASKED_TAG not in _tags(store, far)


@pytest.mark.db
def test_chunk_focus_queues_the_owning_ref_neighbour(store: Any) -> None:
    """A ``pc``-style chunk handle resolves to its owning ref for the
    one-hop rule."""
    from precis.store.types import ChunkInsert

    user = _user(store)
    paper = store.insert_ref(
        kind="paper", slug=f"walked-{uuid.uuid4().hex[:8]}", title="walked paper"
    )
    [chunk] = store.chunks.insert_chunks(
        paper.id, [ChunkInsert(ord=0, text="body", meta={})]
    )
    cid = _concept(store)
    store.add_link(src_ref_id=paper.id, dst_ref_id=cid, relation="related-to")
    _walk(store, user, f"pc{chunk.id}", kind="paper")

    run_walk_cards_pass(store)

    assert ASKED_TAG in _tags(store, cid)


@pytest.mark.db
def test_mastered_carded_and_forge_skipped_are_not_queued(store: Any) -> None:
    user = _user(store)
    mastered = _concept(store)
    carded = _concept(store)
    declined = _concept(store)
    store.update_ref(mastered, meta_patch={"state": "mastered"})
    author_card(
        store,
        text="Water boils at {{c1::100}} degrees.",
        concept_id=carded,
        deck="Precis::reading",
        owner_login=user.login,
    )
    store.update_ref(declined, meta_patch={"card_forge_skip": "not a card"})
    for cid in (mastered, carded, declined):
        _walk(store, user, f"cn{cid}")

    result = run_walk_cards_pass(store)

    assert result["claimed"] == 0 and result["ok"] == 0
    for cid in (mastered, carded, declined):
        assert ASKED_TAG not in _tags(store, cid)
        assert _subs(store, cid) == {}


@pytest.mark.db
def test_a_second_pass_does_not_requeue(store: Any) -> None:
    """The live-walk-submission dedupe makes the daily re-read idempotent."""
    user = _user(store)
    cid = _concept(store)
    _walk(store, user, f"cn{cid}")

    run_walk_cards_pass(store)
    again = run_walk_cards_pass(store)

    assert again["claimed"] == 0 and again["ok"] == 0
    ref = store.get_ref(kind="concept", id=cid)
    assert ref.meta["asked_count"] == 1
    assert len(_subs(store, cid)) == 1


@pytest.mark.db
def test_unqueue_withdraws_the_walk_submission(store: Any) -> None:
    user = _user(store)
    cid = _concept(store)
    _walk(store, user, f"cn{cid}")
    run_walk_cards_pass(store)
    (token,) = _subs(store, cid)

    out = unqueue_asked_term(store, cid, submission=token, owner_login=user.login)

    assert out is not None and out["undone"] is True
    assert _subs(store, cid) == {}


@pytest.mark.db
def test_the_pass_never_mints_a_term(store: Any) -> None:
    """A walk over non-concept handles and dead handles queues nothing
    and creates nothing (the contract's hard rule)."""
    user = _user(store)
    todo = store.insert_ref(kind="todo", slug=None, title="walked todo")
    _walk(store, user, f"td{todo.id}", kind="todo")
    _walk(store, user, "td4242424242", kind="todo")  # not-found walk
    before = _concept_count(store)

    result = run_walk_cards_pass(store)

    assert result["ok"] == 0 and result["claimed"] == 0
    assert _concept_count(store) == before


@pytest.mark.db
def test_another_logins_private_concept_is_not_queued(store: Any) -> None:
    """Owner-bound visibility is what keeps the queue a guaranteed
    self-match — a foreign-owned concept is out of the candidate set."""
    user = _user(store)
    other = _user(store)
    cid = _concept(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET owner_login = %s WHERE ref_id = %s", (other.login, cid)
        )
    _walk(store, user, f"cn{cid}")
    before = _concept_count(store)

    result = run_walk_cards_pass(store)

    assert result["ok"] == 0
    assert _concept_count(store) == before
    assert ASKED_TAG not in _tags(store, cid)


@pytest.mark.db
def test_another_logins_secret_concept_is_not_queued(store: Any) -> None:
    """``user_secret:<other>`` is the read-path gate the walk pass must
    honour too — a hop through the walk can't smuggle a hidden concept
    into this owner's queue."""
    user = _user(store)
    other = _user(store)
    cid = _concept(store)
    store.add_tag(cid, Tag.open(f"user_secret:{other.abbrev}"), set_by="system")
    _walk(store, user, f"cn{cid}")
    before = _concept_count(store)

    result = run_walk_cards_pass(store)

    assert result["ok"] == 0
    assert _concept_count(store) == before
    assert ASKED_TAG not in _tags(store, cid)


@pytest.mark.db
def test_the_owners_own_secret_concept_is_queued(store: Any) -> None:
    """Their own ``user_secret:<me>`` concept stays inside the fence."""
    user = _user(store)
    cid = _concept(store)
    store.add_tag(cid, Tag.open(f"user_secret:{user.abbrev}"), set_by="system")
    _walk(store, user, f"cn{cid}")

    result = run_walk_cards_pass(store)

    assert result["ok"] == 1
    assert ASKED_TAG in _tags(store, cid)


@pytest.mark.db
def test_an_se_slug_focus_form_queues_the_one_hop_concept(store: Any) -> None:
    """``hcol-a2~p``-style walks resolve their slug base to the ``se``
    ref — the same fallback ``_handle_target`` runs."""
    user = _user(store)
    slug = f"hcol-{uuid.uuid4().hex[:6]}"
    design = store.insert_ref(kind="se", slug=slug, title="walked design")
    cid = _concept(store)
    store.add_link(src_ref_id=design.id, dst_ref_id=cid, relation="related-to")
    _walk(store, user, f"{slug}~p", kind="se")

    run_walk_cards_pass(store)

    assert ASKED_TAG in _tags(store, cid)


@pytest.mark.db
def test_a_name_collision_can_not_misroute_the_submission(store: Any) -> None:
    """If ``queue_asked_term``'s name match lands on a different node
    (an own same-named concept sorts before a shared walk-met one), the
    pass withdraws — a walk submission lands only on a met node."""
    user = _user(store)
    shared = _concept(store, name="collision-term")
    own = _concept(store, name="collision-term")
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET owner_login = %s WHERE ref_id = %s", (user.login, own)
        )
    _walk(store, user, f"cn{shared}")

    result = run_walk_cards_pass(store)

    assert result["failed"] == 1 and result["ok"] == 0
    assert _subs(store, shared) == {}
    assert _subs(store, own) == {}
    shared_ref = store.get_ref(kind="concept", id=shared)
    assert not shared_ref.meta.get("asked_count")


@pytest.mark.db
def test_disabled_owner_is_skipped(store: Any) -> None:
    user = _user(store)
    cid = _concept(store)
    _walk(store, user, f"cn{cid}")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE web_users SET disabled_at = now() WHERE id = %s", (user.id,)
        )
        conn.commit()

    result = run_walk_cards_pass(store)

    assert result["owners"] == 0 and result["ok"] == 0
    assert ASKED_TAG not in _tags(store, cid)


@pytest.mark.db
def test_steps_outside_the_retention_window_are_ignored(store: Any) -> None:
    user = _user(store)
    cid = _concept(store)
    _walk(store, user, f"cn{cid}")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE eye_visits SET visited_at = now() - make_interval(days => %s) "
            "WHERE user_id = %s",
            (RETENTION_DAYS + 1, user.id),
        )
        conn.commit()

    result = run_walk_cards_pass(store)

    assert result["owners"] == 0 and result["ok"] == 0
    assert ASKED_TAG not in _tags(store, cid)


@pytest.mark.db
def test_each_owner_queues_against_their_own_walk(store: Any) -> None:
    first, second = _user(store), _user(store)
    mine, theirs = _concept(store), _concept(store)
    _walk(store, first, f"cn{mine}")
    _walk(store, second, f"cn{theirs}")

    run_walk_cards_pass(store)

    mine_subs = _subs(store, mine)
    theirs_subs = _subs(store, theirs)
    assert [e["owner"] for e in mine_subs.values()] == [first.login]
    assert [e["owner"] for e in theirs_subs.values()] == [second.login]
    assert not any(e["owner"] == first.login for e in theirs_subs.values())


@pytest.mark.db
def test_minted_card_carries_the_walk_source_and_date(store: Any) -> None:
    """The forge stamps ``meta.source='walk'`` + ``walk_date`` on a card
    minted for a walk-queued concept (contract me743214); a plain
    cardless concept minted beside it carries neither."""
    user = _user(store)
    walked = _concept(store)
    plain = _concept(store)
    _walk(store, user, f"cn{walked}")
    run_walk_cards_pass(store)

    client = _FakeClient({"cards": [{"text": "X is {{c1::Y}}."}]})
    report = mint_daily_cards(store, client=client, per_day=5)

    [(_token, entry)] = _subs(store, walked).items()
    minted = {concept: cards for concept, cards in report.minted}
    assert walked in minted and plain in minted
    walked_ref = store.get_ref(kind="anki", id=minted[walked][0])
    assert walked_ref.meta["source"] == "walk"
    assert walked_ref.meta["walk_date"] == entry["at"][:10]
    plain_ref = store.get_ref(kind="anki", id=minted[plain][0])
    assert "source" not in plain_ref.meta
    assert "walk_date" not in plain_ref.meta


@pytest.mark.db
def test_the_forge_daily_cap_still_bounds_walk_cards(store: Any) -> None:
    user = _user(store)
    first, second = _concept(store), _concept(store)
    _walk(store, user, f"cn{first}")
    _walk(store, user, f"cn{second}")
    run_walk_cards_pass(store)

    client = _FakeClient({"cards": [{"text": "X is {{c1::Y}}."}]})
    report = mint_daily_cards(store, client=client, per_day=1)

    assert len(report.minted) == 1
    ref = store.get_ref(kind="anki", id=report.minted[0][1][0])
    assert ref.meta["source"] == "walk" and ref.meta["walk_date"]
