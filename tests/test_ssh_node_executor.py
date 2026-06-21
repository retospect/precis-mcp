"""ssh_node executor — claim → lease → STATUS:running → plugin dispatch.

DB-backed (the claim is real SQL). A monkeypatched job_type stands in
for precis-dft's gpaw_relax so the test exercises the executor flow
without that plugin installed.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import EXECUTOR_PROVIDES, ssh_node
from precis.workers.job_types import JobTypeSpec

pytestmark = pytest.mark.db


# ── helpers ──────────────────────────────────────────────────────


def _mk_job(
    store: Store,
    *,
    executor: str = "ssh_node",
    job_type: str = "fake_relax",
    params: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="fake relax job",
        meta={"executor": executor, "job_type": job_type, "params": params or {}},
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")
    return int(ref.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0] or {})


def _spec(*, dispatch: Any, name: str = "fake_relax") -> JobTypeSpec:
    def _run(*_a: Any, **_k: Any) -> str:
        return "noop"

    return JobTypeSpec(
        name=name,
        params_schema={"type": "object"},
        compatible_executors=frozenset({"ssh_node"}),
        requires=frozenset({"has_gpaw"}),
        description="fake relax for tests",
        run=_run,
        dispatch=dispatch,
    )


# ── tests ────────────────────────────────────────────────────────


def test_provides_registered() -> None:
    assert "ssh_node" in EXECUTOR_PROVIDES
    assert "has_gpaw" in EXECUTOR_PROVIDES["ssh_node"]


def test_lease_seconds_from_wall_seconds() -> None:
    assert ssh_node._lease_seconds({}) == ssh_node._LEASE_FLOOR_S
    big = {"params": {"resources": {"wall_seconds": 100_000}}}
    assert ssh_node._lease_seconds(big) == 100_000 + ssh_node._LEASE_MARGIN_S


def test_claims_and_dispatches_success(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _dispatch(ctx: Any, _spec: Any) -> None:
        ctx.append_chunk("job_summary", "fake relax done")
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"
    assert "lease_until" in _meta(store, rid)
    blocks = store.list_blocks_for_ref(rid)
    assert any("fake relax done" in b.text for b in blocks)


def test_skips_jobs_for_other_executors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=lambda c, s: None)
    )
    rid = _mk_job(store, executor="claude_inproc")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 0
    assert _status(store, rid) == "queued"  # untouched


def test_missing_dispatch_records_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A job_type with no dispatch — ssh_node runs plugin dispatchers only.
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: _spec(dispatch=None))
    rid = _mk_job(store)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 1
    assert _status(store, rid) == "failed"


def test_unknown_job_type_records_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: None)
    rid = _mk_job(store, job_type="nope")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 1
    assert _status(store, rid) == "failed"


def test_empty_queue_is_noop(store: Store) -> None:
    assert ssh_node.run_ssh_node_pass(store, limit=2) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }
