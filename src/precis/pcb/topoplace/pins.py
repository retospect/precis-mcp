"""topo-place 1b: pin assignment (Decisions 11–13, 15) and its checks.

``check_slots`` is the solve-time half of Decision 12's pin-conflict
contract: every slot's ``any_of`` loses pins already committed (wired to
another net, or named-claimed), and an empty remainder or a Hall
violation raises :class:`~precis_geom.pcb.PinConflict` naming the
signals and the pins in contention.

``assign_pins`` fills the slots for a FIXED pose: the destination of a
slot is the nearest pin of its net (Decision 11's "nearest other pin"),
a Hungarian assignment on pin→destination distance starts it (a
collinear connector-row case takes a positional sort instead — a
min-length Euclidean matching is crossing-free among its own wires),
and a 2-opt pass exchanges slot pins and permutes swap groups while the
incremental crossing count drops.

The crossing evaluator is the proxy's own math
(:mod:`precis.pcb.topoplace.proxy` helpers, same package — the straddle
predicate and MST order stay literally shared) run on a wire model that
knows about reassignment: ``net_pins`` is frozen on the design, so the
model tracks each decl-referenced net's *effective* pin set — base
members plus slot binds, minus swap-group members permuted away. Nets
no declaration touches keep the proxy's committed wires verbatim;
plane-class nets mint none.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.optimize import linear_sum_assignment
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_bipartite_matching

from precis.pcb.topoplace import proxy as _proxy
from precis.pcb.topoplace.design import TopoDesign
from precis.pcb.topoplace.proxy import ProxyState, all_pin_world
from precis_geom.pcb import PinConflict

#: Infeasible Hungarian pairs (pin not in the slot's set) priced out —
#: after the Hall check passes a feasible matching exists and no BIG
#: entry is ever chosen (Decision 11).
BIG = 1e9


@dataclass(frozen=True)
class PinAssignment:
    """The solver's answer for one pose (spec §1b). ``by_name`` maps each
    declared ``name`` (named slots AND named fixed claims) to
    ``(refdes, pin)`` — the pin as ``int`` when its label is numeric,
    ``str`` otherwise. ``swaps`` records the swap-group moves 2-opt
    applied, each a ``(refdes, pin_a, pin_b)`` net exchange.
    ``cost_vs_fixed`` is the crossing delta against the
    declaration-order assignment (negative = the solver beat
    declaration order). ``choice`` is the bound pin id per
    :attr:`TopoDesign.flex` slot — the write-back needs it for slots
    that carry no ``name`` (spec-call: the spec's three fields plus
    this one). ``crossings`` is the assignment's own effective-net
    crossing count — the number the summary's widen-a-slot edit
    diffs against (1c spec-call: ``net_pins`` is frozen, so a
    widened ``any_of`` is priced by re-solving, not by a placer
    re-run)."""

    by_name: dict[str, tuple[str, int | str]]
    swaps: tuple[tuple[str, int | str, int | str], ...]
    cost_vs_fixed: float
    choice: tuple[int, ...] = ()
    crossings: int = 0


def _slot_tag(d: TopoDesign, i: int) -> str:
    """How a conflict message names a slot: its declared ``name``, else
    ``refdes#i`` so the site is still findable."""
    s = d.flex[i]
    return str(s.name) if s.name else f"{d.parts[s.part]}#{i}"


def _pin_value(label: str) -> int | str:
    """``"4"`` → ``4`` for ``pins.h``/the lock file; ``"PA1"`` stays str."""
    try:
        return int(label)
    except ValueError:
        return label


def _labels(d: TopoDesign, pins: Iterable[int]) -> str:
    """Pin ids as ``[1,4]`` — numeric-aware sorted labels."""

    def key(p: int) -> tuple[int, Any]:
        v = _pin_value(str(d.pin_label[p]))
        return (0, v) if isinstance(v, int) else (1, str(v))

    return "[" + ",".join(str(d.pin_label[p]) for p in sorted(pins, key=key)) + "]"


