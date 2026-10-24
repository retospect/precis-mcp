"""The ``survey_extract`` / ``measure_accept`` worker dispatches end to
end on the dev DB (docs/backlog/measures-substrate.md, "Next slice"):
a fixture paper extracted through canned model replies, the spawned
acceptance child, the reviews ledger, and migration 0212.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis.errors import BadInput
from precis.store import Migrator, Store
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.utils.llm.router import Rung, Transport
from precis.workers.executors._yield import Done, Yield
from precis.workers.job_types import survey_extract as job
from tests.extract_helpers import (
    CHUNK1,
    drive,
    fake_ctx,
    fe_reply,
    llm_result,
    make_taxon,
    survey_world,
)


@pytest.fixture
def world(store: Store) -> dict:
    return survey_world(store)


def _extract_meta(world: dict, **extra: Any) -> dict[str, Any]:
    params = {
        "paper": world["paper"],
        "measurand_root": world["root"],
        "chunks_per_slice": 10,
        "min_chars": 10,
        **extra,
    }
    return {
        "job_type": "survey_extract",
        "executor": "coordinator",
        "params": params,
    }


class TestSurveyExtractDispatch:
    @pytest.fixture(autouse=True)
    def _local_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Run every dispatch on a chain that satisfies the reader's
        placement='local' ask — the dispatch preflights resolve_chain
        (gr502793) and the default BIG chain is cloud-only."""
        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.LOCAL, model="test-local", label="local")],
        )

    def test_end_to_end_on_a_fixture_paper(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        replies = iter(
            [
                json.dumps(fe_reply()),
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "subject": "Pd membrane",
                                "measurand": "adsorption-energy",
                                "literal": "-1.87",
                                "unit": "eV",
                                "anchor": "1",
                                "attribution": "own_work",
                                "inputs": [],
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _events = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        rows = store.measures_for(world["paper"], include_superseded=True)
        outs = [r for r in rows if r["direction"] == "output"]
        assert {r["literal"] for r in outs} == {"95", "-1.87"}
        fe = next(r for r in outs if r["literal"] == "95")
        assert fe["tier"] == "measured"
        assert fe["extraction_status"] == "anchor_matched"
        assert fe["meta"]["extraction_run"].startswith("survey-extract:")
        assert fe["anchor_scheme"] == "offsets"
        assert "95%" in CHUNK1[fe["span"][1] : fe["span"][2]]
        run_inputs = [
            r
            for r in rows
            if r["run_key"] == fe["run_key"] and r["direction"] == "input"
        ]
        assert len(run_inputs) == 2
        assert {r["literal"] for r in run_inputs} == {"NH3", "-0.5"}

    def test_rerun_is_idempotent(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(json.dumps(fe_reply()), "m")
        )
        meta = _extract_meta(world, skip_accept=True)
        drive(meta, store, job.SPEC)
        before = len(store.measures_for(world["paper"], include_superseded=True))
        done2, _ = drive(meta, store, job.SPEC)
        after = store.measures_for(world["paper"], include_superseded=True)
        assert done2.success
        assert len(after) == before
        assert "skipped_dup=1" in done2.summary

    def test_bad_replies_drop_not_crash(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setattr(job, "route", lambda req: llm_result("not json"))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        assert not store.measures_for(world["paper"], include_superseded=True)
        assert "dropped=3" in done.summary

    def test_no_local_rung_refuses_up_front(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr502793 (3): a chain with no rung satisfying the job's
        placement='local' ask fails the dispatch before chunk 1 — jo496639
        instead logged the same router error on all 99 chunks and still
        finished 'succeeded'."""
        store = world["store"]
        calls: list[Any] = []

        def _record(req: Any) -> Any:
            calls.append(req)
            return llm_result("{}")

        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.CLAUDE_P, model="claude-x")],
        )
        monkeypatch.setattr(job, "route", _record)
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert not done.success
        assert "the big chain has no local rung" in done.summary
        assert "llm.op.survey_extract" in done.summary
        assert calls == []

    def test_op_chain_local_rung_lets_the_job_run(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The llm.op.survey_extract chain override (e.g. castor's local
        gpt-oss-120b) satisfies the placement='local' ask, so the dispatch
        proceeds."""
        store = world["store"]
        monkeypatch.setattr(
            "precis.utils.llm.operations.resolve_op_chain",
            lambda *a, **k: [
                Rung(Transport.LOCAL, model="openai/gpt-oss-120b", label="local")
            ],
        )
        monkeypatch.setattr(job, "route", lambda req: llm_result('{"results": []}'))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success

    def test_accept_child_is_spawned(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setattr(job, "route", lambda req: llm_result('{"results": []}'))
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO refs (kind, set_by, title, meta) "
                "VALUES ('job', 'system', 'sx', "
                '\'{"job_type": "survey_extract", "executor": "coordinator",'
                ' "params": {}}\') RETURNING ref_id'
            ).fetchone()
        parent_id = int(row[0])
        dispatch = job.SPEC.dispatch
        assert dispatch is not None
        out = dispatch(
            fake_ctx(store, _extract_meta(world), [], ref_id=parent_id), job.SPEC
        )
        while isinstance(out, Yield) and out.state.get("phase") == "extract":
            meta = dict(_extract_meta(world))
            meta["coordinator_state"] = out.state
            out = dispatch(fake_ctx(store, meta, [], ref_id=parent_id), job.SPEC)
        assert isinstance(out, Yield)
        assert out.state["phase"] == "await_accept"
        child = out.state["accept_child"]
        with store.pool.connection() as conn:
            got = conn.execute(
                "SELECT meta->>'job_type', meta->'params'->>'extraction_run' "
                "FROM refs WHERE ref_id = %s",
                (child,),
            ).fetchone()
        assert got[0] == "measure_accept"
        assert got[1].startswith("survey-extract:")


class TestMeasureAcceptDispatch:
    def _batch(self, world: dict, run: str = "test-run") -> list[int]:
        """Three distinct live output rows on the test-run token."""
        store = world["store"]
        ids = []
        for literal, span in (
            ("95", (20, 50)),
            ("1000", (100, 130)),
            ("-0.5", (55, 80)),
        ):
            mid = store.insert_measure(
                MeasureSpec(
                    measurand_ref_id=world["fe"],
                    literal=literal,
                    subject_ref_id=world["paper"],
                    reported_unit="%",
                    tier="measured",
                    source_attribution="own_work",
                    anchor=MeasureAnchor(
                        world["paper"],
                        world["c1"],
                        "offsets",
                        [world["c1"], *span],
                    ),
                    meta={"extraction_run": run},
                ),
                actor="test",
            ).output_id
            ids.append(mid)
        return ids

    def _accept_meta(self, world: dict, **extra: Any) -> dict[str, Any]:
        params = {
            "paper": world["paper"],
            "extraction_run": "test-run",
            "sample_n": 3,
            # a 3-row sample can never pass the 5% bar; the tests raise it
            # so the dispatch's verdict path is exercised, not the math's
            "bar": 0.5,
            "reviews_per_slice": 10,
            **extra,
        }
        return {
            "job_type": "measure_accept",
            "executor": "coordinator",
            "params": params,
        }

    def test_all_correct_accepts_and_reviews(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        ids = self._batch(world)
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "correct", "reason": "matches"}), "frontier-x"
            ),
        )
        done, events = drive(self._accept_meta(world), store, job.ACCEPT_SPEC)
        assert done.success
        assert "ACCEPTED" in done.summary
        reviews = store.reviews_for("measure", ids[0])
        assert reviews and reviews[0].verdict == "approved"
        assert reviews[0].model == "frontier-x"
        acc = next(v for k, v in events if k == "meta")["acceptance"]
        assert acc["accepted"] and acc["errors"] == 0

    def test_wrong_rows_fail_the_batch(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        self._batch(world)
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "wrong", "reason": "bad unit"})
            ),
        )
        done, _ = drive(self._accept_meta(world), store, job.ACCEPT_SPEC)
        assert "FAILED" in done.summary

    def test_empty_batch_fails_fast(self, world: dict) -> None:
        done, _ = drive(self._accept_meta(world), world["store"], job.ACCEPT_SPEC)
        assert not done.success
        assert "no live output rows" in done.summary

    def test_undecidable_counts_as_error(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        self._batch(world)
        monkeypatch.setattr(job, "route", lambda req: llm_result("unreadable output"))
        done, _ = drive(self._accept_meta(world), store, job.ACCEPT_SPEC)
        assert "FAILED" in done.summary


class TestCastorTrialPrep:
    """The fable-castor-trial-review job-side fixes: a ``measurands``
    allow-list for the reader vocabulary, per-reason drop counters, and
    the bibliography/non-prose skip (``include_all_sections`` restores
    the old full walk)."""

    @pytest.fixture(autouse=True)
    def _local_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.LOCAL, model="test-local", label="local")],
        )

    def test_measurands_allow_list_narrows_the_vocab(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        child = make_taxon(store, "nh3-faradaic-efficiency", parent=world["fe"])
        allow = job._measurand_allow(store, ["faradaic-efficiency", world["ads"]])
        assert allow == sorted([world["fe"], world["ads"]])
        vocab = job._vocab(store, world["root"], allow)
        assert set(vocab) == {world["fe"], world["ads"], child}
        prompts: list[str] = []

        def _capture(req: Any) -> Any:
            prompts.append(req.prompt)
            return llm_result('{"results": []}')

        monkeypatch.setattr(job, "route", _capture)
        done, _ = drive(
            _extract_meta(world, measurands=["faradaic-efficiency"], skip_accept=True),
            store,
            job.SPEC,
        )
        assert done.success
        assert prompts
        assert "- faradaic-efficiency —" in prompts[0]
        assert "- nh3-faradaic-efficiency —" in prompts[0]
        assert "adsorption-energy" not in prompts[0]
        assert "total-current-density" not in prompts[0]

    def test_measurands_unknown_slug_refuses_up_front(self, world: dict) -> None:
        store = world["store"]
        with pytest.raises(BadInput, match="unknown measurand"):
            drive(
                _extract_meta(world, measurands=["not-a-taxon"], skip_accept=True),
                store,
                job.SPEC,
            )
        with pytest.raises(BadInput, match="must be a list"):
            drive(
                _extract_meta(
                    world, measurands="faradaic-efficiency", skip_accept=True
                ),
                store,
                job.SPEC,
            )

    def test_measurands_accept_tn_handles(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr511455: the ``tn<id>`` handle every taxon surface prints
        resolves alongside slugs and ref_ids."""
        store = world["store"]
        allow = job._measurand_allow(
            store, [f"tn{world['fe']}", f"TN{world['ads']}", str(world["jtot"])]
        )
        assert allow == sorted([world["fe"], world["ads"], world["jtot"]])
        monkeypatch.setattr(job, "route", lambda req: llm_result('{"results": []}'))
        done, _ = drive(
            _extract_meta(world, measurands=[f"tn{world['fe']}"], skip_accept=True),
            store,
            job.SPEC,
        )
        assert done.success

    def test_measurands_non_taxon_handle_refuses(self, world: dict) -> None:
        store = world["store"]
        with pytest.raises(BadInput, match="tn<id> handles or ids"):
            job._measurand_allow(store, [f"pa{world['paper']}"])
        with pytest.raises(BadInput, match="unknown measurand"):
            job._measurand_allow(store, ["tn999999999"])

    def test_per_reason_drop_counters(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        reply = {
            "results": [
                {"measurand": "nh3-yield-rate", "literal": "95", "anchor": "0"},
                {
                    "measurand": "faradaic-efficiency",
                    "literal": "95",
                    "anchor": "99",
                },
                {
                    "measurand": "faradaic-efficiency",
                    "literal": "999",
                    "anchor": "0",
                },
                {
                    "measurand": "faradaic-efficiency",
                    "literal": "95",
                    "anchor": "0",
                    "bogus": True,
                },
                {
                    "subject": "Cu NWA",
                    "measurand": "faradaic-efficiency",
                    "literal": "95",
                    "unit": "%",
                    "anchor": "0",
                    "attribution": "own_work",
                    "inputs": [
                        {
                            "condition": "product",
                            "measurand": "reaction-product",
                            "literal": "NH3",
                        },
                        {"condition": "product", "bogus_input_field": 1},
                    ],
                },
            ]
        }
        replies = iter([json.dumps(reply), "not json", '{"results": []}'])
        monkeypatch.setattr(job, "route", lambda req: llm_result(next(replies)))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["drop:unknown_measurand"] == 1
        assert stats["drop:unresolvable_anchor"] == 1
        assert stats["drop:literal_not_in_span"] == 1
        assert stats["drop:bad_shape"] == 1
        assert stats["unparseable_reply"] == 1
        assert stats["truncated"] == 0
        assert stats["flag:bad_shape"] == 1
        assert stats["written"] == 1
        assert stats["dropped"] == 5
        esc = done.summary_meta["escalations"]
        assert all(e.get("chunk_kind") == "paragraph" for e in esc)
        assert "stop_reason" in next(e for e in esc if e["kind"] == "unparseable_reply")

    def test_no_fitting_measurand_is_counted(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr511568: the reader may answer 'no listed measurand fits'
        rather than file the number under the closest allowed slug."""
        reply = {
            "results": [
                {
                    "measurand": None,
                    "literal": "40",
                    "unit": "%",
                    "anchor": "0",
                    "attribution": "own_work",
                },
            ]
        }
        replies = iter([json.dumps(reply), '{"results": []}', '{"results": []}'])
        monkeypatch.setattr(job, "route", lambda req: llm_result(next(replies)))
        done, _ = drive(
            _extract_meta(world, skip_accept=True), world["store"], job.SPEC
        )
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["drop:no_fitting_measurand"] == 1
        assert stats["written"] == 0

    def test_write_refused_is_counted(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]

        def _refuse(*a: Any, **k: Any) -> Any:
            raise BadInput("tier check: measured needs an own_work anchor")

        monkeypatch.setattr(store, "insert_measure", _refuse)
        replies = iter([json.dumps(fe_reply()), '{"results": []}', '{"results": []}'])
        monkeypatch.setattr(job, "route", lambda req: llm_result(next(replies)))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["write_refused"] == 1
        assert stats["written"] == 0
        esc = done.summary_meta["escalations"]
        assert esc[0]["kind"] == "write_refused"
        assert esc[0]["chunk_kind"] == "paragraph"

    def test_value_form_inferred_is_counted(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr503034: a value_form that is not a registered form is
        discarded so the literal's shape infers one — the trial counts
        how often castor invents a form, and keeps the emitted form in
        the escalation detail."""
        store = world["store"]
        reply = {
            "results": [
                {
                    "subject": "Cu NWA",
                    "measurand": "faradaic-efficiency",
                    "literal": "95",
                    "unit": "%",
                    "anchor": "0",
                    "attribution": "own_work",
                    "value_form": "point-estimate",
                },
                {
                    "subject": "Cu NWA",
                    "measurand": "faradaic-efficiency",
                    "literal": "-0.5",
                    "unit": "V",
                    "anchor": "0",
                    "attribution": "own_work",
                    "value_form": "point",
                    "inputs": [],
                },
            ]
        }
        replies = iter([json.dumps(reply), '{"results": []}', '{"results": []}'])
        monkeypatch.setattr(job, "route", lambda req: llm_result(next(replies)))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        # gr508162: every reader form but "categorical" is discarded, so the
        # registered "point" counts as inferred too, not only the invented
        # "point-estimate".
        assert stats["value_form_inferred"] == 2
        esc = done.summary_meta["escalations"]
        inferred = [e for e in esc if e["kind"] == "value_form_inferred"]
        assert len(inferred) == 2
        assert any("point-estimate" in e["detail"] for e in inferred)

    def test_summary_meta_carries_the_counts(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setattr(job, "route", lambda req: llm_result("not json"))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["unparseable_reply"] == 3
        assert stats["paused"] == 0
        assert done.summary_meta["escalation_count"] == 3
        assert len(done.summary_meta["escalations"]) == 3
        assert "unparseable_reply=3" in done.summary

    def test_bibliography_and_non_prose_chunks_are_skipped(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="[1] Doe et al. reported 42% FE for this step.",
            section_path=["Results", "References"],
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=4,
            text="We thank the referees for careful reading.",
            section_path=["Body", "7. Acknowledgements"],
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=5,
            text="Figure 3. Polarization curves at 100 mA cm-2.",
            chunk_kind="figure",
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=6,
            text="[2] Roe, J. Catal. 13 (2021) 1000.",
            chunk_kind="references",
        )
        calls: list[Any] = []

        def _record(req: Any) -> Any:
            calls.append(req)
            return llm_result('{"results": []}')

        monkeypatch.setattr(job, "route", _record)
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["skipped_section"] == 4
        assert stats["calls"] == 3
        assert len(calls) == 3
        assert "skipped_section=4" in done.summary

    def test_include_all_sections_disables_the_skip(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="[1] Doe et al. reported 42% FE for this step.",
            chunk_kind="references",
            section_path=["Body", "References"],
        )
        calls: list[Any] = []

        def _record(req: Any) -> Any:
            calls.append(req)
            return llm_result('{"results": []}')

        monkeypatch.setattr(job, "route", _record)
        done, _ = drive(
            _extract_meta(world, skip_accept=True, include_all_sections=True),
            store,
            job.SPEC,
        )
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["skipped_section"] == 0
        assert stats["calls"] == 4


class TestRungAwarePacing:
    """jo504632 pacing (castor pilot): the per-slice ``Yield`` park is for
    a remote/paid endpoint — it spends a whole coordinator claim cycle
    (~minutes) per ``chunks_per_slice`` chunks. A routed call that lands
    ``local`` (no rate limit, no metered cost) must run the paper to
    completion in one dispatch instead."""

    @pytest.fixture(autouse=True)
    def _local_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.LOCAL, model="test-local", label="local")],
        )

    def test_local_placement_runs_the_paper_in_one_dispatch(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        slept: list[float] = []
        monkeypatch.setattr(job.time, "sleep", slept.append)
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result('{"results": []}', placement="local"),
        )
        dispatch = job.SPEC.dispatch
        assert dispatch is not None
        # chunks_per_slice=1 with 3 chunks: the paced path would park
        # after chunk 1; the local lane must finish in one dispatch.
        out = dispatch(
            fake_ctx(
                store,
                _extract_meta(world, skip_accept=True, chunks_per_slice=1),
                [],
            ),
            job.SPEC,
        )
        assert isinstance(out, Done)
        assert out.success
        assert out.summary_meta["stats"]["calls"] == 3
        assert slept == []

    def test_remote_placement_keeps_the_per_slice_park(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result('{"results": []}', placement="cloud"),
        )
        dispatch = job.SPEC.dispatch
        assert dispatch is not None
        out = dispatch(
            fake_ctx(
                store,
                _extract_meta(world, skip_accept=True, chunks_per_slice=1),
                [],
            ),
            job.SPEC,
        )
        assert isinstance(out, Yield)
        assert out.state["phase"] == "extract"
        assert out.state["stats"]["calls"] == 1
        assert out.wake_when.kind == "at_time"

    def test_unreported_placement_keeps_the_per_slice_park(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A result with no placement stamp (older rung, direct provider)
        paces exactly like remote — only a confirmed local landing skips
        the park."""
        store = world["store"]
        monkeypatch.setattr(job, "route", lambda req: llm_result('{"results": []}'))
        dispatch = job.SPEC.dispatch
        assert dispatch is not None
        out = dispatch(
            fake_ctx(
                store,
                _extract_meta(world, skip_accept=True, chunks_per_slice=1),
                [],
            ),
            job.SPEC,
        )
        assert isinstance(out, Yield)
        assert out.state["stats"]["calls"] == 1


class TestPausedBound:
    """gr508162: a ``paused`` router answer parks the slice with a
    ``wake_when.payload.why`` note naming the wait, and an unbroken
    pause streak past ``PRECIS_SURVEY_EXTRACT_MAX_PAUSES`` fails the job
    with the reason on the record instead of parking forever (jo504632
    spent its last ~10 h in exactly that silent loop)."""

    @pytest.fixture(autouse=True)
    def _local_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.LOCAL, model="test-local", label="local")],
        )

    def _paused(self) -> Any:
        return replace(
            llm_result("", placement="local"),
            paused=True,
            reason_class="rate",
        )

    def test_paused_park_annotates_what_it_waits_for(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setattr(job, "route", lambda req: self._paused())
        dispatch = job.SPEC.dispatch
        assert dispatch is not None
        out = dispatch(
            fake_ctx(store, _extract_meta(world, skip_accept=True), []),
            job.SPEC,
        )
        assert isinstance(out, Yield)
        assert out.wake_when.kind == "at_time"
        assert "local reader lane saturated" in out.wake_when.payload["why"]
        assert "rate" in out.wake_when.payload["why"]
        assert out.state["pause_streak"] == 1

    def test_pause_streak_cap_fails_with_the_reason(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        monkeypatch.setenv("PRECIS_SURVEY_EXTRACT_MAX_PAUSES", "3")
        monkeypatch.setattr(job, "route", lambda req: self._paused())
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert not done.success
        assert "saturated" in done.summary
        assert "3 consecutive paused slices" in done.summary

    def test_an_answered_call_breaks_the_streak(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pause sandwiched between answered calls is a blip, not a
        wedge — the streak counts only UNBROKEN saturated slices."""
        store = world["store"]
        monkeypatch.setenv("PRECIS_SURVEY_EXTRACT_MAX_PAUSES", "2")
        replies = iter([self._paused(), llm_result('{"results": []}')])
        monkeypatch.setattr(job, "route", lambda req: next(replies, self._paused()))
        dispatch = job.SPEC.dispatch
        assert dispatch is not None
        meta = _extract_meta(world, skip_accept=True)
        out = dispatch(fake_ctx(store, meta, []), job.SPEC)
        assert isinstance(out, Yield)
        assert out.state["pause_streak"] == 1
        # Next slice answers chunk 1 (streak resets), then pauses on the
        # following chunk → streak 1 again, not 2 → still a Yield.
        meta["coordinator_state"] = out.state
        out = dispatch(fake_ctx(store, meta, []), job.SPEC)
        assert isinstance(out, Yield)
        assert out.state["pause_streak"] == 1


def _seed_body_chunk(
    store: Store,
    paper: int,
    *,
    ord_: int,
    text: str,
    chunk_kind: str = "paragraph",
    section_path: list[str] | None = None,
) -> int:
    """A body chunk carrying a ``section_path`` (``seed_chunk`` sets none)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, "
            "section_path) VALUES (%s, 'system', %s, %s, %s, %s) "
            "RETURNING chunk_id",
            (paper, ord_, chunk_kind, text, section_path or []),
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


# ── migration 0212 ───────────────────────────────────────────────────

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"


def _taxon_meta(slug: str, unit: str, hib: Any = None) -> str:
    meta: dict[str, Any] = {"slug": slug, "canonical_unit": unit}
    if hib is not None:
        meta["higher_is_better"] = hib
    return json.dumps(meta)


def test_0212_sets_higher_is_better_on_the_two_taxa(fresh_db: str) -> None:
    """Replay to 0211, seed the taxa, apply 0212, read the directions back."""
    tmp = Path(tempfile.mkdtemp(prefix="precis_test_0212_"))
    try:
        for f in MIGRATIONS_DIR.glob("*.sql"):
            if f.name >= "0212":
                continue
            shutil.copy(f, tmp / f.name)
        Migrator(fresh_db, tmp).apply_all()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        for slug, unit, hib in (
            ("total-current-density", "A/m^2", None),
            ("adsorption-energy", "J", None),
            ("adsorption-energy", "J", True),  # an explicit ruling stays
            ("total-current-density", "J", None),  # wrong unit: untouched
            ("reaction-step-free-energy", "J", None),  # ambiguous: untouched
        ):
            conn.execute(
                "INSERT INTO refs (kind, set_by, title, meta) "
                "VALUES ('taxon', 'system', %s, %s::jsonb)",
                (f"{slug}-{unit}", _taxon_meta(slug, unit, hib)),
            )
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db) as conn:
        rows = conn.execute(
            "SELECT meta->>'slug' AS slug, meta->>'canonical_unit' AS unit, "
            "       meta->'higher_is_better' AS hib "
            "  FROM refs WHERE kind = 'taxon' ORDER BY ref_id"
        ).fetchall()
    got = {(r[0], r[1]): r[2] for r in rows}
    assert got[("total-current-density", "A/m^2")] is True
    # the NULL-keyed adsorption-energy gained false; the explicitly
    # true-keyed one was left alone
    hibs = sorted(r[2] for r in rows if r[0] == "adsorption-energy")
    assert hibs == [False, True]
    assert got[("total-current-density", "J")] is None
    assert got[("reaction-step-free-energy", "J")] is None
