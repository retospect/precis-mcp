"""Slice 8 of docs/backlog/pcb-lazy-netlist-and-checks.md — one check
surface, an authored phase, finding delta.

``view='check'`` is THE surface (§1g): every rule — geometric, netlist,
later ERC — reports one finding list, filterable on ``rule`` /
``severity`` / ``phase``. ``view='drc'`` stays as its preset for
continuity (its own accept bullet: byte-identical output for an
unchanged fixture). ``op='phase'`` writes the authored design phase to
``pcb_boards.meta['phase']`` — the only way ``fab`` is ever judged at —
and :func:`precis.pcb.drc.derive_phase` stays the default otherwise.

Covered here, per the slice's accept list:

* ``view='drc'`` output is byte-identical to before for a fixture with
  no authored phase (the golden string below was captured on the
  unchanged surface).
* ``args={'phase':'netlist'}`` runs only the netlist rules and never
  touches ``pcb_copper`` (the store method is patched to raise).
* the same ``unrouted`` finding reads ``todo`` at ``placed`` and
  ``error`` at ``fab`` — severity is a function of (rule, phase).
* running an unchanged design twice reports ``0 new, N still,
  0 resolved``.
* ``op='phase'`` sets (and clears) the authored phase the surface reads.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler

from .test_pcb_declared_pins import _mini_design

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


# ── the surface ────────────────────────────────────────────────────────


def test_check_phase_netlist_runs_netlist_rules_without_copper(
    pcb: PcbHandler, store: Any
) -> None:
    """``args={'phase':'netlist'}`` runs ONLY the rules needing no
    geometry — and the copper table is never even opened."""
    pcb.put(id="chk-netlist", args=_mini_design())
    seen: list[int] = []

    def _no_copper(board_id: int) -> list[dict[str, Any]]:
        seen.append(board_id)
        raise AssertionError("pcb_copper_list touched at phase=netlist")

    store.pcb_copper_list = _no_copper
    out = pcb.get(id="chk-netlist", view="check", args={"phase": "netlist"})
    assert "phase netlist" in out.body
    assert not seen
    # No geometric rule may appear at netlist phase; the netlist rules do.
    assert "clearance" not in out.body
    assert "connectivity" not in out.body
    assert "unconnected_pin" in out.body


def test_check_same_finding_todo_at_placed_error_at_fab(pcb: PcbHandler) -> None:
    """The severity table per (rule, phase): ``unrouted`` is ``todo``
    below ``routed``, ``error`` at ``fab`` — one finding, two reads."""
    pcb.put(id="chk-phase", args=_mini_design())
    placed = pcb.get(id="chk-phase", view="check", args={"phase": "placed"}).body
    fab = pcb.get(id="chk-phase", view="check", args={"phase": "fab"}).body
    # Unrouted rows read todo at placed…
    unrouted_placed = [
        line for line in placed.splitlines() if "unrouted" in line and "todo" in line
    ]
    assert unrouted_placed, placed
    # …and the same nets report error at fab.
    unrouted_fab = [
        line for line in fab.splitlines() if "unrouted" in line and "error" in line
    ]
    assert unrouted_fab
    assert "unrouted" in placed and "unrouted" in fab


def test_check_delta_second_run_reports_all_still(pcb: PcbHandler) -> None:
    """An unchanged design run twice: ``0 new, N still, 0 resolved``."""
    pcb.put(id="chk-delta", args=_mini_design())
    first = pcb.get(id="chk-delta", view="check").body
    assert "none to compare against" in first
    second = pcb.get(id="chk-delta", view="check").body
    m = re.search(
        r"vs run \w+: (\d+) new, (\d+) still(?: \(\d+ worse\))?, (\d+) resolved",
        second,
    )
    assert m, second
    assert int(m.group(1)) == 0
    assert int(m.group(2)) > 0
    assert int(m.group(3)) == 0


def test_op_phase_sets_authored_phase_and_clears(pcb: PcbHandler, store: Any) -> None:
    """``op='phase'`` is the only way ``fab`` is judged at — it lands on
    ``pcb_boards.meta['phase']`` and overrides the derived default."""
    pcb.put(id="chk-auth", args=_mini_design())
    ref_id = store.get_ref(kind="pcb", id="chk-auth").id
    # An unmarked SPARE pin warns at the derived 'placed' phase…
    body = pcb.get(id="chk-auth", view="check").body
    assert "phase placed (derived)" in body
    assert "unconnected_pin" in body
    # …and the design reports fab only once the author claims it.
    r = pcb.put(id="chk-auth", args={"op": "phase", "phase": "fab"})
    assert "phase = fab" in r.body
    assert store.pcb_load(ref_id)["board"]["meta"]["phase"] == "fab"
    body = pcb.get(id="chk-auth", view="check").body
    assert "phase fab (authored)" in body
    # args.phase still overrides the claim for a one-off look.
    placed = pcb.get(id="chk-auth", view="check", args={"phase": "placed"}).body
    assert "phase placed (args)" in placed
    # Clearing returns to the derived default.
    pcb.put(id="chk-auth", args={"op": "phase", "phase": None})
    assert "phase" not in store.pcb_load(ref_id)["board"]["meta"]


def test_op_phase_validates_vocabulary(pcb: PcbHandler) -> None:
    pcb.put(id="chk-badphase", args=_mini_design())
    with pytest.raises(Exception, match="not recognized"):
        pcb.put(id="chk-badphase", args={"op": "phase", "phase": "maybe"})


def test_check_severity_filter_includes_todo(pcb: PcbHandler) -> None:
    """``args.severity='todo'`` filters on the phase-tiered severity —
    an unrouted net at placed is a todo row."""
    pcb.put(id="chk-filter", args=_mini_design())
    out = pcb.get(
        id="chk-filter",
        view="check",
        args={"phase": "placed", "severity": "todo"},
    ).body
    assert "unrouted" in out
    assert "filtered: showing" in out


def test_check_sorts_error_warn_todo_info(pcb: PcbHandler) -> None:
    """The table orders the presented severity ladder explicitly —
    error > warn > todo > info — stable within a tier."""
    pcb.put(id="chk-sort", args=_mini_design())
    body = pcb.get(id="chk-sort", view="check", args={"phase": "placed"}).body
    severities = [
        ln.split("\t")[1]
        for ln in body.splitlines()
        if "\t" in ln and not ln.startswith("{id")
    ]
    order = {"error": 0, "warn": 1, "todo": 2, "info": 3}
    assert severities, body
    assert [order[s] for s in severities] == sorted(order[s] for s in severities)
    # And every tier is actually present on this fixture at `placed`:
    # must_terminate errors, the spare-pin warn, unrouted todos, and the
    # untyped ERC not-checked rows as info.
    assert {"error", "warn", "todo", "info"} <= set(severities)


def test_not_checked_rows_present_as_info(pcb: PcbHandler) -> None:
    """A rule that cannot reach a verdict is `info`, not `warn` — a
    clean board reads clean (the row persists at its base severity so
    the delta stays stable)."""
    pcb.put(id="chk-info", args=_mini_design())
    body = pcb.get(id="chk-info", view="check").body
    assert "not fully checked" in body
    info_rows = [ln for ln in body.splitlines() if "\tinfo\t" in ln]
    assert info_rows, body
    assert all("CANNOT be checked" in ln for ln in info_rows)
    # The severity filter accepts the tier.
    filtered = pcb.get(id="chk-info", view="check", args={"severity": "info"}).body
    assert "CANNOT be checked" in filtered


def test_unconnected_pin_warns_below_fab_errors_at_fab(pcb: PcbHandler) -> None:
    """``unconnected_pin`` (the slice-2 finding) is a ``warn`` below
    ``fab`` and a per-pin ``error`` AT ``fab`` — read through
    ``view='check'``'s phase tiering."""
    pcb.put(id="chk-unconn", args=_mini_design())
    placed = pcb.get(id="chk-unconn", view="check", args={"phase": "placed"}).body
    fab = pcb.get(id="chk-unconn", view="check", args={"phase": "fab"}).body
    # The unmarked spare pads aggregate into one warn per part below fab…
    assert re.search(r"warn\tunconnected_pin\tpart U1", placed)
    # …and each reports as its own error row at fab.
    assert re.search(r"error\tunconnected_pin\tpin U1/SPARE", fab)
    assert "SPARE" in fab


