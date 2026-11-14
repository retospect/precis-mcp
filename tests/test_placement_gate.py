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

SLICE_HOST: dict[str, int | None] = {
    "repo_checkout": 1,
    "claude_bin": 1,
    "builder": 1,
    "git": 1,
}


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
        ("run_slice", {"repo_checkout", "builder", "git"}),
        ("land_slice", {"repo_checkout", "git"}),
        ("release_cut", {"repo_checkout", "git"}),
        ("release_deploy", {"repo_checkout", "git"}),
        ("release_confirm", {"repo_checkout", "git"}),
        ("ci_red_digest", {"repo_checkout", "git"}),
        ("plan_dispatch", set()),
        ("factory_rescue_tick", set()),
        ("ci_verdict_poll", set()),
        ("release_poll", set()),
        ("gripe_validate", set()),
        # gr592838: draft_export compiles through latexmk — a host
        # capability, so a TeX-less claim waits for a capable host.
        ("draft_export", {"latexmk"}),
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


ENGINE_JOB_TYPES = [
    "autocatpath_explore",
    "autocatpath_seed",
    "autocatpath_aggregate",
    "autocatpath_kinetics",
    "autocatpath_modes",
    "autocatpath_revalidate",
    "surface_coverage_scan",
]

#: Expected ``ServiceSpec.placement`` per engine job type (gr543909 for
#: ``autocatpath``; gr548810 adds ``has_mace`` to the two minted unpinned
#: whose compute loads the MLIP backend).
ENGINE_PLACEMENT = {
    "autocatpath_explore": {"autocatpath"},
    "autocatpath_seed": {"autocatpath"},
    "autocatpath_aggregate": {"autocatpath"},
    "autocatpath_kinetics": {"autocatpath", "has_mace"},
    "autocatpath_modes": {"autocatpath", "has_mace"},
    "autocatpath_revalidate": {"autocatpath"},
    "surface_coverage_scan": {"autocatpath"},
}


@pytest.mark.parametrize(("name", "placement"), sorted(ENGINE_PLACEMENT.items()))
def test_engine_job_types_declare_autocatpath_placement(
    name: str, placement: set[str]
) -> None:
    """gr543909: every job_type whose dispatch imports the engine is gated."""
    spec = SERVICES_BY_NAME[name]
    assert spec.kind is ServiceKind.JOB
    assert spec.placement == frozenset(placement)
    assert spec.requires == frozenset()


def test_engine_job_types_cover_the_pathway_entry_points() -> None:
    """A new ``precis_pathway`` job_type must join the gated set."""
    import tomllib
    from pathlib import Path

    eps = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["entry-points"]["precis.job_types"]
    pathway = {k for k, v in eps.items() if v.startswith("precis_pathway.")}
    assert pathway == set(ENGINE_JOB_TYPES)


def test_unpinned_coverage_scan_waits_for_an_engine_node(
    store: Store, _ssh_host: list[int]
) -> None:
    """gr543909: an unpinned ``surface_coverage_scan`` claimed by a node
    without the engine failed in 0.1 s (ModuleNotFoundError). It now stays
    queued there and is claimed once the node advertises ``autocatpath``."""
    _advertise(store, "node-a", {"git": 1})
    jid = _queue(store, "surface_coverage_scan")
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 0
    assert _ssh_host == []
    assert _status(store, jid) == "queued"
    assert "lease_host" not in _meta(store, jid)
    _advertise(store, "node-a", {"git": 1, "autocatpath": 1})
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 1
    assert _ssh_host == [jid]


@pytest.mark.parametrize("name", ["autocatpath_kinetics", "autocatpath_modes"])
def test_unpinned_backend_job_waits_for_a_mace_node(
    store: Store, _ssh_host: list[int], name: str
) -> None:
    """gr548810: the gateway's catch-all ``--profile all`` worker runs
    ``job_ssh_node`` and has the engine but no ``mace`` backend — it
    claimed unpinned ``autocatpath_kinetics`` rows and re-solved them
    into garbage. The ``has_mace`` placement holds the row queued until
    a backend host claims it."""
    _advertise(store, "node-a", {"git": 1, "autocatpath": 1})
    jid = _queue(store, name)
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 0
    assert _ssh_host == []
    assert _status(store, jid) == "queued"
    assert "lease_host" not in _meta(store, jid)
    _advertise(store, "node-a", {"git": 1, "autocatpath": 1, "has_mace": 1})
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 1
    assert _ssh_host == [jid]


@pytest.mark.parametrize("name", ["autocatpath_revalidate", "autocatpath_aggregate"])
def test_engine_only_job_still_claims_without_mace(
    store: Store, _ssh_host: list[int], name: str
) -> None:
    """Engine job types that load no MLIP backend keep the
    ``autocatpath``-only gate — they must not strand on a mace-less
    engine host."""
    _advertise(store, "node-a", {"autocatpath": 1})
    jid = _queue(store, name)
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 1
    assert _ssh_host == [jid]


