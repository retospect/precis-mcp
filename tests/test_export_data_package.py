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
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.export import docx, latex
from precis.export._data_package import (
    SECTION_TITLE,
    SIDECAR_NAME,
    DataPackageFigure,
    fmt,
    header_lines,
    table_lines,
)
from precis.handlers.draft import DraftHandler
from precis.handlers.todo import TodoHandler

# A real 1×1 PNG for figure-embed tests (a valid raster blob).
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "C0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
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
