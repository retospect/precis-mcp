"""Tests for the Anki sync engine (slice 2).

Pure helpers (`precis.anki.notes`) run everywhere. The collection ops
(`precis.anki.sync`) need the optional `anki` pylib and run against a throwaway
*local* `.anki2` — NO network, so they're safe in CI when `anki` is present and
skip cleanly when it isn't (the gate container doesn't bake the wheel; ansible
installs it only on the sync runner).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from precis.anki.notes import (
    MANAGED_TAG,
    AnkiCardSpec,
    aggregate_stats,
    guid_for,
    precis_tag,
    ref_id_from_guid,
    spec_from_ref,
)
from precis.store import Store


@pytest.mark.parametrize("media_times_out", [False, True])
def test_attached_note_uses_media_filename_and_preserves_collection_stats(
    monkeypatch: pytest.MonkeyPatch,
    media_times_out: bool,
) -> None:
    from precis.anki import sync

    col = MagicMock()
    col.db.all.return_value = []
    col.db.list.return_value = []
    col.media.write_data.return_value = "renamed&photo.png"
    col.media_sync_status.side_effect = [
        SimpleNamespace(active=True),
        SimpleNamespace(active=False),
    ]
    col.sync_collection.side_effect = [
        SimpleNamespace(required=0, new_endpoint=None),
        SimpleNamespace(required=0, new_endpoint="https://example.org/sync"),
    ]

    class Note(dict[str, str]):
        guid: str
        tags: list[str]

    note = Note(Text="", **{"Back Extra": ""})
    col.new_note.return_value = note
    changes = SimpleNamespace(FULL_UPLOAD=1, FULL_SYNC=2, FULL_DOWNLOAD=3)
    sp = SimpleNamespace(
        SyncCollectionResponse=SimpleNamespace(ChangesRequired=changes),
        SyncAuth=lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(sync, "_import_anki", lambda: (lambda path: col, sp))
    stats = {12: {"reviews": 1}}
    monkeypatch.setattr(sync, "read_precis_stats", lambda _col: stats)
    if media_times_out:
        clock = iter([0, 600])
        monkeypatch.setattr(
            sync, "time", SimpleNamespace(time=lambda: 0, monotonic=lambda: next(clock))
        )
    else:
        monkeypatch.setattr(
            sync,
            "time",
            SimpleNamespace(time=lambda: 0, monotonic=lambda: 0, sleep=lambda _: None),
        )
    spec = AnkiCardSpec(
        12,
        {"Text": "{{c1::Avery}}", "Back Extra": "Ask about replication"},
        image=("precis-photo.png", b"image"),
    )

    result, returned_stats = sync.sync_tick(
        mirror_path="unused", user="user", password="pw", specs=[spec]
    )

    assert result.pushed == 1
    assert result.aborted is None
    assert returned_stats == stats
    assert result.stats_written == 1
    assert note == {
        "Text": '{{c1::Avery}}<br><img src="renamed&amp;photo.png">',
        "Back Extra": "Ask about replication",
    }
    col.media.write_data.assert_called_once_with("precis-photo.png", b"image")
    assert col.sync_media.call_args.args[0].endpoint == "https://example.org/sync"
    assert col.media_sync_status.call_count == (1 if media_times_out else 2)
    assert col.abort_media_sync.call_count == int(media_times_out)
    assert col.method_calls[-1] == call.close()


@pytest.mark.parametrize("full_download", [False, True])
@pytest.mark.parametrize("had_mirror", [False, True])
def test_remote_deletion_is_not_recreated(
    monkeypatch: pytest.MonkeyPatch, full_download: bool, had_mirror: bool
) -> None:
    """Pull deletions before upserting; a rebuilt mirror uses the PG receipt."""
    from precis.anki import sync

    col = MagicMock()
    col.db.list.return_value = [1_793_748_600_000]
    # Ref 7 was synced and then deleted remotely. Ref 8 is a new explicit
    # add of the same text; ref 9 still exists and remains editable.
    col.db.all.side_effect = [
        [(guid_for(7),), (guid_for(9),)] if had_mirror else [],
        [(guid_for(9),), ("foreign-guid",)],
    ]
    changes = SimpleNamespace(FULL_UPLOAD=1, FULL_SYNC=2, FULL_DOWNLOAD=3)
    col.sync_collection.side_effect = [
        SimpleNamespace(
            required=3 if full_download else 0, new_endpoint=None, server_media_usn=0
        ),
        SimpleNamespace(required=0),
    ]
    sp = SimpleNamespace(
        SyncCollectionResponse=SimpleNamespace(ChangesRequired=changes),
        SyncAuth=lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(sync, "_import_anki", lambda: (lambda path: col, sp))
    upsert = MagicMock(return_value=(1, 1))
    monkeypatch.setattr(sync, "upsert_notes", upsert)
    monkeypatch.setattr(sync, "read_precis_stats", lambda col: {})
    specs = [
        AnkiCardSpec(7, {"Text": "{{c1::same}}"}, previously_synced=not had_mirror),
        AnkiCardSpec(8, {"Text": "{{c1::same}}"}),
        AnkiCardSpec(9, {"Text": "{{c1::edited}}"}, previously_synced=True),
        AnkiCardSpec(
            10,
            {"Text": "{{c1::restored}}"},
            previously_synced=True,
            restore_pending=True,
        ),
    ]
    result, _ = sync.sync_tick(
        mirror_path="unused", user="user", password="pw", specs=specs
    )
    assert result.deleted_ref_ids == [7]
    assert result.review_ids == [1_793_748_600_000]
    assert "ease > 0" in col.db.list.call_args.args[0]
    assert upsert.call_args.args[1] == specs[1:]
    assert result.bootstrapped == full_download
    col.close.assert_called_once()


@pytest.mark.parametrize("required", ["FULL_UPLOAD", "FULL_SYNC", "FULL_DOWNLOAD"])
def test_post_upsert_full_sync_leaves_retag_pending(
    monkeypatch: pytest.MonkeyPatch, required: str
) -> None:
    from precis.anki import sync

    col = MagicMock()
    col.db.all.return_value = [(guid_for(7),)]
    changes = SimpleNamespace(
        FULL_UPLOAD=1, FULL_SYNC=2, FULL_DOWNLOAD=3, Name=lambda _: required
    )
    col.sync_collection.side_effect = [
        SimpleNamespace(required=0, new_endpoint=None),
        SimpleNamespace(required=getattr(changes, required)),
    ]
    sp = SimpleNamespace(
        SyncCollectionResponse=SimpleNamespace(ChangesRequired=changes),
        SyncAuth=lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(sync, "_import_anki", lambda: (lambda path: col, sp))
    upsert = MagicMock(return_value=(0, 1))
    monkeypatch.setattr(sync, "upsert_notes", upsert)
    read_stats = MagicMock()
    monkeypatch.setattr(sync, "read_precis_stats", read_stats)
    specs = [
        AnkiCardSpec(
            7, {"Text": "{{c1::x}}"}, deck="Precis::b", pending_deck_revision="retag"
        )
    ]

    result, stats = sync.sync_tick(
        mirror_path="unused", user="user", password="pw", specs=specs, project=True
    )

    assert result.aborted and required in result.aborted
    upsert.assert_called_once_with(col, specs, "Precis")
    col.full_upload_or_download.assert_not_called()
    read_stats.assert_not_called()
    assert result.all_cards is None
    assert stats == {}
    col.close.assert_called_once()


def test_boosted_specs_bring_due_forward_before_the_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only boosted specs are brought forward, and only their markers are
    reported as consumed (the caller clears those)."""
    from precis.anki import sync

    col = MagicMock()
    col.db.all.side_effect = [[], [(guid_for(5),), (guid_for(6),)]]
    changes = SimpleNamespace(FULL_UPLOAD=1, FULL_SYNC=2, FULL_DOWNLOAD=3)
    col.sync_collection.side_effect = [
        SimpleNamespace(required=0, new_endpoint=None, server_media_usn=0),
        SimpleNamespace(required=0),
    ]
    sp = SimpleNamespace(
        SyncCollectionResponse=SimpleNamespace(ChangesRequired=changes),
        SyncAuth=lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setattr(sync, "_import_anki", lambda: (lambda path: col, sp))
    monkeypatch.setattr(sync, "upsert_notes", MagicMock(return_value=(0, 0)))
    monkeypatch.setattr(sync, "read_precis_stats", lambda col: {})
    brought: list[list[int]] = []

    def _bring(c: Any, ids: Any) -> list[int]:
        brought.append(list(ids))
        return list(ids)

    monkeypatch.setattr(sync, "bring_due_today", _bring)
    specs = [
        AnkiCardSpec(5, {"Text": "{{c1::boosted}}"}, boost=True),
        AnkiCardSpec(6, {"Text": "{{c1::plain}}"}),
    ]
    result, _ = sync.sync_tick(
        mirror_path="unused", user="user", password="pw", specs=specs
    )
    assert brought == [[5]]
    assert result.boosted_ref_ids == [5]
    assert "1 boosted" in result.summary()
    col.close.assert_called_once()


def test_remote_deletion_retires_ref_across_ticks(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from precis.anki import sync
    from precis.workers.anki_sync import run_anki_sync

    _add_user(store, "reto", "rs")
    _configure_creds(monkeypatch, "reto", "reto@example.com")
    old = store.insert_ref(
        kind="anki",
        slug=None,
        title="{{c1::same}}",
        owner_login="reto",
        meta={"anki_synced_at": "2026-11-16T00:00:00Z"},
    )
    new = store.insert_ref(
        kind="anki", slug=None, title="{{c1::same}}", owner_login="reto"
    )
    calls: list[list[int]] = []

    def fake_tick(*, specs, **kw):
        calls.append([s.ref_id for s in specs])
        if len(calls) == 1:
            assert next(s for s in specs if s.ref_id == old.id).previously_synced
            return sync.SyncResult(deleted_ref_ids=[old.id]), {}
        return sync.SyncResult(), {}

    monkeypatch.setattr(sync, "sync_tick", fake_tick)
    cfg = _cfg(mirror_dir=str(tmp_path))
    run_anki_sync(store, cfg)
    run_anki_sync(store, cfg)
    assert old.id in calls[0]
    assert calls[1] == [new.id]


class TestPureHelpers:
    def test_guid_roundtrip(self) -> None:
        assert guid_for(1234) == "precis:1234"
        assert ref_id_from_guid("precis:1234") == 1234
        assert ref_id_from_guid("abc123") is None  # foreign note
        assert ref_id_from_guid("precis:notanint") is None

    def test_spec_from_ref_cloze(self) -> None:
        ref = SimpleNamespace(
            id=7,
            title="Paris is the {{c1::capital}} of France.",
            meta={
                "notetype": "Cloze",
                "fields": {
                    "Text": "Paris is the {{c1::capital}} of France.",
                    "Back Extra": "aka the City of Light",
                },
            },
        )
        spec = spec_from_ref(ref)
        assert spec is not None
        assert spec.ref_id == 7
        assert spec.fields["Text"].startswith("Paris")
        assert spec.fields["Back Extra"] == "aka the City of Light"

    def test_spec_from_ref_skips_non_cloze(self) -> None:
        ref = SimpleNamespace(id=1, title="x", meta={"notetype": "Basic"})
        assert spec_from_ref(ref) is None

    def test_spec_from_ref_skips_foreign_projection(self) -> None:
        # Regression (2026-07 incident): a read-only projection of a hand-made
        # card must NEVER be pushed back to Anki (that duplicated the account).
        foreign = SimpleNamespace(
            id=1,
            title="x {{c1::y}}",
            meta={
                "notetype": "Cloze",
                "source": "anki-foreign",
                "fields": {"Text": "x {{c1::y}}"},
            },
        )
        assert spec_from_ref(foreign) is None
        readonly = SimpleNamespace(
            id=2,
            title="x {{c1::y}}",
            meta={"notetype": "Cloze", "readonly": True, "fields": {"Text": "x"}},
        )
        assert spec_from_ref(readonly) is None

    def test_spec_from_ref_falls_back_to_title(self) -> None:
        ref = SimpleNamespace(id=2, title="Body {{c1::x}}", meta={})
        spec = spec_from_ref(ref)
        assert spec is not None and spec.fields["Text"] == "Body {{c1::x}}"
        assert "Back Extra" not in spec.fields

    @pytest.mark.parametrize(
        ("revision", "synced_revision", "pending"),
        [(None, None, None), ("new", "old", "new"), ("new", "new", None)],
    )
    def test_spec_from_ref_carries_only_pending_deck_retags(
        self, revision: str | None, synced_revision: str | None, pending: str | None
    ) -> None:
        ref = SimpleNamespace(
            id=5,
            title="x {{c1::y}}",
            meta={
                "deck": "Precis::ml-potentials",
                "anki_deck_revision": revision,
                "anki_synced_deck_revision": synced_revision,
            },
        )
        spec = spec_from_ref(ref)
        assert spec is not None
        assert spec.deck == "Precis::ml-potentials"
        assert spec.pending_deck_revision == pending

    def test_spec_from_ref_carries_the_boost_flag(self) -> None:
        """gr664414: ``meta.anki_boost_due`` (the /learning boost button)
        flags the spec so the tick brings the mirror card's due forward."""
        boosted = SimpleNamespace(
            id=3,
            title="x {{c1::y}}",
            meta={"anki_boost_due": "2026-11-17", "fields": {"Text": "x {{c1::y}}"}},
        )
        boosted_spec = spec_from_ref(boosted)
        assert boosted_spec is not None and boosted_spec.boost is True
        plain = SimpleNamespace(
            id=4, title="x {{c1::y}}", meta={"fields": {"Text": "x {{c1::y}}"}}
        )
        plain_spec = spec_from_ref(plain)
        assert plain_spec is not None and plain_spec.boost is False

    def test_aggregate_stats(self) -> None:
        # two cards of one cloze note: (ivl, factor, reps, lapses, due, queue)
        rows = [(4, 2500, 6, 1, 100, 2), (34, 2100, 3, 0, 200, 2)]
        st = aggregate_stats(rows)
        assert st["interval_min"] == 4
        assert st["interval_max"] == 34
        assert st["ease_min"] == 2.1
        assert st["reps_total"] == 9
        assert st["lapses_total"] == 1
        assert st["due_min"] == 100
        assert st["cards"] == 2
        assert st["unreviewed"] is False

    def test_aggregate_stats_empty(self) -> None:
        assert aggregate_stats([]) == {}


# ── collection ops — need the anki pylib, local only, no network ──────────
# The pure-helper tests above run everywhere; only these skip when the optional
# `anki` wheel is absent (the gate container; ansible installs it on the runner).


@pytest.fixture
def col(tmp_path):
    pytest.importorskip("anki")
    from anki.collection import Collection

    c = Collection(str(tmp_path / "probe.anki2"))
    yield c
    c.close()


def _specs(*pairs):
    from precis.anki.notes import AnkiCardSpec

    return [AnkiCardSpec(ref_id=r, fields=f) for r, f in pairs]


class TestUpsert:
    def test_insert_creates_cloze_note_with_guid_deck_tag(self, col) -> None:
        from precis.anki.sync import upsert_notes

        pushed, updated = upsert_notes(
            col, _specs((42, {"Text": "The {{c1::heart}} pumps blood."}))
        )
        assert (pushed, updated) == (1, 0)
        nids = col.find_notes("deck:Precis")
        assert len(nids) == 1
        note = col.get_note(nids[0])
        assert note.guid == guid_for(42)
        assert note.note_type()["name"] == "Cloze"
        assert precis_tag(42) in note.tags
        assert MANAGED_TAG in note.tags
        assert col.card_count() == 1  # one deletion → one card

    def test_reupsert_same_ref_updates_not_duplicates(self, col) -> None:
        from precis.anki.sync import upsert_notes

        upsert_notes(col, _specs((42, {"Text": "The {{c1::heart}} pumps blood."})))
        # same ref_id, edited text → update in place, guid preserved
        pushed, updated = upsert_notes(
            col, _specs((42, {"Text": "The {{c1::heart}} pumps {{c2::blood}}."}))
        )
        assert (pushed, updated) == (0, 1)
        nids = col.find_notes("deck:Precis")
        assert len(nids) == 1  # NOT duplicated
        note = col.get_note(nids[0])
        assert note.guid == guid_for(42)  # identity preserved across the edit
        assert "{{c2::blood}}" in note["Text"]

    def test_legacy_id_tag_folded_under_precis_id(self, col) -> None:
        from precis.anki.sync import upsert_notes

        specs = _specs((42, {"Text": "The {{c1::heart}} pumps blood."}))
        upsert_notes(col, specs)
        note = col.get_note(col.find_notes("deck:Precis")[0])
        note.tags = ["precis::42", MANAGED_TAG]
        col.update_note(note)
        upsert_notes(col, specs)
        note = col.get_note(col.find_notes("deck:Precis")[0])
        assert precis_tag(42) == "precis::id::42"
        assert "precis::42" not in note.tags
        assert "precis::id::42" in note.tags

    def test_two_refs_two_notes(self, col) -> None:
        from precis.anki.sync import upsert_notes

        pushed, _ = upsert_notes(
            col,
            _specs(
                (1, {"Text": "{{c1::A}}"}),
                (2, {"Text": "{{c1::B}}"}),
            ),
        )
        assert pushed == 2
        assert len(col.find_notes("deck:Precis")) == 2

    def test_upsert_uses_per_card_subdeck(self, col) -> None:
        from precis.anki.notes import AnkiCardSpec
        from precis.anki.sync import upsert_notes

        upsert_notes(
            col,
            [
                AnkiCardSpec(
                    ref_id=7, fields={"Text": "{{c1::x}}"}, deck="Precis::chinese"
                )
            ],
        )
        # Anki auto-creates the sub-deck; the note lands in it, not bare Precis.
        assert len(col.find_notes("deck:Precis::chinese")) == 1

    def test_deck_change_moves_existing_notes_cards(self, col) -> None:
        from precis.anki.sync import upsert_notes

        upsert_notes(col, _specs((7, {"Text": "{{c1::x}} and {{c2::y}}"})))
        nid = col.db.list("select id from notes where guid = ?", guid_for(7))[0]
        old_ids = {card.id for card in col.get_note(nid).cards()}
        pushed, updated = upsert_notes(
            col,
            [
                AnkiCardSpec(
                    ref_id=7,
                    fields={"Text": "{{c1::x}} and {{c2::y}}"},
                    deck="Precis::ml-potentials",
                    pending_deck_revision="retag",
                )
            ],
        )
        assert (pushed, updated) == (0, 1)
        cards = col.get_note(nid).cards()
        assert {card.id for card in cards} == old_ids
        assert all(
            col.decks.name(card.did) == "Precis::ml-potentials" for card in cards
        )

    def test_manual_anki_move_is_not_undone_without_recorded_retag(self, col) -> None:
        from precis.anki.sync import upsert_notes

        upsert_notes(col, _specs((7, {"Text": "{{c1::x}}"})))
        nid = col.db.list("select id from notes where guid = ?", guid_for(7))[0]
        note = col.get_note(nid)
        hand_did = col.decks.id("HandFiling")
        col.set_deck([c.id for c in note.cards()], hand_did)
        assert upsert_notes(col, _specs((7, {"Text": "{{c1::x}}"}))) == (0, 0)
        note = col.get_note(nid)
        assert all(col.decks.name(c.did) == "HandFiling" for c in note.cards())


class TestReadBack:
    def test_read_precis_stats_shape(self, col) -> None:
        from precis.anki.sync import read_precis_stats, upsert_notes

        upsert_notes(col, _specs((99, {"Text": "{{c1::x}} and {{c2::y}}"})))
        stats = read_precis_stats(col)
        assert set(stats) == {99}
        s = stats[99]
        assert s["cards"] == 2  # two deletions → two cards
        assert s["reps_total"] == 0 and s["unreviewed"] is True

    def test_read_all_cards_and_tag_filter(self, col) -> None:
        from precis.anki.sync import read_all_cards, upsert_notes

        upsert_notes(col, _specs((5, {"Text": "{{c1::z}}"})))
        allc = read_all_cards(col)
        assert len(allc) == 1
        fc = allc[0]
        assert fc.ref_id == 5 and fc.notetype == "Cloze"
        assert fc.deck == "Precis" and MANAGED_TAG in fc.tags
        # tag filter finds it by the per-ref tag, misses a bogus one
        assert len(read_all_cards(col, tag=precis_tag(5))) == 1
        assert len(read_all_cards(col, tag="precis-fix")) == 0


class TestBoost:
    """gr664414: ``bring_due_today`` applies a queued boost — the mirror
    review card's due date moves to today and NOTHING else changes."""

    def test_bring_due_today_moves_review_cards_and_keeps_history(self, col) -> None:
        from precis.anki.sync import _collection_today, bring_due_today, upsert_notes

        upsert_notes(
            col,
            _specs(
                (42, {"Text": "review {{c1::me}} today"}),
                (43, {"Text": "stay {{c1::new}}"}),
            ),
        )
        today = _collection_today(col)
        # Fabricate a reviewed card (queue 2) due far ahead, with history.
        nid = col.db.list("select id from notes where guid = ?", guid_for(42))[0]
        col.db.execute(
            "update cards set queue = 2, due = 400, ivl = 30, reps = 5, "
            "lapses = 1, factor = 2100 where nid = ?",
            nid,
        )
        consumed = bring_due_today(col, [42, 43, 999])
        assert consumed == [42]  # 43 is still a new card; 999 was never pushed
        due, queue, ivl, reps, lapses, factor = col.db.all(
            "select due, queue, ivl, reps, lapses, factor from cards where nid = ?",
            nid,
        )[0]
        assert due == today  # brought forward …
        assert (queue, ivl, reps, lapses, factor) == (
            2,
            30,
            5,
            1,
            2100,
        )  # … history untouched
        # The unboosted sibling card is exactly as upsert left it (still new).
        nid43 = col.db.list("select id from notes where guid = ?", guid_for(43))[0]
        (queue43,) = col.db.all("select queue from cards where nid = ?", nid43)[0]
        assert queue43 == 0
        assert bring_due_today(col, []) == []


class TestRetire:
    def test_remove_notes_for_refs_deletes_only_own_guids(self, col) -> None:
        from precis.anki.sync import remove_notes_for_refs, upsert_notes

        upsert_notes(
            col,
            _specs(
                (7, {"Text": "keep {{c1::me}}"}),
                (8, {"Text": "retire {{c1::me}}"}),
            ),
        )
        removed = remove_notes_for_refs(col, [8, 999])  # 999 was never pushed
        assert removed == 1
        remaining = [col.get_note(n).guid for n in col.find_notes("")]
        assert remaining == [guid_for(7)]

    def test_retired_ref_ids_excludes_foreign_projections(self, store) -> None:
        from precis.cli.anki_sync import _retired_ref_ids
        from precis.users import hash_password

        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        authored = store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::x}}"}},
            owner_login="reto",
        )
        foreign = store.insert_ref(
            kind="anki",
            slug=None,
            title="foreign",
            meta={"source": "anki-foreign", "readonly": True},
            owner_login="reto",
        )
        store.retire_ref(authored.id)
        store.retire_ref(foreign.id)
        ids = _retired_ref_ids(store, login="reto")
        assert int(authored.id) in ids
        assert int(foreign.id) not in ids

    def test_retired_ref_ids_scoped_to_owner(self, store) -> None:
        from precis.cli.anki_sync import _retired_ref_ids
        from precis.users import hash_password

        store.create_web_user(login="reto", abbrev="rs", password=hash_password("pw"))
        store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
        mine = store.insert_ref(
            kind="anki", slug=None, title="{{c1::x}}", owner_login="reto"
        )
        theirs = store.insert_ref(
            kind="anki", slug=None, title="{{c1::y}}", owner_login="alice"
        )
        store.retire_ref(mine.id)
        store.retire_ref(theirs.id)
        ids = _retired_ref_ids(store, login="reto")
        assert int(mine.id) in ids
        assert int(theirs.id) not in ids