def _effective(d: TopoDesign) -> list[tuple[int, ...]]:
    """Each slot's candidates minus the pins already committed —
    Decision 12's "fixed pins leave every ``any_of`` set": a pin wired
    to a different net or carrying a named claim is out. Decision 15's
    drive-rating filter drops candidates RATED under the slot's
    ``current=Max`` bound (unrated pins stay — the rule applies only
    where the library has a rating)."""
    claimed = {p for p, _ in d.claims}
    out: list[tuple[int, ...]] = []
    for i, s in enumerate(d.flex):
        net_ir = int(d.decl_net_ir[s.net])
        eff = []
        for k, p in enumerate(s.pins):
            if p in claimed:
                continue
            wired = int(d.pin_net[p])
            if wired >= 0 and wired != net_ir:
                continue
            if s.requirement_a is not None and k < len(s.pin_max_a):
                rating = s.pin_max_a[k]
                if not math.isnan(rating) and rating < s.requirement_a:
                    continue
            eff.append(p)
        if not eff:
            raise PinConflict(
                f"{d.parts[s.part]} slot {_slot_tag(d, i)}: no feasible pin "
                f"of {_labels(d, s.pins)} — every candidate is claimed, "
                "wired to another net, or rated under the requirement"
            )
        out.append(tuple(eff))
    return out


def check_slots(d: TopoDesign) -> None:
    """Solve-time pin conflicts (Decision 12): an empty effective
    ``any_of`` or a Hall deficiency raises :class:`PinConflict` naming
    the signals and pins in contention — "BTN, INT2, INT3 need [1,4]:
    3 signals, 2 pins"."""
    effs = _effective(d)
    if not effs:
        return
    union = sorted({p for eff in effs for p in eff})
    col = {p: c for c, p in enumerate(union)}
    adj = np.zeros((len(effs), len(union)), dtype=np.int8)
    for s_i, eff in enumerate(effs):
        for p in eff:
            adj[s_i, col[p]] = 1
    match = maximum_bipartite_matching(csr_matrix(adj), perm_type="column")
    if int((match >= 0).sum()) >= len(effs):
        return
    # ``match`` is column-indexed: match[c] = the slot bound to pin
    # column c (-1 free). König witness: alternating BFS from the
    # unmatched SLOTS — slot -(non-match edge)-> pin -(match edge)->
    # slot — gives the deficient slot set Sz with N(Sz) = Tz and
    # |Sz| > |Tz|.
    matched_slots = {int(v) for v in match if v >= 0}
    seen_s = {i for i in range(len(effs)) if i not in matched_slots}
    seen_p: set[int] = set()
    frontier = list(seen_s)
    while frontier:
        s_i = frontier.pop()
        for p in effs[s_i]:
            c = col[p]
            if int(match[c]) == s_i or c in seen_p:
                continue
            seen_p.add(c)
            nxt = int(match[c])
            if nxt >= 0 and nxt not in seen_s:
                seen_s.add(nxt)
                frontier.append(nxt)
    names = ", ".join(_slot_tag(d, i) for i in sorted(seen_s))
    raise PinConflict(
        f"{names} need {_labels(d, (union[c] for c in seen_p))}: "
        f"{len(seen_s)} signals, {len(seen_p)} pins"
    )


# ── the assignment wire model ──────────────────────────────────────────

_EMPTY_W = np.empty((0, 4)), np.empty(0, dtype=np.int64)


