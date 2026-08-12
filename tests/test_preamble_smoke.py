"""Real-toolchain compile smoke test for the shipped draft preamble (gr53208).

Two prod PDF-export breaks (951c23a0, its ``\\IfFileExists``-nested
``\\newcommand`` predecessor) were preamble-only LaTeX errors a TeX-free
gate could not see — ``compile.py`` degrades cleanly when latexmk is
absent, so ``preamble.tex`` regressions sailed through green. The dev
image now bakes a minimal TeX Live (docker/Dockerfile dev-system stage),
so in the gate container this test assembles a minimal real document via
``assemble_document`` (which inlines the checked-in preamble) exercising
``\\newacronym`` + ``\\gls``/``\\glstip``/``\\glspltip`` + a biblatex cite,
and compiles it end-to-end with the production entrypoint
(``compile_pdf`` → latexmk → lualatex + biber + makeglossaries).

Skips on TeX-free hosts (same ``have_latexmk`` gate as the export path),
runs for real in the gate container — that asymmetry is the point.
"""

from __future__ import annotations

import re
import shutil
import zlib
from pathlib import Path

import pytest

from precis.export.compile import compile_pdf, have_latexmk
from precis.export.latex import assemble_document

pytestmark = pytest.mark.skipif(
    not (have_latexmk() and shutil.which("lualatex")),
    reason="TeX toolchain absent (runs in the gate container; skip on bare hosts)",
)

_BIB = """\
@article{smoke2026,
  author = {Smoke, Test},
  title = {A Minimal Entry},
  journal = {J. Gate Coverage},
  year = {2026},
}
"""


def _smoke_project(target: Path) -> None:
    """main.tex (preamble inlined by assemble_document) + refs.bib +
    .latexmkrc — the same self-contained project shape ``export_draft``
    writes."""
    body = "\n".join(
        [
            # First use expands long-short; later uses are the tooltip-wrapped
            # forms — the exact macros whose definitions broke twice in prod.
            "First use: \\gls{tla}.",
            "Later use: \\glstip{tla}, plural \\glspltip{tla}.",
            "A citation~\\cite{smoke2026}.",
        ]
    )
    main = assemble_document(
        title="Preamble gate smoke",
        author_block="\\author{precis}",
        body=body,
        acronyms="\\newacronym{tla}{TLA}{Three Letter Acronym}",
    )
    from precis.export.latex import _template_text

    (target / "main.tex").write_text(main, encoding="utf-8")
    (target / "refs.bib").write_text(_BIB, encoding="utf-8")
    (target / ".latexmkrc").write_text(_template_text("latexmkrc"), encoding="utf-8")


def test_preamble_compiles_with_tooltip_macros(tmp_path: Path) -> None:
    _smoke_project(tmp_path)
    res = compile_pdf(tmp_path, timeout_s=300)
    assert not res.skipped
    assert res.ok, f"preamble smoke compile failed:\n{res.log_tail}"
    assert res.pdf is not None and res.pdf.stat().st_size > 1024

    # The tooltip branch — not the degraded \else fallback — must be the one
    # that compiled: pdfcomment loaded, and the tooltip annotation carrying
    # the full term made it into the PDF. Without this, a TeX install missing
    # pdfcomment.sty would silently un-cover the exact regression surface.
    log_text = (tmp_path / "main.log").read_text(errors="replace")
    assert "pdfcomment" in log_text, "pdfcomment.sty not loaded — tooltip branch dark"
    tooltips = _tooltip_strings(res.pdf.read_bytes())
    assert "Three Letter Acronym" in tooltips, (
        f"tooltip annotation missing the full term; /TU strings found: {tooltips!r}"
    )


def _inflated(pdf: bytes) -> bytes:
    """The raw PDF plus every FlateDecode stream inflated — lualatex packs
    annotation dicts into compressed object streams, so the tooltip string
    is invisible to a plain byte search."""
    parts = [pdf]
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", pdf, re.DOTALL):
        try:
            parts.append(zlib.decompress(m.group(1).rstrip(b"\r\n")))
        except zlib.error:
            pass  # not Flate (image, already-raw) — irrelevant to the search
    return b"\n".join(parts)


def _pdf_unescape(raw: bytes) -> bytes:
    """PDF literal-string unescape: ``\\ooo`` octal + single-char escapes."""
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i : i + 1] == b"\\":
            octal = re.match(rb"\\([0-7]{1,3})", raw[i:])
            if octal:
                out.append(int(octal.group(1), 8))
                i += 1 + len(octal.group(1))
                continue
            out.append(raw[i + 1])
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return bytes(out)


def _tooltip_strings(pdf: bytes) -> list[str]:
    """Every widget-annotation ``/TU`` tooltip string, decoded (pdfcomment
    writes them as UTF-16BE-with-BOM literal strings)."""
    found = []
    for m in re.finditer(rb"/TU \(((?:[^()\\]|\\.)*)\)", _inflated(pdf)):
        text = _pdf_unescape(m.group(1))
        if text.startswith(b"\xfe\xff"):
            found.append(text[2:].decode("utf-16-be", errors="replace"))
        else:
            found.append(text.decode("latin-1"))
    return found