# ── §A: workers/anki_sync.py — the store-taking core shared with the
# scheduler cadence. No sys.exit: raises so the CLI and the cadence wrapper
# each react in their own idiom. ──────────────────────────────────────────


def _cfg(mirror_dir=None, **overrides):
    return SimpleNamespace(
        anki_mirror_dir=mirror_dir,
        anki_deck="Precis",
        anki_fix_enabled=False,
        anki_project_enabled=False,
        **overrides,
    )


def _add_user(store, login: str, abbrev: str) -> None:
    from precis.users import hash_password

    store.create_web_user(login=login, abbrev=abbrev, password=hash_password("pw"))


def _configure_creds(monkeypatch: pytest.MonkeyPatch, login: str, email: str) -> None:
    # env-override-wins is get_secret's documented resolution order (§1) — the
    # sanctioned way to hand a test a credential without a real vault round trip.
    monkeypatch.setenv(f"ANKI_USER:{login}", email)
    monkeypatch.setenv(f"ANKI_PASSWORD:{login}", "hunter2")


class TestRunAnkiSyncNoUsers:
    def test_no_configured_users_returns_a_summary_not_an_exception(
        self, store
    ) -> None:
        from precis.workers.anki_sync import run_anki_sync

        summary = run_anki_sync(store, _cfg())
        assert (
            summary
            == "anki-sync: no user has AnkiWeb credentials — add them on /account"
        )

    def test_explicit_unconfigured_login_raises_misconfigured(self, store) -> None:
        from precis.workers.anki_sync import AnkiSyncMisconfigured, run_anki_sync

        _add_user(store, "reto", "rs")
        with pytest.raises(AnkiSyncMisconfigured):
            run_anki_sync(store, _cfg(), login="reto")

    def test_missing_mirror_dir_raises_once_a_user_is_configured(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers.anki_sync import AnkiSyncMisconfigured, run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        with pytest.raises(AnkiSyncMisconfigured):
            run_anki_sync(store, _cfg(mirror_dir=None))


class TestRunAnkiSyncDryRun:
    def test_dry_run_reports_counts_for_the_configured_user(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::x}}"}},
            owner_login="reto",
        )
        summary = run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)), dry_run=True)
        assert "anki-sync[reto] [DRY-RUN]: 1 cloze card(s) would sync" in summary

    def test_dry_run_does_not_call_sync_tick(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::x}}"}},
            owner_login="reto",
        )
        called = []
        monkeypatch.setattr(sync_mod, "sync_tick", lambda **kw: called.append(kw))
        run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)), dry_run=True)
        assert not called


