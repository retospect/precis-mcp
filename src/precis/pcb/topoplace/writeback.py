"""topo-place 1b write-back (Decisions 14, 24): the assignment leaves
the solver in three forms — ``pins.lock.json``, ``pins.h`` and the
summary table.

``pins.lock.json`` is the one output that is also an input: a later run
loads it to freeze slot→pin choices once firmware has shipped or the
first PCB is cut (Decision 24's Q19 ruling). ``pins.h`` is the firmware
pin map — ``#define <NAME>_PIN <pin>`` per declared name, refdes in a
comment — because a swap the firmware does not know about is a broken
board (Decision 14). The summary table is the same facts rendered for
the run's ``summary.txt``.

Determinism (Decision 22): rows sort by name, JSON keys sort, trailing
newline — equal inputs give byte-identical files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.pins import PinAssignment

#: ``pins.lock.json``'s schema tag — bump on any shape change.
LOCK_SCHEMA = "precis.pins-lock/1"


def _c_name(name: str) -> str:
    """A declared slot/claim name as a C macro token: ``"red led"`` →
    ``RED_LED``. Non-alphanumeric runs collapse to ``_``; a leading
    digit gets a ``_`` prefix."""
    out = re.sub(r"[^0-9A-Za-z]+", "_", name).strip("_").upper()
    return f"_{out}" if out[:1].isdigit() else out


def _pins_rows(d: TopoDesign, a: PinAssignment) -> dict[str, dict[str, object]]:
    """name -> ``{refdes, pin}`` for the lock file — the by_name rows
    plus every unnamed slot under its ``refdes#i`` fallback key (a slot
    with no firmware name still needs its choice frozen)."""
    rows: dict[str, dict[str, object]] = {
        name: {"refdes": refdes, "pin": pin}
        for name, (refdes, pin) in a.by_name.items()
    }
    for i, s in enumerate(d.flex):
        if s.name is None and i < len(a.choice):
            rows[f"{d.parts[s.part]}#{i}"] = {
                "refdes": d.parts[s.part],
                "pin": (
                    int(str(d.pin_label[a.choice[i]]))
                    if str(d.pin_label[a.choice[i]]).isdigit()
                    else str(d.pin_label[a.choice[i]])
                ),
            }
    return rows


def lock_payload(d: TopoDesign, a: PinAssignment) -> dict:
    """The ``pins.lock.json`` payload without the write — the run
    manifest's ``pins.json`` file and the job meta's copy both need it
    (1c)."""
    return {
        "schema": LOCK_SCHEMA,
        "pins": _pins_rows(d, a),
        "swaps": [list(s) for s in a.swaps],
    }


def write_lock(path: str | Path, d: TopoDesign, a: PinAssignment) -> dict:
    """Write ``pins.lock.json`` and return its payload."""
    payload = lock_payload(d, a)
    Path(path).write_text(
        json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def read_lock(path: str | Path) -> dict:
    """A stored ``pins.lock.json`` — raises on a schema it is not."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != LOCK_SCHEMA:
        raise ValueError(
            f"{path}: pins lock schema {payload.get('schema')!r} — "
            f"this build reads {LOCK_SCHEMA!r}"
        )
    return payload


def pins_h(d: TopoDesign, a: PinAssignment) -> str:
    """The firmware pin map: one ``#define <NAME>_PIN <pin> /* refdes
    */`` per declared name, sorted for byte-identical reruns."""
    lines = [
        "/* pin assignment — precis topo-place (pins.lock.json is the",
        " * machine-readable twin; regenerate, do not edit) */",
    ]
    for name in sorted(a.by_name):
        refdes, pin = a.by_name[name]
        lines.append(f"#define {_c_name(name)}_PIN {pin} /* {refdes} */")
    return "\n".join(lines) + "\n"


def write_pins_h(path: str | Path, d: TopoDesign, a: PinAssignment) -> str:
    """Write ``out/pins.h`` and return its text."""
    text = pins_h(d, a)
    Path(path).write_text(text, encoding="utf-8")
    return text


def summary_table(d: TopoDesign, a: PinAssignment) -> str:
    """The run-summary pin table (Decision 19's write-back block): one
    row per declared name plus the swap-group moves and the
    declaration-order delta."""
    lines = ["pin assignment:", "  name            refdes  pin   net"]
    net_of = {
        i: (
            d.decl_net_name[d.flex[i].net]
            if d.flex[i].net < len(d.decl_net_name)
            else "?"
        )
        for i in range(len(d.flex))
    }
    for name in sorted(a.by_name):
        refdes, pin = a.by_name[name]
        net = next((net_of[i] for i, s in enumerate(d.flex) if s.name == name), "-")
        lines.append(f"  {name:<15} {refdes:<7} {pin!s:<5} {net}")
    if a.swaps:
        lines.append(f"  swap-group moves: {len(a.swaps)}")
        for refdes, pa, pb in a.swaps:
            lines.append(f"    {refdes}.{pa} <-> {refdes}.{pb}")
    lines.append(f"  crossings vs declaration order: {a.cost_vs_fixed:+g}")
    return "\n".join(lines) + "\n"


__all__ = [
    "LOCK_SCHEMA",
    "lock_payload",
    "pins_h",
    "read_lock",
    "summary_table",
    "write_lock",
    "write_pins_h",
]
