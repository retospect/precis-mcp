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
from precis.pcb import check_run

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
    first = pcb.put(id="chk-delta", args={"op": "check"}).body
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


# ── gr496242: an unchanged board reuses its last recorded run ──────────


@pytest.fixture
def run_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A counting wrapper over :func:`precis.pcb.check_run.run` — reuse
    must never reach it."""
    calls: list[int] = []
    real = check_run.run

    def _spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(check_run, "run", _spy)
    return calls


def test_check_reuses_recorded_run_on_unchanged_board(
    pcb: PcbHandler, run_spy: list[int]
) -> None:
    """After ``op='check'`` records a run, the same board's
    ``view='check'`` serves it back — no DRC call — and says which run
    and that it was reused."""
    pcb.put(id="chk-reuse", args=_mini_design())
    recorded = pcb.put(id="chk-reuse", args={"op": "check"}).body
    run_id = re.search(r"run (\w{8})", recorded)
    assert run_id, recorded
    n_recorded = len(run_spy)  # the op='check' run itself
    body = pcb.get(id="chk-reuse", view="check").body
    assert len(run_spy) == n_recorded  # the get ran no DRC of its own
    assert f"reused run {run_id.group(1)} recorded" in body, body
    assert "ago" in body


def test_drc_reuses_recorded_run_on_unchanged_board(
    pcb: PcbHandler, run_spy: list[int]
) -> None:
    """``view='drc'`` takes the same reuse path (gr496242 names both
    views)."""
    pcb.put(id="chk-reuse-drc", args=_mini_design())
    pcb.put(id="chk-reuse-drc", args={"op": "check"})
    n_recorded = len(run_spy)
    body = pcb.get(id="chk-reuse-drc", view="drc").body
    assert len(run_spy) == n_recorded
    assert "reused run" in body, body


def test_check_reruns_when_board_changed(
    pcb: PcbHandler, store: Any, run_spy: list[int]
) -> None:
    """A design edit stales the recorded run's revision stamp — the view
    runs inline again."""
    pcb.put(id="chk-stale", args=_mini_design())
    pcb.put(id="chk-stale", args={"op": "check"})
    n_recorded = len(run_spy)
    ref = store.get_ref(kind="pcb", id="chk-stale")
    assert store.pcb_set_placement(ref.id, {"U1": (0.0, 0.0)}) == 1
    body = pcb.get(id="chk-stale", view="check").body
    assert len(run_spy) == n_recorded + 1
    assert "reused" not in body


def test_check_fresh_arg_forces_rerun(pcb: PcbHandler, run_spy: list[int]) -> None:
    """``args={'fresh': true}`` is the explicit way past reuse — the run
    happens inline even with the board unchanged."""
    pcb.put(id="chk-fresh", args=_mini_design())
    pcb.put(id="chk-fresh", args={"op": "check"})
    n_recorded = len(run_spy)
    body = pcb.get(id="chk-fresh", view="check", args={"fresh": True}).body
    assert len(run_spy) == n_recorded + 1
    assert "reused" not in body


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


def test_check_repeated_finding_key_rows_get_distinct_ids(
    pcb: PcbHandler, store: Any
) -> None:
    """gr495288: two under-width tracks on one net+layer share a
    ``finding_key`` by design — the rendered ``id`` must still name each
    row (``<key>#1`` / ``<key>#2``), not repeat one id."""
    pcb.put(id="chk-ids", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="chk-ids")
    board_id = store.pcb_ensure_board(ref.id)
    net_ids = store.pcb_net_ids(ref.id)
    # Two tracks on the same net+layer, 0.100mm wide — over the JLC min
    # (0.090) but under the house default (0.150): two warn findings, one
    # shared (rule, participants, layer) key.
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
                    "width_mm": 0.1,
                },
            },
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net_id": net_ids["SIG"],
                "route_id": None,
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [0.0, 2.0], "end": [1.0, 2.0]}
                    ],
                    "width_mm": 0.1,
                },
            },
        ],
    )
    body = pcb.get(id="chk-ids", view="check").body
    tw = [ln for ln in body.splitlines() if "\ttrace_width\t" in ln]
    assert len(tw) == 2, body
    ids = [ln.split("\t")[0] for ln in tw]
    assert len(set(ids)) == 2, body
    assert sorted(i.rsplit("#", 1)[-1] for i in ids) == ["1", "2"], body


