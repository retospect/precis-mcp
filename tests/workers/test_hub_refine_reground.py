"""Reground tests — ``precis.workers.hub_refine``'s audit/prune/re-discover
extension plus ``precis.taproot.hub``'s edge-removal door
(``docs/backlog/taproot-reground.md``).

Split from ``test_hub_refine.py`` because these exercise a different
contract: enrichment's tests assert *additive* behaviour, these assert
what happens when the pass is allowed to REMOVE. The strict judge is
always injected (``RegroundConfig.judge_fn``) — a counting/scripted fake,
so the convergence-guard acceptance criterion ("re-running an unchanged
hub is a near-no-op, no LLM re-spend") can assert call counts rather than
outcomes.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from precis.errors import BadInput
from precis.store.types import BlockInsert
from precis.taproot.canon import CanonicalClaim, claim_sha
from precis.taproot.hub import (
    META_REGROUND_LOG,
    EvidenceHandle,
    WouldStrandHub,
    attach_evidence,
    live_evidence_handles,
    mint_hub,
    reattach_as_contradicts,
    remove_evidence,
)
from precis.workers.hub_refine import (
    DEPTH_ABSTRACT_OK,
    DEPTH_BODY_REQUIRED,
    PRUNE_INTERLOCK_TOKEN,
    RegroundAdd,
    RegroundConfig,
    RegroundPlan,
    RegroundPrune,
    StrictVerdict,
    apply_reground_plan,
    claim_depth_policy,
    is_front_matter,
    judge_edge_strict,
    prune_interlock_open,
    reground_one_hub,
    repair_hub_intent,
    run_hub_refine_pass,
    verify_hub_intent,
)
from tests.workers._helpers import make_mock_bge_m3

# ── seeding helpers (mirroring test_hub_refine.py's) ─────────────────


def _seed_hub(store: Any, *, sentence: str) -> int:
    return mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))


def _seed_paper(
    store: Any, embedder: Any, *, cite_key: str, texts: list[str]
) -> tuple[int, list[int]]:
    """Mint a paper with ``len(texts)`` embedded body chunks. Returns
    ``(ref_id, [chunk_id per ord])``."""
    ref = store.insert_ref(
        kind="paper", slug=cite_key, title=f"Test paper {cite_key}", meta={}
    )
    store.blocks.insert_blocks(
        ref.id, [BlockInsert(pos=i, text=t, meta={}) for i, t in enumerate(texts)]
    )
    chunk_ids: list[int] = []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "ORDER BY ord",
            (ref.id,),
        ).fetchall()
        for chunk_id, text in rows:
            chunk_ids.append(int(chunk_id))
            conn.execute(
                "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status) "
                "VALUES (%s, %s, %s, 'ok')",
                (int(chunk_id), embedder.model, embedder.embed_one(str(text))),
            )
        conn.commit()
    return ref.id, chunk_ids


def _attach(store: Any, *, hub: int, paper: int, chunk_id: int, role: str) -> None:
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role=role,
        meta={"support": "yes", "source_handle": f"pc{chunk_id}"},
        set_by="system",
        check_retraction=False,
    )


def _handles(store: Any, hub: int) -> set[EvidenceHandle]:
    with store.pool.connection() as conn:
        return live_evidence_handles(conn, hub)


def _hub_meta(store: Any, hub: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (hub,)).fetchone()
    assert row is not None
    return dict(row[0] or {})


class _ScriptedJudge:
    """A strict judge keyed on substrings of the passage text. Counts its
    calls so convergence can be asserted as *no re-spend*, not just as an
    unchanged outcome."""

    def __init__(self, rules: list[tuple[str, str]], default: str = "KEEP") -> None:
        self.rules = rules
        self.default = default
        self.calls = 0
        self.seen_depth: list[str] = []

    def __call__(self, **kwargs: Any) -> StrictVerdict:
        self.calls += 1
        self.seen_depth.append(str(kwargs.get("depth_policy")))
        text = str(kwargs.get("chunk_text") or "")
        for needle, verdict in self.rules:
            if needle in text:
                return StrictVerdict(verdict=verdict, reason=f"scripted:{verdict}")
        return StrictVerdict(verdict=self.default, reason="scripted:default")


# ══ pure units ══════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "sentence,expected",
    [
        ("The device sustains 2.4 kV without breakdown.", DEPTH_BODY_REQUIRED),
        ("Extension doping increases the drain current.", DEPTH_BODY_REQUIRED),
        ("Pd/C catalyzes Suzuki coupling.", DEPTH_BODY_REQUIRED),
        ("A carbon nanotube is a rolled graphene sheet.", DEPTH_ABSTRACT_OK),
        ("Metal-organic frameworks exist as porous solids.", DEPTH_ABSTRACT_OK),
    ],
)
def test_grounding_depth_policy(sentence: str, expected: str) -> None:
    """Measurement/mechanism claims demand a body passage; definition /
    existence claims accept abstract-level grounding (fi189527)."""
    assert claim_depth_policy(sentence) == expected


def test_front_matter_detection() -> None:
    assert is_front_matter(chunk_ord=0, section_path=None) is True
    assert is_front_matter(chunk_ord=3, section_path="Abstract") is True
    assert is_front_matter(chunk_ord=9, section_path="Front Matter > Authors") is True
    assert is_front_matter(chunk_ord=3, section_path="Results > Transport") is False
    # ord 0 WITH a real section is a body passage, not a cover page.
    assert is_front_matter(chunk_ord=0, section_path="Results") is False


def test_reground_is_dark_by_default(monkeypatch: Any) -> None:
    """Every stage ships disabled: no env flags => no reground at all."""
    for var in (
        "PRECIS_TAPROOT_REGROUND",
        "PRECIS_TAPROOT_REGROUND_PRUNE",
        "PRECIS_TAPROOT_REGROUND_EXTERNAL",
    ):
        monkeypatch.delenv(var, raising=False)
    assert RegroundConfig.from_env() is None

    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND", "1")
    cfg = RegroundConfig.from_env()
    assert cfg is not None
    # Audit is on, but prune/external/retire all stay off behind their own
    # gates — the prune gate is the one the slice_refine_eval rubric
    # blocks.
    assert cfg.audit is True
    assert cfg.prune is False
    assert cfg.external is False
    assert cfg.authorize_retire is False

    # The prune switch is NOT a boolean: it must name its precondition, so
    # it can't be flipped from muscle memory while enabling the enrichment
    # half. Every boolean-ish value fails CLOSED.
    for truthy in ("1", "true", "yes", "on", "yeah-ok"):
        monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_PRUNE", truthy)
        cfg_off = RegroundConfig.from_env()
        assert cfg_off is not None and cfg_off.prune is False

    # Case-folded near-misses fail closed too: the token is meant to be a
    # literal, greppable string in a deploy template.
    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_PRUNE", PRUNE_INTERLOCK_TOKEN.upper())
    cfg_case = RegroundConfig.from_env()
    assert cfg_case is not None and cfg_case.prune is False

    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_PRUNE", PRUNE_INTERLOCK_TOKEN)
    cfg2 = RegroundConfig.from_env()
    assert cfg2 is not None and cfg2.prune is True
    assert prune_interlock_open() is True


def test_retire_has_no_env_flag(monkeypatch: Any) -> None:
    """The destructive stage is deliberately unreachable from the
    environment — only a job param plus the per-hub opt-in tag."""
    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND", "1")
    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_RETIRE", "1")
    monkeypatch.setenv("PRECIS_TAPROOT_REGROUND_AUTHORIZE_RETIRE", "1")
    cfg = RegroundConfig.from_env()
    assert cfg is not None and cfg.authorize_retire is False


def test_strict_judge_parses_and_rejects_junk() -> None:
    path = "precis.workers.hub_refine.dispatch"
    kwargs = dict(
        claim="X is 5 nm.",
        scope={},
        cite_key="pa1",
        chunk_ord=3,
        chunk_text="We measured 5 nm by TEM.",
    )

    class _Res:
        def __init__(self, data: Any = None, text: str = "", error: str = "") -> None:
            self.data = data
            self.text = text
            self.error = error

    with patch(path, return_value=_Res({"verdict": "PRUNE", "reason": "proxy"})):
        v = judge_edge_strict(**kwargs)  # type: ignore[arg-type]
    assert v == StrictVerdict(verdict="PRUNE", reason="proxy")

    # A verdict outside the enum is NOT a prune -- it is no verdict.
    with patch(path, return_value=_Res({"verdict": "MAYBE", "reason": "?"})):
        assert judge_edge_strict(**kwargs) is None  # type: ignore[arg-type]

    # A dead dispatch is never conflated with "this edge is a proxy".
    with patch(path, return_value=_Res(error="boom")):
        assert judge_edge_strict(**kwargs) is None  # type: ignore[arg-type]


def test_intended_end_state_is_derived_not_stored() -> None:
    plan = RegroundPlan(hub_ref_id=1, claim_sha="sha")
    plan.live_before = {
        EvidenceHandle(10, 100, "corroborates"),
        EvidenceHandle(11, 110, "corroborates"),
    }
    plan.prunes.append(
        RegroundPrune(
            src_ref_id=10, src_chunk_id=100, relation="corroborates", reason="proxy"
        )
    )
    plan.adds.append(RegroundAdd(src_ref_id=10, src_chunk_id=101))
    assert plan.intended_end_state() == {
        EvidenceHandle(11, 110, "corroborates"),
        EvidenceHandle(10, 101, "corroborates"),
    }


# ══ the removal door ════════════════════════════════════════════════


def test_remove_evidence_deletes_and_logs(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The film reaches 500 S/cm after annealing.")
    p1, c1 = _seed_paper(store, embedder, cite_key="proxy", texts=["review sentence"])
    p2, c2 = _seed_paper(store, embedder, cite_key="prim", texts=["we measured 500"])
    _attach(store, hub=hub, paper=p1, chunk_id=c1[0], role="corroborates")
    _attach(store, hub=hub, paper=p2, chunk_id=c2[0], role="corroborates")

    n = remove_evidence(
        store,
        hub_ref_id=hub,
        src_ref_id=p1,
        src_chunk_id=c1[0],
        role="corroborates",
        reason="asserts and defers to [5-24]",
        claim_sha="abc123",
        handle=f"pc{c1[0]}",
    )
    assert n == 1
    assert _handles(store, hub) == {EvidenceHandle(p2, c2[0], "corroborates")}

    entries = _hub_meta(store, hub)[META_REGROUND_LOG]
    assert len(entries) == 1
    assert entries[0]["edge"] == f"pc{c1[0]}"
    assert entries[0]["verdict"] == "PRUNE"
    assert entries[0]["reason"] == "asserts and defers to [5-24]"
    assert entries[0]["sha"] == "abc123"
    assert entries[0]["action"] == "removed"


def test_remove_evidence_refuses_to_strand_a_hub(store: Any) -> None:
    """The failure the 173020 pass actually hit: paired prunes landing
    while their adds were blocked, emptying two hubs."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Doping raises the drain current.")
    p1, c1 = _seed_paper(store, embedder, cite_key="only", texts=["the sole edge"])
    _attach(store, hub=hub, paper=p1, chunk_id=c1[0], role="corroborates")

    with pytest.raises(WouldStrandHub):
        remove_evidence(
            store,
            hub_ref_id=hub,
            src_ref_id=p1,
            src_chunk_id=c1[0],
            role="corroborates",
            reason="proxy",
        )
    assert len(_handles(store, hub)) == 1

    # ...unless the caller says so out loud (an authorized retire).
    assert (
        remove_evidence(
            store,
            hub_ref_id=hub,
            src_ref_id=p1,
            src_chunk_id=c1[0],
            role="corroborates",
            reason="authorized retire",
            allow_last=True,
        )
        == 1
    )
    assert _handles(store, hub) == set()


