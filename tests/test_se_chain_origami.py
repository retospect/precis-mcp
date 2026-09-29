"""The rectangle-origami acceptance criterion for :mod:`precis_se.chain`.

A 24-helix × 256 bp square-lattice rectangle with a snaking scaffold and 96
staples, built **procedurally here** (the 2026-09-27 decision: no checked-in
fixture), which is ~192 segments once ``layout_chain`` has run. What it
pins:

- ``layout_chain`` under 2 s, and its children's ``[start, end]`` unit
  ranges **tiling each helix exactly** — the seam
  ``se-nucleic-realize-export`` reads;
- ``view='drc'`` under 5 s with no ``overlap_budget_exceeded``;
- **zero** ``precis_se.validate.cad_relate.clearance`` calls where both
  names are segments — the wholesale segment↔segment exclusion, measured by
  monkeypatching the kernel entry point rather than inferred from a timing.

Every number asserted here is recomputed from the design (segment counts
from the declared unit counts, the tiling from the stored records), so a
fixture typo cannot make the test pass for the wrong reason.
"""

from __future__ import annotations

import itertools
import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

import precis_se
from precis.cad import relate as cad_relate
from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se import validate as se_validate
from precis_se.chain.layout import helix_geometry, segment_ranges, units_per_segment
from precis_se.chain.relax import op_relax_chain
from precis_se.chain.vocab import SEGMENT_ROLE, chain_role
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

#: The rectangle's shape. 24 helices of 256 bp is the acceptance criterion's
#: design; 256 is 8 square-lattice repeats, so every helix is in register.
N_HELICES = 24
N_UNITS = 256
#: One square-lattice repeat = 3 turns / 32 bp; its crossover period is 8 bp.
SQUARE_TWIST = 2.0 * math.pi * 3 / 32
#: The scaffold turns at the far end of every helix, offset 255.
TURN = N_UNITS - 1
#: ``phase0``, chosen so that offset :data:`TURN` is register-correct for a
#: forward→reverse crossover to world ``+y`` — the ``row + 1`` neighbour on
#: the square lattice, which is the direction the rectangle stacks in. The
#: rule is ``phase0 + k * twist == azimuth + pi/2``
#: (:func:`precis_se.chain.nucleic.crossover_phase_rad`) and the ``+y``
#: azimuth is ``pi/2``. Every helix shares it, so every crossover rule
#: below is one piece of arithmetic.
PHASE0 = math.pi - TURN * SQUARE_TWIST
#: Staple crossover offsets. A staple runs antiparallel to the scaffold, so
#: on an even helix (scaffold forward) it is reverse and leaves from its
#: ``start``; the offsets at which a reverse backbone faces ``+y`` are
#: ``≡ 15 (mod 32)`` given :data:`PHASE0` (see the module's own derivation
#: in ``tests/test_se_chain_drc.py``: the square lattice's 3-turn repeat
#: puts one crossover site per 8 bp, and the ``+y`` one recurs per repeat).
STAPLE_OFFSETS = tuple(range(15, N_UNITS - 16, 32))
#: Nucleotides in a scaffold turn. The near-end turns (offset 0) are NOT
#: register-correct — a snake has to come back on the same face it left —
#: so the scaffold pays real loop length there, as a real origami scaffold
#: does. 7 nt of contour (5.04 nm) covers the widest turn in the design.
SCAFFOLD_LOOP_NT = 7


