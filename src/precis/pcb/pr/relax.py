"""4c batched geometry relaxation (spec §Method 4c): K sampled boards
relaxed as one tensor computation. Energy = length (1/mm) + via beads
(``via_cost_mm`` each) + a one-sided quadratic barrier on the gap below
0, stiffness ramped with the linear width growth 0 → full. A swept
collision check (orientation-sign flip on same-layer foreign segment
pairs, and on a moving part's pads against same-layer wires) rejects or
halves any step that would create a crossing, so the 4a topology holds.

Slice 2 adds the free parts (spec §Where parts move, 4c): a part's pose
``(x, y, θ)`` is a variable beside the wire points. Wire endpoints are
DERIVED from the pose (pin = centre + R_cw(θ)·local), the part's pads are
obstacles that ride the pose (:class:`~precis.pcb.pr.state.DynGeom`),
courtyards keep parts apart (an oriented-box separating-axis gap, which
is exact when boxes are separated along an axis and conservative at a
corner-corner approach), the outline holds the courtyard in, and a
rotation well priced by :mod:`precis.structsolve.preferred` over
``field.ROT_TIERS_DEG`` (90° free, 45° paid — Reto Q7) keeps θ near a
preferred angle. Pin pulls and torques are the gradient of the energy
through the pose transform, by autograd (spec 4c).

Blocks (spec §Hierarchy) are a parametrisation: variables are GROUP
poses ``(K, G, 3)``; a part's pose is its group's pose composed with a
fixed in-group offset. At the fine level every part is its own group and
the offsets are zero.

Copper appetite (spec §Net attributes) is a per-wire width increment
above the current floor, charged ``α_net × area``; off at
``appetite_scale = 0`` (the bench default — its weight against signal
detours is the spec's bench-tuned open knob).

Torch is imported lazily — the host venv has none; the container gate
does. ``Relaxer`` builds its index tables once per layout; pair
candidates are pruned by a reach radius since pieces and parts move
little inside a swept-checked topology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.pcb.pr.beliefs import Layout
from precis.pcb.pr.state import PrProblem


@dataclass(frozen=True, slots=True)
class RelaxParams:
    """Provisional parameters (spec/brief: fix and report)."""

    via_cost_mm: float = 1.0  # = vroute.Request.via_cost_mm
    stiff_min: float = 20.0  # barrier stiffness at zero width
    stiff_max: float = 400.0  # ... ramped linearly to full width
    steps: int = 150  # per call
    eta0: float = 0.05  # initial step size (mm per unit grad, normalized)
    eta_grow: float = 1.05
    max_disp_mm: float = 0.2  # per-point displacement cap per step
    sweep_halvings: int = 5
    prune_mm: float = 4.0  # static candidate radius for barrier pairs
    bead_prune_mm: float = 4.0
    grow_steps: int = 60  # width growth schedule length (of `steps`)
    # ── slice 2: parts ──
    part_eta0: float = 0.02  # pose step per unit gradient (mm)
    max_part_disp_mm: float = 0.1  # per-step translation cap
    max_part_rot_rad: float = 0.0087  # per-step rotation cap (0.5°)
    part_prune_mm: float = 3.0  # extra reach for pairs touching a movable part
    rot_weight: float = 1.0  # λ on the preferred-tier rotation well
    appetite_scale: float = 0.0  # 0 = widths frozen at their floor
    max_widen_mm: float = 1.0  # cap on appetite widening above the floor
    #: Extra margin inside the barrier so a relaxed board does not sit
    #: exactly on the rule limit (finish-time point dropping and the exact
    #: kernel's quantisation both need slack).
    margin_mm: float = 0.01
    #: Same for courtyards: the one-sided barrier settles a hair inside
    #: the limit under a steady pull; this keeps that hair off the DRC.
    court_margin_mm: float = 0.02


@dataclass(slots=True)
class RelaxStats:
    evals: int = 0
    blocked: int = 0  # accepted-step rejections that needed ≥1 halving
    dropped: int = 0  # steps dropped entirely after max halvings
    steps: int = 0
    seconds: float = 0.0


def _bbox_pairs(
    lo_a: np.ndarray,
    hi_a: np.ndarray,
    lo_b: np.ndarray,
    hi_b: np.ndarray,
    *,
    same: bool,
    skip: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Index pairs ``(i, j)`` whose padded boxes overlap. ``same`` means
    ``lo_a is lo_b`` (upper triangle only). ``skip`` is an optional (A, B)
    bool mask of pairs to leave out."""
    out_i: list[np.ndarray] = []
    out_j: list[np.ndarray] = []
    for i in range(len(lo_a)):
        j0 = i + 1 if same else 0
        if j0 >= len(lo_b):
            continue
        hit = (
            (lo_b[j0:, 0] <= hi_a[i, 0])
            & (hi_b[j0:, 0] >= lo_a[i, 0])
            & (lo_b[j0:, 1] <= hi_a[i, 1])
            & (hi_b[j0:, 1] >= lo_a[i, 1])
        )
        if skip is not None:
            hit &= ~skip[i, j0:]
        js = np.flatnonzero(hit) + j0
        if len(js):
            out_i.append(np.full(len(js), i, dtype=np.int64))
            out_j.append(js.astype(np.int64))
    if not out_i:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    return np.concatenate(out_i), np.concatenate(out_j)


