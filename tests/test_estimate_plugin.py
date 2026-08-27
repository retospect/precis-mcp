"""Tests for `EstimateHandler` — the `estimate` kind's composition (slice 1)
and structure-coupled (slice 2) tiers
(docs/backlog/estimate-kind-ms-chemistry-workup.md).

Cache flow itself is `CacheBackedHandler`'s job (covered by
`test_cache_base.py`); these tests focus on:

- composition-query parsing (space/comma-separated, concatenated formula,
  unknown-symbol rejection) — pure `ase.data` lookups, no `mendeleev`
  needed, so these run unconditionally.
- the rendered composition panel (needs `mendeleev` — importorskip'd).
- cache-hit behaviour (second identical `get` doesn't re-touch mendeleev).
- the optional-dep degrade: the module imports cleanly and `get()` raises
  a clean, actionable error when `mendeleev` isn't installed.
- slice 2: the structure workup panel (geometry lint / coordination-strain
  / symmetry / dedup / BEP), what-if ops, `view='compare'`, and the
  structure-tier cache key (needs `ase`/`spglib`, core deps; dedup needs
  `pymatgen`, importorskip'd per-test).
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import numpy as np
import pytest

import precis_estimate
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound, Unsupported, Upstream
from precis.store import Store
from precis.structure import Scene, apply_ops
from precis.structure.cell import Cell
from precis.utils import handle_registry
from precis_estimate.handler import EstimateHandler, _parse_composition

#: Raw plugin migration files (kinds/providers rows the store's
#: `insert_ref`/`put_cache_entry` require). The shared session-scoped test
#: template only migrates precis-core (``tests/conftest.py``'s
#: ``_initialise_test_db`` builds a bare-dir `Migrator`, no plugin
#: `discover_sources`), and the dev container's installed
#: `entry_points.txt` is a build-time snapshot that doesn't see a
#: pyproject entry point added mid-worktree — so plugin-kind tests seed
#: their own migration directly. Mirrors `test_pathway_plugin.py`'s
#: `pathway_store` fixture.
_MIGRATIONS_DIR = Path(precis_estimate.__file__).parent / "migrations"

# ── composition parsing (no mendeleev needed) ───────────────────────────


@pytest.mark.parametrize(
    "query",
    ["Pd Zr H", "PdZrH", "Pd, Zr, H", "pd zr h", "  Pd   Zr  H  ", "H, PdZr"],
)
def test_parse_composition_variants_agree(query: str) -> None:
    assert _parse_composition(query) == ["H", "Pd", "Zr"]


def test_parse_composition_drops_stoichiometry_digits() -> None:
    assert _parse_composition("PdZrH2") == ["H", "Pd", "Zr"]


def test_parse_composition_dedupes() -> None:
    assert _parse_composition("Pd Pd Zr") == ["Pd", "Zr"]


def test_parse_composition_single_element() -> None:
    assert _parse_composition("Pd") == ["Pd"]


def test_parse_composition_unknown_symbol_names_it() -> None:
    with pytest.raises(BadInput, match="Xx"):
        _parse_composition("Pd Xx")


def test_parse_composition_empty_query_is_bad_input() -> None:
    with pytest.raises(BadInput):
        _parse_composition("   ")


# ── handler fixture ──────────────────────────────────────────────────────


@pytest.fixture
def handler(hub: Hub, store: Store) -> EstimateHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return EstimateHandler(hub=hub)


# ── panel rendering (needs mendeleev) ────────────────────────────────────

pytest.importorskip("mendeleev")


def test_panel_has_per_element_rows_and_pairwise_section(
    handler: EstimateHandler,
) -> None:
    resp = handler.get(q="Pd Zr")

    assert "## Elements" in resp.body
    assert "Pd" in resp.body
    assert "Zr" in resp.body
    assert "## Pairwise" in resp.body
    # Pairwise rows follow the panel's Z-ascending element order (Zr, Z=40,
    # before Pd, Z=46) — not the alphabetical order the cache key sorts by.
    assert "Zr-Pd" in resp.body
    # epistemic-grade footer
    assert "hypothesis-generating only" in resp.body
    assert "measure" in resp.body


def test_single_element_query_has_no_pairwise_section(
    handler: EstimateHandler,
) -> None:
    resp = handler.get(q="Pd")
    assert "## Elements" in resp.body
    assert "## Pairwise" not in resp.body


def test_dband_row_present_for_vendored_metal_absent_for_others(
    handler: EstimateHandler,
) -> None:
    resp = handler.get(q="Pd Zr")
    # Pd is in the vendored Hammer-Norskov table; Zr is not.
    lines = resp.body.splitlines()
    pd_line = next(line for line in lines if line.startswith("46\t"))
    zr_line = next(line for line in lines if line.startswith("40\t"))
    assert "-1.83" in pd_line
    assert zr_line.rstrip().endswith("—")


def test_unknown_view_raises_unsupported_naming_planned_views(
    handler: EstimateHandler,
) -> None:
    with pytest.raises(Unsupported) as exc_info:
        handler.get(q="Pd Zr", view="shape")
    assert exc_info.value.options is not None
    assert "shape" in exc_info.value.options


def test_second_call_hits_cache(
    handler: EstimateHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mendeleev

    calls: list[str] = []
    real_element = mendeleev.element

    def _spy(symbol: str) -> object:
        calls.append(symbol)
        return real_element(symbol)

    monkeypatch.setattr(mendeleev, "element", _spy)

    handler.get(q="Pd Zr")
    n_after_first = len(calls)
    assert n_after_first > 0

    resp2 = handler.get(q="Pd Zr")
    # No new mendeleev lookups is the real cache-hit signal here — `estimate`
    # is a $0 provider, and `_cost_str` renders zero-cost entries as the
    # flat '[cost: free]' on both hit and miss (no '- cached' suffix),
    # unlike the paid kinds (`math`'s own cache-hit test asserts on that
    # suffix precisely because Wolfram is metered).
    assert len(calls) == n_after_first
    assert resp2.cost == "[cost: free]"


def test_query_variants_share_one_cache_row(
    handler: EstimateHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mendeleev

    calls: list[str] = []
    real_element = mendeleev.element

    def _spy(symbol: str) -> object:
        calls.append(symbol)
        return real_element(symbol)

    monkeypatch.setattr(mendeleev, "element", _spy)

    handler.get(q="Pd Zr")
    n_after_first = len(calls)
    handler.get(q="PdZr")  # same composition, different spelling
    assert len(calls) == n_after_first


# ── optional-dep degrade (works with or without mendeleev installed) ────


def test_module_imports_and_get_raises_clean_error_without_extra(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler module must import cleanly with the `estimate` extra
    absent, and a `get()` in that state must fail with a clean, actionable
    error — never an opaque `ModuleNotFoundError` traceback. Simulated by
    poisoning `sys.modules['mendeleev'] = None`, the standard trick to make
    `import mendeleev` raise `ImportError` regardless of whether the real
    package is actually installed in this test environment.
    """
    monkeypatch.setitem(sys.modules, "mendeleev", None)

    import precis_estimate.handler as handler_mod

    # Neither the module nor `__init__` may touch mendeleev — only `_fetch`
    # may, and only lazily. Nothing here references mendeleev at module
    # scope, so reloading with it "absent" is a no-op restore once
    # monkeypatch reverts sys.modules at teardown.
    importlib.reload(handler_mod)  # must not raise even with mendeleev "gone"
    h = handler_mod.EstimateHandler(hub=hub)  # must not touch mendeleev
    with pytest.raises(Upstream, match="estimate.*extra"):
        h.get(q="Pd Zr H")


