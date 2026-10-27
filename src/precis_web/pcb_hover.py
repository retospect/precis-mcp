"""Facts behind the board page's hover tooltip, Selection box and ratsnest
(gr495389 / gr495390 / gr495391).

``hover_facts`` joins the shown snapshot's raw rows (parts, pins, nets) with
:func:`precis.pcb.route_state.route_state` — the one function every route
consumer reads — into the ``hover`` block of the SVG selection manifest:

* ``facts`` — what ``static/pcb-facts.js`` formats: per refdes the display
  ``value``/``package``/``rating`` and stored ``lcsc`` C-number; per net its
  pin labels, estimated current and route status/reason.
* ``unrouted`` — nets the ratsnest is drawn for (every net not ``realized``).
* ``failed`` — nets whose pads render red (router status ``failed``).

The failure text is whatever route_state carries in the net row's ``note``;
this module never writes or rewrites it.
"""

from __future__ import annotations

import re
from typing import Any

from precis.pcb import catalog
from precis.pcb.route_state import route_state

_SI = ((1e-6, "u"), (1e-9, "n"), (1e-12, "p"))


def _num(v: float) -> str:
    return f"{v:.4g}"


def _farads(v: float) -> str:
    if v >= 1e-3:
        return f"{_num(v * 1e3)}mF"
    for scale, prefix in _SI:
        if v >= scale * 0.9999:
            return f"{_num(v / scale)}{prefix}F"
    return f"{_num(v / 1e-12)}pF"


def _ohms(v: float) -> str:
    if v >= 1e6:
        return f"{_num(v / 1e6)}M"
    if v >= 1e3:
        return f"{_num(v / 1e3)}k"
    return f"{_num(v)}R"


#: An opaque footprint snapshot — the EasyEDA uuid, optionally prefixed —
#: is not a package name a human can read (gr520540).
_OPAQUE_FOOTPRINT = re.compile(r"^(?:easyeda:)?[0-9a-f]{32}$", re.IGNORECASE)


def _package(row: dict[str, Any], component: dict[str, Any]) -> str | None:
    pkg = row.get("package")
    if pkg:
        return pkg
    fp = component.get("footprint")
    if fp and not _OPAQUE_FOOTPRINT.match(str(fp)):
        return fp
    return None


def part_summary(part: dict[str, Any] | None, component: dict[str, Any]) -> dict:
    """``{value, package, rating, lcsc}`` display strings for one component;
    a field is ``None`` when the catalogue row does not carry it."""
    row: dict[str, Any] = part or {}
    parsed = catalog.parse_passive_description(row.get("description"))
    value = rating = None
    if parsed and parsed["kind"] == "capacitor":
        if parsed.get("value_f") is not None:
            value = _farads(parsed["value_f"])
        if parsed.get("rated_v") is not None:
            rating = f"{_num(parsed['rated_v'])}V"
    elif parsed and parsed["kind"] == "resistor":
        value = _ohms(parsed["ohm"])
        if parsed.get("rated_w") is not None:
            rating = f"{_num(parsed['rated_w'])}W"
    if value is None:
        value = row.get("mfr_part") or component.get("label") or None
    return {
        "value": value,
        "package": _package(row, component),
        "rating": rating,
        "lcsc": component.get("part_lcsc") or None,
        "datasheet": row.get("datasheet_url") or None,
    }


