"""Tests for the Anki sync engine (slice 2).

Pure helpers (`precis.anki.notes`) run everywhere. The collection ops
(`precis.anki.sync`) need the optional `anki` pylib and run against a throwaway
*local* `.anki2` — NO network, so they're safe in CI when `anki` is present and
skip cleanly when it isn't (the gate container doesn't bake the wheel; ansible
installs it only on the sync runner).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from precis.anki.notes import (
    MANAGED_TAG,
    aggregate_stats,
    guid_for,
    precis_tag,
    ref_id_from_guid,
    spec_from_ref,
)


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
        # Each user needs a card: with nothing to push and nothing to retire the
        # no-op shortcut skips the AnkiWeb round-trip, so a card-less fan-out
        # would never reach sync_tick at all.
        store.insert_ref(kind="anki", slug=None, title="{{c1::x}}", owner_login="reto")
        store.insert_ref(kind="anki", slug=None, title="{{c1::y}}", owner_login="alice")

        def _fake_sync_tick(*, mirror_path, **kw):
            if "/alice/" in mirror_path:
                raise AnkiSyncError("boom")
            return SyncResult(pushed=0), {}

        monkeypatch.setattr(sync_mod, "sync_tick", _fake_sync_tick)
        with pytest.raises(AnkiSyncError) as exc_info:
            run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        message = str(exc_info.value)
        assert "reto" in message and "alice" in message and "boom" in message


class TestNoOpShortcut:
    """Nothing to push and nothing to retire ⇒ no AnkiWeb round-trip at all."""

    def test_card_less_user_skips_the_ankiweb_roundtrip(
        self, store, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from precis.anki import sync as sync_mod
        from precis.workers.anki_sync import run_anki_sync

        _add_user(store, "reto", "rs")
        _configure_creds(monkeypatch, "reto", "reto@example.com")

        def _boom(**kw):
            raise AssertionError("sync_tick must not be reached with nothing to sync")

        monkeypatch.setattr(sync_mod, "sync_tick", _boom)
        summary = run_anki_sync(store, _cfg(mirror_dir=str(tmp_path)))
        assert "nothing to sync" in summary
        assert "skipped the AnkiWeb round-trip" in summary

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
