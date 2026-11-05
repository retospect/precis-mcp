"""Audience links, external supplements and private-URL refusal (gr553415)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zipfile import ZipFile

import pytest
from lxml import etree

from precis.errors import BadInput
from precis.export import audience as policy
from precis.export import docx, latex
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry
from tests._refs import insert_gripe

_BASE = "https://precis.example.test"
_W = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _xml_text(element: etree._Element) -> str:
    return "".join(str(text) for text in element.itertext())


@pytest.fixture(autouse=True)
def export_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install-local URL, and deterministic discovery of the pathway code."""
    from precis import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda *_a, **_kw: _BASE)
    monkeypatch.setattr(handle_registry, "_plugins_loaded", True)
    monkeypatch.setattr(handle_registry, "_plugin_kind_codes", {"pathway": "pw"})
    monkeypatch.setattr(handle_registry, "_plugin_chunk_codes", {})


class _Records:
    """No database needed for inline formatting or reference ordering."""

    def get_ref(self, *, kind: str, id: Any) -> Any:
        return SimpleNamespace(id=id, kind=kind, title=f"Stored {kind} {id}", meta={})


@pytest.mark.parametrize("backend", ["latex", "docx"])
def test_dev_links_each_record_kind(backend: str) -> None:
    store = _Records()
    state = policy.for_export(store, "dev")
    text = "[pw11] [st12] [gr13] [qu14]"
    if backend == "latex":
        ctx = latex._Ctx(keymap={}, known_handles=set(), store=store, audience=state)
        rendered = latex._render_inline(text, ctx)
        urls = rendered
        assert rendered.count("\\href") == 4
    else:
        from docx import Document

        paragraph = Document().add_paragraph()
        context = docx._Ctx(store=store, known_handles=set(), audience=state)
        docx._render_inline(text, context, paragraph)
        urls = "\n".join(
            str(rel.target_ref)
            for rel in paragraph.part.rels.values()
            if rel.is_external
        )
        assert len(paragraph._p.xpath("w:hyperlink")) == 4
    for kind, number in (
        ("pathway", 11),
        ("structure", 12),
        ("gripe", 13),
        ("quest", 14),
    ):
        assert f"{_BASE}/r/{kind}/{number}" in urls
    assert state.entries == []


def test_kind_numbering_uses_first_mention_and_reuses_number() -> None:
    state = policy.Audience(_Records(), "external", _BASE)
    handles = ["st8", "pw9", "qu4", "pw2", "jo7", "gr6", "pw9", "jo3", "st8"]
    references = [state.reference(handle, None) for handle in handles]
    assert [ref.text for ref in references if ref is not None] == [
        "S2.1",
        "S1.1",
        "S4.1",
        "S1.2",
        "S3.1",
        "S5.1",
        "S1.1",
        "S3.2",
        "S2.1",
    ]
    assert [entry.handle for entry in state.entries] == [
        "st8",
        "pw9",
        "qu4",
        "pw2",
        "jo7",
        "gr6",
        "jo3",
    ]
    assert all(ref and ref.footnote and not ref.url for ref in references)


@pytest.mark.parametrize("backend", ["latex", "docx"])
def test_external_export_has_footnote_table_and_zip(
    backend: str, store: Store, tmp_path: Path
) -> None:
    record = insert_gripe(store, "Stored evidence description")
    ref, _title = store.drafts.create_draft(name="audience-external", title="Report")
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Evidence [gr{record.id}] and again [gr{record.id}].",
        at={"last": True},
    )
    if backend == "latex":
        result = latex.export_draft(
            store, ref, target_dir=tmp_path, audience="external"
        )
        rendered = result.main_tex.read_text(encoding="utf-8")
        assert "\\footnote" in rendered
        assert "Stored evidence description" in rendered
        assert "Supplement" in rendered
        assert "\\begin{longtable}" in rendered or "\\begin{tabular}" in rendered
        assert "S4.1" in rendered
        assert _BASE not in rendered
    else:
        target = tmp_path / "report.docx"
        docx.export_docx(store, ref, target_path=target, audience="external")
        with ZipFile(target) as archive:
            document = etree.fromstring(archive.read("word/document.xml"))
            footnotes = etree.fromstring(archive.read("word/footnotes.xml"))
            assert document.xpath("//w:footnoteReference", namespaces=_W)
            assert "Stored evidence description" in _xml_text(footnotes)
            tables = cast(
                list[etree._Element], document.xpath("//w:tbl", namespaces=_W)
            )
            assert any("S4.1" in _xml_text(table) for table in tables)
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    assert _BASE.encode() not in archive.read(name)
    with ZipFile(tmp_path / "supplement.zip") as archive:
        index = json.loads(archive.read("index.json"))
        assert len(index) == 1
        assert index[0]["number"] == "S4.1"
        assert index[0]["handle"] == f"gr{record.id}"
        assert index[0]["description"] == "Stored evidence description"
        entry = json.loads(archive.read("S4.1/entry.json"))
        assert entry["title"] == "Stored evidence description"


