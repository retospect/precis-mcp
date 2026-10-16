"""``DatasheetHandler`` — component datasheet kind.

A ``datasheet`` is an *evidence-role* sibling of
:class:`~precis.handlers.paper.PaperHandler` — an ingested PDF read through
the **identical** Marker → chunks pipeline papers use, so it gets embeddings,
per-chunk keywords, TOC, and in-document search for free. The differences
from a paper are declared, not duplicated:

* ``corpus_role='evidence'`` — a datasheet *is* citable (unlike a ``cfp``),
  but it lives in its own kind so component datasheets never pollute academic
  ``search(kind='paper')`` and vice-versa.
* **One kind for the whole electronics-doc family** — app-notes / errata /
  reference-manuals ride along via a ``meta`` sub-type; we do **not** mint a
  new kind per genre.
* ``put(args={'url', 'part'})`` queues a ``datasheet_pull`` for an
  agent-found PDF URL (the pull job downloads through ``safe_stream``);
  ``get(kind='part')`` queues the catalog's own URL ahead of any design.
  A local-path put is deliberately absent: the pull runs on a worker that
  cannot see an agent's sandbox, and ingesting inline would run Marker in
  the request.

Everything else (``get`` overview + chunk rendering, ``search`` /
``search_hits``, ``edit`` metadata repair, ``tag`` / ``link``) is the paper
machinery verbatim, parameterised on ``self.spec.kind``. Link a datasheet to
the ``part`` it documents with ``link(rel='datasheet-of')`` — the relation is
seeded by migration 0054. It is read at ``/datasheets/<slug>`` in the same
two-pane reader as a paper (``precis_web.routes.datasheets``), and ingested by
dropping a PDF into ``<inbox>/datasheets/`` (``precis ingest --watch`` routes it through
the paper pipeline with ``as_kind='datasheet'``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.paper import PaperHandler
from precis.pcb import jlc_library
from precis.pcb._http import VendorError
from precis.pcb.datasheets import enqueue_one
from precis.protocol import KindSpec
from precis.response import Response
from precis.utils.edit_resolve import normalize_dry_run
from precis.utils.link_tag_ops import apply_link_ops
from precis.utils.safe_fetch import SsrfBlocked, assert_public_http_url

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
# kind for the whole family — the PCB netlist+placement IR). ``datasheet`` is the default.
# Labels for humans/export live in ``export.latex.DATASHEET_SUBTYPE_LABELS``.
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
    "journal",
    "entry_type",
)


class DatasheetHandler(PaperHandler):
    """Read-only component datasheet (evidence role)."""

    # A datasheet isn't acquired via `precis jobs ingest-bundles` (that's
    # the paper backlog) — it's lazily ingested from a part's
    # `datasheet_url` or dropped into `<inbox>/datasheets/`; the
    # empty-listing hint says so instead of leaking paper-pipeline
    # phrasing (gr311347 #12).
    _empty_corpus_message: ClassVar[str] = (
        "no datasheets ingested yet - link a part's `datasheet_url` or "
        "drop a PDF into `<inbox>/datasheets/` to add one"
    )

    spec: ClassVar[KindSpec] = KindSpec(
        kind="datasheet",
        title="Datasheet",
        description=(
            "Component datasheet — a read-only ingested PDF read "
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
        supports_put=True,
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        corpus_role="evidence",
        views=_DATASHEET_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        super().__init__(hub=hub)
        self.hub = hub

    def put(
        self,
        *,
        args: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Response:
        """Queue the fetch of a datasheet PDF for a part.

        ``put(kind='datasheet', args={'url': 'https://…/x.pdf', 'part':
        'C19191684'})``. ``part`` is an LCSC C-number, or a manufacturer part
        number matching exactly one catalog row. The URL is agent-supplied:
        it is checked against the SSRF guard here (private/loopback is
        refused at once) and fetched again, pinned, by the job, which also
        enforces ``%PDF`` magic and the 50 MB cap and records the outcome on
        ``get(kind='part', id=<C-number>)``.
        """
        extras = args or {}
        url = str(extras.get("url") or kw.get("url") or "").strip()
        part = str(extras.get("part") or kw.get("part") or "").strip()
        if extras.get("path") or kw.get("path"):
            raise BadInput(
                "put(kind='datasheet') takes a url, not a local path: the pull "
                "runs on a worker that cannot read your sandbox",
                next="host the PDF and pass args={'url': …, 'part': 'C…'}, or "
                "ask an operator to drop it in <inbox>/datasheets/",
            )
        if not url or not part:
            raise BadInput(
                "put(kind='datasheet') needs url= and part=",
                next="put(kind='datasheet', args={'url': 'https://…/x.pdf', "
                "'part': 'C19191684'})",
            )
        try:
            assert_public_http_url(url)
        except SsrfBlocked as exc:
            raise BadInput(f"url refused: {exc}") from exc
        except OSError as exc:  # DNS failure/timeout: not a verdict on the URL
            raise BadInput(f"url could not be resolved: {exc}") from exc
        lcsc = self._resolve_part(part)
        queued = enqueue_one(self.store, self.hub.sibling("job"), lcsc, url=url)
        note = "queued" if queued else "already queued for this url"
        return Response(
            body=f"datasheet pull {note} for part:{lcsc}. Outcome: "
            f"get(kind='part', id='{lcsc}') (datasheet line)."
        )

    def _resolve_part(self, part: str) -> str:
        from precis.pcb.catalog import lcsc_id

        lcsc = lcsc_id(part)
        if lcsc is not None:
            if self.store.part_row(lcsc) is None:
                # same catalog-miss fallback as get(kind='part'): the dump
                # lacks the Extended range; a hit is upserted by the lookup
                try:
                    found = jlc_library.live_lookup(self.store, lcsc) is not None
                except VendorError:
                    found = False
                if not found:
                    raise NotFound(
                        f"part {lcsc} not in the catalog",
                        next="check the C-number: search(kind='part', q='…')",
                    )
            return lcsc
        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT lcsc FROM parts WHERE lower(mfr_part) = lower(%s) "
                "ORDER BY lcsc LIMIT 3",
                (part,),
            ).fetchall()
        if len(rows) != 1:
            raise BadInput(
                f"part {part!r}: "
                + ("no catalog row has that mpn" if not rows else "several match")
                + "; pass the LCSC C-number",
                next="search(kind='part', q=<mpn>) for the C-number",
            )
        return str(rows[0][0])

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

        ``part_lcsc`` is dual-written: ``meta.part_lcsc`` (read by the
        nanopub evidence, docx and latex exporters) and a ``datasheet-of``
        edge to the part's lazy ref, minted on first use. Changing it moves
        the edge off the previous part; a blank clears both. A C-number not
        in the catalog keeps the meta write and skips the edge.

        ``dry_run=True`` previews the datasheet-specific ``meta`` patch (and,
        when bibliographic fields are also passed, the inherited paper
        preview) without writing either.
        """
        meta_patch = self._datasheet_meta_patch(vendor, subtype, part_lcsc)
        bib = {k: kw[k] for k in _PAPER_EDIT_FIELDS if kw.get(k) not in (None, "")}

        if not meta_patch and not bib:
            raise BadInput(
                "edit(kind='datasheet') needs at least one field to change",
                next="edit(kind='datasheet', id=<slug>, vendor='Espressif')",
            )

        if normalize_dry_run(dry_run):
            meta_preview: Response | None = None
            if meta_patch:
                ref_id = self._resolve_paper_ref_id(id)
                lines = "\n".join(f"  {k}: {v!r}" for k, v in meta_patch.items())
                meta_preview = Response(
                    body=(
                        f"DRY RUN (no write) — would update datasheet id={ref_id}:\n"
                        f"{lines}"
                    )
                )
            if bib:
                # The paper editor owns the bibliographic-field preview;
                # stitch our meta-only preview in front when there is one
                # so the caller sees both halves of the patch in one call.
                bib_preview = super().edit(id=id, dry_run=dry_run, **bib)
                if meta_preview is None:
                    return bib_preview
                return Response(body=f"{meta_preview.body}\n\n{bib_preview.body}")
            assert meta_preview is not None
            return meta_preview

        edge_note = ""
        if meta_patch:
            ref_id = self._resolve_paper_ref_id(id)
            if "part_lcsc" in meta_patch:
                edge_note = self._sync_datasheet_of(ref_id, meta_patch["part_lcsc"])
            self.store.update_paper_fields(ref_id, meta_patch=meta_patch, source="edit")
        if bib:
            # The paper editor rebuilds search cards + returns its own summary;
            # let it own the response when bibliographic fields also changed.
            resp = super().edit(id=id, dry_run=dry_run, **bib)
            return Response(body=resp.body + edge_note) if edge_note else resp
        return Response(
            body=f"updated datasheet {id}: {', '.join(meta_patch)}.{edge_note}"
        )

    def _sync_datasheet_of(self, ref_id: int, new_lcsc: str) -> str:
        """Mirror ``meta.part_lcsc`` onto the ``datasheet-of`` edge; returns
        a note for the ack. Only the edge the previous ``part_lcsc`` implied
        is removed: a hand-made ``datasheet-of`` to another part (a family
        datasheet) stays."""
        ref = self.store.get_ref(kind="datasheet", id=ref_id)
        old_lcsc = str((ref.meta or {}).get("part_lcsc") or "") if ref else ""
        if old_lcsc and old_lcsc != new_lcsc:
            old_part = self.store.part_ref_id(old_lcsc)
            if old_part is not None:
                self.store.remove_link(
                    src_ref_id=ref_id, dst_ref_id=old_part, relation="datasheet-of"
                )
        if not new_lcsc:
            return ""
        try:
            apply_link_ops(
                self.store,
                ref_id,
                link=f"part:{new_lcsc}",
                unlink=None,
                rel="datasheet-of",
            )
        except NotFound:
            return f" Part {new_lcsc} is not in the catalog: no datasheet-of edge."
        return f" datasheet-of → part:{new_lcsc}."

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
            from precis.pcb.catalog import lcsc_id

            # blank clears the field; a bare C-number in any case or a
            # pasted product URL normalises to the canonical ``C<n>``
            patch["part_lcsc"] = lcsc_id(part_lcsc) or part_lcsc.strip().upper()
        return patch


__all__ = ["DatasheetHandler"]
