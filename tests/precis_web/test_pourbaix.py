"""``/pourbaix/{handle}`` — the resting-state surface-Pourbaix page.

Real-store integration (the route's SQL is real); the sweeps are synthetic
payloads through :func:`precis_pathway.surface_pourbaix.sweep`, the same
shape ``coverage_job._dispatch`` stamps as ``meta.che_sweep``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.structure import StructureHandler
from precis.store.types import Tag
from precis_pathway import surface_pourbaix as sp
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.pourbaix_svg import map_svg

AREA = 10.0


def _payload(model: str, gammas: dict[str, list[float]]) -> dict[str, Any]:
    ads = {}
    for frag, gs in gammas.items():
        ads[frag] = {
            "mu": -3.0,
            "site": "fcc",
            "c_ads": 0.0,
            "points": [
                {
                    "n": i + 1,
                    "theta": (i + 1) / 4,
                    "energy": -100.0 + g * AREA,
                    "gamma": g,
                    "converged": True,
                    "detached": False,
                }
                for i, g in enumerate(gs)
            ],
            "best": {"theta": 0.25, "n": 1, "gamma": min(gs)},
        }
    return {
        "model": model,
        "facets": [
            {
                "facet": "111",
                "miller": [1, 1, 1],
                "area": AREA,
                "n_top": 4,
                "n_slab": 36,
                "e_slab": -100.0,
                "e_bulk_per_atom": -3.0,
                "gamma_clean": 0.1,
                "adsorbates": ads,
                "winner": {"adsorbate": None, "theta": 0.0, "gamma": 0.0},
            }
        ],
        "warnings": [],
    }


def _sweep() -> dict[str, Any]:
    return sp.sweep(
        {"m": _payload("m", {"H": [-0.02]})},
        u_window=(-0.5, 0.5),
        grid=21,
        sigma_e_ev=0.05,
    )


@pytest.fixture
def pb_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_structure(
    runtime_with_store, slug: str = "pd_map", *, with_scan: bool = True
) -> int:
    hub = runtime_with_store.hub
    store = runtime_with_store.store
    StructureHandler(hub=hub).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {
                    "a": 10.0,
                    "b": 10.0,
                    "c": 10.0,
                    "pbc": [True, True, False],
                },
                "ops": [{"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]}],
            }
        ),
    )
    st = store.get_ref(kind="structure", id=slug)
    if with_scan:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="scan m",
            parent_id=st.id,
            meta={
                "job_type": "surface_coverage_scan",
                "anchor_key": hashlib.sha256(slug.encode("utf-8")).hexdigest(),
                "model": "m",
                "engine_version": "0.24.0",
                "corrections": None,
                "che_sweep": _sweep(),
            },
        )
        store.add_tag(
            job.id,
            Tag.closed("STATUS", "succeeded"),
            set_by="agent",
            replace_prefix=True,
        )
    return st.id


def test_pourbaix_unknown_handle_404(pb_client: TestClient) -> None:
    r = pb_client.get("/pourbaix/nope")
    assert r.status_code == 404


def test_pourbaix_structure_page_shows_map_provenance_and_incomplete(
    pb_client: TestClient, runtime_with_store
) -> None:
    sid = _seed_structure(runtime_with_store)
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    text = r.text
    assert "<svg" in text and "U (V vs RHE)" in text
    # provenance footer: anchor key, model, engine, corrections, scan job
    assert "anchor_key" in text and "0.24.0" in text
    assert "scan jobs:" in text
    # one model → the model-form band is absent and the page says incomplete
    assert "incomplete:" in text and "model-form" in text
    assert "uncorrected anchors" in text


def test_pourbaix_structure_page_no_anchors(
    pb_client: TestClient, runtime_with_store
) -> None:
    _seed_structure(runtime_with_store, with_scan=False)
    st = runtime_with_store.store.get_ref(kind="structure", id="pd_map")
    r = pb_client.get(f"/pourbaix/st{st.id}")
    assert r.status_code == 200
    assert "no anchors" in r.text


# ── slice 3: the staircase panel on the shared slider bank ───────────────


def _pathway_results() -> dict[str, Any]:
    """A 0.25.0-shape results.json (the ``che_affine`` export + HER)."""
    return {
        "substrate": "NO",
        "target": "NH3",
        "network_digest": "d" * 16,
        "corrections": {"gas": "catsel19"},
        "nodes": {"*": {"mean": 0.0, "std": 0.0}},
        "U_L": -0.15,
        "che_affine": {
            "scale": "RHE",
            "T": 298.15,
            "pH": 0.0,
            "per_route": [
                {
                    "route": "* -> NO -> N+OH -> *~nh3",
                    "steps": [
                        {"edge": "no_ads", "delta_g0": -0.4, "n_e": 0},
                        {"edge": "no_reduce", "delta_g0": 0.3, "n_e": 2},
                        {"edge": "nh3_desorb", "delta_g0": 0.3, "n_e": 0},
                    ],
                    "U_L": -0.15,
                    "U_opt": -0.1,
                    "span_at_UL": 0.7,
                }
            ],
        },
        "her": {
            "U_L_her": -0.2,
            "product": "H2",
            "leaf": "*~h2",
            "dG_H_star": -0.1,
            "edges": [
                {
                    "step": "Volmer",
                    "edge": "*->H",
                    "reactant": "*",
                    "product": "H",
                    "delta_g0": -0.1,
                    "n_e": 1,
                }
            ],
        },
    }


def _pathway(store: Any, slug: str, results: dict[str, Any] | None) -> Any:
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title="NO → NH3 on Pd",
        meta={
            "status": "ready",
            "autocatpath_version": "0.25.0",
            "results": results or {},
        },
    )


def test_pourbaix_page_shows_map_and_staircase_on_one_bank(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store)
    pw = _pathway(store, "pw_linked", _pathway_results())
    store.add_link(
        src_ref_id=sid, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    text = r.text
    # the shared slider bank
    assert 'id="pbx-u"' in text and 'id="pbx-ph"' in text
    # the map carries the U marker + the U_L/U_opt/U_L_her verticals
    assert "pbx-u-marker" in text
    assert "U_L1" in text and "U_opt1" in text and "U_L_her" in text
    # the staircase panel: rungs with affine terms, the pathway link
    assert "pbx-staircase" in text and "no_reduce" in text
    assert 'href="/refs/pathway/' in text
    assert "HER competitor" in text


def test_pourbaix_page_staircase_only_when_no_anchors(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    _seed_structure(runtime_with_store, with_scan=False)
    st = store.get_ref(kind="structure", id="pd_map")
    pw = _pathway(store, "pw_only", _pathway_results())
    store.add_link(
        src_ref_id=st.id, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    r = pb_client.get(f"/pourbaix/st{st.id}")
    assert r.status_code == 200
    assert "no anchors" in r.text
    assert "pbx-staircase" in r.text and "no_reduce" in r.text
    assert "unverified" in r.text  # no resting map — starting surface unverified


def test_pourbaix_page_map_only_when_no_pathway(
    pb_client: TestClient, runtime_with_store
) -> None:
    sid = _seed_structure(runtime_with_store)
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    assert "<svg" in r.text and "pbx-staircase" not in r.text


def _mk_quest(runtime_with_store, text: str = "map quest") -> int:
    from precis.handlers.quest import QuestHandler

    created = QuestHandler(hub=runtime_with_store.hub).put(text=text)
    match = re.search(r"qu(\d+)", created.body)
    assert match is not None
    return int(match.group(1))


def _converged(store: Any, sid: int, *, energy: float) -> None:
    """A converged relax — the measure the default (energy-min) frontier
    ranks the candidate on."""
    store.structure_record_run(
        sid,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=1,
        max_disp=0.0,
        energy=energy,
    )


def test_pourbaix_quest_page_maps_candidates(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store)
    qid = _mk_quest(runtime_with_store)
    store.add_link(src_ref_id=sid, dst_ref_id=qid, relation="serves")
    r = pb_client.get(f"/pourbaix/qu{qid}")
    assert r.status_code == 200
    assert f"st{sid}" in r.text and "<svg" in r.text
    # one surface → drawn directly; the picker still lists it
    assert r.text.count('class="pbx-entry') == 1
    assert f"?surface=st{sid}" in r.text


def _quest_with_two_surfaces(runtime_with_store) -> tuple[int, int, int]:
    """``(qid, sid_a, sid_b)`` — two anchored candidates linked in order
    a-then-b, with b the best-ranked (lower converged energy), so the
    default pick proves ranking beats link order."""
    store = runtime_with_store.store
    sid_a = _seed_structure(runtime_with_store, "pd_a")
    sid_b = _seed_structure(runtime_with_store, "pd_b")
    _converged(store, sid_a, energy=-1.0)
    _converged(store, sid_b, energy=-9.0)
    qid = _mk_quest(runtime_with_store)
    store.add_link(src_ref_id=sid_a, dst_ref_id=qid, relation="serves")
    store.add_link(src_ref_id=sid_b, dst_ref_id=qid, relation="serves")
    return qid, sid_a, sid_b


def test_pourbaix_quest_page_draws_one_surface_best_ranked(
    pb_client: TestClient, runtime_with_store
) -> None:
    qid, sid_a, sid_b = _quest_with_two_surfaces(runtime_with_store)
    r = pb_client.get(f"/pourbaix/qu{qid}")
    assert r.status_code == 200
    text = r.text
    # exactly one surface's panel is drawn — the best-ranked one (b),
    # not the first-linked (a)
    assert text.count('class="pbx-entry') == 1
    assert 'href="/structure/pd_b"' in text
    assert 'href="/structure/pd_a"' not in text
    # the picker lists every surface best-ranked first, each linking to
    # its own /pourbaix/<handle> page plus an on-page draw link
    assert text.index(f">st{sid_b}<") < text.index(f">st{sid_a}<")
    assert f"/pourbaix/st{sid_a}" in text and f"/pourbaix/st{sid_b}" in text
    assert f"?surface=st{sid_a}" in text and f"?surface=st{sid_b}" in text


def test_pourbaix_quest_page_surface_param_selects(
    pb_client: TestClient, runtime_with_store
) -> None:
    qid, sid_a, sid_b = _quest_with_two_surfaces(runtime_with_store)
    r = pb_client.get(f"/pourbaix/qu{qid}?surface=st{sid_a}")
    assert r.status_code == 200
    assert r.text.count('class="pbx-entry') == 1
    assert 'href="/structure/pd_a"' in r.text
    assert 'href="/structure/pd_b"' not in r.text
    # the slug and the bare ref_id address the same surface
    r = pb_client.get(f"/pourbaix/qu{qid}?surface=pd_a")
    assert 'href="/structure/pd_a"' in r.text
    r = pb_client.get(f"/pourbaix/qu{qid}?surface={sid_a}")
    assert 'href="/structure/pd_a"' in r.text


def test_pourbaix_quest_page_unknown_surface_falls_back_with_notice(
    pb_client: TestClient, runtime_with_store
) -> None:
    qid, _sid_a, _sid_b = _quest_with_two_surfaces(runtime_with_store)
    r = pb_client.get(f"/pourbaix/qu{qid}?surface=nope123")
    assert r.status_code == 200
    # quotes in the notice are HTML-escaped (repr → &#x27;)
    assert "no surface" in r.text and "nope123" in r.text
    assert "serves this quest" in r.text
    assert r.text.count('class="pbx-entry') == 1
    assert 'href="/structure/pd_b"' in r.text  # the best-ranked surface is drawn


def test_pourbaix_quest_page_surface_param_is_escaped(
    pb_client: TestClient, runtime_with_store
) -> None:
    qid, _sid_a, _sid_b = _quest_with_two_surfaces(runtime_with_store)
    r = pb_client.get(f"/pourbaix/qu{qid}?surface=<img src=x onerror=alert(1)>")
    assert r.status_code == 200
    assert "<img src=x" not in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text


def test_pourbaix_quest_page_compare_view_draws_all_labelled(
    pb_client: TestClient, runtime_with_store
) -> None:
    qid, _sid_a, _sid_b = _quest_with_two_surfaces(runtime_with_store)
    r = pb_client.get(f"/pourbaix/qu{qid}?view=compare")
    assert r.status_code == 200
    assert r.text.count('class="pbx-entry') == 2
    assert "comparison view" in r.text


def test_pourbaix_structure_page_has_no_surface_picker(
    pb_client: TestClient, runtime_with_store
) -> None:
    sid = _seed_structure(runtime_with_store)
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    assert "compare all on one page" not in r.text
    assert "?surface=" not in r.text


def test_map_svg_draws_regions_bands_and_bulk_overlay() -> None:
    from precis.quest import pourbaix_map as pm

    m = pm.build_map(
        [
            {
                "ref_id": 9,
                "model": "m",
                "anchor_key": "a" * 64,
                "engine_version": "0.24.0",
                "corrections": None,
                "che_sweep": _sweep(),
            }
        ],
        bulk={
            "present": True,
            "point": {"U_RHE": -0.2, "pH": 7.0, "verdict": "stable", "domain": ["Pd"]},
            "window": None,
            "worst_in_window": "stable",
            "cells": [
                {"U_RHE": -0.4, "pH": 7.0, "verdict": "stable", "domain": ["Pd"]},
                {"U_RHE": 0.0, "pH": 10.0, "verdict": "dissolved", "domain": ["PdO2"]},
            ],
            "job_id": 55,
            "uncertain": True,
            "note": pm.BULK_LAYER_NOTE,
        },
    )
    svg = map_svg(m)
    assert "<svg" in svg and "stroke-dasharray" in svg
    assert "bulk layer (uncertain)" in svg
    assert "H* at 0.25 ML" in svg and "clean" in svg
    assert "mfHatch" in svg, "the model-form band hatch pattern is defined"


def test_map_svg_empty_map_is_an_honest_placeholder() -> None:
    svg = map_svg({"U_RHE_window": [None, None]})
    assert "no sweep window" in svg


# ── slice 7: the reset layer on the map ──────────────────────────────────


def _reset_results() -> dict[str, Any]:
    """A route whose reset layer is non-degenerate over the swept
    window: e1 flat-downhill (guaranteed everywhere), e2 electrochemical
    (viable U <= 0, guaranteed U <= -0.2 at η = 0.2)."""
    r = _pathway_results()
    che = r["che_affine"]
    assert isinstance(che, dict)
    route = che["per_route"][0]
    route["route"] = "* -> NO -> *~nh3"
    route["steps"] = [
        {"edge": "e1", "delta_g0": -0.6, "n_e": 0},
        {"edge": "e2", "delta_g0": 0.0, "n_e": 1},
    ]
    return r


def test_map_svg_draws_the_reset_layer_apart_from_the_dft_bands() -> None:
    m = {
        "U_RHE_window": [-0.5, 0.5],
        "pH_window": [0.0, 14.0],
        "regions": [],
        "boundaries": [],
    }
    svg = map_svg(
        m,
        reset=[
            {
                "name": "r1",
                "color": "#7c3aed",
                "ok": [[-0.5, -0.2]],
                "band": [[-0.2, 0.0]],
            }
        ],
    )
    assert 'class="pbx-reset"' in svg
    assert 'stroke-dasharray="3 3"' in svg  # the hysteretic band edge
    assert "r1 reset (dashed = hysteretic)" in svg
    svg_off = map_svg(m)
    assert "pbx-reset" not in svg_off


def test_pourbaix_page_draws_reset_layer_and_ships_rung_margins(
    pb_client: TestClient, runtime_with_store
) -> None:
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store)
    pw = _pathway(store, "pw_reset", _reset_results())
    store.add_link(
        src_ref_id=sid, dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    r = pb_client.get(f"/pourbaix/st{sid}")
    assert r.status_code == 200
    text = r.text
    assert 'id="pbx-reset-toggle"' in text
    assert 'class="pbx-reset"' in text
    assert "hysteretic" in text
    # the client-side verdict needs the per-rung required overdrive
    assert "data-rmargin" in text and "m_e" in text


# ── gr488208: cold render — batched reads, identical output ──────────────


def _scan_jobs_oracle(store: Any, sid: int) -> list[dict[str, Any]]:
    """The pre-gr488208 per-structure read (one query plus one ``tags_for``
    per job) — the equality oracle for ``scan_jobs_many``."""
    from precis.quest import pourbaix_map as pm

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT j.ref_id, j.meta FROM refs j "
            "WHERE j.kind = 'job' AND j.retired_at IS NULL "
            "AND j.meta->>'job_type' = %(jt)s "
            "AND (j.parent_id = %(sid)s OR j.parent_id IN ("
            "      SELECT ref_id FROM refs WHERE parent_id = %(sid)s "
            "        AND kind = 'todo' AND retired_at IS NULL)) "
            "ORDER BY j.ref_id DESC LIMIT 100",
            {"jt": pm.SCAN_JOB_TYPE, "sid": int(sid)},
        ).fetchall()
    out = []
    for ref_id, meta in rows:
        if not any(str(t) == "STATUS:succeeded" for t in store.tags_for(int(ref_id))):
            continue
        m = dict(meta or {})
        out.append(
            {
                "ref_id": int(ref_id),
                "model": m.get("model"),
                "anchor_key": m.get("anchor_key"),
                "engine_version": m.get("engine_version"),
                "corrections": m.get("corrections"),
                "che_sweep": m.get("che_sweep")
                if isinstance(m.get("che_sweep"), dict)
                else None,
            }
        )
    return out


def _scan(store: Any, parent: int, status: str, model: str = "m") -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title=f"scan {model}",
        parent_id=parent,
        meta={
            "job_type": "surface_coverage_scan",
            "anchor_key": "k" * 64,
            "model": model,
            "engine_version": "0.24.0",
            "corrections": None,
            "che_sweep": _sweep(),
        },
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )
    return int(job.id)


def test_scan_jobs_many_matches_the_per_structure_read(runtime_with_store) -> None:
    from precis.quest import pourbaix_map as pm

    store = runtime_with_store.store
    sa = _seed_structure(runtime_with_store, "pd_sa", with_scan=False)
    sb = _seed_structure(runtime_with_store, "pd_sb", with_scan=False)
    sc = _seed_structure(runtime_with_store, "pd_sc", with_scan=False)
    todo = store.insert_ref(kind="todo", slug=None, title="t", parent_id=sa)
    gone = store.insert_ref(kind="todo", slug=None, title="g", parent_id=sb)
    _scan(store, sa, "succeeded", "m1")
    _scan(store, int(todo.id), "succeeded", "m2")  # via a todo under sa
    _scan(store, sa, "failed")
    _scan(store, sb, "succeeded")
    _scan(store, int(gone.id), "succeeded")
    store.retire_ref(int(gone.id))  # a retired todo's jobs drop out
    store.insert_ref(
        kind="job", slug=None, title="x", parent_id=sb, meta={"job_type": "other"}
    )
    many = pm.scan_jobs_many(store, [sa, sb, sc])
    for sid in (sa, sb, sc):
        assert many[sid] == _scan_jobs_oracle(store, sid)
    assert [j["model"] for j in many[sa]] == ["m2", "m1"]
    assert len(many[sb]) == 1
    assert many[sc] == []


def test_structure_runs_many_matches_structure_runs(runtime_with_store) -> None:
    store = runtime_with_store.store
    sa = _seed_structure(runtime_with_store, "pd_ra", with_scan=False)
    sb = _seed_structure(runtime_with_store, "pd_rb", with_scan=False)
    for e in (-1.0, -2.0, -3.0):
        _converged(store, sa, energy=e)
    many = store.structure_runs_many([sa, sb], limit=2)
    assert many[sa] == store.structure_runs(sa, limit=2)
    assert many[sb] == store.structure_runs(sb, limit=2)
    assert len(many[sa]) == 2
    assert store.structure_runs_many([]) == {}


def _many_surface_quest(runtime_with_store, n: int) -> tuple[int, list[int]]:
    """A quest served by ``n`` ranked candidates (no geometry, so no scene
    loads) plus two anchored surfaces carrying scan jobs."""
    store = runtime_with_store.store
    qid = _mk_quest(runtime_with_store)
    sids = []
    for i in range(n):
        st = store.insert_ref(
            kind="structure",
            slug=f"pbc_{qid}_{i}",
            title=f"cand {i}",
            meta={"geom_hash_c": f"h{i}", "barrier": 0.5 + i * 0.01},
        )
        _converged(store, int(st.id), energy=-float(i))
        store.add_link(src_ref_id=st.id, dst_ref_id=qid, relation="serves")
        sids.append(int(st.id))
    for k in range(2):
        sid = _seed_structure(runtime_with_store, f"pbc_{qid}_anch_{k}")
        store.add_link(src_ref_id=sid, dst_ref_id=qid, relation="serves")
        sids.append(sid)
    return qid, sids


class _QueryCounter:
    """Counts SQL statements issued through psycopg while installed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import psycopg

        self.n = 0
        cur_exec = psycopg.Cursor.execute
        conn_exec = psycopg.Connection.execute

        def _cur(this: Any, *a: Any, **k: Any) -> Any:
            self.n += 1
            return cur_exec(this, *a, **k)

        def _conn(this: Any, *a: Any, **k: Any) -> Any:
            self.n += 1
            return conn_exec(this, *a, **k)

        monkeypatch.setattr(psycopg.Cursor, "execute", _cur)
        monkeypatch.setattr(psycopg.Connection, "execute", _conn)


