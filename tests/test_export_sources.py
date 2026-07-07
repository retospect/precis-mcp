"""``precis.export.sources`` — resolve a draft's cited sources to local PDFs
and bundle them. Pure unit tests (no DB): a fake store + a temp corpus dir.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

from precis.export import latex, sources


class _FakeStore:
    """Minimal store surface :mod:`precis.export.sources` needs.

    ``refs`` maps ``(kind, slug) -> Ref-ish``; ``storage`` maps
    ``sha -> path str`` (the authoritative ``pdfs.storage_path``); ``held``
    is the set of shas ``pdf_held_anywhere`` reports true for.
    """

    def __init__(self, refs, storage=None, held=None):
        self._refs = refs
        self._storage = storage or {}
        self._held = set(held or ())

    def get_ref(self, *, kind, id):
        return self._refs.get((kind, id))

    def pdf_storage_path(self, sha):
        return self._storage.get(sha)

    def pdf_held_anywhere(self, sha):
        return sha in self._held


def _paper(slug, sha, *, kind="paper", title=None, authors=None, year=None):
    return SimpleNamespace(
        kind=kind,
        slug=slug,
        title=title or slug.title(),
        authors=authors,
        year=year,
        pdf_sha256=sha,
    )


def _draft():
    return SimpleNamespace(id=1, kind="draft", slug="mydraft", title="My Report")


def test_collect_splits_present_and_missing(tmp_path: Path) -> None:
    """A cited slug whose PDF is on this host lands in ``present``; the
    reasons distinguish no-such-ref / no-pdf / not-on-host(+held)."""
    root = tmp_path / "corpus"
    (root / "s").mkdir(parents=True)
    pdf = root / "s" / "smith2020.pdf"
    pdf.write_bytes(b"%PDF-smith")

    store = _FakeStore(
        refs={
            ("paper", "smith2020"): _paper(
                "smith2020", "a" * 64, authors=[{"name": "A. Smith"}], year=2020
            ),
            ("datasheet", "stm32"): _paper("stm32", "b" * 64, kind="datasheet"),
            ("paper", "nopdf"): _paper("nopdf", None),
        },
        storage={"a" * 64: str(pdf)},
        held={"b" * 64},  # datasheet held on another node, not here
    )
    bundle = sources.collect_cited_sources(
        store,
        _draft(),
        cited_slugs=["smith2020", "stm32", "nopdf", "ghost"],
        corpus_dirs=(root,),
    )
    by_slug = {e.slug: e for e in bundle.entries}
    assert by_slug["smith2020"].local_path == pdf
    assert by_slug["stm32"].reason == "not-on-host-held-elsewhere"
    assert by_slug["nopdf"].reason == "no-pdf"
    assert by_slug["ghost"].reason == "unresolved"
    assert [e.slug for e in bundle.present] == ["smith2020"]
    assert {e.slug for e in bundle.missing} == {"stm32", "nopdf", "ghost"}


def test_collect_falls_back_to_cite_key_convention(tmp_path: Path) -> None:
    """No authoritative storage_path → the cite_key shard convention finds
    the PDF across the configured roots."""
    root = tmp_path / "corpus"
    (root / "k").mkdir(parents=True)
    pdf = root / "k" / "kong24.pdf"
    pdf.write_bytes(b"%PDF-kong")
    store = _FakeStore(refs={("paper", "kong24"): _paper("kong24", "c" * 64)})
    bundle = sources.collect_cited_sources(
        store, _draft(), cited_slugs=["kong24"], corpus_dirs=(root,)
    )
    assert bundle.present[0].local_path == pdf


def test_collect_dedupes_repeated_slug(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    (root / "k").mkdir(parents=True)
    (root / "k" / "kong24.pdf").write_bytes(b"%PDF")
    store = _FakeStore(refs={("paper", "kong24"): _paper("kong24", "c" * 64)})
    bundle = sources.collect_cited_sources(
        store, _draft(), cited_slugs=["kong24", "kong24"], corpus_dirs=(root,)
    )
    assert len(bundle.entries) == 1


def test_build_sources_zip_papers_only(tmp_path: Path) -> None:
    """``build_sources_zip`` with no report → ``<slug>.pdf`` members +
    ``manifest.txt`` at the zip root."""
    root = tmp_path / "corpus"
    (root / "s").mkdir(parents=True)
    (root / "s" / "smith2020.pdf").write_bytes(b"%PDF-smith")
    store = _FakeStore(
        refs={("paper", "smith2020"): _paper("smith2020", "a" * 64)},
        storage={"a" * 64: str(root / "s" / "smith2020.pdf")},
    )
    out = tmp_path / "papers.zip"
    res = sources.build_sources_zip(
        store, _draft(), out, cited_slugs=["smith2020"], corpus_dirs=(root,)
    )
    assert res.path == out
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert names == {"smith2020.pdf", "manifest.txt"}
        manifest = zf.read("manifest.txt").decode()
    assert "Bundled: 1 of 1" in manifest
    assert "smith2020" in manifest


def test_build_sources_zip_with_report_nests_sources(tmp_path: Path) -> None:
    """With a report, the report sits at the zip root and sources go under
    ``sources/`` — a self-contained report+sources bundle. Missing sources
    still surface in the manifest."""
    root = tmp_path / "corpus"
    (root / "s").mkdir(parents=True)
    (root / "s" / "smith2020.pdf").write_bytes(b"%PDF-smith")
    report = tmp_path / "report.docx"
    report.write_bytes(b"PK-docx")
    store = _FakeStore(
        refs={
            ("paper", "smith2020"): _paper("smith2020", "a" * 64),
            ("paper", "gone"): _paper("gone", "z" * 64),
        },
        storage={"a" * 64: str(root / "s" / "smith2020.pdf")},
    )
    out = tmp_path / "bundle.zip"
    sources.build_sources_zip(
        store,
        _draft(),
        out,
        cited_slugs=["smith2020", "gone"],
        report_path=report,
        corpus_dirs=(root,),
    )
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        manifest = zf.read("sources/manifest.txt").decode()
    assert "report.docx" in names
    assert "sources/smith2020.pdf" in names
    assert "sources/manifest.txt" in names
    assert "Not bundled" in manifest and "gone" in manifest


# ── LaTeX appendix emission (pure, no DB) ─────────────────────────────


def test_build_source_appendix_emits_includepdf() -> None:
    """A present source becomes a bookmarked ``\\includepdf`` under an
    appendix section; a missing one becomes a comment + a warning."""
    bundle = sources.SourceBundle(
        entries=[
            sources.SourceEntry(
                slug="smith2020",
                kind="paper",
                title="A Study",
                authors="A. Smith",
                year=2020,
                pdf_sha256="a" * 64,
                local_path=Path("/tmp/smith2020.pdf"),
            ),
            sources.SourceEntry(
                slug="gone",
                kind="paper",
                title="Gone",
                authors="",
                year=None,
                pdf_sha256="z" * 64,
                local_path=None,
                reason="not-on-host",
            ),
        ]
    )
    warnings: list[str] = []
    tex = latex.build_source_appendix(bundle, warnings)
    assert r"\appendix" in tex and r"\section{Referenced Sources}" in tex
    assert r"\includepdf[pages=-]{sources/smith2020.pdf}" in tex
    assert r"\addcontentsline{toc}{subsection}{A Study}" in tex
    assert "% not bundled: paper:gone" in tex
    assert any("gone" in w for w in warnings)


def test_build_source_appendix_empty_when_nothing_present() -> None:
    """No local PDFs → no appendix at all (avoids an empty section)."""
    bundle = sources.SourceBundle(
        entries=[
            sources.SourceEntry(
                "gone", "paper", "Gone", "", None, "z" * 64, None, "not-on-host"
            )
        ]
    )
    assert latex.build_source_appendix(bundle, []) == ""


def test_preamble_template_carries_pdfpages() -> None:
    """The appendix's ``\\includepdf`` needs the ``pdfpages`` package in the
    checked-in preamble."""
    assert r"\usepackage{pdfpages}" in latex._template_text("preamble.tex")
