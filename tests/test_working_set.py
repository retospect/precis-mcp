"""Working-set data model + snapshot round-trip (ADR 0051 §6/§15, phase B)."""

from __future__ import annotations

import pytest

from precis.workers.working_set import (
    META_KEY,
    SCHEMA_VERSION,
    Extent,
    Eye,
    Persistence,
    Provenance,
    WorkingSet,
    adopt,
    default_persistence,
    ttl_for,
)


def test_extent_is_an_ordinal_ladder() -> None:
    assert Extent.NONE < Extent.TOC < Extent.SUMMARY < Extent.FULL < Extent.FIDELITY
    assert Extent.parse("full") is Extent.FULL
    assert Extent.parse(4) is Extent.FIDELITY
    assert Extent.parse(Extent.TOC) is Extent.TOC


def test_extent_vocabulary_and_hop1() -> None:
    # the top rung + the user-facing vocabulary aliases (§ refeye)
    assert Extent.NONE < Extent.FIDELITY < Extent.HOP1
    assert Extent.parse("kwd") is Extent.TOC
    assert Extent.parse("verbatim") is Extent.FULL
    assert Extent.parse("fisheye") is Extent.FIDELITY
    assert Extent.parse("fisheye+1hop") is Extent.HOP1
    assert Extent.parse("1hop") is Extent.HOP1
    assert Extent.parse(5) is Extent.HOP1
    # labels are the user vocabulary
    assert Extent.HOP1.label == "fisheye+1hop"
    assert Extent.FULL.label == "verbatim"
    assert Extent.FIDELITY.label == "fisheye"


def test_hop1_decay_peels_the_ring_first() -> None:
    # a neglected fisheye+1hop eye sheds its reference ring before its
    # neighborhood: HOP1 → fisheye → kwd → gone (§6b).
    ws = WorkingSet()
    ws.focus("pc1", "fisheye+1hop")
    assert ws.get("pc1").extent is Extent.HOP1
    ws.age(4)
    demoted, dropped = ws.crunch()
    assert demoted == ["pc1"] and dropped == []
    assert ws.get("pc1").extent is Extent.FIDELITY  # ring peeled, neighborhood kept
    ws.age(4)
    ws.crunch()
    assert ws.get("pc1").extent is Extent.TOC  # then down to kwd
    ws.age(4)
    _, dropped = ws.crunch()
    assert dropped == ["pc1"]  # then gone


def test_persistence_derives_from_provenance() -> None:
    # explicit → normal; auto-lens → transient (a fading suggestion, §6)
    assert default_persistence(Provenance.REQUESTED) is Persistence.NORMAL
    assert default_persistence(Provenance.INFERRED) is Persistence.TRANSIENT


def test_focus_places_and_replaces_one_eye_per_handle() -> None:
    ws = WorkingSet()
    ws.focus("pc12", "summary")
    assert ws.get("pc12").extent is Extent.SUMMARY
    # re-focus replaces (the refresh/adopt action, §6)
    ws.focus("pc12", "full")
    assert len(ws.eyes) == 1
    assert ws.get("pc12").extent is Extent.FULL


def test_focus_none_clears_the_eye() -> None:
    ws = WorkingSet()
    ws.focus("pc12", "full")
    ws.focus("pc12", "none")
    assert ws.get("pc12") is None


def test_inferred_eye_is_transient() -> None:
    ws = WorkingSet()
    ws.focus("pc99", "toc", provenance=Provenance.INFERRED)
    eye = ws.get("pc99")
    assert eye.provenance is Provenance.INFERRED
    assert eye.persistence is Persistence.TRANSIENT


def test_pin_never_decays() -> None:
    ws = WorkingSet()
    ws.pin("pe3", "fidelity")
    assert ws.get("pe3").persistence is Persistence.PINNED


def test_cursor_is_model_owned_and_clearable() -> None:
    ws = WorkingSet()
    ws.set_cursor("pe7")
    assert ws.cursor == "pe7"
    ws.set_cursor("")
    assert ws.cursor is None


def test_snapshot_round_trips_through_meta() -> None:
    ws = WorkingSet()
    ws.focus("pc12", "full")
    ws.focus("pc88", "toc", provenance=Provenance.INFERRED)
    ws.pin("pe1", "fidelity")
    ws.set_cursor("pe1")

    patch = ws.to_meta_patch()
    assert patch[META_KEY]["version"] == SCHEMA_VERSION
    # simulate the tick writing then the next tick reading its meta
    restored = WorkingSet.from_meta(patch)
    assert restored.cursor == "pe1"
    assert restored.get("pc12").extent is Extent.FULL
    assert restored.get("pc88").persistence is Persistence.TRANSIENT
    assert restored.get("pe1").persistence is Persistence.PINNED


