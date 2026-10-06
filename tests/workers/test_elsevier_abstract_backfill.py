"""Explicit cohort requeue preserves bodies and events, then claims despite hash."""

import pytest

from precis.errors import BadInput
from precis.workers.fetch_oa import claim_stubs_to_fetch
from precis.workers.job_types.elsevier_abstract_backfill import requeue
from tests.ingest.test_paper_hygiene import _stamp_pdf


def test_preview_backfill_dryrun_then_idempotent_requeue(store):
    ref = store.insert_ref(
        kind="paper", slug="synthetic-preview", title="Synthetic preview"
    )
    _stamp_pdf(store, ref.id, "a" * 64)
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers(ref_id,id_kind,id_value,source) VALUES (%s,'doi','10.1016/j.synthetic.2026.1','test')",
            (ref.id,),
        )
        conn.execute(
            "INSERT INTO chunks(ref_id,ord,chunk_kind,text) VALUES (%s,0,'paragraph','Synthetic abstract only')",
            (ref.id,),
        )
    store.append_event(ref.id, source="fetcher:elsevier", event="fetch_ok", payload={})
    assert requeue(store, [ref.id], expected_count=1)["eligible"] == 1
    with store.pool.connection() as conn:
        assert not claim_stubs_to_fetch(conn, limit=1, explore_fraction=0)
    assert requeue(store, [ref.id], expected_count=1, dry_run=False)["eligible"] == 1
    assert requeue(store, [ref.id], expected_count=1, dry_run=False)["eligible"] == 0
    with store.pool.connection() as conn:
        assert (
            conn.execute(
                "SELECT text FROM chunks WHERE ref_id=%s AND ord=0", (ref.id,)
            ).fetchone()[0]
            == "Synthetic abstract only"
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM ref_events WHERE ref_id=%s AND source='fetcher:elsevier'",
                (ref.id,),
            ).fetchone()[0]
            == 1
        )
        assert [
            s.ref_id for s in claim_stubs_to_fetch(conn, limit=1, explore_fraction=0)
        ] == [ref.id]


def test_backfill_count_guard_before_write(store):
    with pytest.raises(BadInput):
        requeue(store, [1, 1], expected_count=2, dry_run=False)
    with pytest.raises(BadInput):
        requeue(store, [1], expected_count=2007, dry_run=False)


@pytest.mark.parametrize(
    "dry_run,expected_count", [("false", 1), (False, True), (False, 0)]
)
def test_backfill_invalid_controls_refuse(store, dry_run, expected_count):
    with pytest.raises(BadInput):
        requeue(store, [1], expected_count=expected_count, dry_run=dry_run)


def test_backfill_registered_without_model_requirement():
    from precis.workers.job_types import get_job_type, known_job_types

    spec = get_job_type("elsevier_abstract_backfill")
    assert spec is not None
    assert spec.dispatch is not None
    assert not spec.requires
    assert "elsevier_abstract_backfill" in known_job_types()


@pytest.mark.parametrize(
    "doi,body,source",
    [
        ("10.1234/synthetic", "Synthetic preview", "fetcher:elsevier"),
        ("10.1016/synthetic", "x" * 5000, "fetcher:elsevier"),
        ("10.1016/synthetic", "Synthetic preview", "fetcher:core"),
    ],
)
def test_backfill_unconfirmed_shape_not_eligible(store, doi, body, source):
    ref = store.insert_ref(
        kind="paper", slug="synthetic-ineligible", title="Synthetic ineligible"
    )
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers(ref_id,id_kind,id_value,source) VALUES (%s,'doi',%s,'test')",
            (ref.id, doi),
        )
        conn.execute(
            "INSERT INTO chunks(ref_id,ord,chunk_kind,text) VALUES (%s,0,'paragraph',%s)",
            (ref.id, body),
        )
    store.append_event(ref.id, source=source, event="fetch_ok", payload={})
    assert requeue(store, [ref.id], expected_count=1, dry_run=False)["eligible"] == 0
    with store.pool.connection() as conn:
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id=%s", (ref.id,)
        ).fetchone()[0]
    assert "markup_refetch" not in meta
