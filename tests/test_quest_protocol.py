"""Tests for :mod:`precis.quest.protocol` (gr593142).

Covers the per-quest protocol record (config/pathway derivation, the tier
overlay), the protocol drift logbook entry (diff fields, dedupe), the
candidate protocol histogram, and the header lines. Store-backed tests run
against real PG (the ``store`` fixture); derivation/diff tests are pure.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import protocol as protocol_mod
from precis.quest.protocol import (
    QuestProtocol,
    protocol_diff,
    protocol_from_config,
    protocol_from_pathway,
)

_VERSION_ENV = "PRECIS_AUTOCATPATH_VERSION"

_RX = {
    "substrate": "NO",
    "target": "NH3",
    "network": "ammonia",
    "mlip": {"backend": "mace", "model": "medium"},
}


def _mk_quest(store: Any, text: str = "A NO→NH3 catalyst") -> int:
    resp = QuestHandler(hub=Hub(store=store)).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _mk_candidate(store: Any, qid: int, slug: str) -> int:
    """A bare `structure` ref serving the quest — enough for the protocol
    seams (``_candidate_struct_ids`` + ``_serving_quest_id`` read the
    link/kind, never the geometry)."""
    ref = store.insert_ref(kind="structure", slug=slug, title=f"cand {slug}")
    store.add_link(
        src_ref_id=int(ref.id), dst_ref_id=qid, relation="serves", set_by="system"
    )
    return int(ref.id)


def _mk_pathway(
    store: Any,
    cand_id: int,
    slug: str,
    *,
    meta: dict[str, Any],
) -> int:
    ref = store.insert_ref(
        kind="pathway",
        slug=slug,
        title=f"pw {slug}",
        meta={"candidate_ref": cand_id, **meta},
        parent_id=cand_id,
    )
    return int(ref.id)


def _log_texts(store: Any, qid: int) -> list[str]:
    return [b.text for b in store.chunks.list_chunks_for_ref(qid)]


def _drift_entries(store: Any, qid: int) -> list[Any]:
    return [
        b
        for b in store.chunks.list_chunks_for_ref(qid)
        if (b.meta or {}).get("protocol_drift_key")
    ]


# ── pure derivation ───────────────────────────────────────────────────


class TestProtocolFromConfig:
    def test_mace_single_model(self) -> None:
        p = protocol_from_config(
            {"network": "ammonia", "mlip": {"backend": "mace", "model": "medium"}},
            engine="0.50.0",
        )
        assert p.model == "mace:medium"
        assert p.engine == "0.50.0"
        assert p.template == "coadsorbed"
        assert p.seeds == 3  # default search.seeds [0,1,2]
        assert p.tolerances.get("endpoint_tol_eV") == pytest.approx(0.05)

    def test_explicit_template_and_seeds(self) -> None:
        p = protocol_from_config(
            {
                "network": "ammonia",
                "template": "parked",
                "search": {"seeds": [0], "energy_thresh": 0.1},
            }
        )
        assert p.template == "parked"
        assert p.seeds == 1
        assert p.tolerances["energy_thresh_eV"] == pytest.approx(0.1)

    def test_multi_model_label(self) -> None:
        p = protocol_from_config(
            {"mlip": {"backend": "mace", "models": ["medium", "orb:v2"]}}
        )
        assert p.model == "mace:medium+orb:v2"

    def test_non_ammonia_defaults_parked(self) -> None:
        assert protocol_from_config({"network": "other"}).template == "parked"


class TestProtocolFromPathway:
    def _pw(self, **extra: Any) -> dict[str, Any]:
        return {
            "autocatpath_version": "0.32.0",
            "config": dict(_RX),
            "requested_seeds": [0, 1, 2],
            "results": {"seeds": [0, 1, 2], "trust": []},
            "tier": "neb",
            **extra,
        }

    def test_reads_stamps(self) -> None:
        p = protocol_from_pathway(self._pw())
        assert p is not None
        assert p.engine == "0.32.0"
        assert p.model == "mace:medium"
        assert p.template == "coadsorbed"
        assert p.seeds == 3

    def test_requested_beats_declared_and_config(self) -> None:
        pw = self._pw(requested_seeds=[0], results={"seeds": [0, 1]})
        pw["config"]["search"] = {"seeds": [0, 1, 2]}
        p = protocol_from_pathway(pw)
        assert p is not None and p.seeds == 1

    def test_stamped_tol_wins(self) -> None:
        pw = self._pw()
        pw["results"]["trust"] = [
            {
                "check": "endpoint_agreement",
                "evidence": {"tol_eV": 0.07},
            }
        ]
        p = protocol_from_pathway(pw)
        assert p is not None
        assert p.tolerances["endpoint_tol_eV"] == pytest.approx(0.07)

    def test_no_config_is_none(self) -> None:
        assert protocol_from_pathway({"autocatpath_version": "0.32.0"}) is None
        assert protocol_from_pathway(None) is None


class TestProtocolDiff:
    def test_each_field(self) -> None:
        e = QuestProtocol(
            model="mace:medium",
            engine="0.50.0",
            template="coadsorbed",
            seeds=3,
            tolerances={"endpoint_tol_eV": 0.05},
        )
        a = QuestProtocol(
            model="emt",
            engine="0.32.0",
            template="parked",
            seeds=1,
            tolerances={"endpoint_tol_eV": 0.07},
        )
        diff = protocol_diff(e, a)
        assert set(diff) == {"model", "engine", "template", "seeds", "tolerances"}
        assert diff["engine"] == ("0.50.0", "0.32.0")

    def test_unprovable_actual_never_diffs(self) -> None:
        e = QuestProtocol(model="mace:medium", engine="0.50.0", seeds=3)
        a = QuestProtocol()  # nothing stamped — pre-stamp run, not drift
        assert protocol_diff(e, a) == {}

    def test_equal_is_empty(self) -> None:
        e = QuestProtocol(model="mace:medium", engine="0.50.0", seeds=3)
        assert protocol_diff(e, e) == {}


# ── store-backed ──────────────────────────────────────────────────────


class TestQuestProtocol:
    def test_neb_tier_derives(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        p = protocol_mod.quest_protocol(store, qid, tier="neb")
        assert p is not None
        assert p.model == "mace:medium"
        assert p.engine == "0.50.0"
        assert p.template == "coadsorbed"
        assert p.seeds == 3

    def test_neb_tier_widens_short_seed_list(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        cfg = dict(_RX, search={"seeds": [0]})
        store.stamp_ref_meta(qid, {"reaction_config": cfg})
        # _neb_multiseed_config widens a declared <3 list to 3 fresh seeds —
        # a one-seed pin is not a license to dispatch under-powered evidence.
        p = protocol_mod.quest_protocol(store, qid, tier="neb")
        assert p is not None and p.seeds == 3

    def test_screening_tier_is_parked_not_drift(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ladder's screening rung pins ``template='parked'`` by design —
        comparing a screening run against it must not report drift."""
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        p = protocol_mod.quest_protocol(store, qid, tier="screening")
        assert p is not None and p.template == "parked"

    def test_no_reaction_config_is_none(self, store: Any) -> None:
        qid = _mk_quest(store)
        assert protocol_mod.quest_protocol(store, qid) is None


