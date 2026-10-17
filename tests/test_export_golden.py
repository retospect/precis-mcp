"""Byte-identical safety net for the export-parity walker
(docs/backlog/export-latex-docx-parity.md): one synthetic golden draft,
exported end to end through ``export_draft`` (plain, patent, reMarkable
footnote mode) and ``export_docx`` (plain, EndNote, patent), compared with
the checked-in baseline under ``tests/fixtures/export/golden/``.

DB-free: a fake store answers every lookup, so ids, handles and numbering
are fixed. The draft covers SI resolution, alias canonicalisation, adjacent
run merge with dedupe, same-paper collapse, multi-key finding cites, a
dropped cite, patent / footnote / EndNote modes, nested abbreviation long
forms and every ``BACK_MATTER_ORDER`` section that applies to each backend.

A diff here means an export changed. If the change is intended, regenerate
with ``PRECIS_UPDATE_EXPORT_GOLDEN=1`` set in the test process (add it to
``scripts/test``'s forwarded ``ARTIFACT_ENV`` list, as the kinetics golden
does) and list the change in the commit body.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import struct
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("docx")

from lxml import etree

from precis.export import docx as dx
from precis.export import latex
from precis.store.types import ResolvedHandle

_GOLDEN = Path(__file__).parent / "fixtures" / "export" / "golden"
_UPDATE = bool(os.environ.get("PRECIS_UPDATE_EXPORT_GOLDEN"))


def _png_1x1() -> bytes:
    """A 1x1 RGBA PNG, built at runtime (byte-stable: level-9 deflate of
    one Sub-filtered pixel row)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(tag + data))
        return struct.pack(">I", len(data)) + tag + data + crc

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes([1, 0, 0, 255, 127]), 9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


_PNG = _png_1x1()

_ABBREVS = {"MOF": "metal-organic framework", "FMOF": "functionalized MOF"}


def _paper(rid: int, slug: str, *, title: str, year: int, **kw: Any) -> Any:
    return SimpleNamespace(
        id=rid,
        slug=slug,
        kind=kw.pop("kind", "paper"),
        title=title,
        year=year,
        authors=[{"name": f"Author{rid}, A."}, {"name": f"Second{rid}, B."}],
        meta={"venue": "J. Golden", "volume": str(rid), "pages": f"{rid}-{rid + 9}"},
        pdf_role=kw.pop("pdf_role", None),
        **kw,
    )


_SMITH = _paper(1, "smith20", title="Amine uptake in MOFs", year=2020)
_JONES = _paper(2, "jones21", title="Arxiv-only preprint", year=2021)
_LEE = _paper(3, "lee22", title="No identifier paper", year=2022)
_PARENT = _paper(4, "parent24", title="Parent paper", year=2024)
_SI = _paper(5, "parent24si", title="Supplement", year=2024, pdf_role="supplement")
_PATENT = SimpleNamespace(
    id=6,
    slug="us123",
    kind="patent",
    title="Capture device",
    year=2019,
    authors=None,
    meta={"number": "US 1,234,567 B2", "assignee": "Acme"},
    pdf_role=None,
)

_REFS: dict[tuple[str, str], Any] = {
    ("paper", "smith20"): _SMITH,
    ("paper", "jones21"): _JONES,
    ("paper", "lee22"): _LEE,
    ("paper", "parent24"): _PARENT,
    ("paper", "parent24si"): _SI,
    ("paper", "alias4"): _PARENT,  # an alias cite key of parent24
    ("patent", "us123"): _PATENT,
}

_HANDLES = {
    "pc10": ResolvedHandle(ref_id=1, kind="paper", public_id="smith20", chunk_id=10),
    "pa2": ResolvedHandle(ref_id=2, kind="paper", public_id="jones21"),
    "pc50": ResolvedHandle(
        ref_id=5,
        kind="paper",
        public_id="parent24si",
        chunk_id=50,
        cite_public_id="parent24",
    ),
    "pt7": ResolvedHandle(ref_id=6, kind="patent", public_id="us123"),
}

#: finding pk -> cite keys the (monkeypatched) shared resolver returns.
_FINDING_KEYS = {
    7: ["smith20"],
    8: ["jones21", "lee22"],
    9: ["lee22", "parent24si"],
    10: [],
}


def _chunk(cid: int, kind: str, depth: int, text: str, **kw: Any) -> Any:
    return SimpleNamespace(
        chunk_id=cid,
        dc=f"dc{cid}",
        handle=f"h{cid}",
        chunk_kind=kind,
        depth=depth,
        parent_chunk_id=kw.get("parent"),
        text=text,
        meta=kw.get("meta"),
    )


_SNAPSHOT = {
    "schema": 1,
    "source": {"kind": "pathway", "handle": "pw3", "title": "Golden pathway"},
    "generated_at": "2026-10-01T00:00:00Z",
    "precis": {"version": "1.0", "sha": "abc123"},
    "params": {"T": 298.15, "mlip": {"backend": "mace"}},
    "columns": ["step", "dG"],
    "rows": [{"step": "A", "dG": 0.12}, {"step": "B", "dG": -0.4}],
}

