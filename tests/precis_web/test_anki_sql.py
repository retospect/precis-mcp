"""Real-PG regression tests for the ``/anki`` route's raw SQL
(``precis_web.routes.anki``) — the route-level ``test_anki.py`` runs against
a fake store that never parses SQL, so ``_counts`` / ``_card_groups`` /
the known/undo transaction are exercised here against the live ``store`` fixture.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from typing import Any

import pytest

from precis.errors import BadInput
from precis.reading.flashcards import author_card
from precis.reading.known import known_undo_entries, mark_known, undo_known
from precis.reading.promote import create_concept
from precis.users import hash_password
from precis_web.routes.anki import _card_groups, _counts


def _user(store: Any, login: str = "reto") -> str:
    try:
        store.create_web_user(
            login=login, abbrev=login[:2], password=hash_password("pw")
        )
    except Exception:
        pass  # fixture user may already exist from another test
    return login


def _forge_card(store: Any, concept_id: int, owner: str) -> int:
    card_id = author_card(
        store,
        text=f"The answer is {{{{c1::{uuid.uuid4().hex[:6]}}}}}.",
        concept_id=concept_id,
        deck="Precis::reading",
    )
    store.set_ref_owner(card_id, owner)
    return card_id


def _agent_card(
    store: Any,
    concept_id: int,
    owner: str,
    *,
    source: str | None = None,
    readonly: bool = False,
) -> int:
    """A card the way ``AnkiHandler._create`` writes it (meta shape without
    ``authored_by``) — what an agent's MCP put leaves behind."""
    text = f"Agent says {{{{c1::{uuid.uuid4().hex[:6]}}}}}."
    meta: dict[str, Any] = {
        "notetype": "Cloze",
        "deck": "Precis::reading",
        "fields": {"Text": text},
    }
    if source is not None:
        meta["source"] = source
    if readonly:
        meta["readonly"] = True
    ref = store.insert_ref(
        kind="anki", slug=None, title=text, meta=meta, owner_login=owner
    )
    store.add_link(src_ref_id=concept_id, dst_ref_id=ref.id, relation="represents")
    return ref.id


def _unlinked_card(
    store: Any, owner: str, *, source: str | None = None, readonly: bool = False
) -> int:
    """A precis-authored card with no ``represents`` link at all — the
    orphan cohort of gr742735's second half."""
    text = f"No concept {{{{c1::{uuid.uuid4().hex[:6]}}}}}."
    meta: dict[str, Any] = {
        "notetype": "Cloze",
        "deck": "Precis::reading",
        "fields": {"Text": text},
    }
    if source is not None:
        meta["source"] = source
    if readonly:
        meta["readonly"] = True
    ref = store.insert_ref(
        kind="anki", slug=None, title=text, meta=meta, owner_login=owner
    )
    return int(ref.id)


def test_concept_retire_cascades_to_its_authored_cards(store: Any) -> None:
    """gr742735: retiring a concept retires the precis-authored cards that
    represent it (both link directions) — an orphaned card would keep
    syncing to Anki while invisible on /learning and /anki. Read-only
    foreign projections and other concepts' cards survive."""
    owner = _user(store)
    cid = create_concept(store, name=f"casc-{uuid.uuid4().hex[:8]}")
    forge = _forge_card(store, cid, owner)
    agent = _agent_card(store, cid, owner)
    foreign = _agent_card(store, cid, owner, source="anki-foreign")
    readonly = _agent_card(store, cid, owner, readonly=True)
    reverse = _unlinked_card(store, owner)
    # add_link canonicalises represented-by -> represents, so seed the
    # stored reverse edge directly.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by) "
            "VALUES (%s, %s, 'represented-by', 'system')",
            (reverse, cid),
        )
    other_cid = create_concept(store, name=f"other-{uuid.uuid4().hex[:8]}")
    shared = _forge_card(store, cid, owner)
    store.add_link(src_ref_id=other_cid, dst_ref_id=shared, relation="represents")
    keep_cid = create_concept(store, name=f"keep-{uuid.uuid4().hex[:8]}")
    keep = _forge_card(store, keep_cid, owner)

    store.retire_ref(cid)

    assert store.get_ref(kind="concept", id=cid) is None
    assert store.get_ref(kind="anki", id=forge) is None
    assert store.get_ref(kind="anki", id=agent) is None
    assert store.get_ref(kind="anki", id=reverse) is None
    assert store.get_ref(kind="anki", id=foreign) is not None
    assert store.get_ref(kind="anki", id=readonly) is not None
    assert store.get_ref(kind="anki", id=keep) is not None
    assert store.get_ref(kind="anki", id=shared) is not None


