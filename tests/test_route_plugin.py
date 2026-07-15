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

import json
from pathlib import Path
from typing import Any

import pytest

import precis_chem
from precis.dispatch import Hub, _try
from precis.store import Store
from precis.workers import job_types as jt
from precis_chem import jobs as chem_jobs
from precis_chem.aizynth import (
    CONTAINER_MODELS,
    build_aizynth_argv,
    parse_aizynth_trees,
)
from precis_chem.engine import (
    DEFAULT_ENGINE,
    AiZynthEngine,
    StubEngine,
    resolve_engine,
)
from precis_chem.ir import RouteGraph, RouteStep, cache_key, normalize_smiles
from precis_chem.jobs import RETROSYNTH_SPEC, run_retrosynth
from precis_chem.normalize import ROUTE_FILE, parse_syngraph
from precis_chem.route import RouteHandler

_CHEM_MIGRATION = (
    Path(precis_chem.__file__).parent / "migrations" / "0001_route_kind.sql"
)

#: A real ``route.json`` captured from an actual LinChemIn 3.2.0 translate +
#: routes_descriptors run (2-step aspirin route) — the slice-2 contract.
_ROUTE_FIXTURE = Path(__file__).parent / "fixtures" / "chem" / "aspirin_route.json"


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


# ─────────────────────────── AiZynth (slice 1b) ───────────────────────────


def _aizynth_trees(*, solved: bool = True) -> str:
    """A realistic aizynthcli `trees.json` — one route, CCO ⇐ CC=O + [H][H]
    (a ReactionTree dict: mol → reaction → mols)."""
    leaf_stock = solved
    return json.dumps(
        [
            {
                "type": "mol",
                "smiles": "CCO",
                "in_stock": False,
                "children": [
                    {
                        "type": "reaction",
                        "smiles": "[CH3:1][CH:2]=O.[H][H]>>[CH3:1][CH2:2]O",
                        "metadata": {
                            "template": "tmpl-42",
                            "classification": "reduction",
                            "policy_probability": 0.9,
                        },
                        "children": [
                            {"type": "mol", "smiles": "CC=O", "in_stock": leaf_stock},
                            {"type": "mol", "smiles": "[H][H]", "in_stock": leaf_stock},
                        ],
                    }
                ],
            }
        ]
    )


def test_parse_aizynth_trees_solved() -> None:
    g = parse_aizynth_trees(_aizynth_trees(solved=True), target="CCO")
    assert g.engine == "aizynth" and g.target == "CCO" and g.solved
    assert len(g.steps) == 1
    step = g.steps[0]
    assert step.product == "CCO"
    assert step.reactants == ["CC=O", "[H][H]"]
    assert step.template_id == "tmpl-42"
    assert step.conditions == "reduction"
    assert step.confidence == 0.9
    assert step.in_stock is True  # both precursors buyable


def test_parse_aizynth_trees_unsolved() -> None:
    g = parse_aizynth_trees(_aizynth_trees(solved=False), target="CCO")
    assert g.solved is False  # a leaf is not in stock


def test_parse_aizynth_trees_empty_is_unsolved() -> None:
    g = parse_aizynth_trees("[]", target="CCO")
    assert g.steps == [] and g.solved is False
    assert g.provenance["n_routes"] == 0


def test_build_aizynth_argv() -> None:
    argv = build_aizynth_argv(
        ref_id=7, in_dir="/s/in", out_dir="/s/out", smiles="CCO", image="img:sha"
    )
    assert argv[:5] == ["podman", "run", "--rm", "--name", "precis-route-7"]
    assert "img:sha" in argv and argv[-2:] == ["precis-aizynth-run", "CCO"]
    assert "/s/in:/work/in:ro" in argv and "/s/out:/work/out" in argv
    # No models mount unless asked.
    assert CONTAINER_MODELS not in " ".join(argv)
    argv2 = build_aizynth_argv(
        ref_id=7,
        in_dir="/s/in",
        out_dir="/s/out",
        smiles="CCO",
        image="img",
        models_dir="/nas/models",
    )
    assert f"/nas/models:{CONTAINER_MODELS}:ro" in argv2