def test_remove_evidence_guards(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A claim about 5 nm crystals.")
    p1, c1 = _seed_paper(store, embedder, cite_key="g", texts=["t"])
    _attach(store, hub=hub, paper=p1, chunk_id=c1[0], role="corroborates")

    with pytest.raises(BadInput):
        remove_evidence(
            store,
            hub_ref_id=hub,
            src_ref_id=p1,
            role="cites",
            reason="wrong role",
        )
    with pytest.raises(BadInput):
        remove_evidence(
            store,
            hub_ref_id=p1,  # a paper, not a claim hub
            src_ref_id=p1,
            role="corroborates",
            reason="wrong target",
        )
    # A missing edge is a no-op, not a raise.
    assert (
        remove_evidence(
            store,
            hub_ref_id=hub,
            src_ref_id=p1,
            src_chunk_id=None,
            role="contradicts",
            reason="never existed",
        )
        == 0
    )


def test_contradictor_is_reattached_never_dropped(store: Any) -> None:
    """A true contradictor becomes a ``contradicts`` edge (ADR 0073) —
    even when it is the hub's ONLY edge, which is exactly when a plain
    drop would strand the hub."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The formula predicts an 85 degree angle.")
    p1, c1 = _seed_paper(store, embedder, cite_key="against", texts=["predicts 83.6"])
    _attach(store, hub=hub, paper=p1, chunk_id=c1[0], role="corroborates")

    ok = reattach_as_contradicts(
        store,
        hub_ref_id=hub,
        src_ref_id=p1,
        src_chunk_id=c1[0],
        reason="its formula predicts 83.6 degrees, not 85",
        claim_sha="sha9",
        handle=f"pc{c1[0]}",
    )
    assert ok is True
    handles = _handles(store, hub)
    assert {h.relation for h in handles} == {"contradicts"}
    entries = _hub_meta(store, hub)[META_REGROUND_LOG]
    assert entries[-1]["verdict"] == "CONTRADICTS"
    assert entries[-1]["action"].startswith("reattached-contradicts")


# ══ the applier: add-first, in code ═════════════════════════════════


def test_applier_releases_a_prune_only_behind_a_confirmed_add(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(
        store, sentence="Extension doping raises drain current to 13.95 uA."
    )
    paper, chunks = _seed_paper(
        store,
        embedder,
        cite_key="pa36266",
        texts=["front matter / abstract proxy", "drain current from 5.09 to 13.95 uA"],
    )
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")
    # The replacement add, committed the way _refine_one_hub would.
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[1], role="corroborates")

    plan = RegroundPlan(hub_ref_id=hub, claim_sha="sha1")
    plan.live_before = _handles(store, hub)
    plan.adds.append(RegroundAdd(src_ref_id=paper, src_chunk_id=chunks[1]))
    plan.prunes.append(
        RegroundPrune(
            src_ref_id=paper,
            src_chunk_id=chunks[0],
            relation="corroborates",
            reason="abstract-for-a-measurement",
        )
    )

    res = apply_reground_plan(store, plan)
    assert res.confirmed_adds == 1
    assert res.missing_adds == 0
    assert res.pruned == 1
    assert res.withheld == 0
    assert res.clean is True
    assert _handles(store, hub) == {EvidenceHandle(paper, chunks[1], "corroborates")}


def test_applier_withholds_the_prune_when_the_add_never_committed(store: Any) -> None:
    """The exact 173020 partial failure, reproduced: the plan believes it
    added a replacement, the table disagrees. The prune must be withheld
    AND surfaced, never silently skipped."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(
        store, sentence="Extension doping raises drain current to 13.95 uA."
    )
    paper, chunks = _seed_paper(
        store, embedder, cite_key="pa36266", texts=["proxy", "primary"]
    )
    other, other_chunks = _seed_paper(store, embedder, cite_key="other", texts=["x"])
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")
    _attach(store, hub=hub, paper=other, chunk_id=other_chunks[0], role="corroborates")

    plan = RegroundPlan(hub_ref_id=hub, claim_sha="sha1")
    plan.live_before = _handles(store, hub)
    # Claimed but never written (the permission classifier blocked it).
    plan.adds.append(RegroundAdd(src_ref_id=paper, src_chunk_id=chunks[1]))
    plan.prunes.append(
        RegroundPrune(
            src_ref_id=paper,
            src_chunk_id=chunks[0],
            relation="corroborates",
            reason="proxy",
        )
    )

    res = apply_reground_plan(store, plan)
    assert res.missing_adds == 1
    assert res.pruned == 0
    assert res.withheld == 1
    assert res.clean is False
    assert any(f.startswith("add-not-committed") for f in res.flags)
    assert any("prune-withheld-no-confirmed-add" in f for f in res.flags)
    # The proxy edge is still there — nothing was lost.
    assert EvidenceHandle(paper, chunks[0], "corroborates") in _handles(store, hub)
    # ...and the withholding is on the audit trail.
    assert any(
        "withheld" in e["action"] for e in _hub_meta(store, hub)[META_REGROUND_LOG]
    )


