"""``view='fit'`` — se-fit-check-and-probe slice 2 (gr514325): the
server-side mesh fit check. The imported unicycle fixture reports the
same findings the offline ``fit.check`` does; the importer stores the
recomputed report at ``meta.workspaces[<ws>].fit``; a re-import diffs
against it (cleared/new); an envelope-only design gets a labelled
report; a bad ``fit_rules`` refuses naming the key.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_geom import fit as geom_fit
from precis_se.handler import SeHandler
from precis_se.workspace_import import import_workspace_run

DATA = Path(__file__).resolve().parent / "workspaces" / "data"
FIXTURE = DATA / "unicycle" / "out"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def _design(store: Store, slug: str = "ws-fit") -> str:
    store.insert_ref(kind="se", slug=slug, title="fit test", meta={})
    return slug


def _job(store: Store) -> int:
    return int(store.insert_ref(kind="job", slug=None, title="ws run", meta={}).id)


def _import(store: Store, out: Path, design: str = "ws-fit") -> Any:
    return import_workspace_run(
        store,
        workspace="fit-test",
        design=design,
        out_dir=out,
        run_job=_job(store),
        script_sha="deadbee5",
        requested_by="agent:test",
    )


def _conform(out: Path, dst: Path) -> Path:
    """Copy ``out`` to ``dst`` renaming manifest blocks to ``t_<name>``
    — the fixture names instances identically to their template blocks,
    which the import vet refuses (one namespace, gr514240). Placed part
    names (the instances) are unchanged, so findings and made-by lines
    carry the same labels the offline check prints."""
    shutil.copytree(out, dst)
    mpath = dst / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    names = {b["name"] for b in manifest["blocks"]}
    for b in manifest["blocks"]:
        b["name"] = "t_" + b["name"]
    for inst in manifest.get("instances") or []:
        if inst.get("template") in names:
            inst["template"] = "t_" + str(inst["template"])
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    return dst


def _fit_meta(store: Store, design: str = "ws-fit") -> dict[str, Any]:
    ref = store.get_ref(kind="se", id=design)
    assert ref is not None
    return (((ref.meta or {}).get("workspaces") or {}).get("fit-test") or {}).get(
        "fit"
    ) or {}


def _pairs(findings: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {tuple(f["pair"]) for f in findings}


def test_import_stores_report_and_view_matches_offline(
    handler: SeHandler, store: Store, tmp_path: Path
) -> None:
    _design(store)
    res = _import(store, _conform(FIXTURE, tmp_path / "out-se"))
    assert not res.findings or all("fit:" not in f for f in res.findings)

    rec = _fit_meta(store)
    assert rec.get("schema") == 1 and rec.get("report")
    stored_pairs = _pairs(rec["report"]["findings"])
    assert ("flange_bolt_left", "fork_left") in stored_pairs
    assert ("crown", "seatpost") in stored_pairs
    assert ("fork_left", "wheel") in stored_pairs

    # the view recomputes live; its table is the engine's own text
    body = handler.get(id="ws-fit", view="fit").body
    assert body.startswith("# view='fit' — live")
    unicycle_copy = tmp_path / "uni"
    shutil.copytree(FIXTURE, unicycle_copy)
    offline = geom_fit.check(unicycle_copy)
    assert offline.text() in body
    assert "# diff" in body


def test_reimport_diff_shows_cleared_and_new(
    handler: SeHandler, store: Store, tmp_path: Path
) -> None:
    _design(store)
    _import(store, _conform(FIXTURE, tmp_path / "first"))

    # second build: drop the crown/seatpost press connect (the +0.24 mm
    # press-gap finding clears) and move the saddle onto the axle's pose
    # (fresh overlaps are new findings).
    second = _conform(FIXTURE, tmp_path / "second")
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    manifest["connects"] = [
        c
        for c in manifest["connects"]
        if not (c["a"] == "crown.seat_tube" and c["b"] == "seatpost.tube")
    ]
    for inst in manifest["instances"]:
        if inst["name"] == "saddle":
            inst["pose"] = "[0 0 254.0] mm"
    (second / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _import(store, second)

    rec = _fit_meta(store)
    diff = rec["diff"]
    cleared_pairs = _pairs(diff["cleared"])
    new_pairs = _pairs(diff["new"])
    assert ("crown", "seatpost") in cleared_pairs
    assert any("saddle" in p for p in new_pairs)

    body = handler.get(id="ws-fit", view="fit").body
    assert "cleared:" in body and "new:" in body
    assert "crown / seatpost" in body


def test_envelope_only_design_is_labelled(handler: SeHandler, store: Store) -> None:
    handler.put(
        id="env-only",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "a",
                        "envelope": "box:w0.1d0.1h0.1",
                    },
                    {
                        "op": "add_block",
                        "name": "b",
                        "envelope": "box:w0.1d0.1h0.1",
                    },
                    {
                        "op": "set_pose",
                        "block": "b",
                        "pose": "[0.05 0 0] m",
                    },
                ]
            }
        ),
    )
    body = handler.get(id="env-only", view="fit").body
    assert "envelope×2" in body
    assert "error" in body  # the two boxes overlap
    assert "# diff: no earlier report on record" in body


def test_recompute_arg_is_accepted(
    handler: SeHandler, store: Store, tmp_path: Path
) -> None:
    _design(store)
    _import(store, _conform(FIXTURE, tmp_path / "out-se"))
    body = handler.get(id="ws-fit", view="fit", args={"recompute": True}).body
    assert body.startswith("# view='fit' — live")


def test_bad_fit_rules_refuses(
    handler: SeHandler, store: Store, tmp_path: Path
) -> None:
    _design(store, slug="ws-fit-bad")
    out = tmp_path / "out"
    (out / "blocks").mkdir(parents=True)
    (out / "blocks" / "a.cad").write_text(
        "base add box:w10mmd10mmh10mm\n", encoding="utf-8"
    )
    manifest = {
        "schema": 1,
        "blocks": [{"name": "a", "file": "blocks/a.cad", "scale": "macro"}],
        "fit_rules": {"min_clearance": [{"pair": "ia", "value": "1 mm"}]},
    }
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BadInput, match="fit_rules"):
        _import(store, out, design="ws-fit-bad")


def test_stored_report_labelled_stale_after_edit(
    handler: SeHandler,
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_se import fit as se_fit

    _design(store)
    _import(store, _conform(FIXTURE, tmp_path / "out-se"))
    monkeypatch.setattr(se_fit, "_LIVE_BUDGET_MS", -1.0)  # force the stored path
    fresh = handler.get(id="ws-fit", view="fit").body
    assert fresh.startswith("# view='fit' — stored report")
    assert "stale" not in fresh

    # Age the stored report an hour; stamping bumps the design's updated_at.
    ref = store.get_ref(kind="se", id="ws-fit")
    assert ref is not None
    ws = json.loads(json.dumps((ref.meta or {})["workspaces"]))
    ws["fit-test"]["fit"]["at"] = "2020-01-01T00:00:00+00:00"
    store.stamp_ref_meta(int(ref.id), {"workspaces": ws})
    body = handler.get(id="ws-fit", view="fit").body
    assert body.startswith("# view='fit' — stored report")
    assert "# stale: design edited after this report" in body
    assert "recompute: args={'recompute': true}" in body


def test_bad_min_clearance_value_refuses_at_vet(store: Store, tmp_path: Path) -> None:
    _design(store, slug="ws-fit-badval")
    out = tmp_path / "out"
    (out / "blocks").mkdir(parents=True)
    (out / "blocks" / "a.cad").write_text(
        "base add box:w10mmd10mmh10mm\n", encoding="utf-8"
    )
    manifest = {
        "schema": 1,
        "blocks": [{"name": "a", "file": "blocks/a.cad", "scale": "macro"}],
        "fit_rules": {"min_clearance": [{"pair": ["ia", "ib"], "value": "two mm"}]},
    }
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BadInput, match=r"min_clearance\[0\]\.value") as ei:
        _import(store, out, design="ws-fit-badval")
    assert "2 mm" in str(ei.value.next)
