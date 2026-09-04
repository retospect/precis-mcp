"""``executors._common.set_meta`` must survive non-finite floats.

Regression pin for the batch-2 seed shredding
(docs/backlog/autocatpath-seed-child-killed-shredding.md): Python's json
round-trips ``Infinity``/``NaN`` but Postgres ``jsonb`` rejects the tokens,
so an unsanitized ``Jsonb(fields)`` write raised
``InvalidTextRepresentation`` — discarding 48 finished GPU seed results and
misclassifying them as ``infra:child-killed``. Real PG on purpose: a mocked
store cannot catch this class of bug (see ``psycopg_percent_like_
fakestore_gap``).

Also covers ``record_failure``'s ``meta.error`` mirror (gr309200): before
this, the only place a job's failure reason landed was a ``job_event``
chunk, so a downstream consumer that reads only ``refs.meta`` (e.g. quest's
auto-filed infra gripe, ``precis.quest.compute._file_infra_gripe``) had
nothing to show.
"""

from __future__ import annotations

import math

from precis.store.store import Store
from precis.workers.executors import _common


class TestFiniteJson:
    def test_maps_non_finite_floats_to_none_recursively(self) -> None:
        out = _common._finite_json(
            {
                "a": float("inf"),
                "b": [1.5, float("-inf"), {"c": float("nan")}],
                "d": (2, float("inf")),
            }
        )
        assert out == {"a": None, "b": [1.5, None, {"c": None}], "d": [2, None]}

    def test_finite_values_pass_through_untouched(self) -> None:
        payload = {"x": 1.25, "y": "inf", "z": [0, -3, True, None]}
        assert _common._finite_json(payload) == payload
        assert math.isfinite(_common._finite_json(1e308))


def test_set_meta_persists_a_payload_carrying_inf_and_nan(store: Store) -> None:
    # The exact shape that killed batch 2: trust evidence with
    # min_dist_A = inf (a single-atom adsorbate has no pair distance).
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('job', 'seed') RETURNING ref_id"
        ).fetchone()
        assert row is not None
        ref_id = int(row[0])
        _common.set_meta(
            conn,
            ref_id,
            partial={
                "states": {
                    "N": {"evidence": {"min_dist_A": float("inf"), "state": "N"}}
                },
                "score": float("nan"),
            },
        )
        got = conn.execute(
            "SELECT meta->'partial' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert got is not None
        assert got[0] == {
            "states": {"N": {"evidence": {"min_dist_A": None, "state": "N"}}},
            "score": None,
        }


class TestRecordFailureErrorMeta:
    def test_stamps_reason_into_meta_error_alongside_failure_class(
        self, store: Store
    ) -> None:
        job = store.insert_ref(kind="job", slug=None, title="struct_relax")
        _common.record_failure(
            store,
            job.id,
            "container exited with code 137 (OOM-killed)",
            gripe_rollback=None,
            failure_class="infra",
        )
        meta = store.fetch_refs_by_ids({job.id})[job.id].meta or {}
        assert meta.get("failure_class") == "infra"
        assert meta.get("error") == "container exited with code 137 (OOM-killed)"

    def test_truncates_an_oversized_reason_to_the_meta_cap(self, store: Store) -> None:
        job = store.insert_ref(kind="job", slug=None, title="struct_relax")
        reason = "x" * (_common._ERROR_META_CAP + 250)
        _common.record_failure(
            store, job.id, reason, gripe_rollback=None, failure_class="infra"
        )
        meta = store.fetch_refs_by_ids({job.id})[job.id].meta or {}
        assert meta.get("error") == reason[: _common._ERROR_META_CAP]
        assert len(meta["error"]) == _common._ERROR_META_CAP

        # the full, untruncated reason still lands in the job_event chunk
        chunks = store.chunks.list_chunks_for_ref(job.id)
        job_events = [c for c in chunks if c.chunk_kind == "job_event"]
        assert len(job_events) == 1
        assert job_events[0].text == reason

    def test_no_meta_error_when_no_failure_class_given(self, store: Store) -> None:
        """Unclassified failures keep their old shape — no ``failure_class``
        means no meta write at all (still logged to the job_event chunk)."""
        job = store.insert_ref(kind="job", slug=None, title="struct_relax")
        _common.record_failure(
            store, job.id, "some non-infra reason", gripe_rollback=None
        )
        meta = store.fetch_refs_by_ids({job.id})[job.id].meta or {}
        assert "error" not in meta
        assert "failure_class" not in meta
