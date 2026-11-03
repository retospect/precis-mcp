"""gr549574 — pathway titles name the candidate structure, and the
``precis pathway retitle`` maintenance command renames existing ones.

gr553416 — the candidate slot renders the readable catalyst label off the
structure's recorded construction (``design_revisions`` ops +
``meta.params``) instead of the spaced name, when construction is
recorded.

Engine-free: synthetic ``PathwayArtifact``s through the real
``persist_result``/``retitle_plan`` store paths — no autocatpath import.
"""

from __future__ import annotations

import argparse
import copy
from typing import Any

import pytest

from precis.design.history import record_revision
from precis.store import Store
from precis_pathway import cli as pw_cli
from precis_pathway.persist import (
    pathway_title,
    persist_result,
    retitle_plan,
    slab_name,
)
from precis_pathway.types import PathwayArtifact

_RESULTS: dict[str, Any] = {
    "substrate": "NO",
    "target": "NH3",
    "pathway": ["A", "B"],
    "models": ["emt"],
    "n_samples": 1,
}

_GRAPH: dict[str, Any] = {
    "nodes": [
        {"id": "A", "rel_energy": 0.0},
        {"id": "B", "rel_energy": 0.2},
    ],
    "links": [
        {"source": "A", "target": "B", "kind": "reaction", "barrier": 0.4},
    ],
}


def _artifact(element: str = "Pd", **slab_extra: Any) -> PathwayArtifact:
    return {
        "content_key": "ck-title",
        "autocatpath_version": "test",
        "config": {"slab": {"element": element, **slab_extra}},
        "config_snapshot_yaml": "",
        "results_json": dict(_RESULTS),
        "graph_json": copy.deepcopy(_GRAPH),
        "methods_md": "synthetic fixture — no pipeline run",
        "structures_extxyz": {},
        "warnings": [],
    }


def _structure(store: Store, slug: str, title: str) -> Any:
    return store.insert_ref(kind="structure", slug=slug, title=title, meta={})


def _pathway(
    store: Store, slug: str, title_text: str | None = None, **meta: Any
) -> Any:
    """A pathway ref as the dispatch mints it: the ``(computing)`` seed
    title + whatever meta the mint stamps (``candidate_ref`` et al)."""
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title=title_text or f"pathway {slug} (computing)",
        meta={"status": "computing", **meta},
    )


def _run_retitle(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any, *, apply: bool
) -> tuple[str, str]:
    monkeypatch.setattr(pw_cli, "_open_store", lambda _args: store)
    pw_cli.run(
        argparse.Namespace(pathway_cmd="retitle", apply=apply, database_url=None)
    )
    out = capsys.readouterr()
    return out.out, out.err


# ── write-time title ──────────────────────────────────────────────


def test_persist_titles_the_candidate_structure(store: Store) -> None:
    """The spec form (gr549574, Reto 2026-11-02): the candidate's own name
    with '-' rendered as spaces — not the bare slab element."""
    cand = _structure(
        store, "q1cand-abc123", "Pd111-Ta-subsurf-1-vacancy-surf-colocated"
    )
    pw = _pathway(store, "q1cand-abc123-rx-deadbeef01", candidate_ref=int(cand.id))

    persist_result(store, int(pw.id), _artifact(), pathway_slug=pw.slug, ingest=False)

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd111 Ta subsurf 1 vacancy surf colocated"


def test_persist_titles_the_candidate_off_the_slug(store: Store) -> None:
    """No ``candidate_ref`` stamp (a pre-stamp mint): the pathway slug's
    ``q<quest>cand-<digest>`` prefix resolves the same structure."""
    _structure(store, "q1cand-abc123", "Pd111-B-subsurf-1")
    pw = _pathway(store, "q1cand-abc123-rx-deadbeef01")

    persist_result(store, int(pw.id), _artifact(), pathway_slug=pw.slug, ingest=False)

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd111 B subsurf 1"


def test_persist_titles_the_slab_without_a_candidate(store: Store) -> None:
    """No candidate, no ``slab.miller``: the element alone — a missing
    miller is the engine default, not a stated facet."""
    pw = _pathway(store, "demo-pd")
    persist_result(store, int(pw.id), _artifact(), pathway_slug="demo-pd", ingest=False)
    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd"


