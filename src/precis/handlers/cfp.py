"""``CfpHandler`` — call-for-proposal / requirements document kind.

A ``cfp`` is a *spec-role* sibling of :class:`~precis.handlers.paper.PaperHandler`
(proposal writing). It is an ingested, read-only PDF document — the
**identical** Marker → chunks pipeline papers use (``precis add --as cfp``
/ ``inbox/cfp/``) — so it gets embeddings, per-chunk keywords, TOC, and
in-document search for free. The only differences from a paper are
declared, not duplicated:

* ``corpus_role='spec'`` — a CFP is the *requirements* a proposal must
  satisfy, **never** citable evidence. The citation handler resolves
  ``source_handle`` only against ``kind='paper'``, so a CFP is
  non-citable by construction; the flag formalises it for the planner
  prompt and the reader chrome, and keeps CFPs out of
  ``search(kind='paper', …)`` (a different kind entirely).
* No ``put`` — a CFP is acquired by ingesting a PDF, not by minting a
  stub from a DOI/arXiv backlog. ``supports_put=False`` ⇒ the base
  handler raises ``Unsupported``.
* Restricted view enum — citation-export views (``bibtex`` / ``ris`` /
  ``endnote`` / ``bibliography``) are dropped; a spec is not a
  bibliographic entry.
* Typed call dates — ``meta.deadline`` / ``meta.opens_on`` (ISO
  ``YYYY-MM-DD``, set via ``edit(args=…)``) and ``meta.source_url``
  (the page ``workers/cfp_watch.py`` re-checks). A queryless
  ``search`` browses the corpus deadline-sorted.

Everything else (``get`` overview + chunk rendering, ``search`` /
``search_hits``, ``edit`` metadata repair, ``tag`` / ``link``) is the
paper machinery verbatim, parameterised on ``self.spec.kind`` so it
addresses the ``cfp`` corpus.

The inverse of the proposal-project ``has-requirement`` link
(``requirement-of``) lives on the CFP node — see ``store/types.py``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.protocol import KindSpec

if TYPE_CHECKING:
    from precis.response import Response
    from precis.store import Ref

# Views a spec-role document supports. Mirrors paper's enum minus the
# citation-export formats (a CFP is never cited, so bibtex/ris/endnote/
# bibliography are meaningless). ``toc`` / ``abstract`` / ``health`` /
# ``log`` / ``abbrevs`` all carry over unchanged.
_CFP_VIEWS: tuple[str, ...] = (
    "abstract",
    "toc",
    "health",
    "log",
    "abbrevs",
)


class CfpHandler(PaperHandler):
    """Read-only call-for-proposal / requirements document (spec role)."""

    # A CFP isn't acquired via `precis jobs ingest-bundles` (that's the
    # paper backlog) — it's dropped into `inbox/cfp/` or added directly
    # with `precis add --as cfp`; the empty-listing hint says so instead
    # of leaking paper-pipeline phrasing (gr311347 #12).
    _empty_corpus_message: ClassVar[str] = (
        "no CFPs cached yet - drop a PDF into `inbox/cfp/` or run "
        "`precis add --as cfp <file>` to add one"
    )

    spec: ClassVar[KindSpec] = KindSpec(
        kind="cfp",
        title="Call for Proposal",
        description=(
            "Call-for-proposal / requirements document. A read-only "
            "ingested PDF (via `precis add --as cfp` or the inbox/cfp/ "
            "watch dir) that a proposal draft must satisfy. Addressable "
            "by slug; one ref per document, blocks per chunk — gets "
            "search / TOC / keywords like a paper. Spec role: NEVER "
            "citable evidence (it is the requirements, not a source). "
            "Link it to a proposal project with "
            "link(rel='has-requirement') so the planner consults it. "
            "Use get(view='toc') to read the required sections + limits."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # No stub minting — a CFP is acquired by ingesting a PDF, not
        # by requesting one into a fetch backlog.
        supports_put=False,
        # Metadata repair (title / year / abstract) reuses paper's edit;
        # useful for the web meta editor.
        supports_edit=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        corpus_role="spec",
        placement="corpus",
        views=_CFP_VIEWS,
    )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        # Spec docs expose the reading views only — no citation exports.
        return list(_CFP_VIEWS)

    # ``source_url`` is the page the WATCH re-check fetches
    # (workers/cfp_watch.py); ``deadline``/``opens_on`` are the typed
    # dates the kind exists for (gr462088 slice 1). All live in
    # ``refs.meta``.
    ARGS_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "edit": frozenset({"source_url", "deadline", "opens_on"}),
    }

    #: Queryless ``search(kind='cfp')`` sorts by deadline — a call's
    #: deadline is the reason to browse the corpus at all.
    _BROWSE_SORT_DEFAULT: ClassVar[str] = "deadline"

    def _edit_args(
        self, ref_id: int, args: dict[str, Any] | None, *, dry: bool
    ) -> list[str]:
        """``edit(args=…)`` — ``source_url``: the page ``WATCH:*`` re-checks
        fetch (workers/cfp_watch.py); ``deadline``/``opens_on``: ISO
        ``YYYY-MM-DD`` dates (a bad value is a BadInput, nothing written);
        an empty string clears a date."""
        args = args or {}
        labels: list[str] = []
        url = args.get("source_url")
        if url is not None:
            url = str(url).strip()
            if not re.match(r"https?://[^\s/]+", url):
                raise BadInput(
                    "source_url must be an http(s) URL",
                    next="edit(kind='cfp', id=<slug|id>, "
                    "args={'source_url': 'https://example.org/call'})",
                )
            if not dry:
                self.store.stamp_ref_meta(ref_id, {"source_url": url})
            labels.append(f"source_url={url}")
        patch: dict[str, Any] = {}
        for key in ("deadline", "opens_on"):
            if key not in args:
                continue
            raw = args[key]
            if raw is None or not str(raw).strip():
                patch[key] = None
                labels.append(f"{key} cleared")
                continue
            day = str(raw).strip()
            try:
                parsed = date.fromisoformat(day)
            except ValueError:
                parsed = None
            if parsed is None or parsed.isoformat() != day:
                raise BadInput(
                    f"{key} must be an ISO date (YYYY-MM-DD), got {raw!r}",
                    next=f"edit(kind='cfp', id=<slug|id>, "
                    f"args={{'{key}': '2026-11-15'}})",
                )
            patch[key] = day
            labels.append(f"{key}={day}")
        if patch and not dry:
            self.store.stamp_ref_meta(ref_id, patch)
        return labels

    def _overview_meta_lines(self, ref: Ref) -> list[str]:
        meta = ref.meta or {}
        return [
            f"{label}: {meta[key]}"
            for key, label in (("deadline", "deadline"), ("opens_on", "opens on"))
            if meta.get(key)
        ]

    def search(self, *, sort: str | None = None, **kw: Any) -> Response:
        """Cfp search — paper's block search, plus a queryless browse.

        ``sort`` is a browse-only key: ``'deadline'`` (the default —
        soonest first, undated calls last), ``'opens_on'`` or
        ``'recency'``. With ``q=`` ranking owns the order and ``sort=``
        is rejected. ``search(kind='cfp')`` bare enumerates the corpus.
        """
        if (
            (kw.get("q") and str(kw["q"]).strip())
            or kw.get("title")
            or kw.get("author")
        ):
            if sort is not None:
                raise BadInput(
                    "sort= is a queryless cfp browse key — a block search "
                    "ranks by relevance",
                    next="search(kind='cfp', args={'sort': 'deadline'})",
                )
            return super().search(**kw)
        for flag in ("good", "queries", "answers", "per_paper"):
            if kw.get(flag):
                raise BadInput(
                    f"{flag}= needs q= — a queryless cfp browse has no ranking to tune",
                    next="search(kind='cfp', q='…') or drop the flag",
                )
        if kw.get("include_ref_ids") is not None or kw.get("exclude_ref_ids"):
            raise BadInput(
                "cited=/uncited= needs q= for a cfp search — a queryless "
                "browse can't apply the ref filter",
                next="search(kind='cfp', q='…', uncited=<draft>)",
            )
        chosen = (
            str(sort).strip().lower() if sort is not None else self._BROWSE_SORT_DEFAULT
        )
        if chosen not in self._BROWSE_SORTS:
            raise BadInput(
                f"sort= must be one of {sorted(self._BROWSE_SORTS)}, got {sort!r}",
                next="search(kind='cfp', args={'sort': 'deadline'})",
            )
        return self._render_tags_only_search(
            tags=kw.get("tags"),
            page=int(kw.get("page") or 1),
            page_size=int(kw.get("page_size") or 10),
            sort=chosen,
        )


__all__ = ["CfpHandler"]
