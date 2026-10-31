"""USB-C PD Nano test board — the baseline measurement the anneal item
(``docs/backlog/pcb-anneal-closes-router-loop.md``, acceptance board per
``docs/backlog/pcb-usb-c-pd-nano-testboard.md``) measures against.

The fixture (``tests/fixtures/pcb/nano_usb_pd_testboard.py``) is authored
with the doc's corrections applied — fixed PDO set 5/9/15/20 V, one
20 V contract with 15/9/5 V bucks, provisional high-side driver + N-FET
channels, per-net ``current``/``working_voltage_v`` so widths and
pairwise clearances resolve per net.

**This test MEASURES; it no longer gates on counts.** Reto 2026-10-19:
the grid router's per-seed floors are dropped ("Drop the grid router's
floors? yes"). Vector routing is retired (gr515107, Reto 2026-10-25:
vroute deleted, tag ``vroute-final``; topo is the only router), so what
remains is the place + stored-DRC half of the old baseline — the runs
still have to drain cleanly and the per-rule DRC breakdown is printed as
evidence so a regression stays visible. Synthesized bounds and
provisional topology are documented in the fixture.
"""

from __future__ import annotations

import collections
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.executors._common import TERMINAL, current_status
from precis.workers.executors.job_inproc import run_job_inproc_pass
from tests.fixtures.pcb import nano_usb_pd_testboard as fx

SLUG = "nano-usb-pd-test"
SEED = 1

#: Seeds the cross-placement sweep runs (gr489909); each seed's place
#: must drain cleanly.
NANO_SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8)

_DRC_HEAD_RE = re.compile(r"— (\d+) error\(s\), (\d+) warn\(s\)")


def _pin_names_for(comp: dict[str, Any], design: dict[str, Any]) -> set[str]:
    """The pin names a connection may legally name for one component:
    authored ``pins`` plus every pin the component's footprint declares
    (declare-all completes them)."""
    names = {str(p["name"]) for p in comp.get("pins") or []}
    part = comp.get("part") or comp.get("part_lcsc")
    local = comp.get("footprint")
    if part:
        fp = fx.CATALOG_FOOTPRINTS.get(str(part).strip().upper())
        if fp is not None:
            names |= {str(e["name"]) for e in fp["pin_map"].values()}
    if local:
        fp = next((f for f in design["footprints"] if f["name"] == local), None)
        if fp is not None:
            names |= {str(p["pin"]) for p in fp["pads"]}
    return names


# ── structural pins (DB-free) ────────────────────────────────────────────────


def test_every_connection_resolves_against_declared_or_footprint_pins() -> None:
    """There is no ERC on an L0-authored netlist — this check is the
    netlist review the doc asks a human to do, mechanised: a wrong pin or
    net name here is a wrong board."""
    design = fx.DESIGN
    pins_by_refdes = {
        c["refdes"]: _pin_names_for(c, design) for c in design["components"]
    }
    net_names = {n["name"] for n in design["nets"]}
    problems = []
    for conn in design["connections"]:
        refdes, pin, net = conn["refdes"], conn["pin"], conn["net"]
        if refdes not in pins_by_refdes:
            problems.append(f"unknown refdes {refdes!r}")
        elif pin not in pins_by_refdes[refdes]:
            problems.append(f"{refdes} has no pin {pin!r}")
        if net not in net_names:
            problems.append(f"unknown net {net!r}")
    assert not problems, f"{len(problems)} bad connection(s): {problems[:10]}"


def test_doc_corrections_are_applied() -> None:
    """The three doc corrections pinned structurally: fixed PDO set
    (5/9/15/20 — no 12 V net), one contract (VBUS), derived rails."""
    design = fx.DESIGN
    net_names = {n["name"] for n in design["nets"]}
    for rail in ("VBUS", "V15", "V9", "V5"):
        assert rail in net_names
    assert "V12" not in net_names and not any("12V" in n for n in net_names)
    by_name = {n["name"]: n for n in design["nets"]}
    assert by_name["VBUS"]["working_voltage_v"] == 20.0
    assert by_name["V15"]["working_voltage_v"] == 15.0
    assert by_name["V9"]["working_voltage_v"] == 9.0
    assert by_name["V5"]["working_voltage_v"] == 5.0