def test_prune_pairing_is_one_to_one_not_any_add_releases_all(store: Any) -> None:
    """Two proxy edges, one replacement. Only the prune whose replacement
    actually landed may go — the other has nothing to put in its place.

    The quiet version of the 173020 damage: an "any confirmed add releases
    every prune" rule deletes the second paper's evidence for free and
    never strands the hub, so nothing ever complains."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The film reaches 500 S/cm after annealing.")
    paper_a, a_chunks = _seed_paper(
        store, embedder, cite_key="paA", texts=["A proxy", "A primary"]
    )
    paper_b, b_chunks = _seed_paper(store, embedder, cite_key="paB", texts=["B proxy"])
    _attach(store, hub=hub, paper=paper_a, chunk_id=a_chunks[0], role="corroborates")
    _attach(store, hub=hub, paper=paper_b, chunk_id=b_chunks[0], role="corroborates")
    # Only paper A got a deeper replacement (paper B has no deeper passage).
    _attach(store, hub=hub, paper=paper_a, chunk_id=a_chunks[1], role="corroborates")

    plan = RegroundPlan(hub_ref_id=hub, claim_sha="sha1")
    plan.live_before = _handles(store, hub)
    plan.adds.append(RegroundAdd(src_ref_id=paper_a, src_chunk_id=a_chunks[1]))
    plan.prunes.extend(
        [
            RegroundPrune(
                src_ref_id=paper_a,
                src_chunk_id=a_chunks[0],
                relation="corroborates",
                reason="A is a proxy",
            ),
            RegroundPrune(
                src_ref_id=paper_b,
                src_chunk_id=b_chunks[0],
                relation="corroborates",
                reason="B is a proxy",
            ),
        ]
    )

    res = apply_reground_plan(store, plan)
    assert res.pruned == 1
    assert res.withheld == 1
    assert res.clean is False
    assert any(f"prune-withheld-no-confirmed-add:{paper_b}" == f for f in res.flags)
    handles = _handles(store, hub)
    # A's proxy is gone (replaced); B's edge survives untouched.
    assert EvidenceHandle(paper_a, a_chunks[0], "corroborates") not in handles
    assert EvidenceHandle(paper_a, a_chunks[1], "corroborates") in handles
    assert EvidenceHandle(paper_b, b_chunks[0], "corroborates") in handles


def test_cross_paper_swap_consumes_a_leftover_add(store: Any) -> None:
    """The rarer tail (fi191169's wrong-paper swap): a prune with no
    same-source add still releases against an unclaimed add from another
    paper — one add, one prune."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Touch modules ship with 500 S/cm films.")
    wrong, wrong_chunks = _seed_paper(store, embedder, cite_key="wrong", texts=["off"])
    right, right_chunks = _seed_paper(store, embedder, cite_key="right", texts=["on"])
    _attach(store, hub=hub, paper=wrong, chunk_id=wrong_chunks[0], role="corroborates")
    _attach(store, hub=hub, paper=right, chunk_id=right_chunks[0], role="corroborates")

    plan = RegroundPlan(hub_ref_id=hub, claim_sha="sha1")
    plan.live_before = _handles(store, hub)
    plan.adds.append(RegroundAdd(src_ref_id=right, src_chunk_id=right_chunks[0]))
    plan.prunes.append(
        RegroundPrune(
            src_ref_id=wrong,
            src_chunk_id=wrong_chunks[0],
            relation="corroborates",
            reason="wrong paper entirely",
        )
    )

    res = apply_reground_plan(store, plan)
    assert res.pruned == 1
    assert res.withheld == 0
    assert _handles(store, hub) == {
        EvidenceHandle(right, right_chunks[0], "corroborates")
    }