def test_author_card_refuses_a_retired_concept(store: Any) -> None:
    cid = create_concept(store, name=f"gone-{uuid.uuid4().hex[:8]}")
    store.retire_ref(cid)
    with pytest.raises(ValueError, match="retired or missing"):
        author_card(store, text="A {{c1::b}} c", concept_id=cid, deck="Precis::t")


def test_represents_link_refuses_a_retired_concept(store: Any) -> None:
    """gr742735: an agent ``link``/``put`` may not home a card on a
    retired concept — that is exactly how the orphaned prod cards were
    made. Both spellings of the edge refuse; the live concept still
    links."""
    owner = _user(store)
    cid = create_concept(store, name=f"dead-{uuid.uuid4().hex[:8]}")
    live = create_concept(store, name=f"ok-{uuid.uuid4().hex[:8]}")
    card = _unlinked_card(store, owner)
    store.retire_ref(cid)

    with pytest.raises(BadInput, match="retired"):
        store.add_link(src_ref_id=cid, dst_ref_id=card, relation="represents")
    with pytest.raises(BadInput, match="retired"):
        store.add_link(src_ref_id=card, dst_ref_id=cid, relation="represented-by")
    store.add_link(src_ref_id=live, dst_ref_id=card, relation="represents")


def test_second_concept_retire_waits_then_takes_the_shared_card(
    store: Any,
) -> None:
    """gr742735 race: two concepts sharing one card retire in overlapping
    transactions. The retire lock pass serialises them — the second
    retire waits out the first's commit, sees its concept gone, and
    retires the shared card rather than sparing it on a stale "the other
    concept is still live" read."""
    owner = _user(store)
    cid_a = create_concept(store, name=f"race-a-{uuid.uuid4().hex[:8]}")
    cid_b = create_concept(store, name=f"race-b-{uuid.uuid4().hex[:8]}")
    card = _forge_card(store, cid_a, owner)
    store.add_link(src_ref_id=cid_b, dst_ref_id=card, relation="represents")

    errors: list[BaseException] = []

    def retire_b() -> None:
        try:
            store.retire_ref(cid_b)
        except BaseException as exc:
            errors.append(exc)

    # The first cascade holds the card lock while its retire is uncommitted.
    with store.pool.connection() as c1:
        store.retire_ref(cid_a, conn=c1)
        t = threading.Thread(target=retire_b)
        t.start()
        t.join(timeout=15)
        assert t.is_alive(), "retire_ref did not wait on the shared concept"
    t.join(timeout=30)
    assert not errors
    assert store.get_ref(kind="concept", id=cid_a) is None
    assert store.get_ref(kind="concept", id=cid_b) is None
    assert store.get_ref(kind="anki", id=card) is None


def test_concurrent_concept_retires_wait_for_uncommitted_second_home(
    store: Any,
) -> None:
    owner = _user(store)
    cid_a = create_concept(store, name=f"pending-a-{uuid.uuid4().hex[:8]}")
    cid_b = create_concept(store, name=f"pending-b-{uuid.uuid4().hex[:8]}")
    card = _forge_card(store, cid_a, owner)
    pids: Queue[int] = Queue()

    def retire(cid: int) -> None:
        with store.pool.connection() as conn:
            conn.execute("SET LOCAL lock_timeout = '20s'")
            pids.put(conn.info.backend_pid)
            store.retire_ref(cid, conn=conn)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with store.pool.connection() as writer:
            store.add_link(
                src_ref_id=cid_b, dst_ref_id=card, relation="represents", conn=writer
            )
            retires = [executor.submit(retire, cid) for cid in (cid_a, cid_b)]
            pending = {pids.get(timeout=5), pids.get(timeout=5)}
            # A waits on the card, B on its concept. Release the link only
            # after both retire transactions have reached their lock waits.
            deadline = time.monotonic() + 5
            while pending and time.monotonic() < deadline:
                pending = {
                    pid
                    for pid in pending
                    if writer.execute(
                        "SELECT %s = ANY(pg_blocking_pids(%s))",
                        (writer.info.backend_pid, pid),
                    ).fetchone()
                    != (True,)
                }
                if pending:
                    time.sleep(0.01)
            assert not pending, "both retires must wait for the uncommitted link"
        for future in retires:
            future.result(timeout=25)

    assert store.get_ref(kind="concept", id=cid_a) is None
    assert store.get_ref(kind="concept", id=cid_b) is None
    assert store.get_ref(kind="anki", id=card) is None


