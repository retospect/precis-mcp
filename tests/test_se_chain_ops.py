"""The chain ops, their storage round-trip and the two views —
docs/backlog/se-nucleic-acid.md slice 1.

Store-backed where storage is the point (migration ``0015_se_chain.sql``:
``se_blocks.chain`` and the ``kind='domain'`` rows of ``se_topology``), pure
over the tree everywhere else. Theorem-style throughout: a round-trip is
asserted by rebuilding the value from the loaded tree, never by matching a
stored blob.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis.utils.units import UnitRequiredError
from precis_se import persist
from precis_se.chain import nucleic
from precis_se.chain.layout import HelixGeometry, helix_geometry, segment_ranges
from precis_se.chain.pairing import strand_length_nt, strand_letters
from precis_se.chain.vocab import ChainError, DomainSpec, validate_chain
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops, known_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


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


def _hairpin_ops() -> list[dict[str, Any]]:
    """One helix, one strand, two antiparallel domains and a 4-nt loop — a
    hairpin, the smallest design that exercises every stored field."""
    return [
        {"op": "add_block", "name": "stem"},
        {
            "op": "declare_helix",
            "block": "stem",
            "n_units": 4,
            "lattice": "honeycomb",
            "row": 0,
            "col": 0,
            "min_bend_radius": "12 nm",
            "min_gap": "0.6 nm",
        },
        {"op": "add_block", "name": "hp"},
        {"op": "declare_strand", "block": "hp", "sequence": "GGGGAAAACCCC"},
        {
            "op": "add_domain",
            "strand": "hp",
            "helix": "stem",
            "start": 0,
            "end": 4,
            "forward": True,
            "geometry": "WC",
        },
        {
            "op": "add_domain",
            "strand": "hp",
            "helix": "stem",
            "start": 0,
            "end": 4,
            "forward": False,
            "loop_before_nt": 4,
            "overrides": {"2": "W-W-cis"},
        },
    ]


# ── the op roster ───────────────────────────────────────────────────────


def test_the_seven_pure_ops_are_on_the_live_roster() -> None:
    assert {
        "declare_helix",
        "declare_strand",
        "add_domain",
        "set_domain",
        "remove_domain",
        "clear_chain",
        "layout_chain",
    } <= known_ops()


def test_remove_domain_is_destructive_but_set_domain_is_not() -> None:
    from precis_web.design_turn import DESTRUCTIVE_SE_OPS

    assert "remove_domain" in DESTRUCTIVE_SE_OPS
    # ``clear_chain`` is not, matching ``clear_dof``/``clear_build_frame``:
    # un-declaring a facet is redoing a decision, not undoing one.
    assert "clear_chain" not in DESTRUCTIVE_SE_OPS
    # ``set_domain`` exists precisely so a 1 bp register edit needs no human
    # Apply, so it must be neither destructive nor handler-level.
    from precis_se.atomic.apply import HANDLER_LEVEL_OPS

    assert "set_domain" not in DESTRUCTIVE_SE_OPS
    assert "set_domain" not in HANDLER_LEVEL_OPS


# ── units ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("min_bend_radius", 1e-8),
        ("min_gap", 5e-10),
    ],
)
def test_helix_lengths_refuse_bare_numbers(key: str, value: float) -> None:
    tree = SeTree()
    with pytest.raises(UnitRequiredError):
        apply_ops(
            tree,
            [
                {"op": "add_block", "name": "h"},
                {
                    "op": "declare_helix",
                    "block": "h",
                    "n_units": 21,
                    "lattice": "honeycomb",
                    key: value,
                },
            ],
        )


def test_waypoints_refuse_bare_numbers_but_store_metres() -> None:
    tree = SeTree()
    base = [{"op": "add_block", "name": "h"}]
    with pytest.raises(UnitRequiredError):
        apply_ops(
            tree,
            [
                *base,
                {
                    "op": "declare_helix",
                    "block": "h",
                    "n_units": 4,
                    "path": {"waypoints": [[0, 0, 0], [0, 0, 5e-9]]},
                },
            ],
        )
    tree = SeTree()
    apply_ops(
        tree,
        [
            *base,
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 4,
                "path": {"waypoints": [[0, 0, 0], ["0 nm", "0 nm", "5 nm"]]},
            },
        ],
    )
    # Stored metres, and the zero components round-trip as exact zeros (a
    # bare 0 is the one length a unit cannot disambiguate).
    assert _chain_of(tree.blocks["h"])["path"]["waypoints_m"] == [
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 5e-9],
    ]


def test_max_seg_len_refuses_a_bare_number() -> None:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 42,
                "lattice": "honeycomb",
            },
        ],
    )
    with pytest.raises(UnitRequiredError):
        apply_ops(tree, [{"op": "layout_chain", "block": "h", "max_seg_len": 7e-9}])
    apply_ops(tree, [{"op": "layout_chain", "block": "h", "max_seg_len": "7 nm"}])
    # 7 nm of a 0.334 nm rise is 20 whole units, so 42 units is 3 segments.
    assert sorted(n for n in tree.blocks if n.startswith("h.s")) == [
        "h.s0",
        "h.s1",
        "h.s2",
    ]


def test_helix_lengths_store_metres() -> None:
    tree = SeTree()
    apply_ops(tree, _hairpin_ops())
    record = _chain_of(tree.blocks["stem"])
    assert record["min_bend_radius_m"] == pytest.approx(12e-9)
    assert record["min_gap_m"] == pytest.approx(0.6e-9)
    # And the authored limits are what the geometry reads, not the defaults.
    geom = helix_geometry(tree.blocks["stem"])
    assert geom.bend_authored and geom.gap_authored
    assert geom.min_bend_radius_m == pytest.approx(12e-9)
    assert geom.min_gap_m == pytest.approx(0.6e-9)
    assert geom.min_bend_radius_m != nucleic.DEFAULT_MIN_BEND_RADIUS_M


# ── _helix_path's lattice-site straight run ──────────────────────────────
#
# ``_helix_path``'s ``max(n_units - 1, 1) * motif.rise`` is private; pinned
# here as a theorem over ``helix_geometry``'s PUBLIC unit origins instead
# (module docstring's own contract): for n_units >= 2 the straight run is
# exactly (n_units - 1) * rise and the last unit's origin lands exactly at
# the path end; for n_units == 1 the run is one rise long as a dummy
# extension (a path needs 2 samples for a tangent) with the single unit at
# the path start and nothing placed on the extra rise.


def _lattice_helix(n_units: int) -> HelixGeometry:
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "h"},
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": n_units,
                "lattice": "square",
                "row": 0,
                "col": 0,
            },
        ],
    )
    return helix_geometry(tree.blocks["h"])


def test_unit_origins_are_exactly_one_rise_apart() -> None:
    for n_units in (2, 5):
        geom = _lattice_helix(n_units)
        rise = geom.motif.rise
        origins = geom.units.origins
        assert len(origins) == n_units
        for k in range(n_units - 1):
            gap = float(np.linalg.norm(origins[k + 1] - origins[k]))
            assert gap == pytest.approx(rise, rel=1e-9), (n_units, k, gap, rise)
        span = float(np.linalg.norm(origins[-1] - origins[0]))
        assert span == pytest.approx((n_units - 1) * rise, rel=1e-9)
        # The straight run ends exactly ON the last unit's origin: no unit is
        # placed past it, so a longer path would be a phantom extension that
        # `sample_at` hides (the origins alone cannot see it).
        assert float(geom.path.s[-1]) == pytest.approx((n_units - 1) * rise, rel=1e-9)


def test_one_unit_helix_places_its_single_origin_at_the_path_start() -> None:
    geom = _lattice_helix(1)
    assert len(geom.units.origins) == 1
    assert np.allclose(geom.units.origins[0], geom.path.points[0])
    # One rise of dummy extension, and exactly one — a path needs two samples
    # to have a tangent at all.
    assert float(geom.path.s[-1]) == pytest.approx(geom.motif.rise, rel=1e-9)


# ── vetting ─────────────────────────────────────────────────────────────


def test_rejections_name_what_was_wrong() -> None:
    tree = SeTree()
    apply_ops(
        tree, [{"op": "add_block", "name": "h"}, {"op": "add_block", "name": "s"}]
    )
    with pytest.raises(OpError, match="needs a centre line"):
        apply_ops(tree, [{"op": "declare_helix", "block": "h", "n_units": 21}])
    with pytest.raises(OpError, match="unknown lattice"):
        apply_ops(
            tree,
            [{"op": "declare_helix", "block": "h", "n_units": 21, "lattice": "hex"}],
        )
    with pytest.raises(OpError, match="reserved hook"):
        apply_ops(
            tree,
            [
                {
                    "op": "declare_helix",
                    "block": "h",
                    "n_units": 21,
                    "lattice": "honeycomb",
                    "register": {"lattice": "honeycomb", "insertions": [3]},
                }
            ],
        )
    apply_ops(
        tree,
        [
            {
                "op": "declare_helix",
                "block": "h",
                "n_units": 21,
                "lattice": "honeycomb",
            },
            {"op": "declare_strand", "block": "s"},
        ],
    )
    with pytest.raises(OpError, match="needs 'forward'"):
        apply_ops(
            tree,
            [{"op": "add_domain", "strand": "s", "helix": "h", "start": 0, "end": 4}],
        )
    with pytest.raises(OpError, match="must exceed"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_domain",
                    "strand": "s",
                    "helix": "h",
                    "start": 4,
                    "end": 4,
                    "forward": True,
                }
            ],
        )
    with pytest.raises(OpError, match="unknown pair geometry"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_domain",
                    "strand": "s",
                    "helix": "h",
                    "start": 0,
                    "end": 4,
                    "forward": True,
                    "geometry": "W-Q-cis",
                }
            ],
        )
    with pytest.raises(OpError, match="FIRST domain"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_domain",
                    "strand": "s",
                    "helix": "h",
                    "start": 0,
                    "end": 4,
                    "forward": True,
                    "loop_before_nt": 3,
                }
            ],
        )
    with pytest.raises(OpError, match="outside the DNA alphabet"):
        apply_ops(tree, [{"op": "declare_strand", "block": "s", "sequence": "ACGX"}])
    with pytest.raises(OpError, match="must be different blocks"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_domain",
                    "strand": "s",
                    "helix": "s",
                    "start": 0,
                    "end": 4,
                    "forward": True,
                }
            ],
        )


def test_geometry_aliases_canonicalise() -> None:
    assert nucleic.canonical_geometry("WC") == "W-W-cis"
    assert nucleic.canonical_geometry("H-W-cis") == "W-H-cis"
    assert nucleic.canonical_geometry("reverse-Hoogsteen") == "W-H-trans"
    assert nucleic.canonical_geometry("nonsense") is None
    assert len(nucleic.FAMILIES) == 12
    assert set(nucleic.ALLOWED_PAIRS) == set(nucleic.FAMILIES)


def test_validate_chain_rejects_a_stray_key() -> None:
    with pytest.raises(ChainError, match="unknown key"):
        validate_chain({"role": "strand", "sequence": "AC", "colour": "red"})


# ── route bookkeeping ───────────────────────────────────────────────────


def test_ordinals_are_assigned_and_close_up_after_a_removal() -> None:
    tree = SeTree()
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "h"},
        {"op": "declare_helix", "block": "h", "n_units": 21, "lattice": "honeycomb"},
        {"op": "add_block", "name": "s"},
        {"op": "declare_strand", "block": "s"},
    ]
    for i in range(3):
        domain: dict[str, Any] = {
            "op": "add_domain",
            "strand": "s",
            "helix": "h",
            "start": i * 5,
            "end": i * 5 + 4,
            "forward": i % 2 == 0,
        }
        if i:
            domain["loop_before_nt"] = 2
        ops.append(domain)
    apply_ops(tree, ops)
    assert [d.ord for d in tree.domains] == [0, 1, 2]

    apply_ops(tree, [{"op": "remove_domain", "strand": "s", "ord": 0}])
    assert [d.ord for d in tree.domains] == [0, 1]
    # The domain that became the new 5' end has no preceding exit to reach
    # from any more, so its loop is dropped rather than left dangling.
    assert tree.domains[0].loop_before_nt is None
    assert tree.domains[1].loop_before_nt == 2

    with pytest.raises(OpError, match="no domain #7"):
        apply_ops(tree, [{"op": "remove_domain", "strand": "s", "ord": 7}])


def _tile(phase0: float) -> SeTree:
    """A four-helix square-lattice ribbon with four register-correct 0-nt
    crossovers — the dogfood's tile, rebuilt here so the ``set_domain`` test
    hits the gap the dogfood hit (moving one crossover by 1 bp)."""
    tree = SeTree()
    ops: list[dict[str, Any]] = []
    for col in range(4):
        ops.append({"op": "add_block", "name": f"h{col}"})
        ops.append(
            {
                "op": "declare_helix",
                "block": f"h{col}",
                "n_units": 32,
                "lattice": "square",
                "row": 0,
                "col": col,
                "phase0": f"{phase0} rad",
            }
        )
    for i, (a, b) in enumerate((("h0", "h1"), ("h1", "h2"), ("h2", "h3"))):
        strand = f"s{i}"
        ops.append({"op": "add_block", "name": strand})
        ops.append({"op": "declare_strand", "block": strand})
        ops.append(
            {
                "op": "add_domain",
                "strand": strand,
                "helix": a,
                "start": 0,
                "end": 8,
                "forward": True,
            }
        )
        ops.append(
            {
                "op": "add_domain",
                "strand": strand,
                "helix": b,
                "start": 0,
                "end": 8,
                "forward": False,
                "loop_before_nt": 0,
            }
        )
    apply_ops(tree, ops)
    return tree


def test_set_domain_moves_one_crossover_by_one_bp_and_back() -> None:
    from precis_se.chain.drc import findings

    # phase0 that makes offset 7 register-correct for a forward→reverse
    # crossover to the ``+x`` neighbour (azimuth 0): the rule is
    # ``phase0 + k * twist == azimuth + pi/2``.
    twist = 2.0 * math.pi * 3 / 32
    tree = _tile(math.pi / 2 - 7 * twist)
    assert [f.rule for f in findings(tree) if f.severity == "error"] == []

    # ONE op moves the landing domain a base pair along — the dogfood spent
    # 11 ops (clear_chain + a full re-route of three strands) on this.
    apply_ops(
        tree, [{"op": "set_domain", "strand": "s1", "ord": 1, "start": 1, "end": 9}]
    )
    short = [f for f in findings(tree) if f.rule == "chain_loop_short"]
    assert [f.subject for f in short] == ["s1#0→#1"]
    moved = next(d for d in tree.domains if d.strand == "s1" and d.ord == 1)
    assert (moved.start, moved.end) == (1, 9)
    # Everything else about the row is untouched — absent means unchanged.
    assert moved.forward is False and moved.loop_before_nt == 0

    apply_ops(
        tree, [{"op": "set_domain", "strand": "s1", "ord": 1, "start": 0, "end": 8}]
    )
    assert [f.rule for f in findings(tree) if f.severity == "error"] == []


def test_set_domain_refuses_what_it_must() -> None:
    tree = _tile(0.0)
    with pytest.raises(OpError, match="unknown key"):
        apply_ops(tree, [{"op": "set_domain", "strand": "s0", "ord": 0, "nonsense": 1}])
    with pytest.raises(OpError, match="'ord' identifies the row"):
        apply_ops(tree, [{"op": "set_domain", "strand": "s0", "ord": 0, "new_ord": 1}])
    with pytest.raises(OpError, match="needs 'ord'"):
        apply_ops(tree, [{"op": "set_domain", "strand": "s0", "start": 1}])
    with pytest.raises(OpError, match="no domain #9"):
        apply_ops(tree, [{"op": "set_domain", "strand": "s0", "ord": 9, "start": 1}])
    # A loop cannot be smuggled onto the 5' end: build_domain refuses it for
    # set_domain exactly as it does for add_domain.
    with pytest.raises(OpError, match="FIRST domain"):
        apply_ops(
            tree, [{"op": "set_domain", "strand": "s0", "ord": 0, "loop_before_nt": 2}]
        )
    with pytest.raises(OpError, match="must exceed"):
        apply_ops(tree, [{"op": "set_domain", "strand": "s0", "ord": 0, "end": 0}])
    with pytest.raises(OpError, match="one block cannot be both"):
        apply_ops(tree, [{"op": "set_domain", "strand": "s0", "ord": 0, "helix": "s0"}])


def test_set_domain_clears_a_field_with_an_explicit_null() -> None:
    tree = _tile(0.0)
    apply_ops(
        tree, [{"op": "set_domain", "strand": "s0", "ord": 1, "geometry": "wobble"}]
    )
    assert tree.domains[1].geometry == "W-W-cis"
    apply_ops(tree, [{"op": "set_domain", "strand": "s0", "ord": 1, "geometry": None}])
    assert tree.domains[1].geometry is None
    # …and the loop survives both edits, because it was never named.
    assert tree.domains[1].loop_before_nt == 0


def test_clear_chain_cascades_both_ways() -> None:
    tree = SeTree()
    apply_ops(tree, [*_hairpin_ops(), {"op": "layout_chain"}])
    assert any(n.startswith("stem.s") for n in tree.blocks)
    assert len(tree.domains) == 2

    apply_ops(tree, [{"op": "clear_chain", "block": "stem"}])
    assert tree.blocks["stem"].chain is None
    # The helix's segments and every domain routed along it go with it.
    assert not any(n.startswith("stem.s") for n in tree.blocks)
    assert tree.domains == []

    tree = SeTree()
    apply_ops(tree, _hairpin_ops())
    apply_ops(tree, [{"op": "clear_chain", "block": "hp"}])
    assert tree.blocks["hp"].chain is None
    assert tree.domains == []


def test_redeclaring_a_helix_drops_its_stale_segments() -> None:
    tree = SeTree()
    apply_ops(tree, [*_hairpin_ops(), {"op": "layout_chain"}])
    before = sorted(n for n in tree.blocks if n.startswith("stem.s"))
    assert before
    apply_ops(
        tree,
        [
            {
                "op": "declare_helix",
                "block": "stem",
                "n_units": 42,
                "lattice": "honeycomb",
            }
        ],
    )
    assert not any(n.startswith("stem.s") for n in tree.blocks)
    apply_ops(tree, [{"op": "layout_chain", "block": "stem"}])
    records = [tree.blocks[n].chain for n in tree.blocks if n.startswith("stem.s")]
    assert len(records) == 2  # 42 units, one 21-unit honeycomb repeat each
    assert segment_ranges(42, 21) == [(0, 20), (21, 41)]


def test_sequence_letters_account_for_the_loops() -> None:
    tree = SeTree()
    apply_ops(tree, _hairpin_ops())
    route = sorted(tree.domains, key=lambda d: d.ord)
    assert strand_length_nt(route) == 12  # 4 + 4-nt loop + 4
    letters = strand_letters("GGGGAAAACCCC", route)
    # The stem's two domains take the first four and the last four letters;
    # the loop consumes the AAAA in between, which is the whole point of
    # counting loops in the cursor.
    assert [letters[(0, i)] for i in range(4)] == ["G", "G", "G", "G"]
    assert [letters[(1, i)] for i in (3, 2, 1, 0)] == ["C", "C", "C", "C"]


# ── storage round-trip ──────────────────────────────────────────────────


def test_chain_and_domains_round_trip_through_the_store(
    handler: SeHandler, store: Store
) -> None:
    handler.put(
        id="hairpin",
        text=json.dumps({"ops": [*_hairpin_ops(), {"op": "layout_chain"}]}),
    )
    loaded = persist.load_tree(store, _ref_id(store, "hairpin"))

    # The helix record survives whole, including the authored limits.
    stem = _chain_of(loaded.blocks["stem"])
    assert stem["role"] == "helix"
    assert stem["min_bend_radius_m"] == pytest.approx(12e-9)
    assert stem["register"] == {"lattice": "honeycomb"}
    assert loaded.blocks["hp"].chain == {
        "role": "strand",
        "nucleic": "DNA",
        "sequence": "GGGGAAAACCCC",
    }
    # Both domain rows come back in ord order with every meta field.
    assert [d.ord for d in loaded.domains] == [0, 1]
    first, second = loaded.domains
    assert (first.strand, first.helix, first.forward) == ("hp", "stem", True)
    assert first.geometry == "W-W-cis"
    assert second.loop_before_nt == 4
    assert second.overrides == {"2": "W-W-cis"}
    assert second.forward is False
    # Segment children round-trip their range, and it still tiles.
    segments = sorted(
        (n for n in loaded.blocks if n.startswith("stem.s")),
        key=lambda n: _chain_of(loaded.blocks[n])["ord"],
    )
    ranges = [
        (_chain_of(loaded.blocks[n])["start"], _chain_of(loaded.blocks[n])["end"])
        for n in segments
    ]
    assert ranges == segment_ranges(4, 21) == [(0, 3)]
    assert loaded.blocks[segments[0]].origins == {
        "envelope": "proposed",
        "pose": "proposed",
    }


def test_the_json_snapshot_round_trip_carries_domains(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="hairpin", text=json.dumps({"ops": _hairpin_ops()}))
    loaded = persist.load_tree(store, _ref_id(store, "hairpin"))
    restored = persist.tree_from_json(persist.tree_to_json(loaded))
    assert [
        (d.strand, d.helix, d.ord, d.forward, d.start, d.end, d.loop_before_nt)
        for d in restored.domains
    ] == [
        (d.strand, d.helix, d.ord, d.forward, d.start, d.end, d.loop_before_nt)
        for d in loaded.domains
    ]
    assert restored.blocks["stem"].chain == loaded.blocks["stem"].chain


def test_an_edit_can_add_a_domain_to_a_stored_strand(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="hairpin", text=json.dumps({"ops": _hairpin_ops()}))
    handler.edit(
        id="hairpin",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_domain",
                        "strand": "hp",
                        "helix": "stem",
                        "start": 0,
                        "end": 2,
                        "forward": True,
                        "loop_before_nt": 1,
                    }
                ]
            }
        ),
    )
    loaded = persist.load_tree(store, _ref_id(store, "hairpin"))
    assert [d.ord for d in loaded.domains] == [0, 1, 2]
    assert loaded.domains[2].loop_before_nt == 1


def test_removing_a_strand_block_takes_its_route(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="hairpin", text=json.dumps({"ops": _hairpin_ops()}))
    handler.edit(
        id="hairpin",
        text=json.dumps({"ops": [{"op": "remove_block", "block": "hp"}]}),
    )
    loaded = persist.load_tree(store, _ref_id(store, "hairpin"))
    assert loaded.domains == []


# ── views ───────────────────────────────────────────────────────────────


def test_chain_view_reports_the_derived_occupancy(handler: SeHandler) -> None:
    handler.put(
        id="hairpin",
        text=json.dumps({"ops": [*_hairpin_ops(), {"op": "layout_chain"}]}),
    )
    body = handler.get(id="hairpin", view="chain").body
    assert "## helices" in body
    assert "## strands" in body
    assert "derived pairing" in body
    # The honeycomb repeat IS B-DNA's own 10.5 bp/turn, so the motif is
    # unretuned here (the square lattice is the one that renames it).
    assert "B-DNA (DNA)" in body
    # Four offsets, each occupied by both of the hairpin's domains → four
    # pairs and no single-stranded span.
    assert "4 paired offset(s)" in body
    assert "every occupied offset is paired" in body
    # The strand row names its route and its loop.
    assert "stem[0:4]" in body
    assert "4 nt" in body


def test_chain_view_segments_column_says_not_laid_out_before_layout_chain(
    handler: SeHandler,
) -> None:
    # No layout_chain yet: the segments cell must not claim a tiling exists.
    # The stem is 4 units and the honeycomb's repeat (the default
    # max_seg_len) is 21, so the one prospective segment is 4 units long —
    # the cell reports the range's own span, never the cap it was cut with.
    handler.put(id="hairpin", text=json.dumps({"ops": _hairpin_ops()}))
    body = handler.get(id="hairpin", view="chain").body
    assert "not laid out" in body
    assert "would be 1 × 4 units" in body
    assert "21 units" not in body


def test_chain_view_segments_column_reports_real_tiling_after_layout_chain(
    handler: SeHandler,
) -> None:
    handler.put(
        id="hairpin",
        text=json.dumps({"ops": [*_hairpin_ops(), {"op": "layout_chain"}]}),
    )
    body = handler.get(id="hairpin", view="chain").body
    assert "not laid out" not in body
    # Read off the stored children's own [start, end], so the span is the
    # helix's 4 units — never the 21-unit cap.
    assert "1 × 4 units" in body
    assert "21 units" not in body


def test_topology_view_gains_the_domain_rows(handler: SeHandler) -> None:
    handler.put(id="hairpin", text=json.dumps({"ops": _hairpin_ops()}))
    body = handler.get(id="hairpin", view="topology").body
    assert "## domains" in body
    assert "[0:4)" in body
    assert "forward" in body and "reverse" in body
    assert "W-W-cis" in body
    # The ``loop_before`` cell: the hairpin's first domain (ord 0) carries
    # no loop_before_nt (there is no preceding exit to reach from) and
    # renders "—"; its second domain declares ``loop_before_nt=4`` and
    # renders "4 nt". ``is → is not`` on the None-check swaps which domain
    # gets which cell — under the mutant the loopless domain 0 renders the
    # bare "None nt" and the real 4-nt loop renders "—" instead.
    assert "4 nt" in body
    assert "None nt" not in body


def test_chain_view_is_registered_and_takes_no_args(handler: SeHandler) -> None:
    from precis.errors import BadInput

    handler.put(id="hairpin", text=json.dumps({"ops": _hairpin_ops()}))
    assert "chain" in SeHandler.spec.views
    with pytest.raises(BadInput, match="unknown args key"):
        handler.get(id="hairpin", view="chain", args={"block": "stem"})


def test_an_empty_design_renders_the_chain_view(handler: SeHandler) -> None:
    handler.put(id="bare", text=json.dumps({"ops": [{"op": "add_block", "name": "b"}]}))
    body = handler.get(id="bare", view="chain").body
    assert "(none)" in body
    assert "no domains" in body


# ── the kernel's own numbers, as this binding states them ────────────────


def test_the_lattice_mapping_puts_neighbours_at_the_spacing() -> None:
    for kind in ("honeycomb", "square"):
        for row, col in ((0, 0), (1, 1), (2, 3)):
            here = nucleic.site_position(kind, row, col)
            distances = []
            for d_row in (-1, 0, 1):
                for d_col in (-1, 0, 1):
                    if (d_row, d_col) == (0, 0):
                        continue
                    there = nucleic.site_position(kind, row + d_row, col + d_col)
                    distances.append(math.dist(here, there))
            nearest = min(distances)
            assert nearest == pytest.approx(nucleic.HELIX_SPACING_M, rel=1e-12)
            # Honeycomb has three nearest neighbours, square four.
            count = sum(
                1
                for d in distances
                if d == pytest.approx(nucleic.HELIX_SPACING_M, rel=1e-9)
            )
            assert count == (3 if kind == "honeycomb" else 4)


def test_the_square_lattice_retunes_the_motif_and_honeycomb_does_not() -> None:
    b_dna = nucleic.MOTIFS["B-DNA"]
    assert nucleic.lattice_motif(b_dna, "honeycomb") is b_dna
    square = nucleic.lattice_motif(b_dna, "square")
    assert square is not b_dna
    # 32 bp / 3 turns is 10.67 bp/turn — a 1.6 % over-wind on the solution
    # value, which is the documented design twist of square-lattice origami.
    assert 2.0 * math.pi / square.twist == pytest.approx(32 / 3)
    assert square.twist / b_dna.twist == pytest.approx(10.5 / (32 / 3))
    assert nucleic.lattice_motif(b_dna, None) is b_dna


def test_a_domain_spec_knows_its_own_ends() -> None:
    forward = DomainSpec(strand="s", helix="h", ord=0, forward=True, start=4, end=9)
    assert (forward.entry_offset, forward.exit_offset) == (4, 8)
    assert list(forward.offsets()) == [4, 5, 6, 7, 8]
    reverse = DomainSpec(strand="s", helix="h", ord=1, forward=False, start=4, end=9)
    assert (reverse.entry_offset, reverse.exit_offset) == (8, 4)
    assert list(reverse.offsets()) == [8, 7, 6, 5, 4]
    assert forward.n_units == reverse.n_units == 5
