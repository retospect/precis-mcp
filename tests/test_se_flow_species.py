"""precis_se species adapter tests — docs/backlog/se-multiphysics.md
slice 1's se surface: ``fluid=``/``species=`` token keys, patch-measure
BCs, the four species COMPUTERS, and the ``wall_rate`` catpath binding.

Adapter cases run the c-box chamber (``box:w0.1d0.03h0.001``, L = 0.1 m,
h0 = 1 mm, ε = 0.01); the bare-tree path keeps measures as plain
``MeasureSpec`` rows (BCs are read by name/value/datum; units ride the
measurand when the store backs the tree, which only the ``wall_rate``
tests need).
"""

from __future__ import annotations

import json
import math

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis_se import persist, wall_rate
from precis_se.drc import drc
from precis_se.flow import FLOW_COMPUTERS, FlowSpec, parse_flow_shape
from precis_se.handler import SeHandler
from precis_se.measures import MeasureSpec
from precis_se.ops import SeTree, apply_ops
from tests.test_se_flow import _box_tree

IN_SEL = "patch:c.side3@0,0+0.006x0.001"
OUT_SEL = "patch:c.side1@0,0+0.006x0.001"
FLOOR_SEL = "patch:c.bottom@0,0+0.098x0.028"
LID_SEL = "patch:c.top@0,0+0.098x0.028"
SHORT_SEL = "patch:c.bottom@0,0+0.001x0.028"

# Operating point: u0 = 1e-5 m/s, D = 1e-9 m²/s on the box → Pe = 10,
# Pe·ε = 0.1 (the Eulerian bound, inclusive). k_s = 5e-8 m/s on both
# walls → reaction-limited, Da_I ≈ 1, conversion ≈ 0.63.


def _species_tree(
    *,
    species: str = "co",
    fluid: str | None = None,
    wall_sels: tuple[str, ...] = (FLOOR_SEL, LID_SEL),
    regions_extra: tuple[str, ...] = (),
) -> SeTree:
    tree = _box_tree()
    shape = f"flow:in={IN_SEL};out={OUT_SEL};dv=6"
    if fluid is not None:
        shape += f";fluid={fluid}"
    shape += f";species={species}"
    regions = [{"selector": s} for s in (IN_SEL, OUT_SEL, *wall_sels, *regions_extra)]
    apply_ops(
        tree,
        [
            {
                "op": "add_pocket",
                "block": "c",
                "name": "flow",
                "shape": shape,
                "regions": regions,
            }
        ],
    )
    return tree


def _bc(tree: SeTree, name: str, value: float, datum: str) -> None:
    tree.measures.append(
        MeasureSpec(block="c", name=name, value=value, datum=datum, unit="ratio")
    )


def _full_bc(
    tree: SeTree,
    *,
    k_s: float = 5e-8,
    sels: tuple[str, ...] = (FLOOR_SEL, LID_SEL),
) -> None:
    _bc(tree, "co/species-diffusivity", 1e-9, FLOOR_SEL)
    _bc(tree, "co/inlet-velocity", 1e-5, IN_SEL)
    for sel in sels:
        _bc(tree, "co/wall-rate-constant", k_s, sel)


def _measure(name: str, datum: str, measurand: str) -> MeasureSpec:
    return MeasureSpec(block="c", name=name, datum=datum, measurand=measurand)


# ── token keys ────────────────────────────────────────────────────────


def test_parse_flow_shape_fluid_and_species() -> None:
    spec = parse_flow_shape("flow:in=p;out=q;dv=6;fluid=water;species=co,h2")
    assert isinstance(spec, FlowSpec)
    assert spec.fluid == "water" and spec.species == ("co", "h2")
    bare = parse_flow_shape("flow:in=p;out=q;dv=6")
    assert isinstance(bare, FlowSpec)
    assert bare.fluid is None and bare.species == ()
    with pytest.raises(ValueError, match="unknown key"):
        parse_flow_shape("flow:in=p;out=q;dv=6;temperature=300")


# ── the species computers ─────────────────────────────────────────────


