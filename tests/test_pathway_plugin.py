"""precis_pathway `pathway` kind + `autocatpath_explore` job (bundle-pathway-
in-tree proposal, docs/proposals/bundle-pathway-in-tree-plugin.md).

Ported from autocatpath's ``tests/test_precis_bridge.py`` (+ the slab case
from ``tests/test_precis_runner_slab.py``) now that the glue lives in this
repo as ``src/precis_pathway/``.

Two layers, mirroring ``tests/test_protein_plugin.py``:

* the **pure runner/analysis/views** (``precis_pathway.runner`` /
  ``.analysis`` / ``.text_views`` / ``.toon_views``) — needs only autocatpath's
  own deps (EMT is free), no DB;
* the **handler** (``precis_pathway.handler.PathwayHandler``) + the
  ``autocatpath_explore`` job dispatch — needs the shared test Postgres (the
  ``store`` fixture) with the plugin migration applied.

Skips cleanly (whole module) if ``autocatpath`` isn't installed.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("autocatpath")

import precis_pathway
from precis.dispatch import Hub, InitError, _try
from precis.store import Store
from precis.workers import job_types as jt
from precis_pathway import job as pathway_job
from precis_pathway import runner
from precis_pathway.handler import PathwayHandler

SMOKE = """
name: bridge_smoke
substrate: "NO"
target: "NO3"
network: oxidation
slab: {element: Pd, size: [2, 2, 3], vacuum: 8.0, fix_layers: 1, relax_lattice: false}
mlip: {backend: emt}
search: {seeds: [0], neb_images: 3, neb_max_steps: 15, neb_retries: 0, max_steps: 40, pose_count: 2}
"""

# Two seeds — §B-1 seed fan-out (run_seed_partial x N + aggregate_seed_partials)
# needs >1 seed to exercise pooling; SMOKE's single seed can't.
FANOUT = """
name: fanout_smoke
substrate: "NO"
target: "NO3"
network: oxidation
slab: {element: Pd, size: [2, 2, 3], vacuum: 8.0, fix_layers: 1, relax_lattice: false}
mlip: {backend: emt}
search: {seeds: [0, 1], neb_images: 3, neb_max_steps: 15, neb_retries: 0, max_steps: 40, pose_count: 2}
"""

# Branching network — connected (supply bridges), so it has a real root→target
# path and exercises the interleaved profile / compare. The linear `oxidation`
# SMOKE is disconnected (rate-limiting still works via max-over-edges fallback).
BRANCH = """
name: branch_smoke
substrate: "NO"
target: "NO3"
network: branching
slab: {element: Pd, size: [2, 2, 3], vacuum: 8.0, fix_layers: 1, relax_lattice: false}
mlip: {backend: emt}
search: {seeds: [0], neb_images: 3, neb_max_steps: 12, neb_retries: 0, max_steps: 30, pose_count: 2}
"""

_MIGRATIONS_DIR = Path(precis_pathway.__file__).parent / "migrations"


def _yaml_dict(text: str) -> dict:
    from autocatpath.config import _load_yaml

    return _load_yaml(text)


# ─────────────────────────── fixtures ───────────────────────────


@pytest.fixture
def pathway_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The shared test store with the precis_pathway migration seeded + the
    dark flag on (the `pathway` kind + `pathway_body` chunk kind)."""
    monkeypatch.setenv("PRECIS_AUTOCATPATH_ENABLED", "1")
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return store


@pytest.fixture
def register_autocatpath_explore() -> Any:
    """Inject the `autocatpath_explore` job_type into the registry for the
    test (no entry point at test time)."""
    jt._REGISTRY["autocatpath_explore"] = pathway_job.SPEC
    yield
    jt._REGISTRY.pop("autocatpath_explore", None)


class _FakeCtx:
    """Minimal precis DispatchContext double for the job dispatcher."""

    def __init__(self, *, store: Store, params: dict[str, Any]) -> None:
        self.store = store
        self.meta: dict[str, Any] = {"params": params}
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


# ─────────────────────────── pure runner: autocatpath-only, no DB ──────────


def test_runner_emt_smoke_and_determinism() -> None:
    art = runner.run_pathway_from_yaml(SMOKE)
    r = art["results_json"]
    assert r["backend"] == "emt"
    assert r["nodes"] and r["edges"], "network produced no states/steps"
    assert art["methods_md"].startswith("# Methods")
    assert art["graph_json"]["nodes"] and art["graph_json"]["links"]
    # every state's relaxed geometry is harvested for slice-1 ingest
    assert set(art["structures_extxyz"]) == set(r["nodes"])
    # deterministic content address for an unchanged config
    assert runner.run_pathway_from_yaml(SMOKE)["content_key"] == art["content_key"]


