"""Exact STEP export via OpenCASCADE (ADR 0041 §10, the ``[cad-step]`` extra).

A mesh kernel (manifold3d / OpenSCAD) cannot emit STEP — STEP is a
boundary-representation interchange carrying *exact* surfaces. This
backend rebuilds the design as an OCCT B-rep: each primitive maps 1:1
onto an OCCT make-primitive (our rigid ``Transform`` is exactly a
``gp_Trsf``), the boolean DAG folds with ``Fuse``/``Cut``/``Common``, and
``STEPCAFControl_Writer`` serialises each component as its own named
solid in millimetres.

Everything is lazily imported through :func:`_occt` so importing this
module never requires the (heavy) ``cadquery-ocp`` wheel; callers gate on
:func:`available` first. The public entry point is :func:`export_step`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from precis.cad.dsl import parse
from precis.cad.scene import NodeSpec, SceneSpec, _node_xform, _pattern_transforms
from precis.cad.tessellate import _ngon_xy
from precis.cad.vec import Transform


class StepExportError(RuntimeError):
    """STEP export failed inside the OCCT backend."""


def available() -> bool:
    """True iff the OpenCASCADE Python binding (``OCP``) is importable."""
    try:
        import OCP.gp  # noqa: F401
    except Exception:
        return False
    return True


def _occt() -> Any:
    """Lazily import and bundle the OCCT names this backend uses."""
    from OCP.BRepAlgoAPI import (
        BRepAlgoAPI_Common,
        BRepAlgoAPI_Cut,
        BRepAlgoAPI_Fuse,
    )
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_MakeVertex,
        BRepBuilderAPI_Transform,
    )
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.BRepPrimAPI import (
        BRepPrimAPI_MakeBox,
        BRepPrimAPI_MakeCone,
        BRepPrimAPI_MakeCylinder,
        BRepPrimAPI_MakePrism,
        BRepPrimAPI_MakeSphere,
        BRepPrimAPI_MakeTorus,
    )
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    return {
        "BRepAlgoAPI_Common": BRepAlgoAPI_Common,
        "BRepAlgoAPI_Cut": BRepAlgoAPI_Cut,
        "BRepAlgoAPI_Fuse": BRepAlgoAPI_Fuse,
        "BRepBuilderAPI_MakeFace": BRepBuilderAPI_MakeFace,
        "BRepBuilderAPI_MakePolygon": BRepBuilderAPI_MakePolygon,
        "BRepBuilderAPI_MakeVertex": BRepBuilderAPI_MakeVertex,
        "BRepBuilderAPI_Transform": BRepBuilderAPI_Transform,
        "BRepOffsetAPI_ThruSections": BRepOffsetAPI_ThruSections,
        "BRepPrimAPI_MakeBox": BRepPrimAPI_MakeBox,
        "BRepPrimAPI_MakeCone": BRepPrimAPI_MakeCone,
        "BRepPrimAPI_MakeCylinder": BRepPrimAPI_MakeCylinder,
        "BRepPrimAPI_MakePrism": BRepPrimAPI_MakePrism,
        "BRepPrimAPI_MakeSphere": BRepPrimAPI_MakeSphere,
        "BRepPrimAPI_MakeTorus": BRepPrimAPI_MakeTorus,
        "gp_Pnt": gp_Pnt,
        "gp_Trsf": gp_Trsf,
        "gp_Vec": gp_Vec,
        "IFSelect_ReturnStatus": IFSelect_ReturnStatus,
        "Interface_Static": Interface_Static,
        "STEPControl_AsIs": STEPControl_AsIs,
        "STEPCAFControl_Writer": STEPCAFControl_Writer,
        "TCollection_ExtendedString": TCollection_ExtendedString,
        "TDataStd_Name": TDataStd_Name,
        "TDocStd_Document": TDocStd_Document,
        "XCAFDoc_DocumentTool": XCAFDoc_DocumentTool,
    }


# ---------------------------------------------------------------------------
# primitive → OCCT shape (local frame)
# ---------------------------------------------------------------------------


def _polygon_wire(o: Any, ring: list[tuple[float, float]], z: float) -> Any:
    poly = o["BRepBuilderAPI_MakePolygon"]()
    for x, y in ring:
        poly.Add(o["gp_Pnt"](x, y, z))
    poly.Close()
    return poly.Wire()


def _prism(o: Any, ring: list[tuple[float, float]], h: float) -> Any:
    face = o["BRepBuilderAPI_MakeFace"](_polygon_wire(o, ring, 0.0)).Face()
    return o["BRepPrimAPI_MakePrism"](face, o["gp_Vec"](0.0, 0.0, h)).Shape()


def _loft(o: Any, bottom: list[tuple[float, float]], top: Any, h: float) -> Any:
    gen = o["BRepOffsetAPI_ThruSections"](True, True)  # solid, ruled
    gen.AddWire(_polygon_wire(o, bottom, 0.0))
    if isinstance(top, list):
        gen.AddWire(_polygon_wire(o, top, h))
    else:  # apex point (pyramid)
        gen.AddVertex(o["BRepBuilderAPI_MakeVertex"](o["gp_Pnt"](0.0, 0.0, h)).Vertex())
    gen.Build()
    return gen.Shape()


def _primitive_shape(o: Any, config: str) -> Any:
    spec = parse(config)
    a, p = spec.alias, spec.params
    if a == "box":
        w, d, h = p["w"], p["d"], p["h"]
        return o["BRepPrimAPI_MakeBox"](
            o["gp_Pnt"](-w / 2, -d / 2, 0.0), w, d, h
        ).Shape()
    if a == "cyl":
        return o["BRepPrimAPI_MakeCylinder"](p["r"], p["h"]).Shape()
    if a == "cone":
        return o["BRepPrimAPI_MakeCone"](p["r"], 0.0, p["h"]).Shape()
    if a == "tcone":
        return o["BRepPrimAPI_MakeCone"](p["rb"], p["rt"], p["h"]).Shape()
    if a == "sphere":
        return o["BRepPrimAPI_MakeSphere"](p["r"]).Shape()
    if a == "torus":
        return o["BRepPrimAPI_MakeTorus"](p["R"], p["r"]).Shape()
    if a == "hex":
        return _prism(o, _ngon_xy(6, p["r"]), p["h"])
    if a == "ngon":
        return _prism(o, _ngon_xy(int(p["n"]), p["r"]), p["h"])
    if a == "frustum":
        n = int(p["n"])
        return _loft(o, _ngon_xy(n, p["rb"]), _ngon_xy(n, p["rt"]), p["h"])
    if a == "pyramid":
        return _loft(o, _ngon_xy(int(p["n"]), p["r"]), None, p["h"])
    raise StepExportError(f"shape {a!r} has no STEP export (chamfer is unbounded)")


def _placed(o: Any, shape: Any, xf: Transform) -> Any:
    trsf = o["gp_Trsf"]()
    R, t = xf.R, xf.t
    trsf.SetValues(
        float(R[0, 0]),
        float(R[0, 1]),
        float(R[0, 2]),
        float(t[0]),
        float(R[1, 0]),
        float(R[1, 1]),
        float(R[1, 2]),
        float(t[1]),
        float(R[2, 0]),
        float(R[2, 1]),
        float(R[2, 2]),
        float(t[2]),
    )
    return o["BRepBuilderAPI_Transform"](shape, trsf, True).Shape()


def _node_shape(o: Any, node: NodeSpec) -> Any:
    base = _primitive_shape(o, node.config)
    if node.pattern is not None:
        xfs = _pattern_transforms(node)
        cur = _placed(o, base, xfs[0])
        for xf in xfs[1:]:
            cur = o["BRepAlgoAPI_Fuse"](cur, _placed(o, base, xf)).Shape()
        return cur
    return _placed(o, base, _node_xform(node.loc, node.rot))


def _component_shapes(o: Any, spec: SceneSpec) -> list[tuple[str, Any]]:
    """Fold each component into its own OCCT solid, **without** fusing
    across components. STEP carries multiple solids natively, so an
    assembly travels as separate named bodies in the one file (ADR 0041
    §10) — exactly what a downstream CAD tool wants."""
    by_comp: dict[str, list[NodeSpec]] = {}
    for node in spec.nodes:
        by_comp.setdefault(node.component, []).append(node)
    comps: list[tuple[str, Any]] = []
    for comp in spec.components:
        cur: Any = None
        for node in by_comp.get(comp, []):
            shape = _node_shape(o, node)
            if cur is None:
                cur = shape
            elif node.op == "add":
                cur = o["BRepAlgoAPI_Fuse"](cur, shape).Shape()
            elif node.op == "cut":
                cur = o["BRepAlgoAPI_Cut"](cur, shape).Shape()
            elif node.op == "intersect":
                cur = o["BRepAlgoAPI_Common"](cur, shape).Shape()
        if cur is not None:
            comps.append((comp, cur))
    if not comps:
        raise StepExportError("design has no solid geometry to export")
    return comps


def export_step(spec: SceneSpec, out_path: Path) -> Path:
    """Build the design as an OCCT B-rep and write it as a STEP file (mm).

    Each component becomes its **own named solid** in the file (a true
    assembly via the XCAF document model), so a 2-part design exports as
    two distinct ``MANIFOLD_SOLID_BREP`` bodies — not one welded blob."""
    o = _occt()
    comps = _component_shapes(o, spec)
    doc = o["TDocStd_Document"](o["TCollection_ExtendedString"]("XmlXCAF"))
    tool = o["XCAFDoc_DocumentTool"].ShapeTool_s(doc.Main())
    for name, shape in comps:
        label = tool.AddShape(shape, False, False)  # not an assembly node
        o["TDataStd_Name"].Set_s(label, o["TCollection_ExtendedString"](name))
    o["Interface_Static"].SetCVal_s("write.step.unit", "MM")
    writer = o["STEPCAFControl_Writer"]()
    writer.Transfer(doc, o["STEPControl_AsIs"])
    status = writer.Write(str(out_path))
    if status != o["IFSelect_ReturnStatus"].IFSelect_RetDone:
        raise StepExportError(f"STEPCAFControl_Writer.Write failed (status={status})")
    return out_path
