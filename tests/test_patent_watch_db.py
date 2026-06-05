"""Tests for ``_patent_watch_db`` — the DAO over ``patent_watches``."""

from __future__ import annotations

import time

import pytest

from precis.errors import BadInput, NotFound
from precis.handlers import _patent_watch_db as db
from precis.store import Store

# ---------------------------------------------------------------------------
# create + get_by_name
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_round_trip(self, store: Store) -> None:
        w = db.create(
            store,
            name="my-watch",
            cql="cpc=B01J27/24",
        )
        assert w.id > 0
        assert w.name == "my-watch"
        assert w.cql == "cpc=B01J27/24"
        assert w.interval_s == 604_800
        assert w.last_run_at is None
        assert w.last_seen_pn == []
        assert w.max_per_pass is None

        again = db.get_by_name(store, "my-watch")
        assert again is not None
        assert again.id == w.id

    def test_name_lowercased_and_trimmed(self, store: Store) -> None:
        w = db.create(store, name="  MyWatch  ", cql="cpc=B01J27/24")
        assert w.name == "mywatch"
        # Lookup matches via the lowercased form.
        assert db.get_by_name(store, "MYWATCH") is not None

    def test_explicit_options_persisted(self, store: Store) -> None:
        w = db.create(
            store,
            name="auto",
            cql="cpc=B01J27/24",
            interval_s=86_400,
            max_per_pass=5,
            created_by="system",
        )
        assert w.interval_s == 86_400
        assert w.max_per_pass == 5
        assert w.created_by == "system"

    def test_bare_keyword_cql_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="bare keyword"):
            db.create(store, name="bad", cql="photocatalysis")

    def test_empty_name_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="name is empty"):
            db.create(store, name="   ", cql="cpc=B01J27/24")

    def test_negative_interval_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="interval_s"):
            db.create(
                store,
                name="bad",
                cql="cpc=B01J27/24",
                interval_s=-1,
            )

    def test_zero_max_per_pass_rejected(self, store: Store) -> None:
        with pytest.raises(BadInput, match="max_per_pass"):
            db.create(
                store,
                name="bad",
                cql="cpc=B01J27/24",
                max_per_pass=0,
            )

    def test_duplicate_name_rejected(self, store: Store) -> None:
        db.create(store, name="dupe", cql="cpc=B01J27/24")
        with pytest.raises(BadInput, match="already exists"):
            db.create(store, name="dupe", cql="cpc=Y02E60/13")


# ---------------------------------------------------------------------------
# list_all + list_due ordering
# ---------------------------------------------------------------------------


class TestListing:
    def test_list_all_alphabetical(self, store: Store) -> None:
        db.create(store, name="zeta", cql="cpc=B01J27/24")
        db.create(store, name="alpha", cql="cpc=Y02E60/13")
        db.create(store, name="middle", cql="cpc=H01M")
        names = [w.name for w in db.list_all(store)]
        assert names == ["alpha", "middle", "zeta"]

    def test_list_due_picks_fresh_first(self, store: Store) -> None:
        # Two never-run watches → both due, alphabetical order on the
        # NULL last_run_at tier.
        db.create(store, name="b", cql="cpc=B01J27/24")
        db.create(store, name="a", cql="cpc=Y02E60/13")
        due = db.list_due(store)
        assert {w.name for w in due} == {"a", "b"}

    def test_list_due_skips_cooling(self, store: Store) -> None:
        # Mark a watch as having just run with a long interval; it
        # should not appear in due.
        w = db.create(store, name="cool", cql="cpc=B01J27/24", interval_s=3600)
        db.record_pass(store, watch_id=w.id, new_pn=[])
        due = db.list_due(store)
        assert "cool" not in {x.name for x in due}

    def test_list_due_picks_short_interval_after_sleep(self, store: Store) -> None:
        # interval_s=1 means "ready again in a second". Sleep, then
        # re-list; the watch should now be due.
        w = db.create(store, name="quick", cql="cpc=B01J27/24", interval_s=1)
        db.record_pass(store, watch_id=w.id, new_pn=[])
        time.sleep(1.1)
        due = db.list_due(store)
        assert "quick" in {x.name for x in due}


# ---------------------------------------------------------------------------
# record_pass
# ---------------------------------------------------------------------------


class TestRecordPass:
    def test_records_new_pns(self, store: Store) -> None:
        w = db.create(store, name="acc", cql="cpc=B01J27/24")
        db.record_pass(
            store,
            watch_id=w.id,
            new_pn=["ep1234567b1", "wo2023123456a1"],
        )
        again = db.get_by_name(store, "acc")
        assert again is not None
        assert set(again.last_seen_pn) == {"ep1234567b1", "wo2023123456a1"}
        assert again.last_run_at is not None

    def test_unions_with_existing(self, store: Store) -> None:
        w = db.create(store, name="union", cql="cpc=B01J27/24")
        db.record_pass(store, watch_id=w.id, new_pn=["ep1111111a1"])
        db.record_pass(
            store,
            watch_id=w.id,
            new_pn=["ep2222222a1", "ep1111111a1"],  # duplicate intentional
        )
        again = db.get_by_name(store, "union")
        assert again is not None
        # array_agg(DISTINCT) keeps the union de-duplicated.
        assert set(again.last_seen_pn) == {"ep1111111a1", "ep2222222a1"}

    def test_empty_pass_still_bumps_last_run(self, store: Store) -> None:
        # An all-empty pass (no new hits) must still bump last_run_at
        # so the watch cools off — otherwise it'd re-fetch every tick.
        w = db.create(store, name="empty", cql="cpc=B01J27/24")
        db.record_pass(store, watch_id=w.id, new_pn=[])
        again = db.get_by_name(store, "empty")
        assert again is not None
        assert again.last_run_at is not None
        assert again.last_seen_pn == []

    def test_unknown_watch_id_raises(self, store: Store) -> None:
        with pytest.raises(NotFound, match="no longer exists"):
            db.record_pass(store, watch_id=99_999, new_pn=["ep1111111a1"])


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_round_trip(self, store: Store) -> None:
        db.create(store, name="goner", cql="cpc=B01J27/24")
        assert db.get_by_name(store, "goner") is not None
        db.delete(store, "goner")
        assert db.get_by_name(store, "goner") is None

    def test_delete_unknown_raises(self, store: Store) -> None:
        with pytest.raises(NotFound, match="no patent watch named"):
            db.delete(store, "never-existed")

    def test_delete_normalises_name(self, store: Store) -> None:
        db.create(store, name="case", cql="cpc=B01J27/24")
        db.delete(store, "  CASE  ")
        assert db.get_by_name(store, "case") is None
