"""kind='machine' (gr549585): sync, heartbeat overlay, filters, no-live-state-in-body."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from precis.dispatch import boot
from precis.errors import BadInput, NotFound
from precis.handlers.machine import MachineHandler, slug_for, status_of
from precis.store import Store
from precis.utils.link_target import parse_link_target

HOSTS = ("alpha", "beta")


@pytest.fixture
def h(store: Store) -> MachineHandler:
    hub = boot(store=store)
    handler = hub.handlers["machine"]
    assert isinstance(handler, MachineHandler)
    return handler


@pytest.fixture
def seeded(h: MachineHandler, store: Store) -> MachineHandler:
    for host in HOSTS:
        store.record_heartbeat(
            host,
            temp_c=41.5,
            load1=0.5,
            load5=0.6,
            load15=0.7,
            meta={"platform": "Linux", "release": "6.1"},
        )
    return h


def _chunk_texts(store: Store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s", (ref_id,)
        ).fetchall()
    return [str(r[0]) for r in rows]


def test_sync_creates_one_ref_per_host(seeded: MachineHandler, store: Store) -> None:
    body = seeded.put(mode="sync").body
    assert "2 created" in body
    for host in HOSTS:
        ref = store.get_ref(kind="machine", id=f"ma_{host}")
        assert ref is not None, host
        assert (ref.meta or {}).get("host") == host
        assert (ref.meta or {}).get("platform") == "Linux"


def test_sync_is_idempotent(seeded: MachineHandler) -> None:
    seeded.put(mode="sync")
    assert "2 updated" in seeded.put(mode="sync").body


def test_get_overlays_live_state(seeded: MachineHandler) -> None:
    seeded.put(mode="sync")
    body = seeded.get(id="ma_alpha").body
    assert "status: live" in body
    assert "load (1/5/15m): 0.50 / 0.60 / 0.70" in body
    assert "41.5" in body
    assert "last heartbeat" in body and "ago" in body


def test_no_heartbeat_never_down(h: MachineHandler) -> None:
    h.put(id="gamma", platform="Darwin")
    body = h.get(id="ma_gamma").body
    assert "no heartbeat" in body
    assert "down" not in body


def test_stale_status(h: MachineHandler, store: Store) -> None:
    store.record_heartbeat("alpha", load1=0.1)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE host_heartbeat SET ts = now() - interval '1 hour' "
            "WHERE host = 'alpha'"
        )
    h.put(mode="sync")
    assert "status: stale" in h.get(id="ma_alpha").body


def test_search_filters(h: MachineHandler, store: Store) -> None:
    store.record_heartbeat("alpha", meta={"platform": "Linux"})
    h.put(mode="sync")
    h.put(id="beta", platform="Darwin", role="builder")
    all_rows = h.search().body
    assert "ma_alpha" in all_rows and "ma_beta" in all_rows
    linux = h.search(platform="linux").body
    assert "ma_alpha" in linux and "ma_beta" not in linux
    builder = h.search(role="builder").body
    assert "ma_beta" in builder and "ma_alpha" not in builder
    assert "ma_beta" in h.search(status="no heartbeat").body
    assert "ma_alpha" in h.search(status="live").body


def test_search_bad_status_refused(h: MachineHandler) -> None:
    with pytest.raises(BadInput):
        h.search(status="down")


def test_live_state_never_in_body(h: MachineHandler, store: Store) -> None:
    store.record_heartbeat("alpha", temp_c=99.9, load1=9.9)
    h.put(mode="sync")
    ref = store.get_ref(kind="machine", id="ma_alpha")
    assert ref is not None
    for text in _chunk_texts(store, ref.id):
        assert "9.9" not in text
        assert "99.9" not in text
        assert "load" not in text.lower()
        assert "heartbeat" not in text.lower()


def test_sync_preserves_hand_facts(h: MachineHandler, store: Store) -> None:
    h.put(id="alpha", role="builder", gpu="none")
    store.record_heartbeat("alpha", meta={"platform": "Linux"})
    h.put(mode="sync")
    ref = store.get_ref(kind="machine", id="ma_alpha")
    assert ref is not None
    meta = ref.meta
    assert meta.get("role") == "builder"
    assert meta.get("gpu") == "none"
    assert meta.get("platform") == "Linux"


def test_link_target_resolves(h: MachineHandler, store: Store) -> None:
    h.put(id="alpha")
    target = parse_link_target("machine:ma_alpha", store=store)
    assert target.kind == "machine"


def test_unknown_host_ref_notfound(h: MachineHandler) -> None:
    with pytest.raises(NotFound):
        h.get(id="ma_nohost")


def test_slug_for() -> None:
    assert slug_for("Alpha") == "ma_alpha"
    assert slug_for("node-1") == "ma_node-1"
    assert slug_for("") == ""


def test_status_of() -> None:
    now = datetime.now(UTC)
    assert status_of(None, now=now) == "no heartbeat"


def test_sync_skips_retired_hosts(h: MachineHandler, store: Store) -> None:
    store.record_heartbeat("alpha")
    store.set_host_retired("alpha", retired=True)
    h.put(mode="sync")
    assert store.get_ref(kind="machine", id="ma_alpha") is None


# ── view='daemons' — the release daemon cohort (gr627361) ────────────

SHA = "d5e4f3a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7"


def _daemon_meta(**over: Any) -> dict[str, Any]:
    """A live worker host's heartbeat meta: the served boot sha, an
    installed-unit snapshot and per-process activity."""
    meta: dict[str, Any] = {
        "code_sha": SHA,
        "worker_units": {
            "observed_at": "2026-11-12T00:00:00Z",
            "units": {"precis-worker": {"active_state": "active"}},
        },
        "activity": {"precis-worker": {"idle": True}},
    }
    meta.update(over)
    return meta


def _stale_beat(store: Store, host: str, minutes: int = 90) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE host_heartbeat SET ts = now() - make_interval(mins => %s) "
            "WHERE host = %s",
            (minutes, host),
        )


def _deploy_ledger(store: Store, covered: list[str]) -> None:
    """Publish ``app_settings.deploy.fleet`` as ``scripts/deploy`` does."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('deploy.fleet', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps({"sha": SHA, "covered": covered}),),
        )


