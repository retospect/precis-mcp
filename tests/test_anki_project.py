"""Tests for the read-only foreign-card PG projection (slice 3).

`project_cards` takes plain `ForeignCard`s + a real store — no `anki` wheel, no
network — so these run in the gate. Uses unique guids per test so the shared
`precis_test` DB doesn't cross-contaminate.
"""

from __future__ import annotations

import uuid

import pytest

from precis.anki.project import (
    FOREIGN_SOURCE,
    content_sha,
    project_cards,
    searchable_text,
    title_for,
)
from precis.anki.sync import ForeignCard
from precis.users import hash_password

OWNER = "reto"


@pytest.fixture(autouse=True)
def _owner(store) -> None:
    """Every projected ref carries `owner_login` (FK to `web_users.login`) —
    seed the one login these tests project against."""
    store.create_web_user(login=OWNER, abbrev="rs", password=hash_password("pw"))


def _card(
    guid, fields, *, notetype="Cloze", deck="geo", note_id=1, ref_id=None, stats=None
):
    return ForeignCard(
        note_id=note_id,
        guid=guid,
        notetype=notetype,
        deck=deck,
        tags=[],
        fields=fields,
        ref_id=ref_id,
        stats=stats,
    )


def _uid() -> str:
    return f"g-{uuid.uuid4().hex[:12]}"


class TestPure:
    def test_plain_strips_html_and_cloze(self) -> None:
        assert (
            searchable_text({"Text": "The <b>{{c1::heart}}</b> pumps&nbsp;blood."})
            == "The heart pumps blood."
        )

    def test_title_first_nonempty(self) -> None:
        assert title_for({"Front": "", "Back": "answer"}) == "answer"
        assert title_for({"a": "<div></div>"}) == "(empty card)"

    def test_content_sha_stable_and_sensitive(self) -> None:
        a = content_sha({"Text": "x"}, "Cloze")
        assert a == content_sha({"Text": "x"}, "Cloze")
        assert a != content_sha({"Text": "y"}, "Cloze")
        assert a != content_sha({"Text": "x"}, "Basic")


class TestProjection:
    def test_insert_creates_readonly_ref_with_card(self, store) -> None:
        guid = _uid()
        res = project_cards(
            store,
            [_card(guid, {"Front": "Q?", "Back": "A!"}, notetype="Basic")],
            owner_login=OWNER,
        )
        assert res.inserted == 1
        idx = _lookup(store, guid)
        assert idx is not None
        ref = store.get_ref(kind="anki", id=idx)
        assert ref.meta["source"] == FOREIGN_SOURCE
        assert ref.meta["readonly"] is True
        assert ref.meta["notetype"] == "Basic"
        assert ref.meta["anki"]["guid"] == guid
        assert ref.owner_login == OWNER
        # searchable card_combined chunk emitted
        with store.pool.connection() as conn:
            card = conn.execute(
                "select text from chunks where ref_id=%s and ord=-1", (idx,)
            ).fetchone()
        assert card is not None and "A!" in card[0]

    def test_reproject_unchanged_is_noop(self, store) -> None:
        guid = _uid()
        card = _card(guid, {"Text": "The {{c1::x}} y."})
        project_cards(store, [card], owner_login=OWNER)
        res = project_cards(store, [card], owner_login=OWNER)  # identical content
        assert res.unchanged == 1 and res.updated == 0 and res.inserted == 0

    def test_reproject_changed_updates(self, store) -> None:
        guid = _uid()
        project_cards(
            store, [_card(guid, {"Text": "old {{c1::a}}"})], owner_login=OWNER
        )
        res = project_cards(
            store, [_card(guid, {"Text": "new {{c1::b}}"})], owner_login=OWNER
        )
        assert res.updated == 1
        idx = _lookup(store, guid)
        assert (
            store.get_ref(kind="anki", id=idx).meta["fields"]["Text"] == "new {{c1::b}}"
        )

    def test_vanished_card_soft_deleted(self, store) -> None:
        keep, drop = _uid(), _uid()
        project_cards(
            store,
            [_card(keep, {"Text": "{{c1::k}}"}), _card(drop, {"Text": "{{c1::d}}"})],
            owner_login=OWNER,
        )
        drop_id = _lookup(store, drop)
        # next sync: only `keep` present → `drop` disappears from the mirror
        project_cards(store, [_card(keep, {"Text": "{{c1::k}}"})], owner_login=OWNER)
        assert _lookup(store, drop) is None  # soft-deleted (not in live index)
        # confirm the row is actually soft-deleted, not hard-deleted
        with store.pool.connection() as conn:
            row = conn.execute(
                "select retired_at from refs where ref_id=%s", (drop_id,)
            ).fetchone()
        assert row[0] is not None

    def test_precis_owned_card_skipped(self, store) -> None:
        # a precis-authored note carries guid `precis:<id>` → never re-projected
        res = project_cards(
            store, [_card("precis:123", {"Text": "{{c1::mine}}"})], owner_login=OWNER
        )
        assert res.skipped_own == 1 and res.inserted == 0

    def test_projection_stores_stats(self, store) -> None:
        guid = _uid()
        project_cards(
            store,
            [
                _card(
                    guid,
                    {"Text": "{{c1::x}}"},
                    stats={"lapses_total": 3, "ease_min": 2.1},
                )
            ],
            owner_login=OWNER,
        )
        idx = _lookup(store, guid)
        assert (
            store.get_ref(kind="anki", id=idx).meta["anki_stats"]["lapses_total"] == 3
        )

    def test_stats_writeback_does_not_clobber_guid_or_dedup(self, store) -> None:
        """Regression for the 2026-07 incident: the sync's stats write-back must
        patch FLAT keys (`anki_synced_at`), never nested `{"anki": {...}}` — a
        shallow jsonb `||` merge would replace meta.anki, wiping the guid the
        projection dedups on, so re-projection would create a duplicate ref."""
        guid = _uid()
        project_cards(store, [_card(guid, {"Text": "{{c1::x}}"})], owner_login=OWNER)
        idx = _lookup(store, guid)
        # simulate the CLI stats write-back (the FIXED flat shape)
        store.update_ref(
            idx,
            meta_patch={
                "anki_stats": {"lapses_total": 2},
                "anki_synced_at": "2026-07-14",
            },
        )
        meta = store.get_ref(kind="anki", id=idx).meta
        assert meta["anki"]["guid"] == guid  # guid survived the write-back
        assert meta["anki_synced_at"] == "2026-07-14"
        # re-projection still dedups — no duplicate ref
        res = project_cards(
            store, [_card(guid, {"Text": "{{c1::x}}"})], owner_login=OWNER
        )
        assert res.inserted == 0 and res.unchanged == 1
        assert _lookup(store, guid) == idx  # same ref, no dupe

    def test_stats_refresh_without_reembed(self, store) -> None:
        # same content, new stats → 'unchanged' (no card re-emit) but stats updated
        guid = _uid()
        project_cards(
            store,
            [_card(guid, {"Text": "{{c1::x}}"}, stats={"lapses_total": 1})],
            owner_login=OWNER,
        )
        res = project_cards(
            store,
            [_card(guid, {"Text": "{{c1::x}}"}, stats={"lapses_total": 5})],
            owner_login=OWNER,
        )
        assert res.unchanged == 1 and res.updated == 0
        idx = _lookup(store, guid)
        assert (
            store.get_ref(kind="anki", id=idx).meta["anki_stats"]["lapses_total"] == 5
        )


