"""``DatasheetHandler`` — component datasheet kind (ADR 0042 §7).

A ``datasheet`` is an *evidence-role* sibling of
:class:`~precis.handlers.paper.PaperHandler` — an ingested PDF read through
the **identical** Marker → chunks pipeline papers use, so it gets embeddings,
per-chunk keywords, TOC, and in-document search for free. The differences
from a paper are declared, not duplicated:

* ``corpus_role='evidence'`` — a datasheet *is* citable (unlike a ``cfp``),
  but it lives in its own kind so component datasheets never pollute academic
  ``search(kind='paper')`` and vice-versa (ADR 0042 §7.1).
* **One kind for the whole electronics-doc family** — app-notes / errata /
  reference-manuals ride along via a ``meta`` sub-type; we do **not** mint a
  new kind per genre.
* No ``put`` here yet — a datasheet is acquired by *lazy ingest* from a
  part's ``datasheet_url`` (Slice 3); ``supports_put=False`` ⇒ the base
  handler raises ``Unsupported``.

Everything else (``get`` overview + chunk rendering, ``search`` /
``search_hits``, ``edit`` metadata repair, ``tag`` / ``link``) is the paper
machinery verbatim, parameterised on ``self.spec.kind``. Link a datasheet to
the ``part`` it documents with ``link(rel='datasheet-of')`` — the relation is
seeded by migration 0054. It is read at ``/datasheets/<slug>`` in the same
two-pane reader as a paper (``precis_web.routes.datasheets``), and ingested by
dropping a PDF into ``<inbox>/datasheets/`` (``precis watch`` routes it through
the paper pipeline with ``as_kind='datasheet'``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.protocol import KindSpec
from precis.response import Response

# Reader views a datasheet supports — paper's enum minus the bibliographic
# citation-export formats (a datasheet is cited as a part's evidence, not a
# bibliography entry).
_DATASHEET_VIEWS: tuple[str, ...] = (
    "abstract",
    "toc",
    "health",
    "log",
    "abbrevs",
)

# The electronics-doc sub-genres a datasheet's ``meta.subtype`` may name (one
# kind for the whole family — ADR 0042 §7). ``datasheet`` is the default.
# Labels for humans/export live in ``export.latex._DATASHEET_SUBTYPE_LABELS``.
_DATASHEET_SUBTYPES: frozenset[str] = frozenset(
    {"datasheet", "app-note", "errata", "reference-manual"}
)

# Bibliographic fields handled by the inherited PaperHandler.edit — forwarded
# there verbatim; the datasheet-specific meta fields are handled here.
_PAPER_EDIT_FIELDS: tuple[str, ...] = (
    "title",
    "year",
    "authors",
    "abstract",
    "doi",
    "arxiv",
)


class DatasheetHandler(PaperHandler):
    """Read-only component datasheet (evidence role)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="datasheet",
        title="Datasheet",
        description=(
            "Component datasheet (ADR 0042 §7) — a read-only ingested PDF read "
            "in the same two-pane reader as a paper (embeddings / keywords / "
            "TOC / in-doc search), addressable by slug. Evidence role: citable "
            "as a part's source, but scoped out of academic paper search. One "
            "kind for the whole electronics-doc family (app-note / errata via a "
            "meta sub-type). Link it to a part with link(rel='datasheet-of'). "
            "Use get(view='toc') to read the pinout / sections. "
            "See precis-datasheet-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=False,
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        corpus_role="evidence",
        views=_DATASHEET_VIEWS,
    )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return list(_DATASHEET_VIEWS)

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int,
        vendor: str | None = None,
        subtype: str | None = None,
        part_lcsc: str | None = None,
        dry_run: bool | str | None = None,
        **kw: Any,
    ) -> Response:
        """Edit a datasheet's metadata.

        Extends the inherited paper editor with three datasheet-specific
        ``meta`` fields — ``vendor`` (manufacturer), ``subtype`` (one of
        :data:`_DATASHEET_SUBTYPES`), and ``part_lcsc`` (the LCSC C-number of
        the documented part) — which flow into the bibliography + docx
        reference line (``export.latex.build_bib`` / ``export.docx``). A
        blank string clears a field; an unrecognised ``subtype`` is rejected.
        Any bibliographic field (``title`` / ``year`` / ``authors`` / …) is
        forwarded to the paper editor unchanged.

        NB the ``part_lcsc`` is stored on the datasheet's ``meta`` rather than
        as a ``datasheet-of`` graph edge: ``part`` is a catalog-table kind, not
        a ``refs`` row, so it can't yet be a link target. Promote to the seeded
        relation once parts become ref-backed.
        """
        meta_patch = self._datasheet_meta_patch(vendor, subtype, part_lcsc)
        bib = {k: kw[k] for k in _PAPER_EDIT_FIELDS if kw.get(k) not in (None, "")}

        if meta_patch:
            ref_id = self._resolve_paper_ref_id(id)
            self.store.update_paper_fields(ref_id, meta_patch=meta_patch, source="edit")
        if bib:
            # The paper editor rebuilds search cards + returns its own summary;
            # let it own the response when bibliographic fields also changed.
            return super().edit(id=id, dry_run=dry_run, **bib)  # type: ignore[misc]
        if not meta_patch:
            raise BadInput(
                "edit(kind='datasheet') needs at least one field to change",
                next="edit(kind='datasheet', id=<slug>, vendor='Espressif')",
            )
        return Response(body=f"updated datasheet {id}: {', '.join(meta_patch)}.")

    @staticmethod
    def _datasheet_meta_patch(
        vendor: str | None, subtype: str | None, part_lcsc: str | None
    ) -> dict[str, Any]:
        """Validate + normalise the datasheet-specific meta fields into a
        ``meta`` patch. A passed-but-blank value clears the field (stored as
        ``""``); an omitted (``None``) value is left untouched."""
        patch: dict[str, Any] = {}
        if vendor is not None:
            patch["vendor"] = vendor.strip()
        if subtype is not None:
            sub = subtype.strip()
            if sub and sub not in _DATASHEET_SUBTYPES:
                raise BadInput(
                    f"unknown datasheet subtype {sub!r}; "
                    f"expected one of {sorted(_DATASHEET_SUBTYPES)}",
                    next="edit(kind='datasheet', id=<slug>, subtype='app-note')",
                )
            patch["subtype"] = sub
        if part_lcsc is not None:
            patch["part_lcsc"] = part_lcsc.strip().upper()
        return patch


__all__ = ["DatasheetHandler"]