def test_persist_titles_the_linked_structure(store: Store) -> None:
    """No ``candidate_ref`` stamp and no q-cand slug: the quest harvest's
    ``related-to`` structure link singles out the candidate (one linked
    structure = the candidate)."""
    cand = _structure(store, "cand-struct", "Pd111-B-surf-1")
    pw = _pathway(store, "linked-pw")
    store.add_link(
        src_ref_id=int(cand.id),
        dst_ref_id=int(pw.id),
        relation="related-to",
        set_by="system",
    )

    persist_result(
        store, int(pw.id), _artifact(), pathway_slug="linked-pw", ingest=False
    )

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd111 B surf 1"


def test_persist_ambiguous_links_fall_back_to_slab(store: Store) -> None:
    """Several related-to structures single out nothing (the per-state
    intermediates ride the same relation): the slab names the title."""
    a = _structure(store, "cand-a", "Pd111-B-surf-1")
    b = _structure(store, "cand-b", "Pd111-Ta-surf-1")
    pw = _pathway(store, "ambiguous-pw")
    for s in (a, b):
        store.add_link(
            src_ref_id=int(s.id),
            dst_ref_id=int(pw.id),
            relation="related-to",
            set_by="system",
        )

    persist_result(
        store, int(pw.id), _artifact(), pathway_slug="ambiguous-pw", ingest=False
    )

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd"


def test_persist_keeps_title_when_candidate_vanished(store: Store) -> None:
    """A named candidate gone without even a tombstone: keep whatever the
    ref already says — the same keep rule ``stored_title`` applies."""
    pw = _pathway(store, "gone-cand", candidate_ref=424242, title_text="NO → NH3 on Pd")

    persist_result(
        store, int(pw.id), _artifact(), pathway_slug="gone-cand", ingest=False
    )

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd"


def test_persist_keeps_title_without_an_element(store: Store) -> None:
    pw = _pathway(store, "no-element", title_text="hand-made title")
    artifact = _artifact()
    artifact["config"] = {"slab": {}}
    persist_result(store, int(pw.id), artifact, pathway_slug="no-element", ingest=False)
    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "hand-made title"


def test_persist_titles_a_deleted_candidate(store: Store) -> None:
    """A tombstone still names the catalyst, marked deleted."""
    cand = _structure(store, "q1cand-gone", "Pd111-B-subsurf-1")
    pw = _pathway(store, "q1cand-gone-rx-deadbeef01", candidate_ref=int(cand.id))
    store.retire_ref(int(cand.id))

    persist_result(store, int(pw.id), _artifact(), pathway_slug=pw.slug, ingest=False)

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd111 B subsurf 1 (deleted)"


def test_slab_name_reads_the_miller_facet() -> None:
    assert slab_name({"element": "Pd"}) == "Pd"
    assert slab_name({"element": "Pt", "miller": [1, 0, 0]}) == "Pt(100)"
    # negative indices get the bar-free comma form
    assert slab_name({"element": "Pd", "miller": [1, -1, 1]}) == "Pd(1,-1,1)"
    # no element: unnameable — the caller keeps the old title
    assert slab_name({}) is None and slab_name({"element": ""}) is None


def test_pathway_title_plain_call_unchanged() -> None:
    """The insert-time call (no candidate in scope) still yields the
    slab form — never tier/potential/seed count (gr549574 spec)."""
    assert pathway_title(_artifact()) == "NO → NH3 on Pd"
    assert pathway_title(_artifact(miller=[1, 1, 1])) == "NO → NH3 on Pd(111)"


# ── the retitle door ──────────────────────────────────────────────


def _stored_pathway(store: Store, slug: str, title: str, **meta: Any) -> Any:
    """A finished pathway as prod holds it: spec-bearing meta under an
    old bare-element title."""
    results = dict(_RESULTS)
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title=title,
        meta={
            "status": "ready",
            "results": results,
            "config": {"slab": {"element": "Pd"}},
            **meta,
        },
    )


def test_retitle_plan_lists_only_diffs(store: Store) -> None:
    cand = _structure(
        store, "q1cand-abc123", "Pd111-Ta-subsurf-1-vacancy-surf-colocated"
    )
    stale = _stored_pathway(
        store,
        "q1cand-abc123-rx-deadbeef01",
        "NO → NH3 on Pd",
        candidate_ref=int(cand.id),
    )
    already = _stored_pathway(
        store,
        "already-spec",
        "NO → NH3 on Pd",
    )
    computing = _pathway(store, "still-computing")

    checked, changes = retitle_plan(store)

    assert checked == 3
    assert [c.ref_id for c in changes] == [int(stale.id)]
    assert changes[0].old_title == "NO → NH3 on Pd"
    assert changes[0].new_title == (
        "NO → NH3 on Pd111 Ta subsurf 1 vacancy surf colocated"
    )
    # planning wrote nothing
    assert store.fetch_refs_by_ids([int(stale.id)])[int(stale.id)].title == (
        "NO → NH3 on Pd"
    )
    assert int(already.id) and int(computing.id)  # seeded, untouched