def test_outlet_conversion_and_hard_drc_gate() -> None:
    """Acceptance 11: a hard outlet-conversion ≥ 0.9 on a chamber that
    reaches ≈ 0.63 produces a measure_unmet finding."""
    tree = _species_tree()
    _full_bc(tree)
    comp = FLOW_COMPUTERS["outlet-conversion"]
    v = comp(tree, _measure("conv", OUT_SEL, "outlet-conversion"))
    assert v is not None and 0.4 < v < 0.8
    tree.measures.append(
        MeasureSpec(
            block="c",
            name="conv_req",
            measurand="outlet-conversion",
            min_value=0.9,
            strength="hard",
            datum=OUT_SEL,
        )
    )
    findings = [
        f
        for f in drc(tree).findings
        if f.rule == "measure_unmet" and f.subject == "c.conv_req"
    ]
    assert findings, "expected a measure_unmet finding on c.conv_req"


def test_missing_wall_rate_is_a_reason_not_zero() -> None:
    """Acceptance 11: no wall-rate-constant measure → a reason string,
    never a silent number."""
    tree = _species_tree()
    _bc(tree, "co/species-diffusivity", 1e-9, FLOOR_SEL)
    _bc(tree, "co/inlet-velocity", 1e-5, IN_SEL)
    wff = FLOW_COMPUTERS["wall-flux-fraction"]
    assert wff(tree, _measure("wff", FLOOR_SEL, "wall-flux-fraction")) is None
    assert "reactive" in (wff.reason or "")
    da = FLOW_COMPUTERS["damkohler"]
    assert da(tree, _measure("da", OUT_SEL, "damkohler")) is None
    assert "wall-rate-constant" in (da.reason or "")


def test_species_computers_registered() -> None:
    from precis_se.properties import COMPUTERS, is_checked

    for slug in (
        "outlet-conversion",
        "outlet-concentration",
        "wall-flux-fraction",
        "damkohler",
    ):
        assert slug in COMPUTERS and is_checked(slug)


def test_short_reactive_patch_entrance_corrected_no_flag() -> None:
    """Slice 1b: a short reactive patch (< 0.2·Pe·h0, k_s/k_m ≥ 0.1) is
    now entrance-corrected in the Eulerian solve (Lévêque k_m at the
    leading edge), so wall-flux-fraction answers a plain value with no
    entrance-region flag."""
    tree = _species_tree(wall_sels=(SHORT_SEL,))
    _bc(tree, "co/species-diffusivity", 1e-9, FLOOR_SEL)
    _bc(tree, "co/inlet-velocity", 1e-5, IN_SEL)
    # k_s = 1e-5 → k_s/k_m ≈ 4 ≥ 0.1; patch streamwise 1 mm < 2 mm bound.
    _bc(tree, "co/wall-rate-constant", 1e-5, SHORT_SEL)
    comp = FLOW_COMPUTERS["wall-flux-fraction"]
    v = comp(tree, _measure("wff", SHORT_SEL, "wall-flux-fraction"))
    assert v is not None and 0.0 < v <= 1.0
    assert comp.reason is None


def test_lagrangian_route_when_pe_eps_high() -> None:
    """Pe·ε > 0.1 is a route verdict, not a failure: the adapter answers
    through the Lagrangian tracer."""
    tree = _species_tree(wall_sels=(FLOOR_SEL,))
    _bc(tree, "co/species-diffusivity", 1e-9, FLOOR_SEL)
    _bc(tree, "co/inlet-velocity", 1e-4, IN_SEL)  # Pe = 100, Pe·ε = 1
    _bc(tree, "co/wall-rate-constant", 5e-8, FLOOR_SEL)
    comp = FLOW_COMPUTERS["outlet-conversion"]
    v = comp(tree, _measure("conv", OUT_SEL, "outlet-conversion"))
    assert v is not None and 0.0 <= v <= 1.0


def test_species_scoping_and_reasons() -> None:
    tree = _species_tree(species="co,h2")
    _full_bc(tree)
    comp = FLOW_COMPUTERS["outlet-conversion"]
    # Two declared species, unscoped measure → a reason naming the fix.
    assert comp(tree, _measure("conv", OUT_SEL, "outlet-conversion")) is None
    assert "species" in (comp.reason or "")
    # Scoped to an undeclared species → refused with the declared names.
    m = _measure("bt/outlet-conversion", OUT_SEL, "outlet-conversion")
    assert comp(tree, m) is None
    assert "not declared" in (comp.reason or "")
    # Scoped correctly → a value.
    m2 = _measure("co/outlet-conversion", OUT_SEL, "outlet-conversion")
    v = comp(tree, m2)
    assert v is not None and 0.0 <= v <= 1.0


