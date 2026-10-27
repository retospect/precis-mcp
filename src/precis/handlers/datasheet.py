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
* ``put(args={'part', 'url'?})`` queues a ``datasheet_pull`` for an
  agent-found PDF URL, or without ``url`` re-queues the catalog's own URL
  (the pull job downloads through ``safe_stream``), and queues the part's
  ``datasheet_facts`` read; ``part`` is required because the link, the pull
  record and the fact block all live on the part ref.
  ``get(kind='part')`` queues the catalog's own URL ahead of any design.
  A local-path put is deliberately absent: the pull runs on a worker that
  cannot see an agent's sandbox, and ingesting inline would run Marker in
  the request.
* ``put(args={'mfr_part', 'manufacturer'?, 'url'})`` is the non-LCSC door
  (gr519849): a consigned part has no C-number, so it find-or-mints a
  ``component`` ref (mpn+manufacturer, category ``electronic``), stamps the
  URL as its supplier identity link and queues the pull's
  ``component_ref_id`` leg — the sheet lands ``datasheet-of`` the
  component.

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
from precis.handlers.paper import _STATUS_MAX_IDS, PaperHandler, _args_truthy
from precis.ingest.kind_convert import DATASHEET_SUBTYPES as _DATASHEET_SUBTYPES
from precis.pcb import jlc_api
from precis.pcb._http import VendorError
from precis.pcb.datasheets import (
    datasheet_state,
    datasheet_states,
    enqueue_facts_one,
    enqueue_one,
    pull_in_flight,
)
from precis.protocol import ArgsSpec, KindSpec
from precis.response import Response
from precis.store.types import Ref
from precis.utils import handle_registry
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
    # gr520326: a datasheet's figure crops / page renders ride the same
    # signed-link door as a paper's — datasheet is the kind that hit the
    # caption-only failure this view exists for.
    "page_image",
)

# The electronics-doc sub-genres a datasheet's ``meta.subtype`` may name (one
# kind for the whole family — the PCB netlist+placement IR). ``datasheet`` is the default.
# Labels for humans/export live in ``export.latex.DATASHEET_SUBTYPE_LABELS``.
# The vocabulary itself lives in ingest.kind_convert so the paper→datasheet
# convert door validates the same set without importing this module.

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


def _pull_line(state_or_pull: Any) -> str:
    """The ``meta.datasheet_pull`` record as one status-cell string.

    Accepts a ``datasheet_states`` row (``{…, "pull": dict|None}``) or
    the pull dict itself. ``{status: ok|failed, reason?, detail?, url?,
    at}``; an ok record can carry a later failed re-pull as
    ``last_attempt`` (datasheet_pull.pull)."""
    pull = (
        state_or_pull.get("pull")
        if isinstance(state_or_pull, dict) and "pull" in state_or_pull
        else state_or_pull
    )
    if not isinstance(pull, dict) or not pull:
        return "pull: never tried"
    status = str(pull.get("status") or "?")
    line = f"pull: {status}"
    if status == "failed" and pull.get("reason"):
        line += f": {pull['reason']}"
    if pull.get("at"):
        line += f" {pull['at']}"
    if pull.get("url"):
        line += f"  {pull['url']}"
    last = pull.get("last_attempt")
    if isinstance(last, dict) and last.get("reason"):
        line += f"  (latest re-pull failed: {last['reason']})"
    return line