class TestRunAnkiSyncClaim:
    def test_sole_user_claims_unowned_refs_before_syncing(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        card = store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::x}}"}},
        )  # unowned — pre-migration legacy row
        assert card.owner_login is None

        seen_specs = []

        def _fake_sync_tick(*, specs, **kw):
            seen_specs.append(specs)
            return SyncResult(pushed=len(specs)), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake_sync_tick)
        summary = run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))

        assert store.get_ref(kind="anki", id=card.id).owner_login == "reto"
        assert "1 unowned ref(s) claimed" in summary
        # the claim happened BEFORE the ref list was built — the claimed card
        # rides this same tick, not just the next one.
        assert len(seen_specs) == 1 and len(seen_specs[0]) == 1

    def test_second_configured_user_disables_the_solo_claim(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _add_user(store, "alice", "al")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        _configure_creds(monkeypatch, "alice", "alice@example.com")
        card = store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::x}}"}},
        )  # unowned, and now ambiguous — two configured users

        monkeypatch.setattr(sync_mod, "sync_tick", lambda **kw: (SyncResult(), {}))
        run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        assert store.get_ref(kind="anki", id=card.id).owner_login is None


class TestRunAnkiSyncLock:
    def test_held_per_user_lock_skips_that_user_only(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The per-user xact lock is held elsewhere => skip message, no
        ``sync_tick``; another login's lock is independent (gr463966)."""
        import precis.anki.sync as sync_mod
        from precis.anki.notes import ANKI_SYNC_LOCK
        from precis.anki.sync import SyncResult
        from precis.store.advisory import try_xact_advisory_lock
        from precis.workers.anki_sync import run_anki_sync
        from tests.workers._helpers import skip_if_backends_multiplexed

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::x}}"}},
            owner_login="reto",
        )
        called: list[dict] = []

        def _fake(**kw):
            called.append(kw)
            return SyncResult(), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake)
        assert store.dsn
        skip_if_backends_multiplexed(store.dsn)
        cfg = _cfg(mirror_dir=str(tmp_path))
        with try_xact_advisory_lock(store.dsn, ANKI_SYNC_LOCK, text_key="reto") as h:
            assert h is True
            summary = run_anki_sync(store, cfg)
        assert "another sync holds the lock; skipping." in summary
        assert not called
        # Released on exit: the next tick syncs.
        run_anki_sync(store, cfg)
        assert len(called) == 1


