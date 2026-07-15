"""precis-chem `route` kind + `retrosynth` job (ADR 0056, slice 1).

Covers the pure IR/engine layer (no DB), the handler's inline slice-0 solve
+ content-addressed cache hit, the compute-lane dispatch branch (mint a
retrosynth job under the route via `can_own_jobs`), the requester-blocking
wiring, the worker write-back, and the dark-ship gate.

The test DB template carries only core migrations, so `route_store` seeds the
plugin's `route` kind + relation directly (the plugin migration's idempotent
INSERTs). The `retrosynth` job_type is injected into the registry (no entry
point at test time).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import precis_chem
from precis.dispatch import Hub, _try
from precis.store import Store
from precis.workers import job_types as jt
from precis_chem import jobs as chem_jobs
from precis_chem.engine import (
    DEFAULT_ENGINE,
    AiZynthEngine,
    StubEngine,
    resolve_engine,
)
from precis_chem.ir import RouteGraph, RouteStep, cache_key, normalize_smiles
from precis_chem.jobs import RETROSYNTH_SPEC, run_retrosynth
from precis_chem.route import RouteHandler

_CHEM_MIGRATION = (
    Path(precis_chem.__file__).parent / "migrations" / "0001_route_kind.sql"
)


@pytest.fixture
def route_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The shared test store with the `route` kind + relation seeded.

    Also sets `PRECIS_CHEM_ENABLED` so the kind is 'on' (not that the handler
    checks it directly — the flag gates the catalogue — but it keeps the test
    representative of prod)."""
    monkeypatch.setenv("PRECIS_CHEM_ENABLED", "1")
    body = _CHEM_MIGRATION.read_text(encoding="utf-8")
    body = body.replace("BEGIN;", "").replace("COMMIT;", "")
    with store.pool.connection() as c:
        c.execute(body)
    return store


@pytest.fixture
def register_retrosynth() -> Any:
    """Inject the `retrosynth` job_type into the registry for the test
    (no entry-point discovery at test time); remove it after."""
    jt._REGISTRY["retrosynth"] = RETROSYNTH_SPEC
    yield
    jt._REGISTRY.pop("retrosynth", None)


# ─────────────────────────── pure IR / engine ───────────────────────────


def test_normalize_smiles_is_lexical() -> None:
    assert normalize_smiles("  CC(=O)O\n") == "CC(=O)O"
    assert normalize_smiles("C C") == "C C"  # no chemistry — whitespace only


def test_cache_key_is_content_addressed() -> None:
    k1 = cache_key(target="CCO", engine="stub", engine_version="v1", max_steps=6)
    k2 = cache_key(target="CCO", engine="stub", engine_version="v1", max_steps=6)
    assert k1 == k2 and k1.startswith("retrosynth:")
    # Any input change flips the key (engine version = image digest in prod).
    assert k1 != cache_key(target="CCO", engine="stub", engine_version="v2")
    assert k1 != cache_key(target="CCO", engine="aizynth", engine_version="v1")
    assert k1 != cache_key(target="CCN", engine="stub", engine_version="v1")


def test_route_graph_json_roundtrip_and_render() -> None:
    g = RouteGraph(
        target="CCO",
        engine="stub",
        engine_version="v1",
        steps=[RouteStep(id=1, product="CCO", reactants=["C", "O"], in_stock=True)],
        solved=True,
        score=0.5,
    )
    again = RouteGraph.from_json(g.to_json())
    assert again == g
    rendered = g.render()
    assert "CCO" in rendered and "solved" in rendered and "1." in rendered
    assert "CCO" in g.card_text() and "C O" in g.card_text()


def test_stub_engine_is_deterministic() -> None:
    a = StubEngine().plan("CCO")
    b = StubEngine().plan("CCO")
    assert a == b
    assert a.solved and a.engine == "stub"
    assert a.provenance.get("engine") == "stub"


def test_resolve_engine() -> None:
    assert isinstance(resolve_engine("stub"), StubEngine)
    assert isinstance(resolve_engine(None), StubEngine)  # default
    assert DEFAULT_ENGINE == "stub"
    az = resolve_engine("aizynth")
    assert isinstance(az, AiZynthEngine) and az.is_container
    with pytest.raises(ValueError, match="unknown retrosynthesis engine"):
        resolve_engine("nope")


def test_aizynth_plan_raises_until_slice_1b() -> None:
    with pytest.raises(NotImplementedError, match="container engine"):
        AiZynthEngine().plan("CCO")


def test_run_retrosynth_from_params() -> None:
    g = run_retrosynth({"target": "CCO", "engine": "stub", "cache_key": "x"})
    assert isinstance(g, RouteGraph) and g.solved and g.target == "CCO"


# ─────────────────────────── handler (inline) ───────────────────────────


def test_put_inline_solves_and_get_renders(route_store: Store) -> None:
    """No route node configured ⇒ the in-process stub runs inline (slice-0)."""
    h = RouteHandler(hub=Hub(store=route_store))
    resp = h.put(id="aspirin", target="CC(=O)Oc1ccccc1C(=O)O", engine="stub")
    assert "solved" in resp.body and "in-process" in resp.body

    ref = route_store.get_ref(kind="route", id="aspirin")
    assert ref is not None
    meta = ref.meta or {}
    assert meta.get("status") == "solved"
    assert meta.get("route", {}).get("target") == "CC(=O)Oc1ccccc1C(=O)O"

    got = h.get(id="aspirin")
    assert "CC(=O)Oc1ccccc1C(=O)O" in got.body

    # The route emitted an embeddable card_combined chunk (ord = -1).
    with route_store.pool.connection() as c:
        n = c.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord = -1",
            (ref.id,),
        ).fetchone()[0]
    assert n == 1