# ─────────────────────── §B-1 seed fan-out: pure runner ────────────────────


def test_model_specs_and_seed_content_key() -> None:
    cfg = _yaml_dict(FANOUT)
    assert runner.model_specs(cfg) == [("emt", None)]
    k0 = runner.seed_content_key(cfg, seed=0, model_index=0)
    k1 = runner.seed_content_key(cfg, seed=1, model_index=0)
    assert k0 != k1  # seed differentiates
    assert runner.seed_content_key(cfg, seed=0, model_index=0) == k0  # deterministic
    assert k0 != runner.content_key(runner.effective_config(cfg))  # not the base key


def _json_eq(a: Any, b: Any) -> bool:
    """Structural equality that treats NaN as equal to NaN (unlike ``==``) —
    barrier/delta_e ``Estimate``s legitimately carry ``nan`` for an
    unsampled edge, and Python's ``float('nan') != float('nan')`` would
    otherwise fail a byte-identical comparison on values that print
    identically."""
    import json

    return json.dumps(a, sort_keys=True, default=str) == json.dumps(
        b, sort_keys=True, default=str
    )


def test_run_seed_partial_shape() -> None:
    cfg = _yaml_dict(FANOUT)
    result = runner.run_seed_partial(cfg, 0, 0, force_backend="emt")
    assert result["seed"] == 0
    assert result["model_index"] == 0
    assert result["model"] == "emt"
    partial = result["partial"]
    assert partial["seed"] == 0
    assert partial["states"], "no states in the per-seed partial"
    assert partial["model"] == "emt"
    # relax_lattice: false in FANOUT -> this unit never touched the lattice
    assert result["lattice"] == {}


def test_seed_fanout_matches_monolith_run() -> None:
    """The whole point of §B-1: fanning ``run()``'s (model, seed) loop out
    across ``run_seed_partial`` x N + ``aggregate_seed_partials`` must
    reproduce EXACTLY what the monolith ``run_pathway`` computes in one
    shot — this is a dispatch/orchestration change, not a numerics change.
    Both paths are deterministic (seeded rattle, EMT), so results must be
    byte-identical, not just close."""
    cfg = _yaml_dict(FANOUT)
    monolith = runner.run_pathway(cfg)

    seed_results = [runner.run_seed_partial(cfg, seed, 0) for seed in (0, 1)]
    fanout = runner.aggregate_seed_partials(cfg, seed_results)

    assert fanout["content_key"] == monolith["content_key"]
    assert _json_eq(fanout["results_json"]["nodes"], monolith["results_json"]["nodes"])
    assert _json_eq(fanout["results_json"]["edges"], monolith["results_json"]["edges"])
    assert fanout["results_json"]["n_samples"] == monolith["results_json"]["n_samples"]
    assert _json_eq(fanout["graph_json"], monolith["graph_json"])

    # and the scalar summary quest.compute.harvest_measures reads matches too
    from precis_pathway._dispatch_common import summarize

    assert summarize(fanout) == summarize(monolith)


def test_aggregate_seed_partials_retry_skips_nothing_extra() -> None:
    """A retry that re-supplies the SAME partials (e.g. a re-run aggregate
    after a transient failure) is a pure function of its inputs — same
    partials in, same artifact out. (The "skip completed seeds" dedup
    itself lives one layer up, in quest.compute.dispatch_autocatpath's
    content-addressed todo tree — this just confirms the aggregate step
    has no hidden state to make that safe.)"""
    cfg = _yaml_dict(FANOUT)
    seed_results = [runner.run_seed_partial(cfg, seed, 0) for seed in (0, 1)]
    a1 = runner.aggregate_seed_partials(cfg, seed_results)
    a2 = runner.aggregate_seed_partials(cfg, seed_results)
    assert _json_eq(a1["results_json"], a2["results_json"])


def test_content_key_discriminates_config() -> None:
    base = {"name": "x", "mlip": {"backend": "emt"}}
    assert runner.content_key(base) != runner.content_key(
        {"name": "x", "mlip": {"backend": "mace"}}
    )
    assert runner.content_key(base) == runner.content_key(dict(base))