def test_retitle_keeps_title_when_candidate_vanished(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """candidate_ref names a ref that is gone without even a tombstone:
    keep the old title rather than guess the slab — and say so."""
    gone = _stored_pathway(
        store,
        "no-slug-candidate",
        "NO → NH3 on Pd",
        candidate_ref=424242,
    )
    checked, changes = retitle_plan(store)
    assert checked == 1
    assert [c.new_title for c in changes] == ["NO → NH3 on Pd"]
    assert changes[0].note is not None

    out, _err = _run_retitle(store, monkeypatch, capsys, apply=True)
    assert f"{gone.id}: NO → NH3 on Pd (kept" in out
    assert store.fetch_refs_by_ids([int(gone.id)])[int(gone.id)].title == (
        "NO → NH3 on Pd"
    )


def test_retitle_marks_ambiguous_linked_structures(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Several related-to structures and no candidate_ref: nothing is
    singled out — the slab/old title stands and the dry-run line says
    why."""
    a = _structure(store, "cand-a", "Pd111-B-surf-1")
    b = _structure(store, "cand-b", "Pd111-Ta-surf-1")
    pw = _stored_pathway(store, "ambiguous-pw", "NO → NH3 on Pd")
    for s in (a, b):
        store.add_link(
            src_ref_id=int(s.id),
            dst_ref_id=int(pw.id),
            relation="related-to",
            set_by="system",
        )

    out, _err = _run_retitle(store, monkeypatch, capsys, apply=False)

    assert f"{pw.id}: NO → NH3 on Pd (kept — ambiguous: 2 related-to structures)" in out
    assert store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)].title == ("NO → NH3 on Pd")


def test_retitle_dry_run_lists_and_writes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    cand = _structure(store, "q1cand-abc123", "Pd111-B-subsurf-1")
    pw = _stored_pathway(
        store,
        "q1cand-abc123-rx-deadbeef01",
        "NO → NH3 on Pd",
        candidate_ref=int(cand.id),
    )

    out, err = _run_retitle(store, monkeypatch, capsys, apply=False)

    assert f"{pw.id}: NO → NH3 on Pd → NO → NH3 on Pd111 B subsurf 1" in out
    assert "DRY-RUN" in err
    assert store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)].title == ("NO → NH3 on Pd")


def test_retitle_apply_is_idempotent_and_keeps_history(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    cand = _structure(store, "q1cand-abc123", "Pd111-B-subsurf-1")
    pw = _stored_pathway(
        store,
        "q1cand-abc123-rx-deadbeef01",
        "NO → NH3 on Pd",
        candidate_ref=int(cand.id),
    )

    out, _err = _run_retitle(store, monkeypatch, capsys, apply=True)
    assert f"{pw.id}: NO → NH3 on Pd → NO → NH3 on Pd111 B subsurf 1" in out
    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd111 B subsurf 1"

    # the old title survives as a title_changed ref_event — the same
    # retitle door memory/todo edits use.
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event, payload FROM ref_events WHERE ref_id = %s",
            (int(pw.id),),
        ).fetchall()
    events = [(e, p) for e, p in rows if e == "title_changed"]
    assert events == [
        (
            "title_changed",
            {
                "old_title": "NO → NH3 on Pd",
                "new_title": "NO → NH3 on Pd111 B subsurf 1",
            },
        )
    ]

    # second apply: nothing left to do
    out, err = _run_retitle(store, monkeypatch, capsys, apply=True)
    assert "0 to retitle" in err and out == ""
    checked, changes = retitle_plan(store)
    assert changes == []


# ── gr553416 — the recipe-derived catalyst label ─────────────────

_RECIPE_SUBSURF_VAC: list[dict[str, Any]] = [
    {"op": "slab", "element": "Pd", "size": [3, 3, 4]},
    {
        "op": "set_element",
        "element": "Ta",
        "site": {"type": "subsurface", "near": "aPd35"},
    },
    {"op": "vacancy", "atom": "aPd35"},
]


def test_persist_titles_the_candidate_by_its_recipe(store: Store) -> None:
    """Recorded construction (design_revisions) renders the readable
    label — the gripe's own example — instead of the spaced name."""
    cand = _structure(
        store, "q1cand-abc123", "Pd111-Ta-subsurf-1-vacancy-surf-colocated"
    )
    record_revision(store, int(cand.id), rev=1, ops=list(_RECIPE_SUBSURF_VAC))
    pw = _pathway(store, "q1cand-abc123-rx-deadbeef01", candidate_ref=int(cand.id))

    persist_result(store, int(pw.id), _artifact(), pathway_slug=pw.slug, ingest=False)

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == (
        "NO → NH3 on Pd(111) + Ta (subsurface) + vacancy (surface, co-located)"
    )


def test_persist_recipe_follows_the_derived_from_parent(store: Store) -> None:
    """A derived candidate records only its delta ops; the parent's
    revisions hold the slab — the label still names host and facet."""
    parent = _structure(store, "q1cand-base", "Pd111")
    record_revision(
        store,
        int(parent.id),
        rev=1,
        ops=[{"op": "slab", "element": "Pd", "size": [3, 3, 4]}],
    )
    cand = store.insert_ref(
        kind="structure",
        slug="q1cand-abc123",
        title="Pd111-Cu-surf-2",
        meta={"derived_from": "q1cand-base"},
    )
    record_revision(
        store,
        int(cand.id),
        rev=1,
        ops=[
            {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
            {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
        ],
    )
    store.add_link(
        src_ref_id=int(cand.id),
        dst_ref_id=int(parent.id),
        relation="derived-from",
        set_by="system",
    )
    pw = _pathway(store, "q1cand-abc123-rx-deadbeef01", candidate_ref=int(cand.id))

    persist_result(store, int(pw.id), _artifact(), pathway_slug=pw.slug, ingest=False)

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd(111) + 2 Cu (surface substitution)"


def test_lineage_linked_full_spec_does_not_climb(store: Store) -> None:
    """A quest ``parent`` lineage link is NOT a derive: the child is a
    full spec (top-level cell → rev-1 ops without a cell op), so it must
    not inherit the parent's slab and modifications. Only a real derive
    (``meta.derived_from``) climbs."""
    from precis.design.history import recipe_ops

    parent = _structure(store, "q1cand-base", "Pd111-Ta-subsurf-1")
    record_revision(
        store,
        int(parent.id),
        rev=1,
        ops=[
            {"op": "slab", "element": "Pd", "size": [3, 3, 4]},
            {"op": "set_element", "element": "Ta", "site": {"type": "subsurface"}},
        ],
    )
    child_ops = [
        {"op": "set_element", "element": "Cu", "site": {"type": "top"}},
    ]
    cand = _structure(store, "q1cand-abc123", "Pd111-Cu-surf-1")
    record_revision(store, int(cand.id), rev=1, ops=list(child_ops))
    store.add_link(
        src_ref_id=int(cand.id),
        dst_ref_id=int(parent.id),
        relation="derived-from",
        set_by="system",
    )

    assert recipe_ops(store, int(cand.id)) == child_ops


def test_retitle_plan_uses_the_recipe_label(store: Store) -> None:
    cand = _structure(store, "q1cand-abc123", "Pd111-Pt-hollow-1")
    record_revision(
        store,
        int(cand.id),
        rev=1,
        ops=[
            {"op": "slab", "element": "Pd", "size": [3, 3, 4]},
            {"op": "add_atom_site", "element": "Pt", "site": {"type": "hollow"}},
        ],
    )
    stale = _stored_pathway(
        store,
        "q1cand-abc123-rx-deadbeef01",
        "NO → NH3 on Pd111 Pt hollow 1",
        candidate_ref=int(cand.id),
    )

    checked, changes = retitle_plan(store)

    assert checked == 1
    assert changes[0].new_title == "NO → NH3 on Pd(111) + Pt adatom (hollow)"
    assert store.fetch_refs_by_ids([int(stale.id)])[int(stale.id)].title == (
        "NO → NH3 on Pd111 Pt hollow 1"
    )


def test_no_recorded_construction_falls_back_to_spaced_name(
    store: Store,
) -> None:
    """A candidate with no design_revisions keeps the gr549574 form —
    the recipe is the trigger, not the slug."""
    cand = _structure(store, "q1cand-abc123", "Pd111-Ta-subsurf-1")
    pw = _pathway(store, "q1cand-abc123-rx-deadbeef01", candidate_ref=int(cand.id))

    persist_result(store, int(pw.id), _artifact(), pathway_slug=pw.slug, ingest=False)

    after = store.fetch_refs_by_ids([int(pw.id)])[int(pw.id)]
    assert after.title == "NO → NH3 on Pd111 Ta subsurf 1"
