"""The ``survey_extract`` / ``measure_accept`` worker dispatches end to
end on the dev DB (docs/backlog/measures-substrate.md, "Next slice"):
a fixture paper extracted through canned model replies, the spawned
acceptance child, the reviews ledger, and migration 0212.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis.store import Migrator, Store
from precis.store._measures_ops import MeasureAnchor, MeasureSpec
from precis.workers.executors._yield import Yield
from precis.workers.job_types import survey_extract as job
from tests.extract_helpers import (
    CHUNK1,
    drive,
    fake_ctx,
    fe_reply,
    llm_result,
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
