"""Declared netlist motifs; pure stdlib, with the builder's finding contract.

Eligibility uses pin functions or semantic pin labels, never catalog/model
names. An explicit function table entry overrides its pin's label. Passive
roles override conventional R/C/L designators. The FB divider identifies a
buck's output independently of the inductor; fixed-output parts instead
sense that output directly at FB. Multi-terminal passives use every pair
of terminals as a bridge, since the builder has no internal winding/network
map. Unclassified parts are ineligible, not errors.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis_geom.pcb import Board, CheckFinding, Part


@dataclass(frozen=True)
class Motif:
    """One pattern: a name, per-part eligibility and a board check callback.

    The callback receives eligible parts in authored order; it returns the
    same CheckFinding records as the builder's other netlist checks.
    """

    name: str
    eligibility: Callable[[Part], bool]
    check: Callable[[Board, list[Part]], list[CheckFinding]]


def _pins(part: Part, function: str) -> list[str]:
    names = dict.fromkeys([*part._pins, *part._pin_functions])
    return [
        name
        for name in names
        if function
        in {
            str(f).lower()
            for f in part._pin_functions.get(name, {"functions": [name]})["functions"]
        }
    ]


def _ldo_pins(part: Part, function: str) -> list[str]:
    pins = _pins(part, function)
    if pins or "ldo" not in {role.lower() for role in part.roles}:
        return pins
    etype = {"vin": "power_in", "vout": "power_out"}.get(function)
    return [
        name
        for name, pin in part._pins.items()
        if name not in part._pin_functions
        and etype is not None
        and pin.extra.get("etype") == etype
    ]


def _buck_eligible(part: Part) -> bool:
    return all(_pins(part, f) for f in ("sw", "fb", "gnd"))


def _ldo_eligible(part: Part) -> bool:
    return not _pins(part, "sw") and all(
        _ldo_pins(part, f) for f in ("vin", "vout", "gnd")
    )


def _led_eligible(part: Part) -> bool:
    return any(
        str(f).upper().startswith("LED")
        for rec in part._pin_functions.values()
        for f in rec.get("functions", ())
    )


def _pin_nets(board: Board) -> dict[str, dict[str, str]]:
    nets: dict[str, dict[str, str]] = {}
    for connection in board._connections:
        nets.setdefault(connection["refdes"], {})[connection["pin"]] = connection["net"]
    return nets


def _passive(part: Part, role: str) -> bool:
    roles = {r.lower() for r in part.roles}
    if roles & {"resistor", "capacitor", "inductor"}:
        return role in roles
    prefix = re.match(r"[A-Za-z]+", part.refdes.rsplit(".", 1)[-1])
    return (
        prefix is not None
        and prefix[0].upper()
        == {
            "resistor": "R",
            "capacitor": "C",
            "inductor": "L",
        }[role]
    )


def _bridges(
    board: Board, nets: dict[str, dict[str, str]], role: str
) -> list[tuple[Part, str, str, str, str]]:
    bridges = []
    for ref in board._part_order:
        part = board._parts[ref]
        terminals = list(nets.get(ref, {}).items())
        if _passive(part, role):
            for (p1, n1), (p2, n2) in combinations(terminals, 2):
                if n1 != n2:
                    bridges.append((part, p1, n1, p2, n2))
    return bridges


def _finding(
    motif: str, part: Part, pin: str, net: str | None, reason: str
) -> CheckFinding:
    from precis_geom.pcb import CheckFinding

    declared_pin = part._pins.get(pin)
    return CheckFinding(
        code=f"motif.{motif}",
        refdes=part.refdes,
        pin=pin,
        net=net,
        site=(declared_pin.site if declared_pin else None)
        or part._pin_function_sites.get(pin)
        or part.site,
        message=f"{part.refdes}.{pin} on net {net or '(unconnected)'}: {reason}",
    )


def _buck_check(board: Board, parts: list[Part]) -> list[CheckFinding]:
    nets = _pin_nets(board)
    resistors = _bridges(board, nets, "resistor")
    inductors = _bridges(board, nets, "inductor")
    findings = []
    for part in parts:
        pins = nets.get(part.refdes, {})
        ground = {pins[p] for p in _pins(part, "gnd") if p in pins}
        inputs = {pins[p] for p in _pins(part, "vin") if p in pins}
        switches = {pins[p] for p in _pins(part, "sw") if p in pins}
        outputs: set[str] = set()
        for pin in _pins(part, "fb"):
            net = pins.get(pin)
            neighbors = {
                n2 if n1 == net else n1
                for _, _, n1, _, n2 in resistors
                if net in (n1, n2)
            }
            rails = neighbors - ground - inputs - switches
            # A fixed-output converter senses the output directly, without
            # an external divider. Reject input/ground/SW shorts as outputs.
            # Existing divider evidence wins: an inductor mistakenly wired
            # to FB must not make an adjustable converter look fixed-output.
            if (
                net is not None
                and not rails
                and net not in ground | inputs | switches
                and any(
                    (n1 == net and n2 in switches) or (n2 == net and n1 in switches)
                    for _, _, n1, _, n2 in inductors
                )
            ):
                outputs.add(net)
                continue
            if not neighbors & ground:
                findings.append(
                    _finding("buck", part, pin, net, "FB needs a resistor to GND")
                )
            if not rails:
                findings.append(
                    _finding(
                        "buck",
                        part,
                        pin,
                        net,
                        "FB needs a divider resistor to the output rail",
                    )
                )
            outputs.update(rails)
        # Without a divider or direct sense there is no output identity;
        # report that cause above instead of guessing an inductor's far rail.
        if not outputs:
            continue
        for pin in _pins(part, "sw"):
            net = pins.get(pin)
            candidates = [i for i in inductors if net in (i[2], i[4])]
            if any(
                (n2 if n1 == net else n1) in outputs for _, _, n1, _, n2 in candidates
            ):
                continue
            if candidates:
                # One finding per SW pin, with every offending far terminal.
                # Multiple SW terminals on a passive can yield the same far
                # endpoint through several pairs; name that endpoint once.
                endpoints = {}
                for inductor, p1, n1, p2, n2 in candidates:
                    bad_pin, bad_net = (p2, n2) if n1 == net else (p1, n1)
                    endpoints[(inductor.refdes, bad_pin, bad_net)] = inductor
                subject_net: str | None
                if len(endpoints) == 1:
                    (_, bad_pin, bad_net), inductor = next(iter(endpoints.items()))
                    subject, subject_pin, subject_net = inductor, bad_pin, bad_net
                else:
                    subject, subject_pin, subject_net = part, pin, net
                terminals = ", ".join(
                    f"{ref}.{p} on net {n}" for ref, p, n in endpoints
                )
                findings.append(
                    _finding(
                        "buck",
                        subject,
                        subject_pin,
                        subject_net,
                        f"inductor terminals {terminals} from {part.refdes}.{pin} "
                        f"({net}) must end on output rail {'/'.join(sorted(outputs))}",
                    )
                )
            else:
                findings.append(
                    _finding(
                        "buck",
                        part,
                        pin,
                        net,
                        "SW needs an inductor to output rail "
                        + "/".join(sorted(outputs)),
                    )
                )
    return findings


def _ldo_check(board: Board, parts: list[Part]) -> list[CheckFinding]:
    nets = _pin_nets(board)
    capacitors = _bridges(board, nets, "capacitor")
    findings = []
    for part in parts:
        pins = nets.get(part.refdes, {})
        ground = {pins[p] for p in _ldo_pins(part, "gnd") if p in pins}
        for function in ("vin", "vout"):
            for pin in _ldo_pins(part, function):
                net = pins.get(pin)
                if not any(
                    (n1 == net and n2 in ground) or (n2 == net and n1 in ground)
                    for _, _, n1, _, n2 in capacitors
                ):
                    findings.append(
                        _finding(
                            "ldo",
                            part,
                            pin,
                            net,
                            f"{function.upper()} needs a capacitor to GND",
                        )
                    )
    return findings


def motif_findings(board: Board) -> list[CheckFinding]:
    """Run eligible motifs at the builder's geometry-free netlist phase.

    Args:
        board (Board): Board whose declared parts and connections are checked.
    """
    findings: list[CheckFinding] = []
    for motif in MOTIFS:
        parts = [
            board._parts[ref]
            for ref in board._part_order
            if motif.eligibility(board._parts[ref])
        ]
        if parts:
            findings.extend(motif.check(board, parts))
    return findings


def _led_check(board: Board, parts: list[Part]) -> list[CheckFinding]:
    # Keep the existing traversal and finding bytes; eligibility only selects
    # whether to run it. Requirements can originate on non-LED parts.
    from precis_geom.pcb import CheckFinding

    reqs = [
        c
        for c in board._connections
        if c.get("current_max_a") is not None or c.get("current_min_a") is not None
    ]
    slot_reqs = [
        fp
        for r in board._part_order
        for fp in board._parts[r]._flex_slots
        if fp.requirement is not None and fp.net is not None
    ]
    if not reqs and not slot_reqs:
        return []
    out: list[CheckFinding] = []
    req_nets = {c["net"] for c in reqs} | {
        fp.net for fp in slot_reqs if fp.net is not None
    }
    pin_nets_map: dict[str, dict[str, str]] = {}
    for c in board._connections:
        pin_nets_map.setdefault(c["refdes"], {})[c["pin"]] = c["net"]
    led_pins = [
        (r, p, pin_nets_map[r][p])
        for r in board._part_order
        for p, rec in board._parts[r]._pin_functions.items()
        if any(str(f_).upper().startswith("LED") for f_ in rec.get("functions", ()))
        and p in pin_nets_map.get(r, {})
    ]
    if led_pins:

        def is_limiter(refdes: str) -> bool:
            part = board._parts[refdes]
            return any(
                (part._pins.get(p) is not None)
                and part._pins[p].meta.get("current_role") == "series"
                for p in pin_nets_map[refdes]
            )

        net_to_parts: dict[str, set[str]] = {}
        for c in board._connections:
            net_to_parts.setdefault(c["net"], set()).add(c["refdes"])
        # Nets reachable from a requirement net through non-limiting
        # 2-pin bridges. A limiter on the path breaks the reach — a
        # series resistor is exactly the fix the check asks for.
        reached = set(req_nets)
        frontier = list(req_nets)
        while frontier:
            n = frontier.pop()
            for r in net_to_parts.get(n, ()):
                if len(pin_nets_map.get(r, {})) != 2 or is_limiter(r):
                    continue
                for n2 in pin_nets_map[r].values():
                    if n2 not in reached:
                        reached.add(n2)
                        frontier.append(n2)
        for r, p, net in led_pins:
            if net in reached:
                pin = board._parts[r]._pins.get(p)
                out.append(
                    CheckFinding(
                        code="led_without_limit",
                        refdes=r,
                        net=net,
                        pin=p,
                        site=(
                            (pin.site if pin is not None else None)
                            or board._parts[r]._pin_function_sites.get(p)
                        ),
                        message=(
                            f"{r}.{p} (LED) sits on a current path with "
                            "no series current-limit part"
                        ),
                    )
                )
    return out


# Authored order preserves the pre-registry LED findings before new motifs.
MOTIFS = (
    Motif("led_without_limit", _led_eligible, _led_check),
    Motif("buck", _buck_eligible, _buck_check),
    Motif("ldo", _ldo_eligible, _ldo_check),
)