def test_unpinned_coverage_scan_alerts_when_no_host_has_the_engine(
    store: Store,
) -> None:
    _advertise(store, "node-a", {"git": 1})
    jid = _queue(store, "surface_coverage_scan")
    assert _alert_unschedulable_jobs(store) == 1
    assert _alert_open(store, f"unschedulable:{jid}")


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


# ── lane → <a>_bin substitution (gr676054) ─────────────────────────


def test_adapter_placement_tokens_cover_every_slice_adapter() -> None:
    """The ``builder`` → ``<a>_bin`` map is restated in
    ``executors._common`` (``job_types`` pulls it, never the reverse): it
    must name every adapter ``run_slice`` can launch, and each token must
    be a lane-bin token the capability probe advertises — a token no
    probe emits would strand that lane's jobs on every host."""
    from precis.workers import capability_probe
    from precis.workers.executors._common import _ADAPTER_PLACEMENT_TOKENS
    from precis.workers.job_types._slice_adapters import ADAPTERS

    assert set(_ADAPTER_PLACEMENT_TOKENS) == set(ADAPTERS)
    assert set(_ADAPTER_PLACEMENT_TOKENS.values()) <= set(
        capability_probe.LANE_BIN_TOKENS
    )


@pytest.mark.parametrize(
    ("meta", "lane_adapters", "expected"),
    [
        # No lane named → the ``builder`` floor stands.
        ({"job_type": "run_slice"}, None, {"repo_checkout", "builder", "git"}),
        # A stamped adapter (a prior submit resolved the lane) swaps the
        # floor for its bin token — no lane table needed.
        (
            {"job_type": "run_slice", "adapter": "kimi_tree"},
            None,
            {"repo_checkout", "kimi_bin", "git"},
        ),
        # An unknown adapter stamp falls back to the floor, not a
        # phantom token.
        (
            {"job_type": "run_slice", "adapter": "no_such"},
            None,
            {"repo_checkout", "builder", "git"},
        ),
        # A ``params.lane`` pin resolves through the lane→adapter map.
        (
            {"job_type": "run_slice", "params": {"lane": "kimi"}},
            {"kimi": "kimi_tree"},
            {"repo_checkout", "kimi_bin", "git"},
        ),
        # Lane the map can't resolve → the floor stands (the claim path
        # then leaves it to run_slice's pinned-bin park).
        (
            {"job_type": "run_slice", "params": {"lane": "kimi"}},
            {"codex": "codex_exec"},
            {"repo_checkout", "builder", "git"},
        ),
        (
            {"job_type": "run_slice", "params": {"lane": "kimi"}},
            None,
            {"repo_checkout", "builder", "git"},
        ),
        # The adapter stamp wins over a lane pin — it is the resolved
        # truth, the pin only a fallback.
        (
            {
                "job_type": "run_slice",
                "adapter": "codex_exec",
                "params": {"lane": "kimi"},
            },
            {"kimi": "kimi_tree"},
            {"repo_checkout", "codex_bin", "git"},
        ),
        # A non-run_slice spec never substitutes — no ``builder`` floor
        # to swap, and an adapter stamp is irrelevant to it.
        (
            {"job_type": "land_slice", "adapter": "kimi_tree"},
            None,
            {"repo_checkout", "git"},
        ),
        # A job_type with no spec places nowhere.
        ({"job_type": "no_such_type"}, None, set()),
    ],
)
def test_placement_requires_substitutes_the_lane_bin(
    meta: dict[str, Any],
    lane_adapters: dict[str, str] | None,
    expected: set[str],
) -> None:
    from precis.workers.executors._common import placement_requires

    assert placement_requires(meta, lane_adapters) == frozenset(expected)