def test_dispatch_path_accepts_phase_and_check_args(runtime_with_store) -> None:
    """The runtime dispatch gate: ``op='phase'`` and the ``view='check'``
    filters must reach the handler through the real MCP path — ARGS_KEYS
    is the gate, undeclared keys are refused before dispatch."""
    rt = runtime_with_store
    out = rt.dispatch(
        "put",
        {"kind": "pcb", "id": "chk-disp", "__extras__": _mini_design()},
    )
    assert "[error:" not in out, out
    out = rt.dispatch(
        "put",
        {
            "kind": "pcb",
            "id": "chk-disp",
            "__extras__": {"op": "phase", "phase": "placed"},
        },
    )
    assert "phase = placed" in out, out
    out = rt.dispatch(
        "get",
        {
            "kind": "pcb",
            "id": "chk-disp",
            "view": "check",
            "__extras__": {
                "rule": "unrouted",
                "severity": "todo",
                "phase": "placed",
            },
        },
    )
    assert "not accepted" not in out, out
    assert "unrouted" in out


def test_gerber_banner_judges_at_fab_regardless_of_authored_phase(
    pcb: PcbHandler, store: Any
) -> None:
    """An authored ``phase='placed'`` on a board WITH copper errors must
    not produce a clean fab banner — the banner always evaluates at
    ``fab`` so a scoped claim never hides a geometric finding."""
    pcb.put(id="chk-banner", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="chk-banner")
    board_id = store.pcb_ensure_board(ref.id)
    net_ids = store.pcb_net_ids(ref.id)
    # Two copper tracks 0.02mm apart — a real clearance error only a
    # full (copper-reading) pass can see.
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["SIG"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.0], "end": [1.0, 0.0]}
                    ],
                    "width_mm": 0.2,
                },
            },
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["GND"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 0.22], "end": [1.0, 0.22]}
                    ],
                    "width_mm": 0.2,
                },
            },
        ],
    )
    pcb.put(id="chk-banner", args={"op": "phase", "phase": "placed"})
    banner, n_errors = pcb._gerber_drc_banner(ref.id, "chk-banner")
    assert "DRC FAILED" in banner, banner
    assert "clearance" in banner
    assert n_errors > 0


