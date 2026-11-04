"""The slice-2 hex chamber (tests/test_se_flow.py's cfdtest geometry) as
an se design: the chamber void as a ``field:`` envelope (an elongated
hexagon with drilled floor holes is not one DSL primitive), two port
plates at the tips carrying the ``patch:`` ports, and the ``flow:``
chamber-spec pocket. Shared by the web Flow-tab test and the dev-DB seed
the browser check uses.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from precis.cad import fieldops
from precis.cad.primitives import Field

# cfdtest geometry (metres) — the same numbers as tests/test_se_flow.py.
L, W, TIP, H = 0.1, 0.03, 0.015, 1e-3
HOLE_R = 2.5e-3
PORT_W = 0.006
D_V_STAR = 0.06 * (L / H)
HOLES = np.array(
    [
        [0.030, 0.015, 2.5e-3],
        [0.045, 0.008, 2.0e-3],
        [0.045, 0.022, 2.0e-3],
        [0.060, 0.015, 3.0e-3],
        [0.075, 0.009, 1.5e-3],
        [0.075, 0.021, 1.5e-3],
    ]
)
PLATE = 0.002  # port plate thickness along x (m)


def hex_chamber_field(pitch: float = 2.5e-4) -> Field:
    """The chamber void (hex prism ``0 ≤ z ≤ H`` union the hole
    cylinders down to ``-depth``) as an exact field, voxel centres on a
    ``pitch`` lattice so the zero set lands on the walls."""
    z_lo = -float(HOLES[:, 2].max())
    nx = round(L / pitch)
    ny = round(W / pitch)
    nz = round((H - z_lo) / pitch)
    xc = (np.arange(nx) + 0.5) * pitch
    yc = (np.arange(ny) + 0.5) * pitch
    zc = z_lo + (np.arange(nz) + 0.5) * pitch
    X, Y = np.meshgrid(xc, yc, indexing="ij")
    half = W / 2
    plan = np.ones_like(X, dtype=bool)
    plan &= (X >= TIP) | (np.abs(Y - half) <= half * (X / TIP))
    plan &= (X <= L - TIP) | (np.abs(Y - half) <= half * ((L - X) / TIP))
    occ = np.zeros((nx, ny, nz), dtype=bool)
    occ[:, :, (zc >= 0.0) & (zc <= H)] = plan[:, :, None]
    for hx, hy, hd in HOLES:
        disc = plan & ((X - hx) ** 2 + (Y - hy) ** 2 <= HOLE_R**2)
        occ[:, :, (zc >= -hd) & (zc <= H)] |= disc[:, :, None]
    return fieldops.redistance(occ, pitch, (xc[0], yc[0], zc[0]))


def hex_chamber_ops(field_sha: str) -> list[dict[str, Any]]:
    """se ops for the chamber block, the two tip port plates and the
    ``flow:`` pocket (plug profile — the cfdtest reproduction case)."""
    inlet = f"patch:pin.side1@0,0+{PORT_W}x{H}"
    outlet = f"patch:pout.side3@0,0+{PORT_W}x{H}"
    plate = f"box:w{PLATE}d{PORT_W * 2}h{H}"
    return [
        {"op": "add_block", "name": "chamber", "envelope": f"field:{field_sha}"},
        {
            "op": "add_block",
            "name": "pin",
            "pose": f"[{-PLATE / 2:g} {W / 2:g} 0] m",
            "envelope": plate,
        },
        {
            "op": "add_block",
            "name": "pout",
            "pose": f"[{L + PLATE / 2:g} {W / 2:g} 0] m",
            "envelope": plate,
        },
        {
            "op": "add_pocket",
            "block": "chamber",
            "name": "flow",
            "shape": f"flow:in={inlet};out={outlet};dv={D_V_STAR:g};profile=plug",
            "regions": [],
        },
    ]


def seed_flow_measurands() -> None:
    """The taxonomy the flow measures resolve through: the per-test
    TRUNCATE empties it, so a test that writes a measurand re-runs the
    seed migrations in order: the term tree, 0231's species nodes, then
    0274, whose eight rows skip the four 0231 already seeded."""
    import psycopg

    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            for name in (
                "0174_taxon_seed.sql",
                "0182_se_measurand_seed.sql",
                "0231_se_species_measurands.sql",
                "0274_se_flow_measurands.sql",
            ):
                _execute_dump_sql(
                    cur, (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
                )
