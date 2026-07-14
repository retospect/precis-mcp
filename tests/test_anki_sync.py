"""Tests for the Anki sync engine (slice 2).

Pure helpers (`precis.anki.notes`) run everywhere. The collection ops
(`precis.anki.sync`) need the optional `anki` pylib and run against a throwaway
*local* `.anki2` — NO network, so they're safe in CI when `anki` is present and
skip cleanly when it isn't (the gate container doesn't bake the wheel; ansible
installs it only on the sync runner).
"""

from __future__ import annotations

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
