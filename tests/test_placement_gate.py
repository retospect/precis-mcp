"""Capability placement for the dark-factory job types (td543917).

``run_slice`` / ``land_slice`` / ``release_cut`` / ``release_deploy`` declare
``ServiceSpec.placement`` host capabilities; the claim gate holds them
``STATUS:queued`` on a host whose heartbeat does not advertise every token
(check-only, nothing reserved), and the sweeper's unschedulable alert pages
when no single live host advertises them all. jo543902: an ssh_node worker
with no git checkout around its package claimed a ``run_slice`` and failed it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import ssh_node
from precis.workers.registry import SERVICES_BY_NAME, ServiceKind
from precis.workers.sweeper import _alert_unschedulable_jobs

SLICE_HOST = {"repo_checkout": 1, "claude_bin": 1, "git": 1}


def _queue(store: Store, job_type: str, params: dict[str, Any] | None = None) -> int:
    meta = {"job_type": job_type, "executor": "ssh_node", "params": params or {}}
    ref = store.insert_ref(kind="job", slug=None, title=job_type, meta=meta)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def _advertise(store: Store, host: str, slots: dict[str, int | None]) -> None:
    store.sync_host_resource_slots(host, slots)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host,),
        )
        conn.commit()


def _status(store: Store, ref_id: int) -> str | None:
    for t in store.tags_for(ref_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _alert_open(store: Store, fingerprint: str) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'alert' AND retired_at IS NULL "
            "AND resolved_at IS NULL AND meta->>'fingerprint' = %s LIMIT 1",
            (fingerprint,),
        ).fetchone()
    return row is not None


# ── registry ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "placement"),
    [
        ("run_slice", {"repo_checkout", "claude_bin", "git"}),
        ("land_slice", {"repo_checkout", "git"}),
        ("release_cut", {"repo_checkout", "git"}),
        ("release_deploy", {"repo_checkout", "git"}),
        ("plan_dispatch", set()),
        ("ci_verdict_poll", set()),
        ("gripe_validate", set()),
    ],
)
def test_factory_job_types_declare_placement(name: str, placement: set[str]) -> None:
    spec = SERVICES_BY_NAME[name]
    assert spec.kind is ServiceKind.JOB
    assert spec.category == "jobs"
    assert spec.doc_skill == "precis-job-help"
    assert set(spec.placement) == placement
    # Check-only: no counted reservation (a multi-hour slice would hold a
    # capacity-1 git/claude slot and serialise every other user of it).
    assert spec.requires == frozenset()


# ── claim gate on the real ssh_node pass ───────────────────────────


@pytest.fixture
def _ssh_host(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Run ssh_node as ``node-a`` (no PRECIS_NODE pin) with the dispatch
    stubbed; returns the ids handed to ``_run_one``."""
    monkeypatch.setenv("PRECIS_HOST_NAME", "node-a")
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    ran: list[int] = []

    def _fake_run_one(ctx: Any) -> bool:
        ran.append(int(ctx.ref_id))
        return True

    monkeypatch.setattr(ssh_node, "_run_one", _fake_run_one)
    return ran


def test_ssh_node_refuses_run_slice_on_host_without_checkout(
    store: Store, _ssh_host: list[int]
) -> None:
    _advertise(store, "node-a", {"claude_bin": 1, "git": 1})
    jid = _queue(store, "run_slice", {"todo_id": 1})
    out = ssh_node.run_ssh_node_pass(store, limit=5)
    assert out["claimed"] == 0
    assert _ssh_host == []
    assert _status(store, jid) == "queued"
    assert "lease_host" not in _meta(store, jid)


def test_ssh_node_claims_run_slice_on_slice_host(
    store: Store, _ssh_host: list[int]
) -> None:
    _advertise(store, "node-a", SLICE_HOST)
    jid = _queue(store, "run_slice", {"todo_id": 1})
    out = ssh_node.run_ssh_node_pass(store, limit=5)
    assert out["claimed"] == 1
    assert _ssh_host == [jid]
    # Nothing reserved: the slots stay at full capacity.
    assert "reserved" not in _meta(store, jid)
    free = {s.resource: s.free for s in store.resource_slots_for_host("node-a")}
    assert free == SLICE_HOST


def test_ssh_node_land_slice_gate_reads_the_pinned_host(
    store: Store, _ssh_host: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """land_slice inherits the run's host via target_node: the gate checks
    that host's advertisement, not the claimer's."""
    monkeypatch.setenv("PRECIS_NODE", "node-b")
    _advertise(store, "node-b", {"repo_checkout": 1})  # no git there
    jid = _queue(store, "land_slice", {"todo_id": 1, "target_node": "node-b"})
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 0
    _advertise(store, "node-b", {"repo_checkout": 1, "git": 1})
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 1
    assert _ssh_host == [jid]


def test_ssh_node_gate_leaves_other_job_types_alone(
    store: Store, _ssh_host: list[int]
) -> None:
    """A job_type with no placement is claimed on a host advertising nothing."""
    jid = _queue(store, "demo")
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 1
    assert _ssh_host == [jid]


# ── sweeper: unschedulable alert ───────────────────────────────────


def test_unplaceable_run_slice_alerts(store: Store) -> None:
    _advertise(store, "node-a", {"claude_bin": 1, "git": 1})
    jid = _queue(store, "run_slice", {"todo_id": 1})
    assert _alert_unschedulable_jobs(store) == 1
    assert _alert_open(store, f"unschedulable:{jid}")


def test_capabilities_split_across_hosts_still_alert(store: Store) -> None:
    """Every token is advertised somewhere, but no one host has them all."""
    _advertise(store, "node-a", {"repo_checkout": 1})
    _advertise(store, "node-b", {"claude_bin": 1, "git": 1})
    jid = _queue(store, "run_slice", {"todo_id": 1})
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}")


def test_alert_resolves_once_a_slice_host_appears(store: Store) -> None:
    jid = _queue(store, "run_slice", {"todo_id": 1})
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}")
    _advertise(store, "node-a", SLICE_HOST)
    assert _alert_unschedulable_jobs(store) == 0
    assert not _alert_open(store, f"unschedulable:{jid}")


def test_pinned_release_deploy_alerts_when_its_host_lacks_git(store: Store) -> None:
    """Placement applies to pinned rows too, judged on the pinned host."""
    _advertise(store, "node-a", {"repo_checkout": 1, "git": 1})
    _advertise(store, "node-b", {"repo_checkout": 1})
    jid = _queue(store, "release_deploy", {"todo_id": 1, "target_node": "node-b"})
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}")
