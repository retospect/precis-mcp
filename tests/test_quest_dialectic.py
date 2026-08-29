"""Tests for the quest-dossier **dialectic blocks** layer
(quest-dossier-dialectic §Mechanism, ``src/precis/quest/dossier.py``'s
"Dialectic blocks" section + ``src/precis/quest/tick.py``'s
``dialectic_ops`` wiring).

Covers: ``apply_dialectic_op``'s op vocabulary (open/support/counter/
experiment/settle) — block creation, near-dup dedup per role, inline-handle
evidence-edge minting, the in-place experiment upsert, settle collapsing the
render, and re-opening a settled block; ``read_dialectic``'s rendering
(missing-experiment nudge); that ``rewrite_dossier``/``read_narrative``
leave dialectic blocks untouched; ``read_dossier``'s whole-body join; and
the tick's ``dialectic_ops`` apply stage (degrade-don't-crash, prompt
wiring). Runs against real PG (the ``store`` fixture).
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.dossier import (
    apply_dialectic_op,
    read_dialectic,
    read_dossier,
    read_narrative,
    rewrite_dossier,
)
from precis.quest.tick import run_quest_tick
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


def _sequenced_dispatch(
    payloads: list[dict[str, Any] | None],
) -> tuple[Any, list[Any]]:
    """Records every ``LlmRequest`` it's called with (mirrors
    ``test_quest_tick.py``'s own helper) so a test can inspect the prompt."""
    calls: list[Any] = []

    def _d(req: Any) -> Any:
        idx = min(len(calls), len(payloads) - 1)
        calls.append(req)
        return SimpleNamespace(
            data=payloads[idx], text="", error=None, cost_usd=0.01, paused=False
        )

    return _d, calls


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


