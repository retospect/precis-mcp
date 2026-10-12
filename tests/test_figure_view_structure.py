"""A draft figure sourced from a ``structure`` design (gr461790,
se-view-figures §2): put(chunk_kind='figure', meta={'view': {...}}) stores a
recipe; the stick-figure PNG renders via viz3d at export time and re-renders
when the structure's geometry changes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import docx
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.export.docx import export_docx
from precis.handlers.draft import _PLACEHOLDER_PNG, DraftHandler
from precis.handlers.structure import StructureHandler
from precis.handlers.todo import TodoHandler
from precis.render.figure import needs_render

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [False, False, False]},
        "ops": [
            {"op": "add_atom", "element": "C", "frac": [0.1, 0.1, 0.1]},
            {"op": "add_atom", "element": "O", "frac": [0.22, 0.1, 0.1]},
            {"op": "add_bond", "i": "aC1", "j": "aO1", "order": 1},
        ],
    }
)
_VIEW = {
    "source": "structure:co",
    "camera": {"azimuth_deg": 30, "elevation_deg": 15},
    "refine": 1,
}


def _draft(hub: Hub, view: dict | None = None):
    StructureHandler(hub=hub).put(id="co", text=_PD)
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=pid)
    d.put(
        id="g",
        chunk_kind="figure",
        text="Fig 1. CO.",
        meta={"view": view or _VIEW},
        at={"last": True},
    )
    ref = hub.live_store.get_ref(kind="draft", id="g")
    assert ref is not None
    fig = next(
        c
        for c in hub.live_store.drafts.reading_order(ref.id)
        if c.chunk_kind == "figure"
    )
    return ref, fig


def _blob(hub: Hub, handle: str) -> bytes:
    blob = hub.live_store.drafts.get_chunk_blob(handle)
    assert blob is not None
    return blob[0]


def _image_shas(path: Path) -> list[str]:
    doc = docx.Document(str(path))
    return [
        hashlib.sha256(r.target_part.blob).hexdigest()
        for r in doc.part.rels.values()
        if "image" in r.reltype
    ]


def test_view_figure_pending_then_rendered_on_export(hub: Hub, tmp_path: Path) -> None:
    ref, fig = _draft(hub)
    drafts = hub.live_store.drafts
    assert fig.meta["figure"]["render_pending"] is True
    assert fig.meta["render"]["kind"] == "view"
    assert fig.meta["render"]["source"] == {"kind": "structure", "slug": "co"}
    assert _blob(hub, fig.handle) == _PLACEHOLDER_PNG

    res = export_docx(hub.live_store, ref, target_path=tmp_path / "a.docx")

    shas = _image_shas(res.path)
    assert len(shas) == 1 and shas[0] != hashlib.sha256(_PLACEHOLDER_PNG).hexdigest()
    assert not [w for w in res.warnings if "render" in w]
    meta = drafts.draft_chunk_meta(fig.handle)
    assert "render_pending" not in meta["figure"]
    assert len(meta["render"]["cached_key"]) == 64
    assert _blob(hub, fig.handle)[:8] == b"\x89PNG\r\n\x1a\n"
    assert not needs_render(hub.live_store, drafts.get_draft_chunk(fig.dc))


def test_view_figure_rerenders_after_geometry_edit(hub: Hub, tmp_path: Path) -> None:
    ref, fig = _draft(hub)
    drafts = hub.live_store.drafts
    export_docx(hub.live_store, ref, target_path=tmp_path / "a.docx")
    key1 = drafts.draft_chunk_meta(fig.handle)["render"]["cached_key"]
    png1 = _blob(hub, fig.handle)

    # unchanged geometry: key stable
    export_docx(hub.live_store, ref, target_path=tmp_path / "b.docx")
    assert drafts.draft_chunk_meta(fig.handle)["render"]["cached_key"] == key1

    StructureHandler(hub=hub).edit(
        id="co", ops=[{"op": "add_atom", "element": "N", "frac": [0.22, 0.2, 0.3]}]
    )
    assert needs_render(hub.live_store, drafts.get_draft_chunk(fig.dc))
    export_docx(hub.live_store, ref, target_path=tmp_path / "c.docx")
    assert drafts.draft_chunk_meta(fig.handle)["render"]["cached_key"] != key1
    assert _blob(hub, fig.handle) != png1


@pytest.mark.parametrize(
    "view",
    [
        {"source": "cad:co"},
        {"source": "se:"},
        {"source": "structure:"},
        {"source": "structure:co", "block": "hub"},
        {"source": "structure:co", "camera": {"bogus": 1}},
        {"source": "structure:co", "refine": 7},
        {"source": "structure:co", "refine": 1.9},
        {"source": "structure:co", "refine": True},
        {"source": "structure:co", "camera": {"azimuth_deg": float("nan")}},
        {"source": "structure:co", "camera": {"elevation_deg": float("inf")}},
        {"source": "structure:co", "camera": {"azimuth_deg": "30"}},
        {"source": "structure:co", "camera": {"zoom": 100}},
        {"source": "structure:co", "camera": {"zoom": True}},
        {"source": "structure:co", "camera": {"projection": "fisheye"}},
        {"source": "structure:co", "style": {"stick_radius": 5}},
        {"source": "structure:co", "style": {"ball_scale": 0.0}},
        {"source": "structure:co", "style": {"color": "red"}},
    ],
)
def test_view_figure_rejects_bad_recipe(hub: Hub, view: dict) -> None:
    StructureHandler(hub=hub).put(id="co", text=_PD)
    with pytest.raises(BadInput):
        _draft_with(hub, view)


def test_view_figure_unknown_structure(hub: Hub) -> None:
    with pytest.raises(NotFound):
        _draft_with(hub, {"source": "structure:nope"})


def _draft_with(hub: Hub, view: dict) -> None:
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=pid)
    d.put(id="g", chunk_kind="figure", text="Fig.", meta={"view": view})


def test_view_recipe_normalises_angles() -> None:
    from precis.render.figure import view_recipe

    r = view_recipe("co", {"azimuth_deg": 390, "elevation_deg": -90}, None, 2)
    assert r["camera"] == {"azimuth_deg": 30.0, "elevation_deg": 270.0}


def test_view_put_refuses_oversize_structure(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.render.figure.VIEW_MAX_ATOMS", 1)
    with pytest.raises(BadInput, match="capped"):
        _draft(hub)


def test_view_render_time_cap_and_failure_marker(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over the cap at render time -> failure message, marker stamped, and a
    second consume does NOT re-render until the structure changes."""
    import precis.render.figure as rf

    ref, fig = _draft(hub)
    drafts = hub.live_store.drafts
    monkeypatch.setattr(rf, "VIEW_MAX_ATOMS", 1)
    calls: list[int] = []
    real = rf._render_view_png

    def spy(scene: object, recipe: dict) -> bytes | None:
        calls.append(1)
        return real(scene, recipe)

    monkeypatch.setattr(rf, "_render_view_png", spy)

    err = rf.ensure_rendered(hub.live_store, drafts.get_draft_chunk(fig.dc))
    assert err is not None and "too-large" in err
    meta = drafts.draft_chunk_meta(fig.handle)
    assert meta["render"]["failed"]["version"] >= 1
    assert meta["figure"]["render_pending"] is True

    err2 = rf.ensure_rendered(hub.live_store, drafts.get_draft_chunk(fig.dc))
    assert err2 == err
    assert calls == []  # never rasterised; second call was marker-only

    # structure edit bumps the version -> marker no longer matches -> retry
    monkeypatch.setattr(rf, "VIEW_MAX_ATOMS", 20000)
    StructureHandler(hub=hub).edit(
        id="co", ops=[{"op": "add_atom", "element": "N", "frac": [0.3, 0.2, 0.3]}]
    )
    assert rf.ensure_rendered(hub.live_store, drafts.get_draft_chunk(fig.dc)) is None
    assert calls == [1]
    meta = drafts.draft_chunk_meta(fig.handle)
    assert "failed" not in meta["render"] and meta["render"]["cached_key"]
    # fresh: the cheap version check short-circuits (no further load/render)
    assert rf.ensure_rendered(hub.live_store, drafts.get_draft_chunk(fig.dc)) is None
    assert calls == [1]