def test_store_only_construction_does_not_import_mendeleev(store: Store) -> None:
    """`__init__` itself must not touch mendeleev — only `_fetch` may."""
    EstimateHandler(hub=Hub(store=store))  # no exception even without deps


# ── structure tier (slice 2) ─────────────────────────────────────────────
#
# Small fcc slabs, built directly via `apply_ops` + `store.structure_save`
# (bypassing `StructureHandler.put` — no relax, no dispatch), mirroring
# `test_structure_kernel.py`'s `_pd_slab` fixture style. `handler` (the
# module-scoped fixture above) already migrated the `estimate` kind; a
# fresh `EstimateHandler` per test avoids any cross-test cache-key bleed
# since the shared `store` fixture truncates between tests anyway.


def _slab_scene(
    *, element: str = "Pd", size: tuple[int, int, int] = (2, 2, 3), vacuum: float = 10.0
) -> Scene:
    pytest.importorskip("ase")
    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, True)))
    apply_ops(
        scene,
        [{"op": "slab", "element": element, "size": list(size), "vacuum": vacuum}],
    )
    return scene


def _save_structure(store: Store, slug: str, scene: Scene) -> int:
    ref, _created = store.structure_save(
        slug=slug, title=slug, scene=scene, version=1, card_text=slug
    )
    return int(ref.id)


