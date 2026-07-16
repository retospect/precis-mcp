"""Unit tests for the markup-first producer (no DB, no network).

Covers the pure parsers in :mod:`precis.ingest.markup` (JATS / arXiv
HTML / LaTeX flatten-and-chunk) and the :func:`extract_paper_from_markup`
assembly in :mod:`precis.ingest.pipeline`. Requires the ``tex`` extra
(lxml); skipped cleanly when lxml is absent.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("lxml")

from precis.ingest.markup import (
    MARKUP_FORMATS,
    MarkupParseError,
    parse_arxiv_html,
    parse_elsevier,
    parse_jats,
    parse_latex,
    parse_markup,
    sniff_xml_format,
)

# ---------------------------------------------------------------------------
# JATS
# ---------------------------------------------------------------------------

_JATS = b"""<?xml version="1.0"?>
<article>
 <front><article-meta>
  <article-id pub-id-type="doi">10.1234/abc</article-id>
  <title-group><article-title>A Study of Widgets</article-title></title-group>
  <contrib-group>
   <contrib contrib-type="author">
    <name><surname>Smith</surname><given-names>Jane</given-names></name>
   </contrib>
  </contrib-group>
  <pub-date><year>2021</year></pub-date>
  <abstract><p>We study widgets at 1.5 eV.</p></abstract>
  <kwd-group><kwd>widgets</kwd><kwd>spectroscopy</kwd></kwd-group>
 </article-meta></front>
 <body>
  <sec><title>Introduction</title>
   <p>Widgets are important. We measured 12% yield.</p>
   <sec><title>Methods</title><p>We used a spectrometer.</p></sec>
  </sec>
 </body>
 <back><ref-list><ref><mixed-citation>Doe 2019</mixed-citation></ref></ref-list></back>
</article>"""


def test_parse_jats_metadata() -> None:
    ext = parse_jats(_JATS)
    assert ext.title == "A Study of Widgets"
    assert ext.doi == "10.1234/abc"
    assert ext.year == 2021
    assert ext.authors == [{"name": "Smith, Jane"}]
    assert "widgets" in ext.abstract.lower()
    assert ext.keywords == ["widgets", "spectroscopy"]


def test_parse_jats_section_paths_and_references() -> None:
    ext = parse_jats(_JATS)
    body = [b for b in ext.blocks if b["type"] != "references"]
    refs = [b for b in ext.blocks if b["type"] == "references"]
    assert body[0]["section_path"] == ["Introduction"]
    assert body[1]["section_path"] == ["Introduction", "Methods"]
    assert refs and "Doe 2019" in refs[0]["text"]


def test_parse_jats_no_body_raises() -> None:
    with pytest.raises(MarkupParseError):
        parse_jats(b"<article><front></front></article>")


# ---------------------------------------------------------------------------
# Elsevier full-text XML
# ---------------------------------------------------------------------------

_ELSEVIER = b"""<?xml version="1.0"?>
<full-text-retrieval-response
    xmlns:ce="http://www.elsevier.com/xml/common/dtd"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"
    xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
 <coredata>
  <dc:title>Elsevier Widgets</dc:title>
  <prism:doi>10.1016/j.widget.2021.01.001</prism:doi>
 </coredata>
 <originalText>
  <xocs:serial-item>
   <article>
    <head>
     <ce:author><ce:surname>Curie</ce:surname><ce:given-name>Marie</ce:given-name></ce:author>
     <ce:abstract><ce:para>We study elsevier widgets at scale.</ce:para></ce:abstract>
    </head>
    <body>
     <ce:sections>
      <ce:section>
       <ce:section-title>Introduction</ce:section-title>
       <ce:para>Widgets matter in industry.</ce:para>
      </ce:section>
     </ce:sections>
    </body>
   </article>
  </xocs:serial-item>
 </originalText>
</full-text-retrieval-response>"""


def test_parse_elsevier_metadata_and_body() -> None:
    ext = parse_elsevier(_ELSEVIER)
    assert ext.source_format == "elsevier_xml"
    assert ext.title == "Elsevier Widgets"
    assert ext.doi == "10.1016/j.widget.2021.01.001"
    assert ext.authors == [{"name": "Curie, Marie"}]
    assert "elsevier widgets" in ext.abstract.lower()
    body = [b for b in ext.blocks if b["type"] == "paragraph"]
    assert body and "Widgets matter" in body[0]["text"]
    assert body[0]["section_path"] == ["Introduction"]


def test_parse_elsevier_no_body_raises() -> None:
    with pytest.raises(MarkupParseError):
        parse_elsevier(
            b"<full-text-retrieval-response><coredata>"
            b"<dc:title xmlns:dc='u'>x</dc:title></coredata>"
            b"</full-text-retrieval-response>"
        )


def test_sniff_xml_format_discriminates_elsevier_from_jats() -> None:
    # The root element / namespace decides: a hand-dropped Elsevier XML must not
    # be force-parsed as JATS (gripe 161850).
    assert sniff_xml_format(_ELSEVIER) == "elsevier_xml"
    assert sniff_xml_format(_JATS) == "jats"
    # An Elsevier doc identified purely by its ce: namespace (no wrapper elem).
    ns_only = (
        b"<article xmlns:ce='http://www.elsevier.com/xml/common/dtd'><body/></article>"
    )
    assert sniff_xml_format(ns_only) == "elsevier_xml"
    assert sniff_xml_format(b"not xml at all \x00") is None


def test_parse_xml_does_not_expand_entities_billion_laughs() -> None:
    # Untrusted XML: the parser is hardened (resolve_entities=False,
    # no_network=True), so an internal entity bomb must NOT expand into the
    # extracted text (gripe 161850 #3). Parsing completes cheaply.
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE article [
 <!ENTITY lol "lol">
 <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<article>
 <front><article-meta><title-group><article-title>Bomb</article-title></title-group>
 </article-meta></front>
 <body><sec><title>S</title><p>payload starts &lol3; payload ends</p></sec></body>
</article>"""
    ext = parse_jats(bomb)
    blob = " ".join(b["text"] for b in ext.blocks)
    # The bomb never expands to its 1000×"lol" payload — the entity is left
    # unresolved, so no quadratic/exponential blowup reaches the output.
    assert "lol" * 100 not in blob
    assert len(blob) < 10_000