def test_fluid_material_supplies_diffusivity() -> None:
    """No species-diffusivity measure: the fluid material's diffusivity
    is the fallback; a bare resolver gives a reason, not a guess."""
    tree = _species_tree(fluid="water")
    tree.materials = lambda slug: {"diffusivity": 1e-9, "viscosity": 1e-3}
    _bc(tree, "co/inlet-velocity", 1e-5, IN_SEL)
    _bc(tree, "co/wall-rate-constant", 5e-8, FLOOR_SEL)
    comp = FLOW_COMPUTERS["outlet-conversion"]
    v = comp(tree, _measure("conv", OUT_SEL, "outlet-conversion"))
    assert v is not None and 0.0 < v < 1.0
    # No material resolver → reason.
    tree2 = _species_tree(fluid="water")
    _bc(tree2, "co/inlet-velocity", 1e-5, IN_SEL)
    assert comp(tree2, _measure("conv", OUT_SEL, "outlet-conversion")) is None
    assert "diffusivity" in (comp.reason or "")


def test_size_invariance_through_adapter() -> None:
    """The same chamber at 5× the size, with D scaled to keep (ε, Pe,
    Da_w), gives the same conversion — the reduced solve is identical."""
    tree = _species_tree(wall_sels=(FLOOR_SEL,))
    _bc(tree, "co/species-diffusivity", 1e-9, FLOOR_SEL)
    _bc(tree, "co/inlet-velocity", 1e-5, IN_SEL)
    _bc(tree, "co/wall-rate-constant", 5e-8, FLOOR_SEL)
    comp = FLOW_COMPUTERS["outlet-conversion"]
    v1 = comp(tree, _measure("conv", OUT_SEL, "outlet-conversion"))
    assert v1 is not None

    big = SeTree()
    apply_ops(
        big, [{"op": "add_block", "name": "c", "envelope": "box:w0.5d0.15h0.005"}]
    )
    in5 = "patch:c.side3@0,0+0.03x0.005"
    out5 = "patch:c.side1@0,0+0.03x0.005"
    floor5 = "patch:c.bottom@0,0+0.49x0.14"
    apply_ops(
        big,
        [
            {
                "op": "add_pocket",
                "block": "c",
                "name": "flow",
                "shape": f"flow:in={in5};out={out5};dv=6;species=co",
                "regions": [{"selector": s} for s in (in5, out5, floor5)],
            }
        ],
    )
    big.measures.append(
        MeasureSpec(block="c", name="d", value=5e-9, datum=floor5, unit="ratio")
    )
    big.measures[-1].name = "co/species-diffusivity"
    big.measures.append(
        MeasureSpec(
            block="c", name="co/inlet-velocity", value=1e-5, datum=in5, unit="ratio"
        )
    )
    big.measures.append(
        MeasureSpec(
            block="c",
            name="co/wall-rate-constant",
            value=5e-8,
            datum=floor5,
            unit="ratio",
        )
    )
    v2 = comp(big, _measure("conv", out5, "outlet-conversion"))
    assert v2 is not None
    assert v2 == pytest.approx(v1, rel=1e-9)


# ── the wall_rate catpath binding (test 12) ───────────────────────────


def _run_migration(name: str) -> None:
    import psycopg

    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


@pytest.fixture
def store(store):
    return store


@pytest.fixture
def taxonomy(store):
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    _run_migration("0231_se_species_measurands.sql")
    return store


def _se_tree(store, slug: str = "catdesign") -> SeTree:
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id=slug,
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "cat",
                        "envelope": "box:w0.01d0.01h0.001",
                    }
                ]
            }
        ),
    )
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(store, ref.id)


def _solve_meta(tof: float = 2.0, **kw) -> dict:
    meta = {
        "tof": tof,
        "log_tof": math.log10(tof),
        "kinetics": {
            "site_area_A2": 4.0,
            "T_K": 500.0,
            "p_bar": 0.5,
            "phase": "gas",
        },
    }
    meta.update(kw)
    return meta


def _quest_with_solve(store, meta: dict) -> int:
    q = store.insert_ref(kind="quest", slug=None, title="cat quest")
    s = store.insert_ref(kind="structure", slug="slab-1", title="slab", meta=meta)
    store.add_link(src_ref_id=s.id, dst_ref_id=q.id, relation="serves")
    return q.id


def _expected_ks(tof: float, p_bar: float = 0.5, t_k: float = 500.0) -> float:
    c_wall = p_bar * 1e5 / (wall_rate.R_GAS * t_k)
    return tof / (4.0e-20 * wall_rate.N_A * c_wall)