@pytest.mark.parametrize("backend", ["latex", "docx"])
def test_omitted_audience_still_exports_dev_links(
    backend: str, store: Store, tmp_path: Path
) -> None:
    record = insert_gripe(store, "Evidence")
    ref, _title = store.drafts.create_draft(name="audience-default", title="Report")
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Evidence [gr{record.id}].",
        at={"last": True},
    )
    expected = f"{_BASE}/r/gripe/{record.id}"
    if backend == "latex":
        result = latex.export_draft(store, ref, target_dir=tmp_path)
        assert expected in result.main_tex.read_text(encoding="utf-8")
    else:
        target = tmp_path / "report.docx"
        docx.export_docx(store, ref, target_path=target)
        with ZipFile(target) as archive:
            assert expected in archive.read("word/_rels/document.xml.rels").decode()
    assert not (tmp_path / "supplement.zip").exists()


@pytest.mark.parametrize("backend", ["latex", "docx"])
def test_external_guard_names_chunk_before_writing(
    backend: str, store: Store, tmp_path: Path
) -> None:
    ref, _title = store.drafts.create_draft(name="audience-refusal", title="Report")
    chunk = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Details at {_BASE}/r/pathway/7.",
        at={"last": True},
    )[0]
    target = tmp_path / "refused"
    with pytest.raises(BadInput, match=chunk.dc):
        if backend == "latex":
            latex.export_draft(store, ref, target_dir=target, audience="external")
        else:
            docx.export_docx(
                store, ref, target_path=target / "report.docx", audience="external"
            )
    assert not target.exists()


def test_guard_reports_all_chunks_and_checks_table_metadata() -> None:
    chunks = [
        SimpleNamespace(dc="dc10", handle="h10", text=f"<{_BASE}/one>", meta={}),
        SimpleNamespace(
            dc="dc11",
            handle="h11",
            text="Table caption",
            meta={"table": {"rows": [[f"[details]({_BASE}/two)"]]}},
        ),
    ]
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: chunks))
    ref = SimpleNamespace(id=1, title="Report")
    with pytest.raises(BadInput) as exc:
        policy.guard_audience(store, ref, "external")
    assert "dc10" in str(exc.value)
    assert "dc11" in str(exc.value)
    policy.guard_audience(store, ref, "dev")


def test_invalid_audience_refuses_without_reading_store() -> None:
    with pytest.raises(BadInput, match="audience"):
        policy.guard_audience(None, None, "public")


@pytest.mark.parametrize("base_url", [_BASE, ""], ids=["configured", "unconfigured"])
@pytest.mark.parametrize(
    "text",
    [
        "[https://example.org/public](http://private.example.internal/report)",
        "https://example.org/public)http://private.example.internal/report",
        "https://example.org/public]http://private.example.internal/report",
        "https://example.org/public'http://private.example.internal/report'",
        'https://example.org/public"http://private.example.internal/report"',
        "https://example.org/public<http://private.example.internal/report>",
        "https://example.org/public http://private.example.internal/report",
    ],
    ids=[
        "markdown",
        "parenthesis",
        "bracket",
        "single-quote",
        "double-quote",
        "angle",
        "space",
    ],
)
def test_guard_audience_finds_private_url_after_public_url(
    monkeypatch: pytest.MonkeyPatch, base_url: str, text: str
) -> None:
    monkeypatch.setattr(policy, "web_base_url", lambda _store: base_url)
    chunk = SimpleNamespace(dc="dc42", text=text, meta={})
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: [chunk]))
    ref = SimpleNamespace(id=1, title="Report")
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_audience(store, ref, "external")
    policy.guard_audience(store, ref, "dev")


