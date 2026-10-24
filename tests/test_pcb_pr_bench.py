"""Slice-1 bench for the pcb.pr prototype (docs/backlog/pcb-place-route-
datamodel.md §Slices 1). Measurement only: reports legality, DRC, vias
and length vs vroute on the same IR, loop convergence, swept-block rate
and CPU evals/s; asserts only correctness (finite state, determinism,
IR validity). Quality numbers are reported, not asserted."""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("torch")

from precis.pcb import realize, session
from precis.pcb.capabilities import capability_for
from precis.pcb.pr import pipeline
from precis.pcb.pr import state as prstate
from precis.pcb.pr.pipeline import PrParams
from precis.pcb.pr.relax import RelaxParams
from precis.workers.job_types.pcb_route import _graph_net_rules
from tests.fixtures.pcb import qfp_fanout as qf

SMALL = qf.FanoutSpec(
    layers=("F.Cu", "B.Cu"), pins_per_side=6, n_nets=12, sources="grid", seed=1
)
MAIN = qf.FanoutSpec(
    layers=("F.Cu", "B.Cu"), pins_per_side=10, n_nets=40, sources="grid", seed=1
)


def _built(spec):
    built = qf.build(spec)
    cfg = realize.RealizeConfig(
        fab_caps=capability_for("2layer"), class_rules=built.graph["net_classes"]
    )
    prob = prstate.build_problem(
        built.ir,
        outline=qf.outline(spec),
        config=cfg,
        footprints=built.footprints,
    )
    return built, cfg, prob


def _drc(built, spec, rres):
    findings = session.routed_drc_findings(
        built.ir,
        rres,
        capability=capability_for("2layer"),
        footprints=built.footprints,
        outline=qf.outline(spec),
        net_rules=_graph_net_rules(built.graph, capability_for("2layer")),
    )
    by_rule: dict[str, int] = {}
    for f in findings:
        if str(getattr(f, "rule", "")).startswith("silk_"):
            continue
        by_rule[str(getattr(f, "rule", "unknown"))] = (
            by_rule.get(str(getattr(f, "rule", "unknown")), 0) + 1
        )
    return findings, by_rule


def test_pr_energy_gradient_fd():
    """Finite-difference check of the 4c energy: autograd vs central
    differences on a handful of free coordinates (differentiation skill:
    FD is verification only)."""
    import torch

    from precis.pcb.pr import beliefs
    from precis.pcb.pr import seed as prseed
    from precis.pcb.pr.relax import Relaxer

    built, cfg, prob = _built(SMALL)
    seeded = prseed.seed(prob)
    lay = beliefs.build_layout(prob, [s.polyline for s in seeded.seeds])
    rlx = Relaxer(prob, lay, RelaxParams())
    gen = np.random.default_rng(0)
    assign = torch.tensor(
        beliefs.sample_layers(beliefs.init_q(lay, prob.n_layers), 2, gen),
        dtype=torch.int64,
    )
    pos = torch.tensor(
        np.broadcast_to(lay.points, (2, len(lay.points), 2)).copy(),
        dtype=torch.float64,
        requires_grad=True,
    )
    E, _ = rlx.energy(pos, assign, 0.5)
    assert np.isfinite(E.detach().numpy()).all()
    g = torch.autograd.grad(E.sum(), pos)[0]
    for fi in np.nonzero(lay.free)[0][:6]:
        for ax in range(2):
            d = torch.zeros_like(pos)
            d[0, fi, ax] = 1e-5
            Ep, _ = rlx.energy(pos.detach() + d, assign, 0.5)
            Em, _ = rlx.energy(pos.detach() - d, assign, 0.5)
            fd = float((Ep[0] - Em[0]) / 2e-5)
            assert abs(fd - float(g[0, fi, ax])) < 1e-4