def test_view_key_includes_bonds(hub: Hub) -> None:
    from precis.render.figure import view_key, view_recipe

    sh = StructureHandler(hub=hub)
    sh.put(id="co", text=_PD)
    nobond = json.loads(_PD)
    nobond["ops"] = nobond["ops"][:2]
    sh.put(id="co2", text=json.dumps(nobond))
    store = hub.live_store
    scenes = []
    for slug in ("co", "co2"):
        ref = store.get_ref(kind="structure", id=slug)
        assert ref is not None
        scenes.append(store.structure_load(ref.id)[0])
    recipe = view_recipe("co", None, None, 2)
    assert view_key(scenes[0], recipe) != view_key(scenes[1], recipe)


# ── se:<design> source: resolves a bound block to its structure ─────────


def _se_design(hub: Hub, slug: str, bindings: dict[str, str]) -> None:
    """An se design with one block per ``bindings`` entry, each bound to the
    named structure (no ports)."""
    ops: list[dict] = []
    for block, design in bindings.items():
        ops.append({"op": "add_block", "name": block, "envelope": "sphere:r2e-09"})
        ops.append({"op": "bind_structure", "block": block, "design": design})
    hub.sibling("se").put(id=slug, text=json.dumps({"ops": ops}))


def test_se_source_resolves_block_and_renders(hub: Hub, tmp_path: Path) -> None:
    StructureHandler(hub=hub).put(id="co-b", text=_PD)
    _se_design(hub, "buds", {"hub": "co-b"})
    ref, fig = _draft(hub, {"source": "se:buds", "block": "hub", "refine": 0})
    assert fig.meta["render"]["source"] == {
        "kind": "structure",
        "slug": "co-b",
        "se": "buds",
        "block": "hub",
    }
    res = export_docx(hub.live_store, ref, target_path=tmp_path / "a.docx")
    assert not [w for w in res.warnings if "render" in w]
    assert _blob(hub, fig.handle)[:8] == b"\x89PNG\r\n\x1a\n"
    assert _blob(hub, fig.handle) != _PLACEHOLDER_PNG

    # a geometry edit to the bound structure re-renders the se-sourced figure
    drafts = hub.live_store.drafts
    StructureHandler(hub=hub).edit(
        id="co-b", ops=[{"op": "add_atom", "element": "N", "frac": [0.3, 0.2, 0.3]}]
    )
    assert needs_render(hub.live_store, drafts.get_draft_chunk(fig.dc))


