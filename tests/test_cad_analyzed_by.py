"""Attached-models v1 — `analyzed-by` links with version-pinned staleness.

The mechanism under test (attached-models-layer.md): `cad_save` records a
content sha in `ref_events` on every save; attaching an analysis pins the
current sha into `links.meta`; a later *content-changing* save makes the
pin mismatch — the links view flags it and the `analysis-stale` condition
probe fires (which rides the existing gripe/alert lane).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cad import CadHandler

_V1 = "component bracket\nslab add box:w60d40h10\n"
_V2 = "component bracket\nslab add box:w60d40h12\n"  # changed geometry
_V1_RESTYLED = "component bracket\nslab  add   box:w60d40h10\n"  # same content


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


def _shas(store, ref_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload->>'sha' FROM ref_events "
            "WHERE ref_id = %s AND source = 'cad' AND event = 'saved' "
            "ORDER BY event_id",
            (ref_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _finding(store, title: str) -> int:
    ref = store.insert_ref(kind="finding", slug=None, title=title, meta={})
    return ref.id


def test_cad_save_records_a_content_sha_event(cad, store):
    cad.put(id="anchor_demo", text=_V1)
    ref = store.get_ref(kind="cad", id="anchor_demo")
    (sha1,) = _shas(store, ref.id)
    assert sha1 and len(sha1) == 16
    # a content-identical re-save (whitespace only) keeps the same sha —
    # staleness is content-driven, not save-driven
    cad.put(id="anchor_demo", text=_V1_RESTYLED)
    shas = _shas(store, ref.id)
    assert shas == [sha1, sha1]
    # a real geometry change moves it
    cad.put(id="anchor_demo", text=_V2)
    assert _shas(store, ref.id)[-1] != sha1


def test_attach_pin_and_staleness_lifecycle(cad, store):
    cad.put(id="analyzed_demo", text=_V1)
    ref = store.get_ref(kind="cad", id="analyzed_demo")
    fi = _finding(store, "FEA: max stress 42 MPa at the slab root")

    out = cad.link(id="analyzed_demo", target=f"finding:{fi}", rel="analyzed-by")
    assert "pinned design version" in out.body
    (lk,) = store.links_for(ref.id, direction="out", relation="analyzed-by")
    assert lk.dst_ref_id == fi and lk.meta.get("sha")

    # fresh attachment: links view carries no stale flag, probe is green
    from precis.workers.conditions import _probe_analysis_stale

    assert "STALE" not in cad.get(id="analyzed_demo", view="links").body
    assert not [f for f in _probe_analysis_stale(store) if f"fi{fi}" in f.key]

    # the design changes under the analysis → both channels flag it,
    # and the bare get's one-hop footer warns too (no second call needed)
    cad.put(id="analyzed_demo", text=_V2)
    assert "STALE" in cad.get(id="analyzed_demo", view="links").body
    assert "STALE" in cad.get(id="analyzed_demo").body
    fired = [f for f in _probe_analysis_stale(store) if f"fi{fi}" in f.key]
    assert fired and fired[0].key == f"analysis-stale:analyzed_demo/fi{fi}"
    assert "re-run" in fired[0].detail

    # re-attaching refreshes the pin → auto-clears both channels
    cad.link(id="analyzed_demo", target=f"finding:{fi}", rel="analyzed-by")
    assert "STALE" not in cad.get(id="analyzed_demo", view="links").body
    assert not [f for f in _probe_analysis_stale(store) if f"fi{fi}" in f.key]

    # detach removes the edge
    out = cad.link(
        id="analyzed_demo", target=f"finding:{fi}", rel="analyzed-by", mode="remove"
    )
    assert "detached 1" in out.body
    assert not store.links_for(ref.id, direction="out", relation="analyzed-by")


def test_analyzed_by_target_must_be_an_analysis_kind(cad, store):
    cad.put(id="analyzed_demo2", text=_V1)
    other = store.insert_ref(kind="cad", slug="not_an_analysis", title="x", meta={})
    with pytest.raises(BadInput, match="finding or estimate"):
        cad.link(id="analyzed_demo2", target=f"cad:{other.slug}", rel="analyzed-by")


def test_export_event_carries_the_content_sha(cad, store, tmp_path):
    from precis.cad.export import manifold_available

    if not manifold_available():
        pytest.skip("manifold3d not installed")
    cad.put(id="export_demo", text=_V1)
    ref = store.get_ref(kind="cad", id="export_demo")
    out = cad.get(id="export_demo", view="stl", args={"path": str(tmp_path / "d.stl")})
    assert "design version" in out.body and "recorded" in out.body
    with store.pool.connection() as conn:
        (sha,) = conn.execute(
            "SELECT payload->>'sha' FROM ref_events "
            "WHERE ref_id = %s AND source = 'cad' AND event = 'exported'",
            (ref.id,),
        ).fetchone()
    # the exported sha equals the save-time sha — same content anchor
    assert sha == _shas(store, ref.id)[-1]
