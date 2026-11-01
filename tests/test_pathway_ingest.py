"""``precis_pathway.ingest`` — the injective per-state structure slug and
the seed-job re-ingest repair path (gr549179).

Engine-free: the extxyz is plain ``ase.Atoms`` (``ase`` is a core dep);
no autocatpath import anywhere.
"""

from __future__ import annotations

import io
from typing import Any

from precis.store import Store, Tag
from precis_pathway import ingest as pw_ingest


def _extxyz(symbols: list[str]) -> str:
    from ase import Atoms
    from ase.io import write as ase_write

    atoms = Atoms(
        symbols,
        positions=[(float(i), 0.0, 0.0) for i in range(len(symbols))],
        cell=[10.0, 10.0, 10.0],
        pbc=True,
    )
    buf = io.StringIO()
    ase_write(buf, atoms, format="extxyz")
    return buf.getvalue()


def _pathway(store: Store, slug: str = "pw-ingest", **meta: Any) -> Any:
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title=f"{slug} pathway",
        meta={"content_key": "ck-test", "status": "ready", **meta},
    )


def _seed_job(
    store: Store,
    pathway_id: int,
    *,
    status: str = "succeeded",
    structures: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="seed",
        meta={
            "job_type": "autocatpath_seed",
            "content_key": "ck-test",
            "params": {"pathway_ref_id": pathway_id},
            "structures": structures or {},
        },
    )
    store.add_tag(ref.id, Tag.parse_strict(f"STATUS:{status}"), set_by="agent")
    return int(ref.id)


# ── the slug itself ──────────────────────────────────────────────


def test_state_slugs_lone_names_keep_legacy_slug() -> None:
    """A state that collides with nothing WITHIN ITS PATHWAY keeps the
    legacy slug even when it carries characters ``_slug`` drops —
    already-ingested pathways keep resolving and re-ingesting in place."""
    for name, want in (
        ("*~co", "co"),
        ("CO*", "co"),
        ("H2O(g)", "h2o-g"),
        ("NH2OH", "nh2oh"),
        ("*", "x"),
        ("inter-1", "inter-1"),
    ):
        assert pw_ingest.state_slugs([name])[name] == want


def test_state_slugs_collision_pairs_diverge() -> None:
    """gr549179: ``_slug`` folded ``*~nh2oh`` → ``nh2oh`` — the same ref as
    adsorbed ``NH2OH``, so one geometry overwrote the other (prod
    pw527720 mapped both states to the clean slab). Inside a colliding
    group the member with no dropped characters keeps the legacy slug;
    the rest take a ``-<sha8>`` suffix."""
    for sink, adsorbed in (
        ("*~nh2oh", "NH2OH"),
        ("*~h2o", "H2O"),
        ("*~h2", "H2"),
        ("*", "x"),
    ):
        got = pw_ingest.state_slugs([sink, adsorbed])
        assert got[adsorbed] == pw_ingest._slug(adsorbed)
        assert got[sink] != got[adsorbed]
        assert got[sink].startswith(f"{pw_ingest._slug(sink)}-")


def test_state_slugs_ties_fall_to_sorted_order() -> None:
    """No member — or two members — with clean names: the first in
    sorted order keeps the legacy slug (``H2`` over ``h2``, ``*a`` over
    ``*~a``). Deterministic, so re-ingest is idempotent."""
    got = pw_ingest.state_slugs(["h2", "H2"])
    assert got["H2"] == "h2"
    assert got["h2"].startswith("h2-") and got["h2"] != "h2"
    got = pw_ingest.state_slugs(["*~a", "*a"])
    assert got["*a"] == "a" and got["*~a"].startswith("a-")


def test_colliding_states_names_the_legacy_groups() -> None:
    got = pw_ingest.colliding_states(["NH2OH", "*~nh2oh", "H2", "CO"])
    assert got == {"nh2oh": ["NH2OH", "*~nh2oh"]}
    assert pw_ingest.colliding_states(["NH2OH", "H2", "CO"]) == {}


# ── store-level ingest ───────────────────────────────────────────

_BARE = ["Pd"] * 4
_COVERED = ["Pd"] * 4 + ["N", "H", "H", "O"]


def test_ingest_keeps_sink_and_adsorbed_on_disjoint_refs(store: Store) -> None:
    """The gr549179 shape: ``NH2OH`` adsorbed (slab + molecule) and
    ``*~nh2oh`` desorbed (clean slab) must store two structures with the
    right atoms — and ``NH2OH`` keeps the legacy ``-nh2oh`` slug so the
    collided ref is repaired in place, not orphaned."""
    ref = _pathway(store)
    structures = {
        "NH2OH": _extxyz(_COVERED),
        "*~nh2oh": _extxyz(_BARE),
    }
    out = pw_ingest.ingest_intermediates(
        store, int(ref.id), "pw-ingest", "ck-test", structures
    )
    assert set(out) == {"NH2OH", "*~nh2oh"}
    assert out["NH2OH"] != out["*~nh2oh"]

    slugs = pw_ingest.intermediate_slugs("ck-test", structures)
    assert slugs["NH2OH"].endswith("-nh2oh")
    assert slugs["*~nh2oh"].startswith(slugs["NH2OH"] + "-")
    assert slugs["*~nh2oh"] != slugs["NH2OH"]

    fetched = store.fetch_refs_by_ids([out["NH2OH"], out["*~nh2oh"]])
    assert fetched[out["NH2OH"]].meta["composition"] == {
        "Pd": 4,
        "N": 1,
        "H": 2,
        "O": 1,
    }
    assert fetched[out["*~nh2oh"]].meta["composition"] == {"Pd": 4}

    links = store.links_for(int(ref.id), direction="out", relation="related-to")
    assert {getattr(link, "dst_ref_id", None) for link in links} >= set(out.values())