@pytest.mark.parametrize("separator", [",", ";", "|", ""])
@pytest.mark.parametrize("scheme", ["http", "hTtPs", "CuStOm+V1"])
@pytest.mark.parametrize("base_url", [_BASE, ""])
def test_all_guards_check_each_scheme_occurrence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    separator: str,
    scheme: str,
    base_url: str,
) -> None:
    monkeypatch.setattr(policy, "web_base_url", lambda _store: base_url)
    text = f"See https://example.org/{separator}{scheme}://private.example.internal/report."
    chunk = SimpleNamespace(dc="dc42", text=text, meta={})
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: [chunk]))
    ref = SimpleNamespace(id=1, title="Report")
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_text(text, "chunk dc42", base_url)
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_audience(store, ref, "external")
    path = tmp_path / "main.tex"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(BadInput, match="main.tex.*dc42"):
        policy.guard_artifacts(store, ref, [path])


@pytest.mark.parametrize(
    "text",
    [
        f"[details]({_BASE})",
        f"See {_BASE}.",
        f"See [{_BASE}]",
        f"[details]({_BASE.upper()}/record)",
        f"<{_BASE}/record>",
        f"`{_BASE}`",
        f"**{_BASE}**",
        f"*{_BASE}*.",
        f"__{_BASE}__!",
        f"~~{_BASE}~~",
        f"[x]({_BASE}:8443)",
        f"<{_BASE}:8443/record>",
        f"**{_BASE}/record**",
        f"[**{_BASE}**].",
    ],
)
def test_guard_recognizes_private_origins_inside_markup(text: str) -> None:
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_text(text, "chunk dc42", _BASE)


@pytest.mark.parametrize("base_url", [_BASE, ""])
@pytest.mark.parametrize(
    "url",
    [
        "https://public.example.test@private.example.internal/report",
        "https://public.example.test:password@private.example.internal/report",
        "https://first@public.example.test@private.example.internal/report",
        "//private.example.internal/report",
        "https://PRIVATE.EXAMPLE.INTERNAL/report",
        "https://public.example.test@private.example.internal:8443/report",
        "//public.example.test:password@PRIVATE.EXAMPLE.INTERNAL:8443/report",
        "https://[2001:db8::1]:8443/report",
        "//public.example.test:password@[2001:db8::1]:8443/report",
    ],
)
def test_guard_classifies_authority_host(url: str, base_url: str) -> None:
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_text(f"[details]({url})", "chunk dc42", base_url)


@pytest.mark.parametrize(
    "url",
    [
        "https://public.example.test/report",
        "//PUBLIC.EXAMPLE.TEST:8443/report",
        "https://private.example.internal:password@public.example.test/report",
        "https://public.example.test/path/private.example.internal/report",
        "https://public.example.test/path/@private.example.internal",
        "https://public.example.test?next=@private.example.internal",
        "https://public.example.test#@private.example.internal",
    ],
)
def test_guard_allows_public_authority(url: str) -> None:
    policy.guard_text(f"[details]({url})", "chunk dc42", _BASE)


@pytest.mark.parametrize("host", ["équipe.internal", "équipe.example.test"])
def test_guard_recognizes_unicode_private_hosts(host: str) -> None:
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_text(f"https://{host}/report", "chunk dc42", f"https://{host}")
    if host.endswith(".internal"):
        with pytest.raises(BadInput, match="dc42"):
            policy.guard_text(f"https://{host}/report", "chunk dc42", "")


@pytest.mark.parametrize(
    "text",
    [
        "Read https://example.org/a//b",
        "n//2",
        "//2",
        "//word/report",
        "n//private.example.internal/report",
        "https://example.org/?next=//private.example.internal/report",
        "https://example.org/a//private.example.internal/report",
        "//example.org/?next=//private.example.internal/report",
        "https://équipe.example.test/report",
    ],
)
def test_guard_allows_text_and_slashes_inside_public_urls(text: str) -> None:
    policy.guard_text(text, "chunk dc42", _BASE)


@pytest.mark.parametrize("prefix", ["", " ", "\n", "\t", "(", "[", "<", '"', "'", "="])
@pytest.mark.parametrize("host", ["private.example.internal", "configured"])
def test_guard_checks_protocol_relative_urls_at_token_start(
    prefix: str, host: str
) -> None:
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_text(
            f"Read https://example.org/report {prefix}//{host}/report",
            "chunk dc42",
            "https://configured",
        )
    with pytest.raises(BadInput, match="dc42"):
        policy.guard_text(
            f"{prefix}//{host}/report", "chunk dc42", "https://configured"
        )


def test_private_origin_requires_exact_hostname() -> None:
    for url in (_BASE + ".evil.org", "https://other.precis.example.test"):
        assert not policy.private_url(url, _BASE)
        policy.guard_text(f"**{url}**", "chunk dc42", _BASE)