# ---------------------------------------------------------------------------
# arXiv HTML (LaTeXML shape)
# ---------------------------------------------------------------------------

_ARXIV_HTML = b"""<!DOCTYPE html>
<html><body>
 <article class="ltx_document">
  <h1 class="ltx_title ltx_title_document">Neural Widgets</h1>
  <span class="ltx_personname">Ada Lovelace</span>
  <div class="ltx_abstract"><p>Widgets, but neural.</p></div>
  <section class="ltx_section">
   <h2 class="ltx_title">Intro</h2>
   <p class="ltx_p">We train on 1000 samples.</p>
  </section>
  <ul class="ltx_biblist"><li>Ref one.</li></ul>
 </article>
</body></html>"""


def test_parse_arxiv_html_basic() -> None:
    ext = parse_arxiv_html(_ARXIV_HTML)
    assert ext.title == "Neural Widgets"
    assert {"name": "Ada Lovelace"} in ext.authors
    body = [b for b in ext.blocks if b["type"] != "references"]
    refs = [b for b in ext.blocks if b["type"] == "references"]
    assert any("train on 1000" in b["text"] for b in body)
    assert body[0]["section_path"] == ["Intro"]
    assert refs and "Ref one" in refs[0]["text"]


def test_parse_arxiv_html_extracts_arxiv_id_from_url() -> None:
    # The identity a DOI-less arXiv paper needs comes from the source URL —
    # arXiv markup is only ever reached by fetching a known id.
    ext = parse_arxiv_html(
        _ARXIV_HTML, source_url="https://arxiv.org/html/2301.12345v2"
    )
    assert ext.arxiv_id == "2301.12345v2"


def test_parse_arxiv_html_no_url_leaves_arxiv_id_none() -> None:
    ext = parse_arxiv_html(_ARXIV_HTML)
    assert ext.arxiv_id is None


# ---------------------------------------------------------------------------
# LaTeX flatten-and-chunk
# ---------------------------------------------------------------------------


