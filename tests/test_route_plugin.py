"""precis-chem `route` kind + `retrosynth` job.

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
from precis_chem.askcos import (
    TREE_SEARCH_PATH,
    build_treebuilder_request,
    extract_paths,
)
from precis_chem.engine import (
    ASKCOS_ENDPOINT_ENV,
    DEFAULT_ENGINE,
    TRANSPORT_SERVICE,
    AiZynthEngine,
    AskcosEngine,
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


def _route_meta(store: Store, id: str) -> dict[str, Any]:
    """Fetch a `route` ref's meta dict — asserts the ref actually landed."""
    ref = store.get_ref(kind="route", id=id)
    assert ref is not None
    return ref.meta or {}


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
        n_row = c.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord = -1",
            (ref.id,),
        ).fetchone()
    assert n_row is not None
    assert n_row[0] == 1


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
    assert calls["n"] == 0  # zero recompute


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
    (compute lane, via `can_own_jobs`), not an inline solve."""
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
            "WHERE kind = 'job' AND parent_id = %s AND retired_at IS NULL",
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
    assert reloaded is not None
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
    assert landed is not None
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
        (out_dir / "trees.json").write_text(
            _aizynth_trees(solved=True), encoding="utf-8"
        )
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
    assert landed is not None
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
    g = parse_syngraph(_ROUTE_FIXTURE.read_text(encoding="utf-8"))
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
        (out_dir / "trees.json").write_text(
            _aizynth_trees(solved=True), encoding="utf-8"
        )
        (out_dir / ROUTE_FILE).write_text(_route_json(), encoding="utf-8")
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
    route = _route_meta(route_store, "both").get("route") or {}
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
        (out_dir / "trees.json").write_text(
            _aizynth_trees(solved=True), encoding="utf-8"
        )
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
    route = _route_meta(route_store, "treesonly").get("route") or {}
    assert route["steps"][0]["product"] == "CCO"  # trees.json parsed


def test_container_bad_route_json_falls_back_to_trees(
    route_store: Store, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbled route.json is not fatal — the dispatch falls back to trees.json."""
    out_dir = tmp_path / "out"
    (tmp_path / "in").mkdir()
    out_dir.mkdir()

    def _runner(argv: list[str], *, node: str, timeout: Any = None) -> tuple[int, str]:
        (out_dir / "trees.json").write_text(
            _aizynth_trees(solved=True), encoding="utf-8"
        )
        (out_dir / ROUTE_FILE).write_text("{ this is not valid json ", encoding="utf-8")
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
    route = _route_meta(route_store, "badjson").get("route") or {}
    assert route["steps"][0]["product"] == "CCO"  # trees.json fallback won


# ─────────────────────────── ASKCOS service (slice 3) ───────────────────────────


def test_resolve_askcos_engine() -> None:
    az = resolve_engine("askcos")
    assert isinstance(az, AskcosEngine)
    assert az.transport == TRANSPORT_SERVICE
    assert az.input_format == "askcosv2"  # LinChemIn translate format
    assert az.is_container is False
    with pytest.raises(NotImplementedError, match="service engine"):
        az.plan("CCO")


def test_askcos_endpoint_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ASKCOS_ENDPOINT_ENV, raising=False)
    assert AskcosEngine().endpoint is None
    monkeypatch.setenv(ASKCOS_ENDPOINT_ENV, "http://askcos.internal:9100")
    assert AskcosEngine().endpoint == "http://askcos.internal:9100"


def test_build_treebuilder_request() -> None:
    req = build_treebuilder_request("CCO", max_steps=5, expansion_time_s=30)
    assert req["smiles"] == "CCO"
    assert req["max_depth"] == 5 and req["expansion_time"] == 30
    assert "max_branching" in req
    assert TREE_SEARCH_PATH.startswith("/api/tree-search/mcts")


