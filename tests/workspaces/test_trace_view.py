"""Structure-workspaces slice 2t acceptance — trace views + selector.

- a seam atom's trace names ``junction#k`` and a ``main.py:<line>``;
- the LCA of one strip's atoms is its ``strip`` frame at 100 %, and of
  one atom from each of two strips the layout frame;
- a pocket over ``frame:<key>`` holds exactly the frame's ``made`` count;
- a finding row carries the script line;
- the token count of a 6-frame trace is ≤ 150.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.store import Store
from precis.store.types import ChunkInsert
from precis_se import datums as se_datums
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run

DATA = Path(__file__).resolve().parent / "data"
FIXTURE = DATA / "trace_demo" / "out"

#: frame ids/keys in the fixture's trace.json.
STRIP1, STRIP2, LAYOUT = "c4b52e3b78c8343c", "0109a483c6b46905", "9c2ba7e133c3bae5"
JUNCTION1 = "1b7377e82e6689d6"


def _design(store: Store, slug: str = "ws-trace") -> int:
    ref = store.insert_ref(kind="se", slug=slug, title="ws trace", meta={})
    return int(ref.id)


def _job(store: Store) -> int:
    ref = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    return int(ref.id)


@pytest.fixture
def se(store: Store) -> SeHandler:
    _design(store)
    res = import_workspace_run(
        store,
        workspace="hcol-trace",
        design="ws-trace",
        out_dir=FIXTURE,
        run_job=_job(store),
    )
    assert res.trace is not None
    return SeHandler(hub=Hub(store=store))


def test_seam_atom_names_junction_and_script_line(se: SeHandler) -> None:
    body = se.get(id="ws-trace", view="trace", args={"atom": "p/s1/s1"}).body
    assert "junction(k=3)#1" in body
    assert "main.py:" in body


def test_seam_atom_modifiers(se: SeHandler) -> None:
    body = se.get(
        id="ws-trace", view="trace", args={"atom": "p/s1/s1", "touched": True}
    ).body
    assert "changed by: sheetnet.decorate" in body


def test_lca_one_strip_at_full_coverage(se: SeHandler) -> None:
    atoms = [f"p/w1/(0,{i},A)" for i in range(4)]
    body = se.get(id="ws-trace", view="trace", args={"atoms": atoms}).body
    assert "sheetnet.strip" in body
    assert "covers 4/4" in body


def test_lca_two_strips_is_layout(se: SeHandler) -> None:
    body = se.get(
        id="ws-trace",
        view="trace",
        args={"atoms": ["p/w1/(0,0,A)", "p/w2/(0,0,A)"]},
    ).body
    assert "main.layout" in body
    assert "covers 2/2" in body
    assert f"frame:{LAYOUT}/rest" in body


def test_frame_selector_holds_made_count(store: Store, se: SeHandler) -> None:
    ref = store.get_ref(kind="se", id="ws-trace")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    rdat = se_datums.resolve(tree, "p", f"frame:{STRIP1}")
    assert rdat.error is None
    assert sorted(rdat.atoms) == [0, 1, 2, 3]
    # The frame's own made count is exactly the resolved set.
    bs = tree.structures("ws-trace-p") if tree.structures else None
    assert bs is not None
    doc = (
        tree.traces((bs.meta.get("generated") or {}).get("trace"))
        if tree.traces
        else None
    )
    assert doc is not None
    fr = next(f for f in doc["frames"] if f["key"] == STRIP1)
    assert len(rdat.atoms) == fr["made"]["atoms"]


def test_frame_selector_modifiers(store: Store, se: SeHandler) -> None:
    ref = store.get_ref(kind="se", id="ws-trace")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert sorted(se_datums.resolve(tree, "p", f"frame:{LAYOUT}!").atoms) == []
    assert sorted(se_datums.resolve(tree, "p", f"frame:{LAYOUT}/rest").atoms) == list(
        range(10)
    )
    assert sorted(
        se_datums.resolve(tree, "p", "frame:847e4f54a1451dac/touched").atoms
    ) == [8, 9]


def test_frame_card(se: SeHandler) -> None:
    body = se.get(id="ws-trace", view="trace", args={"frame": STRIP1}).body
    assert "sheetnet.strip" in body
    assert f"selector: frame:{STRIP1}" in body
    assert "w1/(0,0,A)" in body


def _add_stale_measure(store: Store, se: SeHandler, slug: str) -> None:
    ref = store.get_ref(kind="se", id=slug)
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    from precis_se.ops import apply_ops

    apply_ops(
        tree,
        [
            {
                "op": "add_measure",
                "block": "p",
                "name": "gone",
                "datum": "atoms:p[99]",
            }
        ],
    )
    persist.save_tree(store, ref_id=ref.id, tree=tree, card_text=slug)


def test_finding_row_carries_script_line(store: Store, se: SeHandler) -> None:
    _add_stale_measure(store, se, "ws-trace")
    body = se.get(id="ws-trace", view="validate").body
    header = next(l for l in body.splitlines() if "subject" in l)
    assert "frame" in header
    row = next(l for l in body.splitlines() if "main.py:" in l)
    assert re.search(r"main\.py:\d+", row)


def test_finding_row_without_trace_has_no_frame_column(
    store: Store, tmp_path: Path
) -> None:
    """A bound structure with no trace.json renders the same finding —
    no frame column (the column is omitted, not emptied)."""
    import shutil

    no_trace = tmp_path / "out"
    shutil.copytree(FIXTURE, no_trace)
    (no_trace / "trace.json").unlink()
    _design(store, "ws-plain")
    res = import_workspace_run(
        store,
        workspace="hcol-trace",
        design="ws-plain",
        out_dir=no_trace,
        run_job=_job(store),
    )
    assert res.trace is None
    se = SeHandler(hub=Hub(store=store))
    _add_stale_measure(store, se, "ws-plain")
    body = se.get(id="ws-plain", view="validate").body
    header = next(l for l in body.splitlines() if "subject" in l)
    assert "frame" not in header
    assert "main.py:" not in body


def test_run_diff(store: Store, se: SeHandler) -> None:
    doc = json.loads((FIXTURE / "trace.json").read_text(encoding="utf-8"))
    for f in doc["frames"]:
        if f["key"] == STRIP1:
            f["made"] = {"atoms": 6, "bonds": 3}
    job2 = _job(store)
    store.chunks.insert_chunks(
        job2,
        [
            ChunkInsert(
                ord=0,
                text=json.dumps(doc),
                meta={"chunk_kind": "ws_trace"},
            )
        ],
    )
    body = se.get(id="ws-trace", view="trace", args={"against": job2}).body
    assert "~" in body and "sheetnet.strip" in body
    assert "made" in body


def test_six_frame_trace_under_token_budget(store: Store, se: SeHandler) -> None:
    from precis.utils.llm.admit import estimate_tokens
    from precis_se.traceview import Trace

    ref = store.get_ref(kind="se", id="ws-trace")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    assert tree.structures is not None and tree.traces is not None
    bs = tree.structures("ws-trace-p")
    assert bs is not None
    doc = tree.traces((bs.meta.get("generated") or {}).get("trace"))
    assert doc is not None
    tr = Trace(doc)
    seam = bs.scene.atoms["s1/s1"]
    fr = tr.maker_frame(seam.maker)
    assert fr is not None
    assert len(tr.path(fr)) == 6  # root → build → bundle → layout → assemble → junction
    body = se.get(id="ws-trace", view="trace", args={"atom": "p/s1/s1"}).body
    assert estimate_tokens(len(body)) <= 150


def test_no_trace_is_a_clean_answer(store: Store) -> None:
    store.insert_ref(kind="se", slug="plain", title="plain", meta={})
    se = SeHandler(hub=Hub(store=store))
    body = se.get(id="plain", view="trace").body
    assert "no construction trace" in body


# ── gr514244 — trace views on a macro-only run ─────────────────────

_MACRO_TRACE: dict[str, Any] = {
    "schema": 1,
    "run": {"workspace": "hcol-macro"},
    "frames": [
        {
            "id": 0,
            "key": "root0000",
            "parent": None,
            "qual": "main",
            "file": "main.py",
            "line": 1,
            "occ": 0,
            "kind": "root",
            "args": {},
            "made": {},
            "touched": {},
        },
        {
            "id": 1,
            "key": "build001",
            "parent": "root0000",
            "qual": "main.build",
            "file": "main.py",
            "line": 12,
            "occ": 0,
            "kind": "user",
            "args": {},
            "made": {},
            "touched": {},
        },
        {
            "id": 2,
            "key": "blk00002",
            "parent": "build001",
            "qual": "out.block",
            "file": "out.py",
            "line": 30,
            "occ": 0,
            "kind": "core",
            "args": {},
            "made": {"blocks": 1},
            "touched": {},
        },
    ],
}


def _macro_design(store: Store, tmp_path: Path, slug: str = "ws-macro") -> None:
    """Import a macro-only run — one cad-DSL block, no structure-bound
    block at all (gr514244's case)."""
    _design(store, slug)
    out = tmp_path / "out"
    (out / "blocks").mkdir(parents=True)
    (out / "blocks/plate.cad").write_text(
        "plate add box:w40mmd20mmh10mm\n", encoding="utf-8"
    )
    (out / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "blocks": [
                    {
                        "name": "plate",
                        "file": "blocks/plate.cad",
                        "scale": "macro",
                        "frame": 2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (out / "trace.json").write_text(json.dumps(_MACRO_TRACE), encoding="utf-8")
    res = import_workspace_run(
        store,
        workspace="hcol-macro",
        design=slug,
        out_dir=out,
        run_job=_job(store),
    )
    assert res.trace is not None


def test_macro_only_trace_summary_and_made_by(store: Store, tmp_path: Path) -> None:
    """gr514244: a macro-only run's trace.json answers the no-args
    summary — the run line plus a which-line-made-which-block
    listing — instead of 'no construction trace'."""
    _macro_design(store, tmp_path)
    se = SeHandler(hub=Hub(store=store))
    body = se.get(id="ws-macro", view="trace").body
    assert "no construction trace" not in body
    assert "workspace hcol-macro" in body and "3 frames" in body
    assert "made-by:" in body
    assert "plate: ws-hcol-macro::main.build (main.py:12)" in body


def test_macro_only_frame_card(store: Store, tmp_path: Path) -> None:
    """gr514244: args['frame'] renders the frame card on a cad-bound
    trace (samples are atom paths — structure-only, so none here)."""
    _macro_design(store, tmp_path)
    se = SeHandler(hub=Hub(store=store))
    body = se.get(id="ws-macro", view="trace", args={"frame": "build001"}).body
    assert "main.build" in body
    assert "selector: frame:build001" in body
    assert "out.block" in body  # the child line


def test_macro_only_run_diff(store: Store, tmp_path: Path) -> None:
    """gr514244: args['against'] diffs the run against an earlier
    ws_trace chunk."""
    _macro_design(store, tmp_path)
    se = SeHandler(hub=Hub(store=store))
    old = json.loads(json.dumps(_MACRO_TRACE))
    for f in old["frames"]:
        if f["key"] == "blk00002":
            f["made"] = {"blocks": 2}
    job2 = _job(store)
    store.chunks.insert_chunks(
        job2,
        [
            ChunkInsert(
                ord=0,
                text=json.dumps(old),
                meta={"chunk_kind": "ws_trace"},
            )
        ],
    )
    body = se.get(id="ws-macro", view="trace", args={"against": job2}).body
    assert "run diff" in body and "out.block" in body
    assert "~" in body  # blk00002 changed (made)


def test_macro_atom_ask_stays_structure_bound(store: Store, tmp_path: Path) -> None:
    """gr514244: atom asks on a cad-bound block refuse naming the
    limit, not 'no block' or a crash on scene=None."""
    from precis.errors import BadInput

    _macro_design(store, tmp_path)
    se = SeHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="cad-bound"):
        se.get(id="ws-macro", view="trace", args={"atom": "plate/0"})


def test_pocket_over_frame_holds_made_count(store: Store, se: SeHandler) -> None:
    ref = store.get_ref(kind="se", id="ws-trace")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    from precis_se.ops import apply_ops

    apply_ops(
        tree,
        [
            {
                "op": "add_pocket",
                "block": "p",
                "name": "strip_pocket",
                "regions": [{"selector": f"frame:{STRIP1}"}],
            }
        ],
    )
    persist.save_tree(store, ref_id=ref.id, tree=tree, card_text="ws trace")
    body = se.get(id="ws-trace", view="pockets").body
    assert "strip_pocket" in body
    assert f"frame:{STRIP1}" in body
    # The pocket's region resolves to exactly the frame's made count.
    tree = persist.load_tree(store, ref.id)
    rdat = se_datums.resolve(tree, "p", f"frame:{STRIP1}")
    assert rdat.error is None
    assert tree.structures is not None and tree.traces is not None
    bs = tree.structures("ws-trace-p")
    assert bs is not None
    doc = tree.traces((bs.meta.get("generated") or {}).get("trace"))
    assert doc is not None
    fr = next(f for f in doc["frames"] if f["key"] == STRIP1)
    assert len(rdat.atoms) == fr["made"]["atoms"] == 4
