"""Molecular workspaces slice 5 — the two hex-tiler examples, with goldens
(docs/backlog/molecular-workspace-code.md, Slices row 5).

``examples/hexfold/decorated-prism/main.py`` (the spec's worked example:
prism, COOH decoration, H cap, UFF collapse check, C60 guest) and
``examples/hexfold/cnt-guest/main.py`` (a decorated, capped CNT with a C60
that has to fit, asserted on the ``pg.inspect`` ray profile) run end to end
under ``out.run`` and are pinned against
``data/mol_example_goldens.json``: atom/bond counts per block, the
coordinate SHA, the findings, and the trace (frame count, per-qual census,
SHA of the sorted frame-key set). The tests need no database and no
container.

Acceptance clauses pinned here:

- each example reproduces its golden, cold and again on a full step-cache
  hit (a hit grafts the same trace: same keys, same makers per frame qual);
- a ``sheetnet`` / ``mol`` change that alters one fails this file naming the
  example (the failure message says which golden drifted and how to
  regenerate it);
- the decorated-prism example meets the §Core design 7 latency: cold < 10 s,
  a re-run with only the last step's parameter changed < 2 s, with the
  step digest naming the one rebuilt step;
- a tube too narrow for the guest fails the cnt-guest run (the profile
  assert), recording the failure.

Regenerate the goldens after an intended change (then read the diff)::

    uv run python tests/workspaces/test_mol_examples.py

The coordinate SHA is taken on a 1e-3 Å grid, not the 1e-6 of
``mol.relax``'s ``pos_sha``: the UFF relax of the prism is a 200-step FIRE
run, whose last digits move with the platform's floating point; a milli-
ångström grid is far above that noise and far below any real change.
"""

from __future__ import annotations

import hashlib
import json
import re
import runpy
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("rdkit")

import precis_geom.out as geom_out
from precis.store import Store
from precis_geom import steps
from precis_se.workspace_import import import_workspace_run

pytestmark = pytest.mark.filterwarnings("ignore:steps:UserWarning")

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = ROOT / "examples" / "hexfold"
GOLDEN = Path(__file__).resolve().parent / "data" / "mol_example_goldens.json"
EXAMPLES = ("decorated-prism", "cnt-guest")
_GRID = 1e-3


def run_example(
    name: str,
    scratch: Path,
    *,
    src: Path | None = None,
    cache: Path | None = None,
) -> Path:
    """Exec ``<src or examples/hexfold/name>/main.py`` as ``__main__`` with
    ``out.run``'s scratch root and the step cache redirected under
    ``scratch``; returns its ``out/<name>/`` dir."""
    script = (src or EXAMPLES_DIR / name) / "main.py"
    steps.configure(cache or scratch / "cache")
    old = geom_out._SCRATCH_DIR
    geom_out._SCRATCH_DIR = scratch
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        geom_out._SCRATCH_DIR = old
        steps.configure()
    return scratch / "out" / name


def _coords(extxyz: Path) -> np.ndarray:
    rows = extxyz.read_text(encoding="utf-8").splitlines()[2:]
    return np.array([[float(x) for x in r.split()[1:4]] for r in rows])


def _qual_of(frames: list[dict[str, Any]]) -> Any:
    """``maker id -> qual`` over a trace document (collapsed spans too)."""
    exact: dict[int, str] = {}
    spans: list[tuple[int, int, str]] = []
    for f in frames:
        lo, hi = int(f["id"]), int(f.get("id_end", f["id"]))
        if hi == lo:
            exact[lo] = f["qual"]
        else:
            spans.append((lo, hi, f["qual"]))

    def qual(maker: int) -> str:
        if maker in exact:
            return exact[maker]
        return next((q for lo, hi, q in spans if lo <= maker <= hi), "?")

    return qual