def test_extract_paths_defensive() -> None:
    # Documented envelope.
    assert extract_paths({"result": {"paths": [{"a": 1}, {"b": 2}]}}) == [
        {"a": 1},
        {"b": 2},
    ]
    # Bare list + top-level key + result-as-list.
    assert extract_paths([{"a": 1}]) == [{"a": 1}]
    assert extract_paths({"paths": [{"x": 1}]}) == [{"x": 1}]
    assert extract_paths({"result": [{"y": 1}]}) == [{"y": 1}]
    # Nothing solved.
    assert extract_paths({"result": {"paths": []}}) == []
    assert extract_paths({"stats": {}}) == []
    assert extract_paths(None) == []


def test_service_dispatch_round_trip(
    route_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The askcos service path — stubbed SERVICE_CALLER + NORMALIZER — POSTs,
    normalizes, and writes back without a cluster or a running ASKCOS."""
    monkeypatch.setenv(ASKCOS_ENDPOINT_ENV, "http://askcos.internal:9100")

    seen: dict[str, Any] = {}

    def _caller(url: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen["url"] = url
        seen["payload"] = payload
        # A minimal askcosv2-ish response envelope with one path.
        return {"result": {"paths": [{"smiles": "CCO", "children": []}]}}

    def _normalizer(**kw: Any) -> str:
        seen["normalizer_kw"] = kw
        return _route_json()  # a valid precis-canonical route.json

    monkeypatch.setattr(chem_jobs, "SERVICE_CALLER", _caller)
    monkeypatch.setattr(chem_jobs, "NORMALIZER", _normalizer)

    ref = route_store.insert_ref(
        kind="route", slug="viaservice", title="viaservice", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "askcos",
        "engine_version": "askcos-v2",
        "cache_key": "retrosynth:svc",
        "target_node": "spark",  # runs the normalizer container
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    # POSTed to the tree-search endpoint with the target.
    assert seen["url"].endswith(TREE_SEARCH_PATH)
    assert seen["payload"]["smiles"] == "CCO"
    # Normalizer invoked with the askcosv2 input_format.
    assert seen["normalizer_kw"]["input_format"] == "askcosv2"
    landed = route_store.get_ref(kind="route", id="viaservice")
    assert landed is not None
    route = (landed.meta or {}).get("route") or {}
    assert route["metrics"]["nr_steps"] == 2  # from the normalized route.json


def test_service_dispatch_no_endpoint_fails(
    route_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ASKCOS_ENDPOINT_ENV, raising=False)
    ref = route_store.insert_ref(
        kind="route", slug="noep", title="noep", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "askcos",
        "engine_version": "askcos-v2",
        "cache_key": "retrosynth:noep",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status != "succeeded"
    assert ctx.failure is not None and "PRECIS_ASKCOS_URL" in ctx.failure


def test_service_dispatch_no_paths_is_unsolved(
    route_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ASKCOS returning no paths is a legitimate unsolved result, not a failure."""
    monkeypatch.setenv(ASKCOS_ENDPOINT_ENV, "http://askcos.internal:9100")
    monkeypatch.setattr(
        chem_jobs, "SERVICE_CALLER", lambda url, payload: {"result": {"paths": []}}
    )
    ref = route_store.insert_ref(
        kind="route", slug="nopath", title="nopath", meta={"target": "CCO"}
    )
    params = {
        "route_ref_id": ref.id,
        "target": "CCO",
        "engine": "askcos",
        "engine_version": "askcos-v2",
        "cache_key": "retrosynth:nopath",
        "target_node": "spark",
    }
    ctx = _FakeCtx(store=route_store, params=params)
    chem_jobs._dispatch(ctx, RETROSYNTH_SPEC)
    assert ctx.status == "succeeded"  # unsolved, but not an error
    landed = route_store.get_ref(kind="route", id="nopath")
    assert landed is not None
    meta = landed.meta or {}
    assert (
        meta.get("status") == "unsolved"
        or (meta.get("route") or {}).get("solved") is False
    )


# ─────────────────────────── dark-ship gate ───────────────────────────


def test_kind_is_available_without_any_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`route` carries no private enable flag — it is on wherever the
    plugin is installed. Pinned so the dark-ship gate cannot creep back:
    an operator who wants the kind off uses `PRECIS_KINDS_DISABLED`, the
    one general control, not a per-kind switch."""
    from precis import settings as _settings

    _settings.bind_store(None)
    _settings.invalidate()
    monkeypatch.delenv("PRECIS_CHEM_ENABLED", raising=False)
    assert RouteHandler.spec.requires_setting == ()
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


def test_route_render_puts_smiles_in_code_spans() -> None:
    """A stereocentre followed by a branch matches markdown's inline-link
    grammar, so a bare SMILES in the route tree renders as a link and the
    structure vanishes. Measured before the fix on this exact route:
    `[C@H](N)` became a link with text 'C@H' targeting 'N'."""
    import re

    from precis_chem.ir import RouteGraph, RouteStep

    g = RouteGraph(
        target="C[C@H](N)C(=O)OCC",
        engine="stub",
        engine_version="0",
        steps=[
            RouteStep(
                id=1,
                product="C[C@H](N)C(=O)OCC",
                reactants=["C[C@H](N)C(=O)O", "CCO"],
                in_stock=True,
            )
        ],
        solved=True,
    )
    for body in (g.render(), g.metrics_render()):
        outside_code = re.sub(r"`[^`]*`", "", body)
        assert not re.search(r"\[[^\]]*\]\([^)]*\)", outside_code), body

    # card_text feeds the SEARCH INDEX, not a renderer — it must stay bare,
    # or the backticks end up in the embedded text.
    assert "`" not in g.card_text()


def test_route_render_escapes_conditions_and_template() -> None:
    """conditions/template_id are free text from the engine — ligand notation
    (Pd[P(t-Bu)3](OAc)2) and SMARTS both hit the inline-link grammar."""
    import re

    from precis_chem.ir import RouteGraph, RouteStep

    g = RouteGraph(
        target="CCO",
        engine="stub",
        engine_version="0",
        steps=[
            RouteStep(
                id=1,
                product="CCO",
                reactants=["CC=O"],
                template_id="[C:1]=[O:2]>>[C:1][O:2]",
                conditions="Pd[P(t-Bu)3](OAc)2, THF, 60C",
            )
        ],
    )
    body = g.render()
    outside_code = re.sub(r"`[^`]*`", "", body)
    assert not re.search(r"\[[^\]]*\]\([^)]*\)", outside_code), body
    assert "Pd[P(t-Bu)3](OAc)2" in body


def test_route_render_step_with_no_reactants_shows_dash() -> None:
    """The ``or "—"`` fallback on an empty precursor list — a step the engine
    returned with no reactants must not render a bare ``⇐``."""
    from precis_chem.ir import RouteGraph, RouteStep

    g = RouteGraph(
        target="CCO",
        engine="stub",
        engine_version="0",
        steps=[RouteStep(id=1, product="CCO", reactants=[])],
    )
    body = g.render()
    assert "⇐ —" in body, body


# ─────────────────────────── platform constraints ───────────────────────────


def test_resolve_constraints_shapes() -> None:
    from precis_chem.constraints import EWOD_OIL, resolve_constraints

    assert resolve_constraints(None) == []
    assert resolve_constraints([]) == []
    assert [c.name for c in resolve_constraints("ewod-oil")] == ["ewod-oil"]
    assert [c.name for c in resolve_constraints(["EWOD-OIL", "ewod-oil"])] == [
        "ewod-oil"
    ]  # case-folded + de-duped
    assert resolve_constraints(["ewod-oil"])[0] is EWOD_OIL
    with pytest.raises(ValueError, match="unknown platform constraint"):
        resolve_constraints(["no-such-platform"])


def test_cache_key_folds_constraints_only_when_present() -> None:
    base = cache_key(target="CCO", engine="stub", engine_version="1")
    empty = cache_key(target="CCO", engine="stub", engine_version="1", constraints=())
    assert base == empty  # pre-constraint keys stay valid
    constrained = cache_key(
        target="CCO", engine="stub", engine_version="1", constraints=("ewod-oil",)
    )
    assert constrained != base


def test_screen_step_flags_are_honest() -> None:
    from precis_chem.constraints import EWOD_OIL, screen_step

    def step(conditions: str | None) -> RouteStep:
        return RouteStep(id=1, product="CCO", reactants=["CC"], conditions=conditions)

    # No conditions from the engine ⇒ unscreened, never a fake pass.
    assert "unscreened" in screen_step(step(None), EWOD_OIL)[0]
    # Deny-listed solvent ⇒ check flag.
    flags = screen_step(step("NaBH4, THF, 0 °C"), EWOD_OIL)
    assert any("check" in f and "thf" in f for f in flags), flags
    # Allow-listed solvent, no hazard ⇒ ok flag.
    flags = screen_step(step("K2CO3, water, 25 °C"), EWOD_OIL)
    assert any(": ok — " in f for f in flags), flags
    # Hazard keyword ⇒ check flag even in an allowed solvent.
    flags = screen_step(step("water, reflux"), EWOD_OIL)
    assert any("reflux" in f for f in flags), flags
    # A deny hit is never masked by a coincidental allow-term match.
    flags = screen_step(step("water/THF mixture, 25 °C"), EWOD_OIL)
    assert all(": ok — " not in f for f in flags), flags


def test_screen_step_matches_on_word_boundaries_not_substrings() -> None:
    """A reagent whose name merely contains a solvent name is not a solvent.

    All three of these false-flagged under substring matching.
    """
    from precis_chem.constraints import EWOD_OIL, screen_step

    def flags_for(conditions: str) -> list[str]:
        return screen_step(
            RouteStep(id=1, product="CCO", reactants=["CC"], conditions=conditions),
            EWOD_OIL,
        )

    # 'p-toluenesulfonic acid' is not toluene; 'cyclohexane' is not hexane;
    # 'acetohydroxamic' is not EtOH; 'acetoacetate' is not EtOAc.
    for conditions, absent in [
        ("p-toluenesulfonic acid, water, 40 °C", "toluene"),
        ("benzenesulfonyl chloride, water, 25 °C", "benzene"),
        ("acetohydroxamic acid workup in water", "etoh"),
        ("ethyl acetoacetate, water, 30 °C", "etoac"),
    ]:
        got = flags_for(conditions)
        assert all(f"'{absent}'" not in f for f in got), (conditions, got)
        # …and the real (allowed) solvent still earns its ok.
        assert any(": ok — " in f for f in got), (conditions, got)

    # 'cyclohexane' must not be reported as 'hexane' (same verdict, wrong name).
    got = flags_for("cyclohexane wash")
    assert all("'hexane'" not in f for f in got), got
    # The deny list still fires on the real solvent, standalone.
    assert any("'hexane'" in f for f in flags_for("hexane, 25 °C"))
    assert any("'toluene'" in f for f in flags_for("toluene, 80 °C"))


def test_temperature_is_parsed_not_keyword_matched() -> None:
    """`150 °C` / `150°C` / `150 C` are one hazard, not three spellings."""
    from precis_chem.constraints import EWOD_OIL, _temperatures_c, screen_step

    def flags_for(conditions: str) -> list[str]:
        return screen_step(
            RouteStep(id=1, product="CCO", reactants=["CC"], conditions=conditions),
            EWOD_OIL,
        )

    # Every spelling of an over-ceiling temperature is caught.
    for conditions in ("heat to 160°C, 2 h", "160 °C", "160 C", "at 160 degrees C"):
        got = flags_for(conditions)
        assert any("above the platform ceiling" in f for f in got), (conditions, got)

    # Inside the envelope ⇒ no temperature flag.
    assert not any("ceiling" in f for f in flags_for("water, 80 °C"))
    # Below the floor (cryogenic) ⇒ flagged.
    assert any("below the platform floor" in f for f in flags_for("water, -78 °C"))
    # A range's upper end is what trips it; '70-78 °C' is NOT read as -78.
    assert not any("floor" in f for f in flags_for("water, 70-78 °C"))

    # Formulas and column names are not temperatures.
    for text in ("cs2co3", "cacl2 brine", "c18 column", "5 equiv k2co3"):
        assert _temperatures_c(text) == [], text
    # Unrecognised units fall through rather than being rescaled.
    assert _temperatures_c("423 k") == []


def test_render_distinguishes_unscreened_from_check() -> None:
    """`unscreened` (no data) and `check` (a problem) must not share a glyph."""
    from precis_chem.constraints import screen_route

    g = RouteGraph(
        target="CCO",
        engine="stub",
        engine_version="0",
        steps=[
            RouteStep(id=1, product="CCO", reactants=["CC"], conditions=None),
            RouteStep(id=2, product="CC", reactants=["C"], conditions="THF, 0 °C"),
            RouteStep(id=3, product="C", reactants=["O"], conditions="water, 25 °C"),
        ],
    )
    body = screen_route(g, ["ewod-oil"]).render()
    assert "· ewod-oil: unscreened" in body, body
    assert "⚠ ewod-oil: check" in body, body
    assert "✓ ewod-oil: ok" in body, body


def test_screen_route_sets_constraints_and_flags_roundtrip() -> None:
    from precis_chem.constraints import screen_route

    g = RouteGraph(
        target="CCO",
        engine="stub",
        engine_version="0",
        steps=[RouteStep(id=1, product="CCO", reactants=["CC"], conditions="water")],
    )
    out = screen_route(g, ["ewod-oil"])
    assert out.constraints == ["ewod-oil"]
    assert out.steps[0].constraint_flags
    # Serialization round-trips the new fields.
    back = RouteGraph.from_json(out.to_json())
    assert back.constraints == ["ewod-oil"]
    assert back.steps[0].constraint_flags == out.steps[0].constraint_flags
    # And renders the ledger + per-step flag.
    body = out.render()
    assert "constraints: ewod-oil" in body
    assert "## constraint: ewod-oil" in body
    # No-op path returns the graph unchanged.
    assert screen_route(g, []) is g


def test_put_with_constraint_screens_and_stamps(route_store: Store) -> None:
    h = RouteHandler(hub=Hub(store=route_store))
    resp = h.put(
        id="aspirin-ewod",
        target="CC(=O)Oc1ccccc1C(=O)O",
        engine="stub",
        constraints=["ewod-oil"],
    )
    assert "constraints: ewod-oil" in resp.body
    meta = _route_meta(route_store, "aspirin-ewod")
    assert meta.get("constraints") == ["ewod-oil"]
    assert meta.get("route", {}).get("constraints") == ["ewod-oil"]
    # The stub reports no real solvent ⇒ the step carries an honest flag.
    step_flags = meta["route"]["steps"][0]["constraint_flags"]
    assert step_flags and "ewod-oil" in step_flags[0]


def test_put_constrained_is_distinct_cache_row(route_store: Store) -> None:
    """Same target with vs without a constraint must not share a cache hit."""
    h = RouteHandler(hub=Hub(store=route_store))
    h.put(id="tgt", target="CC(=O)O", engine="stub")
    resp = h.put(id="tgt", target="CC(=O)O", engine="stub", constraints=["ewod-oil"])
    assert "cache hit" not in resp.body


def test_put_unknown_constraint_is_bad_input(route_store: Store) -> None:
    from precis.errors import BadInput

    h = RouteHandler(hub=Hub(store=route_store))
    with pytest.raises(BadInput, match="unknown platform constraint"):
        h.put(id="x", target="CCO", engine="stub", constraints=["mars-glovebox"])


# ──────────────────── droplet polarization / RC constraint ────────────────────


def test_charge_relaxation_frequency_physics() -> None:
    """f_c = σ/(2π εr ε0) — the Maxwell–Wagner relaxation frequency."""
    import math

    from precis_chem.constraints import EPS_0, charge_relaxation_frequency_hz

    # Closed form, checked against a hand computation.
    assert charge_relaxation_frequency_hz(1.2, 80.0) == pytest.approx(
        1.2 / (2 * math.pi * 80.0 * EPS_0), rel=1e-12
    )
    # 100 mM buffer sits in the hundreds of MHz — far above any EWOD band.
    assert charge_relaxation_frequency_hz(1.2, 80.0) == pytest.approx(2.7e8, rel=0.05)
    # Neat acetonitrile lands at single-digit Hz. This is the load-bearing
    # number: it is why the platform's second permitted solvent needs salt.
    assert charge_relaxation_frequency_hz(1e-8, 37.5) == pytest.approx(4.8, rel=0.05)
    # Scaling is linear in σ and inverse in εr.
    assert charge_relaxation_frequency_hz(2e-8, 37.5) == pytest.approx(
        2 * charge_relaxation_frequency_hz(1e-8, 37.5)
    )
    with pytest.raises(ValueError, match="eps_r must be positive"):
        charge_relaxation_frequency_hz(1.0, 0.0)


def test_polarization_regime_crossover() -> None:
    from precis_chem.constraints import (
        charge_relaxation_frequency_hz,
        polarization_regime,
    )

    sigma, eps_r = 1e-4, 80.0  # DI water, f_c ≈ 22 kHz
    f_c = charge_relaxation_frequency_hz(sigma, eps_r)
    # Well below f_c ⇒ conductor ⇒ full electrowetting force.
    assert polarization_regime(sigma, eps_r, f_c / 100) == "electrowetting"
    # At f_c ⇒ the marginal decade, where ionic strength decides behaviour.
    assert polarization_regime(sigma, eps_r, f_c) == "marginal"
    # Well above ⇒ the liquid is a dielectric; EWOD force has collapsed.
    assert polarization_regime(sigma, eps_r, f_c * 100) == "dielectrophoretic"


def test_medium_verdict_flags_the_empty_window() -> None:
    """Neat MeCN and toluene have NO usable actuation frequency."""
    from precis_chem.constraints import DROPLET_MEDIA, EWOD_OIL, medium_verdict

    # Dielectrophoretic even at the band floor ⇒ empty window, needs salt.
    for medium in ("acetonitrile, neat", "toluene"):
        assert "EMPTY WINDOW" in medium_verdict(EWOD_OIL, medium), medium
    # Buffered aqueous and salted MeCN actuate across the whole band.
    for medium in (
        "aqueous buffer, 10 mM",
        "aqueous buffer, 100 mM / PBS",
        "acetonitrile, 0.1 M supporting electrolyte",
    ):
        assert "ok: electrowetting" in medium_verdict(EWOD_OIL, medium), medium
    # Pure water is marginal, not ok — the band straddles its f_c.
    assert "marginal" in medium_verdict(EWOD_OIL, "water, ultrapure")
    # Every reference medium is evaluable.
    for medium in DROPLET_MEDIA:
        assert medium_verdict(EWOD_OIL, medium)
    with pytest.raises(ValueError, match="unknown medium"):
        medium_verdict(EWOD_OIL, "liquid helium")


def test_screen_flags_polarization_and_separation_conditions() -> None:
    from precis_chem.constraints import EWOD_OIL, screen_step

    def flags_for(conditions: str) -> list[str]:
        return screen_step(
            RouteStep(id=1, product="C", reactants=["O"], conditions=conditions),
            EWOD_OIL,
        )

    # Low-σ media: the platform forces an electrolyte the chemistry didn't pick.
    assert any("electrolyte" in f for f in flags_for("anhydrous MeCN, 100 °C"))
    assert any("buffer or salt it" in f for f in flags_for("deionized water, 25 °C"))
    assert any("electrolyte" in f for f in flags_for("salt-free conditions"))
    # Operations that REMOVE the needed conductivity.
    assert any("conductivity" in f for f in flags_for("desalt the product"))
    # Separations are absent on chip.
    assert any("chromatography" in f for f in flags_for("purify by chromatography"))
    assert any("phase split" in f for f in flags_for("aqueous wash"))
    # No inert blanket.
    assert any("dissolves O2" in f for f in flags_for("under argon, water"))
    # Buffered aqueous is the sweet spot — no polarization flag at all.
    got = flags_for("aqueous buffer, 37 °C")
    assert any(": ok — " in f for f in got), got


def test_one_finding_reported_once_not_per_spelling() -> None:
    """'column chromatography' is one problem; several terms map to it."""
    from precis_chem.constraints import EWOD_OIL, screen_step

    flags = screen_step(
        RouteStep(
            id=1,
            product="C",
            reactants=["O"],
            conditions="purify by column chromatography",
        ),
        EWOD_OIL,
    )
    chroma = [f for f in flags if "chromatography" in f or "column" in f]
    assert len(chroma) == 1, flags
