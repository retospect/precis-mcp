"""scripts/colour-codemod: palette classes and hex values -> semantic tokens."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/colour-codemod"


@pytest.fixture(scope="module")
def codemod() -> dict[str, Any]:
    return runpy.run_path(str(SCRIPT))


def _rewrite(codemod: dict[str, Any], tmp_path: Path, name: str, text: str) -> Any:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return codemod["rewrite"](path)


def test_classes_keep_variants_and_opacity(
    codemod: dict[str, Any], tmp_path: Path
) -> None:
    res = _rewrite(
        codemod,
        tmp_path,
        "a.html.j2",
        '<div class="text-slate-500 hover:bg-emerald-50 border-t-rose-200/60 '
        'bg-white text-white fill-white divide-slate-100 text-red-600">',
    )
    assert res.text == (
        '<div class="text-fg-subtle hover:bg-success-subtle border-t-danger/60 '
        'bg-surface text-on-solid fill-surface divide-line-subtle text-danger">'
    )
    assert res.classes == 8 and not res.left


def test_dark_ink_on_a_solid_fill_is_left(
    codemod: dict[str, Any], tmp_path: Path
) -> None:
    res = _rewrite(
        codemod, tmp_path, "b.html.j2", '<span class="bg-amber-500 text-slate-900">'
    )
    assert res.text == '<span class="bg-warning-solid-soft text-slate-900">'
    assert [what for _, what, _ in res.left] == ["text-slate-900"]


def test_css_hex_converts_but_script_and_legend_hex_stay(
    codemod: dict[str, Any], tmp_path: Path
) -> None:
    res = _rewrite(
        codemod,
        tmp_path,
        "c.html.j2",
        "<style>.x{color:#94a3b8;border:1px solid #e2e8f0}</style>\n"
        '<span style="background: linear-gradient(to right, #e0f2fe, #0369a1)"></span>\n'
        "<script>const c = '#94a3b8';</script>\n",
    )
    assert (
        ".x{color:rgb(var(--text-fg-faint));border:1px solid rgb(var(--border-line))}"
        in (res.text)
    )
    assert "#e0f2fe, #0369a1" in res.text and "'#94a3b8'" in res.text
    assert sorted(what for _, what, _ in res.left) == ["#0369a1", "#94a3b8", "#e0f2fe"]


def test_python_gets_the_class_pass_only(
    codemod: dict[str, Any], tmp_path: Path
) -> None:
    res = _rewrite(
        codemod,
        tmp_path,
        "d.py",
        'CLS = "bg-sky-50 text-sky-700"\nDEF = "classDef a fill:#e0f2fe,color:#0c4a6e"\n',
    )
    assert res.text == (
        'CLS = "bg-info-subtle text-info-strong"\nDEF = "classDef a fill:#e0f2fe,color:#0c4a6e"\n'
    )


def test_a_second_run_changes_nothing(codemod: dict[str, Any], tmp_path: Path) -> None:
    first = _rewrite(
        codemod,
        tmp_path,
        "e.html.j2",
        '<p class="text-slate-700 bg-teal-50 text-fuchsia-700" style="color:#fff">',
    )
    second = _rewrite(codemod, tmp_path, "e.html.j2", first.text)
    assert second.text == first.text and second.classes == second.hexes == 0