@dataclass
class Relaxer:
    """Precomputed index tables + the step loop for one layout."""

    problem: PrProblem
    layout: Layout
    params: RelaxParams = field(default_factory=RelaxParams)
    #: True: parts move as their blocks (``DynGeom.group`` — the coarse
    #: hierarchy level); False: every part is its own group (fine level).
    blocks: bool = False

    def __post_init__(self) -> None:
        import torch

        prob, lay, prm = self.problem, self.layout, self.params
        dyn = prob.dyn
        t64 = torch.float64
        self.t64 = t64
        self._collect: list[Any] | None = None
        self.p0 = torch.tensor(lay.piece_p0)
        self.p1 = torch.tensor(lay.piece_p1)
        self.free = torch.tensor(lay.free)
        self.piece_wire = torch.tensor(lay.piece_wire)
        n_pts = len(lay.points)
        self.n_pts = n_pts
        pw = lay.piece_wire
        n_wires = len(prob.wires)
        # piece width (per wire) and per-pair required spacing.
        w_of_wire = np.asarray([w.width_mm for w in prob.wires])
        self.wire_w = torch.tensor(w_of_wire, dtype=t64)
        self.piece_w = torch.tensor(w_of_wire[pw], dtype=t64)
        wire_net = np.asarray([w.net_id for w in prob.wires], dtype=np.int64)
        self.wire_net = wire_net
        req = np.zeros((n_wires, n_wires, prob.n_layers))
        for i in range(n_wires):
            for j in range(n_wires):
                for l in range(prob.n_layers):
                    req[i, j, l] = prob.required_mm(wire_net[i], wire_net[j], l)
        self.req_wires = torch.tensor(req, dtype=t64)  # indexed by WIRE idx
        margin = prm.margin_mm

        # ── parts: anchors, groups, offsets ──
        self.n_parts = dyn.n_parts
        pose0 = dyn.pose0 if self.n_parts else np.zeros((0, 3))
        if self.blocks and self.n_parts:
            group = dyn.group
        else:
            group = np.arange(self.n_parts, dtype=np.int64)
        self.n_groups = int(group.max()) + 1 if self.n_parts else 0
        gpose0 = np.zeros((self.n_groups, 3))
        off_d = np.zeros((self.n_parts, 2))
        off_th = np.zeros(self.n_parts)
        glock_xy = np.zeros(self.n_groups, dtype=bool)
        glock_rot = np.zeros(self.n_groups, dtype=bool)
        for g in range(self.n_groups):
            members = np.flatnonzero(group == g)
            if len(members) == 1:
                gpose0[g] = pose0[members[0]]
            else:
                gpose0[g, :2] = pose0[members, :2].mean(0)
                gpose0[g, 2] = 0.0
            glock_xy[g] = bool(dyn.lock_xy[members].any())
            glock_rot[g] = bool(dyn.lock_rot[members].any())
            c, s = np.cos(gpose0[g, 2]), np.sin(gpose0[g, 2])
            for m in members:
                dx, dy = pose0[m, 0] - gpose0[g, 0], pose0[m, 1] - gpose0[g, 1]
                # inverse of w = c_g + R_cw(θ_g)·d
                off_d[m] = (dx * c - dy * s, dx * s + dy * c)
                off_th[m] = pose0[m, 2] - gpose0[g, 2]
        self.gpose0 = torch.tensor(gpose0, dtype=t64)
        self.part_group = torch.tensor(group, dtype=torch.int64)
        self.off_d = torch.tensor(off_d, dtype=t64)
        self.off_th = torch.tensor(off_th, dtype=t64)
        self.glock_xy = torch.tensor(glock_xy)
        self.glock_rot = torch.tensor(glock_rot)
        self.part_rot_free = torch.tensor(
            ~dyn.lock_rot if self.n_parts else np.zeros(0, dtype=bool)
        )
        # wire endpoints that ride a part
        first_pt: dict[int, int] = {}
        last_pt: dict[int, int] = {}
        for i, w in enumerate(lay.wire_of_point):
            first_pt.setdefault(int(w), i)
            last_pt[int(w)] = i
        ep_pt: list[int] = []
        ep_part: list[int] = []
        ep_local: list[tuple[float, float]] = []
        for wi, ws in enumerate(prob.wires):
            if wi not in first_pt:
                continue
            if len(lay.wire_active) == len(prob.wires) and not lay.wire_active[wi]:
                continue  # an unrouted stub stays put, anchored to nothing
            if ws.a_part >= 0:
                ep_pt.append(first_pt[wi])
                ep_part.append(ws.a_part)
                ep_local.append(ws.a_local)
            if ws.b_part >= 0:
                ep_pt.append(last_pt[wi])
                ep_part.append(ws.b_part)
                ep_local.append(ws.b_local)
        self.ep_pt = torch.tensor(ep_pt, dtype=torch.int64)
        self.ep_part = torch.tensor(ep_part, dtype=torch.int64)
        self.ep_local = torch.tensor(
            np.asarray(ep_local, dtype=np.float64).reshape(-1, 2), dtype=t64
        )
        # initial world geometry of dynamic rows (for pair pruning)
        ppose0 = pose0
        dseg_w0 = np.zeros((len(dyn.dseg_part), 4))
        for k in range(len(dyn.dseg_part)):
            cx, cy, th = ppose0[dyn.dseg_part[k]]
            c, s = np.cos(th), np.sin(th)
            lx0, ly0, lx1, ly1 = dyn.dseg_local[k]
            dseg_w0[k] = (
                cx + lx0 * c + ly0 * s,
                cy - lx0 * s + ly0 * c,
                cx + lx1 * c + ly1 * s,
                cy - lx1 * s + ly1 * c,
            )
        dbox_w0 = np.zeros((len(dyn.dbox_part), 2))
        dbox_rad = np.zeros(len(dyn.dbox_part))
        for k in range(len(dyn.dbox_part)):
            cx, cy, th = ppose0[dyn.dbox_part[k]]
            c, s = np.cos(th), np.sin(th)
            lx, ly = dyn.dbox_c[k]
            dbox_w0[k] = (cx + lx * c + ly * s, cy - lx * s + ly * c)
            dbox_rad[k] = float(np.hypot(*dyn.dbox_hw[k]))

        # ── candidate pair tables (static, pruned by reach) ──
        a = lay.points[lay.piece_p0]
        b = lay.points[lay.piece_p1]
        reach = prm.prune_mm + (prm.part_prune_mm if self.n_parts else 0.0)
        lo = np.minimum(a, b) - reach
        hi = np.maximum(a, b) + reach
        # an unrouted wire's stub pieces join no pair at all: NaN boxes
        # never overlap anything
        act = (
            lay.wire_active[pw]
            if len(lay.wire_active) == len(prob.wires)
            else np.ones(len(pw), dtype=bool)
        )
        lo[~act] = np.nan
        hi[~act] = np.nan
        self.piece_act = torch.tensor(act.astype(np.float64), dtype=t64)
        same_wire = pw[:, None] == pw[None, :]
        pp_i, pp_j = _bbox_pairs(lo, hi, lo, hi, same=True, skip=same_wire)
        self.pp_i = torch.tensor(pp_i)
        self.pp_j = torch.tensor(pp_j)
        # piece × static obstacle segment rows
        obs = prob.obs
        if len(obs.seg_xy):
            olo = np.minimum(obs.seg_xy[:, :2], obs.seg_xy[:, 2:]) - obs.seg_r[:, None]
            ohi = np.maximum(obs.seg_xy[:, :2], obs.seg_xy[:, 2:]) + obs.seg_r[:, None]
            skip = wire_net[pw][:, None] == obs.seg_net[None, :]
            po_i, po_j = _bbox_pairs(lo, hi, olo, ohi, same=False, skip=skip)
        else:
            po_i = po_j = np.zeros(0, dtype=np.int64)
        self.po_i = torch.tensor(po_i)
        self.po_j = torch.tensor(po_j)
        self.obs_seg = torch.tensor(
            obs.seg_xy if len(obs.seg_xy) else np.zeros((0, 4)), dtype=t64
        )
        self.obs_seg_r = torch.tensor(obs.seg_r, dtype=t64)
        self.obs_seg_lmask = torch.tensor(obs.seg_lmask.astype(np.int64))
        self.po_req = torch.tensor(
            self._req_rows(wire_net[pw[po_i]], obs.seg_net[po_j]) + margin, dtype=t64
        )
        # piece × static obstacle box rows
        if len(obs.box_c):
            brad = np.hypot(obs.box_hw[:, 0], obs.box_hw[:, 1])[:, None]
            blo = obs.box_c - brad
            bhi = obs.box_c + brad
            skip = wire_net[pw][:, None] == obs.box_net[None, :]
            pb_i, pb_j = _bbox_pairs(lo, hi, blo, bhi, same=False, skip=skip)
        else:
            pb_i = pb_j = np.zeros(0, dtype=np.int64)
        self.pb_i = torch.tensor(pb_i)
        self.pb_j = torch.tensor(pb_j)
        self.obs_box_c = torch.tensor(
            obs.box_c if len(obs.box_c) else np.zeros((0, 2)), dtype=t64
        )
        self.obs_box_hw = torch.tensor(
            obs.box_hw if len(obs.box_hw) else np.zeros((0, 2)), dtype=t64
        )
        self.obs_box_cs = torch.tensor(
            obs.box_cs if len(obs.box_cs) else np.zeros((0, 2)), dtype=t64
        )
        self.obs_box_lmask = torch.tensor(obs.box_lmask.astype(np.int64))
        self.pb_req = torch.tensor(
            self._req_rows(wire_net[pw[pb_i]], obs.box_net[pb_j]) + margin, dtype=t64
        )
        # piece × DYNAMIC pad rows (segments and boxes of movable parts)
        if len(dyn.dseg_part):
            dlo = np.minimum(dseg_w0[:, :2], dseg_w0[:, 2:]) - dyn.dseg_r[:, None]
            dhi = np.maximum(dseg_w0[:, :2], dseg_w0[:, 2:]) + dyn.dseg_r[:, None]
            skip = wire_net[pw][:, None] == dyn.dseg_net[None, :]
            pd_i, pd_j = _bbox_pairs(lo, hi, dlo, dhi, same=False, skip=skip)
        else:
            pd_i = pd_j = np.zeros(0, dtype=np.int64)
        self.pd_i = torch.tensor(pd_i)
        self.pd_j = torch.tensor(pd_j)
        self.dseg_part = torch.tensor(dyn.dseg_part, dtype=torch.int64)
        self.dseg_local = torch.tensor(dyn.dseg_local.reshape(-1, 4), dtype=t64)
        self.dseg_r = torch.tensor(dyn.dseg_r, dtype=t64)
        self.dseg_lmask = torch.tensor(dyn.dseg_lmask.astype(np.int64))
        self.pd_req = torch.tensor(
            self._req_rows(wire_net[pw[pd_i]], dyn.dseg_net[pd_j]) + margin, dtype=t64
        )
        if len(dyn.dbox_part):
            Blo = dbox_w0 - dbox_rad[:, None]
            Bhi = dbox_w0 + dbox_rad[:, None]
            skip = wire_net[pw][:, None] == dyn.dbox_net[None, :]
            pB_i, pB_j = _bbox_pairs(lo, hi, Blo, Bhi, same=False, skip=skip)
        else:
            pB_i = pB_j = np.zeros(0, dtype=np.int64)
        self.pB_i = torch.tensor(pB_i)
        self.pB_j = torch.tensor(pB_j)
        self.dbox_part = torch.tensor(dyn.dbox_part, dtype=torch.int64)
        self.dbox_c = torch.tensor(dyn.dbox_c.reshape(-1, 2), dtype=t64)
        self.dbox_hw = torch.tensor(dyn.dbox_hw.reshape(-1, 2), dtype=t64)
        self.dbox_phi = torch.tensor(dyn.dbox_phi, dtype=t64)
        self.dbox_lmask = torch.tensor(dyn.dbox_lmask.astype(np.int64))
        self.pB_req = torch.tensor(
            self._req_rows(wire_net[pw[pB_i]], dyn.dbox_net[pB_j]) + margin, dtype=t64
        )
        # swept-check rows for moving pads: the dyn segments plus each dyn
        # box's long axis (local), paired with pieces by reach
        sw_local = [tuple(r) for r in dyn.dseg_local.reshape(-1, 4)]
        sw_part: list[int] = [int(x) for x in dyn.dseg_part]
        sw_net: list[int] = [int(x) for x in dyn.dseg_net]
        sw_lmask: list[int] = [int(x) for x in dyn.dseg_lmask]
        sw_w0 = [tuple(r) for r in dseg_w0]
        for k in range(len(dyn.dbox_part)):
            hw = dyn.dbox_hw[k]
            phi = dyn.dbox_phi[k]
            axis = np.array([np.cos(phi), np.sin(phi)]) * (
                hw[0] if hw[0] >= hw[1] else 0.0
            )
            if hw[1] > hw[0]:
                axis = np.array([-np.sin(phi), np.cos(phi)]) * hw[1]
            c0 = dyn.dbox_c[k] - axis
            c1 = dyn.dbox_c[k] + axis
            sw_local.append((c0[0], c0[1], c1[0], c1[1]))
            sw_part.append(int(dyn.dbox_part[k]))
            sw_net.append(int(dyn.dbox_net[k]))
            sw_lmask.append(int(dyn.dbox_lmask[k]))
            cx, cy, th = ppose0[dyn.dbox_part[k]]
            c, s = np.cos(th), np.sin(th)
            sw_w0.append(
                (
                    cx + c0[0] * c + c0[1] * s,
                    cy - c0[0] * s + c0[1] * c,
                    cx + c1[0] * c + c1[1] * s,
                    cy - c1[0] * s + c1[1] * c,
                )
            )
        sw_w0a = np.asarray(sw_w0, dtype=np.float64).reshape(-1, 4)
        if len(sw_part):
            slo = np.minimum(sw_w0a[:, :2], sw_w0a[:, 2:]) - 0.5
            shi = np.maximum(sw_w0a[:, :2], sw_w0a[:, 2:]) + 0.5
            skip = wire_net[pw][:, None] == np.asarray(sw_net)[None, :]
            ps_i, ps_j = _bbox_pairs(lo, hi, slo, shi, same=False, skip=skip)
        else:
            ps_i = ps_j = np.zeros(0, dtype=np.int64)
        self.ps_i = torch.tensor(ps_i)
        self.ps_j = torch.tensor(ps_j)
        self.sw_part = torch.tensor(np.asarray(sw_part, dtype=np.int64))
        self.sw_local = torch.tensor(
            np.asarray(sw_local, dtype=np.float64).reshape(-1, 4), dtype=t64
        )
        self.sw_lmask = torch.tensor(np.asarray(sw_lmask, dtype=np.int64))

        # ── courtyards ──
        self.court_c = torch.tensor(dyn.court_c.reshape(-1, 2), dtype=t64)
        self.court_hw = torch.tensor(dyn.court_hw.reshape(-1, 2), dtype=t64)
        side = dyn.side if self.n_parts else np.zeros(0, dtype=np.int8)
        cc_i: list[int] = []
        cc_j: list[int] = []
        if self.n_parts:
            cw0 = np.zeros((self.n_parts, 2))
            crad = np.hypot(dyn.court_hw[:, 0], dyn.court_hw[:, 1])
            for mi in range(self.n_parts):
                cx, cy, th = ppose0[mi]
                c, s = np.cos(th), np.sin(th)
                lx, ly = dyn.court_c[mi]
                cw0[mi] = (cx + lx * c + ly * s, cy - lx * s + ly * c)
            reach_c = prm.part_prune_mm * 2.0
            for i in range(self.n_parts):
                for j in range(i + 1, self.n_parts):
                    if side[i] != side[j] and side[i] != 2 and side[j] != 2:
                        continue
                    if group[i] == group[j] and self.n_groups < self.n_parts:
                        continue  # rigid inside a block
                    if np.hypot(*(cw0[i] - cw0[j])) <= crad[i] + crad[j] + reach_c:
                        cc_i.append(i)
                        cc_j.append(j)
        self.cc_i = torch.tensor(cc_i, dtype=torch.int64)
        self.cc_j = torch.tensor(cc_j, dtype=torch.int64)
        cs_i: list[int] = []
        cs_j: list[int] = []
        if self.n_parts and len(dyn.scourt_c):
            srad = np.hypot(dyn.scourt_hw[:, 0], dyn.scourt_hw[:, 1])
            for i in range(self.n_parts):
                for j in range(len(dyn.scourt_c)):
                    sj = int(dyn.scourt_side[j])
                    if side[i] != sj and side[i] != 2 and sj != 2:
                        continue
                    if (
                        np.hypot(*(cw0[i] - dyn.scourt_c[j]))
                        <= crad[i] + srad[j] + 2.0 * prm.part_prune_mm
                    ):
                        cs_i.append(i)
                        cs_j.append(j)
        self.cs_i = torch.tensor(cs_i, dtype=torch.int64)
        self.cs_j = torch.tensor(cs_j, dtype=torch.int64)
        self.scourt_c = torch.tensor(dyn.scourt_c.reshape(-1, 2), dtype=t64)
        self.scourt_hw = torch.tensor(dyn.scourt_hw.reshape(-1, 2), dtype=t64)
        self.scourt_cs = torch.tensor(dyn.scourt_cs.reshape(-1, 2), dtype=t64)

        # ── via sites: interior point p of wire w with piece p0→p / p→p1 ──
        sites_pt: list[int] = []
        sites_left: list[int] = []
        sites_right: list[int] = []
        sites_wire: list[int] = []
        end_at: dict[int, int] = {}
        start_at: dict[int, int] = {}
        for pi, (u, v) in enumerate(zip(lay.piece_p0, lay.piece_p1, strict=True)):
            start_at[int(u)] = pi
            end_at[int(v)] = pi
        for pt_i, ok in enumerate(lay.via_site_ok):
            if not ok:
                continue
            li = end_at.get(pt_i)
            ri = start_at.get(pt_i)
            if li is None or ri is None:
                continue
            sites_pt.append(pt_i)
            sites_left.append(li)
            sites_right.append(ri)
            sites_wire.append(int(lay.piece_wire[li]))
        self.site_pt = torch.tensor(sites_pt, dtype=torch.int64)
        self.site_left = torch.tensor(sites_left, dtype=torch.int64)
        self.site_right = torch.tensor(sites_right, dtype=torch.int64)
        self.site_wire = torch.tensor(sites_wire, dtype=torch.int64)
        sw_arr = np.asarray(sites_wire, dtype=np.int64)
        # bead × piece pairs (different wire), bead × static obstacles,
        # bead × dynamic pad rows
        if sites_pt:
            sp = lay.points[np.asarray(sites_pt)]
            slo = sp - prm.bead_prune_mm
            shi = sp + prm.bead_prune_mm
            skip = sw_arr[:, None] == pw[None, :]
            bp_s, bp_j = _bbox_pairs(slo, shi, lo, hi, same=False, skip=skip)
            if len(obs.seg_xy):
                bos_s, bos_j = _bbox_pairs(slo, shi, olo, ohi, same=False)
            else:
                bos_s = bos_j = np.zeros(0, dtype=np.int64)
            if len(obs.box_c):
                bob_s, bob_j = _bbox_pairs(slo, shi, blo, bhi, same=False)
            else:
                bob_s = bob_j = np.zeros(0, dtype=np.int64)
            if len(dyn.dseg_part):
                bd_s, bd_j = _bbox_pairs(slo, shi, dlo, dhi, same=False)
            else:
                bd_s = bd_j = np.zeros(0, dtype=np.int64)
            if len(dyn.dbox_part):
                bB_s, bB_j = _bbox_pairs(slo, shi, Blo, Bhi, same=False)
            else:
                bB_s = bB_j = np.zeros(0, dtype=np.int64)
        else:
            z = np.zeros(0, dtype=np.int64)
            bp_s = bp_j = bos_s = bos_j = bob_s = bob_j = bd_s = bd_j = bB_s = bB_j = z
        self.bp_s, self.bp_j = torch.tensor(bp_s), torch.tensor(bp_j)
        self.bos_s, self.bos_j = torch.tensor(bos_s), torch.tensor(bos_j)
        self.bob_s, self.bob_j = torch.tensor(bob_s), torch.tensor(bob_j)
        self.bd_s, self.bd_j = torch.tensor(bd_s), torch.tensor(bd_j)
        self.bB_s, self.bB_j = torch.tensor(bB_s), torch.tensor(bB_j)
        # A via keeps off every pad and hole whatever its net (no via-in-
        # pad, the hole-wall rule): the pair spacing floors at the house
        # clearance even for the via's own net.
        keep = prob.clearance_mm + margin
        self.bos_req = torch.tensor(
            np.maximum(
                self._req_rows(wire_net[sw_arr[bos_s]], obs.seg_net[bos_j]) + margin,
                keep,
            ),
            dtype=t64,
        )
        self.bob_req = torch.tensor(
            np.maximum(
                self._req_rows(wire_net[sw_arr[bob_s]], obs.box_net[bob_j]) + margin,
                keep,
            ),
            dtype=t64,
        )
        self.bd_req = torch.tensor(
            np.maximum(
                self._req_rows(wire_net[sw_arr[bd_s]], dyn.dseg_net[bd_j]) + margin,
                keep,
            ),
            dtype=t64,
        )
        self.bB_req = torch.tensor(
            np.maximum(
                self._req_rows(wire_net[sw_arr[bB_s]], dyn.dbox_net[bB_j]) + margin,
                keep,
            ),
            dtype=t64,
        )
        # bead × bead (any wires, same net too: hole-wall spacing)
        if sites_pt:
            bb_s, bb_t = _bbox_pairs(slo, shi, slo, shi, same=True)
        else:
            bb_s = bb_t = np.zeros(0, dtype=np.int64)
        self.bb_s, self.bb_t = torch.tensor(bb_s), torch.tensor(bb_t)
        self.bb_req = torch.tensor(
            np.maximum(
                self._req_rows(wire_net[sw_arr[bb_s]], wire_net[sw_arr[bb_t]]) + margin,
                keep,
            ),
            dtype=t64,
        )
        self.outline = torch.tensor(prob.outline, dtype=t64)
        self.via_r = prob.via_dia_mm / 2.0
        # transition cost per piece: via price where the boundary vertex
        # admits a via, else steep (a layer change needs a legal site)
        trans = np.full(len(lay.piece_p0), np.inf)
        for pi in range(len(lay.piece_p0)):
            pt = int(lay.piece_p1[pi])
            if pt in start_at and lay.via_site_ok[pt]:
                trans[pi] = self.params.via_cost_mm
        self.trans_cost = trans  # np; beliefs uses it
        # appetite per wire (α of its net)
        app = (
            np.asarray(
                [prob.net_attrs.appetite[w.net_id] for w in prob.wires],
                dtype=np.float64,
            )
            if n_wires
            else np.zeros(0)
        )
        self.wire_appetite = torch.tensor(app, dtype=t64)
        self.rot_coeffs = None
        if self.n_parts and prm.rot_weight > 0.0:
            from precis.pcb.field import ROT_WELL_COEFFS

            self.rot_coeffs = ROT_WELL_COEFFS

    def _req_rows(self, nets_a: np.ndarray, nets_b: np.ndarray) -> np.ndarray:
        """Worst-layer required spacing per (net, net) row pair — vias and
        obstacles sit on every layer, so the strictest tier is the one."""
        prob = self.problem
        out = np.zeros(len(nets_a))
        cache: dict[tuple[int, int], float] = {}
        for k, (na, nb) in enumerate(zip(nets_a, nets_b, strict=True)):
            key = (int(na), int(nb))
            v = cache.get(key)
            if v is None:
                v = max(
                    prob.required_mm(key[0], key[1], l) for l in range(prob.n_layers)
                )
                cache[key] = v
            out[k] = v
        return out

    # ── pose transforms ───────────────────────────────────────────────

    def part_poses(self, gpose):
        """``(K, G, 3)`` group poses → ``(K, M, 3)`` part poses (block
        prolong: pose ∘ in-group offset)."""
        import torch

        if self.n_parts == 0:
            return gpose.new_zeros((gpose.shape[0], 0, 3))
        g = gpose[:, self.part_group]  # (K, M, 3)
        c, s = torch.cos(g[..., 2]), torch.sin(g[..., 2])
        dx, dy = self.off_d[:, 0], self.off_d[:, 1]
        x = g[..., 0] + dx * c + dy * s
        y = g[..., 1] - dx * s + dy * c
        th = g[..., 2] + self.off_th
        return torch.stack((x, y, th), dim=-1)

    @staticmethod
    def _to_world(ppose, part_idx, local):
        """Local points ``local`` (R, 2) on parts ``part_idx`` (R,) under
        poses ``ppose`` (K, M, 3) → (K, R, 2)."""
        import torch

        p = ppose[:, part_idx]  # (K, R, 3)
        c, s = torch.cos(p[..., 2]), torch.sin(p[..., 2])
        wx = p[..., 0] + local[:, 0] * c + local[:, 1] * s
        wy = p[..., 1] - local[:, 0] * s + local[:, 1] * c
        return torch.stack((wx, wy), dim=-1)

    def full_points(self, pos, gpose):
        """Points with the part-anchored endpoints derived from the poses."""
        if self.n_parts == 0 or not len(self.ep_pt):
            return pos
        ppose = self.part_poses(gpose)
        ep = self._to_world(ppose, self.ep_part, self.ep_local)
        full = pos.clone()
        full[:, self.ep_pt] = ep
        return full

    def _dyn_segs(self, ppose):
        """World (K, Sd, 2, 2) endpoints of the dynamic segment cores."""
        a0 = self._to_world(ppose, self.dseg_part, self.dseg_local[:, :2])
        a1 = self._to_world(ppose, self.dseg_part, self.dseg_local[:, 2:])
        return a0, a1

    def _dyn_boxes(self, ppose):
        import torch

        c = self._to_world(ppose, self.dbox_part, self.dbox_c)
        th = ppose[:, self.dbox_part, 2]
        ang = self.dbox_phi - th
        cs = torch.stack((torch.cos(ang), torch.sin(ang)), dim=-1)
        return c, cs

    def _courts(self, ppose):
        import torch

        idx = torch.arange(self.n_parts)
        c = self._to_world(ppose, idx, self.court_c)
        th = ppose[..., 2]
        cs = torch.stack((torch.cos(-th), torch.sin(-th)), dim=-1)
        return c, cs

    # ── energy ──────────────────────────────────────────────────────

    def energy(self, pos, assign, width_frac, gpose=None, extra=None):
        """``(E_total[K], breakdown dict)`` — differentiable in ``pos``,
        ``gpose`` and ``extra``. ``pos`` holds the wire points (endpoints
        anchored to a part are overwritten from ``gpose``); ``extra`` is
        the per-wire width above the floor (K, W), None = 0."""
        import torch

        prm = self.params
        K = pos.shape[0]
        if gpose is None:
            gpose = self.gpose0[None].expand(K, -1, -1)
        stiff = prm.stiff_min + (prm.stiff_max - prm.stiff_min) * width_frac
        if extra is None:
            r_piece = (self.piece_w * (width_frac / 2.0))[None, :].expand(K, -1)
        else:
            r_piece = (self.piece_w[None, :] + extra[:, self.piece_wire]) * (
                width_frac / 2.0
            )
        full = self.full_points(pos, gpose)
        ppose = self.part_poses(gpose)
        p0 = full[:, self.p0]  # (K,P,2)
        p1 = full[:, self.p1]
        plen = (p1 - p0).norm(dim=-1)  # (K,P)
        length = plen.sum(1)
        E = length.clone()
        via_active = assign[:, self.site_left] != assign[:, self.site_right]
        n_via = via_active.sum(1).to(self.t64)
        E = E + prm.via_cost_mm * n_via
        bar = torch.zeros_like(E)
        bar = bar + self._pp_term(assign, r_piece, p0, p1)
        bar = bar + self._pobs_term(assign, r_piece, p0, p1)
        bar = bar + self._pbox_term(assign, r_piece, p0, p1)
        bar = bar + self._edge_term(r_piece, p0, p1)
        bar = bar + self._bead_terms(full, assign, via_active, ppose)
        court = torch.zeros_like(E)
        rot = torch.zeros_like(E)
        if self.n_parts:
            bar = bar + self._pdyn_terms(assign, r_piece, p0, p1, ppose)
            court = self._court_terms(ppose)
            if self.rot_coeffs is not None:
                rot = prm.rot_weight * _rot_well(
                    ppose[..., 2], self.part_rot_free, self.rot_coeffs
                )
        area = torch.zeros_like(E)
        if extra is not None and prm.appetite_scale > 0.0:
            wtot = self.piece_w[None, :] + extra[:, self.piece_wire]
            area = prm.appetite_scale * (
                self.wire_appetite[self.piece_wire][None, :] * wtot * plen
            ).sum(1)
        # courtyards do not grow with the wires: full stiffness throughout
        E = E + stiff * bar + prm.stiff_max * court + rot + area
        return E, {
            "length": length,
            "vias": n_via,
            "barrier": bar,
            "court": court,
            "rot": rot,
            "area": area,
            "stiff": stiff,
            "pose": ppose,
            "points": full,
        }

    def _gather_req(self, wire_i, wire_j, layer):
        K, M = layer.shape
        req = self.req_wires[wire_i, wire_j, :]
        return (
            req[None, :, :]
            .expand(K, M, req.shape[-1])
            .gather(2, layer.clamp(0, req.shape[-1] - 1).unsqueeze(-1))
            .squeeze(-1)
        )

    def _pp_term(self, assign, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.pp_i, self.pp_j
        if not len(i):
            return torch.zeros(p0.shape[0], dtype=self.t64)
        # plain distance: a crossing pair is flat (0) on purpose — the
        # swept check forbids uncrossing by vertex motion, so a crossing is
        # the layer move's job (pr.uncross), and the flat penalty still
        # scores it for CEM
        d = D.seg_seg_dist(p0[:, i], p1[:, i], p0[:, j], p1[:, j])
        same = assign[:, i] == assign[:, j]
        req = self._gather_req(self.piece_wire[i], self.piece_wire[j], assign[:, i])
        g = d - r_piece[:, i] - r_piece[:, j] - req - self.params.margin_mm
        return self._acc(
            self.piece_wire[i], self.piece_wire[j], torch.relu(-g) ** 2 * same
        ).sum(1)

    def _pobs_term(self, assign, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.po_i, self.po_j
        if not len(i):
            return torch.zeros(p0.shape[0], dtype=self.t64)
        K = p0.shape[0]
        M = len(i)
        o0 = self.obs_seg[j, :2][None, :].expand(K, M, 2).reshape(-1, 2)
        o1 = self.obs_seg[j, 2:][None, :].expand(K, M, 2).reshape(-1, 2)
        d = D.seg_seg_dist(
            p0[:, i].reshape(-1, 2), p1[:, i].reshape(-1, 2), o0, o1
        ).reshape(K, M)
        active = ((self.obs_seg_lmask[j][None, :] >> assign[:, i]) & 1) > 0
        g = d - r_piece[:, i] - self.obs_seg_r[j][None, :] - self.po_req[None, :]
        return self._acc(self.piece_wire[i], None, torch.relu(-g) ** 2 * active).sum(1)

    def _pbox_term(self, assign, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.pb_i, self.pb_j
        if not len(i):
            return torch.zeros(p0.shape[0], dtype=self.t64)
        K = p0.shape[0]
        a0 = p0[:, i].reshape(K * len(i), 2)
        a1 = p1[:, i].reshape(K * len(i), 2)
        c = self.obs_box_c[j][None, :].expand(K, -1, -1).reshape(-1, 2)
        hw = self.obs_box_hw[j][None, :].expand(K, -1, -1).reshape(-1, 2)
        cs = self.obs_box_cs[j][None, :].expand(K, -1, -1).reshape(-1, 2)
        d = D.seg_box_signed(a0, a1, c, hw, cs).reshape(K, -1)
        active = ((self.obs_box_lmask[j][None, :] >> assign[:, i]) & 1) > 0
        g = d - r_piece[:, i] - self.pb_req[None, :]
        return self._acc(self.piece_wire[i], None, torch.relu(-g) ** 2 * active).sum(1)

    def _pdyn_terms(self, assign, r_piece, p0, p1, ppose):
        """Pieces against the pads of movable parts (both ride the state)."""
        import torch

        from precis.pcb.pr import distance as D

        K = p0.shape[0]
        out = torch.zeros(K, dtype=self.t64)
        if len(self.pd_i):
            i, j = self.pd_i, self.pd_j
            a0, a1 = self._dyn_segs(ppose)  # (K, Sd, 2)
            d = D.seg_seg_dist(p0[:, i], p1[:, i], a0[:, j], a1[:, j])
            active = ((self.dseg_lmask[j][None, :] >> assign[:, i]) & 1) > 0
            g = d - r_piece[:, i] - self.dseg_r[j][None, :] - self.pd_req[None, :]
            out = out + self._acc(
                self.piece_wire[i], None, torch.relu(-g) ** 2 * active
            ).sum(1)
        if len(self.pB_i):
            i, j = self.pB_i, self.pB_j
            c, cs = self._dyn_boxes(ppose)  # (K, Bd, 2)
            M = len(i)
            d = D.seg_box_signed(
                p0[:, i].reshape(-1, 2),
                p1[:, i].reshape(-1, 2),
                c[:, j].reshape(-1, 2),
                self.dbox_hw[j][None, :].expand(K, M, 2).reshape(-1, 2),
                cs[:, j].reshape(-1, 2),
            ).reshape(K, M)
            active = ((self.dbox_lmask[j][None, :] >> assign[:, i]) & 1) > 0
            g = d - r_piece[:, i] - self.pB_req[None, :]
            out = out + self._acc(
                self.piece_wire[i], None, torch.relu(-g) ** 2 * active
            ).sum(1)
        return out

    def _court_terms(self, ppose):
        """Courtyard–courtyard barrier (movable × movable, movable ×
        locked) plus hold-in of every movable courtyard's corners."""
        import torch

        from precis.pcb.pr import distance as D

        K = ppose.shape[0]
        out = torch.zeros(K, dtype=self.t64)
        c, cs = self._courts(ppose)  # (K, M, 2)
        hw = self.court_hw
        cm = self.params.court_margin_mm
        if len(self.cc_i):
            i, j = self.cc_i, self.cc_j
            g = _box_box_gap(
                c[:, i], hw[i][None], cs[:, i], c[:, j], hw[j][None], cs[:, j]
            )
            out = out + (torch.relu(cm - g) ** 2).sum(1)
        if len(self.cs_i):
            i, j = self.cs_i, self.cs_j
            g = _box_box_gap(
                c[:, i],
                hw[i][None],
                cs[:, i],
                self.scourt_c[j][None],
                self.scourt_hw[j][None],
                self.scourt_cs[j][None],
            )
            out = out + (torch.relu(cm - g) ** 2).sum(1)
        # hold-in: the four courtyard corners inside the outline
        ex = torch.stack((cs[..., 0], cs[..., 1]), dim=-1) * hw[None, :, 0:1]
        ey = torch.stack((-cs[..., 1], cs[..., 0]), dim=-1) * hw[None, :, 1:2]
        corners = torch.stack(
            (c + ex + ey, c + ex - ey, c - ex + ey, c - ex - ey), dim=2
        )
        m = D.outline_margin(corners.reshape(K, -1, 2), self.outline)
        out = out + (torch.relu(-m) ** 2).sum(1)
        return out

    def _edge_term(self, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        m = torch.minimum(
            D.outline_margin(p0, self.outline), D.outline_margin(p1, self.outline)
        )
        g = m - r_piece - self.problem.edge_mm
        return self._acc(
            self.piece_wire, None, torch.relu(-g) ** 2 * self.piece_act[None, :]
        ).sum(1)

    def _bead_terms(self, full, assign, via_active, ppose):
        """Via beads: a point on EVERY layer — vs foreign pieces on the
        piece's own layer, vs every obstacle (any layer), vs the outline."""
        import torch

        from precis.pcb.pr import distance as D

        K = full.shape[0]
        bar = torch.zeros(K, dtype=self.t64)
        if not len(self.site_pt):
            return bar
        beads = full[:, self.site_pt]  # (K, S, 2)
        act = via_active.to(self.t64)  # (K,S)
        if len(self.bp_s):
            s, j = self.bp_s, self.bp_j
            b = beads[:, s]
            a0 = full[:, self.p0][:, j]
            a1 = full[:, self.p1][:, j]
            d = D.seg_seg_dist(b, b, a0, a1)
            req = self._gather_req(self.site_wire[s], self.piece_wire[j], assign[:, j])
            g = (
                d
                - self.via_r
                - self.piece_w[j][None, :] / 2.0
                - req
                - self.params.margin_mm
            )
            bar = bar + self._acc(
                self.site_wire[s], self.piece_wire[j], torch.relu(-g) ** 2 * act[:, s]
            ).sum(1)
        if len(self.bos_s):
            s, j = self.bos_s, self.bos_j
            b = beads[:, s].reshape(K * len(s), 2)
            o0 = self.obs_seg[j][:, :2][None, :].expand(K, -1, -1).reshape(-1, 2)
            o1 = self.obs_seg[j][:, 2:][None, :].expand(K, -1, -1).reshape(-1, 2)
            d = D.seg_seg_dist(b, b, o0, o1).reshape(K, -1)
            g = d - (self.via_r + self.obs_seg_r[j])[None, :] - self.bos_req[None, :]
            bar = bar + self._acc(
                self.site_wire[s], None, torch.relu(-g) ** 2 * act[:, s]
            ).sum(1)
        if len(self.bob_s):
            s, j = self.bob_s, self.bob_j
            b = beads[:, s].reshape(K * len(s), 2)
            c = self.obs_box_c[j][None, :].expand(K, -1, -1).reshape(-1, 2)
            hw = self.obs_box_hw[j][None, :].expand(K, -1, -1).reshape(-1, 2)
            cs = self.obs_box_cs[j][None, :].expand(K, -1, -1).reshape(-1, 2)
            d = D.point_box_dist(b, c, hw, cs).reshape(K, -1)
            g = d - self.via_r - self.bob_req[None, :]
            bar = bar + self._acc(
                self.site_wire[s], None, torch.relu(-g) ** 2 * act[:, s]
            ).sum(1)
        if len(self.bd_s):
            s, j = self.bd_s, self.bd_j
            a0, a1 = self._dyn_segs(ppose)
            b = beads[:, s]
            d = D.seg_seg_dist(b, b, a0[:, j], a1[:, j])
            g = d - (self.via_r + self.dseg_r[j])[None, :] - self.bd_req[None, :]
            bar = bar + self._acc(
                self.site_wire[s], None, torch.relu(-g) ** 2 * act[:, s]
            ).sum(1)
        if len(self.bB_s):
            s, j = self.bB_s, self.bB_j
            c, cs = self._dyn_boxes(ppose)
            M = len(s)
            d = D.point_box_dist(
                beads[:, s].reshape(-1, 2),
                c[:, j].reshape(-1, 2),
                self.dbox_hw[j][None, :].expand(K, M, 2).reshape(-1, 2),
                cs[:, j].reshape(-1, 2),
            ).reshape(K, M)
            g = d - self.via_r - self.bB_req[None, :]
            bar = bar + self._acc(
                self.site_wire[s], None, torch.relu(-g) ** 2 * act[:, s]
            ).sum(1)
        if len(self.bb_s):
            s, t = self.bb_s, self.bb_t
            d = (beads[:, s] - beads[:, t]).norm(dim=-1)
            g = d - 2.0 * self.via_r - self.bb_req[None, :]
            bar = bar + self._acc(
                self.site_wire[s],
                self.site_wire[t],
                torch.relu(-g) ** 2 * act[:, s] * act[:, t],
            ).sum(1)
        m = D.outline_margin(beads, self.outline)
        g = m - self.via_r - self.problem.edge_mm
        bar = bar + self._acc(self.site_wire, None, torch.relu(-g) ** 2 * act).sum(1)
        return bar

    # ── swept collision check ────────────────────────────────────────

    def _new_crossings(self, old, new, assign, old_pose=None, new_pose=None):
        """``(K,)`` count of same-layer foreign piece pairs — and moving
        pad cores against same-layer wires — that go from not-crossing to
        crossing between ``old`` and ``new`` (full point arrays)."""
        import torch

        from precis.pcb.pr import distance as D

        K = old.shape[0]
        out = torch.zeros(K, dtype=torch.int64)
        i, j = self.pp_i, self.pp_j
        if len(i):
            a0o, a1o = old[:, self.p0][:, i], old[:, self.p1][:, i]
            b0o, b1o = old[:, self.p0][:, j], old[:, self.p1][:, j]
            a0n, a1n = new[:, self.p0][:, i], new[:, self.p1][:, i]
            b0n, b1n = new[:, self.p0][:, j], new[:, self.p1][:, j]
            same = assign[:, i] == assign[:, j]
            before = D.seg_seg_cross(a0o, a1o, b0o, b1o)
            after = D.seg_seg_cross(a0n, a1n, b0n, b1n)
            out = out + (after & ~before & same).sum(1)
        if len(self.ps_i) and old_pose is not None and new_pose is not None:
            i, j = self.ps_i, self.ps_j
            so0 = self._to_world(old_pose, self.sw_part, self.sw_local[:, :2])[:, j]
            so1 = self._to_world(old_pose, self.sw_part, self.sw_local[:, 2:])[:, j]
            sn0 = self._to_world(new_pose, self.sw_part, self.sw_local[:, :2])[:, j]
            sn1 = self._to_world(new_pose, self.sw_part, self.sw_local[:, 2:])[:, j]
            before = D.seg_seg_cross(
                old[:, self.p0][:, i], old[:, self.p1][:, i], so0, so1
            )
            after = D.seg_seg_cross(
                new[:, self.p0][:, i], new[:, self.p1][:, i], sn0, sn1
            )
            on = ((self.sw_lmask[j][None, :] >> assign[:, i]) & 1) > 0
            out = out + (after & ~before & on).sum(1)
        return out

    # ── the step loop ────────────────────────────────────────────────

    def run(
        self,
        assignments,
        pos0=None,
        params: RelaxParams | None = None,
        gpose0=None,
        extra0=None,
    ):
        """Relax K samples from ``assignments`` (K,P) — returns
        ``(points, energies, parts, stats)``; ``points`` has the derived
        endpoints materialised, ``parts['pose']`` the part poses,
        ``parts['gpose']`` the group poses and ``parts['extra']`` the
        appetite widths."""
        import time as _t

        import torch

        prm = params or self.params
        K, P = assignments.shape
        assign = torch.tensor(assignments, dtype=torch.int64)
        pos = torch.tensor(
            np.broadcast_to(self.layout.points, (K, self.n_pts, 2)).copy()
            if pos0 is None
            else pos0,
            dtype=self.t64,
        )
        gpose = (
            self.gpose0[None].expand(K, -1, -1).clone()
            if gpose0 is None
            else torch.tensor(np.asarray(gpose0), dtype=self.t64)
        )
        n_w = len(self.problem.wires)
        widen = prm.appetite_scale > 0.0 and n_w > 0
        extra = (
            (
                torch.zeros((K, n_w), dtype=self.t64)
                if extra0 is None
                else torch.tensor(np.asarray(extra0), dtype=self.t64)
            )
            if widen
            else None
        )
        free = self.free
        eta = torch.full((K,), prm.eta0, dtype=self.t64)
        eta_p = torch.full((K,), prm.part_eta0, dtype=self.t64)
        stats = RelaxStats()
        t0 = _t.monotonic()
        move_parts = self.n_parts > 0
        for step in range(prm.steps):
            width = min(1.0, (step + 1) / max(1, prm.grow_steps))
            pos.requires_grad_(True)
            leaves = [pos]
            if move_parts:
                gpose.requires_grad_(True)
                leaves.append(gpose)
            if extra is not None:
                extra.requires_grad_(True)
                leaves.append(extra)
            E, _ = self.energy(pos, assign, width, gpose, extra)
            stats.evals += 1
            grads = torch.autograd.grad(E.sum(), leaves)
            g = grads[0]
            pos = pos.detach()
            gpose = gpose.detach()
            if extra is not None:
                extra = extra.detach()
            # normalized per-point step, capped displacement
            disp = -eta[:, None, None] * g
            n = disp.norm(dim=-1, keepdim=True).clamp_min(1e-12)
            cap = prm.max_disp_mm * (0.3 + 0.7 * width)
            disp = disp * torch.clamp(cap / n, max=1.0)
            disp = disp * free[None, :, None]
            gdisp = torch.zeros_like(gpose)
            if move_parts:
                gg = grads[1]
                dxy = -eta_p[:, None, None] * gg[..., :2]
                nn = dxy.norm(dim=-1, keepdim=True).clamp_min(1e-12)
                dxy = dxy * torch.clamp(prm.max_part_disp_mm / nn, max=1.0)
                dth = (-eta_p[:, None] * gg[..., 2]).clamp(
                    -prm.max_part_rot_rad, prm.max_part_rot_rad
                )
                dxy = dxy * (~self.glock_xy)[None, :, None]
                dth = dth * (~self.glock_rot)[None, :]
                gdisp = torch.cat((dxy, dth[..., None]), dim=-1)
            old_full = self.full_points(pos, gpose)
            old_pose = self.part_poses(gpose) if move_parts else None
            cand = pos + disp
            cand_g = gpose + gdisp
            new_full = self.full_points(cand, cand_g)
            new_pose = self.part_poses(cand_g) if move_parts else None
            viol = self._new_crossings(old_full, new_full, assign, old_pose, new_pose)
            tries = 0
            while (viol > 0).any() and tries < prm.sweep_halvings:
                stats.blocked += int((viol > 0).sum())
                bad = viol > 0
                eta = torch.where(bad, eta / 2.0, eta)
                eta_p = torch.where(bad, eta_p / 2.0, eta_p)
                cand = torch.where(bad[:, None, None], pos + (cand - pos) / 2.0, cand)
                cand_g = torch.where(
                    bad[:, None, None], gpose + (cand_g - gpose) / 2.0, cand_g
                )
                new_full = self.full_points(cand, cand_g)
                new_pose = self.part_poses(cand_g) if move_parts else None
                viol = self._new_crossings(
                    old_full, new_full, assign, old_pose, new_pose
                )
                tries += 1
            still = viol > 0
            stats.dropped += int(still.sum())
            pos = torch.where(still[:, None, None], pos, cand)
            gpose = torch.where(still[:, None, None], gpose, cand_g)
            if extra is not None:
                ge = grads[-1]
                extra = (extra - prm.eta0 * ge).clamp(0.0, prm.max_widen_mm)
            eta = (eta * prm.eta_grow).clamp(max=prm.eta0 * 8)
            eta_p = (eta_p * prm.eta_grow).clamp(max=prm.part_eta0 * 8)
            stats.steps += 1
        pos.requires_grad_(True)
        E, parts = self.energy(pos, assign, 1.0, gpose, extra)
        stats.evals += 1
        stats.seconds = _t.monotonic() - t0
        parts = {
            k: (v.detach() if hasattr(v, "detach") else v) for k, v in parts.items()
        }
        parts["gpose"] = gpose.detach()
        parts["extra"] = extra.detach() if extra is not None else None
        return parts["points"].detach(), E.detach(), parts, stats

    def violations(self, pos, assign, gpose=None, extra=None):
        """Hard leftover violation depth (mm²-equivalents) per sample —
        the legality read on the relaxed board at full width: copper
        barrier plus courtyard/hold-in."""
        import torch

        with torch.no_grad():
            _, parts = self.energy(pos, assign, 1.0, gpose, extra)
        return parts["barrier"] + parts["court"]

    def _acc(self, wa, wb, pen):
        """Barrier pair penalties pass through here so
        :meth:`wire_violations` can attribute them per wire; ``wa``/``wb``
        are the wire ids of the two sides (``wb`` None for an obstacle)."""
        if self._collect is not None:
            self._collect.append((wa, wb, pen))
        return pen

    def wire_violations(self, pos, assign, gpose=None, extra=None):
        """Per-wire share of the copper violation depth at full width,
        ``(K, W)``: each pair penalty is charged to both wires it names
        (an obstacle pair to its one wire). The spec's per-connection
        failure read (§Results and failures): a wire that keeps a share
        after relaxation is the one to report, with the depth."""
        import torch

        self._collect = []
        try:
            with torch.no_grad():
                self.energy(pos, assign, 1.0, gpose, extra)
            K = pos.shape[0]
            W = len(self.problem.wires)
            out = torch.zeros((K, W), dtype=self.t64)
            for wa, wb, pen in self._collect:
                if pen.numel() == 0:
                    continue
                out.index_add_(1, wa, pen)
                if wb is not None:
                    out.index_add_(1, wb, pen)
        finally:
            self._collect = None
        return out


def _box_box_gap(cA, hwA, csA, cB, hwB, csB):
    """Separating-axis gap between oriented boxes (…, 2) shapes: the
    largest separation over the four face normals. Exact when the boxes
    are separated along a face normal, an underestimate (conservative) at
    a corner–corner approach, and the negative of the least penetration
    when overlapping — a differentiable depth for the barrier."""
    import torch

    d = cB - cA
    pA = torch.stack((-csA[..., 1], csA[..., 0]), dim=-1)
    pB = torch.stack((-csB[..., 1], csB[..., 0]), dim=-1)
    seps = []
    for ax in (csA, pA, csB, pB):
        proj = (d * ax).sum(-1).abs()
        ra = (
            hwA[..., 0] * (csA * ax).sum(-1).abs()
            + hwA[..., 1] * (pA * ax).sum(-1).abs()
        )
        rb = (
            hwB[..., 0] * (csB * ax).sum(-1).abs()
            + hwB[..., 1] * (pB * ax).sum(-1).abs()
        )
        seps.append(proj - ra - rb)
    return torch.stack(seps, dim=-1).max(dim=-1).values


def _rot_well(theta, free_mask, coeffs) -> Any:
    """Σ over rotation-free parts of the preferred-tier well at θ (degrees
    on ``field.ROT_TIERS_DEG``), as a torch autograd function around
    :func:`precis.structsolve.preferred.evaluate` (value + analytic
    gradient, numpy). Returns (K,)."""
    import torch

    class _Well(torch.autograd.Function):
        @staticmethod
        def forward(ctx, th):
            from precis.structsolve import preferred

            deg = np.degrees(th.detach().cpu().numpy())
            res = preferred.evaluate(deg, coeffs)
            val = np.asarray(res.value, dtype=np.float64)
            grad = np.asarray(res.grad, dtype=np.float64) * (180.0 / np.pi)
            ctx.save_for_backward(torch.tensor(grad, dtype=th.dtype))
            return torch.tensor(val, dtype=th.dtype)

        @staticmethod
        def backward(ctx, g):
            (grad,) = ctx.saved_tensors
            return g * grad

    w = _Well.apply(theta)  # (K, M)
    return (w * free_mask.to(w.dtype)[None, :]).sum(1)