def test_phase_scoped_run_never_steals_the_full_runs_baseline(
    pcb: PcbHandler,
) -> None:
    """A ``phase='netlist'`` run must not become the next full run's
    delta baseline — else its zero copper findings would read as
    'everything resolved' and the next full run's as 'new'. Baselines
    are keyed per-phase via ``meta['drc_runs_by_phase']``."""
    pcb.put(id="chk-baseline", args=_mini_design())
    first = pcb.get(id="chk-baseline", view="check").body
    assert "none to compare against" in first
    pcb.get(id="chk-baseline", view="check", args={"phase": "netlist"})
    second = pcb.get(id="chk-baseline", view="check").body
    m = re.search(
        r"vs run \w+: (\d+) new, (\d+) still(?: \(\d+ worse\))?, (\d+) resolved",
        second,
    )
    assert m, second
    assert int(m.group(1)) == 0, second
    assert int(m.group(2)) > 0
    assert int(m.group(3)) == 0


def test_netlist_eval_with_zero_findings_still_records_a_run(
    pcb: PcbHandler, store: Any
) -> None:
    """A netlist-phase eval that finds nothing still writes its
    ``pcb_drc_runs`` row — a clean run is a verdict (and the phase's
    fresh baseline), never "nothing could be checked"."""
    design = {
        "components": [
            {
                "refdes": "U1",
                "label": "x",
                "x": 0.0,
                "y": 0.0,
                "footprint": "F",
                # nc-marked: netless with intent, so the pin is not an
                # unconnected complaint; the design has no nets at all, so
                # there is nothing unrouted either — the eval is genuinely
                # finding-free.
                "pins": [{"name": "P1", "nc": True}],
            }
        ],
        "footprints": [
            {
                "name": "F",
                "pads": [
                    {"number": "1", "shape": "rect", "x": 0, "y": 0, "w": 1, "h": 1}
                ],
                "pin_map": {"1": "P1"},
            }
        ],
        "nets": [],
    }
    pcb.put(id="chk-empty", args=design)
    ref = store.get_ref(kind="pcb", id="chk-empty")
    ran = pcb._drc_run(ref.id, store.pcb_load(ref.id), eval_phase="netlist")
    assert ran is not None
    assert ran.findings == []
    # And the run row itself exists — the phase's baseline is fresh.
    run_id, _rows = store.pcb_drc_findings_latest(ref.id)
    assert run_id == ran.run_id