def test_daemons_view_lists_live_cohort(h: MachineHandler, store: Store) -> None:
    store.record_heartbeat("alpha", meta=_daemon_meta())
    body = h.get(view="daemons").body
    assert "release daemon cohort — 1 live host(s)" in body
    assert "alpha" in body and "ma_alpha" in body
    assert SHA[:12] in body
    assert "precis-worker" in body  # installed unit and live process


def test_daemons_view_excludes_non_cohort_hosts(
    h: MachineHandler, store: Store
) -> None:
    """Ephemeral, container-id and stale beats never attest (the same
    live-row gate release_confirm counts)."""
    store.record_heartbeat("alpha", meta=_daemon_meta())
    store.record_heartbeat("0123456789ab", meta=_daemon_meta())
    store.record_heartbeat("ephem", meta=_daemon_meta(ephemeral=True))
    store.record_heartbeat("stale", meta=_daemon_meta())
    _stale_beat(store, "stale")
    body = h.get(view="daemons").body
    assert "1 live host(s)" in body
    for ghost in ("0123456789ab", "ephem", "stale"):
        assert ghost not in body


def test_daemons_view_workerless_host_is_a_note(
    h: MachineHandler, store: Store
) -> None:
    """gr623769: a live one-off checkout (empty worker evidence, no
    ledger membership) is a note, never counted."""
    store.record_heartbeat("alpha", meta=_daemon_meta())
    store.record_heartbeat(
        "bare",
        meta={"code_sha": SHA, "worker_units": {}, "activity": {}},
    )
    body = h.get(view="daemons").body
    assert "1 live host(s)" in body
    assert "bare" in body and "not counted" in body


def test_daemons_view_deploy_ledger_member_counts(
    h: MachineHandler, store: Store
) -> None:
    """A covered deploy.fleet member attests even with empty worker
    evidence; the header carries the ledger's deployed sha."""
    store.record_heartbeat(
        "bare",
        meta={"code_sha": SHA, "worker_units": {}, "activity": {}},
    )
    _deploy_ledger(store, ["bare"])
    body = h.get(view="daemons").body
    assert "1 live host(s)" in body
    assert f"deployed {SHA[:12]}" in body


def test_daemons_view_empty(h: MachineHandler) -> None:
    body = h.get(view="daemons").body
    assert "0 live host(s)" in body
    assert "no live daemon heartbeat" in body


def test_daemons_view_via_search(h: MachineHandler, store: Store) -> None:
    store.record_heartbeat("alpha", meta=_daemon_meta())
    assert "alpha" in h.search(view="daemons").body


def test_daemons_view_rejects_bad_spellings(h: MachineHandler) -> None:
    with pytest.raises(BadInput):
        h.get(view="bogus")
    with pytest.raises(BadInput):
        h.search(view="bogus")
    with pytest.raises(BadInput):
        h.get(id="ma_alpha", view="daemons")


def test_get_overlays_boot_version(h: MachineHandler, store: Store) -> None:
    """The single-host read carries the same evidence: code_sha (the
    serving boot version) beside the installed-unit snapshot."""
    store.record_heartbeat("alpha", meta=_daemon_meta())
    h.put(mode="sync")
    body = h.get(id="ma_alpha").body
    assert f"code_sha: {SHA}" in body
    assert "precis-worker:active" in body


# ── args= reaches the handler through the real put verb (MCP door) ──────


@pytest.fixture
def mounted_runtime(runtime_with_store) -> Iterator:
    """Mount the store-backed runtime on tools.core so the real put verb runs."""
    from precis.tools import core as tools_core

    tools_core._runtime = runtime_with_store
    try:
        yield runtime_with_store
    finally:
        tools_core._runtime = None


def test_args_facts_reach_handler_over_mcp_put(mounted_runtime, store: Store) -> None:
    from precis.tools import core as tools_core

    out = tools_core.put(
        kind="machine",
        id="delta",
        args={"platform": "Linux", "role": "builder", "gpu": "none"},
    )
    assert "created machine ma_delta" in out
    ref = store.get_ref(kind="machine", id="ma_delta")
    assert ref is not None
    meta = ref.meta
    assert meta["platform"] == "Linux"
    assert meta["role"] == "builder"
    assert meta["gpu"] == "none"


def test_args_hosts_reach_sync_over_mcp_put(mounted_runtime, store: Store) -> None:
    from precis.tools import core as tools_core

    store.record_heartbeat("alpha", meta={"platform": "Linux"})
    out = tools_core.put(kind="machine", mode="sync", args={"hosts": ["gamma"]})
    assert "sync" in out
    assert store.get_ref(kind="machine", id="ma_alpha") is not None
    assert store.get_ref(kind="machine", id="ma_gamma") is not None