def test_view_figure_stale_after_slug_reminted(hub: Hub, tmp_path: Path) -> None:
    """A re-generate deletes the structure and re-mints the same slug at
    version 1; the version alone would match the stamp, the ref id does not."""
    ref, fig = _draft(hub)
    drafts = hub.live_store.drafts
    export_docx(hub.live_store, ref, target_path=tmp_path / "a.docx")
    meta = drafts.draft_chunk_meta(fig.handle)
    old = hub.live_store.get_ref(kind="structure", id="co")
    assert old is not None and meta["render"]["source_ref"] == old.id

    sh = StructureHandler(hub=hub)
    sh.delete(id="co")
    other = json.loads(_PD)
    other["ops"][1]["frac"] = [0.1, 0.25, 0.1]
    sh.put(id="co", text=json.dumps(other))
    new = hub.live_store.get_ref(kind="structure", id="co")
    assert new is not None and new.id != old.id
    assert int((new.meta or {}).get("version", 0)) == meta["render"]["source_version"]

    assert needs_render(hub.live_store, drafts.get_draft_chunk(fig.dc))
    export_docx(hub.live_store, ref, target_path=tmp_path / "b.docx")
    assert drafts.draft_chunk_meta(fig.handle)["render"]["source_ref"] == new.id


def test_se_source_single_bound_block_needs_no_block(hub: Hub) -> None:
    StructureHandler(hub=hub).put(id="co-b", text=_PD)
    _se_design(hub, "buds", {"only": "co-b"})
    _ref, fig = _draft(hub, {"source": "se:buds"})
    assert fig.meta["render"]["source"]["block"] == "only"
    assert fig.meta["render"]["source"]["slug"] == "co-b"


def test_se_source_put_reply_names_provenance(hub: Hub) -> None:
    StructureHandler(hub=hub).put(id="co-b", text=_PD)
    _se_design(hub, "buds", {"hub": "co-b"})
    pid = int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    d = DraftHandler(hub=hub)
    d.put(id="g", title="T", project=pid)
    body = d.put(
        id="g",
        chunk_kind="figure",
        text="Fig.",
        meta={"view": {"source": "se:buds", "block": "hub"}},
    ).body
    assert "se:buds block 'hub' -> structure:co-b v" in body


def test_se_source_ambiguous_or_unbound_block(hub: Hub) -> None:
    sh = StructureHandler(hub=hub)
    sh.put(id="co-a", text=_PD)
    sh.put(id="co-b", text=_PD)
    _se_design(hub, "two", {"left": "co-a", "right": "co-b"})
    with pytest.raises(BadInput, match=r"2 structure-bound blocks.*'left', 'right'"):
        _draft_with(hub, {"source": "se:two"})
    with pytest.raises(BadInput, match="not bound to a structure"):
        _draft_with(hub, {"source": "se:two", "block": "middle"})


def test_se_source_design_without_bindings(hub: Hub) -> None:
    hub.sibling("se").put(
        id="bare",
        text=json.dumps({"ops": [{"op": "add_block", "name": "a"}]}),
    )
    with pytest.raises(BadInput, match="no block bound"):
        _draft_with(hub, {"source": "se:bare"})


def test_se_source_unknown_design(hub: Hub) -> None:
    with pytest.raises(NotFound, match="se design"):
        _draft_with(hub, {"source": "se:nope"})
