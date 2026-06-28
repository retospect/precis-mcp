#!/usr/bin/env python
"""Regenerate every export artifact for the .cad examples (ADR 0041 §10).

For each ``*.cad`` source in this directory, write the four export forms
into ``out/``:

- ``.scad`` — OpenSCAD source (pure, zero-dependency);
- ``.stl``  — printable mesh (needs ``precis-mcp[cad-export]``);
- ``.3mf``  — printable mesh, modern slicer format (same extra);
- ``.step`` — exact OpenCASCADE B-rep (needs ``precis-mcp[cad-step]``).

Mesh / STEP formats are skipped (with a note) when their optional backend
is absent, so this runs everywhere. Usage::

    uv run python examples/cad/generate.py
"""

from __future__ import annotations

from pathlib import Path

from precis.cad.export import (
    export_mesh,
    export_step,
    manifold_available,
    step_available,
    to_openscad,
)
from precis.cad.scene import parse_source

HERE = Path(__file__).parent
OUT = HERE / "out"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    have_mesh = manifold_available()
    have_step = step_available()
    if not have_mesh:
        print(
            "note: manifold3d absent — skipping .stl/.3mf (pip install "
            "'precis-mcp[cad-export]')"
        )
    if not have_step:
        print(
            "note: OpenCASCADE absent — skipping .step (pip install "
            "'precis-mcp[cad-step]')"
        )

    for src in sorted(HERE.glob("*.cad")):
        name = src.stem
        spec = parse_source(src.read_text())
        (OUT / f"{name}.scad").write_text(to_openscad(spec, name=name))
        wrote = ["scad"]
        if have_mesh:
            export_mesh(spec, OUT / f"{name}.stl")
            export_mesh(spec, OUT / f"{name}.3mf")
            wrote += ["stl", "3mf"]
        if have_step:
            export_step(spec, OUT / f"{name}.step")
            wrote.append("step")
        print(f"{name:14s} -> {', '.join(wrote)}")


if __name__ == "__main__":
    main()