def test_rail_nets_carry_per_net_rules() -> None:
    """current + working_voltage_v on every power net, so width and the
    pairwise voltage clearance resolve per net (doc Blockers §2/§3:
    authored, not assumed)."""
    by_name = {n["name"]: n for n in fx.DESIGN["nets"]}
    for rail in ("VBUS", "GND", "V15", "V9", "V5", "OUT1", "OUT2", "OUT3", "OUT4"):
        n = by_name[rail]
        assert n.get("current"), f"{rail}: no current= annotation"
        assert n.get("working_voltage_v") is not None
    # Every net carries a working voltage — the pairwise rule's input.
    unannotated = [
        n["name"] for n in fx.DESIGN["nets"] if n.get("working_voltage_v") is None
    ]
    assert not unannotated, f"nets with no working_voltage_v: {unannotated}"


def test_catalog_parts_have_cached_footprints() -> None:
    """Every component naming a ``part`` C-number must have a
    ``CATALOG_FOOTPRINTS`` row — otherwise its pads are synthesized bounds
    and the ``synthesized_footprint`` DRC rule flags the part (error)."""
    catalog = {(c.get("part") or c.get("part_lcsc")) for c in fx.DESIGN["components"]}
    catalog = {str(p).strip().upper() for p in catalog if p}
    assert catalog <= set(fx.CATALOG_FOOTPRINTS), (
        f"uncached catalog part(s): {sorted(catalog - set(fx.CATALOG_FOOTPRINTS))}"
    )


def test_provisional_high_side_is_marked() -> None:
    """The driver topology is named provisional in meta and the GATE net
    notes carry the netlist-review TODO — auditable, not silent."""
    meta = fx.DESIGN["meta"]
    assert meta["provisional"]["status"].startswith("PROVISIONAL")
    by_name = {n["name"]: n for n in fx.DESIGN["nets"]}
    for i in range(1, 5):
        assert "TODO(netlist-review)" in by_name[f"GATE{i}"]["note"]


def test_outline_is_generous_and_recorded() -> None:
    """100 x 80 mm, not 18 x 45 — the doc's own ask is that the outline is
    sized for real power-net widths/clearances and the choice recorded."""
    outline = next(f for f in fx.DESIGN["features"] if f["ftype"] == "outline")
    xs = [p[0] for p in outline["geom"]["path"]]
    ys = [p[1] for p in outline["geom"]["path"]]
    assert (max(xs) - min(xs), max(ys) - min(ys)) == (100.0, 80.0)
    assert fx.DESIGN["meta"]["outline_mm"] == [100.0, 80.0]


def test_nano_headers_are_one_rigid_group_at_15_24mm() -> None:
    """Nano compatibility is the headers, not the outline — pinned as a
    rigid group holding the real 15.24 mm (0.6 in) row spacing."""
    by_refdes = {c["refdes"]: c for c in fx.DESIGN["components"]}
    j1, j2 = by_refdes["J_N1"], by_refdes["J_N2"]
    assert j1["group"] == j2["group"] == "nano_hdrs"
    assert j2["group_offset"]["x"] == pytest.approx(15.24)


# ── the baseline run (slow) ───────────────────────────────────────────────────


