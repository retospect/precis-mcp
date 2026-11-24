"""Finding-backed dialectic operations and legacy experiment block readers."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.dossier import (
    apply_dialectic_op,
    dossier_ref_id,
    read_dialectic,
    read_dossier,
    read_narrative,
    rewrite_dossier,
)
from precis.quest.tick import run_quest_tick
from precis.store import Store, Tag
from tests.workers._helpers import seed_ref


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _mk_finding(store: Any, title: str = "a hypothesis finding") -> int:
    return seed_ref(store, title=title, kind="finding")


def _fake_dispatch(payload: dict[str, Any] | None = None, **kw: Any) -> Any:
    def _d(_req: Any) -> Any:
        return SimpleNamespace(
            data=payload, text="", error=None, cost_usd=0.01, paused=False, **kw
        )

    return _d


def _links(store: Any, dst_ref_id: int, relation: str) -> list[tuple[int, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE dst_ref_id = %s AND relation = %s",
            (dst_ref_id, relation),
        ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


class TestOpen:
    def test_open_creates_a_readable_block(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store, title="Fe adatom lowers the barrier")
        assert (
            apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
            is True
        )
        rendered = read_dialectic(store, qid)
        assert f"[fi{fid}] Fe adatom lowers the barrier" in rendered

    def test_second_open_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        assert (
            apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
            is True
        )
        assert (
            apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
            is False
        )


def _mk_served_hypothesis(store: Store, qid: int) -> int:
    fid = _mk_finding(store)
    store.stamp_ref_meta(fid, {"artifact_type": "hypothesis"})
    store.add_link(src_ref_id=fid, dst_ref_id=qid, relation="serves")
    return fid


def test_support_and_counter_write_finding_edges_without_dossier(store: Store) -> None:
    qid = _mk_quest(store, "A striving")
    fid = _mk_served_hypothesis(store, qid)
    paper = seed_ref(store, kind="paper")
    text = f"[pa{paper}] measured the same effect"
    for op, relation in (("support", "supports"), ("counter", "disputes")):
        payload = {"op": op, "hypothesis": f"fi{fid}", "text": text}
        assert apply_dialectic_op(store, qid, payload)
        assert _links(store, fid, relation) == [(paper, fid)]
        assert not apply_dialectic_op(store, qid, payload)
        for invalid in ("unanchored", "[pa999999999]", f"[fi{fid}]", f"[st{paper}]"):
            assert not apply_dialectic_op(store, qid, {**payload, "text": invalid})
    assert dossier_ref_id(store, qid) is None


def test_settle_refuted_sets_status_and_ruling_link(store: Store) -> None:
    qid = _mk_quest(store, "A striving")
    fid = _mk_served_hypothesis(store, qid)
    ruling = _mk_finding(store, title="Negative ruling")
    payload = {
        "op": "settle",
        "hypothesis": f"fi{fid}",
        "verdict": "refuted",
        "ruling": f"fi{ruling}",
        "text": f"[fi{ruling}] ruled out the effect",
    }
    assert not apply_dialectic_op(store, qid, {**payload, "ruling": f"fi{fid}"})
    assert apply_dialectic_op(store, qid, payload)
    assert not apply_dialectic_op(store, qid, payload)
    assert Tag.closed("STATUS", "refuted") in store.tags_for(fid)
    edges = store.links_for(fid, direction="out", relation="superseded-by")
    assert [edge.dst_ref_id for edge in edges] == [ruling]
    notes = [b.text for b in store.chunks.list_chunks_for_ref(qid)]
    assert notes.count(f"hypothesis [fi{fid}] settled — refuted") == 1
    assert dossier_ref_id(store, qid) is None


class TestExperiment:
    def test_first_experiment_folds_predicts_into_text(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {
                "op": "experiment",
                "hypothesis": f"fi{fid}",
                "text": "run XPS on the doped surface",
                "predicts": "a shifted Fe 2p peak",
            },
        )
        rendered = read_dialectic(store, qid)
        assert (
            "experiment: run XPS on the doped surface (predicts: a shifted Fe 2p peak)"
            in rendered
        )

    def test_second_experiment_edits_in_place_same_entry_count_and_handle(
        self, store: Any
    ) -> None:
        from precis.quest.dossier import _load_dialectic_blocks, dossier_ref_id

        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {"op": "experiment", "hypothesis": f"fi{fid}", "text": "first design"},
        )
        did = dossier_ref_id(store, qid)
        assert did is not None
        blocks = _load_dialectic_blocks(store, did)
        experiments = [e for b in blocks for e in b.entries if e.role == "experiment"]
        assert len(experiments) == 1
        first_handle = experiments[0].handle

        applied = apply_dialectic_op(
            store,
            qid,
            {"op": "experiment", "hypothesis": f"fi{fid}", "text": "revised design"},
        )
        assert applied is True

        blocks = _load_dialectic_blocks(store, did)
        experiments = [e for b in blocks for e in b.entries if e.role == "experiment"]
        assert len(experiments) == 1
        assert experiments[0].handle == first_handle
        assert experiments[0].text == "revised design"

    def test_identical_reemit_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {"op": "experiment", "hypothesis": f"fi{fid}", "text": "same design"},
        )
        applied = apply_dialectic_op(
            store,
            qid,
            {"op": "experiment", "hypothesis": f"fi{fid}", "text": "same design"},
        )
        assert applied is False


class TestLoadDialecticBlocksInternals:
    def test_returns_empty_when_no_dialectic_container_yet(self, store: Any) -> None:
        from precis.quest.dossier import _load_dialectic_blocks, ensure_dossier

        qid = _mk_quest(store, "A striving")
        did = ensure_dossier(store, qid)  # seeds the ledger, NOT the dialectic
        assert _load_dialectic_blocks(store, did) == []

    def test_garbage_hypothesis_meta_is_skipped_not_raised(self, store: Any) -> None:
        from precis.quest.dossier import _load_dialectic_blocks, dossier_ref_id

        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
        did = dossier_ref_id(store, qid)
        assert did is not None
        blocks = _load_dialectic_blocks(store, did)
        assert len(blocks) == 1
        block_handle = blocks[0].handle
        assert block_handle is not None
        store.drafts.patch_chunk_meta(block_handle, {"hypothesis": "junk"})
        reloaded = _load_dialectic_blocks(store, did)  # must not raise
        assert reloaded == []


def _seed_historical_support(store: Store, qid: int, fid: int, text: str) -> None:
    from precis.quest.dossier import _load_dialectic_blocks

    apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
    did = dossier_ref_id(store, qid)
    assert did is not None
    block = _load_dialectic_blocks(store, did)[0]
    chunk = store.drafts.add_chunks(
        ref_id=did,
        chunk_kind="paragraph",
        text=text,
        at={"into": block.handle},
        split=False,
    )[0]
    store.drafts.patch_chunk_meta(
        chunk.handle, {"pinned": "dialectic-entry", "role": "support"}
    )


class TestHypothesisIsRefuted:
    def test_returns_false_when_tags_for_raises(self) -> None:
        from precis.quest.dossier import _hypothesis_is_refuted

        def _boom(_finding_id: int) -> Any:
            raise RuntimeError("boom")

        fake_store: Any = SimpleNamespace(tags_for=_boom)
        assert _hypothesis_is_refuted(fake_store, 1) is False

    def test_render_shows_refuted_and_hides_entries(self, store: Any) -> None:
        from precis.store import Tag

        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store, title="a refuted hypothesis")
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        _seed_historical_support(store, qid, fid, f"[pa{paper_id}] some evidence")
        store.add_tag(fid, Tag.closed("STATUS", "refuted"))
        rendered = read_dialectic(store, qid)
        assert (
            f"[fi{fid}] a refuted hypothesis — REFUTED (do not re-propose)" in rendered
        )
        assert "some evidence" not in rendered
        assert f"[pa{paper_id}]" not in rendered


class TestMissingExperimentNudge:
    def test_block_with_support_but_no_experiment_shows_missing_line(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        _seed_historical_support(store, qid, fid, f"[pa{paper_id}] some evidence")
        rendered = read_dialectic(store, qid)
        assert "experiment: (MISSING" in rendered


class TestNarrativeIsolation:
    def test_rewrite_dossier_leaves_dialectic_blocks_intact(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store, title="Fe adatom lowers the barrier")
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
        _seed_historical_support(store, qid, fid, f"[pa{paper_id}] supporting evidence")
        before = read_dialectic(store, qid)

        rewrite_dossier(store, qid, "# Understanding\n\nA fresh narrative synthesis.")

        after = read_dialectic(store, qid)
        assert after == before

    def test_read_narrative_contains_no_dialectic_entry_text(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        _seed_historical_support(
            store,
            qid,
            fid,
            f"[pa{paper_id}] a distinctive dialectic-only marker phrase",
        )
        rewrite_dossier(store, qid, "# Understanding\n\nA fresh narrative synthesis.")
        narrative = read_narrative(store, qid)
        assert "a distinctive dialectic-only marker phrase" not in narrative


class TestReadDossier:
    def test_whole_body_contains_rendered_dialectic_exactly_once(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store, title="Fe adatom lowers the barrier")
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
        _seed_historical_support(store, qid, fid, f"[pa{paper_id}] supporting evidence")
        _did, _handle, body = read_dossier(store, qid)
        marker = f"[fi{fid}] Fe adatom lowers the barrier"
        assert body.count(marker) == 1


class TestTickWiring:
    def test_dialectic_applied_counts_valid_ops_and_ignores_garbage(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_served_hypothesis(store, qid)
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        payload = {
            "logbook": [],
            "dialectic_ops": [
                {
                    "op": "support",
                    "hypothesis": f"fi{fid}",
                    "text": f"[pa{paper_id}] solid evidence",
                },
                # garbage: unresolvable hypothesis — must degrade, never raise
                {"op": "support", "hypothesis": "fi999999999", "text": "x"},
            ],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.dialectic_applied == 1

    def test_non_dict_dialectic_op_entries_never_crash_the_tick(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        payload = {
            "logbook": [],
            "dialectic_ops": ["not a dict", 12345, None, {"op": "bogus"}],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.dialectic_applied == 0

    def test_apply_dialectic_op_raising_never_crashes_the_tick(
        self, store: Any, monkeypatch: Any
    ) -> None:
        from precis.quest import tick as tick_mod

        def _boom(_store: Any, _owner_id: int, _op: dict[str, Any]) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(tick_mod.dossier_mod, "apply_dialectic_op", _boom)
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        payload = {
            "logbook": [],
            "dialectic_ops": [{"op": "open", "hypothesis": f"fi{fid}"}],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.dialectic_applied == 0


class TestApplyDialecticOpShapeGuards:
    def test_unknown_op_kind_returns_false(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        assert (
            apply_dialectic_op(
                store, qid, {"op": "not-a-real-op", "hypothesis": f"fi{fid}"}
            )
            is False
        )

    def test_missing_op_key_returns_false(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        assert apply_dialectic_op(store, qid, {"hypothesis": f"fi{fid}"}) is False

    def test_unresolvable_hypothesis_returns_false_not_raise(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert (
            apply_dialectic_op(store, qid, {"op": "open", "hypothesis": "fi999999999"})
            is False
        )

    def test_malformed_hypothesis_handle_returns_false(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert (
            apply_dialectic_op(
                store, qid, {"op": "open", "hypothesis": "not-a-handle-at-all"}
            )
            is False
        )

    def test_blank_hypothesis_returns_false(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        assert apply_dialectic_op(store, qid, {"op": "open", "hypothesis": ""}) is False

    def test_bare_int_hypothesis_resolves(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        assert apply_dialectic_op(store, qid, {"op": "open", "hypothesis": fid}) is True

    def test_wrong_kind_handle_returns_false(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        # "st" is the `structure` kind's handle code, not `finding`'s "fi" —
        # a well-formed handle of the WRONG kind must still degrade to False.
        assert (
            apply_dialectic_op(store, qid, {"op": "open", "hypothesis": "st123"})
            is False
        )