def test_pr_swept_check_keeps_gap():
    """After a swept-checked relax the same-layer foreign-piece crossing
    count is no larger than at the seeded start — steps that create a
    crossing are rejected, never applied."""
    import torch

    from precis.pcb.pr import beliefs
    from precis.pcb.pr import seed as prseed
    from precis.pcb.pr.relax import Relaxer

    built, cfg, prob = _built(SMALL)
    seeded = prseed.seed(prob)
    lay = beliefs.build_layout(prob, [s.polyline for s in seeded.seeds])
    rlx = Relaxer(prob, lay, RelaxParams(steps=20, grow_steps=10))
    gen = np.random.default_rng(0)
    assign = beliefs.sample_layers(beliefs.init_q(lay, prob.n_layers), 2, gen)
    fixed = lay.span_allowed.sum(1) == 1
    assign[:, fixed] = np.argmax(lay.span_allowed[fixed], axis=1)[None, :]
    pos0 = torch.tensor(
        np.broadcast_to(lay.points, (2, len(lay.points), 2)).copy(),
        dtype=torch.float64,
    )
    at = torch.tensor(assign, dtype=torch.int64)
    before = rlx._new_crossings(pos0, pos0, at)  # crossings already present
    pos, E, _, stats = rlx.run(assign, pos0=pos0.numpy())
    after = rlx._new_crossings(pos, pos, at)
    assert np.isfinite(E.numpy()).all()
    assert (after <= before).all(), "swept check let a crossing appear"


@pytest.mark.slow
def test_slice1_bench_small_legal():
    """Easiest fixture: pipeline terminates, tensors finite, result
    emittable; DRC run is the legality read (reported, not asserted)."""
    built, cfg, prob = _built(SMALL)
    run = pipeline.run(
        prob, PrParams(K=8, rounds=3, relax=RelaxParams(steps=50, grow_steps=25))
    )
    assert np.isfinite(run.q).all()
    assert run.result is not None
    findings, by_rule = _drc(built, SMALL, run.result)
    print(
        f"\n[pr-bench small] unrouted={len(run.unrouted)}/{len(prob.wires)} "
        f"vias={run.via_count} wire={run.wire_mm:.1f}mm "
        f"entropy={run.entropy:.3f} hist={[round(e, 2) for e in run.entropy_hist]} "
        f"blocked={run.blocked_frac:.3f} evals/s={run.evals / max(run.relax_seconds, 1e-9):.1f} "
        f"drc={by_rule} overcap={run.over_capacity[:1]}"
    )


@pytest.mark.slow
def test_slice1_bench_deterministic():
    """Same seed twice → same emitted geometry within float tolerance."""
    built, cfg, prob = _built(SMALL)
    prm = PrParams(K=8, rounds=2, relax=RelaxParams(steps=30, grow_steps=15))
    a = pipeline.run(prob, prm)
    b = pipeline.run(prob, prm)
    assert a.via_count == b.via_count
    assert a.unrouted == b.unrouted
    assert abs(a.wire_mm - b.wire_mm) < 1e-6 * max(1.0, a.wire_mm)


@pytest.mark.slow
@pytest.mark.timeout(360)  # CI worst ~145s x2.5 for runner variance (gr503084)
def test_slice1_bench_main_pipeline():
    """Full slice-1 fixture through the pr pipeline (context only — no
    frozen-table row is 2-layer fixed-pad). rt-s3: split from the vroute
    comparison arm — the pair ran ~248 s CI, over the 240 s bar."""
    built, cfg, prob = _built(MAIN)
    run = pipeline.run(
        prob, PrParams(K=16, rounds=3, relax=RelaxParams(steps=60, grow_steps=30))
    )
    pfind, prule = _drc(built, MAIN, run.result)
    print(
        f"\n[pr-bench main] unrouted={len(run.unrouted)}/{len(prob.wires)} "
        f"vias={run.via_count} wire={run.wire_mm:.1f}mm drc={prule}\n"
        f"[loop] hist={[round(e, 2) for e in run.entropy_hist]} "
        f"blocked={run.blocked_frac:.3f} "
        f"evals/s={run.evals / max(run.relax_seconds, 1e-9):.1f} N={run.n_segments} "
        f"wall={run.wall_s:.0f}s overcap={run.over_capacity[:2]}"
    )


