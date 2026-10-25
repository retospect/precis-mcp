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
from psycopg.types.json import Jsonb

from precis.errors import BadInput
from precis.store import Migrator, Store, Tag
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
from tests.workers._helpers import seed_chunk, seed_ref


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


class TestValidate:
    """``JobTypeSpec.validate`` (gr511724): the same store-backed
    refusals the dispatch performs, surfaced at put so a bad submit is
    refused instead of landing as a failed job."""

    def test_unknown_measurand_refuses(self, world: dict) -> None:
        with pytest.raises(BadInput, match="unknown measurand"):
            job.validate(
                world["store"],
                {"paper": world["paper"], "measurands": ["not-a-taxon"]},
            )

    def test_unresolvable_paper_refuses(self, world: dict) -> None:
        with pytest.raises(BadInput, match="does not resolve"):
            job.validate(world["store"], {"paper": 99999999})

    def test_good_params_pass(self, world: dict) -> None:
        job.validate(
            world["store"],
            {"paper": world["paper"], "measurands": ["faradaic-efficiency"]},
        )

    def test_spec_wires_the_hook(self) -> None:
        assert job.SPEC.validate is job.validate
        # gr515504: validator membership, not identity — a second
        # validator wired in later must not break this check.
        validators = {
            v for n, v in vars(job).items() if "validate" in n and callable(v)
        }
        assert job.ACCEPT_SPEC.validate in validators


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

    def test_unanchored_row_is_flagged_but_out_of_the_verdict_math(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr515505: an asserted row with no anchor is still a live output
        row of the run — it joins the frame, is drawn, and is NAMED in the
        summary, flagged "unanchored". But it is not a sampled error:
        unverifiable sits out of both Wilson terms, so its presence leaves
        the batch's verdict exactly what the three checkable rows earn."""
        store = world["store"]
        anchored = self._batch(world)
        unanchored = store.insert_measure(
            MeasureSpec(
                measurand_ref_id=world["jtot"],
                literal="42",
                subject_ref_id=world["paper"],
                reported_unit="A m-2",
                tier="asserted",
                meta={"extraction_run": "test-run"},
            ),
            actor="test",
        ).output_id
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "correct", "reason": "matches"}),
                "frontier-x",
            ),
        )
        done, events = drive(
            self._accept_meta(world, sample_n=10), store, job.ACCEPT_SPEC
        )
        acc = next(v for k, v in events if k == "meta")["acceptance"]
        # The batch has 4 rows, the draw saw 4, but the Wilson math ran
        # on the 3 checkable ones — same verdict as before the LEFT JOIN.
        assert acc["total_rows"] == 4 and acc["sampled"] == 3
        assert acc["errors"] == 0 and acc["unanchored"] == 1
        assert "ACCEPTED" in done.summary
        assert "; 1 unanchored, excluded" in done.summary
        assert f"mx{unanchored}: unanchored — no anchor on the row" in done.summary
        assert f"mx{anchored[0]}:" not in done.summary
        # No review lands for a row that was never checkable.
        assert store.reviews_for("measure", unanchored) == []

    def test_anchor_lost_row_is_excluded_and_named(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The re-accept case the review caught: a batch row whose anchor
        died after extraction (the chunk delete NULLs the link, the scheme
        stays stamped) reads "anchor lost", not "no anchor on the row" —
        and the verdict matches the pre-flag math for the surviving rows."""
        store = world["store"]
        self._batch(world)
        lost = store.insert_measure(
            MeasureSpec(
                measurand_ref_id=world["jtot"],
                literal="42",
                subject_ref_id=world["paper"],
                reported_unit="A m-2",
                tier="measured",
                source_attribution="own_work",
                anchor=MeasureAnchor(
                    world["paper"], world["c3"], "offsets", [world["c3"], 0, 10]
                ),
                meta={"extraction_run": "test-run"},
            ),
            actor="test",
        ).output_id
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (world["c3"],))
        gone = next(r for r in store.measures_for(world["paper"]) if r["id"] == lost)
        assert gone["primary_link_id"] is None and gone["anchor_lost"] is True
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "correct", "reason": "matches"}),
                "frontier-x",
            ),
        )
        done, events = drive(
            self._accept_meta(world, sample_n=10), store, job.ACCEPT_SPEC
        )
        acc = next(v for k, v in events if k == "meta")["acceptance"]
        assert acc["errors"] == 0 and acc["sampled"] == 3
        assert acc["unanchored"] == 1
        assert "ACCEPTED" in done.summary
        assert f"mx{lost}: unanchored — anchor lost" in done.summary
        assert "no anchor on the row" not in done.summary

    def test_undecidable_counts_as_error(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = world["store"]
        self._batch(world)
        monkeypatch.setattr(job, "route", lambda req: llm_result("unreadable output"))
        done, _ = drive(self._accept_meta(world), store, job.ACCEPT_SPEC)
        assert "FAILED" in done.summary


class TestRunLevelAcceptance:
    """Corpus slice T AC5/AC6: the batch is the ``extraction_run`` token,
    not the paper — ``measure_accept`` runs without ``paper`` (stratified
    paper × measurand). The verdict records its coverage (``papers`` +
    the ``max_id`` high-water mark); ``accepted_runs`` resolves the
    latest counting verdict per covered paper and ``best_measure``'s
    ``accepted_only`` gates a token row on its paper's pass, the mark,
    and its anchor."""

    def _row(
        self,
        world: dict,
        *,
        paper: int,
        chunk: int,
        lit: str,
        text: str,
        measurand: int,
        unit: str,
        token: str | None,
    ) -> int:
        start = text.index(lit)
        return (
            world["store"]
            .insert_measure(
                MeasureSpec(
                    measurand_ref_id=measurand,
                    literal=lit,
                    subject_ref_id=paper,
                    reported_unit=unit,
                    tier="measured",
                    source_attribution="own_work",
                    anchor=MeasureAnchor(
                        paper, chunk, "offsets", [chunk, start, start + len(lit)]
                    ),
                    meta={"extraction_run": token} if token else {},
                ),
                actor="test",
            )
            .output_id
        )

    def _accept_job(
        self,
        store: Store,
        token: str,
        accepted: bool,
        finished: str,
        *,
        verdict: bool = True,
        papers: list[int] | None = None,
        max_id: int | None = None,
        bar: float = 0.05,
        z: float = 1.645,
    ) -> int:
        """A finished ``measure_accept`` job ref — ``verdict=False`` is a
        job that finished before reaching a verdict (no meta.acceptance);
        ``papers=None`` writes a pre-coverage verdict, which counts for
        nothing."""
        meta: dict[str, Any] = {
            "job_type": "measure_accept",
            "executor": "coordinator",
            "params": {"extraction_run": token},
            "finished_at": finished,
        }
        if verdict:
            acc: dict[str, Any] = {
                "accepted": accepted,
                "run_token": token,
                "errors": 0 if accepted else 4,
                "sampled": 60,
                "wilson_hi": 0.04 if accepted else 0.12,
                "bar": bar,
                "z": z,
            }
            if papers is not None:
                acc["papers"] = papers
            if max_id is not None:
                acc["max_id"] = max_id
            meta["acceptance"] = acc
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO refs (kind, set_by, title, meta) "
                "VALUES ('job', 'system', 'ma', %s) RETURNING ref_id",
                (Jsonb(meta),),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def test_six_paper_run_level_batch_is_accepted(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC5: a 60-row all-correct batch over 6 papers — under a
        per-paper batch 10 rows could never pass the n=52 bar."""
        store = world["store"]
        row_ids: list[int] = []
        for p in range(6):
            paper = seed_ref(store, title=f"batch paper {p}", kind="paper")
            spec = [(world["jtot"], f"9{p}{i}", "mA cm-2") for i in range(5)]
            spec += [(world["ads"], f"-1.{p}{i}", "eV") for i in range(5)]
            text = "; ".join(f"reading {lit} {u}" for _m, lit, u in spec)
            chunk = seed_chunk(store, ref_id=paper, text=text)
            for measurand, lit, unit in spec:
                row_ids.append(
                    self._row(
                        world,
                        paper=paper,
                        chunk=chunk,
                        lit=lit,
                        text=text,
                        measurand=measurand,
                        unit=unit,
                        token="trial-1",
                    )
                )
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "correct", "reason": "matches"}), "frontier-x"
            ),
        )
        # gr515503: a real job ref so the dispatch's set_meta lands in
        # refs.meta — a finished accept job through the dispatch, not a
        # hand-inserted row. (finished_at is executor-finalize state, so
        # it goes in at insert.)
        job_ref = store.insert_ref(
            kind="job",
            slug=None,
            title="ma",
            meta={
                "job_type": "measure_accept",
                "executor": "coordinator",
                "params": {"extraction_run": "trial-1", "reviews_per_slice": 50},
                "finished_at": "2026-10-24T12:00:00+00:00",
            },
        )
        done, events = drive(
            {
                "job_type": "measure_accept",
                "executor": "coordinator",
                "params": {"extraction_run": "trial-1", "reviews_per_slice": 50},
            },
            store,
            job.ACCEPT_SPEC,
            ref_id=job_ref.id,
        )
        assert done.success
        assert "ACCEPTED" in done.summary
        acc = next(v for k, v in events if k == "meta")["acceptance"]
        assert acc["accepted"] and acc["sampled"] == 60 and acc["errors"] == 0
        assert len(acc["papers"]) == 6
        assert acc["max_id"] == max(row_ids)
        # the verdict persisted to refs.meta and governs per paper
        got = store.accepted_runs("trial-1")["trial-1"]
        assert set(got) == set(acc["papers"])
        assert all(v["accepted"] for v in got.values())

    def test_run_level_needs_a_token_or_a_paper(self, world: dict) -> None:
        done, _ = drive(
            {"job_type": "measure_accept", "executor": "coordinator", "params": {}},
            world["store"],
            job.ACCEPT_SPEC,
        )
        assert not done.success
        assert "extraction_run" in done.summary

    def test_run_level_unknown_token_fails(self, world: dict) -> None:
        done, _ = drive(
            {
                "job_type": "measure_accept",
                "executor": "coordinator",
                "params": {"extraction_run": "no-such-run"},
            },
            world["store"],
            job.ACCEPT_SPEC,
        )
        assert not done.success
        assert "no live output rows" in done.summary

    def test_accepted_runs_maps_latest_finished_verdict(self, world: dict) -> None:
        store = world["store"]
        p = world["paper"]
        self._accept_job(
            store,
            "tok-ok",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[p],
            max_id=10,
        )
        self._accept_job(
            store,
            "tok-bad",
            False,
            "2026-10-24T10:00:00+00:00",
            papers=[p],
            max_id=10,
        )
        # a later finished job's verdict supersedes the earlier one…
        self._accept_job(
            store,
            "tok-bad",
            True,
            "2026-10-24T12:00:00+00:00",
            papers=[p],
            max_id=10,
        )
        # …but a job that finished before a verdict says nothing
        self._accept_job(
            store, "tok-ok", False, "2026-10-24T13:00:00+00:00", verdict=False
        )
        got = store.accepted_runs(["tok-ok", "tok-bad", "tok-none"])
        assert got["tok-ok"][p]["accepted"] is True
        assert got["tok-bad"][p]["accepted"] is True
        assert got["tok-none"] == {}
        assert store.accepted_runs("tok-ok")["tok-ok"][p]["accepted"] is True

    def test_best_measure_accepted_only_skips_failed_token(self, world: dict) -> None:
        """AC6 first half: a failed token's rows drop out of the ranking;
        a row with no token (hand-written) is not an extraction row and
        stays."""
        store = world["store"]
        quest = seed_ref(store, title="accept quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        text = CHUNK1
        top = 0
        for lit, token in (("1000", "bad-run"), ("95", "good-run"), ("-0.5", None)):
            top = self._row(
                world,
                paper=world["paper"],
                chunk=world["c1"],
                lit=lit,
                text=text,
                measurand=world["ads"],
                unit="eV",
                token=token,
            )
        p = world["paper"]
        self._accept_job(
            store,
            "good-run",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[p],
            max_id=top,
        )
        self._accept_job(
            store,
            "bad-run",
            False,
            "2026-10-24T10:00:00+00:00",
            papers=[p],
            max_id=top,
        )

        (group,) = store.best_measure(world["ads"], serving=quest, sense="max")
        assert group["n"] == 3 and group["best"]["literal"] == "1000"

        (group,) = store.best_measure(
            world["ads"], serving=quest, sense="max", accepted_only=True
        )
        assert group["n"] == 2 and group["best"]["literal"] == "95"

    def test_paper_scoped_verdict_covers_only_its_paper(self, world: dict) -> None:
        """One paper's pass under a shared token is partial coverage — the
        other papers' rows stay out."""
        store = world["store"]
        quest = seed_ref(store, title="cov quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        other = seed_ref(store, title="other survey", kind="paper")
        store.add_link(src_ref_id=other, dst_ref_id=quest, relation="serves")
        oc = seed_chunk(store, ref_id=other, text=CHUNK1)
        a = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="shr",
        )
        b = self._row(
            world,
            paper=other,
            chunk=oc,
            lit="1000",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="shr",
        )
        self._accept_job(
            store,
            "shr",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=max(a, b),
        )

        (group,) = store.best_measure(
            world["ads"], serving=quest, sense="max", accepted_only=True
        )
        assert group["n"] == 1 and group["best"]["id"] == a

    def test_paper_scoped_passes_union_to_run_coverage(self, world: dict) -> None:
        """Each paper-scoped pass contributes its paper: the union of the
        per-paper verdicts is the run's coverage."""
        store = world["store"]
        quest = seed_ref(store, title="union quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        other = seed_ref(store, title="other survey", kind="paper")
        store.add_link(src_ref_id=other, dst_ref_id=quest, relation="serves")
        oc = seed_chunk(store, ref_id=other, text=CHUNK1)
        a = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="u",
        )
        b = self._row(
            world,
            paper=other,
            chunk=oc,
            lit="1000",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="u",
        )
        self._accept_job(
            store,
            "u",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=a,
        )
        self._accept_job(
            store,
            "u",
            True,
            "2026-10-24T10:05:00+00:00",
            papers=[other],
            max_id=b,
        )

        (group,) = store.best_measure(
            world["ads"], serving=quest, sense="max", accepted_only=True
        )
        assert group["n"] == 2 and group["best"]["id"] == b

    def test_rows_minted_after_the_verdict_are_outside_coverage(
        self, world: dict
    ) -> None:
        """The verdict's max_id is the high-water mark: a row stamped with
        the token after the accept was never in the sampling frame."""
        store = world["store"]
        quest = seed_ref(store, title="hwm quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        seen = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="grow",
        )
        self._accept_job(
            store,
            "grow",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=seen,
        )
        late = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="1000",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="grow",
        )
        assert late > seen

        (group,) = store.best_measure(world["ads"], serving=quest, sense="max")
        assert group["n"] == 2 and group["best"]["id"] == late

        (group,) = store.best_measure(
            world["ads"], serving=quest, sense="max", accepted_only=True
        )
        assert group["n"] == 1 and group["best"]["id"] == seen

    def test_anchorless_token_rows_fail_the_gate(self, world: dict) -> None:
        """A token row with no anchor is flagged ``unanchored`` in the
        batch and stays out of the verdict math — the gate must not pass
        it either."""
        store = world["store"]
        quest = seed_ref(store, title="anchor quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        store.insert_measure(
            MeasureSpec(
                measurand_ref_id=world["ads"],
                literal="95",
                subject_ref_id=world["paper"],
                reported_unit="eV",
                tier="asserted",
                source_attribution="own_work",
                meta={"extraction_run": "t0"},
            ),
            actor="test",
        )
        anchored = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="1000",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="t0",
        )
        self._accept_job(
            store,
            "t0",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=anchored,
        )

        (ungated,) = store.best_measure(world["ads"], serving=quest, sense="max")
        assert ungated["n"] == 2  # the anchorless row does reach the gate

        (group,) = store.best_measure(
            world["ads"], serving=quest, sense="max", accepted_only=True
        )
        assert group["n"] == 1 and group["best"]["id"] == anchored

    def test_loose_bar_verdict_counts_for_nothing(self, world: dict) -> None:
        """A re-accept under a looser bar neither grants nor overturns: the
        default-bar fail stands."""
        store = world["store"]
        quest = seed_ref(store, title="bar quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        mid = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="bar-t",
        )
        self._accept_job(
            store,
            "bar-t",
            False,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=mid,
            bar=0.05,
        )
        self._accept_job(
            store,
            "bar-t",
            True,
            "2026-10-24T12:00:00+00:00",
            papers=[world["paper"]],
            max_id=mid,
            bar=0.20,
        )

        assert (
            store.best_measure(
                world["ads"], serving=quest, sense="max", accepted_only=True
            )
            == []
        )

    def test_pre_coverage_verdicts_grant_nothing(self, world: dict) -> None:
        """A verdict written before the coverage fields existed is not a
        counting verdict — fail closed."""
        store = world["store"]
        quest = seed_ref(store, title="legacy quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="old",
        )
        self._accept_job(store, "old", True, "2026-10-24T10:00:00+00:00")

        assert (
            store.best_measure(
                world["ads"], serving=quest, sense="max", accepted_only=True
            )
            == []
        )

    def _extract_ref(
        self,
        store: Store,
        token: str | None,
        *,
        via_params: bool = False,
        status: str | None = None,
        finished_at: str | None = None,
    ) -> int:
        """A ``survey_extract`` job ref — the token rides
        ``coordinator_state`` (or ``params.run_token`` with
        ``via_params``); ``None`` leaves a job that has not checkpointed
        yet (its run token will be the default ``survey-extract:<ref_id>``).
        ``status`` adds that ``STATUS:`` tag; ``finished_at`` stamps the
        legacy meta field."""
        meta: dict[str, Any] = {
            "job_type": "survey_extract",
            "executor": "coordinator",
            "params": {},
        }
        if token is not None:
            if via_params:
                meta["params"]["run_token"] = token
            else:
                meta["coordinator_state"] = {"run_token": token}
        if finished_at is not None:
            meta["finished_at"] = finished_at
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO refs (kind, set_by, title, meta) "
                "VALUES ('job', 'system', 'se', %s) RETURNING ref_id",
                (Jsonb(meta),),
            ).fetchone()
        assert row is not None
        ref_id = int(row[0])
        if status is not None:
            store.add_tag(
                ref_id,
                Tag.parse_strict(f"STATUS:{status}"),
                set_by="system",
                replace_prefix=True,
            )
        return ref_id

    def test_later_counting_fail_revokes_the_pass(self, world: dict) -> None:
        """The governing verdict is the latest *counting* one: a re-accept
        at the default bar and z that fails revokes the earlier pass."""
        store = world["store"]
        quest = seed_ref(store, title="revoke quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        mid = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="rev",
        )
        self._accept_job(
            store,
            "rev",
            True,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=mid,
        )
        self._accept_job(
            store,
            "rev",
            False,
            "2026-10-24T12:00:00+00:00",
            papers=[world["paper"]],
            max_id=mid,
        )

        assert store.accepted_runs("rev")["rev"][world["paper"]]["accepted"] is False
        assert (
            store.best_measure(
                world["ads"], serving=quest, sense="max", accepted_only=True
            )
            == []
        )

    def test_rows_committed_after_the_draw_are_outside_coverage(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The frame is frozen at the draw: a row — even on a paper the
        batch had not seen — stamped with the token mid-review never
        lands inside coverage."""
        store = world["store"]
        ids = [
            self._row(
                world,
                paper=world["paper"],
                chunk=world["c1"],
                lit=lit,
                text=CHUNK1,
                measurand=world["ads"],
                unit="eV",
                token="t-drift",
            )
            for lit in ("95", "1000", "-0.5")
        ]
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "correct", "reason": "matches"})
            ),
        )
        meta = {
            "job_type": "measure_accept",
            "executor": "coordinator",
            "params": {
                "extraction_run": "t-drift",
                "reviews_per_slice": 1,
                "bar": 0.5,
            },
        }
        events: list = []
        dispatch = job.ACCEPT_SPEC.dispatch
        assert dispatch is not None
        out = dispatch(fake_ctx(store, dict(meta), events), job.ACCEPT_SPEC)
        assert isinstance(out, Yield)
        # mid-review, another paper commits a row under the same token
        other = seed_ref(store, title="late survey", kind="paper")
        oc = seed_chunk(store, ref_id=other, text=CHUNK1)
        late = self._row(
            world,
            paper=other,
            chunk=oc,
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="t-drift",
        )
        for _ in range(200):
            out = dispatch(
                fake_ctx(
                    store,
                    {**meta, "coordinator_state": out.state},
                    events,
                ),
                job.ACCEPT_SPEC,
            )
            if isinstance(out, Done):
                break
            assert isinstance(out, Yield)
        else:
            raise AssertionError("dispatch never finished")

        acc = next(v for k, v in events if k == "meta")["acceptance"]
        assert acc["max_id"] == max(ids)
        assert late > acc["max_id"]
        assert acc["papers"] == [world["paper"]]
        assert other not in acc["papers"]

    def test_loose_z_verdict_counts_for_nothing(self, world: dict) -> None:
        """A re-accept under a looser z neither grants nor overturns — the
        default-z fail stands, same as the loose-bar case."""
        store = world["store"]
        quest = seed_ref(store, title="z quest", kind="quest")
        store.add_link(src_ref_id=world["paper"], dst_ref_id=quest, relation="serves")
        mid = self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="z-t",
        )
        self._accept_job(
            store,
            "z-t",
            False,
            "2026-10-24T10:00:00+00:00",
            papers=[world["paper"]],
            max_id=mid,
        )
        self._accept_job(
            store,
            "z-t",
            True,
            "2026-10-24T12:00:00+00:00",
            papers=[world["paper"]],
            max_id=mid,
            z=1.0,
        )

        assert (
            store.best_measure(
                world["ads"], serving=quest, sense="max", accepted_only=True
            )
            == []
        )

    def test_run_level_accept_refused_while_extract_jobs_live(
        self, world: dict
    ) -> None:
        """Sequence order is not commit order: while an extract job still
        carries the token, a row can hold an id under the frame's mark and
        commit after the draw — the run-level accept is refused."""
        store = world["store"]
        live = self._extract_ref(store, "live-run")
        queued = self._extract_ref(store, None)

        with pytest.raises(BadInput):
            job.accept_validate(store, {"extraction_run": "live-run"})
        # a queued job that has not checkpointed is caught by its default
        # token, survey-extract:<ref_id>
        with pytest.raises(BadInput):
            job.accept_validate(store, {"extraction_run": f"survey-extract:{queued}"})
        # paper-scoped stays open — it covers only that paper's share
        job.accept_validate(
            store, {"extraction_run": "live-run", "paper": world["paper"]}
        )

        store.add_tag(
            live,
            Tag.parse_strict("STATUS:succeeded"),
            set_by="system",
            replace_prefix=True,
        )
        job.accept_validate(store, {"extraction_run": "live-run"})

    def test_extract_liveness_is_the_terminal_status_tag(self, world: dict) -> None:
        """``finished_at`` does not decide liveness: a terminal STATUS tag
        frees the token even without it (legacy jobs), and a re-queued
        job blocks even with a stale ``finished_at``."""
        store = world["store"]
        legacy = self._extract_ref(store, "tok-legacy", status="failed")
        # the legacy shape is a terminal STATUS with no meta stamp —
        # add_tag stamps finished_at on the terminal write, so strip it
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta - 'finished_at' WHERE ref_id = %s",
                (legacy,),
            )
        job.accept_validate(store, {"extraction_run": "tok-legacy"})

        self._extract_ref(
            store,
            "tok-req",
            status="queued",
            finished_at="2026-10-24T13:00:00+00:00",
        )
        with pytest.raises(BadInput):
            job.accept_validate(store, {"extraction_run": "tok-req"})

        # the params.run_token branch of the token match
        self._extract_ref(store, "tok-par", via_params=True, status="running")
        with pytest.raises(BadInput):
            job.accept_validate(store, {"extraction_run": "tok-par"})

    def test_run_level_dispatch_refuses_a_live_token(self, world: dict) -> None:
        """validate fires at put; a job that sat queued gets the same
        refusal again at dispatch, before the draw."""
        store = world["store"]
        self._extract_ref(store, "t-live")
        self._row(
            world,
            paper=world["paper"],
            chunk=world["c1"],
            lit="95",
            text=CHUNK1,
            measurand=world["ads"],
            unit="eV",
            token="t-live",
        )
        done, _ = drive(
            {
                "job_type": "measure_accept",
                "executor": "coordinator",
                "params": {"extraction_run": "t-live"},
            },
            store,
            job.ACCEPT_SPEC,
        )
        assert not done.success
        assert "still live" in done.summary

    def test_pre_frame_checkpoint_yields_a_non_counting_verdict(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A checkpoint written before frame_* existed resumes to a
        verdict that omits coverage — accepted_runs reads that as
        non-counting rather than scoping coverage to rows live now."""
        store = world["store"]
        ids = [
            self._row(
                world,
                paper=world["paper"],
                chunk=world["c1"],
                lit=lit,
                text=CHUNK1,
                measurand=world["ads"],
                unit="eV",
                token="t-old",
            )
            for lit in ("95", "1000")
        ]
        monkeypatch.setattr(
            job,
            "route",
            lambda req: llm_result(
                json.dumps({"verdict": "correct", "reason": "matches"})
            ),
        )
        done, events = drive(
            {
                "job_type": "measure_accept",
                "executor": "coordinator",
                "params": {
                    "extraction_run": "t-old",
                    "reviews_per_slice": 10,
                    "bar": 0.5,
                },
                "coordinator_state": {
                    "sampled": ids,
                    "verdicts": {},
                    "idx": 0,
                    "total_rows": len(ids),
                    "run_token": "t-old",
                },
            },
            store,
            job.ACCEPT_SPEC,
        )
        assert done.success
        acc = next(v for k, v in events if k == "meta")["acceptance"]
        assert "papers" not in acc and "max_id" not in acc


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

    def test_markup_chunks_write_and_glyph_refusal_escalates(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr511458: a LaTeX span lands (literal/unit checks fold markup);
        gr512368: a glyph-damaged unit ('W mK^2' — the superscript minus
        read as K) is repaired and lands flagged, no longer refused."""
        store = world["store"]
        make_taxon(store, "radiative-forcing", unit="W/m^2", parent=world["root"])
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = %s::jsonb WHERE ref_id = %s",
                (json.dumps({"glyph_health": {"suspected": True}}), world["paper"]),
            )
            conn.commit()
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="ERFs of  $-0.7 \\pm 0.1 \\text{ W m}^{-2}$  were found.",
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=4,
            text="Reflection must rise by 3.7 W mK^2 globally over time.",
        )
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "radiative-forcing",
                                "literal": "-0.7 ± 0.1",
                                "unit": "W m-2",
                                "anchor": "0",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "radiative-forcing",
                                "literal": "3.7",
                                "unit": "W mK^2",
                                "anchor": "0",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["written"] == 2
        assert stats["write_refused"] == 0
        assert stats["flag:glyph_unit_repaired"] == 1
        esc = next(
            e
            for e in done.summary_meta["escalations"]
            if e.get("flag") == "glyph_unit_repaired"
        )
        assert "W mK^2" in esc["detail"]
        assert "W m-2" in esc["detail"]
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output"
        ]
        latex = next(r for r in rows if r["literal"] == "-0.7 ± 0.1")
        assert latex["extraction_status"] == "anchor_matched"
        assert latex["meta"]["extraction"]["prompt"] == "r3"
        repaired = next(r for r in rows if r["literal"] == "3.7")
        assert repaired["reported_unit"] == "W m-2"
        assert repaired["value_num"] == pytest.approx(3.7)
        assert any(
            e["rule"] == "glyph_suspected"
            for e in (repaired["meta"].get("escalation") or [])
        )


class TestR92Units:
    """The climate-orch r92 units-phase fixes (gr512365, gr512366,
    gr512370): each regression is the gripe's own printed cell or
    sentence as a fixture chunk."""

    @pytest.fixture(autouse=True)
    def _local_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.LOCAL, model="test-local", label="local")],
        )

    def _taxon_meta(self, store: Store, ref_id: int, extra: dict[str, Any]) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (json.dumps(extra), ref_id),
            )
            conn.commit()

    def test_below_baseline_measurand_stores_the_magnitude(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512365: tn511707's 'fraction below baseline' reads positive
        however the claim is worded — '54 percent below baseline'
        (sign=-1 channel) and a change table's '−54%' both store +54."""
        store = world["store"]
        reduction = make_taxon(
            store,
            "emission-reduction-potential-fraction-below-baseline",
            unit="%",
            parent=world["root"],
        )
        self._taxon_meta(store, reduction, {"sign_convention": "reduction"})
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text=(
                "The potential is estimated at 54 percent below baseline "
                "emissions of that year."
            ),
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=4,
            text="| Scenario | Emissions in 2050 |\n| Fossil | −54% |",
            chunk_kind="table",
        )
        measurand = "emission-reduction-potential-fraction-below-baseline"
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": measurand,
                                "literal": "54",
                                "unit": "%",
                                "anchor": "0",
                                "attribution": "own_work",
                                "sign": -1,
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": measurand,
                                "literal": "−54",
                                "unit": "%",
                                "anchor": "1",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == reduction
        ]
        assert len(rows) == 2
        assert {r["value_num"] for r in rows} == {54.0}

    def test_difference_measurand_converts_degc_as_a_delta(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512366: mx1162's '0.5°C' warming reduction stored 273.65 K —
        the °C -> K affine offset applied to a change. A ``difference``
        measurand converts °C deltas like K deltas."""
        store = world["store"]
        dtemp = make_taxon(
            store,
            "surface-temperature-change",
            unit="K",
            parent=world["root"],
        )
        self._taxon_meta(store, dtemp, {"difference": True})
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="Net warming could be reduced by about 0.5°C.",
        )
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "surface-temperature-change",
                                "literal": "0.5",
                                "unit": "°C",
                                "anchor": "0",
                                "attribution": "own_work",
                                "sign": -1,
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == dtemp
        ]
        (row,) = rows
        assert row["value_num"] == pytest.approx(-0.5)

    def test_absolute_temperature_taxon_keeps_the_offset(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same '0.5 °C' on an absolute-temperature measurand still
        converts with the offset — only ``difference`` taxa read deltas."""
        store = world["store"]
        tabs = make_taxon(store, "reaction-temperature", unit="K", parent=world["root"])
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="The bath was held at 0.5 °C throughout.",
        )
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "reaction-temperature",
                                "literal": "0.5",
                                "unit": "°C",
                                "anchor": "0",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == tabs
        ]
        (row,) = rows
        assert row["value_num"] == pytest.approx(273.65)

    def test_short_table_row_flags_table_misaligned(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512370: Beerling 2020 Table 1 — five header cells, four row
        cells; the reader took column 1 as National CDR. A row shorter
        than its header lands flagged ``table_misaligned`` on the row's
        escalation and the job counters."""
        store = world["store"]
        cdr = make_taxon(store, "national-cdr", unit="Gt yr-1", parent=world["root"])
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text=(
                "| Country | Cropland area | National CDR (Gt CO2 yr-1) | "
                "Silicate demand (Gt yr-1) | Cost (US$ t CO2-1) |\n"
                "| China | 10 | 0.13 | 102.1 |"
            ),
            chunk_kind="table",
        )
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "national-cdr",
                                "literal": "10",
                                "unit": "Gt yr-1",
                                "anchor": "1",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["flag:table_misaligned"] == 1
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == cdr
        ]
        (row,) = rows
        assert row["value_num"] == 10.0
        assert any(e["rule"] == "table_misaligned" for e in row["meta"]["escalation"])


class TestR92Recall:
    """The climate-orch r92 recall-phase fixes (gr512367, gr512368,
    gr512369, gr512371): each regression is the gripe's own printed
    cell or sentence as a fixture chunk."""

    @pytest.fixture(autouse=True)
    def _local_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "precis.utils.llm.router.resolve_chain",
            lambda *a, **k: [Rung(Transport.LOCAL, model="test-local", label="local")],
        )

    def test_sibling_values_with_identical_conditions_are_flagged(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512367: ALL/SEP/NEP MCB cooling rows carry identical
        conditions {CESM2, SSP2-4.5} — the region that told them apart
        was never written. Colliding siblings land flagged
        ``sibling_condition_collision``; a sibling with a distinct
        condition does not."""
        store = world["store"]
        dtemp = make_taxon(
            store, "surface-temperature-change", unit="K", parent=world["root"]
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text=(
                "ALL MCB gives -1.05 K, SEP gives -0.77 K and NEP gives "
                "-0.20 K under CESM2 SSP2-4.5. MRI-ESM2 gives -0.55 K."
            ),
        )

        def _result(literal: str, conditions: dict[str, str]) -> dict[str, Any]:
            return {
                "measurand": "surface-temperature-change",
                "literal": literal,
                "unit": "K",
                "anchor": "0",
                "attribution": "own_work",
                "conditions": conditions,
            }

        same = {"model": "CESM2", "scenario": "SSP2-4.5"}
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            _result("-1.05", same),
                            _result("-0.77", same),
                            _result("-0.20", same),
                            {
                                **_result("-0.55", {"model": "MRI-ESM2"}),
                                "anchor": "1",
                            },
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["flag:sibling_condition_collision"] == 3
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == dtemp
        ]
        assert len(rows) == 4
        flagged = {
            r["literal"]
            for r in rows
            if any(
                e["rule"] == "sibling_condition_collision"
                for e in (r["meta"].get("escalation") or [])
            )
        }
        assert flagged == {"-1.05", "-0.77", "-0.20"}

    def test_split_sign_cells_and_sup_minus_land(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512368 (1,2): a sign alone in a cell ('− | 2.48'), a
        sign-spaced cell ('− 1.91 ( ± | 0.63)') and an HTML superscript
        minus all reach the literal check as the signed number."""
        store = world["store"]
        rf = make_taxon(store, "radiative-forcing", unit="W m-2", parent=world["root"])
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="| CSIRO-Mk3L-1-2 | − | 2.48 |",
            chunk_kind="table",
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=4,
            text="| Model | ERF |\n| Ensemble median | − 1.91 ( ± | 0.63) |",
            chunk_kind="table",
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=5,
            text="The ensemble ERF is <sup>−</sup>1.91 W m-2 for this model.",
        )
        result = {
            "measurand": "radiative-forcing",
            "unit": "W m-2",
            "attribution": "own_work",
            "anchor": "0",
        }
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps({"results": [{**result, "literal": "-2.48"}]}),
                json.dumps(
                    {"results": [{**result, "literal": "-1.91 ± 0.63", "anchor": "1"}]}
                ),
                json.dumps({"results": [{**result, "literal": "-1.91"}]}),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["written"] == 3
        assert stats.get("drop:literal_not_in_span", 0) == 0
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == rf
        ]
        assert {r["value_num"] for r in rows} == {-2.48, -1.91}
        median = next(r for r in rows if r["literal"] == "-1.91 ± 0.63")
        assert median["value_err"] == pytest.approx(0.63)

    def test_gas_qualified_units_strip_to_a_condition(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512368 (3): 'Gt CO₂ yr⁻¹' and 'USD/t CO2' — the gas qualifier
        is not a unit pint can parse. It strips to the conditions dict as
        ``gas`` and the cleaned unit converts."""
        store = world["store"]
        cdr = make_taxon(store, "national-cdr", unit="Gt yr-1", parent=world["root"])
        cost = make_taxon(store, "mitigation-cost", unit="USD/t", parent=world["root"])
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="| China | 0.13 Gt CO₂ yr⁻¹ |",
            chunk_kind="table",
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=4,
            text="| Hydroxyl radical | $75-100 t-1 CO2 |",
            chunk_kind="table",
        )
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "national-cdr",
                                "literal": "0.13",
                                "unit": "Gt CO₂ yr⁻¹",
                                "anchor": "0",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "mitigation-cost",
                                "literal": "75-100",
                                "unit": "USD/t CO2",
                                "anchor": "0",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["written"] == 2
        rows = {
            r["measurand_ref_id"]: r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output"
        }
        assert rows[cdr]["value_num"] == pytest.approx(0.13)
        assert rows[cdr]["conditions"]["gas"] == "CO2"
        assert rows[cost]["value_low"] == pytest.approx(75.0)
        assert rows[cost]["value_high"] == pytest.approx(100.0)
        assert rows[cost]["conditions"]["gas"] == "CO2"

    def test_glyph_damaged_unit_is_repaired(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512368 (3): 'W mK^2' on a glyph-flagged paper is 'W m-2' with
        the superscript minus read as K — the write is retried repaired
        and flagged ``glyph_unit_repaired`` instead of refused."""
        store = world["store"]
        rf = make_taxon(store, "radiative-forcing", unit="W/m^2", parent=world["root"])
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = %s::jsonb WHERE ref_id = %s",
                (json.dumps({"glyph_health": {"suspected": True}}), world["paper"]),
            )
            conn.commit()
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text="Reflection must rise by 3.7 W mK^2 globally over time.",
        )
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps(
                    {
                        "results": [
                            {
                                "measurand": "radiative-forcing",
                                "literal": "3.7",
                                "unit": "W mK^2",
                                "anchor": "0",
                                "attribution": "own_work",
                            }
                        ]
                    }
                ),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["written"] == 1
        assert stats["write_refused"] == 0
        assert stats["flag:glyph_unit_repaired"] == 1
        esc = next(
            e
            for e in done.summary_meta["escalations"]
            if e.get("flag") == "glyph_unit_repaired"
        )
        assert "W mK^2" in esc["detail"]
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == rf
        ]
        (row,) = rows
        assert row["value_num"] == pytest.approx(3.7)
        assert row["reported_unit"] == "W m-2"

    def test_design_targets_and_comparators_drop(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512369: 'such that the MCB ERF is -1.8' is a design target,
        'intended to produce' a protocol forcing — both drop as
        cited_context; the achieved value still lands."""
        store = world["store"]
        rf = make_taxon(store, "radiative-forcing", unit="W m-2", parent=world["root"])
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=3,
            text=(
                "We specify the strength of the forcing such that the "
                "MCB ERF is -1.8 W m-2."
            ),
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=4,
            text=(
                "The G4sea-salt protocol is intended to produce a forcing "
                "of -2.0 W m-2."
            ),
        )
        _seed_body_chunk(
            store,
            world["paper"],
            ord_=5,
            text="The achieved MCB ERF was -1.8 W m-2 in the CESM2 run.",
        )
        result = {
            "measurand": "radiative-forcing",
            "unit": "W m-2",
            "attribution": "own_work",
            "anchor": "0",
        }
        replies = iter(
            [
                '{"results": []}',
                '{"results": []}',
                '{"results": []}',
                json.dumps({"results": [{**result, "literal": "-1.8"}]}),
                json.dumps({"results": [{**result, "literal": "-2.0"}]}),
                json.dumps({"results": [{**result, "literal": "-1.8"}]}),
            ]
        )
        monkeypatch.setattr(
            job, "route", lambda req: llm_result(next(replies, '{"results": []}'))
        )
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["drop:cited_context"] == 2
        assert stats["written"] == 1
        rows = [
            r
            for r in store.measures_for(world["paper"], include_superseded=True)
            if r["direction"] == "output" and r["measurand_ref_id"] == rf
        ]
        (row,) = rows
        assert row["value_num"] == -1.8

    def test_empty_reader_reply_is_visible(
        self, world: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr512371: a parsed reply that yields no proposals and no
        drops is named per chunk — the empty pc5155615 result that lost
        five approved rows with no trace."""
        store = world["store"]
        monkeypatch.setattr(job, "route", lambda req: llm_result('{"results": []}'))
        done, _ = drive(_extract_meta(world, skip_accept=True), store, job.SPEC)
        assert done.success
        stats = done.summary_meta["stats"]
        assert stats["empty_result"] == 3
        esc = done.summary_meta["escalations"]
        assert {e["kind"] for e in esc} == {"empty_result"}
        assert {e["chunk"] for e in esc} == {world["c1"], world["c2"], world["c3"]}


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