def _mk_quest(hub: Hub, text: str) -> int:
    from precis.handlers.quest import QuestHandler

    resp = QuestHandler(hub=hub).put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _serve(store: Store, quest_id: int, structure_ref_id: int) -> None:
    store.add_link(src_ref_id=structure_ref_id, dst_ref_id=quest_id, relation="serves")


@pytest.fixture
def struct_handler(handler: EstimateHandler) -> EstimateHandler:
    """Alias — the `estimate` migration is already applied by `handler`;
    named separately here purely so the structure-tier tests read as their
    own section."""
    return handler


def test_structure_workup_renders_lint_coordination_symmetry_sections(
    struct_handler: EstimateHandler, store: Store
) -> None:
    scene = _slab_scene()
    # A well-bonded top-site H adsorbate: on top of the highest atom, one
    # covalent-radius-sum above it — never flagged as floating/detached.
    top = max(scene.atoms.values(), key=lambda a: scene.cell.frac_to_cart(a.frac)[2])
    apply_ops(
        scene,
        [
            {
                "op": "add_atom_site",
                "element": "H",
                "site": {"type": "top", "anchors": [top.label]},
            }
        ],
    )
    ref_id = _save_structure(store, "pd-h-slab", scene)
    handle = handle_registry.format_handle("structure", ref_id)

    resp = struct_handler.get(id=handle)

    assert "## Geometry lint" in resp.body
    assert "no lint issues" in resp.body
    assert "## Coordination / strain" in resp.body
    assert "adsorbate" in resp.body
    assert "## Symmetry" in resp.body
    assert "hypothesis-generating only" in resp.body
    assert "measure" in resp.body


def test_structure_workup_lint_flags_floating_atom(
    struct_handler: EstimateHandler, store: Store
) -> None:
    scene = _slab_scene()
    # Deliberately floating: 6 A above the top layer, far past the 3.5 A
    # `preflight.MAX_ADS_HEIGHT` bonding cutoff.
    top_z = max(float(scene.cell.frac_to_cart(a.frac)[2]) for a in scene.atoms.values())
    apply_ops(
        scene,
        [{"op": "add_atom", "element": "H", "cart": [1.0, 1.0, top_z + 6.0]}],
    )
    ref_id = _save_structure(store, "pd-floating-h", scene)
    resp = struct_handler.get(id=handle_registry.format_handle("structure", ref_id))

    assert "detached" in resp.body
    assert "floats" in resp.body


def test_unknown_view_over_a_structure_handle_still_errors(
    struct_handler: EstimateHandler, store: Store
) -> None:
    ref_id = _save_structure(store, "pd-plain", _slab_scene())
    handle = handle_registry.format_handle("structure", ref_id)
    with pytest.raises(Unsupported) as exc_info:
        struct_handler.get(id=handle, view="orbitals")
    assert exc_info.value.options is not None
    assert "orbitals" in exc_info.value.options