def test_put_second_call_is_cache_hit(
    route_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = RouteHandler(hub=Hub(store=route_store))
    h.put(id="aspirin", target="CC(=O)O", engine="stub")

    # A second identical put must NOT re-run the engine.
    calls = {"n": 0}
    orig = StubEngine.plan

    def _counting(self: Any, target: str, **kw: Any) -> Any:
        calls["n"] += 1
        return orig(self, target, **kw)

    monkeypatch.setattr(StubEngine, "plan", _counting)
    resp = h.put(id="aspirin", target="CC(=O)O", engine="stub")
    assert "cache hit" in resp.body
    assert calls["n"] == 0  # zero recompute (ADR 0007)


def test_delete_soft_retires(route_store: Store) -> None:
    h = RouteHandler(hub=Hub(store=route_store))
    h.put(id="aspirin", target="CC(=O)O", engine="stub")
    resp = h.delete(id="aspirin")
    assert "retired" in resp.body
    assert route_store.get_ref(kind="route", id="aspirin") is None


# ─────────────────────────── compute lane ───────────────────────────


def test_put_dispatches_job_when_route_node_set(
    route_store: Store,
    register_retrosynth: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured route node ⇒ mint a retrosynth job parented on the route
    (ADR 0044 compute lane, via `can_own_jobs`), not an inline solve."""
    monkeypatch.setenv("PRECIS_CHEM_ROUTE_NODE", "spark")
    # `_try` constructs + registers so self.hub knows `route` (can_own_jobs).
    hub = Hub(store=route_store)
    h = _try(RouteHandler, hub=hub)
    assert h is not None

    resp = h.put(id="ibuprofen", target="CC(C)Cc1ccccc1", engine="stub")
    assert "dispatched to spark" in resp.body

    route_ref = route_store.get_ref(kind="route", id="ibuprofen")
    assert route_ref is not None
    with route_store.pool.connection() as c:
        row = c.execute(
            "SELECT ref_id, meta FROM refs "
            "WHERE kind = 'job' AND parent_id = %s AND deleted_at IS NULL",
            (route_ref.id,),
        ).fetchone()
    assert row is not None, "a retrosynth job should parent on the route"
    assert row[1].get("job_type") == "retrosynth"
    assert row[1].get("executor") == "ssh_node"
    assert (row[1].get("params") or {}).get("target_node") == "spark"

    # The route itself is still pending (the job hasn't run).
    assert "planning" in h.get(id="ibuprofen").body


def test_requested_by_wires_blocking_todo(
    route_store: Store,
    register_retrosynth: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_CHEM_ROUTE_NODE", "spark")
    todo = route_store.insert_ref(kind="todo", slug=None, title="make the target")

    hub = Hub(store=route_store)
    h = _try(RouteHandler, hub=hub)
    assert h is not None
    h.put(id="tgt", target="CCO", engine="stub", requested_by=todo.id)

    # The todo now blocks on the job: a `requested` link + a
    # derived_job_succeeded auto_check.
    reloaded = route_store.get_ref(kind="todo", id=todo.id)
    assert (reloaded.meta or {}).get("auto_check", {}).get(
        "type"
    ) == "derived_job_succeeded"
    with route_store.pool.connection() as c:
        rel = c.execute(
            "SELECT relation FROM links WHERE src_ref_id = %s",
            (todo.id,),
        ).fetchone()
    assert rel is not None and rel[0] == "requested"


def test_worker_dispatch_writes_route_back(route_store: Store) -> None:
    """The `retrosynth` job dispatch (what ssh_node runs on the node) plans the
    route and writes it back — tested with a fake DispatchContext."""
    ref = route_store.insert_ref(
        kind="route",
        slug="landing",
        title="landing",
        meta={"target": "CCO", "engine": "stub", "status": "planning"},
    )
    key = cache_key(target="CCO", engine="stub", engine_version="stub-v1")
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "stub",
        "engine_version": "stub-v1",
        "cache_key": key,
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)

    assert ctx.status == "succeeded"
    assert ctx.failure is None
    landed = route_store.get_ref(kind="route", id="landing")
    assert (landed.meta or {}).get("status") == "solved"
    assert (landed.meta or {}).get("route", {}).get("target") == "CCO"


def test_worker_dispatch_records_failure_on_container_engine(
    route_store: Store,
) -> None:
    """A container engine selected before slice 1b fails the job cleanly (a
    clear message), never crashing the worker."""
    ref = route_store.insert_ref(
        kind="route", slug="cont", title="cont", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "aizynth",
        "engine_version": "aizynth-container",
        "cache_key": "retrosynth:deadbeef",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status != "succeeded"
    assert ctx.failure is not None and "container engine" in ctx.failure


# ─────────────────────────── dark-ship gate ───────────────────────────


def test_dark_ship_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_CHEM_ENABLED", raising=False)
    assert RouteHandler.spec.requires_env == ("PRECIS_CHEM_ENABLED",)
    assert RouteHandler.spec.is_available() is False
    monkeypatch.setenv("PRECIS_CHEM_ENABLED", "1")
    assert RouteHandler.spec.is_available() is True
    # And it opts into the compute lane.
    assert RouteHandler.spec.can_own_jobs is True


# ─────────────────────────── helpers ───────────────────────────


class _FakeCtx:
    """Minimal DispatchContext double for the retrosynth dispatch."""

    def __init__(self, *, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.meta = {"params": params}
        self.status: str | None = None
        self.failure: str | None = None
        self.chunks: list[tuple[str, str]] = []
        self.meta_updates: dict[str, Any] = {}

    def record_failure(self, reason: str) -> None:
        self.failure = reason
        self.status = "failed"

    def set_status(self, value: str) -> None:
        self.status = value

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **fields: Any) -> None:
        self.meta_updates.update(fields)