class TestNoteProtocolDrift:
    def test_appends_one_observation(self, store: Any) -> None:
        qid = _mk_quest(store)
        expected = QuestProtocol(model="mace:medium", engine="0.50.0", seeds=3)
        actual = QuestProtocol(model="mace:medium", engine="0.32.0", seeds=1)
        n = protocol_mod.note_protocol_drift(
            store, qid, subject="st1 pw2", expected=expected, actual=actual
        )
        assert n is not None
        entries = _drift_entries(store, qid)
        assert len(entries) == 1
        meta = entries[0].meta or {}
        assert meta.get("entry_type") == "observation"
        assert meta.get("by") == "system"
        assert "protocol drift [st1 pw2]" in entries[0].text
        assert "engine 0.32.0 vs protocol 0.50.0" in entries[0].text
        assert "seeds 1 vs protocol 3" in entries[0].text

    def test_dedup_same_run_same_diff(self, store: Any) -> None:
        qid = _mk_quest(store)
        e = QuestProtocol(engine="0.50.0")
        a = QuestProtocol(engine="0.32.0")
        assert (
            protocol_mod.note_protocol_drift(
                store, qid, subject="st1 pw2", expected=e, actual=a
            )
            is not None
        )
        assert (
            protocol_mod.note_protocol_drift(
                store, qid, subject="st1 pw2", expected=e, actual=a
            )
            is None
        )
        assert len(_drift_entries(store, qid)) == 1

    def test_new_run_same_diff_gets_its_own_line(self, store: Any) -> None:
        """ "one logbook line whenever a run differs" — dedupe is per run."""
        qid = _mk_quest(store)
        e = QuestProtocol(engine="0.50.0")
        a = QuestProtocol(engine="0.32.0")
        protocol_mod.note_protocol_drift(
            store, qid, subject="st1 pw2", expected=e, actual=a
        )
        assert (
            protocol_mod.note_protocol_drift(
                store, qid, subject="st3 pw4", expected=e, actual=a
            )
            is not None
        )
        assert len(_drift_entries(store, qid)) == 2

    def test_no_diff_no_entry(self, store: Any) -> None:
        qid = _mk_quest(store)
        p = QuestProtocol(model="mace:medium", engine="0.50.0")
        assert (
            protocol_mod.note_protocol_drift(
                store, qid, subject="st1 pw2", expected=p, actual=p
            )
            is None
        )
        assert not _drift_entries(store, qid)


