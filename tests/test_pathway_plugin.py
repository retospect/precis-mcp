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
    assert body and "view='compare'" in body and "rate-limiting" in body


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
        natoms = c.execute(
            "SELECT count(*) FROM struct_atoms WHERE ref_id=%s AND retired_version IS NULL",
            (sid,),
        ).fetchone()[0]
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
