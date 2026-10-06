"""Compile the packaged poster scaffold: page size, content slots and column fit."""

from __future__ import annotations

import shutil
import subprocess
from importlib import resources
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("populated", "custom", "width_cm", "height_cm"),
    [(False, False, 84.1, 118.9), (True, False, 84.1, 118.9), (True, True, 92, 130)],
)
def test_poster_template_print_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    populated: bool,
    custom: bool,
    width_cm: float,
    height_cm: float,
) -> None:
    """The actual TeX resource renders empty or filled without private assets."""
    latexmk = shutil.which("latexmk")
    if latexmk is None:
        pytest.skip("latexmk required for poster render smoke")
    fitz = pytest.importorskip("fitz")
    template = (
        resources.files("precis.data.templates.draft")
        .joinpath("poster.tex")
        .read_text(encoding="utf-8")
    )
    if custom:
        template = template.replace(
            "size=a0,orientation=portrait,scale=1.13",
            "size=custom,orientation=portrait,width=92,height=130,scale=1.13",
        )
    if populated:
        slots = [
            r"\renewcommand{\PosterTitle}{Synthetic layout check}",
            r"\renewcommand{\PosterAuthors}{Example author}",
        ]
        for index, name in enumerate(("One", "Two", "Three"), start=1):
            slots.append(
                rf"\renewcommand{{\PosterColumn{name}}}{{"
                rf"\begin{{block}}{{Column {index}}}"
                rf"Top marker {index}\end{{block}}\vfill"
                rf"\begin{{block}}{{Lower panel}}"
                rf"Bottom marker {index}\end{{block}}}}"
            )
        template = template.replace(
            r"\begin{document}", "\n".join(slots) + "\n" + r"\begin{document}"
        )
    (tmp_path / "poster.tex").write_text(template, encoding="utf-8")
    # TeX's font cache stays inside this test, never in machine-wide directories.
    monkeypatch.setenv("TEXMFVAR", str(tmp_path / "texmf-var"))
    monkeypatch.setenv("TEXMFCACHE", str(tmp_path / "texmf-var"))
    compiled = subprocess.run(
        [
            latexmk,
            "-lualatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "poster.tex",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    assert r"Overfull \vbox" not in (tmp_path / "poster.log").read_text(
        encoding="utf-8"
    )
    with fitz.open(tmp_path / "poster.pdf") as pdf:
        assert len(pdf) == 1
        page = pdf[0]
        assert page.rect.width == pytest.approx(width_cm / 2.54 * 72, abs=1)
        assert page.rect.height == pytest.approx(height_cm / 2.54 * 72, abs=1)
        if not populated:
            assert page.get_text().strip() == ""
        else:
            assert "Synthetic layout check" in page.get_text()
            tops = [page.search_for(f"Top marker {i}")[0] for i in range(1, 4)]
            bottoms = [page.search_for(f"Bottom marker {i}")[0] for i in range(1, 4)]
            assert tops[0].x0 < tops[1].x0 < tops[2].x0
            assert max(rect.y0 for rect in tops) - min(rect.y0 for rect in tops) < 1
            assert (
                max(rect.y0 for rect in bottoms) - min(rect.y0 for rect in bottoms) < 1
            )
            assert all(
                0 <= rect.y0 < rect.y1 < page.rect.height for rect in tops + bottoms
            )