def test_chem_safe_yaml_keeps_NO_a_string() -> None:
    # YAML 1.1 would coerce bare NO -> False; autocatpath's loader must not.
    art = runner.run_pathway_from_yaml(SMOKE)
    assert art["config"]["substrate"] == "NO"


def test_network_topology_and_mermaid_no_compute() -> None:
    # The "argue before you compute" surface: build the network (rule-based, no
    # ML) and render it as text + mermaid.
    from precis_pathway.text_views import (
        graph_to_mermaid,
        topology_to_mermaid,
        topology_to_text,
    )

    topo = runner.network_topology(_yaml_dict(SMOKE))
    assert topo["states"] and topo["steps"]
    assert "NO3" in {s["name"] for s in topo["states"]}  # target intermediate present
    assert all("composition" in s for s in topo["states"])

    mer = topology_to_mermaid(topo)
    assert mer.startswith("flowchart LR") and "-->" in mer
    txt = topology_to_text(topo)
    assert "Intermediates" in txt and "Elementary steps" in txt

    # a computed run renders with energies + barriers
    gmer = graph_to_mermaid(runner.run_pathway_from_yaml(SMOKE)["graph_json"])
    assert "Ea" in gmer and "eV" in gmer


def test_analysis_over_computed_graph() -> None:
    from precis_pathway import analysis

    art = runner.run_pathway_from_yaml(BRANCH)
    g, res = art["graph_json"], art["results_json"]
    root, target = analysis.roots(g, res)
    assert root in {n["id"] for n in g["nodes"]}  # root is a real node, not the label

    rl = analysis.rate_limiting(g, root, target)
    assert rl and rl["ea"] is not None and "→" in rl["step"]

    span = analysis.energetic_span(g, root, target)
    assert span is not None and span >= 0.0

    ranked = analysis.barriers_ranked(g)
    eas = [r["ea"] for r in ranked]
    assert eas == sorted(eas, reverse=True)  # highest barrier first

    path, cols = analysis.profile_positions(g, root, target)
    assert path and cols[0]["kind"] == "state"
    assert any(c["kind"] == "ts" for c in cols)  # ≥1 barrier column on the path


def test_toon_views_and_aligned_compare() -> None:
    from precis_pathway import analysis, toon_views

    a1 = runner.run_pathway_from_yaml(BRANCH)
    meta = {
        "graph": a1["graph_json"],
        "results": a1["results_json"],
        "warnings": a1["warnings"],
    }
    assert toon_views.intermediates_toon(meta).startswith("{state")
    assert "Ea_eV" in toon_views.steps_toon(meta)
    ana = toon_views.analysis_text(meta)
    assert "rate-limiting" in ana and "barriers (descending)" in ana

    # aligned interleaved compare: two candidates sharing the branching network
    a2 = runner.run_pathway_from_yaml(BRANCH.replace("element: Pd", "element: Pt"))

    def _cand(slug: str, art: dict, el: str) -> dict:
        g, res = art["graph_json"], art["results_json"]
        r, t = analysis.roots(g, res)
        return {"slug": slug, "lever": el, "graph": g, "root": r, "target": t}

    out = toon_views.compare_toon([_cand("pd", a1, "Pd"), _cand("pt", a2, "Pt")])
    assert "RATE" in out and "SPAN" in out
    assert "‡" in out  # aligned → barrier columns present (not the scalar fallback)
    assert "pd" in out and "pt" in out


def test_intermediates_and_steps_surface_structure_handles() -> None:
    # gripe 161576: `structure_refs` ({state -> structure ref_id}, written by
    # ingest.py) must round-trip into a drill-down handle an agent can hand
    # straight to get(kind='structure', id=..., view='atom').
    from precis_pathway import toon_views

    art = runner.run_pathway_from_yaml(BRANCH)
    states = art["results_json"]["pathway"]
    assert len(states) >= 2

    meta_with_refs = {
        "graph": art["graph_json"],
        "results": art["results_json"],
        "warnings": art["warnings"],
        "structure_refs": {states[0]: 101, states[1]: 202},
    }
    inter = toon_views.intermediates_toon(meta_with_refs)
    assert "structure" in inter
    assert "st101" in inter and "st202" in inter
    assert "get(kind='structure'" in inter  # the drill-down hint

    steps = toon_views.steps_toon(meta_with_refs)
    assert "structures" in steps
    assert "st101" in steps or "st202" in steps
    assert "get(kind='structure'" in steps

    # no structure_refs at all (older pathway / preview-only run) — never errors,
    # column present but blank, no drill-down hint dangling with nothing to point at.
    meta_no_refs = {
        "graph": art["graph_json"],
        "results": art["results_json"],
        "warnings": art["warnings"],
    }
    inter_bare = toon_views.intermediates_toon(meta_no_refs)
    assert "structure" in inter_bare  # column header still present
    assert "get(kind='structure'" not in inter_bare
    assert "st101" not in inter_bare and "st202" not in inter_bare

    steps_bare = toon_views.steps_toon(meta_no_refs)
    assert "get(kind='structure'" not in steps_bare


