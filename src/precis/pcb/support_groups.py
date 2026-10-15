"""Support-passive groups — the organised rows a human layout draws and
the anneal otherwise has no way to discover
(docs/backlog/pcb-placement-groups.md; Reto 2026-10-14: "placement is
key — if we have a thing with lots of pull-ups, massive reduction from
organizing nicely").

Today every small passive is an independent body in the anneal. A bank
of I2C pull-ups arrives as six loose resistors; the engine may land each
within reach of the bus, but nothing prefers them *organised* — aligned
in a row, uniform rotation, rail pads on one shared line a single trace
serves. This module recognises that support structure from the netlist
(:func:`detect_support_groups`) and, post-anneal, re-lays each group as
one organised row (:meth:`OptimizeEngine.organize_support_groups` —
detection lives here; the pass lives with the engine because it needs
``_placement_is_legal`` and ``apply_move``, the same legality gate and
cache-synced move application ``snap_to_preferred`` uses).

It is a post-anneal pass, not a rigid-body stamp, on purpose. The first
cut stamped each group as a derived rigid ``inst_group`` before the
anneal; measured on taz15-blank seed 1 (2026-10-14) the rigid rows cost
the anneal 3.4x its settled objective and routed *fewer* nets (64/79 vs
65/79) — a multi-member rigid body is nearly immovable in a dense board
(the same lesson ``_merge_pattern_clusters``'s docstring records for
pattern tiles), so a row stamped anywhere suboptimal stayed there.
Re-organising AFTER the anneal keeps every member's mobility through
the search and ends organised regardless — a row that cannot be placed
legally simply keeps its members' annealed poses.

Three shapes are detected:

- ``rail_bank`` — 2-pin resistors sharing one net (the rail) on one
  side and going to distinct IC pins on the other: pull-up/pull-down
  banks. Members union when their far nets' served-instance sets
  overlap at ``min(2, |A|, |B|)`` — identical sets merge (SCL and SDA
  pull-ups on the same bus become one bank), a single shared hub IC
  alone does not. A bank then SUB-CLUSTERS by each member's nearest
  served pin: the six taz15 pull-ups become three SCL+SDA pairs, one at
  each bus stop — matching the human layout rather than forcing one
  long row at the middle of the bus.
- ``support_cap`` — 2-pin capacitors serving one IC, grouped per (IC,
  package edge): flying caps (both pins on nets of the same IC —
  charge pumps), caps on a fanout-2 net to a single IC pin (a
  reservoir, a dedicated supply pin's bypass), and rail/return-bridging
  caps assigned to the nearest IC pin on the smaller-fanout (served)
  net. A long edge row splits where consecutive served pins are more
  than :data:`_CAP_ROW_SPLIT_MM` apart — a bank of eight caps along an
  MCU's edge becomes a few short rows, not one 16 mm body.
- ``series_pair`` — two 2-pin passives sharing exactly one net (a
  fanout-``<=`` :data:`_SERIES_NET_MAX_FANOUT` link): R+R series,
  LED+resistor, RC snubber. Sharing TWO nets is a parallel RC, not a
  series element, and is excluded.

Members must be free in every axis the row needs — an instance with
``inst_fixed_xy``, ``inst_fixed_rot``, a finite ``inst_max_travel_mm``
leash, an existing ``inst_group``, or an ``inst_edge_mate`` is never a
member (the task's "frozen parts untouched", enforced at detection
rather than left to the legality gate); the pass additionally excludes
steering hard-``align`` operands, same as ``snap_to_preferred``.
Members must share a board side.

Layout (:func:`layout_group`): a row along the group-local X at a
uniform pitch (the largest member's keep-out diameter plus
:data:`_ROW_GAP_MM`), each member's rotation putting its SHARED-side
pad — the rail pin for a bank, the return pin for a cap, the facing
link pads for a series pair — on the same line, so one trace or pour
serves the row. The row anchors at the centroid of the pins it serves
(nudged off a sole served instance so it lands *beside* the pins, not
on them), rotation chosen by argmin member-to-served-centroid distance,
then ring-searched for a legal pose; a group with none keeps its
annealed poses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from precis.pcb import landpattern
from precis.pcb.ir import PcbIR, pin_point

#: A 2-pin passive by schematic refdes convention — the same family rule
#: ``drc.check_no_decoupling`` applies (``^C\d+$`` names a capacitor),
#: extended to the other families a support structure is made of.
_PASSIVE_RE = re.compile(r"^(R|C|L|D|LED)\d+$", re.IGNORECASE)
_RESISTOR_RE = re.compile(r"^R\d+$", re.IGNORECASE)
_CAPACITOR_RE = re.compile(r"^C\d+$", re.IGNORECASE)

#: A net two passives share is a series LINK only while it is small — a
#: shared rail (GND, VCC) makes passives parallel, never series, and a
#: mid-size bus is already better served by the rail-bank rule.
_SERIES_NET_MAX_FANOUT = 4

#: Two bank members belong to the same bus stop when their nearest
#: served pins sit within this distance of each other — sized to a small
#: IC's span so adjacent receivers (taz15's U14/U5 pair) still merge
#: into one stop.
_BANK_STOP_MM = 8.0

#: A per-(IC, edge) cap row splits where consecutive served-pin
#: centroids along the edge are further apart than this — long enough
#: that a real bypass row (the U9 charge pumps' 6.4 mm pin span) stays
#: whole, short enough that unrelated caps along one MCU edge do not
#: become one rigid-length row.
_CAP_ROW_SPLIT_MM = 7.0

#: Upper bound on any single row's membership — the human reference
#: (taz15's U9 charge-pump row) is four. A longer row fans its members'
#: ratsnest over a wide arc (measured on esp32c3: a 7-cap edge row
#: accounted for most of the +70 segment crossings on an otherwise
#: clean board); beyond four the "beside the pins" claim stops being
#: true for the outer members anyway.
_MAX_ROW_MEMBERS = 4

#: Extra room between a row's two neighbouring keep-outs, mm. The
#: anneal's own legality test (``_placement_is_legal``) only asks for
#: non-overlap; a row is meant to be solderable and inspectable, so the
#: pitch carries a small deliberate gap.
_ROW_GAP_MM = 0.6

#: How far past the served-pins centroid a single-owner row is pushed
#: (beyond each member's own keep-out radius), so a support row lands
#: beside the package edge it bypasses rather than on the pins.
_OWNER_NUDGE_MM = 0.4

#: The rotation draws a row's anchor pose is chosen from — the engine's
#: whole rotation vocabulary.
_ROTS = (0.0, 90.0, 180.0, 270.0)


@dataclass(frozen=True, slots=True)
class SupportGroup:
    """One detected support structure, in row order.

    - ``kind``: ``rail_bank`` | ``support_cap`` | ``series_pair``.
    - ``members``: instance ids in the order they sit along the row.
    - ``shared_pin``: per member, the pin id to put on the shared +Y
      line (rail pin / return pin), or ``None`` for a series member —
      series members face their LINK pads along the row instead.
    - ``link_pin``: per member, the pin on the shared series net
      (``series_pair`` only, ``None`` elsewhere).
    - ``served``: per member, the pin ids this member's far side serves —
      the anchor the row lands beside.
    - ``anchor_inst``: the single non-passive instance every served pin
      belongs to (a support-cap row's IC, a bank cluster's receiver),
      else ``None``.
    """

    kind: str
    members: tuple[int, ...]
    shared_pin: tuple[int | None, ...]
    link_pin: tuple[int | None, ...]
    served: tuple[tuple[int, ...], ...]
    anchor_inst: int | None

    def restricted(self, keep: set[int]) -> SupportGroup:
        """This group with only ``keep``'s members — parallel fields
        filtered in step, so the caller can drop frozen or hard-aligned
        members without rebuilding detection."""
        idx = [i for i, m in enumerate(self.members) if m in keep]
        return SupportGroup(
            kind=self.kind,
            members=tuple(self.members[i] for i in idx),
            shared_pin=tuple(self.shared_pin[i] for i in idx),
            link_pin=tuple(self.link_pin[i] for i in idx),
            served=tuple(self.served[i] for i in idx),
            anchor_inst=self.anchor_inst,
        )


def _refdes_key(ir: PcbIR, inst: int) -> tuple[str, int, str]:
    """Natural refdes order (R2 before R10) — the deterministic member
    order every row layout starts from."""
    refdes = str(ir.instance_refdes[inst])
    m = re.match(r"^(\D+)(\d+)$", refdes)
    if m:
        return (m.group(1), int(m.group(2)), refdes)
    return (refdes, 0, refdes)


def _pins_of_instance(ir: PcbIR) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {i: [] for i in range(ir.n_instances)}
    for p in range(ir.n_pins):
        out[int(ir.pin_instance[p])].append(p)
    return out


def _is_two_pin_passive(
    ir: PcbIR, inst: int, pins_of: dict[int, list[int]], pattern: re.Pattern[str]
) -> bool:
    """A support-structure member must be a plain two-pad part — a
    3-pin dual diode or a transistor is a small IC for placement
    purposes, not a passive, whatever its refdes says."""
    return (
        len(pins_of.get(inst, ())) == 2
        and pattern.match(str(ir.instance_refdes[inst])) is not None
    )


def _free_member(ir: PcbIR, inst: int) -> bool:
    """True when the instance may take a row pose: no fixed bits, no
    soft-pin leash, no existing rigid group, no edge-mate requirement.
    "Frozen parts untouched" — detection never claims a part the author
    pinned, so no pose a human (or generator) fixed is ever re-laid-out."""
    if int(ir.inst_group[inst]) >= 0:
        return False
    if bool(ir.inst_fixed_xy[inst]) or bool(ir.inst_fixed_rot[inst]):
        return False
    mt = ir.inst_max_travel_mm
    if mt is not None and len(mt) == ir.n_instances:
        if math.isfinite(float(mt[inst])):
            return False
    if ir.inst_edge_mate is not None and ir.inst_edge_mate[inst] is not None:
        return False
    return True


def _served_sets(
    ir: PcbIR, pins_of: dict[int, list[int]]
) -> tuple[dict[int, set[int]], dict[int, list[int]]]:
    """net -> non-passive member INSTANCES, and net -> their pin ids —
    "the pins a support passive exists to serve"."""
    insts: dict[int, set[int]] = {n: set() for n in range(ir.n_nets)}
    pins: dict[int, list[int]] = {n: [] for n in range(ir.n_nets)}
    for p in range(ir.n_pins):
        net = int(ir.pin_net[p])
        if net < 0:
            continue
        inst = int(ir.pin_instance[p])
        if _is_two_pin_passive(ir, inst, pins_of, _PASSIVE_RE):
            continue
        insts[net].add(inst)
        pins[net].append(p)
    return insts, pins


def _other_pin_net(
    ir: PcbIR, inst: int, pins_of: dict[int, list[int]], net: int
) -> int:
    for p in pins_of[inst]:
        n = int(ir.pin_net[p])
        if n != net:
            return n
    return -1


def _detect_series_pairs(
    ir: PcbIR,
    pins_of: dict[int, list[int]],
    net_pins: dict[int, list[int]],
    served_pins: dict[int, list[int]],
    free: list[bool],
    claimed: set[int],
) -> list[SupportGroup]:
    """Two 2-pin passives sharing exactly one small net — an LED +
    resistor, two series resistors on a bus, an RC snubber. The pair
    becomes a two-member row with the shared-link pads facing, so the
    link net is a straight stub instead of a routed run.

    The shared net must have no more than two passive members and a
    fanout at or under :data:`_SERIES_NET_MAX_FANOUT` — larger means a
    rail or bus the two merely share (parallel, not series). The two
    outer nets must also differ: a resistor and a cap bridging the SAME
    two nets are an RC pull-down to GND in parallel, not a series
    element (measured on esp32c3's BOOT net: R6+C16 share BOOT *and*
    GND — grouping them in-line would wire a pull-down as a series
    element)."""
    groups: list[SupportGroup] = []
    for net in range(ir.n_nets):
        members = net_pins.get(net, [])
        if len(members) > _SERIES_NET_MAX_FANOUT:
            continue
        passives = sorted(
            (
                i
                for i in {int(ir.pin_instance[p]) for p in members}
                if free[i] and _is_two_pin_passive(ir, i, pins_of, _PASSIVE_RE)
            ),
            key=lambda i: _refdes_key(ir, i),
        )
        if len(passives) != 2 or passives[0] in claimed or passives[1] in claimed:
            continue
        a, b = passives
        if bool(ir.inst_bottom[a]) != bool(ir.inst_bottom[b]):
            continue  # a row cannot span board sides
        net_a = _other_pin_net(ir, a, pins_of, net)
        net_b = _other_pin_net(ir, b, pins_of, net)
        if net_a < 0 or net_a == net_b:
            continue  # dangling second side, or a parallel pair
        pin_a = next(p for p in pins_of[a] if int(ir.pin_net[p]) == net)
        pin_b = next(p for p in pins_of[b] if int(ir.pin_net[p]) == net)
        tap = list(served_pins.get(net, []))
        served_a = tap + [
            p
            for p in served_pins.get(net_a, [])
            if len(net_pins.get(net_a, [])) <= _SERIES_NET_MAX_FANOUT
        ]
        served_b = tap + [
            p
            for p in served_pins.get(net_b, [])
            if len(net_pins.get(net_b, [])) <= _SERIES_NET_MAX_FANOUT
        ]
        groups.append(
            SupportGroup(
                kind="series_pair",
                members=(a, b),
                shared_pin=(None, None),
                link_pin=(pin_a, pin_b),
                served=(tuple(served_a), tuple(served_b)),
                anchor_inst=None,
            )
        )
        claimed.update(passives)
    return groups


def _edge_bucket(x: float, y: float) -> str:
    """Which side of the owner's local frame a served-pin centroid sits
    on — a decoupling row mirrors the package edge it bypasses."""
    if abs(y) >= abs(x):
        return "N" if y >= 0 else "S"
    return "E" if x >= 0 else "W"


def _detect_support_caps(
    ir: PcbIR,
    pins_of: dict[int, list[int]],
    net_pins: dict[int, list[int]],
    served_insts: dict[int, set[int]],
    served_pins: dict[int, list[int]],
    free: list[bool],
    claimed: set[int],
) -> list[SupportGroup]:
    """Caps organised per (served IC, package edge, pin-proximity run).

    Owner resolution, most to least certain: a flying cap's both nets
    contain pins of the SAME single IC (charge-pump caps between C1+/C1-
    style pins — the owner is unambiguous); a cap on a net whose ONLY
    non-passive member is one IC owns that IC (a reservoir or dedicated
    bypass); a cap bridging two shared nets goes to the instance owning
    the NEAREST non-passive pin on the smaller-fanout (served) net —
    nearest measured on current poses, so the pass must run after
    placement exists. An ambiguous cap with nothing placed is skipped
    rather than guessed. Each (owner, edge) bucket is ordered along the
    edge and split on gaps past :data:`_CAP_ROW_SPLIT_MM` so unrelated
    caps sharing an edge stay separate rows."""
    candidates: dict[
        tuple[int, str], list[tuple[int, int | None, tuple[int, ...]]]
    ] = {}
    for inst in range(ir.n_instances):
        if inst in claimed or not free[inst]:
            continue
        if not _is_two_pin_passive(ir, inst, pins_of, _CAPACITOR_RE):
            continue
        pa, pb = pins_of[inst]
        na, nb = int(ir.pin_net[pa]), int(ir.pin_net[pb])
        if na < 0 or nb < 0:
            continue  # a dangling cap supports nothing
        insts_a, insts_b = served_insts.get(na, set()), served_insts.get(nb, set())
        common = insts_a & insts_b
        owner: int | None = None
        served: tuple[int, ...] = ()
        shared_pin: int | None = None
        if len(insts_a) == 1 and len(insts_b) == 1 and common:
            # Flying cap: BOTH ends on nets exclusive to the same IC
            # (charge pumps). The "exclusive on both" qualifier matters:
            # a dedicated bypass (fanout-2 net to one IC pin + a rail
            # that also serves that IC) has a common of one too, but its
            # rail side is shared — it is the exclusive-owner case below,
            # not a bridge, and its row must order by its OWN served pin,
            # not the centroid of served pin and rail pin.
            owner = next(iter(common))
            served = tuple(
                p
                for p in served_pins[na] + served_pins[nb]
                if int(ir.pin_instance[p]) == owner
            )
            shared_pin = pa  # arbitrary shared side — the row rotation fixes it
        elif len(insts_a) == 1 or len(insts_b) == 1:
            if len(insts_a) == 1:
                owner_net, return_pin = na, pb
            else:
                owner_net, return_pin = nb, pa
            owner = next(iter(served_insts[owner_net]))
            served = tuple(
                p for p in served_pins[owner_net] if int(ir.pin_instance[p]) == owner
            )
            shared_pin = return_pin
        else:
            # Ambiguous (rail/return): assign to the nearest non-passive
            # pin on the smaller-fanout net. ``pin_point`` needs poses —
            # a caller that ran this before placement simply gets no
            # ambiguous caps.
            rail_net, return_net = (
                (na, nb) if len(net_pins[na]) <= len(net_pins[nb]) else (nb, na)
            )
            cx, cy = float(ir.inst_x[inst]), float(ir.inst_y[inst])
            if not (math.isfinite(cx) and math.isfinite(cy)):
                continue
            best: tuple[float, int] | None = None
            for p in served_pins.get(rail_net, []):
                pt = pin_point(ir, p)
                if pt is None:
                    continue
                d = (pt[0] - cx) ** 2 + (pt[1] - cy) ** 2
                if best is None or d < best[0]:
                    best = (d, p)
            if best is None:
                continue
            served_pin = best[1]
            owner = int(ir.pin_instance[served_pin])
            served = (served_pin,)
            shared_pin = pa if int(ir.pin_net[pa]) == return_net else pb
        if owner is None or not served:
            continue
        ox = sum(float(ir.pin_dx[p]) for p in served) / len(served)
        oy = sum(float(ir.pin_dy[p]) for p in served) / len(served)
        candidates.setdefault((owner, _edge_bucket(ox, oy)), []).append(
            (inst, shared_pin, served)
        )

    groups: list[SupportGroup] = []
    for owner, edge in sorted(candidates):
        rows = candidates[(owner, edge)]
        # Order along the edge by the served pins' owner-local position so
        # the row mirrors the pin order — no crossed ratsnests — then
        # split where consecutive centroids are too far apart for one row.
        horizontal = edge in ("N", "S")
        rows.sort(
            key=lambda r: (_edge_along(ir, r[2], horizontal), _refdes_key(ir, r[0]))
        )
        runs: list[list[tuple[int, int | None, tuple[int, ...]]]] = []
        chunk: list[tuple[int, int | None, tuple[int, ...]]] = []
        prev: float | None = None
        for row in rows:
            along = _edge_along(ir, row[2], horizontal)
            if prev is not None and along - prev > _CAP_ROW_SPLIT_MM and chunk:
                runs.append(chunk)
                chunk = []
            chunk.append(row)
            prev = along
        if chunk:
            runs.append(chunk)
        for run in runs:
            for part in _split_chunks(run, _MAX_ROW_MEMBERS):
                if len(part) >= 2:
                    groups.append(_cap_group(owner, part, claimed))
    return groups


def _split_chunks(items: list, max_n: int) -> list[list]:
    """Order-preserving balanced split into runs of at most ``max_n`` —
    a greedy ``[0:max_n][max_n:]`` leaves a lone trailing member (five
    becomes 4+1), so the remainder is spread over the runs instead
    (five becomes 3+2)."""
    if len(items) <= max_n:
        return [list(items)]
    n_runs = math.ceil(len(items) / max_n)
    base, rem = divmod(len(items), n_runs)
    out, i = [], 0
    for r in range(n_runs):
        take = base + (1 if r < rem else 0)
        out.append(list(items[i : i + take]))
        i += take
    return out


def _edge_along(ir: PcbIR, served: tuple[int, ...], horizontal: bool) -> float:
    """Mean owner-local coordinate of ``served`` along the package edge
    (x for a top/bottom row, y for a left/right one) — the position the
    cap's row slot mirrors."""
    return sum(
        float(ir.pin_dx[p] if horizontal else ir.pin_dy[p]) for p in served
    ) / len(served)