@pytest.mark.parametrize("backend", ["latex", "docx"])
@pytest.mark.parametrize("audience", ["dev", "external"])
@pytest.mark.parametrize("selector", ["1", "1..3", "p2"])
def test_record_selector_survives_legacy_cleanup(
    backend: str, audience: str, selector: str
) -> None:
    state = policy.Audience(_Records(), audience, _BASE)
    text = f"Evidence [gr13~{selector}]."
    if backend == "latex":
        ctx = latex._Ctx(
            keymap={}, known_handles=set(), store=state.store, audience=state
        )
        rendered = latex._render_inline(text, ctx)
    else:
        from docx import Document

        paragraph = Document().add_paragraph()
        context = docx._Ctx(store=state.store, known_handles=set(), audience=state)
        docx._render_inline(text, context, paragraph)
        rendered = _xml_text(paragraph._p)
        rendered += " ".join(
            str(rel.target_ref)
            for rel in paragraph.part.rels.values()
            if rel.is_external
        )
    assert "Evidence" in rendered
    if audience == "dev":
        assert f"{_BASE}/r/gripe/13" in rendered
    else:
        assert "S4.1" in rendered
        assert [entry.handle for entry in state.entries] == ["gr13"]


def test_native_footnotes_reuse_part_and_roundtrip(tmp_path: Path) -> None:
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    from precis.export._footnotes import add_footnote

    document = Document()
    paragraph = document.add_paragraph("Evidence")
    add_footnote(paragraph, "First description")
    add_footnote(paragraph, "Second description")
    assert sum(rel.reltype == RT.FOOTNOTES for rel in document.part.rels.values()) == 1
    target = tmp_path / "footnotes.docx"
    document.save(str(target))
    with ZipFile(target) as archive:
        notes = etree.fromstring(archive.read("word/footnotes.xml"))
        body = etree.fromstring(archive.read("word/document.xml"))
    assert notes.xpath("w:footnote/@w:id", namespaces=_W) == ["-1", "0", "1", "2"]
    assert body.xpath("//w:footnoteReference/@w:id", namespaces=_W) == ["1", "2"]
    assert "First description" in _xml_text(notes)
    assert "Second description" in _xml_text(notes)


def test_external_guard_also_checks_referenced_prose() -> None:
    class Chunks:
        def live_body_text_for_ref(self, ref_id: int) -> list[str]:
            return [f"Stored details at {_BASE}/record"]

    class Store(_Records):
        chunks = Chunks()

    state = policy.Audience(Store(), "external", _BASE)
    with pytest.raises(BadInput, match="gr13.*body.txt"):
        state.reference("gr13", None)


def test_external_guard_decodes_stored_json_strings() -> None:
    class Store(_Records):
        def get_ref(self, *, kind: str, id: Any) -> Any:
            ref = super().get_ref(kind=kind, id=id)
            ref.meta = {"note": _BASE + "\ncontinued"}
            return ref

    state = policy.Audience(Store(), "external", _BASE)
    with pytest.raises(BadInput, match="gr13.*record.json"):
        state.reference("gr13", None)


def test_external_guard_checks_fallback_author() -> None:
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: []))
    ref = SimpleNamespace(id=1, title="Report", authors=None, meta={"author": _BASE})
    with pytest.raises(BadInput, match="byline"):
        policy.guard_audience(store, ref, "external")


@pytest.mark.parametrize("backend", ["latex", "docx"])
def test_title_record_is_numbered_before_body(backend: str) -> None:
    state = policy.Audience(_Records(), "external", _BASE)
    if backend == "latex":
        ctx = latex._Ctx(
            keymap={}, known_handles=set(), store=state.store, audience=state
        )
        title = latex._render_document_title(
            SimpleNamespace(title="Results [gr1]", slug="report"), ctx
        )
        assert title is not None and r"\thanks{" in title
        body = latex._render_inline("See [gr2]", ctx)
        assert "S4.1" in title and "S4.2" in body
    else:
        from docx import Document

        ctx_docx = docx._Ctx(store=state.store, known_handles=set(), audience=state)
        document = Document()
        renderer = docx.DocxRenderer(ctx_docx, title="Results [gr1]")
        renderer.title(document, None)
        docx._render_inline("See [gr2]", ctx_docx, document.add_paragraph())
        assert "S4.1" in document.paragraphs[0].text
        assert "S4.2" in document.paragraphs[-1].text
    assert [entry.handle for entry in state.entries] == ["gr1", "gr2"]


