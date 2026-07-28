"""``/refs/pathway/<id>`` — the autocatpath reaction-energetics detail page.

``pathway`` is an EXTERNAL plugin kind (the autocatpath bridge): its handler
isn't loaded in this test process (or a dev session), so — unlike most
other kinds — this page is verified WITHOUT ever dispatching ``get()``.
Everything is monkeypatched straight onto the fake ``Store`` (mirroring
``test_quest_dashboard.py``'s pattern), never onto ``runtime`` — proving
the route really is read-only-off-storage for this kind. Modeled on
``tests/precis_web/test_quest_dashboard.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from .conftest import make_ref


def _seed_pathway(store: Any, *, meta: dict[str, Any], body_text: str | None) -> None:
    """Wire a pathway ref (id=171696) + its linked structures + one
    ``pathway_body`` chunk into the fake store, the same way
    ``test_quest_dashboard.py`` layers extra refs onto ``fetch_refs_by_ids``."""
    pathway_ref = make_ref(
        id=171696,
        kind="pathway",
        slug="q1cand-abc123-rx-def456",
        title="NO -> NH3 on Cu(111): candidate rx pathway",
        meta=meta,
    )
    extra_refs = {171696: pathway_ref}
    if "candidate_ref" in meta:
        extra_refs[meta["candidate_ref"]] = make_ref(
            id=meta["candidate_ref"], kind="structure", title="Cu(111) slab"
        )
    for label, sid in (meta.get("structure_refs") or {}).items():
        extra_refs[sid] = make_ref(id=sid, kind="structure", title=f"{label} adsorbed")

    original_fetch = store.fetch_refs_by_ids

    def fetch(ids: Any, **kw: Any) -> dict[int, Any]:
        out = dict(original_fetch(ids, **kw))
        for i in ids:
            if i in extra_refs:
                out[i] = extra_refs[i]
        return out

    store.fetch_refs_by_ids = fetch  # type: ignore[method-assign]

    blocks = []
    if body_text is not None:
        blocks.append(
            SimpleNamespace(pos=0, text=body_text, chunk_kind="pathway_body", meta={})
        )
    store._conv_blocks[171696] = blocks


def test_pathway_detail_renders_dedicated_page_not_generic(client, runtime) -> None:
    _seed_pathway(
        runtime.store,
        meta={
            "status": "ready",
            "rate_Ea": 0.87,
            "n_structures": 5,
            "slice": 2,
            "produced_by": "autocatpath_explore",
            "autocatpath_version": "0.4.1",
            "candidate_ref": 166700,
            "structure_refs": {"NO": 166708, "NH3": 166716},
            "results": {
                "substrate": "Cu(111)",
                "target": "NH3",
                "backend": "emt",
                "energy_reference": "NO*+3H*",
                "nodes": ["NO*", "NOH*", "NH3*"],
                "edges": [("NO*", "NOH*"), ("NOH*", "NH3*")],
                "warnings": ["low-confidence barrier on edge 2"],
            },
            "config_snapshot_yaml": "backend: emt\nslice: 2\n",
        },
        body_text="## Methods\n\nRelaxed via EMT; barriers via NEB.",
    )
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    # Dedicated-page markers — no equivalent in the generic detail template.
    assert "Energetics" in resp.text
    assert "Linked structures" in resp.text
    assert "Pathway meta" in resp.text
    # Header fields.
    assert "STATUS:ready" in resp.text
    assert "Ea = 0.87 eV" in resp.text
    assert "autocatpath 0.4.1" in resp.text
    # Meta panel.
    assert "Cu(111) slab" in resp.text
    assert "/refs/structure/166700" in resp.text
    # Linked structures.
    assert "NO adsorbed" in resp.text
    assert "NH3 adsorbed" in resp.text
    assert "/refs/structure/166708" in resp.text
    # Energetics summary, defensively pulled from meta['results'].
    assert "Cu(111)" in resp.text
    assert "emt" in resp.text
    assert "low-confidence barrier on edge 2" in resp.text
    # Body chunk, rendered.
    assert "Relaxed via EMT" in resp.text
    # Never dispatched a handler get() for this kind — rendered purely off
    # stored data (the whole point: the autocatpath handler isn't loaded here).
    pathway_calls = [a for v, a in runtime.calls if a.get("kind") == "pathway"]
    assert pathway_calls == []


def test_pathway_detail_sparse_meta_no_500(client, runtime) -> None:
    """A pathway with almost nothing in ``meta`` still renders — every
    field the page reads is optional, per the autocatpath data model."""
    _seed_pathway(runtime.store, meta={}, body_text=None)
    resp = client.get("/refs/pathway/171696")
    assert resp.status_code == 200
    assert "none recorded" in resp.text
    assert "no linked adsorbate/intermediate structures recorded" in resp.text
    assert "no computed results recorded yet" in resp.text
    assert "no body chunk recorded for this pathway" in resp.text