def test_represents_link_waits_out_an_in_flight_retire_then_refuses(
    store: Any,
) -> None:
    """gr742735 race: a ``represents`` link written while the concept's
    retire is in flight must not land after the cascade — the FOR SHARE
    check waits on the concept row, then refuses the dead endpoint."""
    owner = _user(store)
    cid = create_concept(store, name=f"inflight-{uuid.uuid4().hex[:8]}")
    card = _unlinked_card(store, owner)

    errors: list[BaseException] = []

    def link() -> None:
        try:
            store.add_link(src_ref_id=cid, dst_ref_id=card, relation="represents")
        except BaseException as exc:
            errors.append(exc)

    with store.pool.connection() as c1:
        c1.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (cid,))
        t = threading.Thread(target=link)
        t.start()
        t.join(timeout=15)
        assert t.is_alive(), "add_link did not wait on the retiring concept"
    t.join(timeout=30)
    assert len(errors) == 1 and isinstance(errors[0], BadInput)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (cid, card),
        ).fetchone()
    assert row is None


def test_represents_link_refuses_a_retired_card(store: Any) -> None:
    """gr742735 review: re-homing onto a *live* concept must still refuse
    a card that is itself retired — the link would report success on a
    note the cascade already slated for removal. Both spellings refuse;
    the live card still links."""
    owner = _user(store)
    live = create_concept(store, name=f"ok-{uuid.uuid4().hex[:8]}")
    dead_card = _unlinked_card(store, owner)
    live_card = _unlinked_card(store, owner)
    store.retire_ref(dead_card)

    with pytest.raises(BadInput, match="retired"):
        store.add_link(src_ref_id=live, dst_ref_id=dead_card, relation="represents")
    with pytest.raises(BadInput, match="retired"):
        store.add_link(src_ref_id=dead_card, dst_ref_id=live, relation="represented-by")
    store.add_link(src_ref_id=live, dst_ref_id=live_card, relation="represents")


def test_represents_link_waits_out_a_card_retire_then_refuses(
    store: Any,
) -> None:
    """gr742735 review race: a re-home to a live concept waits behind an
    in-flight retire of the *card* (a concurrent cascade from the card's
    other concept); when the wait releases the liveness re-check refuses
    the edge rather than reporting success on a dead card."""
    owner = _user(store)
    live = create_concept(store, name=f"ok-{uuid.uuid4().hex[:8]}")
    card = _unlinked_card(store, owner)

    errors: list[BaseException] = []

    def link() -> None:
        try:
            store.add_link(src_ref_id=live, dst_ref_id=card, relation="represents")
        except BaseException as exc:
            errors.append(exc)

    with store.pool.connection() as c1:
        c1.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (card,))
        t = threading.Thread(target=link)
        t.start()
        t.join(timeout=15)
        assert t.is_alive(), "add_link did not wait on the retiring card"
    t.join(timeout=30)
    assert len(errors) == 1 and isinstance(errors[0], BadInput)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s",
            (live, card),
        ).fetchone()
    assert row is None