def test_gerber_banner_error_count_matches_check_at_fab(
    pcb: PcbHandler, store: Any
) -> None:
    """gr495288: the gerber banner and ``view='check'`` count errors from
    ONE run shape — the banner used to skip the ERC rules and undercount.
    Here ``over_current`` is an ERC-only finding: a pin rated 0.5 A on a
    net declared at 2 A."""
    design = _mini_design()
    for n in design["nets"]:
        if n["name"] == "VDD":
            n["est_current_a"] = 2.0
    pcb.put(id="chk-banner-erc", args=design)
    ref = store.get_ref(kind="pcb", id="chk-banner-erc")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_pins SET meta = meta || %s::jsonb "
            "WHERE name = 'VDD' AND component_id IN "
            "(SELECT component_id FROM pcb_components WHERE ref_id = %s)",
            ('{"max_current_a": 0.5}', ref.id),
        )
    body = pcb.get(id="chk-banner-erc", view="check", args={"phase": "fab"}).body
    assert "over_current" in body, body
    m = re.search(r"(\d+) error\(s\)", body)
    assert m, body
    banner, n_errors = pcb._gerber_drc_banner(ref.id, "chk-banner-erc")
    assert "DRC FAILED" in banner and "over_current" in banner, banner
    assert n_errors == int(m.group(1)), (banner, body)


def test_phase_scoped_run_never_steals_the_full_runs_baseline(
    pcb: PcbHandler,
) -> None:
    """A ``phase='netlist'`` run must not become the next full run's
    delta baseline — else its zero copper findings would read as
    'everything resolved' and the next full run's as 'new'. Baselines
    are keyed per-phase via ``meta['drc_runs_by_phase']``."""
    pcb.put(id="chk-baseline", args=_mini_design())
    first = pcb.put(id="chk-baseline", args={"op": "check"}).body
    assert "none to compare against" in first
    pcb.put(id="chk-baseline", args={"op": "check", "phase": "netlist"})
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
    # Read-only (gr491591): the head says ``unrecorded run`` and the body
    # ends with the record-hint tail.
    expected = _DRC_GOLDEN.strip("\n").replace("run XXXXXXXX", "unrecorded run")
    expected += (
        "\n\nThis view records nothing; the delta is against the last recorded "
        "run. To record one: put(kind='pcb', id='chk-drc-same', args={'op':'check'})"
    )
    assert body == expected


def test_check_netlist_collapses_unrouted_rows(pcb: PcbHandler, store: Any) -> None:
    """gr485271 — at phase ``netlist`` an unrouted net is the EXPECTED
    state, not a finding: the per-net rows collapse to one head line so
    they stop burying the real netlist findings. The rows still persist
    in the run — the next netlist run reports them ``still``, never
    ``gone`` — and ``args.rule='unrouted'`` expands them."""
    pcb.put(id="chk-unrouted", args=_mini_design())
    body = pcb.put(id="chk-unrouted", args={"op": "check", "phase": "netlist"}).body
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
    # 'still' — never 'gone'. The first (recording) run above is the baseline.
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


def _fixed_vdd_stub_and_gnd_pour(store: Any, slug: str) -> int:
    """The taz15 shape in miniature (gr492989): the board's only copper is
    AUTHORED fixed copper (one VDD stub off U1's VDD pad), and GND is a
    pour-carried net on F.Cu, so U1's and U2's GND pads are joined by the
    fill and nothing else. Returns the ref id."""
    ref = store.get_ref(kind="pcb", id=slug)
    board_id = store.pcb_ensure_board(ref.id)
    assert store.pcb_assign_plane(ref.id, "F.Cu", "GND")
    store.pcb_fixed_copper_put(
        ref.id,
        board_id,
        "__epro_source",
        "epro_import",
        "1",
        [
            {
                "ctype": "track",
                "layer": "F.Cu",
                "net": "VDD",
                "geom": {
                    "segments": [
                        {"shape": "line", "start": [-7.0, 0.0], "end": [-7.0, -3.0]}
                    ],
                    "width_mm": 0.2,
                },
            }
        ],
    )
    return int(ref.id)


def test_check_pours_fixed_copper_board_so_pour_nets_read_connected(
    pcb: PcbHandler, store: Any
) -> None:
    """gr492989: a board whose copper is all fixed source copper was never
    realized, so nothing poured its pour-carried nets and DRC read each as
    split or unrouted (taz15-r2: GND, 3V3, PV+, 12V_POWER). The check run
    pours them with realize's own pour code, so pads its own net's fill
    reaches count as connected."""
    from precis.pcb import check_run

    pcb.put(id="chk-fixed-pour", args=_mini_design())
    ref_id = _fixed_vdd_stub_and_gnd_pour(store, "chk-fixed-pour")
    design = store.pcb_load(ref_id)
    ran = check_run.run(store, ref_id, design, eval_phase="routed")
    assert ran is not None
    by_rule: dict[str, set[str]] = {}
    for f in ran.findings:
        by_rule.setdefault(f.rule, set()).add(f.where)
    assert "net GND" not in by_rule.get("unrouted", set()), by_rule
    assert "net GND" not in by_rule.get("connectivity", set()), by_rule
    # A net the fill does not carry is judged exactly as before.
    assert "net SIG" in by_rule.get("unrouted", set()), by_rule
    # Nothing is written: the pour exists in the check's model only.
    board_id = int(design["board"]["board_id"])
    assert not [c for c in store.pcb_copper_list(board_id) if c.get("ctype") == "pour"]