def test_applier_refuses_a_plan_that_would_strand_the_hub(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(
        store, sentence="A vacuous compound capability claim about 3 things."
    )
    paper, chunks = _seed_paper(store, embedder, cite_key="sole", texts=["proxy"])
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    plan = RegroundPlan(hub_ref_id=hub, claim_sha="sha1")
    plan.live_before = _handles(store, hub)
    plan.prunes.append(
        RegroundPrune(
            src_ref_id=paper,
            src_chunk_id=chunks[0],
            relation="corroborates",
            reason="unsupportable as worded",
            requires_replacement=False,
        )
    )

    res = apply_reground_plan(store, plan)
    assert res.stranded_refused is True
    assert res.pruned == 0
    assert res.withheld == 1
    assert len(_handles(store, hub)) == 1


def test_intent_diff_finds_residue_and_repairs_adds_first(store: Any) -> None:
    """The technique that found the original damage, as a mode: rebuild
    the intended end state, diff against committed, apply the delta."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The film reaches 500 S/cm after annealing.")
    paper, chunks = _seed_paper(
        store, embedder, cite_key="pa1", texts=["proxy", "primary"]
    )
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[1], role="corroborates")

    plan = RegroundPlan(hub_ref_id=hub, claim_sha="sha1")
    plan.live_before = _handles(store, hub)
    plan.adds.append(RegroundAdd(src_ref_id=paper, src_chunk_id=chunks[1]))
    plan.prunes.append(
        RegroundPrune(
            src_ref_id=paper,
            src_chunk_id=chunks[0],
            relation="corroborates",
            reason="proxy",
        )
    )
    apply_reground_plan(store, plan)
    assert verify_hub_intent(store, hub).clean is True

    # Somebody (a stale sibling pass, a hand edit) drops the primary and
    # restores the proxy behind the applier's back.
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM links WHERE dst_ref_id = %s AND src_chunk_id = %s",
            (hub, chunks[1]),
        )
        conn.commit()
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    diff = verify_hub_intent(store, hub)
    assert diff.clean is False
    assert [h.src_chunk_id for h in diff.missing_adds] == [chunks[1]]
    assert [h.src_chunk_id for h in diff.stale_edges] == [chunks[0]]

    residual = repair_hub_intent(store, hub, apply=True)
    assert residual.clean is True
    assert _handles(store, hub) == {EvidenceHandle(paper, chunks[1], "corroborates")}


def test_verify_mode_reports_no_stored_intent(store: Any) -> None:
    hub = _seed_hub(store, sentence="Never regrounded.")
    diff = verify_hub_intent(store, hub)
    assert diff.has_intent is False
    assert diff.clean is True


# ══ the pass end-to-end ═════════════════════════════════════════════


def test_same_paper_depth_correction_is_the_primary_move(store: Any) -> None:
    """fi192855 in miniature: the hub is grounded on a proxy passage while
    the real primary sits deeper in the SAME already-linked paper. Audit
    flags the proxy, re-discovery attaches the primary, the applier
    releases the prune behind the confirmed add, and both land in the
    log with reasons."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(
        store, sentence="Extension p-doping raises CNT FET drain current to 13.95 uA."
    )
    paper, chunks = _seed_paper(
        store,
        embedder,
        cite_key="pa36266",
        texts=[
            "PROXY: carbon nanomaterials can be adjusted by doping [5-24].",
            "PRIMARY: drain current rose from 5.09 to 13.95 uA via extension doping.",
        ],
    )
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    judge = _ScriptedJudge([("PROXY", "PRUNE"), ("PRIMARY", "KEEP")])
    cfg = RegroundConfig(prune=True, judge_fn=judge, deeper_topk=8)

    res = reground_one_hub(store, hub, embedder=embedder, cfg=cfg)
    assert res.confirmed_adds == 1
    assert res.pruned == 1
    assert res.clean is True
    assert _handles(store, hub) == {EvidenceHandle(paper, chunks[1], "corroborates")}

    # The claim carries a number, so the judge was told to demand a body
    # passage.
    assert set(judge.seen_depth) == {DEPTH_BODY_REQUIRED}

    log_entries = _hub_meta(store, hub)[META_REGROUND_LOG]
    actions = {e["action"] for e in log_entries}
    assert "added" in actions and "removed" in actions
    assert all(e["reason"] for e in log_entries)