def test_learning_groups_list_concept_less_cards_as_their_own_group(
    store: Any,
) -> None:
    """gr742735: a live precis-authored card whose ``represents`` concept is
    retired — or that was never linked — lands in /learning's ungrouped
    group instead of vanishing. Foreign projections stay excluded; the
    forge-only /anki grouping does not grow the shelf."""
    owner = _user(store)
    dead = create_concept(store, name=f"dead-{uuid.uuid4().hex[:8]}")
    orphan = _forge_card(store, dead, owner)
    # Pre-cascade prod state: the concept is gone but its card stayed live.
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (dead,))
    unlinked = _unlinked_card(store, owner)
    foreign = _unlinked_card(store, owner, source="anki-foreign")
    # `readonly` alone (no anki-foreign source) is just as unsyncable —
    # spec_from_ref drops it, so the shelf must too.
    readonly = _unlinked_card(store, owner, readonly=True)
    live = create_concept(store, name=f"live-{uuid.uuid4().hex[:8]}")
    grouped = _forge_card(store, live, owner)
    ro_grouped = _agent_card(store, live, owner, readonly=True)

    groups = _card_groups(store, owner, forge_only=False, ungrouped=True)
    shelf = next(g for g in groups if g["ungrouped"])
    assert shelf["concept_id"] is None
    assert {c["id"] for c in shelf["cards"]} == {orphan, unlinked}
    assert foreign not in {c["id"] for c in shelf["cards"]}
    assert readonly not in {c["id"] for c in shelf["cards"]}
    live_group = next(g for g in groups if g["concept_id"] == live)
    assert {c["id"] for c in live_group["cards"]} == {grouped}
    assert ro_grouped not in {c["id"] for c in live_group["cards"]}

    forge_groups = _card_groups(store, owner, forge_only=True)
    assert not any(g["ungrouped"] for g in forge_groups)
    no_shelf = _card_groups(store, owner, forge_only=False)
    assert not any(g["ungrouped"] for g in no_shelf)


def test_counts_split_cards_and_concepts(store: Any) -> None:
    owner = _user(store)
    before = _counts(store, owner)
    cid = create_concept(store, name=f"cnt-{uuid.uuid4().hex[:8]}")
    card_id = _forge_card(store, cid, owner)
    store.update_ref(
        card_id,
        meta_patch={"anki_stats": {"lapses_total": 9, "ease_min": 1.5}},
    )
    after = _counts(store, owner)
    assert after["cards"] == before["cards"] + 1
    assert after["forge"] == before["forge"] + 1
    assert after["leeches"] == before["leeches"] + 1
    assert after["active"] >= before["active"] + 1


def test_forge_groups_group_cards_by_concept(store: Any) -> None:
    owner = _user(store)
    cid = create_concept(store, name=f"grp-{uuid.uuid4().hex[:8]}")
    c1 = _forge_card(store, cid, owner)
    c2 = _forge_card(store, cid, owner)
    groups = _card_groups(store, owner, forge_only=True)
    g = next(g for g in groups if g["concept_id"] == cid)
    assert {c["id"] for c in g["cards"]} == {c1, c2}
    assert g["name"].startswith("grp-")

    # Another owner's card on the same concept must not leak in.
    other = _user(store, login="alice")
    _forge_card(store, cid, other)
    g2 = next(
        g for g in _card_groups(store, owner, forge_only=True) if g["concept_id"] == cid
    )
    assert {c["id"] for c in g2["cards"]} == {c1, c2}


def test_learning_groups_include_agent_cards_but_exclude_foreign(store: Any) -> None:
    """gr742681: /learning's Current context lists every live precis-authored
    concept card — an agent-put card (no ``authored_by``) shows there but not
    on forge-only /anki; a read-only ``anki-foreign`` projection shows on
    neither."""
    owner = _user(store)
    cid = create_concept(store, name=f"agent-{uuid.uuid4().hex[:8]}")
    forge = _forge_card(store, cid, owner)
    agent = _agent_card(store, cid, owner)
    foreign = _agent_card(store, cid, owner, source="anki-foreign")

    learning = next(
        g
        for g in _card_groups(store, owner, forge_only=False)
        if g["concept_id"] == cid
    )
    assert {c["id"] for c in learning["cards"]} == {forge, agent}
    assert foreign not in {c["id"] for c in learning["cards"]}

    anki = next(
        g for g in _card_groups(store, owner, forge_only=True) if g["concept_id"] == cid
    )
    assert {c["id"] for c in anki["cards"]} == {forge}


