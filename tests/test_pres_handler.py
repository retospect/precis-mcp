"""PresentationHandler attribution, citation export and soft deletion.

Covers the ``edit`` verb (writes attribution into ``meta``), the
``view='bibtex'`` / ``'ris'`` render it feeds, and the two pure helpers
(``_normalize_pres_authors`` / ``_format_pres_citation``). The shared
get/search/put machinery is exercised by the ingest tests; here we only
assert attribution and the reversible deletion surface.

DB-bound tests use the ``store`` fixture and skip when no postgres is
reachable (see ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.presentation import (
    PresentationHandler,
    _format_pres_citation,
    _normalize_pres_authors,
)
from precis.runtime import PrecisRuntime
from precis.store import Store

# ---------------------------------------------------------------------------
# Pure unit — author normalisation
# ---------------------------------------------------------------------------


class TestNormalizeAuthors:
    def test_newline_separated(self) -> None:
        assert _normalize_pres_authors("Payne, M. C.\nClark, S. J.") == [
            "Payne, M. C.",
            "Clark, S. J.",
        ]

    def test_semicolon_separated(self) -> None:
        assert _normalize_pres_authors("A; B ;C") == ["A", "B", "C"]

    def test_comma_within_name_not_split(self) -> None:
        # BibTeX "Family, Given" must stay whole — we never split on comma.
        assert _normalize_pres_authors("Payne, M. C.") == ["Payne, M. C."]

    def test_list_of_dicts_and_strings(self) -> None:
        assert _normalize_pres_authors(
            [{"family": "Payne", "given": "M. C."}, {"name": "S. Clark"}, "Z"]
        ) == ["M. C. Payne", "S. Clark", "Z"]

    def test_none_and_blank(self) -> None:
        assert _normalize_pres_authors(None) == []
        assert _normalize_pres_authors("\n ; \n") == []


# ---------------------------------------------------------------------------
# Pure unit — citation formatter
# ---------------------------------------------------------------------------


def _ref(**meta: object) -> SimpleNamespace:
    return SimpleNamespace(
        slug=meta.pop("slug", "deck1"),
        title=meta.pop("title", "Lecture 1"),
        meta=dict(meta),
    )


class TestFormatCitation:
    def test_bibtex_misc_default(self) -> None:
        out = _format_pres_citation(
            _ref(authors=["Payne, M. C."], venue="CASTEP Workshop", date="2001-12-06"),
            style="bibtex",
        )
        assert out.startswith("@misc{deck1,")
        assert "title = {Lecture 1}," in out
        assert "author = {Payne, M. C.}," in out
        assert "year = {2001}," in out
        assert "howpublished = {CASTEP Workshop}," in out

    def test_multiple_authors_joined_with_and(self) -> None:
        out = _format_pres_citation(
            _ref(authors=["Payne, M. C.", "Clark, S. J."]), style="bibtex"
        )
        assert "author = {Payne, M. C. and Clark, S. J.}," in out

    def test_bibtex_type_override(self) -> None:
        out = _format_pres_citation(_ref(bibtex_type="unpublished"), style="bibtex")
        assert out.startswith("@unpublished{deck1,")

    def test_latex_escape_in_title(self) -> None:
        out = _format_pres_citation(_ref(title="Bonds & Bands"), style="bibtex")
        assert r"Bonds \& Bands" in out

    def test_empty_attribution_still_valid_stub(self) -> None:
        out = _format_pres_citation(_ref(), style="bibtex")
        assert out.startswith("@misc{deck1,")
        assert "title = {Lecture 1}," in out

    def test_ris(self) -> None:
        out = _format_pres_citation(
            _ref(authors=["X"], date="2001", venue="V"), style="ris"
        )
        assert "TY  - SLIDE" in out
        assert "AU  - X" in out
        assert "PY  - 2001" in out
        assert "ER  - " in out


# ---------------------------------------------------------------------------
# Spec surface
# ---------------------------------------------------------------------------


def test_spec_supports_edit_and_lists_citation_views(hub: Hub) -> None:
    assert PresentationHandler.spec.supports_edit is True
    assert PresentationHandler.spec.supports_delete is True
    views = PresentationHandler(hub=hub).accepted_views()
    assert "bibtex" in views and "ris" in views


# ---------------------------------------------------------------------------
# DB-bound — edit round-trip
# ---------------------------------------------------------------------------


def _mk_deck(handler: PresentationHandler, slug: str = "2001-nuts-and-bolts") -> str:
    handler.put(
        id=slug,
        text="Timescales and lengthscales.",
        pos=0,
        subtype="slides",
        title="lecture01",
    )
    return slug


class TestEditRoundTrip:
    def test_edit_persists_and_feeds_bibtex(self, store) -> None:
        h = PresentationHandler(hub=Hub(store=store))
        slug = _mk_deck(h)

        resp = h.edit(
            id=slug,
            title="Nuts and Bolts — Lecture 1",
            authors="Payne, M. C.\nClark, S. J.",
            venue="CASTEP Workshop, Durham",
            date="2001-12-06",
        )
        assert "updated" in resp.body

        # Overview surfaces the authors.
        overview = h.get(id=slug)
        assert "Payne, M. C." in overview.body

        # BibTeX view renders from the persisted meta.
        bib = h.get(id=slug, view="bibtex").body
        assert bib.startswith("@misc{")
        assert "author = {Payne, M. C. and Clark, S. J.}," in bib
        assert "year = {2001}," in bib
        assert "Nuts and Bolts" in bib

    def test_edit_leaves_slides_untouched(self, store) -> None:
        h = PresentationHandler(hub=Hub(store=store))
        slug = _mk_deck(h)
        h.edit(id=slug, venue="Somewhere")
        # The single ingested slide body is unchanged (metadata-only edit).
        assert "Timescales and lengthscales." in h.get(id=f"{slug}/full").body

    def test_edit_requires_at_least_one_field(self, store) -> None:
        h = PresentationHandler(hub=Hub(store=store))
        slug = _mk_deck(h)
        with pytest.raises(BadInput):
            h.edit(id=slug)


@pytest.mark.parametrize("address", ["slug", "numeric", "handle"])
def test_delete_retires_and_journals_without_removing_slides(
    runtime_with_store: PrecisRuntime, store: Store, address: str
) -> None:
    handler = PresentationHandler(hub=Hub(store=store))
    slug = _mk_deck(handler)
    ref = store.get_ref(kind="pres", id=slug)
    assert ref is not None
    blocks = store.chunks.list_chunks_for_ref(ref.id)
    addresses: dict[str, str | int] = {
        "slug": slug,
        "numeric": ref.id,
        "handle": f"pr{ref.id}",
    }

    body, is_error = runtime_with_store.dispatch_with_status(
        "delete", {"kind": "pres", "id": addresses[address]}
    )

    assert not is_error, body
    assert "deleted" in body
    assert store.get_ref(kind="pres", id=slug) is None
    retired = store.fetch_refs_by_ids([ref.id], include_deleted=True)[ref.id]
    assert retired.retired_at is not None
    events = store.events_for(ref.id, source="db", event="retired")
    assert len(events) == 1
    assert events[0].payload["kind"] == "pres"
    assert store.chunks.list_chunks_for_ref(ref.id) == blocks


def test_delete_preserves_pdf_attachment_and_file(store: Store, tmp_path: Path) -> None:
    handler = PresentationHandler(hub=Hub(store=store))
    slug = _mk_deck(handler)
    ref = store.get_ref(kind="pres", id=slug)
    assert ref is not None
    pdf_path = tmp_path / "deck.pdf"
    pdf_bytes = b"%PDF-1.4\nsoft-delete preservation fixture\n"
    pdf_path.write_bytes(pdf_bytes)
    sha = "a" * 64
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO pdfs (pdf_sha256, content_hash, page_count, "
            "size_bytes, storage_path) VALUES (%s, %s, 1, %s, %s)",
            (sha, sha, len(pdf_bytes), str(pdf_path)),
        )
        conn.execute("UPDATE refs SET pdf_sha256 = %s WHERE ref_id = %s", (sha, ref.id))

    handler.delete(id=slug)

    retired = store.fetch_refs_by_ids([ref.id], include_deleted=True)[ref.id]
    assert retired.pdf_sha256 == sha
    assert store.pdf_storage_path(sha) == str(pdf_path)
    assert pdf_path.read_bytes() == pdf_bytes


def test_delete_numeric_id_does_not_resolve_a_competing_slug(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    handler = PresentationHandler(hub=Hub(store=store))
    slug = _mk_deck(handler)
    target = store.get_ref(kind="pres", id=slug)
    assert target is not None
    competing_slug = _mk_deck(handler, slug=str(target.id))
    other = store.get_ref(kind="pres", id=competing_slug)
    assert other is not None and other.id != target.id

    body, is_error = runtime_with_store.dispatch_with_status(
        "delete", {"kind": "pres", "id": target.id}
    )

    assert not is_error, body
    assert store.get_ref(kind="pres", id=target.id) is None
    assert store.get_ref(kind="pres", id=other.id) is not None
    assert store.events_for(other.id, event="retired") == []


@pytest.mark.parametrize("selector", ["~0", "/full"])
def test_delete_rejects_block_or_view_selector(store: Store, selector: str) -> None:
    handler = PresentationHandler(hub=Hub(store=store))
    slug = _mk_deck(handler)

    with pytest.raises(BadInput, match="ref level"):
        handler.delete(id=f"{slug}{selector}")

    assert store.get_ref(kind="pres", id=slug) is not None


def test_delete_rejects_other_kind(store: Store) -> None:
    other = store.insert_ref(kind="memory", slug=None, title="Keep this thought")
    handler = PresentationHandler(hub=Hub(store=store))

    with pytest.raises(NotFound):
        handler.delete(id=other.id)

    assert store.get_ref(kind="memory", id=other.id) is not None
    assert store.events_for(other.id, event="retired") == []


def test_meta_view_dumps_ref_meta(store) -> None:
    """gr563572: callers guessed ``view='meta'`` for the ingest-written
    meta keys (``source_pdf`` & co.) the overview doesn't surface."""
    h = PresentationHandler(hub=Hub(store=store))
    slug = _mk_deck(h)
    h.edit(id=slug, venue="CASTEP Workshop, Durham", date="2001-12-06")

    out = h.get(id=slug, view="meta")

    assert "CASTEP Workshop, Durham" in out.body
    assert '"venue"' in out.body


def test_unknown_view_lists_accepted_options(store) -> None:
    """gr563572: the refusal names every accepted view — ``pdf_link``,
    ``tags`` and ``meta`` included — not a bare 'try X/Y/Z'."""
    from precis.errors import Unsupported

    h = PresentationHandler(hub=Hub(store=store))
    slug = _mk_deck(h)

    with pytest.raises(Unsupported) as exc:
        h.get(id=slug, view="frobnicate")

    options = exc.value.options or []
    for view in ("pdf_link", "tags", "meta", "full", "bibtex"):
        assert view in options
    assert "view=" in str(exc.value.next)


def test_delete_rejects_missing_or_already_retired_pres(store: Store) -> None:
    handler = PresentationHandler(hub=Hub(store=store))
    with pytest.raises(NotFound):
        handler.delete(id="absent-presentation")
    slug = _mk_deck(handler)
    ref = store.get_ref(kind="pres", id=slug)
    assert ref is not None
    handler.delete(id=slug)

    with pytest.raises(NotFound):
        handler.delete(id=slug)

    assert len(store.events_for(ref.id, source="db", event="retired")) == 1