def test_container_dispatch_round_trip(
    route_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aizynth container path — stubbed RUNNER/STAGER — plans + writes back
    without a cluster (the struct_relax hook seam)."""
    in_dir = tmp_path / "in"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()

    def _stager(ref_id: int) -> tuple[str, str]:
        return str(in_dir), str(out_dir)

    def _runner(argv: list[str], *, node: str, timeout: Any = None) -> tuple[int, str]:
        # Simulate the container: drop trees.json into the bound out-dir.
        (out_dir / "trees.json").write_text(_aizynth_trees(solved=True))
        return 0, "aizynthcli ok"

    monkeypatch.setattr(chem_jobs, "STAGER", _stager)
    monkeypatch.setattr(chem_jobs, "RUNNER", _runner)

    ref = route_store.insert_ref(
        kind="route", slug="viacontainer", title="viacontainer", meta={"target": "CCO"}
    )
    key = cache_key(target="CCO", engine="aizynth", engine_version="aizynth-container")
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "aizynth",
        "engine_version": "aizynth-container",
        "cache_key": key,
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    landed = route_store.get_ref(kind="route", id="viacontainer")
    route = (landed.meta or {}).get("route") or {}
    assert (
        route.get("engine") == "aizynth"
        and (landed.meta or {}).get("status") == "solved"
    )
    assert route["steps"][0]["product"] == "CCO"


def test_container_dispatch_missing_node_fails(
    route_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chem_jobs, "_NODE", "")
    ref = route_store.insert_ref(
        kind="route", slug="nonode", title="nonode", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "aizynth",
        "engine_version": "aizynth-container",
        "cache_key": "retrosynth:abc",
        # no target_node
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status != "succeeded"
    assert ctx.failure is not None and "route node" in ctx.failure


# ─────────────────────── LinChemIn normalize (slice 2) ───────────────────────


def _route_json(*, solved: bool = True, metrics: bool = True) -> str:
    """A minimal precis-canonical ``route.json`` (what the container shim emits).

    2-step: aspirin ⇐ Ac2O + salicylic acid; salicylic acid ⇐ phenol + CO2."""
    doc: dict[str, Any] = {
        "schema_version": 1,
        "engine": "aizynth",
        "engine_version": "4.3.2",
        "target": "CC(=O)Oc1ccccc1C(=O)O",
        "solved": solved,
        "steps": [
            {
                "id": 1,
                "product": "CC(=O)Oc1ccccc1C(=O)O",
                "reactants": ["CC(=O)OC(C)=O", "O=C(O)c1ccccc1O"],
                "reaction_smiles": "CC(=O)OC(C)=O.O=C(O)c1ccccc1O>>CC(=O)Oc1ccccc1C(=O)O",
                "template_id": "tmpl-acyl-42",
                "confidence": 0.81,
                "conditions": "1.2 O-acylation",
                "in_stock": False,
            },
            {
                "id": 2,
                "product": "O=C(O)c1ccccc1O",
                "reactants": ["Oc1ccccc1", "O=C=O"],
                "reaction_smiles": "O=C=O.Oc1ccccc1>>O=C(O)c1ccccc1O",
                "template_id": "tmpl-kolbe-7",
                "confidence": 0.44,
                "conditions": "3.1 Carboxylation",
                "in_stock": True,
            },
        ],
        "metrics": (
            {"nr_steps": 2, "longest_seq": 2, "nr_branches": 0, "cdscore": 0.33}
            if metrics
            else {}
        ),
        "score": 0.33 if metrics else None,
        "provenance": {"engine": "aizynth", "normalizer": "linchemin", "n_routes": 1},
    }
    return json.dumps(doc)


def test_parse_syngraph_reads_route_json() -> None:
    g = parse_syngraph(_route_json(), target="CC(=O)Oc1ccccc1C(=O)O")
    assert g.engine == "aizynth" and g.solved and len(g.steps) == 2
    # Target-first ordering is authoritative (the shim emits it).
    assert g.steps[0].product == "CC(=O)Oc1ccccc1C(=O)O"
    assert g.steps[1].product == "O=C(O)c1ccccc1O"
    s1 = g.steps[0]
    assert s1.reactants == ["CC(=O)OC(C)=O", "O=C(O)c1ccccc1O"]
    assert s1.template_id == "tmpl-acyl-42" and s1.confidence == 0.81
    assert s1.conditions == "1.2 O-acylation"
    # route.json's reaction_smiles maps onto the IR's reaction string field.
    assert s1.reaction_smarts == "CC(=O)OC(C)=O.O=C(O)c1ccccc1O>>CC(=O)Oc1ccccc1C(=O)O"
    # Route-level descriptors flow through — the scoring substrate.
    assert g.metrics["nr_steps"] == 2 and g.metrics["cdscore"] == 0.33
    assert g.score == 0.33
    assert g.provenance["normalizer"] == "linchemin"
    assert g.provenance["route_schema"] == 1


def test_parse_syngraph_on_real_linchemin_fixture() -> None:
    """The captured real LinChemIn output parses into a coherent RouteGraph."""
    g = parse_syngraph(_ROUTE_FIXTURE.read_text())
    assert g.engine == "aizynth" and g.solved and len(g.steps) == 2
    assert g.steps[0].product == "CC(=O)Oc1ccccc1C(=O)O"  # target-first
    # LinChemIn descriptors are present (nr_steps/longest_seq/convergence/…).
    assert g.metrics.get("nr_steps") == 2
    assert "convergence" in g.metrics and "cdscore" in g.metrics
    # And the whole thing renders without error, showing the metrics line.
    rendered = g.render()
    assert "metrics:" in rendered and "nr_steps=2" in rendered


def test_route_graph_metrics_json_roundtrip() -> None:
    g = RouteGraph(
        target="CCO",
        engine="aizynth",
        engine_version="4.3.2",
        steps=[RouteStep(id=1, product="CCO", reactants=["C", "O"], in_stock=True)],
        solved=True,
        metrics={"nr_steps": 1, "cdscore": 0.5},
    )
    again = RouteGraph.from_json(g.to_json())
    assert again == g and again.metrics == {"nr_steps": 1, "cdscore": 0.5}


def test_metrics_render_and_view(route_store: Store) -> None:
    """get(view='metrics') renders route descriptors; a stub route says none."""
    h = RouteHandler(hub=Hub(store=route_store))
    # Land a normalized route directly (bypass the container).
    ref = route_store.insert_ref(
        kind="route", slug="asp", title="asp", meta={"target": "CC(=O)Oc1ccccc1C(=O)O"}
    )
    g = parse_syngraph(_route_json())
    from precis_chem.persist import apply_route_result

    apply_route_result(route_store, ref.id, g, cache_key="retrosynth:abc")

    metrics_view = h.get(id="asp", view="metrics").body
    assert "route metrics" in metrics_view and "nr_steps" in metrics_view
    assert "cdscore" in metrics_view

    # A stub route (no normalizer) reports the absence, doesn't error.
    h.put(id="stubby", target="CCO", engine="stub")
    stub_metrics = h.get(id="stubby", view="metrics").body
    assert "no route-level descriptors" in stub_metrics

    with pytest.raises(Exception, match="unknown route view"):
        h.get(id="asp", view="bogus")


def test_container_prefers_route_json_over_trees(
    route_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the container drops both route.json and trees.json, the dispatch
    reads the normalized route.json (metrics present)."""
    out_dir = tmp_path / "out"
    (tmp_path / "in").mkdir()
    out_dir.mkdir()

    def _runner(argv: list[str], *, node: str, timeout: Any = None) -> tuple[int, str]:
        (out_dir / "trees.json").write_text(_aizynth_trees(solved=True))
        (out_dir / ROUTE_FILE).write_text(_route_json())
        return 0, "ok"

    monkeypatch.setattr(
        chem_jobs, "STAGER", lambda rid: (str(tmp_path / "in"), str(out_dir))
    )
    monkeypatch.setattr(chem_jobs, "RUNNER", _runner)

    ref = route_store.insert_ref(
        kind="route",
        slug="both",
        title="both",
        meta={"target": "CC(=O)Oc1ccccc1C(=O)O"},
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CC(=O)Oc1ccccc1C(=O)O",
        "engine": "aizynth",
        "engine_version": "aizynth-container",
        "cache_key": "retrosynth:both",
        "target_node": "spark",
    }
    chem_jobs._dispatch(_FakeCtx(store=route_store, params=params), RETROSYNTH_SPEC)
    route = (route_store.get_ref(kind="route", id="both").meta or {}).get("route") or {}
    # route.json's 2-step normalized plan (not trees.json's 1-step) won.
    assert len(route["steps"]) == 2
    assert route["metrics"]["nr_steps"] == 2


def test_container_falls_back_to_trees_when_no_route_json(
    route_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older image / normalizer skipped ⇒ only trees.json ⇒ bespoke parser."""
    out_dir = tmp_path / "out"
    (tmp_path / "in").mkdir()
    out_dir.mkdir()

    def _runner(argv: list[str], *, node: str, timeout: Any = None) -> tuple[int, str]:
        (out_dir / "trees.json").write_text(_aizynth_trees(solved=True))
        return 0, "ok"

    monkeypatch.setattr(
        chem_jobs, "STAGER", lambda rid: (str(tmp_path / "in"), str(out_dir))
    )
    monkeypatch.setattr(chem_jobs, "RUNNER", _runner)

    ref = route_store.insert_ref(
        kind="route", slug="treesonly", title="treesonly", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "aizynth",
        "engine_version": "aizynth-container",
        "cache_key": "retrosynth:trees",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status == "succeeded"
    route = (route_store.get_ref(kind="route", id="treesonly").meta or {}).get(
        "route"
    ) or {}
    assert route["steps"][0]["product"] == "CCO"  # trees.json parsed


def test_container_bad_route_json_falls_back_to_trees(
    route_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbled route.json is not fatal — the dispatch falls back to trees.json."""
    out_dir = tmp_path / "out"
    (tmp_path / "in").mkdir()
    out_dir.mkdir()

    def _runner(argv: list[str], *, node: str, timeout: Any = None) -> tuple[int, str]:
        (out_dir / "trees.json").write_text(_aizynth_trees(solved=True))
        (out_dir / ROUTE_FILE).write_text("{ this is not valid json ")
        return 0, "ok"

    monkeypatch.setattr(
        chem_jobs, "STAGER", lambda rid: (str(tmp_path / "in"), str(out_dir))
    )
    monkeypatch.setattr(chem_jobs, "RUNNER", _runner)

    ref = route_store.insert_ref(
        kind="route", slug="badjson", title="badjson", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "aizynth",
        "engine_version": "aizynth-container",
        "cache_key": "retrosynth:bad",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status == "succeeded" and ctx.failure is None
    route = (route_store.get_ref(kind="route", id="badjson").meta or {}).get(
        "route"
    ) or {}
    assert route["steps"][0]["product"] == "CCO"  # trees.json fallback won


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