def test_pathway_skill_discoverable() -> None:
    import precis.handlers.skill as sk

    sk._SKILLS_MAP_CACHE = None  # re-scan so the bundled skill is seen
    body = sk._load_skills_map().get("precis-pathway-help", "")
    # The skill writes its example with double quotes (house style:
    # `view="compare"`), matching every other precis skill — assert that
    # form, not the single-quoted variant the test originally carried.
    assert body and 'view="compare"' in body and "rate-limiting" in body


# ─────────────────────────── slab input seam (pure) ─────────────────────


def _extxyz(atoms: Any) -> str:
    from ase.io import write as ase_write

    buf = io.StringIO()
    ase_write(buf, atoms, format="extxyz")
    return buf.getvalue()


class _Stop(Exception):
    pass


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def _fake_run(cfg: Any, log: Any = None) -> Any:
        captured["prebuilt_slab"] = getattr(cfg, "_prebuilt_slab", None)
        raise _Stop  # short-circuit before any compute

    monkeypatch.setattr(runner, "run", _fake_run)
    return captured


_SLAB_CONFIG = {
    "substrate": "NO",
    "target": "NO2",
    "network": "oxidation",
    "slab": {"element": "Pd", "size": [2, 2, 3]},
}


def test_run_pathway_hydrates_and_threads_injected_slab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autocatpath.config import SlabConfig
    from autocatpath.structures import build_slab

    captured = _capture_run(monkeypatch)
    slab = build_slab(SlabConfig(element="Pd", size=(2, 2, 3)))
    with pytest.raises(_Stop):
        runner.run_pathway(_SLAB_CONFIG, slab_extxyz=_extxyz(slab))
    got = captured["prebuilt_slab"]
    assert got is not None
    assert len(got) == len(slab)
    assert got.get_chemical_formula() == slab.get_chemical_formula()


def test_run_pathway_without_slab_leaves_config_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_run(monkeypatch)
    with pytest.raises(_Stop):
        runner.run_pathway(_SLAB_CONFIG)
    assert captured["prebuilt_slab"] is None  # label-built slab, as before


