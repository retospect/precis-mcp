"""The "Data package" export appendix: a figure chunk carrying
``meta["figure"]["data_package"]`` (draft-pathway-figures-data-package.md)
gets a small-print end-matter table + JSON sidecar in both exporters; a
figure without the snapshot leaves the export byte-identical. DB-backed
via the ``hub`` fixture, mirroring ``tests/test_export_nanopub_appendix.py``."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import docx as docx_lib
import pytest
from psycopg.types.json import Jsonb

from precis.computed_sources import (
    build_external_source,
    contract_fingerprint,
)
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.export import docx, latex
from precis.export._data_package import (
    SECTION_TITLE,
    SIDECAR_NAME,
    DataPackageFigure,
    fmt,
    header_lines,
    require_verified_snapshot,
    table_lines,
    verify_snapshot,
)
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler

# A real 1×1 PNG for figure-embed tests (a valid raster blob).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    + ("C0lEQVR42mNk+M9QDwAD" + "hgGAWjR9awAAAABJRU5E" + "rkJggg==")
)
_PNG_B64 = base64.b64encode(_PNG).decode()

_SNAPSHOT: dict[str, Any] = {
    "schema": 1,
    "source": {
        "kind": "quest",
        "ref_id": 42,
        "handle": "qu42",
        "title": "Rh oxidation pathway",
    },
    "generated_at": "2026-08-16T00:00:00Z",
    "autocatpath_version": "0.4.0",
    "precis": {"version": "1.2.3", "sha": "abc1234"},
    "params": {
        "mlip": {"backend": "mace", "cutoff": 6.0},
        # The real shape build_pareto_snapshot emits — a list of
        # objective-spec dicts, rendered "energy:min" not a Python repr.
        "objectives": [
            {"key": "energy", "sense": "min"},
            {"key": "barrier", "sense": "min"},
        ],
        "tier": 2,
    },
    "columns": ["species", "energy_ev", "barrier_ev", "trusted"],
    "rows": [
        {
            "species": "Rh4O6",
            "energy_ev": -12.345678,
            "barrier_ev": 0.5,
            "trusted": True,
        },
        {"species": "Rh4O7", "energy_ev": None, "barrier_ev": 1.2, "trusted": False},
    ],
}


def _new_project(hub: Hub) -> int:
    return int(
        TodoHandler(hub=hub)
        .put(text="proj")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )


def _draft_with_figure(hub: Hub, *, slug: str, snapshot: dict[str, Any] | None) -> Any:
    """A draft with a title + one raster figure chunk. When ``snapshot`` is
    given, stamps it onto the figure chunk's ``meta.figure.data_package``
    via a raw update — the field a sibling pipeline (quest/pathway export)
    is responsible for producing, not something ``DraftHandler.put``
    exposes."""
    draft = DraftHandler(hub=hub)
    pid = _new_project(hub)
    draft.put(id=slug, title="T", project=pid)
    store = hub.live_store
    ref = store.get_ref(kind="draft", id=slug)
    assert ref is not None
    title_h = store.drafts.reading_order(ref.id)[0].handle
    draft.put(
        id=slug,
        chunk_kind="figure",
        text="Fig 1. Pareto frontier.",
        image=_PNG_B64,
        origin="original",
        at={"after": f"¶{title_h}"},
    )
    fig = store.drafts.reading_order(ref.id)[-1]
    assert fig.chunk_kind == "figure"
    if snapshot is not None:
        with store.tx() as conn:
            conn.execute(
                "UPDATE chunks SET meta = "
                "jsonb_set(COALESCE(meta, '{}'::jsonb), "
                "'{figure,data_package}', %s::jsonb, true) "
                "WHERE chunk_id = %s",
                (Jsonb(snapshot), fig.chunk_id),
            )
    return ref


def _export_tex(hub: Hub, ref: Any, tmp_path: Path, name: str) -> tuple[str, Path]:
    out = tmp_path / name
    result = latex.export_draft(hub.live_store, ref, target_dir=out)
    return result.main_tex.read_text(encoding="utf-8"), out


def _export_docx_text(
    hub: Hub, ref: Any, tmp_path: Path, name: str
) -> tuple[str, Path]:
    out = tmp_path / f"{name}.docx"
    docx.export_docx(hub.live_store, ref, target_path=out)
    text = "\n".join(p.text for p in docx_lib.Document(str(out)).paragraphs)
    return text, out


# ── latex ──────────────────────────────────────────────────────────


def test_snapshot_figure_gets_data_package_section_latex(
    hub: Hub, tmp_path: Path
) -> None:
    ref = _draft_with_figure(hub, slug="dpkg", snapshot=_SNAPSHOT)

    tex, out = _export_tex(hub, ref, tmp_path, "pkg")

    assert SECTION_TITLE in tex
    assert r"\embedfile" in tex
    assert r"\usepackage{embedfile}" in tex
    assert r"\fontsize{7}{8.4}\selectfont" in tex
    sidecar = out / SIDECAR_NAME
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema"] == 1
    assert len(data["figures"]) == 1
    assert data["figures"][0]["rows"] == _SNAPSHOT["rows"]


def test_sidecar_round_trips_every_printed_number_latex(
    hub: Hub, tmp_path: Path
) -> None:
    """The acceptance criterion: parsing the sidecar and re-running
    header_lines/table_lines reproduces every number the appendix prints —
    verbatim, in the compiled tex."""
    ref = _draft_with_figure(hub, slug="dround", snapshot=_SNAPSHOT)

    tex, out = _export_tex(hub, ref, tmp_path, "round")
    sidecar = out / SIDECAR_NAME
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    entry = data["figures"][0]
    rebuilt = DataPackageFigure(
        label=entry["label"],
        caption=entry["caption"],
        snapshot={k: v for k, v in entry.items() if k not in ("label", "caption")},
    )
    for line in [*header_lines(rebuilt), *table_lines(rebuilt)]:
        assert line in tex


def test_no_snapshot_figure_is_byte_identical_latex(hub: Hub, tmp_path: Path) -> None:
    ref = _draft_with_figure(hub, slug="dplain", snapshot=None)

    tex, out = _export_tex(hub, ref, tmp_path, "plain")

    assert SECTION_TITLE not in tex
    assert r"\embedfile" not in tex
    assert r"\usepackage{embedfile}" not in tex
    assert not (out / SIDECAR_NAME).exists()


def test_export_result_data_package_path_is_none_without_snapshot(
    hub: Hub, tmp_path: Path
) -> None:
    ref = _draft_with_figure(hub, slug="dnone", snapshot=None)
    out = tmp_path / "none"
    result = latex.export_draft(hub.live_store, ref, target_dir=out)
    assert result.data_package_path is None


def test_export_result_data_package_path_set_with_snapshot(
    hub: Hub, tmp_path: Path
) -> None:
    ref = _draft_with_figure(hub, slug="dset", snapshot=_SNAPSHOT)
    out = tmp_path / "set"
    result = latex.export_draft(hub.live_store, ref, target_dir=out)
    assert result.data_package_path == out / SIDECAR_NAME
    assert result.data_package_path is not None
    assert result.data_package_path.exists()


# ── docx ───────────────────────────────────────────────────────────


def test_snapshot_figure_gets_data_package_section_docx(
    hub: Hub, tmp_path: Path
) -> None:
    ref = _draft_with_figure(hub, slug="ddpkg", snapshot=_SNAPSHOT)

    text, out = _export_docx_text(hub, ref, tmp_path, "dpkg")

    assert SECTION_TITLE in text
    sidecar = out.with_name(f"{out.stem}.{SIDECAR_NAME}")
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert len(data["figures"]) == 1


def test_no_snapshot_figure_unchanged_docx(hub: Hub, tmp_path: Path) -> None:
    ref = _draft_with_figure(hub, slug="ddplain", snapshot=None)

    text, out = _export_docx_text(hub, ref, tmp_path, "dplain")

    assert SECTION_TITLE not in text
    sidecar = out.with_name(f"{out.stem}.{SIDECAR_NAME}")
    assert not sidecar.exists()


# ── fmt() unit cases ──────────────────────────────────────────────


def test_fmt_float() -> None:
    assert fmt(1.0 / 3) == "0.333333"
    assert fmt(-12.345678) == "-12.3457"


def test_fmt_none() -> None:
    assert fmt(None) == "-"


def test_fmt_bool() -> None:
    assert fmt(True) == "true"
    assert fmt(False) == "false"


def test_fmt_str() -> None:
    assert fmt("Rh4O6") == "Rh4O6"


def test_fmt_int() -> None:
    assert fmt(5) == "5"


def test_fmt_objective_spec_dict() -> None:
    assert fmt({"key": "barrier", "sense": "min"}) == "barrier:min"


def test_fmt_generic_dict() -> None:
    assert fmt({"a": 1, "b": None}) == "a=1 b=-"


def test_header_lines_objectives_no_python_repr() -> None:
    fig = DataPackageFigure(label="dc1", caption="c", snapshot=_SNAPSHOT)
    joined = "\n".join(header_lines(fig))
    assert "objectives: energy:min, barrier:min" in joined
    assert "{'key'" not in joined


def test_verbatim_breakout_neutralized() -> None:
    """A hostile/unlucky title containing a literal ``\\end{verbatim}``
    must not close the verbatim block and inject live LaTeX."""
    snap = dict(_SNAPSHOT)
    snap["source"] = {
        "kind": "quest",
        "ref_id": 1,
        "handle": "qu1",
        "title": "x \\end{verbatim}\\section*{pwned}",
    }
    section = latex.build_data_package_section(
        [DataPackageFigure(label="dc1", caption="c", snapshot=snap)]
    )
    body = section.split("\\begin{verbatim}", 1)[1]
    inner = body.rsplit("\\end{verbatim}", 1)[0]
    assert "\\end{verbatim}" not in inner
    assert "\\section*{pwned}" not in section.rsplit("\\end{verbatim}", 1)[1]


# ── computed source contracts riding in a snapshot ──────────────────────


def _source_contract() -> dict[str, Any]:
    return build_external_source(
        producer="figrecipe",
        run_key="run-9",
        data=b'{"fe": {"pct": 95}}',
        path="outputs/result.json",
        encoding="json",
        selector="fe.pct",
        literal="95",
        unit="%",
        method="loaded ensemble",
    )


def test_snapshot_with_computed_source_freezes_fingerprints_latex(
    hub: Hub, tmp_path: Path
) -> None:
    snap = dict(_SNAPSHOT, computed_sources=[_source_contract()])
    ref = _draft_with_figure(hub, slug="dsrc", snapshot=snap)

    result = latex.export_draft(hub.live_store, ref, target_dir=tmp_path / "src")
    sidecar = (tmp_path / "src") / SIDECAR_NAME
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    (entry,) = data["figures"]
    assert entry["computed_source_fingerprints"] == [
        contract_fingerprint(_source_contract())
    ]
    # a well-formed contract rides verbatim and raises no computed warning
    assert entry["computed_sources"] == [_source_contract()]
    assert not [w for w in result.warnings if "computed source" in w]


def test_snapshot_with_malformed_computed_source_warns_latex(
    hub: Hub, tmp_path: Path
) -> None:
    snap = dict(_SNAPSHOT, computed_sources=[{"schema": "nope"}])
    ref = _draft_with_figure(hub, slug="dbad", snapshot=snap)

    result = latex.export_draft(hub.live_store, ref, target_dir=tmp_path / "bad")

    # the export still ships, but the operator sees the broken chain named
    assert any("computed source contract" in w for w in result.warnings)
    assert any("schema" in w for w in result.warnings)
    assert ((tmp_path / "bad") / SIDECAR_NAME).exists()


def test_snapshot_with_computed_source_docx(hub: Hub, tmp_path: Path) -> None:
    snap = dict(_SNAPSHOT, computed_sources=[_source_contract()])
    ref = _draft_with_figure(hub, slug="ddsrc", snapshot=snap)

    out = tmp_path / "dsrc.docx"
    result = docx.export_docx(hub.live_store, ref, target_path=out)

    sidecar = out.with_name(f"{out.stem}.{SIDECAR_NAME}")
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    (entry,) = data["figures"]
    assert entry["computed_source_fingerprints"] == [
        contract_fingerprint(_source_contract())
    ]
    assert not [w for w in result.warnings if "computed source" in w]
    # a malformed contract warns in docx exactly like latex
    snap = dict(_SNAPSHOT, computed_sources=[{"schema": "nope"}])
    ref = _draft_with_figure(hub, slug="ddbad", snapshot=snap)
    out2 = tmp_path / "dbad.docx"
    result = docx.export_docx(hub.live_store, ref, target_path=out2)
    assert any("computed source contract" in w for w in result.warnings)


def test_verify_snapshot_units() -> None:
    """verify_snapshot names every carried problem; pins=None checks shape
    only, pins=[...] also checks frozen-pin coverage (external contracts
    carry no pathway pin and always pass)."""
    contract = _source_contract()
    snap = dict(_SNAPSHOT, computed_sources=[contract, {"schema": "nope"}])
    problems = verify_snapshot(snap)
    assert problems and all(
        p.startswith("computed source contract [1]") for p in problems
    )
    assert any("schema" in p for p in problems)
    assert verify_snapshot(dict(_SNAPSHOT, computed_sources=[contract])) == []
    assert verify_snapshot(dict(_SNAPSHOT, computed_sources=[])) == []
    assert verify_snapshot(_SNAPSHOT) == []
    # pin coverage: a pathway contract the frozen pin set does not cover
    pathway_like = dict(
        contract,
        producer={
            "adapter": "pathway_seed",
            "job_id": 7,
            "job_type": "autocatpath_seed",
            "content_key": "seed-key",
            "seed": 3,
            "model_index": 0,
            "model": "mace:small",
            "pathway_ref_id": 11,
            "pathway_content_key": "pathway-key",
            "output_id": "a" * 64,
            "bundle_id": "b" * 64,
        },
    )
    uncovered = verify_snapshot(
        dict(_SNAPSHOT, computed_sources=[pathway_like]), pins=[]
    )
    assert len(uncovered) == 1 and "re-approve" in uncovered[0]
    covered = verify_snapshot(
        dict(_SNAPSHOT, computed_sources=[pathway_like]),
        pins=[{"pathway_ref_id": 11, "content_key": "pathway-key"}],
    )
    assert covered == []
    # the refusal idiom: a producer stamping contracts stops on a bad one
    with pytest.raises(BadInput, match="dc9") as exc:
        require_verified_snapshot(
            snap, label="dc9", pins=[{"pathway_ref_id": 11, "content_key": "k"}]
        )
    assert "schema" in str(exc.value)