# ── the seed-job re-ingest repair path ───────────────────────────


def test_reingest_pathway_structures_from_seed_meta(store: Store) -> None:
    """``meta.structures`` on the seed jobs is keyed by the FULL state
    name, so it holds both geometries the slug collision lost. Re-ingest
    rewrites ``meta.structure_refs`` with disjoint refs and picks the
    min-energy geometry per state across seeds."""
    ref = _pathway(store)
    _seed_job(
        store,
        int(ref.id),
        structures={
            "NH2OH": {"energy": -3.0, "extxyz": _extxyz(_COVERED)},
            "*~nh2oh": {"energy": -1.0, "extxyz": _extxyz(_BARE)},
        },
    )
    # a second seed losing on energy for NH2OH must not win; a failed
    # seed contributes nothing at all.
    _seed_job(
        store,
        int(ref.id),
        structures={"NH2OH": {"energy": -2.0, "extxyz": _extxyz(_BARE)}},
    )
    _seed_job(
        store,
        int(ref.id),
        status="failed",
        structures={"NH2OH": {"energy": -9.0, "extxyz": _extxyz(_BARE)}},
    )

    res = pw_ingest.reingest_pathway_structures(store, ref)

    out = res.structure_refs
    assert set(out) == {"NH2OH", "*~nh2oh"}
    assert res.written == res.input_states == 2 and not res.skipped
    assert out["NH2OH"] != out["*~nh2oh"]
    meta = store.get_ref(kind="pathway", id=int(ref.id))
    assert meta is not None and meta.meta["structure_refs"] == out
    assert meta.meta["n_structures"] == 2

    fetched = store.fetch_refs_by_ids([out["NH2OH"], out["*~nh2oh"]])
    assert fetched[out["NH2OH"]].meta["composition"]["N"] == 1
    assert fetched[out["*~nh2oh"]].meta["composition"] == {"Pd": 4}


def test_reingest_keeps_prior_entry_when_geometry_unreadable(
    store: Store,
) -> None:
    """A state whose seed geometry fails to parse keeps its existing
    ``structure_refs`` entry — the pass repairs what it can instead of
    dropping the state from the map (and reports written vs input)."""
    ref = _pathway(store)
    first = pw_ingest.ingest_intermediates(
        store,
        int(ref.id),
        "pw-ingest",
        "ck-test",
        {"NH2OH": _extxyz(_COVERED), "*~nh2oh": _extxyz(_BARE)},
    )
    store.stamp_ref_meta(int(ref.id), {"structure_refs": first})
    _seed_job(
        store,
        int(ref.id),
        structures={
            "NH2OH": {"energy": -3.0, "extxyz": "not extxyz"},
            "*~nh2oh": {"energy": -1.0, "extxyz": _extxyz(_BARE)},
        },
    )

    res = pw_ingest.reingest_pathway_structures(store, ref)

    assert res.input_states == 2 and res.written == 1
    assert res.skipped == ("NH2OH",)
    assert res.structure_refs["NH2OH"] == first["NH2OH"]
    sink = res.structure_refs["*~nh2oh"]
    assert sink != res.structure_refs["NH2OH"]
    fetched = store.fetch_refs_by_ids([sink])
    assert fetched[sink].meta["composition"] == {"Pd": 4}
    meta = store.get_ref(kind="pathway", id=int(ref.id))
    assert meta is not None
    assert meta.meta["structure_refs"] == res.structure_refs


def test_reingest_pathway_structures_empty_without_seed_geometry(
    store: Store,
) -> None:
    ref = _pathway(store, slug="pw-monolith")
    _seed_job(store, int(ref.id), structures={})
    res = pw_ingest.reingest_pathway_structures(store, ref)
    assert res.input_states == res.written == 0 and not res.skipped
    meta = store.get_ref(kind="pathway", id=int(ref.id))
    assert meta is not None and "structure_refs" not in meta.meta


def test_seed_geometry_merges_min_energy(store: Store) -> None:
    ref = _pathway(store)
    _seed_job(
        store,
        int(ref.id),
        structures={"NH2OH": {"energy": -2.0, "extxyz": _extxyz(_COVERED)}},
    )
    _seed_job(
        store,
        int(ref.id),
        structures={"NH2OH": {"energy": -5.0, "extxyz": _extxyz(_BARE)}},
    )
    structures, key = pw_ingest.seed_geometry(store, int(ref.id))
    assert key == "ck-test"
    assert structures.keys() == {"NH2OH"}
    # the -5.0 geometry won — a 4-atom extxyz, not the 8-atom one
    assert structures["NH2OH"].splitlines()[0] == "4"
