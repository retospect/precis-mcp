"""``ProvenanceHandler`` — retraction / amendment checks against Crossref.

Phase 1 + 2 surface (``docs/design/provenance-kind-plan.md``):

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

import json
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput
from precis.handlers._provenance_report import (
    _VALID_VIEWS,
    render_batch,
    render_exists,
    render_single,
)
from precis.ingest.provenance import (
    BibEntry,
    check_doi,
    check_dois,
    parse_doi_list,
)
from precis.protocol import Handler, KindSpec
from precis.response import Response

_SUPPORTED_VIEWS: tuple[str, ...] = (
    "default",
    "blockers",
    "json",
    "verify",
    "exists",
)


def _parse_structured_input(raw: str) -> list[BibEntry] | None:
    """Try to parse ``raw`` as a JSON-shaped BibEntry batch.

    Accepts a list of objects ``[{doi, title?, authors?, year?, ...}]``
    or a single object. Returns ``None`` if the input doesn't look
    like JSON (so the caller can fall back to plain DOI parsing).

    Raises ``BadInput`` when the input *does* look like JSON but
    parses to something other than a BibEntry shape — that's a
    caller bug worth surfacing loudly rather than silently dropping
    fields.
    """
    stripped = raw.strip()
    if not stripped or stripped[0] not in ("[", "{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise BadInput(
            f"provenance: input looks like JSON but won't parse: {e}",
            next=(
                "get(kind='provenance', "
                'q=\'[{"doi":"10.x/foo","title":"..."}]\', '
                "view='verify')"
            ),
        ) from e

    items = payload if isinstance(payload, list) else [payload]
    out: list[BibEntry] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise BadInput(
                f"provenance: structured input entry {i} is not an object",
                next="each entry must be {doi, title?, authors?, year?}",
            )
        doi = item.get("doi")
        if not isinstance(doi, str) or not doi.strip():
            raise BadInput(
                f"provenance: structured input entry {i} missing 'doi' field",
                next="each entry must carry a 'doi' string",
            )
        authors = item.get("authors")
        # Accept either ['Smith', 'Doe'] or [{name: 'Smith, J.'}, ...]
        normalised_authors: list[str] | None = None
        if isinstance(authors, list):
            normalised_authors = []
            for a in authors:
                if isinstance(a, str):
                    normalised_authors.append(a)
                elif isinstance(a, dict):
                    n = a.get("name") or a.get("family") or ""
                    if isinstance(n, str) and n:
                        # Take the surname (before the comma if present)
                        normalised_authors.append(n.split(",")[0].strip())
            if not normalised_authors:
                normalised_authors = None
        year = item.get("year")
        if year is not None and not isinstance(year, int):
            try:
                year = int(year)
            except (TypeError, ValueError):
                year = None
        out.append(
            BibEntry(
                doi=doi.strip(),
                title=item.get("title") or None,
                authors=normalised_authors,
                year=year,
                journal=item.get("journal") or None,
                pages=item.get("pages") or None,
            )
        )
    return out


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
        transitive: bool = False,
        suggest_candidates: bool = False,
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

        # Phase 2.5: detect JSON-shaped structured input. When present,
        # each entry carries a BibEntry that gets verified against
        # Crossref. Plain DOI input still works — the verify view just
        # has nothing to verify against, so the mismatch section will
        # show a "ran on 0/N entries" note.
        bib_entries: list[BibEntry] | None = _parse_structured_input(raw)
        if bib_entries is not None:
            if not bib_entries:
                raise BadInput(
                    "provenance: structured input parsed to an empty list",
                    next="pass at least one {doi: '...'} entry",
                )
            dois = [e.doi for e in bib_entries]
        else:
            dois = parse_doi_list(raw)
            if not dois:
                raise BadInput(
                    "provenance: no DOIs found in input after parsing",
                    next="get(kind='provenance', id='10.1038/nature05095')",
                )

        # The verify view is only meaningful with structured input. If
        # the caller asked for it but passed bare DOIs, that's almost
        # certainly a mistake — surface it cleanly rather than running
        # a normal report under a misleading name.
        if v == "verify" and bib_entries is None:
            raise BadInput(
                "provenance: view='verify' requires structured input "
                "(JSON list of {doi, title, authors, year}). Plain DOI "
                "input has nothing to verify against.",
                next=(
                    "get(kind='provenance', "
                    'q=\'[{"doi":"10.x/foo","title":"…","year":2020}]\', '
                    "view='verify')"
                ),
            )

        # view='exists' is a cheap shortcut — runs check_doi with
        # store=None so no write-through happens, then renders only
        # the format/resolve outcome (no notice processing). Phase 5.
        if v == "exists":
            results = check_dois(
                dois,
                store=None,  # pure existence check; no DB writes
                mailto=self._mailto,
                bib_entries=bib_entries,
                transitive=False,  # exists view ignores cite-walk
            )
            return Response(body=render_exists(results))

        # Single-DOI path keeps the rich per-paper markdown layout when
        # the caller didn't ask for a structured view. Everything else
        # goes through the batch renderer.
        if len(dois) == 1 and v == "default":
            entry = bib_entries[0] if bib_entries else None
            result = check_doi(
                dois[0],
                store=self.store,
                mailto=self._mailto,
                bib_entry=entry,
                transitive=transitive,
                suggest_candidates=suggest_candidates,
            )
            return Response(body=render_single(result))

        results = check_dois(
            dois,
            store=self.store,
            mailto=self._mailto,
            bib_entries=bib_entries,
            transitive=transitive,
            suggest_candidates=suggest_candidates,
        )
        return Response(body=render_batch(results, view=v))  # type: ignore[arg-type]


__all__ = ["ProvenanceHandler"]
