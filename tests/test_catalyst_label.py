"""gr553416 — :func:`precis.structure.catalyst_label.catalyst_label`
renders the readable catalyst name ('Pd(111) + Ta (subsurface) +
vacancy (surface, co-located)') from the recorded construction —
recipe ops, ``meta.params`` — never by parsing the title string.
"""

from __future__ import annotations

from precis.structure.catalyst_label import catalyst_label

_SLAB = {"op": "slab", "element": "Pd", "size": [3, 3, 4]}


def test_clean_slab() -> None:
    assert catalyst_label([_SLAB]) == "Pd(111), clean"


def test_single_surface_substitution() -> None:
    ops = [
        _SLAB,
        {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
    ]
    assert catalyst_label(ops) == "Pd(111) + Cu (surface substitution)"


def test_two_substitutions_of_the_same_element_count() -> None:
    ops = [
        _SLAB,
        {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
        {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
    ]
    assert catalyst_label(ops) == "Pd(111) + 2 Cu (surface substitution)"


def test_subsurface_dopant_plus_colocated_vacancy() -> None:
    """The gripe's own example: a subsurface Ta substitution placed
    ``near`` a top-layer atom the vacancy then removes — the label map
    layers ``aPd35`` of a [3,3,4] slab as surface (atoms mint z-ascending),
    and the ``near`` back-reference makes the vacancy co-located."""
    ops = [
        _SLAB,
        {
            "op": "set_element",
            "element": "Ta",
            "site": {"type": "subsurface", "near": "aPd35"},
        },
        {"op": "vacancy", "atom": "aPd35"},
    ]
    assert (
        catalyst_label(ops)
        == "Pd(111) + Ta (subsurface) + vacancy (surface, co-located)"
    )


def test_vacancy_without_colocation() -> None:
    ops = [_SLAB, {"op": "vacancy", "atom": "aPd35"}]
    assert catalyst_label(ops) == "Pd(111) + vacancy (surface)"


def test_adatom_on_a_named_site() -> None:
    ops = [
        _SLAB,
        {"op": "add_atom_site", "element": "Pt", "site": {"type": "hollow"}},
    ]
    assert catalyst_label(ops) == "Pd(111) + Pt adatom (hollow)"


def test_subsurface_and_interstitial_markers() -> None:
    """``add_atom``'s declared below-surface intent (gr549760) — the
    marker records intent, not depth: without recorded slab geometry it
    reads plain ``subsurface``/``interstitial``."""
    ops = [
        _SLAB,
        {
            "op": "add_atom",
            "element": "B",
            "frac": [0.5, 0.5, 0.4],
            "site": "subsurface",
        },
        {
            "op": "add_atom",
            "element": "N",
            "frac": [0.5, 0.5, 0.3],
            "site": "interstitial",
        },
    ]
    assert catalyst_label(ops) == "Pd(111) + B (subsurface) + N (interstitial)"


def test_marker_depth_from_recorded_lattice() -> None:
    """With the saved ``meta.lattice`` the marker atom's frac z layers it
    for real — a subsurface mark two layers down reads layer 3."""
    # Pd fcc(111) [3,3,4], a=3.89, vacuum 10: c ≈ 26.738, top layer z ≈
    # 16.738; the depth-3 layer sits at z ≈ 12.246 → frac ≈ .458.
    ops = [
        _SLAB,
        {
            "op": "add_atom",
            "element": "B",
            "frac": [0.5, 0.5, 0.458],
            "site": "subsurface",
        },
    ]
    meta = {
        "lattice": [
            [8.2497, 0.0, 0.0],
            [4.1249, 7.1445, 0.0],
            [0.0, 0.0, 26.7377],
        ]
    }
    assert (
        catalyst_label({"ops": ops, "meta": meta})
        == "Pd(111) + B (subsurface, layer 3)"
    )


def test_by_label_substitution_reads_the_slab_layer() -> None:
    """``set_element`` by atom label: the slab label arithmetic layers
    aPd30 as surface (28–36 of a [3,3,4] mint z-ascending) and aPd12
    (10–18) as subsurface, layer 3."""
    ops = [
        _SLAB,
        {"op": "set_element", "atom": "aPd30", "element": "Cu"},
        {"op": "set_element", "atom": "aPd12", "element": "Ag"},
    ]
    assert (
        catalyst_label(ops)
        == "Pd(111) + Cu (surface substitution) + Ag (subsurface, layer 3)"
    )


def test_adsorbates_render_as_coads() -> None:
    ops = [
        _SLAB,
        {"op": "add_adsorbate", "species": "O", "site": {"type": "bridge"}},
        {"op": "add_atom_site", "element": "H", "site": {"type": "top"}},
        {"op": "add_atom_site", "element": "H", "site": {"type": "top"}},
    ]
    assert catalyst_label(ops) == "Pd(111) + O (bridge) + 2 H (top)"


def test_retransmuted_label_counts_once() -> None:
    """Labels are stable: 'aPd35 to Ag, then to Cu' is one Cu dopant, and
    back to the host is none — the same net-effect rule
    ``params_from_spec`` applies."""
    ops = [
        _SLAB,
        {"op": "set_element", "atom": "aPd35", "element": "Ag"},
        {"op": "set_element", "atom": "aPd35", "element": "Cu"},
    ]
    assert catalyst_label(ops) == "Pd(111) + Cu (surface substitution)"
    back = [
        _SLAB,
        {"op": "set_element", "atom": "aPd35", "element": "Ag"},
        {"op": "set_element", "atom": "aPd35", "element": "Pd"},
    ]
    assert catalyst_label(back) == "Pd(111), clean"


def test_derived_recipe_delta_still_names_host() -> None:
    """A derive's recorded ops carry only the delta — joined onto the
    parent's recipe by ``recipe_ops`` before the label runs, they render
    against the parent's slab."""
    ops = [
        _SLAB,
        {"op": "set_element", "element": "Ta", "site": {"type": "subsurface"}},
        {"op": "vacancy", "atom": "aPd35"},
    ]
    assert catalyst_label(ops) == "Pd(111) + Ta (subsurface) + vacancy (surface)"


def test_meta_params_fallback() -> None:
    """No recorded ops: the ``meta.params`` stamp (params_from_spec) and
    ``composition`` still render a coarser label — no facet the params
    never recorded."""
    meta = {
        "composition": {"Pd": 35, "Cu": 1},
        "params": {"dopant": "Cu", "n_dopant": 1, "site": "subst"},
    }
    assert catalyst_label(meta) == "Pd + Cu (substitution)"
    meta2 = {
        "composition": {"Pd": 34, "Cu": 2, "H": 2},
        "params": {
            "dopant": "Cu",
            "n_dopant": 2,
            "site": "subst",
            "coads": {"H": 2},
            "coads_site": {"H": "top"},
        },
    }
    assert catalyst_label(meta2) == "Pd + 2 Cu (substitution) + 2 H (top)"


def test_missing_construction_returns_none() -> None:
    """Nothing recorded — no ops, no params, no composition: the caller
    falls back to the title-derived name."""
    assert catalyst_label(None) is None
    assert catalyst_label({}) is None
    assert catalyst_label({"title": "Pd111-Ta-subsurf-1"}) is None
    assert catalyst_label([]) is None


def test_generated_spec_ops() -> None:
    """A spec generator's ``meta.generated.spec`` is construction too."""
    meta = {
        "generated": {
            "generator": "test",
            "spec": {
                "ops": [
                    _SLAB,
                    {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
                ]
            },
        }
    }
    assert catalyst_label(meta) == "Pd(111) + Cu (surface substitution)"


def test_ops_and_meta_bundle() -> None:
    """The ``{"ops": …, "meta": …}`` call form — recipe wins; meta backs
    the host when the recipe carries none."""
    bundle = {
        "ops": [_SLAB, {"op": "vacancy", "atom": "aPd35"}],
        "meta": {"composition": {"Pd": 35}},
    }
    assert catalyst_label(bundle) == "Pd(111) + vacancy (surface)"
    hostless = {
        "ops": [{"op": "set_element", "element": "Cu", "site": {"type": "top"}}],
        "meta": {"composition": {"Pd": 35, "Cu": 1}},
    }
    assert catalyst_label(hostless) == "Pd + Cu (surface substitution)"