def test_injected_slab_does_not_leak_into_content_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Atoms travel on a runtime attr, never into to_dict/content_key —
    so two runs that differ only by slab bytes still key on the config."""
    from autocatpath.config import Config, SlabConfig
    from autocatpath.structures import build_slab

    cfg = Config.from_dict(_SLAB_CONFIG)
    key_before = runner.content_key(cfg.to_dict())
    cfg._prebuilt_slab = build_slab(SlabConfig(element="Pd", size=(2, 2, 3)))  # type: ignore[attr-defined]
    assert runner.content_key(cfg.to_dict()) == key_before


# ─────────────────────────── handler: needs the shared test DB ─────────────


def _handler(store: Store) -> PathwayHandler:
    """Construct the handler AND register it with the hub — `_register_with`
    is what stashes `self.hub` (dispatch does this on load; a bare
    `PathwayHandler(hub=...)` leaves `self.hub` unset)."""
    hub = Hub(store=store)
    h = PathwayHandler(hub=hub)
    h._register_with(hub)
    return h


def test_handler_gated_off_by_default(store: Store) -> None:
    with pytest.raises(InitError):
        PathwayHandler(hub=Hub(store=store))


def test_handler_roundtrip(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    slug = "bridge-smoke"

    put = h.put(id="bridge_smoke", text=SMOKE)
    assert f"created pathway '{slug}'" in put.body, put.body

    ref = pathway_store.get_ref(kind="pathway", id=slug)
    assert ref is not None and ref.meta["results"]["nodes"]
    assert ref.meta["backend_forced"] == "emt"
    blocks = pathway_store.list_blocks_for_ref(ref.id)
    assert blocks and blocks[0].text.startswith("# Methods")

    assert "States (relative energy" in h.get(id=slug, view="profile").body
    assert "Ea=" in h.get(id=slug, view="network").body
    assert "substrate" in h.get(id=slug, view="config").body

    # regen cache-hit
    assert "unchanged (cache hit" in h.put(id="bridge_smoke", text=SMOKE).body

    h.delete(id=slug)
    assert pathway_store.get_ref(kind="pathway", id=slug) is None


def test_preview_no_compute(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    slug = "preview-test"

    r = h.put(id="preview_test", text=SMOKE, mode="preview")
    assert "previewed" in r.body and "mermaid" in r.body, r.body

    ref = pathway_store.get_ref(kind="pathway", id=slug)
    assert ref is not None
    assert ref.meta["status"] == "preview"
    assert ref.meta["topology"]["steps"], "topology not stored"
    # views render on a preview (no results yet)
    assert "flowchart" in h.get(id=slug, view="mermaid").body
    assert "Intermediates" in h.get(id=slug, view="intermediates").body


def test_compare_view(pathway_store: Store) -> None:
    h = _handler(pathway_store)

    h.put(id="cmp_pd", text=BRANCH)  # element Pd, branching (connected)
    h.put(id="cmp_pt", text=BRANCH.replace("element: Pd", "element: Pt"))

    out = h.get(id="cmp-pd", view="compare").body
    assert "RATE" in out and "SPAN" in out, out
    assert "cmp-pd" in out and "cmp-pt" in out, out  # both candidates present


def test_native_structure_ingest(pathway_store: Store) -> None:
    h = _handler(pathway_store)
    slug = "ingest-test"

    h.put(id="ingest_test", text=SMOKE)
    ref = pathway_store.get_ref(kind="pathway", id=slug)
    assert ref is not None
    srefs = ref.meta.get("structure_refs") or {}
    assert srefs, "no structure refs ingested"
    assert set(srefs) <= set(ref.meta["results"]["pathway"])  # one per state

    sid = next(iter(srefs.values()))
    with pathway_store.pool.connection() as c:
        kind = c.execute(
            "SELECT kind FROM refs WHERE ref_id=%s AND deleted_at IS NULL", (sid,)
        ).fetchone()
        natoms_row = c.execute(
            "SELECT count(*) FROM struct_atoms WHERE ref_id=%s AND retired_version IS NULL",
            (sid,),
        ).fetchone()
    assert natoms_row is not None
    natoms = natoms_row[0]
    assert kind and kind[0] == "structure"
    assert natoms > 0, "structure ref has no atoms"

    # linked back to the pathway
    links = pathway_store.links_for(ref.id, direction="out", relation="related-to")
    assert any(getattr(link, "dst_ref_id", None) == sid for link in links)


def test_put_dispatches_job_when_route_node_set(
    pathway_store: Store,
    register_autocatpath_explore: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured route node ⇒ mint an `autocatpath_explore` job parented on
    the pathway (ADR 0044 compute lane, via `can_own_jobs`), not an inline run."""
    monkeypatch.setenv("PRECIS_AUTOCATPATH_ROUTE_NODE", "spark")
    hub = Hub(store=pathway_store)
    h = _try(PathwayHandler, hub=hub)
    assert h is not None

    r = h.put(id="route_test", text=SMOKE)
    assert "dispatched autocatpath compute" in r.body and "spark" in r.body, r.body

    ref = pathway_store.get_ref(kind="pathway", id="route-test")
    assert ref is not None
    assert ref.meta["status"] == "computing"
    assert ref.meta["route_node"] == "spark"

    with pathway_store.pool.connection() as c:
        rows = c.execute(
            "SELECT ref_id, meta FROM refs WHERE kind='job' AND parent_id=%s "
            "AND deleted_at IS NULL",
            (ref.id,),
        ).fetchall()
    assert rows, "no autocatpath_explore job minted"
    _job_id, jmeta = rows[0]
    assert jmeta["job_type"] == "autocatpath_explore"
    assert jmeta["executor"] == "ssh_node"
    assert jmeta["params"]["target_node"] == "spark"