#: The exact ``view='drc'`` body the ``_mini_design`` fixture rendered
#: before this slice existed — ``run <8hex>`` masked — so the preset
#: claim is pinned, not implied. gr488909 dropped its three per-pad
#: ``connectivity`` rows: a copperless net is the ``unrouted`` question.
_DRC_GOLDEN = """
# DRC — run XXXXXXXX — 5 error(s), 2 warn(s)
vs previous run: none to compare against (first run with finding ids on this board) — every finding below is reported without a new/still verdict
(pads-only DRC — no routed copper yet)
{id	severity	rule	where	margin_mm	detail}
344573af2263	error	unrouted	net GND		at least one connection on net GND has no route; the board is not finished
7423a0f851af	error	unrouted	net SIG		at least one connection on net SIG has no route; the board is not finished
11898670f503	error	unrouted	net VDD		at least one connection on net VDD has no route; the board is not finished
4bae789a49c5	error	unconnected_pin	pin U1/MT		pin U1/MT is marked must_terminate and has no net — a pin that must not float is floating (the `unused` marker does not exempt it)
6d1dd5cc1ab6	error	unconnected_pin	pin U2/MT		pin U2/MT is marked must_terminate and has no net — a pin that must not float is floating (the `unused` marker does not exempt it)
fc7b2efdb934	warn	unconnected_pin	part U1		1 spare pad(s) on U1 netless and unmarked (SPARE) — mark them `unused` (this board does not use them; full geometry, still matcher-available) or `nc` (part has no internal connection), or wire them
096d7e1e21f2	warn	unconnected_pin	part U2		1 spare pad(s) on U2 netless and unmarked (SPARE) — mark them `unused` (this board does not use them; full geometry, still matcher-available) or `nc` (part has no internal connection), or wire them"""


def test_view_drc_byte_identical_for_unchanged_fixture(pcb: PcbHandler) -> None:
    """``view='drc'`` is the check surface's geometric preset — a fixture
    with no authored phase renders exactly what the pre-slice view
    rendered (run ids masked, the body itself byte-for-byte)."""
    pcb.put(id="chk-drc-same", args=_mini_design())
    body = pcb.get(id="chk-drc-same", view="drc").body
    normalized = re.sub(r"run [0-9a-f]{8}", "run XXXXXXXX", body)
    expected = re.sub(r"run [0-9a-f]{8}", "run XXXXXXXX", _DRC_GOLDEN.strip("\n"))
    assert normalized == expected


def test_check_netlist_collapses_unrouted_rows(pcb: PcbHandler, store: Any) -> None:
    """gr485271 — at phase ``netlist`` an unrouted net is the EXPECTED
    state, not a finding: the per-net rows collapse to one head line so
    they stop burying the real netlist findings. The rows still persist
    in the run — the next netlist run reports them ``still``, never
    ``gone`` — and ``args.rule='unrouted'`` expands them."""
    pcb.put(id="chk-unrouted", args=_mini_design())
    body = pcb.get(id="chk-unrouted", view="check", args={"phase": "netlist"}).body
    assert "3 net(s) unrouted — expected before routing" in body
    rows = [ln for ln in body.splitlines() if "\t" in ln]
    assert not any(ln.split("\t")[2] == "unrouted" for ln in rows), body
    # …while the real netlist findings stay in the table.
    assert "unconnected_pin" in body
    # An explicit rule filter expands the collapsed rows.
    expanded = pcb.get(
        id="chk-unrouted",
        view="check",
        args={"phase": "netlist", "rule": "unrouted"},
    ).body
    assert "\ttodo\tunrouted\t" in expanded
    # The persisted run keeps the rows: a second netlist eval counts them
    # 'still' — never 'gone'.
    second = pcb.get(id="chk-unrouted", view="check", args={"phase": "netlist"}).body
    m = re.search(
        r"vs run \w+: (\d+) new, (\d+) still(?: \(\d+ worse\))?, (\d+) resolved",
        second,
    )
    assert m, second
    assert int(m.group(1)) == 0 and int(m.group(2)) > 0 and int(m.group(3)) == 0
    assert "gone since" not in second


def test_check_routed_phase_keeps_unrouted_rows(pcb: PcbHandler) -> None:
    """``routed``/``fab`` are unchanged — an unrouted net is a per-net
    ``error`` row again once the router has run."""
    pcb.put(id="chk-unrouted2", args=_mini_design())
    body = pcb.get(id="chk-unrouted2", view="check", args={"phase": "routed"}).body
    assert "\terror\tunrouted\t" in body
    assert "expected before routing" not in body