_CHUNKS = [
    _chunk(1, "heading", 0, "Golden Draft"),
    _chunk(2, "heading", 1, "Introduction", parent=1),
    _chunk(
        3,
        "paragraph",
        2,
        "A metal-organic framework (MOF) hosts a functionalized MOF (FMOF); "
        "later MOF and FMOF uses. Plain [§smith20] and run "
        "[§smith20~3][§jones21][§lee22] end.",
        parent=2,
    ),
    _chunk(
        4,
        "paragraph",
        2,
        "Collapse [§smith20~1] [§smith20~2] and [§smith20~1][§smith20~4] then "
        "[§jones21][§smith20][§jones21] stop.",
        parent=2,
    ),
    _chunk(
        5,
        "paragraph",
        2,
        "Handles [pc10][pa2], SI [pc50] and [§parent24si], alias [§alias4], "
        "then [§parent24][§parent24si][§parent24si][§lee22].",
        parent=2,
    ),
    _chunk(6, "heading", 1, "Claims about FMOF [§lee22]", parent=1),
    _chunk(
        7,
        "paragraph",
        2,
        "Finding [fi7] hub [fi8] mixed [fi9] repeat [fi7][fi7]; dropped [fi10].",
        parent=6,
    ),
    _chunk(
        8,
        "paragraph",
        2,
        "Prose paper:lee22 and doi:10.1/x and [st12] and [§nobody99] and "
        "patent [pt7]. See [dc2] and [dc11].",
        parent=6,
    ),
    _chunk(
        9,
        "figure",
        2,
        "A MOF figure [§jones21].",
        parent=6,
        meta={"figure": {"data_package": _SNAPSHOT}},
    ),
    _chunk(11, "paragraph", 2, "$$E = mc^2$$", parent=6),
    _chunk(
        14,
        "paragraph",
        2,
        "[dc9] opens a sentence. Runs [dc2] [dc4] merge, [the intro](dc2) keeps "
        "its text, a dead [dc999] and a legacy [¶h4] resolve, a run-in "
        "[dc16] names itself, the end matter [dc17] too, and [dc11]. "
        "[dc11] starts one.",
        parent=6,
    ),
    _chunk(15, "heading", 2, "Deep part", parent=6),
    _chunk(16, "heading", 4, "Run-in heading", parent=15),
    _chunk(12, "heading", 1, "Glossary", parent=1),
    _chunk(13, "term", 2, "metal-organic framework", parent=12, meta={"short": "MOF"}),
    _chunk(17, "heading", 0, "Acknowledgements"),
    _chunk(18, "paragraph", 1, "Thanks to all; see [dc2].", parent=17),
]


class _Row:
    state = "published"
    trusty_uri = "https://w3id.org/np/RAgolden"
    approved_title = "MOFs capture CO2."
    published_at = datetime(2026, 1, 2, tzinfo=UTC)
    artifact_id = None


class _Drafts:
    def reading_order(self, _ref_id: int) -> list[Any]:
        return list(_CHUNKS)

    def defined_abbrevs(self, _ref_id: int) -> dict[str, str]:
        return dict(_ABBREVS)

    def universal_chunk(self, handle: str) -> dict[str, str] | None:
        return {"text": f"Passage of {handle}."} if handle.startswith("pc") else None

    def chunk_text_at(self, ref_id: int, ordinal: int) -> str:
        return f"Chunk {ordinal} of paper {ref_id}."


class _Chunks:
    def chunk_text_by_id(self, chunk_id: int) -> str:
        return f"Cited passage {chunk_id}."


class _GoldenStore:
    def __init__(self) -> None:
        self.drafts = _Drafts()
        self.chunks = _Chunks()
        self.pool = SimpleNamespace(
            connection=lambda: contextlib.nullcontext(_NullConn())
        )

    def get_ref(self, *, kind: str, id: Any) -> Any:
        return _REFS.get((kind, id))

    def identifiers_for_refs(self, ids: list[int]) -> dict[int, dict[str, str]]:
        known = {1: {"doi": "10.1000/golden.1"}, 2: {"arxiv": "2101.00002"}}
        return {i: known[i] for i in ids if i in known}

    def resolve_handle(self, handle: str) -> Any:
        return _HANDLES.get(handle)

    def fetch_refs_by_ids(self, ids: list[int]) -> dict[int, Any]:
        return {i: SimpleNamespace(id=i, title=f"Claim {i}", meta={}) for i in ids}

    def tags_for(self, _ref_id: int) -> list[str]:
        return []

    def nanopub_publish_row(self, fid: int) -> Any:
        return _Row() if fid == 7 else None


class _NullConn:
    def execute(self, *_a: Any, **_k: Any) -> _NullConn:
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


_REF = SimpleNamespace(
    id=900,
    kind="draft",
    slug="golden",
    title="Golden Draft",
    meta={},
    authors=[{"name": "Ada Lovelace"}],
)