def test_wall_rate_binds_ks_with_provenance(store, taxonomy) -> None:
    qid = _quest_with_solve(
        store,
        _solve_meta(log_tof_p5=math.log10(1.0), log_tof_p95=math.log10(4.0)),
    )
    tree = _se_tree(store)
    echo = wall_rate.op_wall_rate(
        store,
        tree,
        {
            "op": "wall_rate",
            "block": "cat",
            "datum": "patch:cat.bottom@0,0+1e-3x1e-3",
            "source": f"quest:{qid}",
            "species": "co",
        },
    )
    assert "wall-rate-constant" in echo
    m = next(m for m in tree.measures if m.name == "co/wall-rate-constant")
    assert m.value == pytest.approx(_expected_ks(2.0), rel=1e-12)
    assert m.measurand == "wall-rate-constant" and m.origin == "proposed"
    # p5/p95 land as the conversion band on the measure.
    assert m.min_value == pytest.approx(_expected_ks(1.0), rel=1e-12)
    assert m.max_value == pytest.approx(_expected_ks(4.0), rel=1e-12)
    prov = m.provenance or {}
    assert "catpath" in str(prov.get("solver_id", ""))
    assert any("roughness" in a for a in prov.get("assumptions", []))


def test_wall_rate_refusals(store, taxonomy) -> None:
    tree = _se_tree(store)
    op = {
        "op": "wall_rate",
        "block": "cat",
        "datum": "patch:cat.bottom@0,0+1e-3x1e-3",
        "source": "quest:999999",
        "species": "co",
    }
    with pytest.raises(BadInput, match="no quest"):
        wall_rate.op_wall_rate(store, tree, op)
    # untrusted: no log_tof
    qid = _quest_with_solve(store, {"tof": 2.0, "kinetics": _solve_meta()["kinetics"]})
    with pytest.raises(BadInput, match="untrusted|log_tof"):
        wall_rate.op_wall_rate(store, tree, {**op, "source": f"quest:{qid}"})
    # tof ≤ 0
    qid2 = _quest_with_solve(
        store,
        {
            "tof": -1.0,
            "log_tof": 0.0,
            "kinetics": _solve_meta()["kinetics"],
        },
    )
    with pytest.raises(BadInput, match="≤ 0|tof"):
        wall_rate.op_wall_rate(store, tree, {**op, "source": f"quest:{qid2}"})
    # liquid phase refused (spec Q3)
    meta = _solve_meta()
    meta["kinetics"]["phase"] = "liquid"
    qid3 = _quest_with_solve(store, meta)
    with pytest.raises(BadInput, match="liquid"):
        wall_rate.op_wall_rate(store, tree, {**op, "source": f"quest:{qid3}"})


def test_wall_rate_apparent_order_half(store, taxonomy, monkeypatch) -> None:
    """TOF ∝ p^0.5 → α = 0.5 within 1e-6, and the card's apparent_order
    line fails with the X ≲ 0.2 wording."""
    qid = _quest_with_solve(store, _solve_meta())

    def fake_tof(record: dict, p_bar: float) -> float:
        return 2.0 * (p_bar / 0.5) ** 0.5

    monkeypatch.setattr(wall_rate, "_solve_tof", fake_tof)
    tree = _se_tree(store)
    wall_rate.op_wall_rate(
        store,
        tree,
        {
            "op": "wall_rate",
            "block": "cat",
            "datum": "patch:cat.bottom@0,0+1e-3x1e-3",
            "source": f"quest:{qid}",
            "species": "co",
        },
    )
    m = next(m for m in tree.measures if m.name == "co/wall-rate-constant")
    prov = m.provenance or {}
    alphas = [a for a in prov.get("assumptions", []) if a.startswith("alpha=")]
    assert alphas and "unmeasured" not in alphas[0]
    assert abs(float(alphas[0].split("=")[1]) - 0.5) <= 1e-6

    from precis_flow import RegimeCard

    assert m.value is not None
    card = RegimeCard.from_physical(
        L=0.02,
        h0=1e-4,
        mu=1e-3,
        rho=1000.0,
        u0=1e-4,
        molecular_d=1e-9,
        k_s=float(m.value),
        apparent_order=0.5,
    )
    line = {ln.name: ln for ln in card.species}["apparent_order"]
    assert not line.ok and "0.2" in line.otherwise