def _make_tarball(tmp_path: Path, files: dict[str, str]) -> Path:
    tarpath = tmp_path / "src.tar.gz"
    with tarfile.open(tarpath, "w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return tarpath


def test_parse_latex_follows_input_and_skips_anc(tmp_path: Path) -> None:
    main = r"""
\documentclass{article}
\title{Flatten Test}
\begin{document}
% a comment
\section{Intro}
This is the intro with 5 eV. \cite{foo}
\input{methods}
\begin{thebibliography}{9}
\bibitem{foo} Foo et al. 2020.
\end{thebibliography}
\end{document}
"""
    methods = r"""
\section{Methods}
We used a \textbf{spectrometer}.
"""
    anc = r"\documentclass{article} junk that must be ignored"
    tarball = _make_tarball(
        tmp_path,
        {"main.tex": main, "methods.tex": methods, "anc/extra.tex": anc},
    )
    ext = parse_latex(tarball)
    assert ext.title == "Flatten Test"
    body = [b for b in ext.blocks if b["type"] != "references"]
    refs = [b for b in ext.blocks if b["type"] == "references"]
    assert body[0]["section_path"] == ["Intro"]
    assert any("intro" in b["text"].lower() for b in body)
    assert any(b["section_path"] == ["Methods"] for b in body)
    assert any("spectrometer" in b["text"] for b in body)
    assert refs and "Foo et al" in refs[0]["text"]


def test_parse_latex_macro_density_gate(tmp_path: Path) -> None:
    # A body dominated by unexpanded macros trips the OCR-fallback gate.
    macro_soup = (
        r"\documentclass{article}\begin{document}\section{X} "
        + " ".join([r"\customcmd"] * 40)
        + r"\end{document}"
    )
    tarball = _make_tarball(tmp_path, {"main.tex": macro_soup})
    with pytest.raises(MarkupParseError):
        parse_latex(tarball)


def test_parse_latex_no_documentclass_raises(tmp_path: Path) -> None:
    tarball = _make_tarball(tmp_path, {"orphan.tex": "just some text, no class"})
    with pytest.raises(MarkupParseError):
        parse_latex(tarball)


def test_parse_latex_extracts_arxiv_id_from_url(tmp_path: Path) -> None:
    main = (
        r"\documentclass{article}\title{T}\begin{document}"
        r"\section{Intro} body text with content \end{document}"
    )
    tarball = _make_tarball(tmp_path, {"main.tex": main})
    ext = parse_latex(tarball, source_url="https://arxiv.org/e-print/2301.12345")
    assert ext.arxiv_id == "2301.12345"


def test_read_latex_sources_member_count_bomb(tmp_path: Path, monkeypatch) -> None:
    # A tarball with more members than the cap is refused, not expanded.
    from precis.ingest import markup as _markup

    monkeypatch.setattr(_markup, "_LATEX_MAX_MEMBERS", 1)
    tarball = _make_tarball(
        tmp_path, {"a.tex": "\\documentclass{article}", "b.tex": "x"}
    )
    with pytest.raises(MarkupParseError):
        _markup._read_latex_sources(tarball)


def test_read_latex_sources_skips_oversized_member(tmp_path: Path, monkeypatch) -> None:
    # An oversized member is dropped on its declared size (before it is read),
    # so a bomb can't OOM the worker. With the only source skipped, parse_latex
    # then reports "no sources" rather than crashing.
    from precis.ingest import markup as _markup

    monkeypatch.setattr(_markup, "_LATEX_MAX_MEMBER_BYTES", 4)
    tarball = _make_tarball(
        tmp_path, {"main.tex": "\\documentclass{article} plenty of bytes here"}
    )
    files, _bbl = _markup._read_latex_sources(tarball)
    assert files == {}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_parse_markup_dispatch_jats(tmp_path: Path) -> None:
    p = tmp_path / "a.xml"
    p.write_bytes(_JATS)
    ext = parse_markup(p, fmt="jats")
    assert ext.source_format == "jats"
    assert ext.title == "A Study of Widgets"


def test_parse_markup_unknown_format_raises(tmp_path: Path) -> None:
    p = tmp_path / "a.xml"
    p.write_bytes(_JATS)
    with pytest.raises(MarkupParseError):
        parse_markup(p, fmt="pdf")


def test_markup_formats_constant() -> None:
    assert frozenset({"jats", "elsevier_xml", "arxiv_html", "latex"}) == MARKUP_FORMATS


# ---------------------------------------------------------------------------
# pipeline assembly — extract_paper_from_markup
# ---------------------------------------------------------------------------


def test_extract_paper_from_markup_chunks_only(tmp_path: Path) -> None:
    # pipeline imports the crossref/S2 lookup chain (habanero — a paper
    # extra). Present in the dev container; skip on a torch-free host.
    pytest.importorskip("habanero")
    from precis.ingest.pipeline import extract_paper_from_markup

    p = tmp_path / "a.xml"
    p.write_bytes(_JATS)
    paper = extract_paper_from_markup(p, fmt="jats", source_url="http://example/x")

    assert paper.title == "A Study of Widgets"
    assert paper.doi == "10.1234/abc"
    assert paper.provider == "markup"
    assert paper.pdf_sha256 is None  # chunks-only: no printable
    assert paper.pdf_role is None
    assert paper.meta["source_format"] == "jats"
    assert paper.meta["markup_source_url"] == "http://example/x"
    # cards (ord < 0) + body chunks (ord >= 0) both present.
    assert any(c.ord < 0 for c in paper.chunks)
    assert any(c.ord >= 0 for c in paper.chunks)
    assert paper.content_hash is not None


def test_extract_paper_from_markup_arxiv_html_no_doi(tmp_path: Path) -> None:
    # Regression: a DOI-less arXiv HTML must NOT crash on identity — the
    # arXiv id is recovered from the source URL, giving paper_id 'arxiv:…'.
    pytest.importorskip("habanero")
    from precis.ingest.pipeline import extract_paper_from_markup

    p = tmp_path / "a.html"
    p.write_bytes(_ARXIV_HTML)
    paper = extract_paper_from_markup(
        p, fmt="arxiv_html", source_url="https://arxiv.org/html/2301.12345"
    )
    assert paper.arxiv_id == "2301.12345"
    assert paper.paper_id == "arxiv:2301.12345"
    assert paper.doi is None
    assert paper.pdf_sha256 is None


def test_extract_paper_from_markup_no_identity_raises_parse_error(
    tmp_path: Path,
) -> None:
    # Regression: an id-less markup (no DOI, no arXiv id in the URL, no
    # companion PDF) raises MarkupParseError — which _ingest_markup catches to
    # fall back to OCR — NOT a bare ValueError that would crash the worker.
    pytest.importorskip("habanero")
    from precis.ingest.pipeline import extract_paper_from_markup

    p = tmp_path / "a.html"
    p.write_bytes(_ARXIV_HTML)
    with pytest.raises(MarkupParseError):
        extract_paper_from_markup(p, fmt="arxiv_html", source_url="http://example/x")
