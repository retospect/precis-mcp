"""``put(kind='structure', mode='trust')`` — the audited trust-stamp door (gr592893).

The quest harvest stamps bool trust verdicts (``barrier_trusted`` and its
selectivity/kinetics siblings) onto a candidate structure's ``refs.meta``;
the frontier re-reads them every pass. Before this door, no MCP write path
could correct a wrong stamp: ``edit`` takes geometry/bond ops only, ``tag``
cannot set meta, and an unrestricted meta write is refused at the dispatch
gate — leaving prod SQL (denied to sessions) or a not-yet-existing repair
job as the only routes.

The door corrects ONE ``*_trusted`` key per call with a required reason and
optional evidence via ``store.trust_stamp_correct`` — a row-locked
read/append/stamp in one transaction (td625122 review of td625121: a
handler-side read + ``stamp_ref_meta`` merge let concurrent corrections
overwrite ``trust_stamp_log`` from stale snapshots). Stamping a previously
unset key records ``from: None`` and is reversible through the audited
unset (``"key": None`` removes the key, recording ``to: None``; unsetting
an unstamped key refuses). The receipt reports quest-logbook append
failures by name instead of folding them into "no serving quest linked".
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.structure import StructureHandler

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _ref_id(store, slug="pd_pair") -> int:
    return store.get_ref(kind="structure", id=slug).id


def _meta(store, ref_id) -> dict:
    return dict(store.fetch_refs_by_ids({ref_id})[ref_id].meta or {})


def test_trust_stamp_corrects_key_and_writes_audit_log(structure, store):
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    # A harvest pass stamped a trusted barrier that is about to be corrected.
    store.stamp_ref_meta(ref_id, {"barrier_trusted": True})

    resp = structure.put(
        id="pd_pair",
        mode="trust",
        args={
            "stamp": {
                "barrier_trusted": False,
                "reason": "pathway gate says BLOCKED",
                "evidence": "pw339066",
            }
        },
    )

    meta = _meta(store, ref_id)
    assert meta["barrier_trusted"] is False
    (entry,) = meta["trust_stamp_log"]
    assert entry["key"] == "barrier_trusted"
    assert entry["from"] is True and entry["to"] is False
    assert entry["reason"] == "pathway gate says BLOCKED"
    assert entry["evidence"] == "pw339066"
    assert entry["at"].endswith("Z")

    assert "trust-stamp:" in resp.body
    assert "barrier_trusted: True -> False" in resp.body
    assert "st" + str(ref_id) in resp.body  # the printed handle
    assert "quest logbook: no serving quest linked" in resp.body
    assert "reverse: put(kind='structure'" in resp.body
    assert "'barrier_trusted': True" in resp.body  # the exact reverse call


def test_trust_stamp_leaves_geometry_and_version_untouched(structure, store):
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    version_before = store.structure_version(ref_id)

    structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"barrier_trusted": False, "reason": "wrong stamp"}},
    )

    assert store.structure_version(ref_id) == version_before
    # The scene is untouched: the TOC still shows the authored pair.
    toc = structure.get(id="pd_pair")
    assert "Pd2" in toc.body and "1 bonds" in toc.body


def test_trust_stamp_frontier_rereads_corrected_value(structure, store):
    from precis.quest.frontier import _candidate_from_structure

    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    store.stamp_ref_meta(ref_id, {"barrier_trusted": True, "barrier": 0.9})

    trusted = _candidate_from_structure(
        store, store.get_ref(kind="structure", id="pd_pair")
    )
    assert trusted.flags["barrier_trusted"] is True

    structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"barrier_trusted": False, "reason": "gate BLOCKED"}},
    )

    corrected = _candidate_from_structure(
        store, store.get_ref(kind="structure", id="pd_pair")
    )
    assert corrected.flags["barrier_trusted"] is False
    # The correction log itself never ranks as a measure.
    assert "trust_stamp_log" not in corrected.measures


def test_trust_stamp_appends_decision_entry_to_serving_quest_logbook(structure, store):
    from tests.workers._helpers import seed_ref

    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    quest_id = seed_ref(store, kind="quest", title="A NO->NH3 catalyst")
    store.add_link(src_ref_id=ref_id, dst_ref_id=quest_id, relation="serves")

    resp = structure.put(
        id="pd_pair",
        mode="trust",
        args={
            "stamp": {
                "barrier_trusted": False,
                "reason": "pathway gate says BLOCKED",
                "evidence": "pw339066",
            }
        },
    )

    chunks = store.chunks.list_chunks_for_ref(quest_id)
    (chunk,) = chunks
    assert "trust-stamp correction" in chunk.text
    assert "barrier_trusted" in chunk.text
    assert "pathway gate says BLOCKED" in chunk.text
    assert "pw339066" in chunk.text
    assert chunk.meta["entry_type"] == "decision"
    assert chunk.meta["by"] == "agent"
    # The ack names the logbook block handle (entry_no 1 -> ~0).
    assert f"qu{quest_id}~0" in resp.body


def test_trust_stamp_is_reversible(structure, store):
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    store.stamp_ref_meta(ref_id, {"barrier_trusted": True})

    structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"barrier_trusted": False, "reason": "wrong stamp"}},
    )
    structure.put(
        id="pd_pair",
        mode="trust",
        args={
            "stamp": {"barrier_trusted": True, "reason": "verified against pw339066"}
        },
    )

    meta = _meta(store, ref_id)
    assert meta["barrier_trusted"] is True
    first, second = meta["trust_stamp_log"]
    assert (first["from"], first["to"]) == (True, False)
    assert (second["from"], second["to"]) == (False, True)


def test_trust_stamp_on_unstamped_key_records_from_none(structure, store):
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)

    resp = structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"kinetics_trusted": False, "reason": "solve exploded"}},
    )

    meta = _meta(store, ref_id)
    assert meta["kinetics_trusted"] is False
    (entry,) = meta["trust_stamp_log"]
    assert entry["from"] is None and entry["to"] is False
    assert entry["evidence"] is None
    # The reverse of stamping a previously unset key is the audited unset.
    assert "'kinetics_trusted': None" in resp.body
    assert "unsets the key" in resp.body


# ── audited unset ────────────────────────────────────────────────────


def test_trust_stamp_unset_removes_key_and_audits(structure, store):
    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    store.stamp_ref_meta(ref_id, {"barrier_trusted": True})

    resp = structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"barrier_trusted": None, "reason": "harvest misfire"}},
    )

    meta = _meta(store, ref_id)
    assert "barrier_trusted" not in meta
    (entry,) = meta["trust_stamp_log"]
    assert entry["from"] is True and entry["to"] is None
    assert entry["reason"] == "harvest misfire"
    assert "-> unset" in resp.body
    # The reverse of an unset is re-stamping the removed value.
    assert "'barrier_trusted': True" in resp.body
    assert "unsets the key" not in resp.body


def test_trust_stamp_unset_on_unstamped_key_refuses(structure, store):
    structure.put(id="pd_pair", text=_PD)

    with pytest.raises(BadInput, match="not stamped"):
        structure.put(
            id="pd_pair",
            mode="trust",
            args={"stamp": {"barrier_trusted": None, "reason": "x"}},
        )

    meta = _meta(store, _ref_id(store))
    assert "barrier_trusted" not in meta
    assert "trust_stamp_log" not in meta


def test_trust_stamp_unset_frontier_rereads_absence(structure, store):
    from precis.quest.frontier import _candidate_from_structure

    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    store.stamp_ref_meta(ref_id, {"barrier_trusted": True})

    structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"barrier_trusted": None, "reason": "harvest misfire"}},
    )

    candidate = _candidate_from_structure(
        store, store.get_ref(kind="structure", id="pd_pair")
    )
    assert "barrier_trusted" not in candidate.flags


# ── atomicity + logbook failure reporting (td625122 review) ──────────


def test_trust_stamp_concurrent_corrections_both_audit(structure, store):
    """Two overlapping corrections must serialize: both audit entries
    survive and the second records the first's result as its ``from`` —
    the stale-snapshot overwrite this regression test guards. The stored
    ``trust_stamp_log`` order IS the serialization order: the append
    happens under ``SELECT … FOR UPDATE`` while each entry's ``at`` is
    minted in the handler before the lock, so re-sorting by ``at`` can
    reverse a valid audit (gr627152)."""
    import threading

    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    store.stamp_ref_meta(ref_id, {"barrier_trusted": True})

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def correct(new_value: bool) -> None:
        try:
            barrier.wait(timeout=30)
            structure.put(
                id="pd_pair",
                mode="trust",
                args={"stamp": {"barrier_trusted": new_value, "reason": "race"}},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=correct, args=(v,)) for v in (False, True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not any(t.is_alive() for t in threads)
    assert not errors

    meta = _meta(store, ref_id)
    first, second = meta["trust_stamp_log"]
    assert first["from"] is True
    assert second["from"] == first["to"]
    assert meta["barrier_trusted"] == second["to"]


def test_trust_stamp_logbook_failure_named_not_misreported(
    structure, store, monkeypatch
):
    from tests.workers._helpers import seed_ref

    structure.put(id="pd_pair", text=_PD)
    ref_id = _ref_id(store)
    quest_id = seed_ref(store, kind="quest", title="A NO->NH3 catalyst")
    store.add_link(src_ref_id=ref_id, dst_ref_id=quest_id, relation="serves")

    def _boom(*_a, **_kw):
        raise RuntimeError("logbook down")

    monkeypatch.setattr("precis.quest.logbook.append_entry", _boom)

    resp = structure.put(
        id="pd_pair",
        mode="trust",
        args={"stamp": {"barrier_trusted": False, "reason": "wrong stamp"}},
    )

    # The failure is named, not folded into "no serving quest linked"…
    assert f"append FAILED for qu{quest_id}" in resp.body
    assert "no serving quest linked" not in resp.body
    # …and the structure audit still landed.
    meta = _meta(store, ref_id)
    assert meta["barrier_trusted"] is False
    assert len(meta["trust_stamp_log"]) == 1


# ── refusals ─────────────────────────────────────────────────────────


def test_trust_stamp_requires_id(structure):
    with pytest.raises(BadInput, match="requires id="):
        structure.put(
            id=None,
            mode="trust",
            args={"stamp": {"barrier_trusted": False, "reason": "x"}},
        )


def test_trust_stamp_unknown_structure_refuses(structure):
    with pytest.raises(NotFound):
        structure.put(
            id="nope",
            mode="trust",
            args={"stamp": {"barrier_trusted": False, "reason": "x"}},
        )


def test_trust_stamp_requires_stamp_payload(structure, store):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput, match="args=\\{'stamp'"):
        structure.put(id="pd_pair", mode="trust", args={})


def test_trust_stamp_unknown_payload_key_refused(structure, store):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput, match="not accepted"):
        structure.put(
            id="pd_pair",
            mode="trust",
            args={"stamp": {"barrier": 1.2, "reason": "x"}},
        )


def test_trust_stamp_non_bool_value_refused(structure, store):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput, match="bool trust verdict"):
        structure.put(
            id="pd_pair",
            mode="trust",
            args={"stamp": {"barrier_trusted": 1, "reason": "x"}},
        )


def test_trust_stamp_two_keys_per_call_refused(structure, store):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput, match="one trust-stamp key per call"):
        structure.put(
            id="pd_pair",
            mode="trust",
            args={
                "stamp": {
                    "barrier_trusted": False,
                    "kinetics_trusted": False,
                    "reason": "x",
                }
            },
        )


def test_trust_stamp_without_key_refused(structure, store):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput, match="names one trust-stamp key"):
        structure.put(
            id="pd_pair",
            mode="trust",
            args={"stamp": {"reason": "x"}},
        )


def test_trust_stamp_reason_required(structure, store):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput, match="give a reason"):
        structure.put(
            id="pd_pair",
            mode="trust",
            args={"stamp": {"barrier_trusted": False}},
        )