def facts_from_tables(
    tables: dict[str, list[dict[str, Any]]],
    route_nets: list[dict[str, Any]],
    *,
    scripts: dict[str, dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """The ``hover`` manifest block from a snapshot's tables and
    ``RouteStatus.nets`` rows. Pure. ``scripts`` is
    :func:`placed_scripts`'s ``{refdes: {file, line, ws}}`` map — each
    part's ``script`` field is the board-script line that placed it,
    ``None`` when the bound trace knows no maker (gr520540)."""

    def live(name: str) -> list[dict[str, Any]]:
        return [r for r in tables.get(name, []) if not r.get("retired_at")]

    components = {r["component_id"]: r for r in live("pcb_components")}
    parts = {r["lcsc"]: r for r in tables.get("parts", [])}
    pins = {r["pin_id"]: r for r in live("pcb_pins")}
    instances = {r["instance_id"]: r for r in live("pcb_instances")}
    out_parts: dict[str, dict] = {}
    for inst in instances.values():
        comp = components.get(inst["component_id"])
        if comp is not None:
            summary = part_summary(parts.get(comp.get("part_lcsc")), comp)
            summary["script"] = (scripts or {}).get(inst["refdes"])
            out_parts[inst["refdes"]] = summary
    nets_by_id = {r["net_id"]: r for r in live("pcb_nets")}
    labels: dict[int, list[str]] = {}
    for conn in tables.get("pcb_netconns", []):
        owner, pin = instances.get(conn["instance_id"]), pins.get(conn["pin_id"])
        if owner is None or pin is None or conn["net_id"] not in nets_by_id:
            continue
        labels.setdefault(conn["net_id"], []).append(
            f"{owner['refdes']}.{pin.get('pad') or pin.get('name')}"
        )
    route = {r["name"]: r for r in route_nets}
    out_nets: dict[str, dict] = {}
    for net in nets_by_id.values():
        row = route.get(net["name"], {})
        status = row.get("status") or "unrouted"
        reason = None
        if status != "realized":
            reason = row.get("note") or (
                "router reported no reason" if status == "failed" else status
            )
        cur = net.get("est_current_a")
        out_nets[net["name"]] = {
            "pins": sorted(set(labels.get(net["net_id"], []))),
            "net_class": net.get("net_class") or None,
            "current_a": cur if isinstance(cur, (int, float)) and cur > 0 else None,
            "status": status,
            "reason": reason,
        }
    return {
        "facts": {"parts": out_parts, "nets": out_nets},
        "unrouted": sorted(n for n, v in out_nets.items() if v["status"] != "realized"),
        "failed": sorted(n for n, v in out_nets.items() if v["status"] == "failed"),
    }


def _frame_id(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def placed_scripts(store: Any, view: Any) -> dict[str, dict[str, Any] | None]:
    """``{refdes: {'file', 'line', 'ws'}}`` — the board-script call site
    that placed each instance, for the Selection box's script link
    (gr520540). Reads exactly what ``get(kind='pcb', view='frames')``
    walks — the ref's ``meta.generated.trace`` binding, the stored
    ``ws_trace`` doc and the rows' ``meta.frame`` stamps (the instance's
    own, else its component's — :func:`precis.pcb.frames.bound_frames`'
    rule) — never recomputed. A placement is linked only when its maker
    chain holds a user frame carrying ``file``/``line``; ``{}`` when no
    trace is bound."""
    from precis.pcb import frames as pcb_frames

    trace_ref = ((getattr(view.ref, "meta", None) or {}).get("generated") or {}).get(
        "trace"
    )
    if not isinstance(trace_ref, dict):
        return {}
    home = trace_ref.get("run_job")
    doc = pcb_frames.read_ws_trace(
        store,
        view.ref.id if home is None else int(home),
        trace_ref.get("root"),
    )
    if doc is None:
        return {}
    tr = pcb_frames.Trace(doc)
    tables = view.raw["tables"]
    components = {
        r["component_id"]: r
        for r in tables.get("pcb_components", [])
        if not r.get("retired_at")
    }
    out: dict[str, dict[str, Any] | None] = {}
    for inst in tables.get("pcb_instances", []):
        if inst.get("retired_at"):
            continue
        refdes = inst["refdes"]
        comp = components.get(inst["component_id"]) or {}
        raw = (inst.get("meta") or {}).get("frame")
        if raw is None:
            raw = (comp.get("meta") or {}).get("frame")
        maker = tr.maker_frame(_frame_id(raw))
        if maker is None:
            continue
        # The maker itself may be a library-internal frame — the site a
        # human wants is the script's own call, the deepest user frame on
        # the root→maker path.
        site = next(
            (
                f
                for f in reversed(tr.path(maker))
                if f.get("kind") == "user" and f.get("file") and f.get("line")
            ),
            None,
        )
        if site is None:
            continue
        out[refdes] = {
            "file": str(site["file"]),
            "line": int(site["line"]),
            "ws": tr.ws,
        }
    return out


def hover_facts(store: Any, view: Any) -> dict[str, Any]:
    """The ``hover`` block for the shown snapshot; routing facts come from
    :func:`route_state`, never a second computation."""
    status = route_state(store, view.ref.id, drc=False)
    return facts_from_tables(
        view.raw["tables"], status.nets, scripts=placed_scripts(store, view)
    )
