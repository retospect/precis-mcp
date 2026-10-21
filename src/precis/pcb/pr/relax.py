"""4c batched geometry relaxation (spec §Method 4c): K sampled boards
relaxed as one tensor computation. Energy = length (1/mm) + via beads
(``via_cost_mm`` each) + a one-sided quadratic barrier on the gap below
0, stiffness ramped with the linear width growth 0 → full. A swept
collision check (orientation-sign flip on same-layer foreign segment
pairs) rejects or halves any step that would create a crossing, so the
4a topology holds.

Torch is imported lazily — the host venv has none; the container gate
does. ``Relaxer`` builds its index tables once per layout; pair
candidates are pruned by a reach radius since pieces move little inside
a swept-checked topology.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from precis.pcb.pr.beliefs import Layout
from precis.pcb.pr.state import PrProblem


@dataclass(frozen=True, slots=True)
class RelaxParams:
    """Provisional slice-1 parameters (spec/brief: fix and report)."""

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


@dataclass(slots=True)
class RelaxStats:
    evals: int = 0
    blocked: int = 0  # accepted-step rejections that needed ≥1 halving
    dropped: int = 0  # steps dropped entirely after max halvings
    steps: int = 0
    seconds: float = 0.0


@dataclass
class Relaxer:
    """Precomputed index tables + the step loop for one layout."""

    problem: PrProblem
    layout: Layout
    params: RelaxParams = field(default_factory=RelaxParams)

    def __post_init__(self) -> None:
        import torch

        prob, lay, prm = self.problem, self.layout, self.params
        t64 = torch.float64
        self.p0 = torch.tensor(lay.piece_p0)
        self.p1 = torch.tensor(lay.piece_p1)
        self.free = torch.tensor(lay.free)
        self.piece_wire = torch.tensor(lay.piece_wire)
        n_pts = len(lay.points)
        # piece width (per wire) and per-pair required spacing.
        w_of_piece = np.asarray([prob.wires[w].width_mm for w in lay.piece_wire])
        self.piece_w = torch.tensor(w_of_piece, dtype=t64)
        n_wires = len(prob.wires)
        req = np.zeros((n_wires, n_wires, prob.n_layers))
        for i in range(n_wires):
            for j in range(n_wires):
                ni, nj = prob.wires[i].net_id, prob.wires[j].net_id
                for l in range(prob.n_layers):
                    req[i, j, l] = prob.required_mm(ni, nj, l)
        self.req_wires = torch.tensor(req, dtype=t64)  # indexed by WIRE idx
        # ── candidate pair tables (static, pruned by reach) ──
        a = lay.points[lay.piece_p0]
        b = lay.points[lay.piece_p1]
        lo = np.minimum(a, b) - prm.prune_mm
        hi = np.maximum(a, b) + prm.prune_mm
        pw = lay.piece_wire
        pp: list[tuple[int, int]] = []
        for i in range(len(a)):
            for j in range(i + 1, len(a)):
                if pw[i] == pw[j]:
                    continue
                if (lo[j] <= hi[i]).all() and (hi[j] >= lo[i]).all():
                    pp.append((i, j))
        self.pp_i = torch.tensor([p[0] for p in pp], dtype=torch.int64)
        self.pp_j = torch.tensor([p[1] for p in pp], dtype=torch.int64)
        # piece × obstacle segment rows
        obs = prob.obs
        po: list[tuple[int, int]] = []
        if len(obs.seg_xy):
            olo = np.minimum(obs.seg_xy[:, :2], obs.seg_xy[:, 2:]) - obs.seg_r[:, None]
            ohi = np.maximum(obs.seg_xy[:, :2], obs.seg_xy[:, 2:]) + obs.seg_r[:, None]
            for i in range(len(a)):
                hit = np.nonzero(
                    (olo[:, 0] <= hi[i, 0])
                    & (ohi[:, 0] >= lo[i, 0])
                    & (olo[:, 1] <= hi[i, 1])
                    & (ohi[:, 1] >= lo[i, 1])
                    & (obs.seg_net != prob.wires[pw[i]].net_id)
                )[0]
                po += [(i, int(j)) for j in hit]
        self.po_i = torch.tensor([p[0] for p in po], dtype=torch.int64)
        self.po_j = torch.tensor([p[1] for p in po], dtype=torch.int64)
        self.obs_seg = torch.tensor(
            obs.seg_xy if len(obs.seg_xy) else np.zeros((0, 4)), dtype=t64
        )
        self.obs_seg_r = torch.tensor(obs.seg_r, dtype=t64)
        self.obs_seg_net = torch.tensor(obs.seg_net, dtype=torch.int64)
        self.obs_seg_lmask = torch.tensor(obs.seg_lmask, dtype=torch.int64)
        # required per (piece-net, obs-net): constant across pairs here —
        # resolve lazily per pair count is fine at slice-1 scale.
        po_req = np.zeros(len(po))
        for k, (i, j) in enumerate(po):
            po_req[k] = max(
                prob.required_mm(prob.wires[pw[i]].net_id, int(obs.seg_net[j]), l)
                for l in range(prob.n_layers)
            )
        self.po_req = torch.tensor(po_req, dtype=t64)
        # piece × obstacle box rows
        pb: list[tuple[int, int]] = []
        if len(obs.box_c):
            for i in range(len(a)):
                hit = np.nonzero(
                    (obs.box_c[:, 0] - obs.box_hw[:, 0] <= hi[i, 0])
                    & (obs.box_c[:, 0] + obs.box_hw[:, 0] >= lo[i, 0])
                    & (obs.box_c[:, 1] - obs.box_hw[:, 1] <= hi[i, 1])
                    & (obs.box_c[:, 1] + obs.box_hw[:, 1] >= lo[i, 1])
                    & (obs.box_net != prob.wires[pw[i]].net_id)
                )[0]
                pb += [(i, int(j)) for j in hit]
        self.pb_i = torch.tensor([p[0] for p in pb], dtype=torch.int64)
        self.pb_j = torch.tensor([p[1] for p in pb], dtype=torch.int64)
        self.obs_box_c = torch.tensor(
            obs.box_c if len(obs.box_c) else np.zeros((0, 2)), dtype=t64
        )
        self.obs_box_hw = torch.tensor(
            obs.box_hw if len(obs.box_hw) else np.zeros((0, 2)), dtype=t64
        )
        self.obs_box_cs = torch.tensor(
            obs.box_cs if len(obs.box_cs) else np.zeros((0, 2)), dtype=t64
        )
        self.obs_box_net = torch.tensor(obs.box_net, dtype=torch.int64)
        self.obs_box_lmask = torch.tensor(obs.box_lmask, dtype=torch.int64)
        pb_req = np.zeros(len(pb))
        for k, (i, j) in enumerate(pb):
            pb_req[k] = max(
                prob.required_mm(prob.wires[pw[i]].net_id, int(obs.box_net[j]), l)
                for l in range(prob.n_layers)
            )
        self.pb_req = torch.tensor(pb_req, dtype=t64)
        # via sites: interior point p of wire w with piece p0→p / p→p1.
        # site s sits between pieces (s_left, s_right) at point s_pt.
        sites_pt: list[int] = []
        sites_left: list[int] = []
        sites_right: list[int] = []
        sites_wire: list[int] = []
        # piece (wire-local index j) -> point index p1; boundary usable?
        pts_of_wire: dict[int, list[int]] = {}
        for i, w in enumerate(lay.wire_of_point):
            pts_of_wire.setdefault(int(w), []).append(i)
        piece_of = {}
        for pi, (u, v) in enumerate(zip(lay.piece_p0, lay.piece_p1)):
            piece_of[int(u)] = pi  # piece ENDING at u? piece starts at u
        # boundary vertex v between pieces i,j: i ends at v, j starts v
        end_at: dict[int, int] = {}
        start_at: dict[int, int] = {}
        for pi, (u, v) in enumerate(zip(lay.piece_p0, lay.piece_p1)):
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
        # bead × piece pairs (different wire) and bead × obstacle rows
        bp: list[tuple[int, int]] = []
        bos: list[tuple[int, int]] = []
        bob: list[tuple[int, int]] = []
        if sites_pt:
            sp = lay.points[np.asarray(sites_pt)]
            for si, xy in enumerate(sp):
                hit = np.nonzero(
                    (lo[:, 0] <= xy[0] + prm.bead_prune_mm)
                    & (hi[:, 0] >= xy[0] - prm.bead_prune_mm)
                    & (lo[:, 1] <= xy[1] + prm.bead_prune_mm)
                    & (hi[:, 1] >= xy[1] - prm.bead_prune_mm)
                )[0]
                for j in hit:
                    if pw[int(j)] != sites_wire[si]:
                        bp.append((si, int(j)))
                if len(obs.seg_xy):
                    dseg = obs.seg_xy
                    hito = np.nonzero(
                        (
                            np.minimum(dseg[:, 0], dseg[:, 2]) - obs.seg_r
                            <= xy[0] + prm.bead_prune_mm
                        )
                        & (
                            np.maximum(dseg[:, 0], dseg[:, 2]) + obs.seg_r
                            >= xy[0] - prm.bead_prune_mm
                        )
                        & (
                            np.minimum(dseg[:, 1], dseg[:, 3]) - obs.seg_r
                            <= xy[1] + prm.bead_prune_mm
                        )
                        & (
                            np.maximum(dseg[:, 1], dseg[:, 3]) + obs.seg_r
                            >= xy[1] - prm.bead_prune_mm
                        )
                    )[0]
                    bos += [(si, int(j)) for j in hito]
                if len(obs.box_c):
                    hitb = np.nonzero(
                        (
                            obs.box_c[:, 0] - obs.box_hw[:, 0]
                            <= xy[0] + prm.bead_prune_mm
                        )
                        & (
                            obs.box_c[:, 0] + obs.box_hw[:, 0]
                            >= xy[0] - prm.bead_prune_mm
                        )
                        & (
                            obs.box_c[:, 1] - obs.box_hw[:, 1]
                            <= xy[1] + prm.bead_prune_mm
                        )
                        & (
                            obs.box_c[:, 1] + obs.box_hw[:, 1]
                            >= xy[1] - prm.bead_prune_mm
                        )
                    )[0]
                    bob += [(si, int(j)) for j in hitb]
        self.bp_s = torch.tensor([p[0] for p in bp], dtype=torch.int64)
        self.bp_j = torch.tensor([p[1] for p in bp], dtype=torch.int64)
        self.bos_s = torch.tensor([p[0] for p in bos], dtype=torch.int64)
        self.bos_j = torch.tensor([p[1] for p in bos], dtype=torch.int64)
        self.bob_s = torch.tensor([p[0] for p in bob], dtype=torch.int64)
        self.bob_j = torch.tensor([p[1] for p in bob], dtype=torch.int64)
        bos_req = np.zeros(len(bos))
        for k, (si, j) in enumerate(bos):
            bos_req[k] = max(
                prob.required_mm(
                    prob.wires[sites_wire[si]].net_id, int(obs.seg_net[j]), l
                )
                for l in range(prob.n_layers)
            )
        self.bos_req = torch.tensor(bos_req, dtype=t64)
        bob_req = np.zeros(len(bob))
        for k, (si, j) in enumerate(bob):
            bob_req[k] = max(
                prob.required_mm(
                    prob.wires[sites_wire[si]].net_id, int(obs.box_net[j]), l
                )
                for l in range(prob.n_layers)
            )
        self.bob_req = torch.tensor(bob_req, dtype=t64)
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
        self.n_pts = n_pts

    # ── energy ──────────────────────────────────────────────────────

    def energy(self, pos, assign, width_frac):
        """``(E_total[K], breakdown dict)`` — differentiable in ``pos``."""
        import torch

        prm = self.params
        stiff = prm.stiff_min + (prm.stiff_max - prm.stiff_min) * width_frac
        r_piece = self.piece_w * (width_frac / 2.0)  # (P,)
        p0 = pos[:, self.p0]  # (K,P,2)
        p1 = pos[:, self.p1]
        length = (p1 - p0).norm(dim=-1).sum(1)
        E = length.clone()
        # via count (score only — the count is discrete)
        via_active = assign[:, self.site_left] != assign[:, self.site_right]
        n_via = via_active.sum(1).to(torch.float64)
        E = E + prm.via_cost_mm * n_via
        bar = torch.zeros_like(E)
        bar = bar + self._pp_term(pos, assign, r_piece, p0, p1)
        bar = bar + self._pobs_term(pos, assign, r_piece, p0, p1)
        bar = bar + self._pbox_term(pos, assign, r_piece, p0, p1)
        bar = bar + self._edge_term(pos, r_piece, p0, p1)
        bar = bar + self._bead_terms(pos, assign, via_active)
        E = E + stiff * bar
        return E, {
            "length": length,
            "vias": n_via,
            "barrier": bar,
            "stiff": stiff,
        }

    def _pp_term(self, pos, assign, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.pp_i, self.pp_j
        if not len(i):
            return torch.zeros(pos.shape[0], dtype=torch.float64)
        d = D.seg_seg_dist(p0[:, i], p1[:, i], p0[:, j], p1[:, j])
        same = assign[:, i] == assign[:, j]
        K, M = assign[:, i].shape
        req = self.req_wires[self.piece_wire[i], self.piece_wire[j], :]
        req = (
            req[None, :, :]
            .expand(K, M, req.shape[-1])
            .gather(2, assign[:, i].clamp(0, req.shape[-1] - 1).unsqueeze(-1))
            .squeeze(-1)
        )
        g = d - (r_piece[i] + r_piece[j])[None, :] - req
        return (torch.relu(-g) ** 2 * same).sum(1)

    def _pobs_term(self, pos, assign, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.po_i, self.po_j
        if not len(i):
            return torch.zeros(pos.shape[0], dtype=torch.float64)
        K = pos.shape[0]
        M = len(i)
        o0 = self.obs_seg[j, :2][None, :].expand(K, M, 2).reshape(-1, 2)
        o1 = self.obs_seg[j, 2:][None, :].expand(K, M, 2).reshape(-1, 2)
        d = D.seg_seg_dist(
            p0[:, i].reshape(-1, 2), p1[:, i].reshape(-1, 2), o0, o1
        ).reshape(K, M)
        alayer = assign[:, i]  # (K, M)
        active = ((self.obs_seg_lmask[j][None, :] >> alayer) & 1) > 0
        g = (
            d
            - (r_piece[i][None, :] + self.obs_seg_r[j][None, :])
            - self.po_req[j][None, :]
        )
        return (torch.relu(-g) ** 2 * active).sum(1)

    def _pbox_term(self, pos, assign, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.pb_i, self.pb_j
        if not len(i):
            return torch.zeros(pos.shape[0], dtype=torch.float64)
        K = pos.shape[0]
        a0 = p0[:, i].reshape(K * len(i), 2)
        a1 = p1[:, i].reshape(K * len(i), 2)
        c = self.obs_box_c[j][None, :].expand(K, -1, -1).reshape(-1, 2)
        hw = self.obs_box_hw[j][None, :].expand(K, -1, -1).reshape(-1, 2)
        cs = self.obs_box_cs[j][None, :].expand(K, -1, -1).reshape(-1, 2)
        d = D.seg_box_dist(a0, a1, c, hw, cs).reshape(K, -1)
        lmask = self.obs_box_lmask[j]
        alayer = assign[:, i]
        active = (lmask[None, :] >> alayer) & 1
        g = d - r_piece[i][None, :] - self.pb_req[j][None, :]
        return (torch.relu(-g) ** 2 * (active > 0)).sum(1)

    def _edge_term(self, pos, r_piece, p0, p1):
        import torch

        from precis.pcb.pr import distance as D

        m = torch.minimum(
            D.outline_margin(p0, self.outline), D.outline_margin(p1, self.outline)
        )
        g = m - r_piece[None, :] - self.problem.edge_mm
        return (torch.relu(-g) ** 2).sum(1)

    def _bead_terms(self, pos, assign, via_active):
        """Via beads: a point on EVERY layer — vs foreign pieces on the
        piece's own layer, vs every obstacle (any layer), vs the outline."""
        import torch

        from precis.pcb.pr import distance as D

        K = pos.shape[0]
        bar = torch.zeros(K, dtype=torch.float64)
        if not len(self.site_pt):
            return bar
        beads = pos[:, self.site_pt]  # (K, S, 2)
        act = via_active.to(torch.float64)  # (K,S)
        if len(self.bp_s):
            s, j = self.bp_s, self.bp_j
            M = len(s)
            b = beads[:, s].reshape(K * M, 2)
            a0 = pos[:, self.p0][:, j].reshape(K * M, 2)
            a1 = pos[:, self.p1][:, j].reshape(K * M, 2)
            d = D.seg_seg_dist(b, b, a0, a1).reshape(K, M)
            alayer = assign[:, j]
            req = self.req_wires[self.site_wire[s], self.piece_wire[j]]
            req = (
                req[None, :, :]
                .expand(K, M, req.shape[-1])
                .gather(2, alayer.clamp(0, req.shape[-1] - 1).unsqueeze(-1))
                .squeeze(-1)
            )
            g = d - self.via_r - req
            bar = bar + (torch.relu(-g) ** 2 * act[:, s]).sum(1)
        if len(self.bos_s):
            s, j = self.bos_s, self.bos_j
            b = beads[:, s].reshape(K * len(s), 2)
            o0 = self.obs_seg[j][:, :2][None, :].expand(K, -1, -1).reshape(-1, 2)
            o1 = self.obs_seg[j][:, 2:][None, :].expand(K, -1, -1).reshape(-1, 2)
            d = D.seg_seg_dist(b, b, o0, o1).reshape(K, -1)
            g = d - (self.via_r + self.obs_seg_r[j])[None, :] - self.bos_req[j][None, :]
            bar = bar + (torch.relu(-g) ** 2 * act[:, s]).sum(1)
        if len(self.bob_s):
            s, j = self.bob_s, self.bob_j
            b = beads[:, s].reshape(K * len(s), 2)
            c = self.obs_box_c[j][None, :].expand(K, -1, -1).reshape(-1, 2)
            hw = self.obs_box_hw[j][None, :].expand(K, -1, -1).reshape(-1, 2)
            cs = self.obs_box_cs[j][None, :].expand(K, -1, -1).reshape(-1, 2)
            d = D.point_box_dist(b, c, hw, cs).reshape(K, -1)
            g = d - self.via_r - self.bob_req[j][None, :]
            bar = bar + (torch.relu(-g) ** 2 * act[:, s]).sum(1)
        # outline: bead inside the edge band
        m = D.outline_margin(beads, self.outline)
        g = m - self.via_r - self.problem.edge_mm
        bar = bar + (torch.relu(-g) ** 2 * act).sum(1)
        return bar

    # ── swept collision check ────────────────────────────────────────

    def _new_crossings(self, old, new, assign):
        """``(K,)`` count of same-layer foreign piece pairs that go from
        not-crossing to crossing between ``old`` and ``new``."""
        import torch

        from precis.pcb.pr import distance as D

        i, j = self.pp_i, self.pp_j
        if not len(i):
            return torch.zeros(old.shape[0], dtype=torch.int64)
        a0o, a1o = old[:, self.p0][:, i], old[:, self.p1][:, i]
        b0o, b1o = old[:, self.p0][:, j], old[:, self.p1][:, j]
        a0n, a1n = new[:, self.p0][:, i], new[:, self.p1][:, i]
        b0n, b1n = new[:, self.p0][:, j], new[:, self.p1][:, j]
        same = assign[:, i] == assign[:, j]
        before = D.seg_seg_cross(a0o, a1o, b0o, b1o)
        after = D.seg_seg_cross(a0n, a1n, b0n, b1n)
        return (after & ~before & same).sum(1)

    # ── the step loop ────────────────────────────────────────────────

    def run(self, assignments, pos0=None, params: RelaxParams | None = None):
        """Relax K samples from ``assignments`` (K,P) — returns
        ``(pos, energies, stats, n_vias)``."""
        import time as _t

        import torch

        prm = params or self.params
        K, P = assignments.shape
        assign = torch.tensor(assignments, dtype=torch.int64)
        pos = torch.tensor(
            np.broadcast_to(self.layout.points, (K, self.n_pts, 2)).copy()
            if pos0 is None
            else pos0,
            dtype=torch.float64,
        )
        free = self.free
        eta = torch.full((K,), prm.eta0, dtype=torch.float64)
        stats = RelaxStats()
        t0 = _t.monotonic()
        for step in range(prm.steps):
            width = min(1.0, (step + 1) / max(1, prm.grow_steps))
            pos.requires_grad_(True)
            E, _ = self.energy(pos, assign, width)
            stats.evals += 1
            g = torch.autograd.grad(E.sum(), pos)[0]
            pos = pos.detach()
            # normalized per-point step, capped displacement
            disp = -eta[:, None, None] * g
            n = disp.norm(dim=-1, keepdim=True).clamp_min(1e-12)
            cap = prm.max_disp_mm * (0.3 + 0.7 * width)
            disp = disp * torch.clamp(cap / n, max=1.0)
            cand = pos + disp * free[None, :, None]
            viol = self._new_crossings(pos, cand, assign)
            tries = 0
            while (viol > 0).any() and tries < prm.sweep_halvings:
                stats.blocked += int((viol > 0).sum())
                bad = viol > 0
                eta = torch.where(bad, eta / 2.0, eta)
                cand = torch.where(
                    bad[:, None, None],
                    pos + (cand - pos) / 2.0,
                    cand,
                )
                viol = self._new_crossings(pos, cand, assign)
                tries += 1
            still = viol > 0
            stats.dropped += int(still.sum())
            cand = torch.where(still[:, None, None], pos, cand)
            pos = cand
            eta = (eta * prm.eta_grow).clamp(max=prm.eta0 * 8)
            stats.steps += 1
        pos.requires_grad_(True)
        E, parts = self.energy(pos, assign, 1.0)
        stats.evals += 1
        stats.seconds = _t.monotonic() - t0
        return pos.detach(), E.detach(), parts, stats

    def violations(self, pos, assign):
        """Hard leftover violation depth (mm-equivalents) per sample —
        the legality read on the relaxed board at full width."""
        import torch

        with torch.no_grad():
            _, parts = self.energy(pos, assign, 1.0)
        return parts["barrier"]