def test_compare_needs_a_structure_id(struct_handler: EstimateHandler) -> None:
    with pytest.raises(BadInput, match="view='compare'"):
        struct_handler.get(q="Pd Zr", view="compare")


def test_structure_id_not_found_raises_not_found(
    struct_handler: EstimateHandler,
) -> None:
    with pytest.raises(NotFound):
        struct_handler.get(id="st999999")


def test_whatif_ops_build_mutant_reflecting_added_atom_without_touching_original(
    struct_handler: EstimateHandler, store: Store
) -> None:
    scene = _slab_scene()
    ref_id = _save_structure(store, "pd-whatif-base", scene)
    top = max(
        scene.atoms, key=lambda la: scene.cell.frac_to_cart(scene.atoms[la].frac)[2]
    )
    handle = handle_registry.format_handle("structure", ref_id)

    resp = struct_handler.get(
        id=handle,
        args={
            "ops": [
                {
                    "op": "add_atom_site",
                    "element": "O",
                    "site": {"type": "top", "anchors": [top]},
                }
            ]
        },
    )
    assert "O" in resp.body
    assert "adsorbate" in resp.body

    # The held design itself is untouched — no O in its own scene.
    base_scene, _handles = store.structure_load(ref_id)
    assert "O" not in {a.element for a in base_scene.atoms.values()}


def test_whatif_bad_op_raises_bad_input(
    struct_handler: EstimateHandler, store: Store
) -> None:
    ref_id = _save_structure(store, "pd-whatif-badop", _slab_scene())
    handle = handle_registry.format_handle("structure", ref_id)
    with pytest.raises(BadInput, match="what-if op error"):
        struct_handler.get(id=handle, args={"ops": [{"op": "not_a_real_op"}]})


def test_dedup_reports_match_and_excludes_composition_mismatch(
    struct_handler: EstimateHandler, store: Store, hub: Hub
) -> None:
    pytest.importorskip("pymatgen")
    qid = _mk_quest(hub, "Lowest-barrier Pd catalyst")

    # Two structure refs built from the SAME ops — geometrically identical,
    # but distinct refs/slugs (the design doc's "st245406≡st237458" motivating
    # case: a symmetry duplicate that content-addressing alone didn't catch).
    twin_a = _save_structure(store, "pd-twin-a", _slab_scene())
    twin_b = _save_structure(store, "pd-twin-b", _slab_scene())
    # A different composition served to the same quest — must never match.
    other = _save_structure(store, "pt-slab", _slab_scene(element="Pt"))
    for sid in (twin_a, twin_b, other):
        _serve(store, qid, sid)

    handle = handle_registry.format_handle("structure", twin_a)
    resp = struct_handler.get(id=handle, args={"quest": f"qu{qid}"})

    assert "## Dedup" in resp.body
    assert "SKIP DISPATCH" in resp.body
    assert handle_registry.format_handle("structure", twin_b) in resp.body
    assert handle_registry.format_handle("structure", other) not in resp.body


def test_dedup_without_quest_context_names_the_arg(
    struct_handler: EstimateHandler, store: Store
) -> None:
    ref_id = _save_structure(store, "pd-no-quest", _slab_scene())
    resp = struct_handler.get(id=handle_registry.format_handle("structure", ref_id))
    assert "no quest context given" in resp.body
    assert "args={'quest'" in resp.body or 'args={"quest"' in resp.body


#: Three alloyed slabs (a vendored-metal dopant swapped into a Pd slab) with
#: distinct compositions, so each gets its own `_weighted_eps_d` — the BEP
#: descriptor needs *some* spread in x to fit a line at all.
_BEP_DOPANTS = ("Au", "Ag", "Ni")


