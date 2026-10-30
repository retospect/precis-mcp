"""gr522028 — the pcb probe resolver (shared-core-layers S1c):
``pcb.local.probe(board, selector)`` resolves a design-time selector
against the board's batch with no op run — ``pads_of('U3')`` counts
U3's pads (declared pins plus connection-minted ghosts, handle
spellings in the digest's ids) and a ``nets:`` glob returns the
matching nets. Nothing mutates: the batch is byte-identical after a
probe. A selector outside the grammar raises ``precis_diag.ProbeError``
carrying the refusal Diag.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import precis_geom.pcb as geom_pcb
from precis.pcb import local
from precis_diag import ProbeError
from precis_diag import probe as diag_probe

pytestmark = pytest.mark.filterwarnings("ignore")


def _demo_board() -> Any:
    """U3 (3 declared pins), R1 (2), LED1 (2); LED nets tied, VCC
    declared but empty."""
    b = geom_pcb.Board("probe-demo")
    u3 = b.part("U3")
    u3.declare(1, 2, 3)
    r1 = b.part("R1")
    r1.declare(1, 2)
    led = b.part("LED1")
    led.declare(1, 2)
    led[1].etype("passive", fmax_hz=0.0, assume="led anode")
    led[2].etype("passive", fmax_hz=0.0, assume="led cathode")
    r1[1].etype("passive", fmax_hz=0.0, assume="resistor")
    b.net("LED_A").tie(led[1], r1[1])
    b.net("LED_K").tie(led[2])
    b.net("VCC")
    return b


def test_pads_of_returns_the_pad_count_with_handle_ids(
    capsys: pytest.CaptureFixture[str],
) -> None:
    b = _demo_board()
    result = local.probe(b, "pads_of('U3')")
    assert result["count"] == 3
    assert result["digest"]["kind"] == "pads"
    assert result["digest"]["ids"] == ["U3.1", "U3.2", "U3.3"]
    assert "probe pads_of('U3'): resolves to 3 pads" in capsys.readouterr().out


def test_pads_of_includes_connection_minted_ghost_pins() -> None:
    b = _demo_board()
    u3 = b.part("U4")  # no declared pins at all
    u3[7].etype("power_in", max_current_a=0.1, max_voltage_v=3.3, assume="gnd pin")
    b.net("GND").tie(u3[7])
    result = local.probe(b, "pads_of(U4)")  # bare refdes spelling too
    assert result["count"] == 1
    assert result["digest"]["ids"] == ["U4.7"]


def test_nets_pattern_returns_matching_nets() -> None:
    b = _demo_board()
    result = local.probe(b, "nets:LED_*", label="leds")
    assert result["count"] == 2
    assert result["digest"]["kind"] == "nets"
    assert result["digest"]["ids"] == ["net:LED_A", "net:LED_K"]
    # sample: the short taste of the ids
    assert result["sample"] == "net:LED_A, net:LED_K"


def test_nets_pattern_without_wildcard_matches_exactly() -> None:
    b = _demo_board()
    result = local.probe(b, "nets:VCC")
    assert result["count"] == 1
    assert result["digest"]["ids"] == ["net:VCC"]
    # a pattern that matches nothing resolves to zero entities, not an
    # error — and the result points at the nets that do exist
    empty = local.probe(b, "nets:NOPE_*")
    assert empty["count"] == 0
    assert empty["digest"] == {"count": 0, "kind": "nets", "ids": []}
    assert empty["hint"] == ("no nets match 'NOPE_*'; nets look like LED_A, LED_K, VCC")


def test_probe_runs_no_op_and_mutates_nothing() -> None:
    b = _demo_board()
    before = json.dumps(b.to_batch(), sort_keys=True)
    local.probe(b, "pads_of('U3')")
    local.probe(b, "nets:*")
    assert json.dumps(b.to_batch(), sort_keys=True) == before
    # no placement exists — probing never touched the engines
    assert all(p.x is None for p in b._parts.values())


def test_probe_accepts_a_batch_dict() -> None:
    b = _demo_board()
    result = local.probe(b.to_batch(), "pads_of('R1')")
    assert result["count"] == 2
    assert result["digest"]["ids"] == ["R1.1", "R1.2"]


def test_probe_resolver_dispatch_and_unknown_refdes() -> None:
    from precis.pcb.probe import resolver_for

    b = _demo_board()
    result = diag_probe("pads_of('LED1')", resolver=resolver_for(b))
    assert result["count"] == 2
    with pytest.raises(ProbeError) as exc_info:
        diag_probe("pads_of('ZZ9')", resolver=resolver_for(b))
    assert exc_info.value.diag.code == "core.probe-unresolved"
    assert "ZZ9" in exc_info.value.diag.message
    assert exc_info.value.diag.next


def test_probe_outside_the_grammar_refuses() -> None:
    b = _demo_board()
    with pytest.raises(ProbeError) as exc_info:
        local.probe(b, "routed_nets:*")
    assert exc_info.value.diag.code == "core.probe-unresolved"


def test_board_script_probe_through_ws_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A board script's ``out.probe`` — the sandbox lane
    (``precis_ws.out``, what ``from precis_ws import out`` lands on) —
    delegates to ``pcb.local.probe``: count + digest come back, the
    one-line print fires, and the probe record lands on the calling
    trace frame, the same shape as the CAD lane's ``o.probe``
    (gr522028)."""
    from precis_geom import trace as _trace
    from precis_ws import out as ws_out

    ws_out._reset()
    try:
        b = _demo_board()
        with _trace.run(work_root=tmp_path) as tr:
            result = ws_out.probe(b, "pads_of('U3')", label="u3-pads")
    finally:
        ws_out._reset()
    assert result["count"] == 3
    assert result["digest"]["kind"] == "pads"
    assert result["digest"]["ids"] == ["U3.1", "U3.2", "U3.3"]
    assert "probe u3-pads: resolves to 3 pads" in capsys.readouterr().out
    (record,) = tr.root.probes
    assert record["label"] == "u3-pads"
    assert record["count"] == 3
    assert record["kind"] == "pads"
    assert record["digest"]["ids"] == ["U3.1", "U3.2", "U3.3"]


def test_ws_out_probe_out_of_grammar_selector_raises() -> None:
    """The sandbox lane's failure path: ``ws_out.probe`` with a selector
    outside the pcb grammar propagates ``ProbeError`` carrying the
    refusal Diag — nothing is recorded or resolved."""
    from precis_ws import out as ws_out

    ws_out._reset()
    try:
        b = _demo_board()
        with pytest.raises(ProbeError) as exc_info:
            ws_out.probe(b, "routed_nets:*")
    finally:
        ws_out._reset()
    assert exc_info.value.diag.code == "core.probe-unresolved"


def test_frames_card_lists_probe_records_under_the_frame() -> None:
    """view='frames' card: a frame's probes list one line each, the same
    spelling as the script-side probe line."""
    from precis.pcb.frames import Trace, card

    doc: dict[str, Any] = {
        "schema": 3,
        "frames": [
            {"id": 0, "key": "r", "qual": "run", "kind": "root", "parent": None},
            {
                "id": 1,
                "key": "a",
                "qual": "main.build",
                "kind": "user",
                "parent": "r",
                "probes": [
                    {
                        "label": "u3-pads",
                        "count": 3,
                        "kind": "pads",
                        "digest": {
                            "count": 3,
                            "kind": "pads",
                            "ids": ["U3.1", "U3.2", "U3.3"],
                        },
                    }
                ],
            },
        ],
    }
    text = card(Trace(doc), "a")
    assert "  probe u3-pads: resolves to 3 pads" in text