def test_pourbaix_quest_page_query_count_is_flat_in_surfaces(
    pb_client: TestClient, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr488208: the render issued ~4 queries per candidate (1,017 on a
    239-surface fixture shaped like qu164903); the reads are batched now,
    so the count does not grow with the surface count."""
    counts = []
    for n in (5, 40):
        qid, _ = _many_surface_quest(runtime_with_store, n)
        pb_client.get(f"/pourbaix/qu{qid}")  # warm template + imports
        counter = _QueryCounter(monkeypatch)
        r = pb_client.get(f"/pourbaix/qu{qid}")
        monkeypatch.undo()
        assert r.status_code == 200
        counts.append(counter.n)
    assert counts[1] == counts[0], counts
    assert counts[0] < 80, counts


def test_pourbaix_quest_page_cold_render_bound(runtime_with_store, tmp_path) -> None:
    """A fresh app's first render of a 239-surface quest stays well under
    the pre-gr488208 cost (0.86 s locally, 3.4 s at 1 ms per query).
    Generous: it catches an N+1 regression, it is not a benchmark."""
    import time

    qid, _ = _many_surface_quest(runtime_with_store, 239)
    client = TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )
    t0 = time.perf_counter()
    r = client.get(f"/pourbaix/qu{qid}")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert r.text.count('class="pbx-entry') == 1
    assert elapsed < 5.0, f"cold render took {elapsed:.2f}s"


def test_pourbaix_quest_page_batched_equals_per_structure(
    pb_client: TestClient, runtime_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batched reads render the same page, byte for byte, as the
    per-structure reads they replaced — default pick and compare view."""
    from precis.quest import pourbaix_map as pm
    from precis.store.store import Store

    store = runtime_with_store.store
    qid, sids = _many_surface_quest(runtime_with_store, 12)
    pw = _pathway(store, "pw_eq", _reset_results())
    store.add_link(
        src_ref_id=sids[-1], dst_ref_id=pw.id, relation="related-to", set_by="system"
    )
    urls = [
        f"/pourbaix/qu{qid}?surface=st{sids[-1]}",
        f"/pourbaix/qu{qid}",
        f"/pourbaix/qu{qid}?view=compare",
    ]
    batched = [pb_client.get(u).text for u in urls]
    monkeypatch.setattr(
        pm,
        "scan_jobs_many",
        lambda st, ids: {int(i): _scan_jobs_oracle(st, int(i)) for i in ids},
    )
    monkeypatch.setattr(
        Store,
        "structure_runs_many",
        lambda self, ids, limit=20: {
            int(i): self.structure_runs(int(i), limit=limit) for i in ids
        },
    )
    per_structure = [pb_client.get(u).text for u in urls]
    assert batched == per_structure
    assert "<svg" in batched[0] and "pbx-staircase" in batched[0]
    assert batched[2].count('class="pbx-entry') == len(sids)


def test_pourbaix_quest_page_reflects_a_new_scan_on_the_next_render(
    pb_client: TestClient, runtime_with_store
) -> None:
    """Nothing is cached between renders: a scan job that lands after the
    first render shows on the next one."""
    store = runtime_with_store.store
    sid = _seed_structure(runtime_with_store, "pd_late", with_scan=False)
    qid = _mk_quest(runtime_with_store)
    store.add_link(src_ref_id=sid, dst_ref_id=qid, relation="serves")
    first = pb_client.get(f"/pourbaix/qu{qid}").text
    assert "no anchors" in first
    _scan(store, sid, "succeeded")
    second = pb_client.get(f"/pourbaix/qu{qid}").text
    assert "no anchors" not in second and "U (V vs RHE)" in second