def test_autocatpath_explore_dispatch_writes_back(pathway_store: Store) -> None:
    """The `autocatpath_explore` job dispatch (what ssh_node runs) computes +
    writes back onto the pathway ref."""
    slug = "dispatch-test"
    eff = runner.effective_config(_yaml_dict(SMOKE), force_backend="emt")
    with pathway_store.tx() as c:
        ref = pathway_store.insert_ref(
            kind="pathway",
            slug=slug,
            title="t",
            meta={"content_key": runner.content_key(eff), "status": "computing"},
            conn=c,
        )
        from precis.store.types import BlockInsert

        pathway_store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0, text="placeholder", meta={"chunk_kind": "pathway_body"}
                )
            ],
            conn=c,
        )
    ctx = _FakeCtx(
        store=pathway_store,
        params={
            "pathway_ref_id": ref.id,
            "config": _yaml_dict(SMOKE),
            "force_backend": "emt",
            "target_node": "spark",
        },
    )
    pathway_job._dispatch(ctx, pathway_job.SPEC)

    assert ctx.failure is None, ctx.failure
    assert ctx.status == "succeeded"
    got = pathway_store.get_ref(kind="pathway", id=slug)
    assert got is not None
    assert got.meta["results"]["nodes"], "results not written back"
    assert got.meta["produced_by"] == "autocatpath_explore"
    assert got.meta["ran_on"] == "spark"
    blocks = pathway_store.list_blocks_for_ref(got.id)
    assert blocks[0].text.startswith("# Methods")


# ─────────────────── §B-1 seed fan-out: seed / aggregate job dispatch ──────


def test_seed_job_dispatch_writes_partial_onto_its_own_meta(
    pathway_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `autocatpath_seed` job dispatch (what ssh_node runs — the gr180096
    wedge fix) runs ONE (model, seed) unit and stashes the partial for the
    sibling aggregate job to read.

    ``_dispatch`` now runs the compute OUT of the worker process (gr191351 —
    the in-worker MACE/CUDA deadlock). Here we route the subprocess variant to
    the in-process ``run_seed_partial`` so the shape assertions stay fast and
    deterministic without a real child spawn; the real subprocess boundary is
    covered by ``test_run_seed_partial_subprocess_*`` below."""
    from precis_pathway import seed_job

    monkeypatch.setattr(
        runner,
        "run_seed_partial_subprocess",
        lambda *a, **k: runner.run_seed_partial(
            *a, **{kk: vv for kk, vv in k.items() if kk != "timeout"}
        ),
    )

    ctx = _FakeCtx(
        store=pathway_store,
        params={
            "config": _yaml_dict(FANOUT),
            "force_backend": "emt",
            "seed": 1,
            "model_index": 0,
            "content_key": "deadbeef",
            "target_node": "spark",
        },
    )
    seed_job._dispatch(ctx, seed_job.SPEC)

    assert ctx.failure is None, ctx.failure
    assert ctx.status == "succeeded"
    assert ctx.meta_updates["content_key"] == "deadbeef"
    assert ctx.meta_updates["seed"] == 1
    assert ctx.meta_updates["model_index"] == 0
    assert ctx.meta_updates["model"] == "emt"
    partial = ctx.meta_updates["partial"]
    assert isinstance(partial, dict) and partial["states"]
    assert any(kind == "job_summary" for kind, _text in ctx.chunks)


def test_seed_job_dispatch_sizes_timeout_from_wall_seconds(
    pathway_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_dispatch`` runs the compute out-of-process and bounds it at the job's
    declared ``resources.wall_seconds`` (gr191351) — the value that keeps a
    genuine MACE/CUDA hang from wedging the worker for the whole ssh_node lease.
    Passing no wall budget falls back to the subprocess default (unset)."""
    from precis_pathway import seed_job

    seen: dict[str, Any] = {}

    def _fake_sub(
        config: dict[str, Any],
        seed: int,
        model_index: int,
        *,
        force_backend: str | None = None,
        slab_extxyz: str | None = None,
        timeout: int = runner._DEFAULT_SEED_TIMEOUT_S,
    ) -> dict[str, Any]:
        seen.update(
            seed=seed,
            model_index=model_index,
            timeout=timeout,
            force_backend=force_backend,
        )
        return {
            "seed": seed,
            "model": "emt",
            "model_index": model_index,
            "partial": {"states": {"s0": {}}},
            "lattice": {},
        }

    monkeypatch.setattr(runner, "run_seed_partial_subprocess", _fake_sub)

    ctx = _FakeCtx(
        store=pathway_store,
        params={
            "config": _yaml_dict(FANOUT),
            "force_backend": "emt",
            "seed": 2,
            "model_index": 0,
            "content_key": "k",
            "resources": {"wall_seconds": 1234},
        },
    )
    seed_job._dispatch(ctx, seed_job.SPEC)

    assert ctx.status == "succeeded", ctx.failure
    assert seen["seed"] == 2
    assert seen["force_backend"] == "emt"
    assert seen["timeout"] == 1234  # the declared wall budget, not the default


def test_run_seed_partial_subprocess_end_to_end_emt() -> None:
    """The real out-of-process boundary: a fresh child runs one EMT seed and the
    parent parses back the JSON-serialisable partial. Proves the subprocess round
    -trip (the gr191351 isolation) actually works end-to-end, EMT standing in for
    the MACE backend that must not load in the worker process."""
    cfg = _yaml_dict(FANOUT)
    result = runner.run_seed_partial_subprocess(cfg, 0, 0, force_backend="emt")
    assert result["seed"] == 0
    assert result["model"] == "emt"
    assert result["partial"]["states"], "no states in the per-seed partial"


def test_run_seed_partial_subprocess_timeout_is_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run that exceeds its wall budget is SIGKILLed by subprocess and surfaces
    as a clean RuntimeError — so the blocking ssh_node dispatch returns (failed)
    rather than wedging the worker pass for the lease horizon (gr191351)."""
    import subprocess

    def _fake_run(cmd: list[str], **kw: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd, float(kw.get("timeout") or 1))

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="wall budget"):
        runner.run_seed_partial_subprocess({}, 0, 0, timeout=1)


def test_run_seed_partial_subprocess_child_error_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that reports an in-band error envelope (rc 0, ok=False) raises a
    RuntimeError naming the error — the parent never mistakes it for success."""
    import json as _json
    import subprocess

    def _fake_run(cmd: list[str], **kw: Any) -> Any:
        out_path = cmd[-1]
        with open(out_path, "w", encoding="utf-8") as fh:
            _json.dump({"ok": False, "error": "ValueError: bad backend"}, fh)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="compute error.*bad backend"):
        runner.run_seed_partial_subprocess({}, 0, 0, timeout=10)