class TestRunAnkiSyncFanOut:
    def test_two_users_get_two_sync_tick_calls_with_their_own_mirror_and_cards(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _add_user(store, "alice", "al")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        _configure_creds(monkeypatch, "alice", "alice@example.com")
        store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::mine}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::mine}}"}},
            owner_login="reto",
        )
        store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::theirs}}",
            meta={"notetype": "Cloze", "fields": {"Text": "{{c1::theirs}}"}},
            owner_login="alice",
        )

        calls: dict[str, dict] = {}

        def _fake_sync_tick(*, mirror_path, user, password, specs, **kw):
            login = "reto" if "/reto/" in mirror_path else "alice"
            calls[login] = {
                "mirror_path": mirror_path,
                "user": user,
                "password": password,
                "n_specs": len(specs),
            }
            return SyncResult(pushed=len(specs)), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake_sync_tick)
        summary = run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))

        assert set(calls) == {"reto", "alice"}
        assert calls["reto"]["n_specs"] == 1
        assert calls["alice"]["n_specs"] == 1
        assert calls["reto"]["user"] == "reto@example.com"
        assert calls["alice"]["user"] == "alice@example.com"
        assert calls["reto"]["mirror_path"] != calls["alice"]["mirror_path"]
        assert "anki-sync[reto]:" in summary and "anki-sync[alice]:" in summary

    def test_one_users_failure_does_not_stop_the_other(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import AnkiSyncError, SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _add_user(store, "alice", "al")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        _configure_creds(monkeypatch, "alice", "alice@example.com")

        def _fake_sync_tick(*, mirror_path, **kw):
            if "/alice/" in mirror_path:
                raise AnkiSyncError("boom")
            return SyncResult(pushed=0), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake_sync_tick)
        with pytest.raises(AnkiSyncError) as exc_info:
            run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        message = str(exc_info.value)
        assert "reto" in message and "alice" in message and "boom" in message


