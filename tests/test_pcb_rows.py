"""Slice 0c (gr514460, R-e): one normaliser for the pcb batch rows.

``precis.pcb.rows`` is the single row-shaping layer ``_pcb_apply`` and
the wheel-side ``batch_to_graph`` both call. These tests prove it: a
board applied through ``pcb_apply``/``pcb_graph`` on the dev DB emits the
same normalised rows as ``batch_to_graph`` on ``Board.to_batch()`` —
instances, nets (with members), unconnected pins, roles, stackup —
including the two store passes a mirror used to miss (pad-number →
pin-name remap, declare-all footprint completion, connection ghosts).
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.handlers.part import PartHandler
from precis.pcb import local as pcb_local
from precis.pcb import rows as pcb_rows
from precis.store import Store
from precis_geom import pcb

_SRC = Path(__file__).resolve().parents[1] / "src"


def _parity_board() -> Any:
    b = pcb.Board("rows-parity", stackup=4)
    fp = pcb.Footprint(
        "DIP4",
        [
            pcb.pad("1", "rect", 0.0, 0.0, w=1.0, h=2.0),
            {"number": "2", "shape": "rect", "x": 2.54, "y": 0.0, "w": 1.0, "h": 2.0},
            {"number": "3", "shape": "rect", "x": 5.08, "y": 0.0, "w": 1.0, "h": 2.0},
            {
                "number": "8",
                "shape": "circle",
                "x": 7.62,
                "y": 0.0,
                "w": 1.2,
                "h": 1.2,
                "drill": 0.8,
            },
        ],
        pin_map={"3": "GPIO2", "8": "GND1"},
    )
    u1 = b.part(
        "U1",
        footprint=fp,
        label="uC",
        roles={"vbat"},
        pins=[
            {"name": "VDD", "tags": ["power_in"], "meta": {"note": "rail pin"}},
            {"name": "GPIO1", "etype": "bidi"},
            {"name": "NC1", "nc": True},
            {"name": "SW1", "client_of": {"role": "vbat", "min_voltage_v": 2.7}},
            {"name": "HS", "max_voltage_v": 3.6, "rail": "3v3"},
        ],
    )
    r1 = b.part("R1", lcsc="C25804", pins=["1", "2"])
    vbat = b.net("VBAT", cls="power", current=0.5, voltage=3.3)
    gnd = b.net("GND")
    sig = b.net("SIG", function="reset")
    b.role("vbat", voltage=3.3)
    vbat.tie(u1["VDD"], u1["SW1"])
    gnd.tie(u1["GND1"], r1["1"])  # GND1 is the completed pin_map name for pad 8
    sig.tie(u1["GPIO2"])  # GPIO2 = completed name for pad 3
    return b


_CATALOG_FP = {
    "pads": [
        {
            "number": "1",
            "shape": "rect",
            "x": 0.0,
            "y": 0.0,
            "w": 1.0,
            "h": 1.6,
            "rot": 0,
        },
        {
            "number": "2",
            "shape": "rect",
            "x": 1.0,
            "y": 0.0,
            "w": 1.0,
            "h": 1.6,
            "rot": 0,
        },
    ],
    "pin_map": {},
    "courtyard": {"bbox": [-0.5, -0.8, 1.5, 0.8]},
    "centroid": {"x": 0.5, "y": 0.0},
    "holes": [],
}


def _fake_client(call: Callable[..., str]) -> Any:
    """A stand-in ``precis_client`` module — ``Any`` so the injected
    ``.call`` needs no ``type: ignore``."""
    client = cast(Any, types.ModuleType("precis_client"))
    client.call = call
    return client


def _apply_batch(store: Store, batch: dict[str, Any]) -> Any:
    ref, _created, _counts = store.pcb_apply(
        slug=str(batch.get("slug") or "rows-parity"),
        title="rows-parity",
        components=[dict(c) for c in batch.get("components") or []],
        nets=[dict(n) for n in batch.get("nets") or []],
        connections=[dict(k) for k in batch.get("connections") or []],
        measures=[dict(m) for m in batch.get("measures") or []],
        features=[dict(f) for f in batch.get("features") or []],
        footprints=[dict(f) for f in batch.get("footprints") or []],
        meta=dict(batch.get("meta") or {}),
    )
    roles = batch.get("roles") or {}
    if roles:
        store.pcb_upsert_roles(
            ref.id,
            [
                {
                    "name": name,
                    "voltage_v": r.get("voltage"),
                    **({"fanout": r["fanout"]} if "fanout" in r else {}),
                }
                for name, r in roles.items()
            ],
        )
    if isinstance(batch.get("net_classes"), dict) and batch["net_classes"]:
        store.pcb_upsert_net_classes(ref.id, batch["net_classes"])
    return ref


def test_pad_pin_alias_normalizes_to_number() -> None:
    by_pin = pcb_rows.normalize_pad(
        {"pin": "7", "shape": "rect", "x": 0, "y": 0, "w": 1}
    )
    by_num = pcb_rows.normalize_pad(
        {"number": "7", "shape": "rect", "x": 0, "y": 0, "w": 1}
    )
    assert by_pin == by_num
    assert by_pin["number"] == "7" and "pin" not in by_pin


def test_local_has_no_row_shaping_mirror() -> None:
    """local.py imports precis.pcb.rows and defines none of the deleted
    mirror functions (the AST check the spec asks for)."""
    tree = ast.parse((_SRC / "precis" / "pcb" / "local.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for a in node.names
    }
    from_names = {
        (node.module, a.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for a in node.names
    }
    assert (
        "precis.pcb.rows" in imported
        or (
            "precis.pcb",
            "rows",
        )
        in from_names
    )
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    assert defined.isdisjoint({"_pin_facts", "_norm_pad", "_INSTANCE_PASSTHROUGH"})


def test_batch_to_graph_matches_pcb_graph(store: Store) -> None:
    """The wheel's preview and the store's truth emit identical rows."""
    b = _parity_board()
    batch = b.to_batch()
    # The catalog half of the footprint join: R1's C25804 resolves its
    # part_footprints row on the store side; the wheel gets the same row
    # handed in via footprints=.
    store.part_footprint_put("C25804", dict(_CATALOG_FP))
    batch["connections"].append({"net": "SIG", "refdes": "U1", "pin": "3"})
    batch["connections"].append({"net": "SIG2", "refdes": "U1", "pin": "GHOST"})

    ref = _apply_batch(store, batch)
    store_side = store.pcb_graph(ref.id)
    fp_row = store.part_footprint_get("C25804")
    assert fp_row is not None
    wheel_side = pcb_local.batch_to_graph(batch, footprints={"C25804": fp_row})

    assert wheel_side["instances"] == store_side["instances"]
    assert wheel_side["nets"] == store_side["nets"]
    assert wheel_side["unconnected"] == store_side["unconnected"]
    assert wheel_side["roles"] == store_side["roles"]
    assert wheel_side["board"]["stackup"] == store_side["board"]["stackup"]


def test_meta_etype_rides_meta_and_earns_no_tag(store: Store) -> None:
    """gr514460 seam review: ``etype`` is top-level only — the store
    rule (``p['etype']``, never ``meta['etype']``). An etype authored
    under ``meta`` rides meta verbatim and earns NO tag, identically on
    the store path and the wheel's ``batch_to_graph``."""
    b = pcb.Board("etype-meta")
    b.part(
        "U1",
        pins=[
            {"name": "P1", "meta": {"etype": "bidi"}},
            {"name": "P2", "etype": "bidi"},
        ],
    )
    batch = b.to_batch()
    ref = _apply_batch(store, batch)
    sides = (
        store.pcb_graph(ref.id)["unconnected"],
        pcb_local.batch_to_graph(batch)["unconnected"],
    )
    for unconnected in sides:
        pins = {(u["refdes"], u["pin"]): u for u in unconnected}
        assert "bidi" not in pins[("U1", "P1")]["tags"]
        assert pins[("U1", "P1")]["meta"]["etype"] == "bidi"
        assert pins[("U1", "P2")]["tags"] == ["bidi"]