def test_depth_policy_refuses_front_matter_when_depth_exists(store: Any) -> None:
    """A measurement claim will not re-ground onto a cover/abstract chunk
    while the same paper has a body passage — even when the judge says
    KEEP. (A single-body-chunk source is exempt: there is no depth to
    correct to, so refusing would drop a real supporter for nothing —
    exercised by the external-DOI test below.)"""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Doping raises the drain current to 13.95 uA.")
    paper, chunks = _seed_paper(
        store,
        embedder,
        cite_key="pa1",
        texts=["COVER PAGE: Authors, Affiliations", "PRIMARY: 13.95 uA measured"],
    )
    # Already grounded on the body passage; only the cover chunk is left
    # for discovery to offer.
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[1], role="corroborates")

    judge = _ScriptedJudge([], default="KEEP")
    cfg = RegroundConfig(prune=True, judge_fn=judge, deeper_topk=8)
    res = reground_one_hub(store, hub, embedder=embedder, cfg=cfg)

    assert res.confirmed_adds == 0
    assert _handles(store, hub) == {EvidenceHandle(paper, chunks[1], "corroborates")}
    log_entries = _hub_meta(store, hub)[META_REGROUND_LOG]
    assert any("depth policy" in e["action"] for e in log_entries)


def test_prune_stage_is_gated_off_by_default(store: Any) -> None:
    """With the prune gate closed (the shipping default, pending the
    ``slice_refine_eval`` rubric gate) a PRUNE verdict is recorded as a
    withheld proposal and the edge survives."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Doping raises the drain current to 13.95 uA.")
    paper, chunks = _seed_paper(
        store, embedder, cite_key="pa1", texts=["PROXY passage", "PRIMARY passage"]
    )
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    judge = _ScriptedJudge([("PROXY", "PRUNE"), ("PRIMARY", "KEEP")])
    cfg = RegroundConfig(prune=False, judge_fn=judge, deeper_topk=8)
    res = reground_one_hub(store, hub, embedder=embedder, cfg=cfg)

    assert res.pruned == 0
    assert EvidenceHandle(paper, chunks[0], "corroborates") in _handles(store, hub)
    log_entries = _hub_meta(store, hub)[META_REGROUND_LOG]
    assert any("prune stage disabled" in e["action"] for e in log_entries)


def test_rerunning_an_unchanged_hub_costs_no_llm(store: Any) -> None:
    """Convergence guard: an edge is judged at most once per claim_sha, and
    a sha-reopen (an edited claim) clears the memo."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Annealed films reach 500 S/cm.")
    paper, chunks = _seed_paper(store, embedder, cite_key="pa1", texts=["a passage"])
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    judge = _ScriptedJudge([], default="KEEP")
    cfg = RegroundConfig(judge_fn=judge, deeper_topk=8)

    reground_one_hub(store, hub, embedder=embedder, cfg=cfg)
    first = judge.calls
    assert first >= 1
    assert _hub_meta(store, hub)["reground_seen"]

    reground_one_hub(store, hub, embedder=embedder, cfg=cfg)
    assert judge.calls == first  # memo hit: no re-spend

    # Rewording the claim reopens every verdict.
    store.update_ref(hub, title="Annealed films reach 900 S/cm.")
    reground_one_hub(store, hub, embedder=embedder, cfg=cfg)
    assert judge.calls > first
    seen = _hub_meta(store, hub)["reground_seen"]
    assert all(
        v["sha"] == claim_sha("Annealed films reach 900 S/cm.") for v in seen.values()
    )