@pytest.mark.slow
@pytest.mark.timeout(360)  # CI worst ~145s x2.5 for runner variance (gr503084)
def test_slice1_bench_main_vroute():
    """The vroute half of the same-IR comparison, on the same fixture."""
    built, cfg, prob = _built(MAIN)
    vrres = realize.realize(built.ir, config=built.config, footprints=built.footprints)
    vfind, vrule = _drc(built, MAIN, vrres)
    v_vias = len(vrres.vias)
    v_len = sum(t.length_mm for t in vrres.tracks)
    print(
        f"\n[vroute same-IR] unrouted={len(vrres.unrouted)} vias={v_vias} "
        f"wire={v_len:.1f}mm drc={vrule}"
    )


# ---- slice 2 bench (opt-in: scripts/pcb-pr-bench sets PRECIS_PCB_PR_BENCH_OUT)


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("PRECIS_PCB_PR_BENCH_OUT"),
    reason="opt-in: scripts/pcb-pr-bench sets PRECIS_PCB_PR_BENCH_OUT",
)
def test_slice2_bench_table() -> None:
    """The §Slice 2 results table: every bench board routed by vroute
    (fixed poses) and by pr v1 (free parts) from the SAME job placement;
    asserts only that every run is finite and the table is written.
    DB-free (``pr_bench.design_graph``); ``PRECIS_PCB_PR_BENCH_BOARDS=
    nano,qfp-plaza`` picks a subset."""
    from tests.fixtures.pcb import pr_bench as pb

    which = os.environ.get("PRECIS_PCB_PR_BENCH_BOARDS")
    keep = {w.strip() for w in which.split(",") if w.strip()} if which else None
    boards = pb.bench_boards(keep)
    rows: list[pb.PrRow] = []
    notes: list[str] = []
    # the main set, then the "-GND" diagnostic rows (the star-hub ground
    # net left out on the two boards it dominates)
    jobs = [(b, b.name, frozenset[str](), False) for b in boards] + [
        (b, f"{b.name} -GND", frozenset({"GND"}), False)
        for b in boards
        if b.name in ("nano", "esp32c3-s1")
    ]
    # "seed-layers": 4b off, every sample keeps the 4a seed's layers
    jobs += [
        (b, f"{b.name} seed-layers", frozenset[str](), True)
        for b in boards
        if b.name in ("esp32c3-s1", "qfp-plaza")
    ]
    for board, label, skip, freeze in jobs:
        if freeze:
            p, run = pb.measure_pr(
                board, pb.V1Params(K=2, freeze_layers=True), label=label
            )
            assert pb.nan_free(run), label
            rows.append(p)
            notes.append(
                f"- {label}: demoted={run.demoted} crossings={run.crossings_hist}"
            )
            continue
        v = pb.measure_vroute(board, skip, label=label)
        p, run = pb.measure_pr(board, pb.V1Params(skip_nets=skip), label=label)
        assert pb.nan_free(run), label
        rows += [p, v]
        notes.append(
            f"- {label}: pr unrouted={pb.unrouted_names(run.ir, run.result)} "
            f"demoted={run.demoted} entropy={p.entropy} blocked={p.blocked_frac} "
            f"sugar={p.sugar} crossings={run.crossings_hist} levels={run.levels} "
            f"cleanup={run.cleanup_kept} evals={run.evals} "
            f"relax_s={run.relax_seconds:.1f} overcap={p.over_capacity!r}"
        )
        print(pb.render_table([p, v]))
    table = pb.render_table(rows)
    out = Path(os.environ["PRECIS_PCB_PR_BENCH_OUT"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(table + "\n\nNotes:\n" + "\n".join(notes) + "\n", encoding="utf-8")
    out.with_suffix(".json").write_text(
        json.dumps([list(r.key()) for r in rows], indent=1), encoding="utf-8"
    )
    print(table)
    assert all(r.connections > 0 for r in rows)