def test_footprint_view_returns_json_row(store: Store) -> None:
    store.part_footprint_put("C1", dict(_CATALOG_FP))
    body = PartHandler(hub=Hub(store=store)).get(id="C1", view="footprint").body
    row = json.loads(body)
    assert row["lcsc"] == "C1"
    assert row["pads"] == _CATALOG_FP["pads"]
    geometry = {
        k: row[k] for k in ("pads", "pin_map", "courtyard", "centroid", "holes")
    }
    assert (
        row["sha"]
        == hashlib.sha256(
            json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    assert row["part_id"] is None  # no ref minted by a read


def test_catalog_resolves_via_precis_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a staged ``precis_client`` (``precis_access: read``),
    ``pcb.catalog`` populates the part and the manifest's parts block."""
    payload = {
        "lcsc": "C1",
        "part_id": "pn123",
        "package": "0402",
        "height_mm": 0.5,
        **_CATALOG_FP,
        "sha": "abc123",
    }
    monkeypatch.setitem(
        sys.modules,
        "precis_client",
        _fake_client(lambda *a, **kw: json.dumps(payload)),
    )
    pcb._CATALOG_READS.clear()

    b = pcb.Board("m")
    part = pcb.catalog("C1", board=b)
    assert part.resolved
    assert part.pads == _CATALOG_FP["pads"]
    assert part.part_id == "pn123"
    assert part.footprint_sha == "abc123"
    assert part.read_at is not None and part.read_at.endswith("+00:00")

    manifest = b.manifest()
    assert manifest["parts"] == [
        {
            "lcsc": "C1",
            "part_id": "pn123",
            "footprint_sha": "abc123",
            "read_at": part.read_at,
        }
    ]
    assert manifest["precis_geom"]["version"]


def test_catalog_stays_unresolved_without_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "precis_client", raising=False)
    monkeypatch.setattr(
        "importlib.import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )
    pcb._CATALOG_READS.clear()
    part = pcb.catalog("C9")
    assert not part.resolved
    assert pcb.Board("m2").manifest()["parts"] == []


def test_catalog_reads_scope_per_board(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr514460 seam re-review: one board's reads cannot leak into
    another board's manifest — a module-level ``catalog()`` read binds
    at read time to the board named by ``board=`` or the enclosing
    ``with`` block (gr515495: explicit binding, not a global last-built
    board); ``b.catalog`` binds its read outright."""
    payload = {"lcsc": "C1", "part_id": "pn1", **_CATALOG_FP, "sha": "a1"}
    monkeypatch.setitem(
        sys.modules,
        "precis_client",
        _fake_client(lambda *a, **kw: json.dumps(payload)),
    )
    pcb._CATALOG_READS.clear()

    a = pcb.Board("a")
    pcb.catalog("C1", board=a)
    b = pcb.Board("b")
    pcb.catalog("C2", board=b)
    b.catalog("C3")  # bound method — binds to b

    assert pcb.Board("c").manifest()["parts"] == []
    assert [p["lcsc"] for p in a.manifest()["parts"]] == ["C1"]
    assert [p["lcsc"] for p in b.manifest()["parts"]] == ["C2", "C3"]


def test_module_catalog_binds_inside_with_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr515495: ``with b:`` is the explicit current-board context —
    reads inside bind to b, and exiting resets the binding so a stale
    board can never claim a later read."""
    payload = {"lcsc": "C1", "part_id": "pn1", **_CATALOG_FP, "sha": "a1"}
    monkeypatch.setitem(
        sys.modules,
        "precis_client",
        _fake_client(lambda *a, **kw: json.dumps(payload)),
    )
    pcb._CATALOG_READS.clear()

    a = pcb.Board("a")
    with pcb.Board("b") as b:
        pcb.catalog("C1")
    # After __exit__ the binding is gone — a board-less read is pending
    # again (deprecated), not bound to the stale b.
    with pytest.warns(DeprecationWarning, match="no board"):
        pcb.catalog("C2")

    assert [p["lcsc"] for p in a.manifest()["parts"]] == ["C2"]
    assert [p["lcsc"] for p in b.manifest()["parts"]] == ["C1"]


def test_module_catalog_without_board_warns_and_stays_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deprecation shim (gr515495): ``pcb.catalog()`` with no board=
    and no ``with`` block still lands on the next manifest — but warns."""
    payload = {"lcsc": "C1", "part_id": "pn1", **_CATALOG_FP, "sha": "a1"}
    monkeypatch.setitem(
        sys.modules,
        "precis_client",
        _fake_client(lambda *a, **kw: json.dumps(payload)),
    )
    pcb._CATALOG_READS.clear()

    with pytest.warns(DeprecationWarning, match="deprecated"):
        pcb.catalog("C1")
    assert [p["lcsc"] for p in pcb.Board("a").manifest()["parts"]] == ["C1"]


def test_board_catalog_does_not_steal_a_pending_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read made before any board exists stays pending for the next
    manifest; ``b.catalog`` of the same part number does its own read
    bound to b — it must not consume the pending entry."""
    payload = {"lcsc": "C1", "part_id": "pn1", **_CATALOG_FP, "sha": "a1"}
    monkeypatch.setitem(
        sys.modules,
        "precis_client",
        _fake_client(lambda *a, **kw: json.dumps(payload)),
    )
    pcb._CATALOG_READS.clear()

    with pytest.warns(DeprecationWarning):
        pcb.catalog("C1")  # no board bound — pending for the next manifest
    b = pcb.Board("b")
    b.catalog("C1")  # b's own read; the pending one survives
    assert [p["lcsc"] for p in pcb.Board("a").manifest()["parts"]] == ["C1"]
    assert [p["lcsc"] for p in b.manifest()["parts"]] == ["C1"]


def test_catalog_read_failure_records_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read is not silent — the manifest's parts entry records
    ``provenance: 'unresolved'`` plus the error."""

    def boom(*a: Any, **kw: Any) -> str:
        raise RuntimeError("prod read timed out")

    monkeypatch.setitem(sys.modules, "precis_client", _fake_client(boom))
    f = pcb.Board("f")
    part = f.catalog("C7")
    assert not part.resolved and part.error
    entry = f.manifest()["parts"][0]
    assert entry == {"lcsc": "C7", "provenance": "unresolved", "error": part.error}


def test_catalog_paged_reply_names_paging(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body cut mid-JSON by the frame cap ends in a ``more(cursor=...)``
    footer — the recorded manifest error must name paging, not read as a
    generic parse failure."""
    payload = {"lcsc": "C1", "part_id": "pn1", **_CATALOG_FP, "sha": "a1"}

    def paged(*a: Any, **kw: Any) -> str:
        return json.dumps(payload)[:80] + (
            "\n\n---\n⚠️ **Truncated** — Call `more(cursor='pg2')` to fetch it."
        )

    monkeypatch.setitem(sys.modules, "precis_client", _fake_client(paged))

    b = pcb.Board("p")
    with b:
        part = pcb.catalog("C1")
    assert not part.resolved
    assert part.error == "paged response, more() not followed"
    entry = b.manifest()["parts"][0]
    assert entry["provenance"] == "unresolved"
    assert "paged" in entry["error"] and "more()" in entry["error"]


def test_catalog_footer_wrapped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read MCP may hand back the JSON row with a ``Next:`` footer —
    the row still parses."""
    payload = {"lcsc": "C1", "part_id": "pn1", **_CATALOG_FP, "sha": "a1"}

    def wrapped(*a: Any, **kw: Any) -> str:
        return json.dumps(payload) + "\nNext: get(kind='pcb', view='check')"

    monkeypatch.setitem(sys.modules, "precis_client", _fake_client(wrapped))
    w = pcb.Board("w")
    part = pcb.catalog("C1", board=w)
    assert part.resolved and part.pads == _CATALOG_FP["pads"]
    assert w.manifest()["parts"][0]["lcsc"] == "C1"