def _drain_one_job(store: Store, parent_id: int) -> None:
    """Drain the queued job most recently enqueued under ``parent_id`` —
    the same discipline ``test_pcb_second_reference_end_to_end.py`` uses:
    not "whatever's next in the queue" (a shared per-worker test DB can
    carry an orphan)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (parent_id,),
        ).fetchone()
    assert row is not None, f"no job was ever queued for parent_id={parent_id}"
    job_ref_id = row[0]

    for _ in range(25):
        with store.pool.connection() as conn:
            status = current_status(conn, job_ref_id)
        if status in TERMINAL:
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


def _place(store: Store, seed: int, slug: str = SLUG) -> tuple[PcbHandler, int]:
    """Create the board as ``slug`` and place it at ``seed``; return the
    handler and the board's ref id. (Was ``_place_and_route`` — routing
    is retired, gr515107.)"""
    # The catalog footprints are the fixture's cache — seeded into the
    # test DB before the put so the catalog parts resolve to real pad
    # geometry. Read-only lookups at apply; no JLC/LCSC calls.
    for lcsc, fp in fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)

    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id=slug, args=dict(fx.DESIGN))
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None

    place_resp = pcb.put(id=slug, args={"op": "place", "seed": seed})
    assert "enqueued" in place_resp.body
    _drain_one_job(store, ref.id)
    return pcb, ref.id


def _seeded_nano_ir(store: Store, seed: int):
    """The nano design's IR right after :func:`seed_placement` — the seed
    alone, before anneal or polish. Mirrors pcb_place's hydration."""
    import random

    from precis.pcb import session as pcb_session
    from precis.pcb.optimize import seed_placement

    for lcsc, fp in fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)
    pcb = PcbHandler(hub=Hub(store=store))
    slug = f"{SLUG}-seed-s{seed}"
    assert "created" in pcb.put(id=slug, args=dict(fx.DESIGN)).body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    graph = store.pcb_graph(ref.id)
    features = store.pcb_features_list(ref.id)
    ir = pcb_session.build_ir(
        graph,
        outline=pcb_session.outline_from_features(features),
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        footprints_by_lcsc=store.pcb_footprints_for(ref.id),
        local_footprints_by_name=store.pcb_local_footprints_for(ref.id),
    )
    seed_placement(ir, random.Random(seed))
    return ir