def _join(blocks: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    """Concatenate ``(wires, labels)`` blocks — ``_proxy._concat`` takes
    per-block labels; these arrive already expanded."""
    blocks = [(w, n) for w, n in blocks if len(w)]
    if not blocks:
        return _EMPTY_W
    return np.concatenate([w for w, _ in blocks]), np.concatenate(
        [n for _, n in blocks]
    )


class _Assign:
    """Effective-net wire model for one pose — the crossings the solver
    actually counts. An ``eff`` id is the routed net's ``net_pins``
    index or a pseudo id ``len(net_pins)+j`` for an unrouted decl net;
    plane decl nets mint no wires. Only nets a declaration can change
    are dynamic — every other routed net's wires are the proxy's
    committed ones verbatim."""

    def __init__(self, d: TopoDesign, pw: np.ndarray) -> None:
        self.d = d
        self.pw = pw
        k = len(d.net_pins)
        self.eff_of_decl = [
            int(d.decl_net_route[j]) if int(d.decl_net_route[j]) >= 0 else k + j
            for j in range(len(d.decl_nets))
        ]
        self.eff_of_ir = {
            int(d.decl_net_ir[j]): e
            for j, e in enumerate(self.eff_of_decl)
            if int(d.decl_net_ir[j]) >= 0
        }
        self.eff_pins: dict[int, set[int]] = {
            e: {int(p) for p in d.decl_nets[j]}
            for j, e in enumerate(self.eff_of_decl)
            if not bool(d.decl_net_plane[j])
        }
        #: movable pin -> eff net it currently carries (None = unwired).
        #: Absent means immovable: wired-on-net slot candidates and pins
        #: on static nets keep their seats forever.
        self.bound: dict[int, int | None] = {}
        self.wires: dict[int, np.ndarray] = {}
        self.static = _EMPTY_W
        self.crossings = 0

    def movable(self, pins: Iterable[int], *, wired_ok: bool = False) -> None:
        """Register pins the solver may re-bind. An UNWIRED pin seats at
        ``None`` until bound. A pin wired to a decl-referenced net is
        movable only with ``wired_ok`` (swap-group members — slot
        candidates wired to their own net keep their seat: the wire
        exists whether the slot names the pin or not)."""
        for p in pins:
            wired = int(self.d.pin_net[p])
            e = self.eff_of_ir.get(wired) if wired >= 0 else None
            if wired >= 0 and (e is None or not wired_ok):
                continue
            self.bound[p] = e
        # reseat: movable pins leave the eff set of nets they do not
        # carry and join the one they do
        for e, pins_of in self.eff_pins.items():
            for p in set(pins_of):
                if p in self.bound and self.bound[p] != e:
                    pins_of.discard(p)
        for p, e in self.bound.items():
            if e is not None and e in self.eff_pins:
                self.eff_pins[e].add(p)

    def seat_static(self, proxy: ProxyState | None) -> None:
        """Committed wires of the routed nets no decl touches — the
        proxy's own rows when it exists (the "incremental proxy"
        contract), rebuilt from the design otherwise."""
        decl_routed = {e for e in self.eff_of_decl if e < len(self.d.net_pins)}
        blocks = []
        for k in range(len(self.d.net_pins)):
            if k in decl_routed:
                continue
            w = (
                proxy._wires[k]
                if proxy is not None
                else _proxy._net_wires(self.d, self.d.net_pins[k], self.pw, k)
            )
            blocks.append((w, np.full(len(w), k, dtype=np.int64)))
        self.static = _join(blocks)

    def _eff_wires(self, e: int, pins: Iterable[int]) -> np.ndarray:
        """``_net_wires`` for eff net ``e`` over ``pins``: a routed eff
        id IS its ``net_pins`` slot — passing it lets the net's
        fixed-via anchors join the MST vertex set (``_net_points``);
        a pseudo id owns no anchors and passes ``None``."""
        return _proxy._net_wires(
            self.d,
            np.asarray(sorted(pins)),
            self.pw,
            e if e < len(self.d.net_pins) else None,
        )

    def rebuild(self) -> None:
        """Refresh ``wires`` from ``eff_pins`` and take the count."""
        self.wires = {e: self._eff_wires(e, p) for e, p in self.eff_pins.items()}
        self.crossings = _proxy._within(*self.flat())

    def flat(self) -> tuple[np.ndarray, np.ndarray]:
        blocks = [self.static]
        blocks += [
            (w, np.full(len(w), e, dtype=np.int64)) for e, w in self.wires.items()
        ]
        return _join(blocks)


def _destinations(d: TopoDesign, pw: np.ndarray, pose: np.ndarray) -> np.ndarray:
    """Per slot the wire's far end: the pin of its net nearest the
    part's centre under ``pose`` (the slot's own candidates excluded —
    they are the near end). ``nan`` when the net is a plane or has no
    wired member — such slots take no destination cost (Decision 11's
    "plane nets excluded")."""
    dest = np.full((len(d.flex), 2), np.nan)
    for i, s in enumerate(d.flex):
        j = s.net
        if bool(d.decl_net_plane[j]):
            continue
        own = set(s.pins)
        others = [int(p) for p in d.decl_nets[j] if int(p) not in own]
        if not others:
            continue
        c = np.asarray([pose[s.part, 0], pose[s.part, 1]])
        dists = np.hypot(pw[others, 0] - c[0], pw[others, 1] - c[1])
        dest[i] = pw[others[int(np.argmin(dists))]]
    return dest


def _cost_rows(
    d: TopoDesign, effs: list[tuple[int, ...]], dest: np.ndarray, pw: np.ndarray
) -> tuple[list[int], np.ndarray]:
    """The (slots × union) cost rows: pin→destination distance, BIG for
    a candidate the slot does not admit, 0 for a pin already on the
    slot's net (binding it changes nothing) and for a plane/empty net
    (any free pin does)."""
    union = sorted({p for eff in effs for p in eff})
    col = {p: c for c, p in enumerate(union)}
    cost = np.full((len(effs), len(union)), BIG)
    for i, s in enumerate(d.flex):
        on_net = {int(p) for p in d.decl_nets[s.net]}
        have_dest = not np.isnan(dest[i]).any()
        for p in effs[i]:
            if p in on_net or not have_dest:
                cost[i, col[p]] = 0.0
            else:
                cost[i, col[p]] = float(np.hypot(*(pw[p] - dest[i])))
    return union, cost


def _line(pts: np.ndarray) -> np.ndarray | None:
    """The points' principal direction when they sit on ONE line (within
    1e-6 mm), else ``None``."""
    if len(pts) < 2:
        return np.asarray([1.0, 0.0])
    c = pts - pts.mean(axis=0)
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    if len(vt) > 1 and float(np.abs(c @ vt[1]).max()) > 1e-6:
        return None
    return vt[0]


def _sorted_assignment(
    d: TopoDesign,
    effs: list[tuple[int, ...]],
    union: list[int],
    dest: np.ndarray,
    pw: np.ndarray,
) -> list[int] | None:
    """The connector-row fast path (spec: "destinations along one
    outline side: sort by projected position, no Hungarian"). Applies
    only when every destination AND every candidate pin sit on one line
    and all slots share the candidate set — rank-order matching is then
    the zero-crossing answer. ``None`` falls back to Hungarian."""
    if not effs or np.isnan(dest).any():
        return None
    if any(set(eff) != set(effs[0]) for eff in effs):
        return None
    direc = _line(dest)
    if direc is None or _line(pw[np.asarray(union)]) is None:
        return None
    order_s = np.argsort(dest @ direc)
    sorted_pins = np.asarray(union)[np.argsort(pw[np.asarray(union)] @ direc)]
    out = [0] * len(effs)
    for rank, s_i in enumerate(order_s):
        out[int(s_i)] = int(sorted_pins[rank])
    return out


def _hungarian(union: list[int], cost: np.ndarray) -> list[int]:
    rows, cols = linear_sum_assignment(cost)
    choice = [0] * cost.shape[0]
    for s_i, c in zip(rows, cols, strict=True):
        choice[int(s_i)] = union[int(c)]
    return choice


def _decl_order(effs: list[tuple[int, ...]], union: list[int]) -> list[int] | None:
    """The no-solver baseline for ``cost_vs_fixed``: slots in
    declaration order take their first still-free candidate. ``None``
    when greedy dead-ends — Hall holds globally but an early greedy
    pick can strand a later slot, so the Hungarian result is the
    fallback baseline."""
    free = set(union)
    out: list[int] = []
    for eff in effs:
        pick = next((p for p in eff if p in free), None)
        if pick is None:
            return None
        free.discard(pick)
        out.append(pick)
    return out


def _model(
    d: TopoDesign,
    pw: np.ndarray,
    effs: list[tuple[int, ...]],
    choice: list[int],
    proxy: ProxyState | None,
) -> _Assign:
    """A wire model with the candidate assignment applied."""
    m = _Assign(d, pw)
    m.movable(p for eff in effs for p in eff)
    m.movable(
        (p for g in d.swap_groups for unit in g.members for p in unit),
        wired_ok=True,
    )
    m.seat_static(proxy)
    for s_i, p in enumerate(choice):
        e = m.eff_of_decl[d.flex[s_i].net]
        if p in m.bound:
            m.bound[p] = e
        if e in m.eff_pins:
            m.eff_pins[e].add(p)
    m.rebuild()
    return m


def _changes(
    m: _Assign, d: TopoDesign, move: tuple[Any, ...]
) -> list[tuple[int, int | None, int | None]]:
    """A move as ``(pin, old_eff, new_eff)`` rewrites. A pin absent from
    ``bound`` is immovable — its ``old`` net keeps it (a wired pin's
    seat is physical, the move can only add the OTHER pin)."""
    out: list[tuple[int, int | None, int | None]] = []

    def ex(a: int, b: int) -> None:
        ea, eb = m.bound.get(a), m.bound.get(b)
        if a in m.bound:
            out.append((a, ea, eb))
        if b in m.bound:
            out.append((b, eb, ea))

    kind = move[0]
    if kind == "slot":  # two slots exchange bound pins
        _, i, j, pi, pj = move
        ex(pi, pj)
    elif kind == "rebind":  # slot i takes the free pin new_p
        _, i, old_p, new_p = move
        if old_p in m.bound:
            out.append((old_p, m.bound[old_p], None))
        if new_p in m.bound:
            out.append(
                (
                    new_p,
                    None,
                    m.bound[old_p]
                    if old_p in m.bound
                    else m.eff_of_decl[d.flex[i].net],
                )
            )
    elif kind == "member":  # two group members exchange nets
        _, a, b = move
        ex(a, b)
    elif kind == "unit":  # tuple-group unit exchange, position-wise
        _, g, i, j = move
        for a, b in zip(g.members[i], g.members[j], strict=True):
            ex(a, b)
    return out


def _delta(m: _Assign, changes: list[tuple[int, int | None, int | None]]) -> float:
    """The move's crossing delta via the proxy's affected-nets pattern:
    rebuild only the touched nets' wires, re-score them against the
    untouched rest. Nothing is mutated."""
    affected = {
        e
        for _, old, new in changes
        for e in (old, new)
        if e is not None and e in m.eff_pins
    }
    if not affected:
        return 0.0
    new_sets = {e: set(m.eff_pins[e]) for e in affected}
    for p, old, new in changes:
        if old in new_sets:
            new_sets[old].discard(p)
        if new in new_sets:
            new_sets[new].add(p)
    new_wires = {e: m._eff_wires(e, new_sets[e]) for e in affected}
    rest = [m.static] + [
        (m.wires[e], np.full(len(m.wires[e]), e, dtype=np.int64))
        for e in m.wires
        if e not in affected
    ]
    rw, rn = _join(rest)
    ow, on = _join(
        [(m.wires[e], np.full(len(m.wires[e]), e, dtype=np.int64)) for e in affected]
    )
    nw, nn = _join(
        [
            (new_wires[e], np.full(len(new_wires[e]), e, dtype=np.int64))
            for e in affected
        ]
    )
    return float(
        _proxy._cross_ab(nw, rw, nn, rn)
        + _proxy._within(nw, nn)
        - _proxy._cross_ab(ow, rw, on, rn)
        - _proxy._within(ow, on)
    )


def _apply(m: _Assign, changes: list[tuple[int, int | None, int | None]]) -> set[int]:
    affected: set[int] = set()
    for p, old, new in changes:
        if old is not None and old in m.eff_pins:
            m.eff_pins[old].discard(p)
            affected.add(old)
        if new is not None and new in m.eff_pins:
            m.eff_pins[new].add(p)
            affected.add(new)
        m.bound[p] = new
    for e in affected:
        m.wires[e] = m._eff_wires(e, m.eff_pins[e])
    return affected


def assign_pins(
    d: TopoDesign, pose: np.ndarray, proxy: ProxyState | None = None
) -> PinAssignment:
    """Fill the design's slots for one pose and report the swaps.

    :func:`check_slots` runs first. Then: destinations from the pose →
    the collinear sort or Hungarian on pin→destination distance → 2-opt
    over slot exchanges, free-pin rebinds and swap-group permutations
    while crossings drop. ``proxy`` supplies its committed wires for
    the static nets when given (built on the same pose); the eval does
    not need it to exist.
    """
    check_slots(d)
    effs = _effective(d)
    pose = np.asarray(pose, dtype=np.float64)
    pw = (
        proxy._pw
        if proxy is not None and np.array_equal(proxy.pose, pose)
        else all_pin_world(d, pose)
    )
    union: list[int] = []
    if effs:
        dest = _destinations(d, pw, pose)
        union, cost = _cost_rows(d, effs, dest, pw)
        choice = _sorted_assignment(d, effs, union, dest, pw) or _hungarian(union, cost)
    else:
        choice = []

    base = _decl_order(effs, union) if effs else []
    base_m = _model(d, pw, effs, list(base) if base is not None else choice, proxy)
    m = _model(d, pw, effs, choice, proxy)

    swaps: list[tuple[str, int | str, int | str]] = []
    _two_opt(d, m, effs, choice, swaps)

    by_name: dict[str, tuple[str, int | str]] = {
        str(s.name): (d.parts[s.part], _pin_value(str(d.pin_label[choice[i]])))
        for i, s in enumerate(d.flex)
        if s.name is not None
    }
    for p, name in d.claims:
        by_name.setdefault(
            name, (d.parts[int(d.pin_part[p])], _pin_value(str(d.pin_label[p])))
        )
    return PinAssignment(
        by_name=by_name,
        swaps=tuple(swaps),
        cost_vs_fixed=float(m.crossings - base_m.crossings),
        choice=tuple(choice),
        crossings=m.crossings,
    )


def _two_opt(
    d: TopoDesign,
    m: _Assign,
    effs: list[tuple[int, ...]],
    choice: list[int],
    swaps: list[tuple[str, int | str, int | str]],
) -> None:
    """First-improvement hill climb: slot pin exchanges, free-pin
    rebinds, plain member exchanges and tuple-group unit exchanges —
    any move that drops the crossing count, until none does (spec:
    "stop at no improving swap"). Fixed scan order = one answer per
    pose."""
    eff_sets = [set(e) for e in effs]
    while True:
        taken = set(choice)
        moves: list[tuple[Any, ...]] = []
        for i in range(len(effs)):
            for j in range(i + 1, len(effs)):
                if d.flex[i].net == d.flex[j].net:
                    continue
                pi, pj = choice[i], choice[j]
                if pj in eff_sets[i] and pi in eff_sets[j]:
                    moves.append(("slot", i, j, pi, pj))
            for p in effs[i]:
                if p != choice[i] and p not in taken:
                    moves.append(("rebind", i, choice[i], p))
        for g in d.swap_groups:
            units = g.members
            for i in range(len(units)):
                for j in range(i + 1, len(units)):
                    if g.tuple_:
                        moves.append(("unit", g, i, j))
                    else:
                        a, b = int(units[i][0]), int(units[j][0])
                        if m.bound.get(a) != m.bound.get(b):
                            moves.append(("member", a, b))
        for move in moves:
            changes = _changes(m, d, move)
            delta = _delta(m, changes)
            if delta < -1e-9:
                _apply(m, changes)
                m.crossings = round(m.crossings + delta)
                if move[0] == "slot":
                    _, i, j, pi, pj = move
                    choice[i], choice[j] = pj, pi
                elif move[0] == "rebind":
                    _, i, _, new_p = move
                    choice[i] = new_p
                elif move[0] == "member":
                    _, a, b = move
                    refdes = d.parts[int(d.pin_part[a])]
                    swaps.append(
                        (
                            refdes,
                            _pin_value(str(d.pin_label[a])),
                            _pin_value(str(d.pin_label[b])),
                        )
                    )
                elif move[0] == "unit":
                    _, g, i, j = move
                    for a, b in zip(g.members[i], g.members[j], strict=True):
                        swaps.append(
                            (
                                d.parts[g.part],
                                _pin_value(str(d.pin_label[a])),
                                _pin_value(str(d.pin_label[b])),
                            )
                        )
                break
        else:
            m.rebuild()  # exact count, not the accumulated delta
            return


__all__ = ["BIG", "PinAssignment", "assign_pins", "check_slots"]