class TestSupportCounter:
    def test_support_appends_an_entry(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        assert (
            apply_dialectic_op(
                store,
                qid,
                {
                    "op": "support",
                    "hypothesis": f"fi{fid}",
                    "text": "DFT shows a lower barrier",
                },
            )
            is True
        )
        rendered = read_dialectic(store, qid)
        assert "support: DFT shows a lower barrier" in rendered

    def test_near_dup_restatement_same_role_is_a_noop(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        assert (
            apply_dialectic_op(
                store,
                qid,
                {
                    "op": "support",
                    "hypothesis": f"fi{fid}",
                    "text": "identify the rate-limiting step",
                },
            )
            is True
        )
        assert (
            apply_dialectic_op(
                store,
                qid,
                {
                    "op": "support",
                    "hypothesis": f"fi{fid}",
                    "text": "identify the rate limiting step in the pathway",
                },
            )
            is False
        )

    def test_counter_with_same_text_as_a_support_is_allowed(self, store: Any) -> None:
        # dedup is per-role — a counter entry restating a support's exact
        # text is a distinct claim (arguing the OTHER direction), not a dup.
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        text = "identify the rate-limiting step"
        assert (
            apply_dialectic_op(
                store, qid, {"op": "support", "hypothesis": f"fi{fid}", "text": text}
            )
            is True
        )
        assert (
            apply_dialectic_op(
                store, qid, {"op": "counter", "hypothesis": f"fi{fid}", "text": text}
            )
            is True
        )
        rendered = read_dialectic(store, qid)
        assert f"support: {text}" in rendered
        assert f"counter: {text}" in rendered


class TestEvidenceEdges:
    def test_support_citing_a_handle_mints_a_supports_link(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        apply_dialectic_op(
            store,
            qid,
            {
                "op": "support",
                "hypothesis": f"fi{fid}",
                "text": f"[pa{paper_id}] reports the same trend",
            },
        )
        rows = _links(store, fid, "supports")
        assert (paper_id, fid) in rows

    def test_counter_citing_a_handle_mints_a_contradicts_link(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        paper_id = seed_ref(store, title="a contrary paper", kind="paper")
        apply_dialectic_op(
            store,
            qid,
            {
                "op": "counter",
                "hypothesis": f"fi{fid}",
                "text": f"[pa{paper_id}] found the opposite",
            },
        )
        rows = _links(store, fid, "contradicts")
        assert (paper_id, fid) in rows

    def test_reapplying_a_cited_op_is_idempotent(self, store: Any) -> None:
        # a distinct second op (different text) still cites the SAME paper —
        # re-minting the same edge tuple must not duplicate the row or raise.
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        paper_id = seed_ref(store, title="a cited paper", kind="paper")
        apply_dialectic_op(
            store,
            qid,
            {
                "op": "support",
                "hypothesis": f"fi{fid}",
                "text": f"[pa{paper_id}] reports the same trend",
            },
        )
        applied = apply_dialectic_op(
            store,
            qid,
            {
                "op": "support",
                "hypothesis": f"fi{fid}",
                "text": f"[pa{paper_id}] reports the same trend again, differently worded entirely",
            },
        )
        # near-dup guard may or may not accept the second phrasing — either
        # way, no crash and no duplicate edge row.
        assert applied in (True, False)
        rows = _links(store, fid, "supports")
        assert rows.count((paper_id, fid)) == 1

    def test_edge_minting_skips_bad_chunk_self_and_fk_failing_handles(
        self, store: Any
    ) -> None:
        # one entry citing, together: a nonexistent CHUNK handle (chunk-owner
        # lookup misses), the hypothesis's OWN handle (self-edge), and a
        # nonexistent ref handle (add_link FK failure) — every skip path in
        # `_mint_evidence_edges` fires, and the op itself must still land
        # (the entry text) with zero links minted for the hypothesis.
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        text = (
            f"cites a stub chunk [pc999999999], itself [fi{fid}], and a "
            "missing ref [pa999999999]"
        )
        applied = apply_dialectic_op(
            store, qid, {"op": "support", "hypothesis": f"fi{fid}", "text": text}
        )
        assert applied is True
        rendered = read_dialectic(store, qid)
        assert text in rendered
        assert _links(store, fid, "supports") == []


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


class TestSettle:
    def test_settle_collapses_render_and_hides_entries(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {"op": "support", "hypothesis": f"fi{fid}", "text": "supporting evidence"},
        )
        applied = apply_dialectic_op(
            store,
            qid,
            {
                "op": "settle",
                "hypothesis": f"fi{fid}",
                "text": "confirmed by DFT + XPS",
            },
        )
        assert applied is True
        rendered = read_dialectic(store, qid)
        assert "SETTLED: confirmed by DFT + XPS" in rendered
        assert "supporting evidence" not in rendered

    def test_later_open_reopens_and_entries_render_again(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {"op": "support", "hypothesis": f"fi{fid}", "text": "supporting evidence"},
        )
        apply_dialectic_op(
            store, qid, {"op": "settle", "hypothesis": f"fi{fid}", "text": "confirmed"}
        )
        applied = apply_dialectic_op(
            store, qid, {"op": "open", "hypothesis": f"fi{fid}"}
        )
        assert applied is True
        rendered = read_dialectic(store, qid)
        assert "SETTLED" not in rendered
        assert "support: supporting evidence" in rendered


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
        apply_dialectic_op(
            store,
            qid,
            {"op": "support", "hypothesis": f"fi{fid}", "text": "some evidence"},
        )
        store.add_tag(fid, Tag.closed("STATUS", "refuted"))
        rendered = read_dialectic(store, qid)
        assert (
            f"[fi{fid}] a refuted hypothesis — REFUTED (do not re-propose)" in rendered
        )
        assert "some evidence" not in rendered


class TestMissingExperimentNudge:
    def test_block_with_support_but_no_experiment_shows_missing_line(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {"op": "support", "hypothesis": f"fi{fid}", "text": "some evidence"},
        )
        rendered = read_dialectic(store, qid)
        assert "experiment: (MISSING" in rendered


class TestNarrativeIsolation:
    def test_rewrite_dossier_leaves_dialectic_blocks_intact(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store, title="Fe adatom lowers the barrier")
        apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
        apply_dialectic_op(
            store,
            qid,
            {"op": "support", "hypothesis": f"fi{fid}", "text": "supporting evidence"},
        )
        before = read_dialectic(store, qid)

        rewrite_dossier(store, qid, "# Understanding\n\nA fresh narrative synthesis.")

        after = read_dialectic(store, qid)
        assert after == before

    def test_read_narrative_contains_no_dialectic_entry_text(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        apply_dialectic_op(
            store,
            qid,
            {
                "op": "support",
                "hypothesis": f"fi{fid}",
                "text": "a distinctive dialectic-only marker phrase",
            },
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
        apply_dialectic_op(store, qid, {"op": "open", "hypothesis": f"fi{fid}"})
        apply_dialectic_op(
            store,
            qid,
            {"op": "support", "hypothesis": f"fi{fid}", "text": "supporting evidence"},
        )
        _did, _handle, body = read_dossier(store, qid)
        marker = f"[fi{fid}] Fe adatom lowers the barrier"
        assert body.count(marker) == 1


class TestTickWiring:
    def test_dialectic_applied_counts_valid_ops_and_ignores_garbage(
        self, store: Any
    ) -> None:
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store, title="a live hypothesis")
        payload = {
            "logbook": [],
            "dialectic_ops": [
                {"op": "open", "hypothesis": f"fi{fid}"},
                {"op": "support", "hypothesis": f"fi{fid}", "text": "solid evidence"},
                # garbage: unresolvable hypothesis — must degrade, never raise
                {"op": "support", "hypothesis": "fi999999999", "text": "x"},
            ],
        }
        out = run_quest_tick(store, qid, dispatch_fn=_fake_dispatch(payload))
        assert out.status == "succeeded"
        assert out.dialectic_applied == 2

    def test_prompt_contains_dialectic_blocks_section(self, store: Any) -> None:
        qid = _mk_quest(store, "A striving")
        disp, reqs = _sequenced_dispatch([{"logbook": []}])
        run_quest_tick(store, qid, dispatch_fn=disp)
        assert len(reqs) == 1
        assert "## Dialectic blocks" in reqs[0].prompt

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

    def test_blank_text_on_support_is_a_side_effect_open_only(self, store: Any) -> None:
        # a bare op with no usable text still mints the block (open-by-
        # side-effect), but appends no entry.
        qid = _mk_quest(store, "A striving")
        fid = _mk_finding(store)
        applied = apply_dialectic_op(
            store, qid, {"op": "support", "hypothesis": f"fi{fid}", "text": "   "}
        )
        assert applied is True
        rendered = read_dialectic(store, qid)
        assert "support:" not in rendered