class TestRunAnkiSyncBoost:
    """gr664414: a consumed boost (note was in the mirror, due rode the push
    forward) clears ``meta.anki_boost_due``; an unconsumed one stays queued."""

    def _boosted_card(self, store):
        return store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={
                "notetype": "Cloze",
                "fields": {"Text": "{{c1::x}}"},
                "anki_boost_due": "2026-11-17",
            },
            owner_login="reto",
        )

    def test_consumed_boost_clears_the_marker(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        card = self._boosted_card(store)
        monkeypatch.setattr(
            sync_mod,
            "sync_tick",
            lambda **kw: (SyncResult(boosted_ref_ids=[card.id]), {}),
        )
        summary = run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        assert "1 boosted" in summary
        meta = store.get_ref(kind="anki", id=card.id).meta or {}
        assert "anki_boost_due" not in meta

    def test_unconsumed_boost_keeps_the_marker(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        card = self._boosted_card(store)
        monkeypatch.setattr(sync_mod, "sync_tick", lambda **kw: (SyncResult(), {}))
        run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        meta = store.get_ref(kind="anki", id=card.id).meta or {}
        assert meta.get("anki_boost_due") == "2026-11-17"

    def test_aborted_sync_keeps_the_marker(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """An aborted push never applied anything — the marker (and the due
        change, held in the local mirror) survive to the next tick."""
        import precis.anki.sync as sync_mod
        from precis.anki.sync import AnkiSyncError, SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        card = self._boosted_card(store)
        monkeypatch.setattr(
            sync_mod,
            "sync_tick",
            lambda **kw: (SyncResult(boosted_ref_ids=[card.id], aborted="boom"), {}),
        )
        with pytest.raises(AnkiSyncError):
            run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        meta = store.get_ref(kind="anki", id=card.id).meta or {}
        assert meta.get("anki_boost_due") == "2026-11-17"


class TestDeckRetagReceipt:
    @pytest.mark.parametrize("aborted", [False, True])
    def test_tick_acknowledges_only_the_pushed_revision(
        self,
        store,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
        aborted: bool,
    ) -> None:
        import precis.anki.sync as sync_mod
        from precis.anki.sync import AnkiSyncError, SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        card = store.insert_ref(
            kind="anki",
            slug=None,
            title="{{c1::x}}",
            meta={
                "deck": "Precis::ml-potentials",
                "anki_deck_revision": "retag-1",
                "anki_restore_pending": True,
            },
            owner_login="reto",
        )

        def sync_tick(**kwargs: Any) -> tuple[SyncResult, dict[int, dict[str, int]]]:
            assert kwargs["specs"][0].pending_deck_revision == "retag-1"
            assert kwargs["specs"][0].restore_pending
            # A retag arriving while AnkiWeb sync is in flight stays pending.
            store.update_ref(
                card.id,
                meta_patch={"deck": "Precis::b", "anki_deck_revision": "retag-2"},
            )
            if aborted:
                return SyncResult(aborted="incremental push required FULL_SYNC"), {}
            return SyncResult(updated=1), {card.id: {"cards": 1}}

        monkeypatch.setattr(sync_mod, "sync_tick", sync_tick)
        cfg = _cfg(mirror_dir=str(tmp_path))
        if aborted:
            with pytest.raises(AnkiSyncError, match="FULL_SYNC"):
                run_anki_sync(store, cfg)
        else:
            run_anki_sync(store, cfg)
        ref = store.get_ref(kind="anki", id=card.id)
        assert (ref.meta or {}).get("anki_synced_deck_revision") == (
            None if aborted else "retag-1"
        )
        assert bool(ref.meta.get("anki_restore_pending")) is aborted
        spec = spec_from_ref(ref)
        assert spec is not None and spec.pending_deck_revision == "retag-2"


class TestReviewSync:
    def test_card_less_user_imports_reviews_without_double_counting(
        self, store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from precis.anki import sync as sync_mod
        from precis.learning import LearningStore
        from precis.user_context import user_scope
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        account = store.get_web_user("reto")
        assert account is not None
        now = datetime.now(UTC)
        review_id = int(now.timestamp() * 1000)
        monkeypatch.setattr(
            sync_mod,
            "sync_tick",
            lambda **kw: (sync_mod.SyncResult(review_ids=[review_id]), {}),
        )
        cfg = _cfg(mirror_dir=str(tmp_path))
        run_anki_sync(store, cfg)
        run_anki_sync(store, cfg)
        with user_scope(account.id):
            activity = LearningStore(store).activity(days=1, now=now)
        assert activity["days"][0]["anki_reviews"] == 1

    def test_one_card_is_enough_to_sync(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from precis.anki import sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        store.insert_ref(kind="anki", slug=None, title="{{c1::x}}", owner_login="reto")

        calls: list[str] = []

        def _fake_sync_tick(*, mirror_path, **kw):
            calls.append(mirror_path)
            return SyncResult(pushed=1), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake_sync_tick)
        summary = run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        assert len(calls) == 1
        assert "nothing to sync" not in summary

    def test_a_retired_ref_alone_still_syncs(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """No live cards but a retired one: the mirror still needs the removal."""
        from precis.anki import sync as sync_mod
        from precis.anki.sync import SyncResult
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")
        ref = store.insert_ref(
            kind="anki", slug=None, title="{{c1::x}}", owner_login="reto"
        )
        store.retire_ref(ref.id)

        calls: list[list[int]] = []

        def _fake_sync_tick(*, retire_ref_ids, **kw):
            calls.append(list(retire_ref_ids or []))
            return SyncResult(pushed=0), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake_sync_tick)
        run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        assert calls == [[ref.id]]


class TestAnkiCadenceBackoff:
    """A failed cadence tick backs off instead of re-running at full rate."""

    def test_interval_is_daily_not_half_hourly(self) -> None:
        from precis.workers.scheduler import CADENCES

        (anki,) = [c for c in CADENCES if c.name == "anki_sync"]
        assert anki.interval_s >= 24 * 3600, (
            "anki_sync hits someone else's server; it must not go back to a "
            "sub-daily cadence without a deliberate decision"
        )

    def test_failure_widens_the_window_and_success_clears_it(self, store) -> None:
        from precis.workers import scheduler

        assert scheduler._anki_backoff_until(store) is None

        scheduler._anki_record_failure(store)
        first = scheduler._anki_backoff_until(store)
        assert scheduler._anki_fail_count(store) == 1
        assert first is not None

        scheduler._anki_record_failure(store)
        second = scheduler._anki_backoff_until(store)
        assert scheduler._anki_fail_count(store) == 2
        assert second is not None and second > first

        scheduler._anki_clear_backoff(store)
        assert scheduler._anki_fail_count(store) == 0
        assert scheduler._anki_backoff_until(store) is None

    def test_backoff_is_capped(self, store) -> None:
        from precis.workers import scheduler

        for _ in range(scheduler._ANKI_MAX_BACKOFF_STEPS + 4):
            scheduler._anki_record_failure(store)
        until = scheduler._anki_backoff_until(store)
        assert until is not None
        cap_h = scheduler._ANKI_MAX_BACKOFF_STEPS * scheduler._ANKI_BACKOFF_STEP_HOURS
        assert until - datetime.now(UTC) <= timedelta(hours=cap_h)

    def test_cadence_skips_while_backing_off(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers import scheduler

        def _boom(*a, **kw):
            raise AssertionError("must not sync while backing off")

        monkeypatch.setattr("precis.workers.anki_sync.run_anki_sync", _boom)
        scheduler._anki_record_failure(store)
        scheduler._run_anki_sync(store, 1)  # returns quietly

    def test_a_failing_tick_records_backoff_and_re_raises(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.anki.sync import AnkiSyncError
        from precis.workers import scheduler

        def _fail(*a, **kw):
            raise AnkiSyncError("boom")

        monkeypatch.setattr("precis.workers.anki_sync.run_anki_sync", _fail)
        with pytest.raises(AnkiSyncError):
            scheduler._run_anki_sync(store, 1)
        assert scheduler._anki_fail_count(store) == 1
        assert scheduler._anki_backoff_until(store) is not None

    def test_a_clean_tick_clears_a_prior_backoff(
        self, store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from precis.workers import scheduler

        scheduler._anki_record_failure(store)
        # Wind the window back so the gate lets this tick through.
        store.set_setting(
            scheduler._ANKI_NEXT_ATTEMPT_KEY,
            (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        )
        monkeypatch.setattr(
            "precis.workers.anki_sync.run_anki_sync", lambda *a, **kw: "ok"
        )
        scheduler._run_anki_sync(store, 1)
        assert scheduler._anki_fail_count(store) == 0
        assert scheduler._anki_backoff_until(store) is None