def _doped_slab_scene(dopant: str) -> Scene:
    scene = _slab_scene(size=(3, 3, 2))
    # swap one atom for the dopant — an alloyed surface with its own
    # composition-weighted descriptor, distinct from pure Pd's.
    label = next(iter(scene.atoms))
    apply_ops(scene, [{"op": "set_element", "atom": label, "element": dopant}])
    return scene


def test_bep_fits_trusted_points_and_predicts(
    struct_handler: EstimateHandler, store: Store, hub: Hub
) -> None:
    qid = _mk_quest(hub, "BEP scaling test")
    for i, dopant in enumerate(_BEP_DOPANTS):
        sid = _save_structure(store, f"bep-{dopant.lower()}", _doped_slab_scene(dopant))
        store.stamp_ref_meta(sid, {"barrier": 0.3 + 0.1 * i, "barrier_trusted": True})
        _serve(store, qid, sid)

    target_id = _save_structure(store, "bep-target", _doped_slab_scene("Pt"))
    _serve(store, qid, target_id)

    resp = struct_handler.get(
        id=handle_registry.format_handle("structure", target_id),
        args={"quest": f"qu{qid}"},
    )
    assert "## BEP" in resp.body
    bep_section = resp.body.split("## BEP", 1)[1].split("##", 1)[0]
    assert "n_trusted" in bep_section
    assert "insufficient" not in bep_section
    # the fitted-row line starts with the n_trusted count (3 trusted points)
    assert any(line.startswith("3\t") for line in bep_section.splitlines())
    assert (
        "on-trend" in bep_section or "lower" in bep_section or "higher" in bep_section
    )


def test_bep_excludes_untrusted_barrier_from_the_fit(
    struct_handler: EstimateHandler, store: Store, hub: Hub
) -> None:
    qid = _mk_quest(hub, "BEP untrusted-exclusion test")
    for i, dopant in enumerate(_BEP_DOPANTS[:2]):
        sid = _save_structure(
            store, f"bepu-{dopant.lower()}", _doped_slab_scene(dopant)
        )
        store.stamp_ref_meta(sid, {"barrier": 0.3 + 0.1 * i, "barrier_trusted": True})
        _serve(store, qid, sid)
    # A third, untrusted barrier — must not count toward n_trusted.
    untrusted_id = _save_structure(store, "bepu-ni", _doped_slab_scene("Ni"))
    store.stamp_ref_meta(untrusted_id, {"barrier": 0.05, "barrier_trusted": False})
    _serve(store, qid, untrusted_id)

    resp = struct_handler.get(
        id=handle_registry.format_handle("structure", untrusted_id),
        args={"quest": f"qu{qid}"},
    )
    assert "insufficient trusted barriers (n=2)" in resp.body


def test_compare_renders_delta_table(
    struct_handler: EstimateHandler, store: Store
) -> None:
    a_id = _save_structure(store, "cmp-a", _slab_scene())
    b_id = _save_structure(store, "cmp-b", _doped_slab_scene("Au"))

    resp = struct_handler.get(
        id=handle_registry.format_handle("structure", a_id),
        view="compare",
        args={"against": handle_registry.format_handle("structure", b_id)},
    )
    assert "## Delta" in resp.body
    assert "predicted_barrier_eV" in resp.body
    assert "cmp-a" in resp.body and "cmp-b" in resp.body


def test_structure_cache_hit_skips_recompute(
    struct_handler: EstimateHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = _save_structure(store, "cache-slab", _slab_scene())
    from precis_estimate.compute import structure as compute_structure

    calls: list[int] = []
    real = compute_structure.structure_workup

    def _spy(*args: object, **kwargs: object) -> str:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(compute_structure, "structure_workup", _spy)

    handle = handle_registry.format_handle("structure", ref_id)
    resp1 = struct_handler.get(id=handle)
    assert len(calls) == 1
    resp2 = struct_handler.get(id=handle)
    assert len(calls) == 1  # cache hit — no recompute
    assert resp2.cost == "[cost: free]"
    assert resp1.body == resp2.body