@pytest.mark.slow
@pytest.mark.parametrize("seed", [1, 2])
def test_nano_seed_covers_the_board(store: Store, seed: int) -> None:
    """gr503556: the seed must spread parts over the outline, not pile
    them on the bottom edge. Measured as the share of total courtyard
    area whose centre sits in each of five equal horizontal bands of the
    usable outline (79 mm → ~16 mm bands): a band is ``>35 %`` only when
    it hoards more than 1.75x its even share — the old pack put ~85 % of
    the parts in one band, so this bounds the failure it caused.
    The rigid header pair seeds in its own region: its members' centres
    must land in the far-edge strip (x beyond the parts' region)."""
    import numpy as np

    from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM, outline_bbox
    from precis.pcb.ir import (
        COURTYARD_CLEARANCE_MM,
        courtyard_bound_radius_mm,
        instance_courtyard_polygons,
    )

    ir = _seeded_nano_ir(store, seed)
    xs, ys = np.asarray(ir.inst_x), np.asarray(ir.inst_y)
    assert np.isfinite(xs).all() and np.isfinite(ys).all()
    ox0, oy0, ox1, oy1 = outline_bbox(ir.outline)
    areas = (
        np.pi
        * courtyard_bound_radius_mm(
            instance_courtyard_polygons(
                ir,
                clearance_mm=COURTYARD_CLEARANCE_MM,
                fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
            )
        )
        ** 2
    )
    band_h = (oy1 - oy0) / 5.0
    shares = (
        np.array(
            [areas[((ys - oy0) // band_h).clip(0, 4) == b].sum() for b in range(5)]
        )
        / areas.sum()
    )
    print(f"\nseed {seed} band area shares: {[round(float(s), 3) for s in shares]}")
    assert shares.max() < 0.35, f"band hoards {shares.max():.0%} of part area"
    # The authored rigid header group (J_N1/J_N2) packs in its own
    # far-edge region, not interleaved on a shared shelf.
    refs = [str(r) for r in ir.instance_refdes]
    jx = [xs[refs.index(r)] for r in ("J_N1", "J_N2")]
    assert min(jx) > (ox0 + ox1) / 2.0


@pytest.mark.nightly(
    reason="rt-s3 ruling (Reto 2026-10-24): the 8-seed place sweep can't "
    "shrink under the gate's 240 s bar — nightly only"
)
@pytest.mark.slow
# CI worst 463s x2.5 for runner variance (gr503084)
@pytest.mark.timeout(1200)
@pytest.mark.parametrize("seed", NANO_SEEDS)
def test_nano_board_places_cleanly_per_seed(store: Store, seed: int) -> None:
    """Every seed's place job must drain cleanly (``_place`` asserts
    that). Was the routed-count sweep — vector routing is retired
    (gr515107), so
    the placement drain is what remains. One test per seed, not one
    8-seed loop: the loop took 20-30 min on a CI runner and, pinned to a
    single shard, sat at the 40 min job cap (gr495187); per-seed ids
    spread over the shards."""
    _place(store, seed, slug=f"{SLUG}-s{seed}")


@pytest.mark.nightly(
    reason="rt-s3 ruling (Reto 2026-10-24): the place/DRC baseline can't "
    "split under the gate's 240 s bar — nightly only"
)
@pytest.mark.slow
@pytest.mark.timeout(1380)  # CI worst 550s x2.5 for runner variance (gr503084)
def test_nano_board_place_drc_baseline(store: Store) -> None:
    pcb, ref_id = _place(store, SEED)

    # --- DRC ------------------------------------------------------------
    # Record first, then read the view: view='drc' reuses a recorded run
    # whose design-revision stamp matches (gr496242), so the board pays
    # ONE DRC pass — the old order ran it twice (the view is read-only).
    pcb._drc_run(ref_id, store.pcb_load(ref_id), record=True)
    drc = pcb.get(id=SLUG, view="drc")
    match = _DRC_HEAD_RE.search(drc.body)
    assert match is not None, f"DRC view head didn't match: {drc.body!r}"
    n_errors, n_warns = int(match.group(1)), int(match.group(2))

    _run_id, findings = store.pcb_drc_findings_latest(ref_id)
    errors_by_rule = collections.Counter(
        f["rule"] for f in findings if f["severity"] == "error"
    )
    warns_by_rule = collections.Counter(
        f["rule"] for f in findings if f["severity"] != "error"
    )

    # Per-rule evidence kept: the print shows exactly what the baseline is
    # made of, so an anneal change can name what moved. With routing
    # retired every fanout>=2 net is a standing `unrouted` finding —
    # expected, not a defect; the copper/pad rules are the signal.
    print("\n\n# nano_usb_pd_testboard — baseline\n")
    print(f"drc head: {n_errors} error(s), {n_warns} warn(s)")
    print(f"errors by rule: {dict(errors_by_rule)}")
    print(f"warns by rule: {dict(warns_by_rule)}")
    for f in findings:
        print(
            f"  {f['severity']:5s} {f['rule']:28s} {f.get('where') or ''} "
            f"{str(f.get('detail'))[:120]}"
        )
    copper_errors = {
        rule: n
        for rule, n in errors_by_rule.items()
        if rule != "unrouted" and not rule.startswith("silk")
    }
    assert not copper_errors, f"non-unrouted DRC errors: {copper_errors}"
    # Silk carries its own ratchet (the esp32c3 discipline): pads-only DRC
    # drops 5 refdes labels on this dense board — a downward-only bound,
    # not forgiveness.
    silk_errors = sum(
        n for rule, n in errors_by_rule.items() if rule.startswith("silk")
    )
    assert silk_errors <= 5, (
        f"{silk_errors} silk DRC errors, above the 5 baseline: "
        f"{dict(errors_by_rule)} -- this ratchet only goes down"
    )