def test_latex_table_numbers_header_before_rows() -> None:
    state = policy.Audience(_Records(), "external", _BASE)
    ctx = latex._Ctx(keymap={}, known_handles=set(), store=state.store, audience=state)
    chunk = SimpleNamespace(
        text="",
        meta={"table": {"header": ["[gr1]"], "rows": [["[gr2]"]]}},
    )
    lines = latex._render_table(chunk, ctx, "")
    assert [entry.handle for entry in state.entries] == ["gr1", "gr2"]
    assert "S4.1" in "\n".join(lines)


def test_final_guard_refuses_public_label_with_private_target(
    store: Store,
) -> None:
    ref, _ = store.drafts.create_draft(name="final-host-guard", title="Report")
    chunk = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"[https://doi.org/10.1234/example]({_BASE}/r/pathway/7)",
        at={"last": True},
    )[0]
    # The input tokenizer now catches the hidden target before rendering.
    with pytest.raises(BadInput) as exc:
        policy.guard_audience(store, ref, "external")
    assert chunk.dc in str(exc.value)


@pytest.mark.parametrize("artifact", ["latex", "docx-body", "docx-rels"])
def test_artifact_guard_directly_refuses_rendered_private_host(
    artifact: str, tmp_path: Path
) -> None:
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    chunk = SimpleNamespace(dc="dc42", text=f"Evidence at {_BASE}/record", meta={})
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: [chunk]))
    ref = SimpleNamespace(id=1)
    if artifact == "latex":
        path = tmp_path / "main.tex"
        path.write_text(r"\href{" + _BASE + r"/record}{Public label}", encoding="utf-8")
        location = "main.tex"
    else:
        path = tmp_path / "report.docx"
        document = Document()
        if artifact == "docx-body":
            document.add_paragraph(f"Evidence at {_BASE}/record")
            location = "report.docx:word/document.xml"
        else:
            document.add_paragraph("https://example.org/public")
            document.part.relate_to(_BASE + "/record", RT.HYPERLINK, is_external=True)
            location = "report.docx:word/_rels/document.xml.rels"
        document.save(str(path))
    with pytest.raises(BadInput) as exc:
        policy.guard_artifacts(store, ref, [path])
    assert location in str(exc.value)
    assert chunk.dc in str(exc.value)


def test_final_guard_refuses_private_bibliography_url(
    store: Store, tmp_path: Path
) -> None:
    paper = store.insert_ref(
        kind="paper",
        slug="private-bib",
        title="Reference",
        provider="manual",
        meta={"record_type": "software", "url": _BASE + "/private"},
    )
    ref, _ = store.drafts.create_draft(name="bibliography-host-guard", title="Report")
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"Source [pa{paper.id}].",
        at={"last": True},
    )
    with pytest.raises(BadInput) as exc:
        latex.export_draft(store, ref, target_dir=tmp_path, audience="external")
    assert "refs.bib" in str(exc.value)
    assert "bibliography key private-bib" in str(exc.value)


@pytest.mark.parametrize(
    ("filename", "member"),
    [
        ("main.tex", None),
        ("refs.bib", None),
        ("report.docx", "word/header1.xml"),
        ("report.docx", "word/_rels/document.xml.rels"),
        ("supplement.zip", "S1.1/extra.csv"),
    ],
)
def test_final_guard_scans_all_text_artifacts_and_host_boundaries(
    filename: str, member: str | None, tmp_path: Path
) -> None:
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: []))
    ref = SimpleNamespace(id=1)
    path = tmp_path / filename
    for value, blocked in [
        ("PRECIS.EXAMPLE.TEST/private", True),
        ("precis.example.test.evil.org/private", False),
        ("other.precis.example.test/private", False),
        ("precis.example.test.", True),
    ]:
        if member is not None:
            with ZipFile(path, "w") as archive:
                archive.writestr(member, value)
        else:
            path.write_text(value, encoding="utf-8")
        if blocked:
            with pytest.raises(BadInput, match=member or filename):
                policy.guard_artifacts(store, ref, [path])
        else:
            policy.guard_artifacts(store, ref, [path])


