"""``precis.export.reading_edition`` — pure unit tests (no DB): a fake block
store + monkeypatched claims lookups (mirrors ``tests/test_export_sources.py``'s
fake-store pattern). Never runs latexmk — assertions are against the emitted
``.tex`` text."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from precis.export import reading_edition as re_mod


@dataclass
class _FakeChunk:
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


class _FakeChunks:
    def __init__(self, blocks: list[_FakeChunk]) -> None:
        self._blocks = blocks

    def list_chunks_for_ref(self, ref_id: int) -> list[_FakeChunk]:
        return self._blocks


class _FakeStore:
    def __init__(self, blocks: list[_FakeChunk]) -> None:
        self.chunks = _FakeChunks(blocks)


def _source(
    slug: str = "smith2020", title: str = "A Study", authors: Any = None
) -> Any:
    return SimpleNamespace(
        id=42, kind="paper", slug=slug, title=title, authors=authors, meta={}
    )


def _main_tex(target_dir: Path) -> str:
    return (target_dir / "main.tex").read_text(encoding="utf-8")


# ── body: section headings ─────────────────────────────────────────────


def test_section_heading_emission_on_change(tmp_path: Path) -> None:
    blocks = [
        _FakeChunk("intro text", {"section_path": ["Introduction"]}),
        _FakeChunk("more intro", {"section_path": ["Introduction"]}),
        _FakeChunk("methods text", {"section_path": ["Methods", "Materials"]}),
        _FakeChunk("deeper", {"section_path": ["Methods", "Materials", "Reagents"]}),
    ]
    store = _FakeStore(blocks)
    res = re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert tex.count("\\section*{Introduction}") == 1  # not re-emitted for chunk 2
    assert "\\subsection*{Materials}" in tex
    assert "\\subsubsection*{Reagents}" in tex
    assert res.chunk_count == 4


def test_no_heading_when_section_path_absent(tmp_path: Path) -> None:
    blocks = [_FakeChunk("plain prose", {})]
    store = _FakeStore(blocks)
    re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "\\section*{" not in tex
    assert "plain prose" in tex


# ── body: image-only chunk placeholder ─────────────────────────────────


def test_image_only_chunk_placeholder_no_original(tmp_path: Path) -> None:
    blocks = [_FakeChunk('<span id="page-3-0"></span>![](_page_3_Figure_1.jpeg)', {})]
    store = _FakeStore(blocks)
    re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "\\emph{[figure — not available on this host]}" in tex
    assert "_page_3_Figure_1" not in tex  # the raw marker never leaks


def test_image_only_chunk_placeholder_with_original(tmp_path: Path) -> None:
    original = tmp_path / "orig.pdf"
    original.write_bytes(b"%PDF-1.4 fake")
    out_dir = tmp_path / "out"
    blocks = [_FakeChunk('<span id="page-3-0"></span>![](_page_3_Figure_1.jpeg)', {})]
    store = _FakeStore(blocks)
    re_mod.export_reading_edition(store, _source(), out_dir, original_pdf=original)
    tex = _main_tex(out_dir)
    assert "\\emph{[figure — see original PDF appended below]}" in tex


# ── claims appendix ──────────────────────────────────────────────────


def test_claims_appendix_renders_state_chip(tmp_path: Path, monkeypatch: Any) -> None:
    hub = {
        "hub_ref_id": 501,
        "pub_id": "pub123",
        "claim": "X causes Y",
        "role": "establishes",
    }
    monkeypatch.setattr(
        "precis.taproot.lookup.hubs_grounded_by_paper",
        lambda store, ref_id, *, require_pub_id=False: [hub],
    )
    row = SimpleNamespace(ref_id=501, state="published")
    monkeypatch.setattr(
        "precis.nanopub.overview.hub_rows",
        lambda store, *, ref_ids: [row],
    )
    store = _FakeStore([_FakeChunk("body", {})])
    res = re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "Claims grounded in this source" in tex
    assert "X causes Y" in tex
    assert "fi501" in tex
    assert "published" in tex
    assert "establishes" in tex
    assert res.claim_count == 1


def test_claims_appendix_unminted_state_when_no_publish_row(
    tmp_path: Path, monkeypatch: Any
) -> None:
    hub = {
        "hub_ref_id": 502,
        "pub_id": None,
        "claim": "A causes B",
        "role": "corroborates",
    }
    monkeypatch.setattr(
        "precis.taproot.lookup.hubs_grounded_by_paper",
        lambda store, ref_id, *, require_pub_id=False: [hub],
    )
    monkeypatch.setattr(
        "precis.nanopub.overview.hub_rows",
        lambda store, *, ref_ids: [],
    )
    store = _FakeStore([_FakeChunk("body", {})])
    re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "unminted" in tex


def test_claims_appendix_omitted_when_empty(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "precis.taproot.lookup.hubs_grounded_by_paper",
        lambda store, ref_id, *, require_pub_id=False: [],
    )
    store = _FakeStore([_FakeChunk("body", {})])
    res = re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "Claims grounded in this source" not in tex
    assert res.claim_count == 0


def test_claims_appendix_degrades_on_lookup_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr("precis.taproot.lookup.hubs_grounded_by_paper", _boom)
    store = _FakeStore([_FakeChunk("body", {})])
    res = re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "Claims grounded in this source" not in tex
    assert res.claim_count == 0


# ── original PDF appendix ───────────────────────────────────────────────


def test_original_appended_when_given(tmp_path: Path) -> None:
    original = tmp_path / "orig.pdf"
    original.write_bytes(b"%PDF-1.4 fake")
    out_dir = tmp_path / "out"
    store = _FakeStore([_FakeChunk("body", {})])
    res = re_mod.export_reading_edition(
        store, _source(), out_dir, original_pdf=original
    )
    tex = _main_tex(out_dir)
    assert "\\includepdf[pages=-]{sources/original.pdf}" in tex
    assert (out_dir / "sources" / "original.pdf").is_file()
    assert "Original PDF not available on this host" not in tex
    assert res.has_original is True


def test_no_original_note_when_absent(tmp_path: Path) -> None:
    store = _FakeStore([_FakeChunk("body", {})])
    res = re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    # The preamble carries an unrelated ``\includepdf`` mention in a comment
    # (pdfpages package note) — assert on the actual command form we'd emit.
    assert "\\includepdf[pages=-]" not in tex
    assert "Original PDF not available on this host" in tex
    assert res.has_original is False


# ── project scaffolding ─────────────────────────────────────────────────


def test_project_files_written(tmp_path: Path) -> None:
    store = _FakeStore([_FakeChunk("body", {})])
    re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    assert (tmp_path / "refs.bib").read_text(encoding="utf-8") == ""
    assert (tmp_path / "preamble.tex").is_file()
    assert (tmp_path / ".latexmkrc").is_file()
    tex = _main_tex(tmp_path)
    assert "\\addbibresource{refs.bib}" in tex


def test_zero_chunks_no_original_reports_empty(tmp_path: Path) -> None:
    store = _FakeStore([])
    res = re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    assert res.chunk_count == 0
    assert res.has_original is False


# ── escaping ─────────────────────────────────────────────────────────────


def test_body_text_escaped(tmp_path: Path) -> None:
    blocks = [_FakeChunk("50% & $x_1$", {})]
    store = _FakeStore(blocks)
    re_mod.export_reading_edition(store, _source(), tmp_path, original_pdf=None)
    tex = _main_tex(tmp_path)
    assert "50\\% \\& \\$x\\_1\\$" in tex
    assert "50% &" not in tex
    assert "$x_1$" not in tex