def test_check_netlist_unrouted_only_board_reads_clean(
    pcb: PcbHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A netlist-phase run whose ONLY rows are unrouted nets reads clean
    under the collapse line — ``— no findings ✓``, not the misleading
    "no findings match the filter" tail (there IS no filter)."""
    from precis.handlers import pcb as pcb_handler
    from precis.pcb import drc as pcb_drc

    pcb.put(id="chk-only-unrouted", args=_mini_design())
    findings = [
        pcb_drc.DrcFinding("unrouted", "error", f"net {n}", f"net {n} has no route")
        for n in ("SIG", "VDD", "GND")
    ]
    run = pcb_handler._DrcRun(
        "abcdef0123", findings, True, pcb_drc.FindingDelta(previous_run_id=None)
    )
    monkeypatch.setattr(pcb, "_drc_run", lambda ref_id, design, **kw: run)
    body = pcb.get(id="chk-only-unrouted", view="check", args={"phase": "netlist"}).body
    assert "3 net(s) unrouted — expected before routing" in body
    assert "— no findings ✓" in body
    assert "no findings match" not in body


def _two_tracks_too_close(store: Any, slug: str) -> None:
    """SIG and GND tracks 0.02 mm apart — a clearance error only a pass
    that reads the copper can see. No route rows, so the derived phase
    stays ``placed``."""
    ref = store.get_ref(kind="pcb", id=slug)
    board_id = store.pcb_ensure_board(ref.id)
    net_ids = store.pcb_net_ids(ref.id)
    store.pcb_copper_replace(
        board_id,
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids[net],
                "route_id": None,
                "geom": {
                    "segments": [{"shape": "line", "start": [0.0, y], "end": [1.0, y]}],
                    "width_mm": 0.2,
                },
            }
            for net, y in (("SIG", 0.0), ("GND", 0.22))
        ],
    )


def test_check_placed_phase_reports_no_connectivity_for_copperless_nets(
    pcb: PcbHandler,
) -> None:
    """gr488909: a net with no copper is the ``unrouted`` question (a todo
    before routing), not a ``connectivity`` error per pad — taz15's
    placed run read ~80 such errors ("GND 78 pieces" = its 78 pads)."""
    pcb.put(id="chk-copperless", args=_mini_design())
    body = pcb.get(id="chk-copperless", view="check", args={"phase": "placed"}).body
    assert "\tunrouted\t" in body
    assert "\tconnectivity\t" not in body, body


def test_check_derived_phase_reads_existing_copper(pcb: PcbHandler, store: Any) -> None:
    """gr488909: only a CLAIMED ``placed`` (args or ``op='phase'``) is the
    pads-only pass. A merely derived ``placed`` board that carries copper
    (taz15: 749 imported tracks, no route rows) gets it checked."""
    pcb.put(id="chk-derived-copper", args=_mini_design())
    _two_tracks_too_close(store, "chk-derived-copper")
    body = pcb.get(id="chk-derived-copper", view="check").body
    assert "phase placed (derived)" in body
    assert "pads-only" not in body, body
    assert "\tclearance\t" in body, body
    # The explicit claim still scopes the run to pads.
    claimed = pcb.get(
        id="chk-derived-copper", view="check", args={"phase": "placed"}
    ).body
    assert "pads-only" in claimed
    assert "\tclearance\t" not in claimed


def test_first_run_at_a_phase_never_diffs_another_phases_run(
    pcb: PcbHandler,
) -> None:
    """gr488910: two ``placed`` runs supersede the first placed baseline;
    the first ``routed`` run then has no baseline of its own and must say
    so — not diff the superseded placed run as if it were a pre-keying
    (unkeyed) one (taz15: placed and routed both read "vs run 523fda88")."""
    pcb.put(id="chk-phase-delta", args=_mini_design())
    pcb.get(id="chk-phase-delta", view="check", args={"phase": "placed"})
    pcb.get(id="chk-phase-delta", view="check", args={"phase": "placed"})
    routed = pcb.get(id="chk-phase-delta", view="check", args={"phase": "routed"}).body
    assert "none to compare against" in routed, routed


def test_check_netlist_head_counts_collapsed_unrouted_todos(
    pcb: PcbHandler,
) -> None:
    """gr488911: the collapsed unrouted rows are still todos — the head's
    tally must count them, the same with or without a rule filter."""
    pcb.put(id="chk-todo-tally", args=_mini_design())
    body = pcb.get(id="chk-todo-tally", view="check", args={"phase": "netlist"}).body
    head = body.splitlines()[0]
    assert ", 3 todo" in head, head
    expanded = pcb.get(
        id="chk-todo-tally",
        view="check",
        args={"phase": "netlist", "rule": "unrouted"},
    ).body
    assert ", 3 todo" in expanded.splitlines()[0]