@pytest.mark.parametrize("base_url", [_BASE, ""], ids=["configured", "unconfigured"])
@pytest.mark.parametrize(
    ("filename", "member"),
    [
        ("main.tex", None),
        ("refs.bib", None),
        ("report.docx", "word/_rels/document.xml.rels"),
        ("report.docx", "word/extra.txt"),
        ("supplement.zip", "S1.1/extra.csv"),
    ],
)
@pytest.mark.parametrize(
    "text",
    [
        "[https://example.org/public](http://private.example.internal/report)",
        "http://private.example.internal/report",
    ],
    ids=["public-label", "private-url"],
)
def test_final_guard_checks_private_urls_in_every_text_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    base_url: str,
    filename: str,
    member: str | None,
    text: str,
) -> None:
    monkeypatch.setattr(policy, "web_base_url", lambda _store: base_url)
    chunk = SimpleNamespace(dc="dc42", text=text, meta={})
    store = SimpleNamespace(drafts=SimpleNamespace(reading_order=lambda _id: [chunk]))
    ref = SimpleNamespace(id=1)
    path = tmp_path / filename
    if member is None:
        path.write_text(text, encoding="utf-8")
    else:
        with ZipFile(path, "w") as archive:
            archive.writestr(member, text)
    with pytest.raises(BadInput) as exc:
        policy.guard_artifacts(store, ref, [path])
    message = str(exc.value)
    assert (f"{filename}:{member}" if member else filename) in message
    assert "dc42" in message
    assert "private.example.internal" not in message


@pytest.mark.parametrize("backend", ["latex", "docx"])
@pytest.mark.parametrize(
    "source", ["bodyless-paper", "paper-chunk", "paper-and-chunks"]
)
@pytest.mark.parametrize("location", ["body", "title"])
def test_paper_dataset_keeps_citation_and_gets_s3(
    backend: str, source: str, location: str, store: Store, tmp_path: Path
) -> None:
    dataset = store.insert_ref(
        kind="paper",
        slug="export-dataset",
        title="Stored measurements",
        provider="manual",
        meta={"record_type": "dataset", "url": "https://example.org/data"},
    )
    handle = f"pa{dataset.id}"
    handles = [handle]
    if source != "bodyless-paper":
        chunks = store.chunks.insert_chunks(
            dataset.id,
            [
                ChunkInsert(ord=0, text="Stored measurements", slug="data"),
                ChunkInsert(ord=1, text="More measurements", slug="more-data"),
            ],
        )
        chunk_handles = [f"pc{chunk.id}" for chunk in chunks]
        assert policy.record_handle(chunk_handles[0], store) == (
            "dataset",
            False,
            dataset.id,
        )
        if source == "paper-chunk":
            handle = chunk_handles[0]
            handles = [handle]
        else:
            handles.extend(chunk_handles)
    text = "Measurements " + " ".join(f"[{h}]" for h in handles)
    ref, _ = store.drafts.create_draft(
        name="dataset-supplement", title=text if location == "title" else "Report"
    )
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text=f"{text} and again [{handle}]." if location == "body" else "Results.",
        at={"last": True},
    )
    if backend == "latex":
        result = latex.export_draft(
            store, ref, target_dir=tmp_path, audience="external"
        )
        assert "export-dataset" in result.cited_slugs
        rendered = result.main_tex.read_text(encoding="utf-8")
        assert "\\cite{export-dataset}" in rendered
        assert "S3.1" in rendered
        assert "S3.2" not in rendered and "S3.3" not in rendered
        if location == "title":
            assert r"\thanks{" in rendered
    else:
        target = tmp_path / "report.docx"
        word = docx.export_docx(store, ref, target_path=target, audience="external")
        assert "export-dataset" in word.cited_slugs
        with ZipFile(target) as archive:
            document: etree._Element = etree.fromstring(
                archive.read("word/document.xml")
            )
            assert "S3.1" in _xml_text(document)
            assert "[1]" in _xml_text(document)
            assert "S3.2" not in _xml_text(document)
            assert "S3.3" not in _xml_text(document)
            notes = etree.fromstring(archive.read("word/footnotes.xml"))
            assert "Stored measurements" in _xml_text(notes)
            assert document.xpath("//w:footnoteReference", namespaces=_W)
    with ZipFile(tmp_path / "supplement.zip") as archive:
        index = json.loads(archive.read("index.json"))
        assert len(index) == 1
        assert index[0]["number"] == "S3.1"
        assert index[0]["kind"] == "dataset"
        assert index[0]["handle"] == handle
        entry = json.loads(archive.read("S3.1/entry.json"))
        if source != "bodyless-paper":
            assert entry["body"] == "body.txt"
            assert archive.read("S3.1/body.txt") == (
                b"Stored measurements\n\nMore measurements"
            )
        else:
            assert entry["body"] == "absent"
        assert json.loads(archive.read("S3.1/record.json"))["record_type"] == "dataset"