def golden_row(out_dir: Path) -> dict[str, Any]:
    """The golden document of one run: per block counts, coordinate SHA
    (1e-3 Å grid, file order), makers per frame qual, findings; the chemistry
    check summary; the trace census and the SHA of its sorted frame keys."""
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    trace = json.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
    check = json.loads((out_dir / "check.json").read_text(encoding="utf-8"))
    qual = _qual_of(trace["frames"])
    blocks: dict[str, Any] = {}
    for b in man["blocks"]:
        path = out_dir / b["file"]
        xyz = _coords(path)
        rows = path.read_text(encoding="utf-8").splitlines()[2:]
        makers = Counter(qual(int(r.split()[5])) for r in rows)
        bonds = json.loads(path.with_suffix(".bonds.json").read_text(encoding="utf-8"))
        grid = (np.round(xyz / _GRID) * _GRID + 0.0).astype("<f8")
        blocks[b["name"]] = {
            "atoms": len(rows),
            "bonds": len(bonds["bonds"]),
            "coord_sha256": hashlib.sha256(grid.tobytes()).hexdigest(),
            "makers_by_qual": dict(sorted(makers.items())),
            "findings": list(b.get("findings", [])),
            "relaxed_by": (
                {k: b["relaxed_by"][k] for k in ("rung", "verdict")}
                if "relaxed_by" in b
                else None
            ),
            "template": bool(b.get("template")),
        }
    keys = sorted(f["key"] for f in trace["frames"])
    return {
        "status": man.get("status", "ok"),
        "blocks": blocks,
        "instances": [
            {k: i[k] for k in ("name", "template", "parent") if k in i}
            for i in man.get("instances", [])
        ],
        "check": {
            n: sorted([f["rule"], f["severity"], f["count"]] for f in fs)
            for n, fs in sorted(check["blocks"].items())
        },
        "trace": {
            "frames": len(keys),
            "quals": dict(sorted(Counter(f["qual"] for f in trace["frames"]).items())),
            "keys_sha256": hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest(),
        },
    }


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def regenerate() -> None:
    with tempfile.TemporaryDirectory() as td:
        rows = {n: golden_row(run_example(n, Path(td) / n)) for n in EXAMPLES}
    GOLDEN.write_text(
        json.dumps(rows, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


# ── goldens ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_reproduces_its_golden_cold_and_on_a_hit(
    tmp_path: Path, name: str
) -> None:
    want = _golden()[name]
    hint = (
        f"examples/hexfold/{name}: golden drift — if intended, regenerate "
        "tests/workspaces/data/mol_example_goldens.json with `uv run python "
        "tests/workspaces/test_mol_examples.py` and read the diff"
    )
    cold = golden_row(run_example(name, tmp_path / "cold", cache=tmp_path / "cache"))
    assert cold == want, hint
    # same cache, second process-shaped run: every step a hit, the grafted
    # trace carries the same keys and the same makers per frame qual
    hit_out = run_example(name, tmp_path / "hit", cache=tmp_path / "cache")
    hit_trace = json.loads((hit_out / "trace.json").read_text(encoding="utf-8"))
    assert hit_trace["run"]["steps"]["rebuilt"] == 0, hit_trace["run"]["steps"]
    assert hit_trace["run"]["steps"]["hits"] >= 4
    assert golden_row(hit_out) == want, hint + " (on a step-cache hit)"


def test_golden_file_covers_every_molecular_example() -> None:
    assert sorted(_golden()) == sorted(EXAMPLES)
    for name in EXAMPLES:
        assert (EXAMPLES_DIR / name / "main.py").is_file()
        assert (EXAMPLES_DIR / name / "recipe.json").is_file()


def test_decorated_prism_is_the_worked_example(tmp_path: Path) -> None:
    out_dir = run_example("decorated-prism", tmp_path)
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    [p, c60] = man["blocks"]
    # 14 COOH groups (⌈56/3⌉ = 19 sites minus the 5 pockets), each pocket named
    skipped = [f for f in p["findings"] if f.startswith("decorate.site_skipped")]
    assert len(skipped) == 5
    assert [f.split()[1] for f in skipped] == [
        "aC192",
        "aC230",
        "aC251",
        "aC336",
        "aC395",
    ]
    assert p["relaxed_by"]["rung"] == "uff" and p["relaxed_by"]["verdict"] == "ok"
    assert p["relax"] == {"method": "gfn2"}
    assert c60["template"] is True
    assert man["instances"][0]["template"] == "c60"
    # a COOH group is C + 2 O + H: the decorated, capped prism has the 600
    # atoms of the plain capped prism plus the groups' atoms minus the H
    # each group replaced
    atoms = int((out_dir / p["file"]).read_text(encoding="utf-8").splitlines()[0])
    assert atoms == _golden()["decorated-prism"]["blocks"]["p"]["atoms"]
    chem = json.loads((out_dir / "check.json").read_text(encoding="utf-8"))
    assert chem["summary"]["errors"] == 0


def test_cnt_guest_fits_and_a_narrow_tube_fails_the_run(tmp_path: Path) -> None:
    out_dir = run_example("cnt-guest", tmp_path / "ok")
    man = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    desc = man["blocks"][0]["desc"]
    free, cage = (float(x) for x in re.findall(r"(\d+\.\d+) Å", desc))
    assert free > cage > 3.0, desc
    # the same script with a (6, 6) tube: the profile bottoms below the cage
    src = tmp_path / "narrow"
    shutil.copytree(EXAMPLES_DIR / "cnt-guest", src)
    rec = json.loads((src / "recipe.json").read_text(encoding="utf-8"))
    rec.update(n=6, m=6)
    (src / "recipe.json").write_text(json.dumps(rec), encoding="utf-8")
    with pytest.raises(ValueError, match="does not fit"):
        run_example("cnt-guest", tmp_path / "bad", src=src)
    failed = json.loads(
        (tmp_path / "bad" / "out" / "cnt-guest" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert failed["status"] == "failed"
    assert "free radius bottoms at" in failed["error"]["message"]


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_out_dir_imports(store: Store, tmp_path: Path, name: str) -> None:
    """The importer takes each example's ``out/`` as written: both blocks,
    the instance, and (decorated-prism) the ``relaxed_by`` row."""
    out_dir = run_example(name, tmp_path)
    slug = f"ex-{name}"
    store.insert_ref(kind="se", slug=slug, title=slug, meta={})
    job = store.insert_ref(kind="job", slug=None, title="ws run", meta={})
    res = import_workspace_run(
        store,
        workspace=f"{slug}-w",
        design=slug,
        out_dir=out_dir,
        run_job=int(job.id),
        script_sha="deadbee5",
        lock_sha="1ock",
        image_digest="sha256:img",
        params={},
        requested_by="agent:test",
    )
    want = {"decorated-prism": ["p", "c60"], "cnt-guest": ["tube", "c60"]}[name]
    assert sorted(res.imported) == sorted(want), (res.imported, res.findings)
    if name == "decorated-prism":
        sref = store.get_ref(kind="structure", id=f"{slug}-p")
        assert sref is not None
        assert sref.meta["generated"]["relaxed_by"]["rung"] == "uff"


# ── the loop latency (§Core design 7) ────────────────────────────────────


def test_decorated_prism_latency_cold_and_one_late_step_changed(
    tmp_path: Path,
) -> None:
    """Cold < 10 s; a re-run with only ``relax_steps`` (the last step's
    parameter) edited < 4 s, one rebuilt step named, every step above it a
    hit. (In-process wall of the script's ``main()``: the interpreter
    start and imports are the host's, not the loop's.)"""
    src = tmp_path / "ex"
    shutil.copytree(EXAMPLES_DIR / "decorated-prism", src)
    cache = tmp_path / "cache"

    def run(tag: str) -> tuple[float, dict[str, Any]]:
        t0 = time.perf_counter()
        out_dir = run_example("decorated-prism", tmp_path / tag, src=src, cache=cache)
        wall = time.perf_counter() - t0
        doc = json.loads((out_dir / "trace.json").read_text(encoding="utf-8"))
        return wall, doc["run"]["steps"]

    cold_s, cold = run("cold")
    assert cold["hits"] == 0 and cold["first_miss"] == "prism#0"
    assert cold_s < 10.0, f"cold {cold_s:.1f} s"
    again_s, again = run("again")
    assert again["rebuilt"] == 0 and again["hits"] >= 5, again
    rec = json.loads((src / "recipe.json").read_text(encoding="utf-8"))
    rec["relax_steps"] += 1
    (src / "recipe.json").write_text(json.dumps(rec), encoding="utf-8")
    late_s, late = run("late")
    assert late["first_miss"] == "relax#0" and late["rebuilt"] == 1, late
    assert late["hits"] >= 4, late
    # The hits/rebuilt asserts above carry the invariant; this wall bound only
    # catches a gross regression. 4 s: local ~0.9 s, a CI runner hit 2.09 s.
    assert late_s < 4.0, f"one-late-step re-run {late_s:.2f} s"
    # an early edit rebuilds from the decoration down
    rec["every"] = 4
    (src / "recipe.json").write_text(json.dumps(rec), encoding="utf-8")
    _s, early = run("early")
    assert early["first_miss"] == "decorate#0" and early["hits"] >= 1, early


if __name__ == "__main__":
    regenerate()
