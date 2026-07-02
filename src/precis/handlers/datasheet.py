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
machinery verbatim, parameterised on ``self.spec.kind``. The
``datasheet-of`` / ``has-datasheet`` relation to a ``part`` lands in Slice 3.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.handlers.paper import PaperHandler
from precis.protocol import KindSpec

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


__all__ = ["DatasheetHandler"]