def _cap_group(
    owner: int,
    rows: list[tuple[int, int | None, tuple[int, ...]]],
    claimed: set[int],
) -> SupportGroup:
    for r in rows:
        claimed.add(r[0])
    return SupportGroup(
        kind="support_cap",
        members=tuple(r[0] for r in rows),
        shared_pin=tuple(r[1] for r in rows),
        link_pin=tuple(None for _ in rows),
        served=tuple(r[2] for r in rows),
        anchor_inst=owner,
    )


def _union_by_served_overlap(
    hanging: list[int], served_of: dict[int, set[int]]
) -> dict[int, list[int]]:
    """Union-find ``hanging`` indices merged by served-set overlap —
    kept out of the per-net loop so the closure never captures a loop
    variable."""
    parent = list(range(len(hanging)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ai in range(len(hanging)):
        for bi in range(ai + 1, len(hanging)):
            a, b = served_of[hanging[ai]], served_of[hanging[bi]]
            if len(a & b) >= min(2, len(a), len(b)):
                parent[find(bi)] = find(ai)
    unions: dict[int, list[int]] = {}
    for k, inst in enumerate(hanging):
        unions.setdefault(find(k), []).append(inst)
    return unions


@dataclass
class _Stop:
    """One nominated bus stop during bank sub-clustering — running
    centroid of the chosen served-pin positions plus the members that
    nominated it."""

    x: float
    y: float
    members: list[int]


def _bank_stop_clusters(
    ir: PcbIR,
    members: list[int],
    served_pins: dict[int, list[int]],
    far_net_of: dict[int, int],
) -> list[tuple[list[int], dict[int, list[int]], int]]:
    """Sub-cluster one rail bank's members by WHERE their served pins
    are — the pull-ups that belong at one bus stop, not one row for the
    whole bus. Each member's far-net served pin nearest its current
    pose nominates a stop; stops closer than :data:`_BANK_STOP_MM` merge
    (an SCL pin and an SDA pin on adjacent receivers are one stop). A
    member with no placed served pin keeps the whole far net as its
    served set and lands in a shared fallback cluster.

    Returns ``(members, served_by_member, anchor_inst)`` clusters:
    ``served_by_member`` maps member -> the pin ids its row should anchor
    to (the nominated stop's pins, or the full far net's for a fallback
    member); ``anchor_inst`` is the sole receiver when every nominated
    pin sits on the same instance, else ``-1``."""
    choice: dict[int, tuple[int, float, float] | None] = {}
    for m in members:
        mx, my = float(ir.inst_x[m]), float(ir.inst_y[m])
        best: tuple[float, int, float, float] | None = None
        if math.isfinite(mx) and math.isfinite(my):
            for p in served_pins.get(far_net_of[m], []):
                pt = pin_point(ir, p)
                if pt is None:
                    continue
                d = (pt[0] - mx) ** 2 + (pt[1] - my) ** 2
                if best is None or d < best[0]:
                    best = (d, p, pt[0], pt[1])
        choice[m] = None if best is None else (best[1], best[2], best[3])

    # Agglomerate nominated stop positions within _BANK_STOP_MM; members
    # with no placed served pin land in the single fallback cluster.
    stops: list[_Stop] = []
    fallback: list[int] = []
    for m in members:
        ch = choice[m]
        if ch is None:
            fallback.append(m)
            continue
        _pin, px, py = ch
        for stop in stops:
            if math.hypot(stop.x - px, stop.y - py) <= _BANK_STOP_MM:
                stop.members.append(m)
                stop.x = (stop.x * (len(stop.members) - 1) + px) / len(stop.members)
                stop.y = (stop.y * (len(stop.members) - 1) + py) / len(stop.members)
                break
        else:
            stops.append(_Stop(x=px, y=py, members=[m]))

    clusters: list[tuple[list[int], dict[int, list[int]], int]] = []
    for stop in stops:
        insts = {
            int(ir.pin_instance[ch[0]])
            for m in stop.members
            if (ch := choice[m]) is not None
        }
        served: dict[int, list[int]] = {}
        for m in stop.members:
            ch = choice[m]
            if len(insts) == 1:
                # All pins of that one receiver on the member's far net —
                # the anchor is the served edge, not one pad.
                served[m] = [
                    p
                    for p in served_pins.get(far_net_of[m], [])
                    if int(ir.pin_instance[p]) in insts
                ]
            elif ch is not None:
                served[m] = [ch[0]]
            else:
                served[m] = list(served_pins.get(far_net_of[m], []))
        anchor = next(iter(insts)) if len(insts) == 1 else -1
        clusters.append((list(stop.members), served, anchor))
    if fallback:
        clusters.append(
            (
                fallback,
                {m: list(served_pins.get(far_net_of[m], [])) for m in fallback},
                -1,
            )
        )
    return clusters


def _detect_rail_banks(
    ir: PcbIR,
    pins_of: dict[int, list[int]],
    net_pins: dict[int, list[int]],
    served_insts: dict[int, set[int]],
    served_pins: dict[int, list[int]],
    free: list[bool],
    claimed: set[int],
) -> list[SupportGroup]:
    """Resistors sharing one net on one side and going to distinct IC
    pins on the other — a pull-up/pull-down bank on one shared line,
    sub-clustered into per-stop rows by :func:`_bank_stop_clusters`.

    A rail candidate is any net at least two free resistors hang off;
    candidates are taken largest-first so a true rail (many hanging
    resistors) claims its members before a signal net with two resistors
    on it can (on taz15 both 3V3 and SCL "hold" R11/R14/R15 — the 3V3
    bank is the rail, and the largest-first order is what tells them
    apart)."""
    groups: list[SupportGroup] = []
    candidates: list[tuple[int, list[int]]] = []
    for net in range(ir.n_nets):
        hanging = sorted(
            (
                i
                for i in {int(ir.pin_instance[p]) for p in net_pins.get(net, [])}
                if free[i]
                and i not in claimed
                and _is_two_pin_passive(ir, i, pins_of, _RESISTOR_RE)
            ),
            key=lambda i: _refdes_key(ir, i),
        )
        # Only members whose other side reaches a real IC pin count —
        # a resistor between two rails pulls nothing anywhere.
        hanging = [
            i for i in hanging if served_insts.get(_other_pin_net(ir, i, pins_of, net))
        ]
        if len(hanging) >= 2:
            candidates.append((net, hanging))
    candidates.sort(key=lambda c: -len(c[1]))

    for net, hanging in candidates:
        hanging = [i for i in hanging if i not in claimed]
        if len(hanging) < 2:
            continue
        far_net_of = {i: _other_pin_net(ir, i, pins_of, net) for i in hanging}
        served_of = {i: served_insts.get(far_net_of[i], set()) for i in hanging}
        unions = _union_by_served_overlap(hanging, served_of)
        for members in unions.values():
            if len(members) < 2:
                continue
            for cluster, served, anchor_inst in _bank_stop_clusters(
                ir, members, served_pins, far_net_of
            ):
                cluster = sorted(
                    cluster,
                    key=lambda i: (
                        str(ir.net_name[far_net_of[i]]),
                        _refdes_key(ir, i),
                    ),
                )
                for part in _split_chunks(cluster, _MAX_ROW_MEMBERS):
                    if len(part) < 2:
                        continue
                    groups.append(
                        SupportGroup(
                            kind="rail_bank",
                            members=tuple(part),
                            shared_pin=tuple(
                                next(p for p in pins_of[i] if int(ir.pin_net[p]) == net)
                                for i in part
                            ),
                            link_pin=tuple(None for _ in part),
                            served=tuple(
                                tuple(served[i])
                                if served.get(i) is not None
                                else tuple(served_pins.get(far_net_of[i], ()))
                                for i in part
                            ),
                            anchor_inst=anchor_inst if anchor_inst >= 0 else None,
                        )
                    )
                    claimed.update(part)
    return groups


def detect_support_groups(ir: PcbIR) -> list[SupportGroup]:
    """The support structure one netlist carries, as :class:`SupportGroup`
    rows in detection order (series pairs, per-IC cap rows, rail banks —
    earlier kinds claim members first, so no passive lands in two
    groups). Pure detection: nothing is stamped or moved here. Position-
    dependent answers (the ambiguous-cap assignment, a bank's stop
    clustering) read the IR's current poses — call after placement
    exists for the full answer."""
    pins_of = _pins_of_instance(ir)
    net_pins: dict[int, list[int]] = {n: [] for n in range(ir.n_nets)}
    for p in range(ir.n_pins):
        net = int(ir.pin_net[p])
        if net >= 0:
            net_pins[net].append(p)
    free = [_free_member(ir, i) for i in range(ir.n_instances)]
    served_insts, served_pins = _served_sets(ir, pins_of)
    claimed: set[int] = set()

    groups = _detect_series_pairs(ir, pins_of, net_pins, served_pins, free, claimed)
    groups += _detect_support_caps(
        ir, pins_of, net_pins, served_insts, served_pins, free, claimed
    )
    groups += _detect_rail_banks(
        ir, pins_of, net_pins, served_insts, served_pins, free, claimed
    )
    return groups


# ── layout ───────────────────────────────────────────────────────────────


def _pad_world_at(ir: PcbIR, inst: int, pin: int, drot: float) -> tuple[float, float]:
    """Where one pad sits in the GROUP-local frame if the member's own
    rotation is ``drot`` — honours ``inst_bottom``'s mirror, the same
    transform :func:`pin_point` applies."""
    return landpattern.rotate_offset(
        float(ir.pin_dx[pin]),
        float(ir.pin_dy[pin]),
        drot,
        mirrored=bool(ir.inst_bottom[inst]),
    )


def _shared_up_drot(ir: PcbIR, inst: int, pin: int) -> float:
    """Member rotation putting ``pin``'s pad on the +Y (shared) side of
    the row, pad axis perpendicular to the row where the footprint
    allows. Falls back to 0° for a pad geometry no quarter-turn can
    place that way — a rotated-still-wrong answer must never silently
    stand in for a refusal to rotate."""
    for drot in _ROTS:
        rx, ry = _pad_world_at(ir, inst, pin, drot)
        if ry > 0 and abs(ry) >= abs(rx):
            return drot
    return 0.0


def _link_facing_drot(ir: PcbIR, inst: int, pin: int, want_positive_x: bool) -> float:
    """Member rotation facing ``pin``'s pad along +X or -X — a series
    pair's link pads point at each other so the shared net is a stub."""
    for drot in _ROTS:
        rx, ry = _pad_world_at(ir, inst, pin, drot)
        if (rx > 0) == want_positive_x and abs(rx) >= abs(ry):
            return drot
    return 0.0


def _member_served_centroid(
    ir: PcbIR, served: tuple[int, ...]
) -> tuple[float, float] | None:
    pts = [pt for p in served if (pt := pin_point(ir, p)) is not None]
    if not pts:
        return None
    return (
        sum(p[0] for p in pts) / len(pts),
        sum(p[1] for p in pts) / len(pts),
    )


def layout_group(
    ir: PcbIR, group: SupportGroup, radii: np.ndarray
) -> tuple[list[tuple[float, float, float]], float, float, list[float]]:
    """One group's ``(offsets, anchor_x, anchor_y, rots)``.

    The row runs along the group-local X at one pitch — the largest
    member's own keep-out diameter plus :data:`_ROW_GAP_MM`, centred on
    the anchor. The anchor is the centroid of every member's served-pin
    centroid (the pins the row exists beside), pushed a member-radius
    plus :data:`_OWNER_NUDGE_MM` off the owner's own centre for a
    single-owner row. ``rots`` is the four quarter-turns ordered by how
    close each puts the members' served-side pads to their served
    centroids — the caller tries them in order until the legality gate
    accepts one (best first: the difference between a row facing the IC
    and a row facing away from it)."""
    members = group.members
    n = len(members)
    pitch = max(2.0 * float(radii[m]) for m in members) + _ROW_GAP_MM
    dxs = [(i - (n - 1) / 2.0) * pitch for i in range(n)]

    drots: list[float] = []
    for i, m in enumerate(members):
        link, shared = group.link_pin[i], group.shared_pin[i]
        if link is not None:
            drots.append(_link_facing_drot(ir, m, link, want_positive_x=(i == 0)))
        elif shared is not None:
            drots.append(_shared_up_drot(ir, m, shared))
        else:
            drots.append(0.0)

    centroids = [_member_served_centroid(ir, s) if s else None for s in group.served]
    placed = [c for c in centroids if c is not None]
    if placed:
        ax = sum(c[0] for c in placed) / len(placed)
        ay = sum(c[1] for c in placed) / len(placed)
    else:
        xs = [float(ir.inst_x[m]) for m in members]
        ys = [float(ir.inst_y[m]) for m in members]
        ax, ay = sum(xs) / n, sum(ys) / n

    if group.anchor_inst is not None:
        ux, uy = (
            float(ir.inst_x[group.anchor_inst]),
            float(ir.inst_y[group.anchor_inst]),
        )
        if math.isfinite(ux) and math.isfinite(uy):
            dx, dy = ax - ux, ay - uy
            d = math.hypot(dx, dy)
            if d > 1e-6:
                reach = max(float(radii[m]) for m in members) + _OWNER_NUDGE_MM
                ax, ay = ax + dx / d * reach, ay + dy / d * reach

    # The member's served-side pad is the one OPPOSITE its shared (or
    # link) pad: after the drot above it sits on the -Y side of the row.
    served_dy: list[float] = []
    for i, m in enumerate(members):
        member_pins = [p for p in range(ir.n_pins) if int(ir.pin_instance[p]) == m]
        others = [
            p
            for p in member_pins
            if p != group.shared_pin[i] and p != group.link_pin[i]
        ]
        if not others:
            served_dy.append(0.0)
            continue
        rx, ry = _pad_world_at(ir, m, others[0], drots[i])
        served_dy.append(ry)
    rot_cost: list[tuple[float, float]] = []
    for arot in _ROTS:
        cost = 0.0
        for i, m in enumerate(members):
            sc = centroids[i]
            if sc is None:
                continue
            px, py = landpattern.rotate_offset(dxs[i], served_dy[i], arot)
            cost += (ax + px - sc[0]) ** 2 + (ay + py - sc[1]) ** 2
        rot_cost.append((cost, arot))
    rot_cost.sort()
    return (
        [(dxs[i], 0.0, drots[i]) for i in range(n)],
        ax,
        ay,
        [arot for _cost, arot in rot_cost],
    )


def summary_lines(ir: PcbIR, groups: tuple[SupportGroup, ...]) -> tuple[str, ...]:
    """One ``"kind: refdes,refdes,…"`` string per group — the job
    summary's record of what the pass organised, refdes-named so a
    reader can map it onto the board."""
    out = []
    for g in groups:
        refdes = ",".join(str(ir.instance_refdes[m]) for m in g.members)
        out.append(f"{g.kind}: {refdes}")
    return tuple(out)