def test_from_meta_degrades_to_empty_on_missing_or_bad_snapshot() -> None:
    assert WorkingSet.from_meta(None).eyes == {}
    assert WorkingSet.from_meta({}).eyes == {}
    # future/unknown version → cold start, not a crash
    assert WorkingSet.from_meta({META_KEY: {"version": 999}}).eyes == {}


def test_from_meta_skips_a_corrupt_eye_keeps_the_rest() -> None:
    snap = {
        META_KEY: {
            "version": SCHEMA_VERSION,
            "eyes": [
                {"handle": "pc1", "extent": 3},
                {"extent": 3},  # missing handle — corrupt
                {"handle": "pc2", "extent": "bogus"},  # bad extent — corrupt
            ],
            "cursor": None,
        }
    }
    ws = WorkingSet.from_meta(snap)
    assert set(ws.eyes) == {"pc1"}


def test_copy_is_independent_for_fork() -> None:
    ws = WorkingSet()
    ws.focus("pc1", "full")
    child = ws.copy()
    child.focus("pc2", "toc")
    child.set_cursor("pe9")
    assert "pc2" not in ws.eyes  # parent unaffected by the fork's diff
    assert ws.cursor is None


def test_ttl_for_by_persistence() -> None:
    assert ttl_for(Persistence.TRANSIENT) == 1
    assert ttl_for(Persistence.NORMAL) == 4  # default floor
    assert ttl_for(Persistence.PINNED) is None


def test_ttl_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import precis.workers.working_set as ws

    monkeypatch.setenv("PRECIS_EYE_TTL", "9")
    assert ws.ttl_for(Persistence.NORMAL) == 9
    monkeypatch.setenv("PRECIS_EYE_TTL", "bad")
    assert ws.ttl_for(Persistence.NORMAL) == 4  # malformed → default


def test_focus_derives_ttl_from_persistence() -> None:
    ws = WorkingSet()
    ws.focus("pc1", "full")  # normal
    ws.focus("pc2", "toc", provenance=Provenance.INFERRED)  # transient
    assert ws.get("pc1").ttl == 4
    assert ws.get("pc2").ttl == 1


def test_age_ripens_but_does_not_drop_and_spares_pinned() -> None:
    ws = WorkingSet()
    ws.focus("pc1", "full")  # ttl 4
    ws.pin("pe1", "fidelity")  # pinned, no ttl
    ws.age(2)
    assert ws.get("pc1").ttl == 2
    assert ws.get("pe1").ttl is None  # pinned untouched
    assert ws.expiring() == []  # nothing ripe yet
    # floor at 0, never negative
    ws.age(10)
    assert ws.get("pc1").ttl == 0
    assert "pc1" in ws.expiring()


def test_crunch_demotes_a_neglected_normal_eye_then_drops_it() -> None:
    ws = WorkingSet()
    ws.focus("pc1", "full")
    ws.age(4)  # ripe
    demoted, dropped = ws.crunch()
    assert demoted == ["pc1"] and dropped == []
    assert ws.get("pc1").extent is Extent.TOC  # full → toc (first warn)
    assert ws.get("pc1").ttl == 4  # refreshed at the new rung
    # neglected again at toc → gone
    ws.age(4)
    demoted, dropped = ws.crunch()
    assert dropped == ["pc1"] and demoted == []
    assert ws.get("pc1") is None


def test_crunch_drops_a_transient_lens_without_demoting() -> None:
    ws = WorkingSet()
    ws.focus("pc9", "full", provenance=Provenance.INFERRED)  # transient, ttl 1
    ws.age(1)  # ripe at next crunch
    demoted, dropped = ws.crunch()
    assert dropped == ["pc9"] and demoted == []  # dies, does not demote to toc
    assert ws.get("pc9") is None


def test_crunch_never_touches_pinned() -> None:
    ws = WorkingSet()
    ws.pin("pe1", "full")
    ws.age(100)
    assert ws.expiring() == []
    assert ws.crunch() == ([], [])
    assert ws.get("pe1").persistence is Persistence.PINNED


def test_crunch_is_bunched_over_all_ripe_eyes() -> None:
    ws = WorkingSet()
    ws.focus("pc1", "full")
    ws.focus("pc2", "summary")
    ws.focus("pc3", "toc")
    ws.age(4)
    demoted, dropped = ws.crunch()
    assert set(demoted) == {"pc1", "pc2"}  # rich → toc
    assert dropped == ["pc3"]  # toc → gone, in the same batch


def test_adopt_promotes_an_inferred_eye() -> None:
    inferred = Eye(
        handle="pc5",
        extent=Extent.SUMMARY,
        persistence=Persistence.TRANSIENT,
        provenance=Provenance.INFERRED,
    )
    promoted = adopt(inferred)
    assert promoted.provenance is Provenance.REQUESTED
    assert promoted.persistence is Persistence.NORMAL
    assert promoted.extent is Extent.SUMMARY  # extent unchanged
