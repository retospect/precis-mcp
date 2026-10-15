"""USB-C PD Nano test board — the baseline measurement the anneal item
(``docs/backlog/pcb-anneal-closes-router-loop.md``, acceptance board per
``docs/backlog/pcb-usb-c-pd-nano-testboard.md``) measures against.

The fixture (``tests/fixtures/pcb/nano_usb_pd_testboard.py``) is authored
with the doc's corrections applied — fixed PDO set 5/9/15/20 V, one
20 V contract with 15/9/5 V bucks, provisional high-side driver + N-FET
channels, per-net ``current``/``working_voltage_v`` so widths and
pairwise clearances resolve per net.

**This test PINS the honest current state — it does not assert clean.**
``_BASELINE_*`` below are the measured counts of the first seeded run,
recorded so the anneal work can show improvement off a fixed reference
(the two-tier discipline of ``test_pcb_reference_end_to_end.py``: errors
are below-fab-floor violations, warnings are class/house shortfalls —
both are pinned, and the per-rule breakdowns are kept as evidence so a
regression names the rule, not just the count). Neither number is a
verdict that the board is manufacturable; synthesized bounds and
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

#: Measured 2026-10-13 on this tree at ``SEED`` — the fixture's honest
#: first-run state, pinned so the anneal item measures deltas against it.
#: Keys are DRC rule names; values are finding counts at that severity.
#:
#: The mix is the honest part: ``unrouted`` is the 26 nets the maze
#: could not realize on the two routable layers (F.Cu/B.Cu — the default
#: stackup pours In1/In2); ``connectivity`` is the 10 of them left with
#: partial copper (gr488909: the 16 with none are the ``unrouted``
#: question only, not a second error per net), ``silk_missing`` is
#: 35 refdes labels with no legal spot, and the warn tier is real margin
#: spend (clearance 0.100 vs required 0.150 mm, board-edge). Zero errors
#: alone would NOT satisfy the authored requirements here (doc Blockers
#: §3 residue: the shared router clearance is one value, so per-net
#: shortfalls land as warnings) — the pin is the whole tuple.
BASELINE_DRC_ERRORS_BY_RULE: dict[str, int] = {
    "connectivity": 10,
    "unrouted": 26,
    "outline_containment": 1,
    "silk_edge_clearance": 1,
    "silk_missing": 35,
}
BASELINE_DRC_WARNS_BY_RULE: dict[str, int] = {
    "clearance": 10,
    "board_edge_clearance": 2,
    "silk_edge_clearance": 3,
}
BASELINE_DRC_ERRORS = sum(BASELINE_DRC_ERRORS_BY_RULE.values())
BASELINE_DRC_WARNS = sum(BASELINE_DRC_WARNS_BY_RULE.values())

#: Route status over nets with fanout >= 2 (the honest denominator —
#: single-member nets trivially "route" and are never the question).
#: 38 of 64 nets realized; the 26 failures are congestion-dominated
#: (the route job's failure reasons: congestion, same-layer-crossing,
#: width) — the residue the anneal feedback loop exists to close.
BASELINE_ROUTED = 38
BASELINE_FAILED = 26

_DRC_HEAD_RE = re.compile(r"— (\d+) error\(s\), (\d+) warn\(s\)")


def _fanout(connections: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for conn in connections:
        counts[conn["net"]] = counts.get(conn["net"], 0) + 1
    return counts


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


def _drain_one_job(store: Store, parent_id: int, *, unrouted_ok: bool) -> None:
    """Drain the queued job most recently enqueued under ``parent_id`` —
    the same discipline ``test_pcb_second_reference_end_to_end.py`` uses:
    not "whatever's next in the queue" (a shared per-worker test DB can
    carry an orphan). ``unrouted_ok`` accepts exactly the
    ``non-convergence`` failure class a route job ends with when nets are
    left unrealized — this test MEASURES that partial result."""
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
            if unrouted_ok and status == "failed":
                with store.pool.connection() as conn:
                    meta_row = conn.execute(
                        "SELECT meta->>'failure_class', meta->>'error' "
                        "FROM refs WHERE ref_id = %s",
                        (job_ref_id,),
                    ).fetchone()
                assert meta_row is not None
                assert meta_row[0] == "non-convergence", (
                    f"job {job_ref_id} failed for another reason: "
                    f"{meta_row[0]!r} error={meta_row[1]!r}"
                )
                return
            assert status == "succeeded", (
                f"job {job_ref_id} failed to drain cleanly: status={status!r}"
            )
            return
        result = run_job_inproc_pass(store, limit=1)
        assert result["claimed"] == 1, f"expected a queued job, got {result}"
    raise AssertionError(f"job {job_ref_id} never reached a terminal status")


@pytest.mark.slow
def test_nano_board_place_route_drc_baseline(store: Store) -> None:
    # The catalog footprints are the fixture's cache — seeded into the
    # test DB before the put so the catalog parts resolve to real pad
    # geometry. Read-only lookups at apply; no JLC/LCSC calls.
    for lcsc, fp in fx.CATALOG_FOOTPRINTS.items():
        store.part_footprint_put(lcsc, fp)

    pcb = PcbHandler(hub=Hub(store=store))
    created = pcb.put(id=SLUG, args=dict(fx.DESIGN))
    assert "created" in created.body
    ref = store.get_ref(kind="pcb", id=SLUG)
    assert ref is not None

    place_resp = pcb.put(id=SLUG, args={"op": "place", "seed": SEED})
    assert "enqueued" in place_resp.body
    _drain_one_job(store, ref.id, unrouted_ok=False)

    route_resp = pcb.put(id=SLUG, args={"op": "route", "seed": SEED})
    assert "enqueued" in route_resp.body
    _drain_one_job(store, ref.id, unrouted_ok=True)

    # --- route status, honestly denominated --------------------------------
    fanout = _fanout(fx.DESIGN["connections"])
    fanout2_nets = {name for name, n in fanout.items() if n >= 2}
    status_rows = store.pcb_route_status(ref.id)
    routed = sum(
        1
        for row in status_rows
        if row["name"] in fanout2_nets and row["status"] == "realized"
    )
    failed = sum(
        1
        for row in status_rows
        if row["name"] in fanout2_nets and row["status"] == "failed"
    )

    # --- DRC, both tiers ----------------------------------------------------
    drc = pcb.get(id=SLUG, view="drc")
    match = _DRC_HEAD_RE.search(drc.body)
    assert match is not None, f"DRC view head didn't match: {drc.body!r}"
    n_errors, n_warns = int(match.group(1)), int(match.group(2))

    _run_id, findings = store.pcb_drc_findings_latest(ref.id)
    errors_by_rule = collections.Counter(
        f["rule"] for f in findings if f["severity"] == "error"
    )
    warns_by_rule = collections.Counter(
        f["rule"] for f in findings if f["severity"] != "error"
    )

    # Per-rule evidence kept: the print shows exactly what the baseline is
    # made of, so an anneal change can name what moved.
    print("\n\n# nano_usb_pd_testboard — baseline\n")
    print(
        f"drc head: {n_errors} error(s), {n_warns} warn(s); "
        f"routed {routed}/{len(fanout2_nets)}, failed {failed}"
    )
    print(f"errors by rule: {dict(errors_by_rule)}")
    print(f"warns by rule: {dict(warns_by_rule)}")
    for f in findings:
        print(
            f"  {f['severity']:5s} {f['rule']:28s} {f.get('where') or ''} "
            f"{str(f.get('detail'))[:120]}"
        )

    assert routed == BASELINE_ROUTED
    assert failed == BASELINE_FAILED
    assert dict(errors_by_rule) == BASELINE_DRC_ERRORS_BY_RULE
    assert dict(warns_by_rule) == BASELINE_DRC_WARNS_BY_RULE
    assert n_errors == BASELINE_DRC_ERRORS
    assert n_warns == BASELINE_DRC_WARNS