def test_known_undo_restores_only_its_cards_and_prior_mastery(store: Any) -> None:
    owner = store.get_web_user(_user(store))
    other = store.get_web_user(_user(store, login="alice"))
    cid = create_concept(store, name=f"own-{uuid.uuid4().hex[:8]}")
    prior = {
        "state": "active",
        "mastery": 0.35,
        "mastery_updated_at": "2026-11-20T00:00:00Z",
    }
    store.update_ref(cid, meta_patch=prior)
    mine = _forge_card(store, cid, owner.login)
    dropped = _forge_card(store, cid, owner.login)
    theirs = _forge_card(store, cid, other.login)
    redropped = _forge_card(store, cid, owner.login)
    store.retire_ref(dropped)

    assert mark_known(store, owner, [cid, cid], require_cards=True) == 2
    assert mark_known(store, owner, [cid], require_cards=True) == 0
    assert store.get_ref(kind="concept", id=cid).meta["state"] == "mastered"
    assert store.get_ref(kind="anki", id=mine) is None
    assert store.get_ref(kind="anki", id=theirs) is not None
    [(entry_id, _, token)] = known_undo_entries(store, owner)
    assert entry_id == cid
    assert known_undo_entries(store, other) == []
    assert not undo_known(store, other, cid, token)

    assert store.restore_ref(redropped)
    store.retire_ref(redropped)
    store.update_ref(cid, meta_patch={"definition": "Updated after dismissal."})
    assert undo_known(store, owner, cid, token)
    assert not undo_known(store, owner, cid, token)
    assert store.get_ref(kind="anki", id=mine).meta["anki_restore_pending"] is True
    assert store.get_ref(kind="anki", id=dropped) is None
    assert store.get_ref(kind="anki", id=redropped) is None
    meta = store.get_ref(kind="concept", id=cid).meta
    assert {key: meta[key] for key in prior} == prior
    assert meta["definition"] == "Updated after dismissal."


def test_overlapping_owners_share_the_original_mastery_snapshot(store: Any) -> None:
    first = store.get_web_user(_user(store))
    second = store.get_web_user(_user(store, login="alice"))
    cid = create_concept(store, name=f"overlap-{uuid.uuid4().hex[:8]}")
    mine = _forge_card(store, cid, first.login)
    theirs = _forge_card(store, cid, second.login)
    before = store.get_ref(kind="concept", id=cid).meta

    assert mark_known(store, first, [cid], require_cards=True) == 1
    [(_, _, first_token)] = known_undo_entries(store, first)
    assert mark_known(store, second, [cid], require_cards=True) == 1
    [(_, _, second_token)] = known_undo_entries(store, second)
    assert undo_known(store, first, cid, first_token)
    assert store.get_ref(kind="concept", id=cid).meta["state"] == "mastered"
    assert store.get_ref(kind="anki", id=mine) is not None
    assert store.get_ref(kind="anki", id=theirs) is None

    assert undo_known(store, second, cid, second_token)
    assert store.get_ref(kind="anki", id=theirs) is not None
    assert store.get_ref(kind="concept", id=cid).meta == before


def test_forge_groups_carry_memory_link_from_meta(store: Any) -> None:
    owner = _user(store)
    cid = create_concept(store, name=f"mem-{uuid.uuid4().hex[:8]}")
    _forge_card(store, cid, owner)
    store.update_ref(
        cid,
        meta_patch={"memory_match": {"ref_id": 7, "kind": "memory", "title": "Why X"}},
    )
    group = next(
        g for g in _card_groups(store, owner, forge_only=True) if g["concept_id"] == cid
    )
    assert group["memory"] == {"href": "/eye/me7", "label": "me7 Why X"}


def test_memory_link_misses_render_nothing() -> None:
    from precis_web.routes.anki import _memory_link

    assert _memory_link(None) is None
    assert _memory_link({"ref_id": None}) is None
    assert _memory_link({"ref_id": 5, "kind": "finding", "title": "T"}) == {
        "href": "/eye/fi5",
        "label": "fi5 T",
    }