def _seed_se_migrations(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    _seed_se_migrations(store)
    return SeHandler(hub=hub)


def _rectangle_ops() -> list[dict[str, Any]]:
    """The whole design as one ops list — 24 helices, 1 scaffold, 96
    staples, 216 domains."""
    ops: list[dict[str, Any]] = []
    for row in range(N_HELICES):
        name = f"h{row}"
        ops.append({"op": "add_block", "name": name})
        ops.append(
            {
                "op": "declare_helix",
                "block": name,
                "n_units": N_UNITS,
                "lattice": "square",
                "row": row,
                "col": 0,
                "phase0": f"{PHASE0} rad",
            }
        )
    # The scaffold snakes: forward along an even helix, back along the odd
    # one above it, one domain per helix.
    ops.append({"op": "add_block", "name": "scaffold"})
    ops.append({"op": "declare_strand", "block": "scaffold"})
    for row in range(N_HELICES):
        domain: dict[str, Any] = {
            "op": "add_domain",
            "strand": "scaffold",
            "helix": f"h{row}",
            "start": 0,
            "end": N_UNITS,
            "forward": row % 2 == 0,
        }
        if row:
            domain["loop_before_nt"] = SCAFFOLD_LOOP_NT
        ops.append(domain)
    # Staples: one per (paired helices, crossover offset). Pairing the
    # helices off (0,1), (2,3), … keeps each staple's two 16-unit domains
    # clear of every other staple's, so the design has no parallel-occupancy
    # contradiction to report.
    for pair in range(N_HELICES // 2):
        lower, upper = 2 * pair, 2 * pair + 1
        for offset in STAPLE_OFFSETS:
            name = f"st{lower}_{offset}"
            ops.append({"op": "add_block", "name": name})
            ops.append({"op": "declare_strand", "block": name})
            ops.append(
                {
                    "op": "add_domain",
                    "strand": name,
                    "helix": f"h{lower}",
                    "start": offset,
                    "end": offset + 16,
                    "forward": False,
                }
            )
            ops.append(
                {
                    "op": "add_domain",
                    "strand": name,
                    "helix": f"h{upper}",
                    "start": offset,
                    "end": offset + 16,
                    "forward": True,
                    "loop_before_nt": 0,
                }
            )
    return ops


def _chain_of(node: Any) -> dict[str, Any]:
    """``node.chain``, narrowed — the record is what the test is about, so a
    missing one is a failure, not an ``Optional`` to thread through every
    assertion."""
    record = node.chain
    assert record is not None, f"{node.name} carries no chain record"
    return record


def _ref_id(store: Store, slug: str) -> int:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None, f"no se design {slug!r}"
    return int(ref.id)


def _rectangle_tree() -> SeTree:
    tree = SeTree()
    apply_ops(tree, _rectangle_ops())
    return tree


def test_rectangle_fixture_has_the_shape_the_criterion_names() -> None:
    tree = _rectangle_tree()
    helices = [n for n in tree.blocks if chain_role(tree.blocks[n]) == "helix"]
    strands = [n for n in tree.blocks if chain_role(tree.blocks[n]) == "strand"]
    assert len(helices) == 24
    assert len(strands) == 1 + 96  # scaffold + staples
    assert len(tree.domains) == 24 + 2 * 96
    assert len(STAPLE_OFFSETS) == 8


def test_layout_chain_is_fast_and_tiles_every_helix_exactly() -> None:
    tree = _rectangle_tree()
    started = time.perf_counter()
    apply_ops(tree, [{"op": "layout_chain"}])
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, f"layout_chain took {elapsed:.2f} s"

    segments = {
        name: _chain_of(node)
        for name, node in tree.blocks.items()
        if chain_role(node) == SEGMENT_ROLE
    }
    # One square-lattice repeat per segment is the default, so 256 bp is 8
    # children per helix — the criterion's ≈192 segments, counted not assumed.
    per = units_per_segment(helix_geometry(tree.blocks["h0"]))
    assert per == 32
    assert len(segments) == N_HELICES * (N_UNITS // per) == 192

    for row in range(N_HELICES):
        helix = f"h{row}"
        mine = sorted(
            (record for name, record in segments.items() if record["helix"] == helix),
            key=lambda r: r["ord"],
        )
        assert len(mine) == N_UNITS // per
        # Every child names its own [start, end] range …
        assert all(
            {"start", "end", "ord", "helix", "role"} <= set(record) for record in mine
        )
        # … and the ranges TILE the helix: first starts at 0, last ends at
        # the final unit, and each next start is the previous end + 1. No
        # gap, no overlap — the realizer seam.
        assert mine[0]["start"] == 0
        assert mine[-1]["end"] == N_UNITS - 1
        assert all(
            later["start"] == earlier["end"] + 1
            for earlier, later in itertools.pairwise(mine)
        )
        covered = sum(record["end"] - record["start"] + 1 for record in mine)
        assert covered == N_UNITS
        # The ranges the op wrote are the ranges the pure layout computes.
        assert [(r["start"], r["end"]) for r in mine] == segment_ranges(N_UNITS, per)
        # Each segment has both backbone anchor ports, and its envelope and
        # pose are both stamped proposed (relax_chain moves them).
        child = tree.blocks[f"{helix}.s0"]
        assert set(child.ports) == {"5p", "3p"}
        assert child.origins == {"envelope": "proposed", "pose": "proposed"}
        assert child.parent == helix
        assert child.envelope is not None and child.envelope.startswith("cyl:")


def test_relayout_replaces_rather_than_accumulates() -> None:
    tree = _rectangle_tree()
    apply_ops(tree, [{"op": "layout_chain", "block": "h0"}])
    first = sorted(n for n in tree.blocks if n.startswith("h0.s"))
    apply_ops(tree, [{"op": "layout_chain", "block": "h0"}])
    assert sorted(n for n in tree.blocks if n.startswith("h0.s")) == first
    # A coarser max_seg_len re-tiles the same helix, still exactly.
    apply_ops(tree, [{"op": "layout_chain", "block": "h0", "max_seg_len": "50 nm"}])
    coarse = sorted(n for n in tree.blocks if n.startswith("h0.s"))
    assert len(coarse) < len(first)
    records = [_chain_of(tree.blocks[n]) for n in coarse]
    assert min(r["start"] for r in records) == 0
    assert max(r["end"] for r in records) == N_UNITS - 1


def test_relax_chain_settles_the_whole_rectangle_inside_the_budget() -> None:
    """The slice-2 acceptance criterion, on the same fixture: 192 bodies,
    119 loop springs and 168 hinges settle in under 30 s."""
    tree = _rectangle_tree()
    apply_ops(tree, [{"op": "layout_chain"}])
    started = time.perf_counter()
    echo = op_relax_chain(None, tree, {"op": "relax_chain"})
    elapsed = time.perf_counter() - started
    assert elapsed < 30.0, f"relax_chain took {elapsed:.2f} s — {echo}"
    assert "settled 192 segment bodies over 24 helices" in echo
    # Every scaffold turn and every staple crossover is a spring: 23 + 96.
    assert "with 119 loop spring(s)" in echo
    assert "NOT converged" not in echo, echo
    # One pose per body, and one curve per loop that has nucleotides in it —
    # the 96 zero-nt staple crossovers get a curve too (a 0-nt loop is a real
    # connection with one bond of reach).
    assert "wrote 192 proposed pose(s) and 119 loop curve(s)" in echo
    assert all(
        node.origins.get("pose") == "proposed"
        for node in tree.blocks.values()
        if chain_role(node) == SEGMENT_ROLE
    )


def test_drc_view_is_fast_and_never_clears_two_segments(
    handler: SeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = [*_rectangle_ops(), {"op": "layout_chain"}]
    handler.put(id="rect", text=json.dumps({"ops": ops}))

    calls: list[tuple[str, str]] = []
    real = cad_relate.clearance

    def recording(design: Any, a: str, b: str, **kw: Any) -> Any:
        calls.append((a, b))
        return real(design, a, b, **kw)

    monkeypatch.setattr(cad_relate, "clearance", recording)

    started = time.perf_counter()
    drc_body = handler.get(id="rect", view="drc").body
    drc_elapsed = time.perf_counter() - started
    assert drc_elapsed < 5.0, f"view='drc' took {drc_elapsed:.2f} s"
    assert "overlap_budget_exceeded" not in drc_body
    # A correct origami on lattice spacing reports nothing actionable: the
    # header counts errors and warnings, and both are zero (the design's
    # single-stranded spans and slack scaffold turns are info-tier, and
    # ``chain_clash`` in particular must NOT fire at nominal spacing).
    assert drc_body.splitlines()[0] == "# 0 error(s), 0 warning(s)", (
        drc_body.splitlines()[0]
    )
    assert "chain_clash" not in drc_body

    # The exclusion lives on validate's undeclared-interpenetration scan, so
    # read that view too — between them they are every caller of the kernel
    # clearance query on this path.
    validate_body = handler.get(id="rect", view="validate").body
    assert "overlap_budget_exceeded" not in validate_body
    # And a derived segment's anchor ports are not reported as unconnected:
    # 192 segments × 2 ports would otherwise be 384 rows of noise.
    assert "unconnected_port" not in validate_body

    tree = persist.load_tree(store, _ref_id(store, "rect"))
    segments = {
        name for name, node in tree.blocks.items() if chain_role(node) == SEGMENT_ROLE
    }
    assert len(segments) == 192
    segment_pairs = [(a, b) for a, b in calls if a in segments and b in segments]
    assert segment_pairs == [], segment_pairs[:5]
    # The measurement is only meaningful if the exclusion is what suppressed
    # them: without it there would be 192·191/2 candidate pairs.
    assert len(segments) * (len(segments) - 1) // 2 == 18336


def test_segment_pairs_are_excluded_from_envelope_overlaps() -> None:
    """The exclusion itself, at the function the SDF budget belongs to."""
    tree = _rectangle_tree()
    apply_ops(
        tree,
        [{"op": "layout_chain", "block": "h0"}, {"op": "layout_chain", "block": "h1"}],
    )
    overlaps, cross_scale, unchecked = se_validate.envelope_overlaps(tree, budget_s=0.0)
    # budget_s=0.0 would put EVERY pair that survives the broad phase into
    # ``unchecked`` — so an empty list here says the segment pairs never
    # reached the narrow phase at all.
    assert overlaps == []
    assert cross_scale == []
    assert unchecked == []
