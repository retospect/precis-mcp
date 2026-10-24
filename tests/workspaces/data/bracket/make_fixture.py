"""Regenerate the bracket ray fixture (structure-workspaces 2r.2) — numbers
only: a 40×40×10 mm ``base`` with four M5 holes tapped from the top
(``DEPTHS``: 8, 8, 10 through, 6 mm) and a 6 mm ``bracket`` 0.2 mm above
it with ⌀5.5 clearance holes, ports on both, bolt-axis rays down each
clearance hole and a tool-access bundle up from a1. Over the 6.2 mm
stack: h1/h2 (8 mm) fit no ISO 4762 length (12 is short, 16 bottoms
out), h3 (10 mm) fits M5 × 16, h4 (6 mm) is under 1.5 d.
``python make_fixture.py [out_dir] [--h2-dx MM]`` (``--h2-dx`` moves
``base.h2`` along x — the ray.changed case)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

XS = (-0.015, -0.005, 0.005, 0.015)  # hole x positions, m
DEPTHS = (0.008, 0.008, 0.010, 0.006)  # tapped depth from the base top, m
BASE_H, GAP, BRACKET_H = 0.010, 0.0002, 0.006
TOP = BASE_H + GAP + BRACKET_H  # bracket top, m


def _key(i: int) -> str:
    return f"b2{i:014x}"


def _frame(
    i: int,
    parent: int | None,
    qual: str,
    kind: str,
    line: int,
    occ: int = 0,
    line_in_def: int = 0,
    src: str | None = None,
) -> dict:
    return {
        "id": i,
        "key": _key(i),
        "parent": _key(parent) if parent is not None else None,
        "qual": qual,
        "file": "build.py",
        "line": line,
        "line_in_def": line_in_def,
        "src": src,
        "occ": occ,
        "kind": kind,
        "args": {},
        "made": {},
        "touched": {},
    }


def _node(name: str, op: str, config: str, loc: tuple, frame: int) -> dict:
    return {
        "name": name,
        "op": op,
        "config": config,
        "component": "part",
        "loc": list(loc),
        "rot": [0, 0, 0],
        "frame": frame,
    }


def write(out: Path, h2_dx: float = 0.0) -> None:
    (out / "blocks").mkdir(parents=True, exist_ok=True)
    base = [_node("plate", "add", f"box:w0.04d0.04h{BASE_H:g}", (0, 0, 0), 2)]
    for k, (x, depth) in enumerate(zip(XS, DEPTHS, strict=True), start=1):
        dx = h2_dx if k == 2 else 0.0
        if depth >= BASE_H:  # through: overshoot both faces
            z0, h = -0.001, BASE_H + 0.002
        else:  # blind from the top, overshooting the top face only
            z0, h = BASE_H - depth, depth + 0.001
        base.append(_node(f"h{k}", "cut", f"cyl:r0.0025h{h:g}", (x + dx, 0, z0), 3))
    bracket = [
        _node("pad", "add", f"box:w0.04d0.04h{BRACKET_H:g}", (0, 0, BASE_H + GAP), 4)
    ]
    for k, x in enumerate(XS, start=1):
        bracket.append(
            _node(
                f"hole{k}", "cut", "cyl:r0.00275h0.008", (x, 0, BASE_H + GAP - 0.001), 5
            )
        )
    for name, nodes in (("base", base), ("bracket", bracket)):
        (out / "blocks" / f"{name}.cad.json").write_text(
            json.dumps({"schema": 1, "nodes": nodes, "components": ["part"]}, indent=1)
            + "\n",
            encoding="utf-8",
        )
    base_ports = [
        {
            "name": f"h{k}",
            "roles": ["fastener"],
            "direction": [0, 0, -1],
            "pose": f"[{(x + (h2_dx if k == 2 else 0.0)) * 1e3:g} 0 {BASE_H * 1e3:g}] mm",
            "annotations": {"thread": "M5x0.8", "depth": depth},
            "frame": 3,
        }
        for k, (x, depth) in enumerate(zip(XS, DEPTHS, strict=True), start=1)
    ]
    bracket_ports = [
        {
            "name": f"a{k}",
            "roles": ["fastener"],
            "direction": [0, 0, -1],
            "pose": f"[{x * 1e3:g} 0 {TOP * 1e3:g}] mm",
            "annotations": {"diameter": 0.0055},
            "frame": 5,
        }
        for k, x in enumerate(XS, start=1)
    ]
    rays = [
        {
            "name": f"bolt-a{k}",
            "block": "bracket",
            "in": f"port:a{k}",
            "origin": [0, 0, -0.003],
            "direction": [0, 0, 1],
            "length": 0.028,
            "kind": "bolt_axis",
            "anchor": [f"bracket.a{k}", f"base.h{k}"],
            "frame": 6,
        }
        for k in range(1, len(XS) + 1)
    ]
    rays.append(
        {
            "name": "tool-a1",
            "block": "bracket",
            "in": "port:a1",
            "origin": [0, 0, -0.003],
            "direction": [0, 0, -1],
            "length": 0.03,
            "kind": "tool_access",
            "bundle": {"radius": 0.004, "count": 8},
            "anchor": ["bracket.a1"],
            "frame": 6,
        }
    )
    manifest = {
        "schema": 1,
        "blocks": [
            {
                "name": "base",
                "file": "blocks/base.cad.json",
                "scale": "macro",
                "ports": base_ports,
                "desc": "base plate",
                "frame": 2,
            },
            {
                "name": "bracket",
                "file": "blocks/bracket.cad.json",
                "scale": "macro",
                "ports": bracket_ports,
                "desc": "bracket",
                "frame": 4,
            },
        ],
        "rays": rays,
        "notes": "bracket ray fixture",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
    )
    frames = [
        _frame(0, None, "main", "root", 1),
        _frame(1, 0, "main.build", "user", 10),
        _frame(
            2,
            1,
            "pga.box",
            "core",
            20,
            line_in_def=10,
            src="base = pga.box(w=40, d=40, h=10)",
        ),
        _frame(3, 1, "pga.cut", "core", 22, line_in_def=12, src="pga.cut(base, cyl)"),
        _frame(
            4,
            1,
            "pga.box",
            "core",
            40,
            occ=1,
            line_in_def=30,
            src="bracket = pga.box(w=40, d=40, h=6)",
        ),
        _frame(
            5,
            1,
            "pga.cut",
            "core",
            44,
            occ=1,
            line_in_def=34,
            src="pga.cut(bracket, cyl)",
        ),
        _frame(
            6, 1, "pga.ray", "core", 50, line_in_def=40, src="pga.ray('bolt-a1', ...)"
        ),
    ]
    trace = {
        "schema": 2,
        "run": {
            "workspace": "mount",
            "job": None,
            "script_sha": None,
            "lock_sha": None,
            "started_utc": None,
            "finished_utc": None,
            "collapsed": 0,
            "truncated": False,
            "key_collisions": 0,
        },
        "frames": frames,
    }
    (out / "trace.json").write_text(
        json.dumps(trace, indent=1) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    dx = 0.0
    if "--h2-dx" in args:
        i = args.index("--h2-dx")
        dx = float(args[i + 1]) * 1e-3
        del args[i : i + 2]
    write(Path(args[0]) if args else Path(__file__).parent / "out", dx)
