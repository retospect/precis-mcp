"""kind='machine' (gr549585): sync, heartbeat overlay, filters, no-live-state-in-body."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

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