def test_contradicting_edge_is_converted_not_deleted_end_to_end(store: Any) -> None:
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The opening angle is 85 degrees.")
    paper, chunks = _seed_paper(
        store, embedder, cite_key="pa1", texts=["AGAINST: the formula gives 83.6"]
    )
    other, other_chunks = _seed_paper(
        store, embedder, cite_key="pa2", texts=["neutral"]
    )
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")
    _attach(store, hub=hub, paper=other, chunk_id=other_chunks[0], role="corroborates")

    judge = _ScriptedJudge([("AGAINST", "CONTRADICTS")], default="KEEP")
    cfg = RegroundConfig(prune=True, judge_fn=judge, deeper_topk=8)
    res = reground_one_hub(store, hub, embedder=embedder, cfg=cfg)

    assert res.contradicts_reattached == 1
    handles = _handles(store, hub)
    relations = {(h.src_ref_id, h.relation) for h in handles}
    assert (paper, "contradicts") in relations
    assert (paper, "corroborates") not in relations
    # The converted edge keeps its grounding passage, so the stored
    # intent matches what actually committed (no phantom drift).
    assert EvidenceHandle(paper, chunks[0], "contradicts") in handles
    assert verify_hub_intent(store, hub).clean is True


def test_zero_supporters_needs_no_schema_change(store: Any) -> None:
    """A hub stripped to zero supporters is expressible without any new
    trust/questionable state — the removal door allows it only under an
    explicit ``allow_last``, and nothing else is written."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="An unsupportable compound claim about 3 things.")
    paper, chunks = _seed_paper(store, embedder, cite_key="pa1", texts=["proxy"])
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    remove_evidence(
        store,
        hub_ref_id=hub,
        src_ref_id=paper,
        src_chunk_id=chunks[0],
        role="corroborates",
        reason="authorized retire",
        allow_last=True,
    )
    meta = _hub_meta(store, hub)
    assert _handles(store, hub) == set()
    assert "questionable" not in meta
    assert "trust" not in meta


def test_retire_needs_both_gates(store: Any) -> None:
    """The destructive stage is double-gated: the run-level
    ``authorize_retire`` param AND a per-hub opt-in tag. Neither alone
    fires it, and even when it does fire it only *flags* — no prose edit,
    no deletion (the deliberately stubbed half)."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="A vacuous compound capability claim, 3 ways.")
    paper, chunks = _seed_paper(store, embedder, cite_key="pa1", texts=["PROXY only"])
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")
    judge = _ScriptedJudge([("PROXY", "PRUNE")])

    # 1. prune on, external on, but NO authorization -> verdict recorded,
    #    nothing tagged, nothing removed (the prune would strand).
    cfg = RegroundConfig(
        prune=True,
        external=True,
        judge_fn=judge,
        deeper_topk=8,
        external_probe_fn=(lambda _q: []),
    )
    res = reground_one_hub(store, hub, embedder=embedder, cfg=cfg)
    # The lone proxy prune is withheld twice over: no confirmed
    # replacement add (the first guard), and it would strand the hub (the
    # second). Defence in depth — the first one fires, so the second
    # never has to.
    assert res.pruned == 0
    assert res.withheld == 1
    assert any("prune-withheld-no-confirmed-add" in f for f in res.flags)
    meta = _hub_meta(store, hub)
    assert meta["reground_verdict"]["verdict"] == "retire"
    assert "retire-flagged" not in res.flags
    assert len(_handles(store, hub)) == 1

    # 2. authorize_retire set but the hub carries no opt-in tag -> still
    #    not authorized.
    store.update_ref(hub, title="A vacuous compound capability claim, 4 ways.")
    cfg2 = RegroundConfig(
        prune=True,
        external=True,
        authorize_retire=True,
        judge_fn=judge,
        deeper_topk=8,
        external_probe_fn=lambda _q: [],
    )
    res2 = reground_one_hub(store, hub, embedder=embedder, cfg=cfg2)
    assert "retire-flagged" not in res2.flags
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'TAPROOT_REGROUND_RETIRE'",
            (hub,),
        ).fetchone()
    assert row is None

    # 3. both gates open -> flagged for the prose pass, still not deleted.
    from precis.store.types import Tag

    store.add_tag(hub, Tag.closed("TAPROOT_REGROUND_OK", "1"), set_by="system")
    store.update_ref(hub, title="A vacuous compound capability claim, 5 ways.")
    res3 = reground_one_hub(store, hub, embedder=embedder, cfg=cfg2)
    assert "retire-flagged" in res3.flags
    verdict = _hub_meta(store, hub)["reground_verdict"]
    assert verdict["verdict"] == "retire"
    assert verdict["prose_pass"].startswith("pending")
    # Flagged, never rewritten and never emptied.
    assert len(_handles(store, hub)) == 1