class TestDispatchDrift:
    def test_seed_override_logs(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        sid = _mk_candidate(store, qid, "cand-a")
        # A rerun's explicit one-seed set vs the neb protocol's three.
        run_config = dict(_RX, search={"seeds": [7]})
        n = protocol_mod.note_dispatch_drift(
            store, sid, run_config, tier="neb", subject="st-x pw1"
        )
        assert n is not None
        entries = _drift_entries(store, qid)
        assert len(entries) == 1
        assert "seeds 1 vs protocol 3" in entries[0].text

    def test_matching_config_is_quiet(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        sid = _mk_candidate(store, qid, "cand-a")
        assert (
            protocol_mod.note_dispatch_drift(
                store, sid, dict(_RX), tier="neb", subject="st-x pw1"
            )
            is None
        )
        assert not _drift_entries(store, qid)

    def test_no_quest_no_entry(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        ref = store.insert_ref(kind="structure", slug="lone", title="lone")
        assert (
            protocol_mod.note_dispatch_drift(
                store, int(ref.id), dict(_RX), tier="neb", subject="st-x pw1"
            )
            is None
        )


class TestHarvestDrift:
    def test_stale_engine_logs(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        pw_meta = {
            "autocatpath_version": "0.32.0",
            "config": dict(_RX),
            "requested_seeds": [0, 1, 2],
            "tier": "neb",
            "results": {},
        }
        n = protocol_mod.note_harvest_drift(
            store, qid, subject="st1 pw2", pw_meta=pw_meta
        )
        assert n is not None
        texts = _log_texts(store, qid)
        assert any(
            "protocol drift" in t and "engine 0.32.0 vs protocol 0.50.0" in t
            for t in texts
        )

    def test_parked_neb_run_logs_network_drift(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        pw_meta = {
            "autocatpath_version": "0.50.0",
            "config": dict(_RX, template="parked"),
            "tier": "neb",
            "results": {},
        }
        n = protocol_mod.note_harvest_drift(
            store, qid, subject="st1 pw2", pw_meta=pw_meta
        )
        assert n is not None
        entries = _drift_entries(store, qid)
        assert len(entries) == 1
        assert "network parked vs protocol coadsorbed" in entries[0].text

    def test_screening_parked_is_not_drift(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A screening-tier pathway on ``parked`` matches the screening
        protocol — the ladder is not drift."""
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        pw_meta = {
            "autocatpath_version": "0.50.0",
            "config": dict(_RX, template="parked", search={"screening": True}),
            "tier": "screening",
            "results": {},
        }
        assert (
            protocol_mod.note_harvest_drift(
                store, qid, subject="st1 pw2", pw_meta=pw_meta
            )
            is None
        )

    def test_no_config_no_entry(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        assert (
            protocol_mod.note_harvest_drift(
                store, qid, subject="st1 pw2", pw_meta={"tier": "neb"}
            )
            is None
        )


class TestProtocolHistogram:
    def test_counts_candidates_per_engine_and_seed(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        a = _mk_candidate(store, qid, "cand-a")
        b = _mk_candidate(store, qid, "cand-b")
        _mk_candidate(store, qid, "cand-c")  # no run — counted, not bucketed
        _mk_pathway(
            store,
            a,
            "pw-a",
            meta={
                "status": "ready",
                "tier": "neb",
                "autocatpath_version": "0.20.0",
                "config": dict(_RX, template="parked"),
                "requested_seeds": [0],
            },
        )
        _mk_pathway(
            store,
            b,
            "pw-b",
            meta={
                "status": "ready",
                "tier": "neb",
                "autocatpath_version": "0.50.0",
                "config": dict(_RX),
                "requested_seeds": [0, 1, 2],
            },
        )
        hist = protocol_mod.protocol_histogram(store, qid)
        assert hist is not None
        assert hist.candidates == 3
        assert hist.measured == 2
        assert dict(hist.engines) == {"0.20.0": 1, "0.50.0": 1}
        assert dict(hist.seeds) == {1: 1, 3: 1}
        assert dict(hist.templates) == {"parked": 1, "coadsorbed": 1}
        assert dict(hist.models) == {"mace:medium": 2}

    def test_ready_run_wins_over_newer_computing(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        cand = _mk_candidate(store, qid, "cand-a")
        _mk_pathway(
            store,
            cand,
            "pw-old",
            meta={
                "status": "ready",
                "autocatpath_version": "0.32.0",
                "config": dict(_RX),
            },
        )
        _mk_pathway(
            store,
            cand,
            "pw-new",
            meta={
                "status": "computing",
                "autocatpath_version": "0.50.0",
                "config": dict(_RX),
            },
        )
        hist = protocol_mod.protocol_histogram(store, qid)
        assert hist is not None and dict(hist.engines) == {"0.32.0": 1}

    def test_no_candidates_is_none(self, store: Any) -> None:
        qid = _mk_quest(store)
        assert protocol_mod.protocol_histogram(store, qid) is None


class TestProtocolLines:
    def test_renders_record_and_histogram(
        self, store: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_VERSION_ENV, "0.50.0")
        qid = _mk_quest(store)
        store.stamp_ref_meta(qid, {"reaction_config": dict(_RX)})
        cand = _mk_candidate(store, qid, "cand-a")
        _mk_pathway(
            store,
            cand,
            "pw-a",
            meta={
                "status": "ready",
                "autocatpath_version": "0.32.0",
                "config": dict(_RX),
                "requested_seeds": [0, 1, 2],
            },
        )
        lines = protocol_mod.protocol_lines(store, qid)
        assert any(
            ln.startswith("protocol (neb):")
            and "mace:medium" in ln
            and "0.50.0" in ln
            and "coadsorbed" in ln
            for ln in lines
        )
        assert any(
            ln.startswith("measured:")
            and "engines 0.32.0×1" in ln
            and "seeds 3×1" in ln
            for ln in lines
        )

    def test_empty_quest_prints_nothing(self, store: Any) -> None:
        qid = _mk_quest(store)
        assert protocol_mod.protocol_lines(store, qid) == []


class TestHistSortKey:
    def test_versions_sort_numerically(self) -> None:
        assert protocol_mod._hist_sort_key("0.9.0") < protocol_mod._hist_sort_key(
            "0.50.0"
        )
        assert protocol_mod._hist_sort_key(1) < protocol_mod._hist_sort_key(3)