def test_placement_gap_reads_the_substituted_token() -> None:
    """``placement_gap`` (the sweeper alert + plan-tick note) judges the
    lane's bin token, not the ``builder`` floor: a kimi-pinned row fits a
    kimi host, and a host carrying only ``builder``/``claude_bin`` is a
    gap that names the kimi requirement."""
    from precis.workers.executors._common import placement_gap

    meta = {"job_type": "run_slice", "params": {"lane": "kimi"}}
    lane_map = {"kimi": "kimi_tree"}
    kimi_host = {"repo_checkout", "kimi_bin", "git"}
    builder_only = {"repo_checkout", "builder", "claude_bin", "git"}
    assert placement_gap(meta, {"h1": kimi_host}, lane_map) == frozenset()
    assert placement_gap(meta, {"h1": builder_only}, lane_map) == frozenset(
        {"repo_checkout", "kimi_bin", "git"}
    )
    # The map absent → the floor stands and the builder host fits.
    assert placement_gap(meta, {"h1": builder_only}) == frozenset()
    assert placement_gap(meta, {"h1": kimi_host}) == frozenset(
        {"repo_checkout", "builder", "git"}
    )


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (
            {"job_type": "run_slice", "params": {"repo": "catpath"}},
            {"repo_checkout", "builder", "git", "repo_checkout_catpath"},
        ),
        # Composes with the lane-bin substitution.
        (
            {"job_type": "run_slice", "params": {"repo": "catpath", "lane": "kimi"}},
            {"repo_checkout", "kimi_bin", "git", "repo_checkout_catpath"},
        ),
        (
            {"job_type": "land_slice", "params": {"repo": "catpath"}},
            {"repo_checkout", "git", "repo_checkout_catpath"},
        ),
        # A repo with no profile still adds its token: fail closed.
        (
            {"job_type": "run_slice", "params": {"repo": "nope"}},
            {"repo_checkout", "builder", "git", "repo_checkout_nope"},
        ),
        # The default repo, an empty or non-string value, and no repo at
        # all keep the plain tokens.
        (
            {"job_type": "run_slice", "params": {"repo": "precis"}},
            {"repo_checkout", "builder", "git"},
        ),
        (
            {"job_type": "land_slice", "params": {"repo": "precis"}},
            {"repo_checkout", "git"},
        ),
        (
            {"job_type": "run_slice", "params": {"repo": ""}},
            {"repo_checkout", "builder", "git"},
        ),
        ({"job_type": "land_slice", "params": {}}, {"repo_checkout", "git"}),
    ],
)
def test_placement_requires_adds_the_second_repo_token(
    meta: dict[str, Any], expected: set[str]
) -> None:
    """A second-repo slice job must require ``repo_checkout_<repo>``; if it
    does not, any host with ``repo_checkout`` + ``git`` claims it and the job
    fails at submit for want of a clone."""
    from precis.workers.executors._common import placement_requires

    assert placement_requires(meta, {"kimi": "kimi_tree"}) == frozenset(expected)


def test_second_repo_job_needs_a_host_with_the_repo_token() -> None:
    """A host advertising everything except ``repo_checkout_catpath`` fails
    the gate and ``placement_gap`` names the token; breaking this lets a
    builder host without a clone claim the job."""
    from precis.workers.executors._common import _placement_ok, placement_gap

    meta = {"job_type": "run_slice", "params": {"repo": "catpath"}}
    base = {"repo_checkout", "builder", "git"}
    full = base | {"repo_checkout_catpath"}
    assert not _placement_ok(meta, {"h1": base}, "h1")
    assert _placement_ok(meta, {"h1": full}, "h1")
    assert placement_gap(meta, {"h1": base}) == frozenset(full)
    assert placement_gap(meta, {"h1": base, "h2": full}) == frozenset()
    assert placement_gap(meta, {}) == frozenset(full)


def test_lane_adapter_map_reads_llm_account(store: Store) -> None:
    """``lane_adapter_map`` is the claim/sweeper/plan-tick lookup that
    turns ``params.lane`` into an adapter — it reads ``llm_account``
    (the seeded lane rows)."""
    from precis.workers.executors._common import lane_adapter_map

    with store.pool.connection() as conn:
        lane_map = lane_adapter_map(conn)
    assert lane_map["claude-sub"] == "claude_p"
    assert lane_map["codex"] == "codex_exec"
    assert lane_map["devin"] == "devin_tree"
    assert lane_map["kimi"] == "kimi_tree"


def test_lane_pinned_run_slice_waits_for_its_adapter_bin(
    store: Store, _ssh_host: list[int]
) -> None:
    """gr676054: on the claim path a ``params.lane`` pin turns the
    ``builder`` floor into the lane's ``<a>_bin`` token — a builder host
    carrying only claude holds a kimi-pinned row queued until a kimi bin
    shows up (the lane table is the seeded ``llm_account``)."""
    _advertise(
        store, "node-a", {"repo_checkout": 1, "builder": 1, "claude_bin": 1, "git": 1}
    )
    jid = _queue(store, "run_slice", {"todo_id": 1, "lane": "kimi"})
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 0
    assert _ssh_host == []
    assert _status(store, jid) == "queued"
    _advertise(store, "node-a", {"repo_checkout": 1, "kimi_bin": 1, "git": 1})
    assert ssh_node.run_ssh_node_pass(store, limit=5)["claimed"] == 1
    assert _ssh_host == [jid]


def test_lane_pinned_run_slice_alert_heals_on_a_matching_bin_host(
    store: Store,
) -> None:
    """The sweeper's unschedulable alert applies the same substitution:
    a kimi-pinned row alerts on a claude-only fleet and heals when a
    kimi-capable host appears."""
    _advertise(
        store, "node-a", {"repo_checkout": 1, "builder": 1, "claude_bin": 1, "git": 1}
    )
    jid = _queue(store, "run_slice", {"todo_id": 1, "lane": "kimi"})
    assert _alert_unschedulable_jobs(store) == 1
    assert _alert_open(store, f"unschedulable:{jid}")
    _advertise(store, "node-b", {"repo_checkout": 1, "kimi_bin": 1, "git": 1})
    assert _alert_unschedulable_jobs(store) == 0
    assert not _alert_open(store, f"unschedulable:{jid}")