def test_external_stage_mines_reference_dois(store: Any) -> None:
    """Stage 5 mines the grounding papers' own bibliographies: a DOI we
    already hold becomes an ordinary candidate; one we don't is reported,
    never auto-acquired."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="Extension doping raises drain current 13.95 uA.")
    proxy, proxy_chunks = _seed_paper(
        store, embedder, cite_key="proxy", texts=["PROXY review sentence [5-24]"]
    )
    primary, primary_chunks = _seed_paper(
        store, embedder, cite_key="primary", texts=["PRIMARY measured 13.95 uA"]
    )
    _attach(store, hub=hub, paper=proxy, chunk_id=proxy_chunks[0], role="corroborates")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO paper_bib_entries "
            "(ref_id, marker, raw_text, doi, held_ref_id, parse_version) "
            "VALUES (%s, 5, 'Krishnan et al.', '10.1000/held', %s, 1), "
            "       (%s, 6, 'Paywalled et al.', '10.1038/41284', NULL, 1)",
            (proxy, primary, proxy),
        )
        conn.commit()

    judge = _ScriptedJudge([("PROXY", "PRUNE"), ("PRIMARY", "KEEP")])
    # Force the external leg: no corpus-wide candidates by making the
    # semantic legs return nothing.
    cfg = RegroundConfig(
        prune=True,
        external=True,
        judge_fn=judge,
        deeper_topk=8,
        external_probe_fn=lambda _q: [],
    )
    with patch.object(store.blocks, "search_blocks", side_effect=_only_scoped(store)):
        res = reground_one_hub(store, hub, embedder=embedder, cfg=cfg)

    assert res.confirmed_adds == 1
    assert EvidenceHandle(primary, primary_chunks[0], "corroborates") in _handles(
        store, hub
    )
    report = _hub_meta(store, hub)["reground_external"]
    assert {d["doi"] for d in report["unheld_dois"]} == {"10.1038/41284"}


def _only_scoped(store: Any) -> Any:
    """A ``search_blocks`` wrapper that answers only *scoped* (per-paper)
    searches, so the corpus-wide discovery legs come back empty and the
    external last resort is the only source left."""
    real = store.blocks.__class__.search_blocks
    blocks = store.blocks

    def _wrapped(**kwargs: Any) -> Any:
        if kwargs.get("scope_ref_id") is None:
            return []
        return real(blocks, **kwargs)

    return _wrapped


def test_additive_only_behaviour_is_unchanged_when_reground_is_off(store: Any) -> None:
    """Regression guard on the DRY invariant: with reground off, the pass
    is byte-for-byte the additive enrichment it was — an already-attached
    source is skipped outright and never re-judged."""
    embedder = make_mock_bge_m3()
    hub = _seed_hub(store, sentence="The film reaches 500 S/cm.")
    paper, chunks = _seed_paper(
        store, embedder, cite_key="pa1", texts=["proxy", "primary"]
    )
    _attach(store, hub=hub, paper=paper, chunk_id=chunks[0], role="corroborates")

    with patch("precis.workers.hub_refine._verify_support_with_caveats") as mock_verify:
        result = run_hub_refine_pass(
            store, limit=10, embedder=embedder, topk=8, reground=None
        )
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    # The attached source's other passage was never even offered.
    assert mock_verify.call_count == 0
    assert _handles(store, hub) == {EvidenceHandle(paper, chunks[0], "corroborates")}