class TestOwnership:
    def test_insert_sets_owner_login(self, store) -> None:
        guid = _uid()
        project_cards(store, [_card(guid, {"Text": "{{c1::x}}"})], owner_login=OWNER)
        idx = _lookup(store, guid)
        assert store.get_ref(kind="anki", id=idx).owner_login == OWNER

    def test_legacy_unowned_row_is_claimed_on_content_change(self, store) -> None:
        """A row projected before per-user ownership existed (`owner_login`
        NULL) is picked up by `_foreign_index` and claimed the next time its
        content changes — belt-and-braces alongside `claim_unowned_refs`."""
        guid = _uid()
        sha = content_sha({"Text": "old"}, "Cloze")
        legacy = store.insert_ref(
            kind="anki",
            slug=None,
            title="old",
            meta={
                "source": FOREIGN_SOURCE,
                "readonly": True,
                "notetype": "Cloze",
                "fields": {"Text": "old"},
                "anki": {"guid": guid, "note_id": 1, "content_sha": sha},
            },
        )
        assert legacy.owner_login is None

        res = project_cards(
            store, [_card(guid, {"Text": "new {{c1::x}}"})], owner_login=OWNER
        )
        assert res.updated == 1
        assert store.get_ref(kind="anki", id=legacy.id).owner_login == OWNER

    def test_legacy_unowned_row_is_claimed_on_unchanged_content(self, store) -> None:
        """Same claim, taken on the cheap unchanged-content path (stats-only
        refresh) — not just the full-update path."""
        guid = _uid()
        card = _card(guid, {"Text": "{{c1::x}}"})
        sha = content_sha(card.fields, card.notetype)
        legacy = store.insert_ref(
            kind="anki",
            slug=None,
            title="x",
            meta={
                "source": FOREIGN_SOURCE,
                "readonly": True,
                "notetype": card.notetype,
                "fields": card.fields,
                "anki": {"guid": guid, "note_id": 1, "content_sha": sha},
            },
        )
        assert legacy.owner_login is None

        res = project_cards(store, [card], owner_login=OWNER)
        assert res.unchanged == 1
        assert store.get_ref(kind="anki", id=legacy.id).owner_login == OWNER


def _lookup(store, guid) -> int | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "select ref_id from refs where kind='anki' and retired_at is null "
            "and meta->'anki'->>'guid' = %s",
            (guid,),
        ).fetchone()
    return row[0] if row else None