def test_subprocess_main_round_trips_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child entrypoint writes an ``ok`` envelope on success and an error
    envelope (never a bare crash) on failure — the contract the parent parses."""
    import json as _json

    monkeypatch.setattr(
        runner, "run_seed_partial", lambda *a, **k: {"seed": 3, "model": "emt"}
    )
    req = tmp_path / "req.json"
    out = tmp_path / "out.json"
    req.write_text(_json.dumps({"config": {}, "seed": 3, "model_index": 0}))
    rc = runner._subprocess_main(["prog", str(req), str(out)])
    assert rc == 0
    payload = _json.loads(out.read_text())
    assert payload["ok"] and payload["result"]["seed"] == 3

    def _boom(*a: Any, **k: Any) -> Any:
        raise ValueError("nope")

    monkeypatch.setattr(runner, "run_seed_partial", _boom)
    rc = runner._subprocess_main(["prog", str(req), str(out)])
    assert rc == 1
    payload = _json.loads(out.read_text())
    assert payload["ok"] is False and "nope" in payload["error"]


def test_seed_job_dispatch_malformed_params_fails_cleanly(
    pathway_store: Store,
) -> None:
    from precis_pathway import seed_job

    ctx = _FakeCtx(store=pathway_store, params={"config": {}})  # missing seed
    seed_job._dispatch(ctx, seed_job.SPEC)
    assert ctx.status == "failed"
    assert ctx.failure and "malformed params" in ctx.failure


def _seed_a_todo_tree(
    store: Store, cfg: dict, seeds: tuple[int, ...] = (0, 1)
) -> tuple[Any, int]:
    """Build a minimal (pathway ref + aggregate todo + N succeeded seed-todo
    -> seed-job children) tree, mirroring the shape
    ``quest.compute.dispatch_autocatpath`` mints — enough for
    ``aggregate_job._dispatch`` to walk. Returns ``(pathway_ref, agg_todo_id)``."""
    from precis.store import Tag
    from precis.store.types import BlockInsert

    eff = runner.effective_config(cfg, force_backend="emt")
    with store.tx() as c:
        ref = store.insert_ref(
            kind="pathway",
            slug="aggregate-dispatch-test",
            title="t",
            meta={"content_key": runner.content_key(eff), "status": "computing"},
            conn=c,
        )
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0, text="placeholder", meta={"chunk_kind": "pathway_body"}
                )
            ],
            conn=c,
        )
        agg_todo = store.insert_ref(
            kind="todo", slug=None, title="agg", meta={}, conn=c
        )
        for seed in seeds:
            result = runner.run_seed_partial(cfg, seed, 0, force_backend="emt")
            seed_todo = store.insert_ref(
                kind="todo",
                slug=None,
                title=f"seed {seed}",
                meta={},
                parent_id=agg_todo.id,
                conn=c,
            )
            job = store.insert_ref(
                kind="job",
                slug=None,
                title="seed job",
                meta={
                    "job_type": "autocatpath_seed",
                    "seed": seed,
                    "model_index": 0,
                    "model": result["model"],
                    "partial": result["partial"],
                    "lattice": result.get("lattice") or {},
                },
                parent_id=seed_todo.id,
                conn=c,
            )
            store.add_tag(
                job.id, Tag.closed("STATUS", "succeeded"), set_by="system", conn=c
            )
    return ref, int(agg_todo.id)


def test_aggregate_job_dispatch_combines_seed_partials_and_writes_pathway(
    pathway_store: Store,
) -> None:
    """The `autocatpath_aggregate` job dispatch (what the dispatch worker
    mints under T_agg once every seed todo is done — see
    ``quest.compute.dispatch_autocatpath``'s docstring) walks the seed-todo
    tree, combines the partials (pure numpy), and writes the SAME pathway
    contract `autocatpath_explore` used to write in one shot."""
    from precis_pathway import aggregate_job

    cfg = _yaml_dict(FANOUT)
    ref, agg_todo_id = _seed_a_todo_tree(pathway_store, cfg)

    ctx = _FakeCtx(
        store=pathway_store,
        params={
            "pathway_ref_id": ref.id,
            "pathway_slug": ref.slug,
            "config": cfg,
            "force_backend": "emt",
            "target_node": "spark",
        },
    )
    ctx.meta["dispatched_from_todo"] = agg_todo_id
    aggregate_job._dispatch(ctx, aggregate_job.SPEC)

    assert ctx.failure is None, ctx.failure
    assert ctx.status == "succeeded"
    assert ctx.meta_updates["n_states"] > 0
    got = pathway_store.get_ref(kind="pathway", id=ref.slug)
    assert got is not None
    assert got.meta["results"]["nodes"], "results not written back"
    assert got.meta["produced_by"] == "autocatpath_aggregate"
    assert got.meta["ran_on"] == "spark"
    assert got.meta["n_seed_partials"] == 2
    blocks = pathway_store.list_blocks_for_ref(got.id)
    assert blocks[0].text.startswith("# Methods")

    # matches what the monolith would have produced on the same seeds
    monolith = runner.run_pathway(cfg, force_backend="emt")
    assert _json_eq(got.meta["results"]["nodes"], monolith["results_json"]["nodes"])


def test_aggregate_job_dispatch_no_seed_partials_fails_cleanly(
    pathway_store: Store,
) -> None:
    """An aggregate job minted with no succeeded seed underneath it (a bad
    tree, or all seeds still failing) fails loud rather than writing a
    hollow pathway result."""
    from precis.store.types import BlockInsert
    from precis_pathway import aggregate_job

    cfg = _yaml_dict(FANOUT)
    eff = runner.effective_config(cfg, force_backend="emt")
    with pathway_store.tx() as c:
        ref = pathway_store.insert_ref(
            kind="pathway",
            slug="empty-aggregate-test",
            title="t",
            meta={"content_key": runner.content_key(eff), "status": "computing"},
            conn=c,
        )
        pathway_store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0, text="placeholder", meta={"chunk_kind": "pathway_body"}
                )
            ],
            conn=c,
        )
        agg_todo = pathway_store.insert_ref(
            kind="todo", slug=None, title="agg", meta={}, conn=c
        )

    ctx = _FakeCtx(
        store=pathway_store,
        params={"pathway_ref_id": ref.id, "config": cfg, "force_backend": "emt"},
    )
    ctx.meta["dispatched_from_todo"] = int(agg_todo.id)
    aggregate_job._dispatch(ctx, aggregate_job.SPEC)

    assert ctx.status == "failed"
    assert ctx.failure and "no succeeded seed partials" in ctx.failure


def test_aggregate_job_dispatch_requires_dispatched_from_todo(
    pathway_store: Store,
) -> None:
    """A misconfigured aggregate job (not minted under T_agg by the dispatch
    worker) fails loud instead of silently aggregating nothing."""
    from precis_pathway import aggregate_job

    ctx = _FakeCtx(
        store=pathway_store,
        params={"pathway_ref_id": 1, "config": _yaml_dict(FANOUT)},
    )
    aggregate_job._dispatch(ctx, aggregate_job.SPEC)
    assert ctx.status == "failed"
    assert ctx.failure and "dispatched_from_todo" in ctx.failure