class DatasheetHandler(PaperHandler):
    """Component datasheet (evidence role); put queues a pull, never a stub."""

    # A datasheet isn't acquired via `precis jobs ingest-bundles` (that's
    # the paper backlog) — it's lazily ingested from a part's
    # `datasheet_url` or dropped into `<inbox>/datasheets/`; the
    # empty-listing hint says so instead of leaking paper-pipeline
    # phrasing (gr311347 #12).
    _empty_corpus_message: ClassVar[str] = (
        "no datasheets ingested yet - link a part's `datasheet_url` or "
        "drop a PDF into `<inbox>/datasheets/` to add one"
    )

    #: Overrides paper's ``'native'``: this edit's scalar-id signature
    #: forwards bibliographic fields to the paper editor, which refuses a
    #: list — so the dispatcher's per-id repeat applies instead.
    edit_id_list: ClassVar[str] = "repeat"

    spec: ClassVar[KindSpec] = KindSpec(
        kind="datasheet",
        title="Datasheet",
        description=(
            "Component datasheet — an ingested PDF read "
            "in the same two-pane reader as a paper (embeddings / keywords / "
            "TOC / in-doc search), addressable by slug. Evidence role: citable "
            "as a part's source, but scoped out of academic paper search. One "
            "kind for the whole electronics-doc family (app-note / errata via a "
            "meta sub-type). Attach one with put(args={'part': 'C…', 'url': "
            "'https://…pdf'}) (url optional: the catalog's), which also queues "
            "the part's fact extraction. A part with no C-number (consigned) "
            "uses args={'mfr_part', 'manufacturer'?, 'url'} — it lands on a "
            "component ref. Link a datasheet to a part with "
            "link(rel='datasheet-of'). "
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

    # put() is a datasheet pull, not a paper acquire: its own args keys
    # (``path`` is accepted only to refuse it with a hint; ``stub``
    # mints a bodiless ref a human fills by named drop — gr520099;
    # ``mfr_part``/``manufacturer`` address a non-LCSC part through the
    # component kind — gr519849), the rest inherited from PaperHandler.
    ARGS: ClassVar[dict[str, ArgsSpec]] = {
        **PaperHandler.ARGS,
        "put": ArgsSpec({"url", "part", "path", "stub", "mfr_part", "manufacturer"}),
    }

    def __init__(self, *, hub: Hub) -> None:
        super().__init__(hub=hub)
        self.hub = hub

    def put(
        self,
        *,
        args: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Response:
        """Queue the fetch of a datasheet PDF for a part, and its fact read.

        ``put(kind='datasheet', args={'url': 'https://…/x.pdf', 'part':
        'C19191684'})``. ``part`` is an LCSC C-number, or a manufacturer part
        number matching exactly one catalog row. The URL is agent-supplied:
        it is checked against the SSRF guard here (private/loopback is
        refused at once) and fetched again, pinned, by the job, which also
        enforces ``%PDF`` magic and the 50 MB cap and records the outcome on
        ``get(kind='part', id=<C-number>)``. Without ``url`` the catalog's
        URL is re-pulled (a failed earlier attempt included) unless the part
        already has a datasheet. Either way the part's ``datasheet_facts``
        job is queued unless its facts are already extracted or queued.

        ``args={'part': 'C…', 'stub': True}`` is the manual-fill door
        (gr520099): it mints a bodiless datasheet ref already linked
        ``datasheet-of`` the part — for a PDF no pull can fetch, filled
        later by a named ``da<N>.pdf`` drop — and queues nothing.
        ``stub`` refuses to combine with ``url`` (one door per call).

        ``args={'mfr_part': '…', 'manufacturer': '…', 'url': '…'}`` is the
        non-LCSC door (gr519849) for a consigned part with no C-number:
        it find-or-mints a ``component`` ref on (mpn, manufacturer) and
        queues the pull's ``component_ref_id`` leg. ``url`` is required
        there — no catalog row means no catalog URL to fall back to.
        """
        extras = args or {}
        url = str(extras.get("url") or kw.get("url") or "").strip()
        part = str(extras.get("part") or kw.get("part") or "").strip()
        mfr_part = str(extras.get("mfr_part") or kw.get("mfr_part") or "").strip()
        manufacturer = str(
            extras.get("manufacturer") or kw.get("manufacturer") or ""
        ).strip()
        want_stub = _args_truthy(extras.get("stub") or kw.get("stub"))
        if part and mfr_part:
            raise BadInput(
                "part= and mfr_part= are two addressing doors — pass one",
                next="part=<C-number> for a catalog part, mfr_part= (+ "
                "manufacturer=) for a consigned/non-LCSC one",
            )
        if want_stub and mfr_part:
            raise BadInput(
                "stub= is the C-number door's manual-fill path",
                next="put(kind='datasheet', args={'part': 'C…', 'stub': True})",
            )
        if want_stub and url:
            raise BadInput(
                "stub and url don't combine: a stub is the manual-fill "
                "door, a url is a pull",
                next="put(kind='datasheet', args={'part': 'C…', 'stub': True}) "
                "or put(kind='datasheet', args={'part': 'C…', "
                "'url': 'https://…/x.pdf'})",
            )
        if extras.get("path") or kw.get("path"):
            raise BadInput(
                "put(kind='datasheet') takes a url, not a local path: the pull "
                "runs on a worker that cannot read your sandbox",
                next="host the PDF and pass args={'url': …, 'part': 'C…'}, or "
                "ask an operator to drop it in <inbox>/datasheets/",
            )
        if not part and not mfr_part:
            raise BadInput(
                "put(kind='datasheet') needs part= or mfr_part= (url= is "
                "optional on the part= door: without it the catalog's "
                "datasheet URL is pulled)",
                next="put(kind='datasheet', args={'url': 'https://…/x.pdf', "
                "'part': 'C19191684'}), or args={'mfr_part': 'FC0V474ZFTBR24', "
                "'manufacturer': 'KEMET', 'url': 'https://…/x.pdf'} for a "
                "non-LCSC part",
            )
        if url:
            try:
                assert_public_http_url(url)
            except SsrfBlocked as exc:
                raise BadInput(
                    f"url refused: {exc}",
                    next="use a public http(s) URL: "
                    "put(kind='datasheet', args={'url': 'https://…/x.pdf', "
                    "'part': 'C19191684'})",
                ) from exc
            except OSError as exc:  # DNS failure/timeout: not a verdict on the URL
                raise BadInput(
                    f"url could not be resolved: {exc}",
                    next="retry with a reachable host: "
                    "put(kind='datasheet', args={'url': 'https://…/x.pdf', "
                    "'part': 'C19191684'})",
                ) from exc
        if mfr_part:
            if not url:
                raise BadInput(
                    "a non-LCSC part has no catalog datasheet URL — "
                    "mfr_part= needs url= (the pull fetches exactly that)",
                    next="put(kind='datasheet', args={'mfr_part': "
                    f"{mfr_part!r}, 'url': 'https://…/x.pdf'" + "})",
                )
            return self._put_component_datasheet(mfr_part, manufacturer, url)
        lcsc = self._resolve_part(part)
        if want_stub:
            return self._put_stub(lcsc)
        job = self.hub.sibling("job")
        if url:
            queued = enqueue_one(self.store, job, lcsc, url=url)
            pull = f"datasheet pull {'queued' if queued else 'already queued'} "
            pull += f"for part:{lcsc}" + ("" if queued else " and this url")
        elif enqueue_one(self.store, job, lcsc, force=True):
            pull = f"datasheet pull queued for part:{lcsc} (catalog url)"
        elif (ds := datasheet_state(self.store, lcsc)["datasheet"]) is not None:
            pull = (
                f"part:{lcsc} already has datasheet {ds[1] or ds[0]} "
                "(pass url= to add another)"
            )
        elif pull_in_flight(self.store, lcsc):
            pull = f"datasheet pull already queued for part:{lcsc}"
        else:
            # enqueue_one swallowed a failure — don't claim a job exists
            pull = f"datasheet pull could not be queued for part:{lcsc}"
        facts = {
            "queued": "facts extraction queued",
            "in_flight": "facts extraction already queued",
            "extracted": "facts already extracted",
            "no_datasheet": "no datasheet to extract facts from",
        }.get(enqueue_facts_one(self.store, job, lcsc), "facts queue failed")
        return Response(
            body=f"{pull}; {facts}. Outcome: get(kind='part', id='{lcsc}') "
            "(datasheet line; view='facts' for the fact block)."
        )

    def _put_stub(self, lcsc: str) -> Response:
        """Mint the bodiless datasheet ``args={'part': …, 'stub': True}``
        asks for (gr520099).

        The manual-fill door for a PDF no pull can fetch: a
        ``kind='datasheet'`` ref with ``pdf_sha256 IS NULL``, titled
        ``<mfr> <mfr_part>`` from the catalog row, already linked
        ``datasheet-of`` the part's lazy ref — the paper-stub
        equivalent a human fills by dropping ``da<N>.pdf`` into
        ``<inbox>/datasheets/`` (the ingest folds it in place via the
        handle stem). Idempotent: a live bodiless datasheet already
        linked to the part is reported, never duplicated; a *filled*
        one doesn't block a new stub (a second sheet for the part is
        legitimate).
        """
        from precis.ingest.db_writer import resolve_slug
        from precis.ingest.pipeline import datasheet_slug_base, mpn_slug_hint
        from precis.utils import handle_registry

        part_id = self.store.ensure_part_ref(lcsc, uncatalogued_ok=True)
        with self.store.pool.connection() as conn:
            row = conn.execute(
                "SELECT d.ref_id, ck.id_value FROM links l "
                "JOIN refs d ON d.ref_id = l.src_ref_id "
                "LEFT JOIN ref_identifiers ck ON ck.ref_id = d.ref_id "
                "  AND ck.id_kind = 'cite_key' "
                "WHERE l.dst_ref_id = %s AND l.relation = 'datasheet-of' "
                "  AND d.kind = 'datasheet' AND d.retired_at IS NULL "
                "  AND d.pdf_sha256 IS NULL "
                "ORDER BY d.ref_id LIMIT 1",
                (part_id,),
            ).fetchone()
        if row is not None:
            handle = handle_registry.format_handle("datasheet", int(row[0]))
            return Response(
                body=f"part:{lcsc} already tracks datasheet stub {handle} "
                f"[{row[1] or handle}] — drop the PDF as "
                f"<inbox>/datasheets/{handle}.pdf to fill it."
            )

        cat = self.store.part_row(lcsc) or {}
        mfr = str(cat.get("mfr") or "").strip()
        mpn = str(cat.get("mfr_part") or "").strip()
        title = " ".join(p for p in (mfr, mpn) if p) or lcsc
        base = datasheet_slug_base(mpn_slug_hint(mfr, mpn), "", title)
        with self.store.tx() as conn:
            slug = resolve_slug(base or f"datasheet-{lcsc.lower()}", conn=conn)
            ref = self.store.insert_ref(
                kind="datasheet",
                slug=slug,
                title=title,
                meta={
                    "set_by": "agent",
                    "part_lcsc": lcsc,
                    "subtype": "datasheet",
                    **({"vendor": mfr} if mfr else {}),
                },
                conn=conn,
            )
            self.store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=part_id,
                relation="datasheet-of",
                conn=conn,
            )
        handle = handle_registry.format_handle("datasheet", ref.id)
        return Response(
            body=f"datasheet stub {handle} minted for part:{lcsc} [{slug}] — "
            f"drop the PDF as <inbox>/datasheets/{handle}.pdf to fill it. "
            f"Backlog: search(kind='datasheet', view='stubs')."
        )

    def _put_component_datasheet(
        self, mfr_part: str, manufacturer: str, url: str
    ) -> Response:
        """``args={'mfr_part', 'manufacturer'?, 'url'}`` — the non-LCSC door
        (gr519849).

        A consigned or Extended-catalog-absent part has no C-number, so the
        ``parts`` table and its lcsc-keyed pull state can't hold it; its
        home is a ``component`` ref (the kind that already carries
        mpn/manufacturer and consigned BOM lines), find-or-minted on
        ``(mpn, manufacturer)`` with category ``electronic``. The
        agent-supplied ``url`` is stamped as the component's supplier
        identity link first — ``datasheet_pull``'s ``component_ref_id``
        leg (:func:`precis.workers.job_types.datasheet_pull.pull_component`)
        refuses a URL the stored identity doesn't name, so the stamp is
        what makes the queued job legal.
        """
        import hashlib

        from precis.ingest.db_writer import resolve_slug
        from precis.ingest.pres import kebab_slug
        from precis.utils.timeutil import now_iso

        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, "
                "  (SELECT id_value FROM ref_identifiers ri "
                "   WHERE ri.ref_id = refs.ref_id "
                "     AND ri.id_kind = 'cite_key' LIMIT 1) AS slug, "
                "  meta->>'manufacturer' FROM refs "
                "WHERE kind = 'component' AND retired_at IS NULL "
                "  AND lower(meta->>'mpn') = lower(%s) "
                "ORDER BY ref_id LIMIT 5",
                (mfr_part,),
            ).fetchall()
        if manufacturer:
            rows = [
                r for r in rows if (r[2] or "").strip().lower() == manufacturer.lower()
            ]
        if len(rows) > 1:
            raise BadInput(
                f"mfr_part {mfr_part!r} matches several components",
                options=[str(r[1]) for r in rows],
                next="disambiguate with manufacturer=, or point a pcb's "
                "consigned line at one of these slugs",
            )
        if rows:
            comp_id, comp_slug = int(rows[0][0]), str(rows[0][1])
            created = False
        else:
            title = " ".join(p for p in (manufacturer, mfr_part) if p)
            with self.store.pool.connection() as conn:
                comp_slug = resolve_slug(
                    kebab_slug(title) or "consigned-part", conn=conn
                )
            meta: dict[str, Any] = {
                "category": "electronic",
                "mpn": mfr_part,
                "supply": "consign",
            }
            if manufacturer:
                meta["manufacturer"] = manufacturer
            ref, created = self.store.component_entity_upsert(
                slug=comp_slug, title=title or mfr_part, meta_patch=meta
            )
            comp_id = ref.id
        # The identity record the pull's provenance check reads. supplier
        # 'manufacturer' is a label for an agent-supplied sheet URL, not a
        # live adapter — ``live_lines`` marks it unreachable, harmlessly.
        self.store.component_supplier_link(
            comp_id,
            {
                "supplier": "manufacturer",
                "supplier_part_number": mfr_part,
                "manufacturer": manufacturer,
                "mpn": mfr_part,
                "product_url": "",
                "datasheet_url": url,
                "match_confidence": "agent_supplied_url",
                "first_linked_at": now_iso(),
            },
        )
        job = self.hub.sibling("job")
        resp = job.put(
            job_type="datasheet_pull",
            executor="job_inproc",
            parent_id=comp_id,
            params={"component_ref_id": comp_id, "url": url},
            idem_key=f"datasheet_pull:component:{comp_id}:"
            + hashlib.sha256(url.encode()).hexdigest()[:16],
        )
        verb = "created" if created else "reused"
        queued = "already queued" if getattr(resp, "reused", False) else "queued"
        return Response(
            body=f"{verb} component {comp_slug} (non-LCSC part {mfr_part}"
            + (f", {manufacturer}" if manufacturer else "")
            + f"); datasheet pull {queued}. Outcome: get(kind='component', "
            f"id='{comp_slug}') — the pulled sheet lands as a "
            "datasheet-of link."
        )

    def _resolve_part(self, part: str) -> str:
        from precis.pcb.catalog import lcsc_id

        lcsc = lcsc_id(part)
        if lcsc is not None:
            if self.store.part_row(lcsc) is None:
                # same catalog-miss fallback as get(kind='part'): the dump
                # lacks the Extended range; a hit is upserted by the lookup
                try:
                    found = jlc_api.live_lookup(self.store, lcsc) is not None
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

    def _overview_meta_lines(self, ref: Ref) -> list[str]:
        """``source: <meta.source_url>`` — which PDF the sheet came from
        (gr520077; a pull or an agent-supplied URL stamps it, an inbox
        drop leaves it absent and no line shows)."""
        url = str((ref.meta or {}).get("source_url") or "").strip()
        return [f"source: {url}"] if url else []

    def _fetch_status_view(self, ids: list[str] | None) -> Response:
        """``search(kind='datasheet', view='status', ids=[…])`` — one row
        per datasheet (gr520077).

        ``ids=`` mixes ``da<N>`` handles, cite_key slugs and C-numbers:
        a ref shows its body state (full-text / card-only / stub) and,
        when ``meta.part_lcsc`` links it to a catalog part, that part's
        recorded ``datasheet_pull`` outcome (status / reason / url /
        at) — the failure record a ``not_pdf`` or dead-vendor-host pull
        leaves, which is otherwise only readable via SQL. A C-number
        shows the pull record plus its linked datasheet, if any.
        """
        from precis.handlers._exclude_closure import resolve_exclude_paper_ids
        from precis.pcb.catalog import lcsc_id
        from precis.utils.next_block import render_next_section

        if isinstance(ids, str):
            ids = [ids]
        if not ids:
            raise BadInput(
                "view='status' needs ids= (da handles, slugs or C-numbers)",
                next="search(kind='datasheet', view='status', "
                "ids=['da519921', 'C2895249'])",
            )
        if len(ids) > _STATUS_MAX_IDS:
            raise BadInput(
                f"ids= has {len(ids)} entries, max {_STATUS_MAX_IDS}",
                next=f"split the list into pages of up to {_STATUS_MAX_IDS}",
            )
        lcscs: list[str] = []
        ref_entries: list[str] = []
        for raw in ids:
            entry = str(raw).strip()
            if (c := lcsc_id(entry)) is not None:
                lcscs.append(c)
            else:
                ref_entries.append(entry)
        resolved = resolve_exclude_paper_ids(
            ref_entries, store=self.store, kind="datasheet"
        )
        rows = self.store.paper_fetch_status(sorted(resolved), kind="datasheet")
        # The pull record lives on the part — fetch it for C-numbers named
        # in ids= AND for parts a listed datasheet's meta.part_lcsc names.
        row_parts = {p for r in rows if (p := self._ref_part_lcsc(r["ref_id"]))}
        states = datasheet_states(self.store, lcscs + sorted(row_parts - set(lcscs)))
        # Linked-datasheet ref_ids behind each C-number, for body state.
        linked_ids = [
            s["datasheet"][0] for s in states.values() if s["datasheet"] is not None
        ]
        linked = {
            r["ref_id"]: r
            for r in self.store.paper_fetch_status(linked_ids, kind="datasheet")
        }
        lines = [
            f"datasheet status — {len(rows) + len(lcscs)} of {len(ids)} ids resolved:",
            "",
        ]
        for r in rows:
            h = handle_registry.format_handle("datasheet", r["ref_id"])
            pdf = "pdf" if r["has_pdf"] else "no-pdf"
            lines.append(
                f"  {h}  [{r['cite_key'] or h}]  {r['body']}"
                f" ({r['full_chunks']} body chunks)  {pdf}"
            )
            part = self._ref_part_lcsc(r["ref_id"])
            if part:
                lines.append(f"      part {part}  {_pull_line(states.get(part))}")
        for lcsc in lcscs:
            st = states[lcsc]
            ds = st["datasheet"]
            lines.append(f"  {lcsc}  {_pull_line(st)}")
            if ds is not None:
                h = handle_registry.format_handle("datasheet", ds[0])
                body = linked.get(ds[0])
                detail = (
                    f"  {body['body']} ({body['full_chunks']} body chunks)"
                    f"  {'pdf' if body['has_pdf'] else 'no-pdf'}"
                    if body
                    else ""
                )
                lines.append(f"      datasheet {h}  [{ds[1] or h}]{detail}")
            else:
                lines.append("      (no datasheet linked)")
        unresolved = len(ids) - len(rows) - len(lcscs)
        lines.append("")
        lines.append(
            f"summary: {len(rows)} datasheets · {len(lcscs)} parts"
            + (f" · {unresolved} unresolved" if unresolved > 0 else "")
        )
        lines.append(
            render_next_section(
                [
                    (
                        "search(kind='datasheet', view='stubs')",
                        "datasheets still needing a PDF",
                    ),
                    (
                        "get(kind='datasheet', id='<handle>', view='log')",
                        "full event log",
                    ),
                ]
            )
        )
        return Response(body="\n".join(lines), result_count=len(rows) + len(lcscs))

    def _ref_part_lcsc(self, ref_id: int) -> str:
        ref = self.store.get_ref(kind="datasheet", id=ref_id)
        return str((ref.meta or {}).get("part_lcsc") or "") if ref else ""

    def _ingest_rate_view(self, days: int | None) -> Response:
        """``search(kind='datasheet', view='ingest', days=N)`` — which
        datasheets landed recently (gr520077).

        A per-ref listing, not paper's per-day rate: the question it
        answers is "which sheets came in, and where did each PDF come
        from" — handle, title, part (``meta.part_lcsc``), live
        body-chunk count (a stub reads 0), ``meta.source_url`` and
        ``created_at``, newest first.
        """
        from datetime import UTC, datetime, timedelta

        from precis.utils.next_block import render_next_section

        n = 14 if days is None else days
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 90:
            raise BadInput(
                f"days must be an integer 1..90, got {days!r}",
                next="search(kind='datasheet', view='ingest', args={'days': 14})",
            )
        since = datetime.now(UTC) - timedelta(days=n)
        rows = self.store.datasheet_ingest_list(since=since)
        lines = [f"datasheets landed, last {n} days ({len(rows)}):", ""]
        if not rows:
            lines.append(
                "  none — put(kind='datasheet', args={'part': 'C…', "
                "'url': …}) pulls one, or a named da<N>.pdf drop fills a stub"
            )
        for r in rows:
            h = handle_registry.format_handle("datasheet", r["ref_id"])
            title = r["title"] or "(untitled)"
            part = f"  part {r['part_lcsc']}" if r["part_lcsc"] else ""
            url = f"  source {r['source_url']}" if r["source_url"] else ""
            lines.append(f"  {h}  [{r['cite_key'] or h}]  {title}")
            lines.append(
                f"      {r['created_at'][:10]}  {r['chunks']} chunks{part}{url}"
            )
        body = "\n".join(lines)
        hints = [
            (
                "search(kind='datasheet', view='stubs')",
                "datasheets still needing a PDF",
            ),
        ]
        if rows:
            top = handle_registry.format_handle("datasheet", rows[0]["ref_id"])
            hints.insert(0, (f"get(kind='datasheet', id='{top}')", "open the latest"))
        body += render_next_section(hints)
        return Response(body=body, result_count=len(rows))

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int,
        vendor: str | None = None,
        subtype: str | None = None,
        part_lcsc: str | None = None,
        title: str | None = None,
        year: int | None = None,
        authors: Any = None,
        abstract: str | None = None,
        doi: str | None = None,
        arxiv: str | None = None,
        journal: str | None = None,
        entry_type: str | None = None,
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
        # Named for the dispatch gates (gr520510): an undeclared bib kwarg
        # is refused top-level and in args= — the signature is both gates.
        bib = {
            k: v
            for k, v in {
                "title": title,
                "year": year,
                "authors": authors,
                "abstract": abstract,
                "doi": doi,
                "arxiv": arxiv,
                "journal": journal,
                "entry_type": entry_type,
            }.items()
            if v not in (None, "")
        }

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
                hub=getattr(self, "hub", None),
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