@pytest.fixture
def golden_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The fake store (typed ``Any``: it stands in for ``Store``)."""
    from precis.export import sources
    from precis.render import figure as render_figure
    from precis.store import si_links
    from precis.taproot import cite as taproot_cite
    from precis.utils import figure_source

    monkeypatch.setattr(
        si_links,
        "supplement_parent",
        lambda _conn, rid: (4, "parent24") if rid == 5 else None,
    )
    monkeypatch.setattr(
        taproot_cite,
        "finding_cite_keys",
        lambda _store, pk: SimpleNamespace(
            cite_keys=list(_FINDING_KEYS.get(pk, [])), is_hub=False, evidence=None
        ),
    )
    monkeypatch.setattr(latex, "_conjunct_cite_keys", lambda _s, _pk: [])
    monkeypatch.setattr(dx, "_conjunct_cite_keys", lambda _s, _pk: [])
    monkeypatch.setattr(render_figure, "ensure_rendered", lambda _s, _c: None)
    monkeypatch.setattr(
        figure_source, "figure_export_asset", lambda _s, _c: (_PNG, "png")
    )
    monkeypatch.setattr(
        sources,
        "collect_cited_sources",
        lambda _s, _r, cited_slugs: SimpleNamespace(
            present=[],
            missing=[
                SimpleNamespace(slug=s, kind="paper", reason="no pdf")
                for s in cited_slugs
            ],
        ),
    )
    return _GoldenStore()


def _check(name: str, actual: str) -> None:
    path = _GOLDEN / f"{name}.txt"
    if _UPDATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    assert path.is_file(), f"no baseline {path}; set PRECIS_UPDATE_EXPORT_GOLDEN=1"
    expected = path.read_text(encoding="utf-8")
    if actual != expected:
        import difflib

        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                f"golden/{name}.txt",
                "actual",
                n=2,
            )
        )
        pytest.fail(f"export {name} changed:\n{diff[:6000]}")


def _meta_block(cited: list[str], warnings: list[str]) -> str:
    return (
        "== cited ==\n"
        + "\n".join(cited)
        + "\n== warnings ==\n"
        + "\n".join(warnings)
        + "\n"
    )


_LATEX_MODES: dict[str, dict[str, Any]] = {
    "latex_plain": {},
    "latex_patent": {"doc_type": "patent"},
    "latex_remarkable": {"remarkable": True},
    "latex_nolinks": {"doi_links": False, "library_links": False},
    "latex_sources": {
        "include_sources": True,
        "retraction_override": [
            SimpleNamespace(title="Retracted work", slug="lee22", label="retracted")
        ],
    },
}


@pytest.mark.parametrize("name", sorted(_LATEX_MODES))
def test_latex_export_matches_golden(
    name: str, golden_env: Any, tmp_path: Path
) -> None:
    res = latex.export_draft(
        golden_env, _REF, target_dir=tmp_path, **_LATEX_MODES[name]
    )
    out = (
        "== main.tex ==\n"
        + res.main_tex.read_text(encoding="utf-8")
        + "== refs.bib ==\n"
        + res.bib.read_text(encoding="utf-8")
        + _meta_block(res.cited_slugs, res.warnings)
    )
    _check(name, out)


_DOCX_MODES: dict[str, dict[str, Any]] = {
    "docx_plain": {},
    "docx_endnote": {"citations": "endnote"},
    "docx_patent": {"doc_type": "patent"},
}

#: Parts left out of the comparison: core.xml carries save timestamps.
_DOCX_SKIP = frozenset({"docProps/core.xml"})
#: Parts the exporter writes per draft, kept as canonical XML text; every
#: other part (template styles, theme, fonts) is compared by its c14n hash.
_DOCX_TEXT = frozenset(
    {"word/document.xml", "word/settings.xml", "word/_rels/document.xml.rels"}
)


def _canonical_docx(path: Path) -> str:
    today = datetime.now(UTC)
    date_line = f"{today:%B} {today.day}, {today.year}"
    blocks: list[str] = []
    with zipfile.ZipFile(path) as z:
        for part in sorted(z.namelist()):
            if part in _DOCX_SKIP:
                continue
            data = z.read(part)
            if part.endswith((".xml", ".rels")):
                data = etree.tostring(etree.fromstring(data), method="c14n")
            if part in _DOCX_TEXT:
                text = data.decode("utf-8").replace(date_line, "<DATE>")
                text = text.replace("><", ">\n<")
            else:
                text = f"sha256 {hashlib.sha256(data).hexdigest()}"
            blocks.append(f"== {part} ==\n{text}\n")
    return "".join(blocks)


@pytest.mark.parametrize("name", sorted(_DOCX_MODES))
def test_docx_export_matches_golden(name: str, golden_env: Any, tmp_path: Path) -> None:
    out_path = tmp_path / "golden.docx"
    res = dx.export_docx(golden_env, _REF, target_path=out_path, **_DOCX_MODES[name])
    _check(name, _canonical_docx(out_path) + _meta_block(res.cited_slugs, res.warnings))
