"""``ProvenanceHandler`` — retraction / amendment checks against Crossref.

Phase 1 + 2 surface (``docs/provenance-kind-plan.md``):

- ``get(id='<doi>')`` — single-DOI provenance check returning a
  markdown report. Accepts the DOI in any common form: bare
  ``10.x/foo``, ``doi:10.x/foo``, ``https://doi.org/10.x/foo``.
- ``get(q='10.x/a, 10.x/b, ...')`` — batch input, comma- or
  whitespace-separated. Up to ~300 DOIs comfortably; concurrency
  is bounded by an 8-worker thread pool.
- ``view='blockers'`` — show only 🔴/🟠 entries (preflight summary).
- ``view='json'`` — structured payload for downstream tooling.

Behaviour:

- Always returns a report. Malformed DOIs and unknown DOIs surface
  with a clear status section rather than as errors — the preflight
  use case wants to see "this one's wrong" alongside the rest of
  the batch, not a hard fail.
- When the parent paper is in the local store, write-through
  happens (notice refs ingested, links attached, status column +
  STATUS tag set). When it isn't, the result is informational only.
- Notice refs created here gain kind='paper', provider='crossref',
  and a ``STATUS:notice`` tag so search surfaces can distinguish
  them from primary papers.

Out of scope: Retraction Watch reason codes (Phase 3), transitive
cite-walk (Phase 4), fuzzy DOI resolution + ``view='exists'`` /
``view='verify'`` (Phase 5).

The kind is stateless from the dispatch perspective — no refs of
kind ``'provenance'`` exist; the handler enriches existing
``paper`` refs on the side. That's why ``'provenance'`` isn't in
``kinds`` table seed in ``0001_initial.sql``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput
from precis.handlers._provenance_report import (
    _VALID_VIEWS,
    render_batch,
    render_single,
)
from precis.ingest.provenance import check_doi, check_dois, parse_doi_list
from precis.protocol import Handler, KindSpec
from precis.response import Response


_SUPPORTED_VIEWS: tuple[str, ...] = ("default", "blockers", "json")


class ProvenanceHandler(Handler):
    """Stateless-from-dispatch tool kind for retraction monitoring.

    The handler holds a reference to the store when one is wired
    (so write-through can persist notice refs and STATUS tags), but
    does not require it — passing a DOI for a paper not in the
    local store still works, returning an informational report.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="provenance",
        title="Provenance / health check",
        description=(
            "Check a DOI (or batch of DOIs) for retractions, expressions "
            "of concern, and corrections via Crossref. Use id='10.x/foo' "
            "for a single DOI; q='10.x/a, 10.x/b, ...' for a batch. "
            "view='blockers' shows only 🔴/🟠; view='json' emits a "
            "structured payload."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        views=_SUPPORTED_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        # habanero is the Crossref client used by both the existing
        # ingest path and provenance. Import here so a bare install
        # without ``[paper]`` extras surfaces a clean missing-dep at
        # boot (dispatch._try catches ImportError and drops the
        # kind), rather than failing at module import and taking
        # the whole handlers package down with it. Mirrors the calc
        # → sympy pattern.
        try:
            import habanero  # noqa: F401
        except ImportError as e:
            raise InitError(
                "provenance: habanero not installed "
                "(install with `pip install 'precis-mcp[paper]'`)"
            ) from e

        # Store is optional. When wired, write-through happens; when
        # not, results are informational only. Keep ``self.store``
        # always-set (possibly ``None``) so the get() body has one
        # path that handles both.
        self.store = hub.store

        # Crossref polite-pool mailto. ``ingest/crossref.py`` reads
        # this from PRECIS_CROSSREF_MAILTO at call time too; mirror
        # so we make the same recommended-headers HTTP call.
        import os

        self._mailto = os.environ.get("PRECIS_CROSSREF_MAILTO") or None

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Accept id= as primary; q= as a synonym. Tool kinds across
        # the surface treat them interchangeably (see calc.py for the
        # same shape). Both can carry a single DOI or a batch.
        raw = id if id is not None else q
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise BadInput(
                "provenance: id (or q) is required — a DOI, or a comma/"
                "whitespace-separated batch of DOIs",
                next="get(kind='provenance', id='10.1038/nature05095')",
            )
        if not isinstance(raw, str):
            raise BadInput(
                f"provenance id must be a string, got {type(raw).__name__}",
                next="get(kind='provenance', id='10.1038/nature05095')",
            )

        # Validate view eagerly so a typo doesn't run a 250-DOI batch
        # before the error surfaces.
        v: str = view or "default"
        if v not in _VALID_VIEWS:
            raise BadInput(
                f"provenance: unknown view={view!r}; "
                f"supported views are {', '.join(_VALID_VIEWS)}",
                next="get(kind='provenance', id='10.x/foo', view='blockers')",
            )

        dois = parse_doi_list(raw)
        if not dois:
            # ``parse_doi_list`` strips empty / comment-only inputs;
            # we surface a clear error rather than running an empty batch.
            raise BadInput(
                "provenance: no DOIs found in input after parsing",
                next="get(kind='provenance', id='10.1038/nature05095')",
            )

        # Single-DOI path keeps the rich per-paper markdown layout when
        # the caller didn't ask for a structured view. Everything else
        # goes through the batch renderer.
        if len(dois) == 1 and v == "default":
            result = check_doi(dois[0], store=self.store, mailto=self._mailto)
            return Response(body=render_single(result))

        results = check_dois(dois, store=self.store, mailto=self._mailto)
        return Response(body=render_batch(results, view=v))  # type: ignore[arg-type]


__all__ = ["ProvenanceHandler"]
