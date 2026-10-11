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

Everything else (``get`` overview + chunk rendering, ``search`` /
``search_hits``, ``edit`` metadata repair, ``tag`` / ``link``) is the
paper machinery verbatim, parameterised on ``self.spec.kind`` so it
addresses the ``cfp`` corpus.

The inverse of the proposal-project ``has-requirement`` link
(``requirement-of``) lives on the CFP node — see ``store/types.py``.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.protocol import KindSpec

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
    # (workers/cfp_watch.py); it lives in ``refs.meta.source_url``.
    ARGS_KEYS: ClassVar[dict[str, frozenset[str]]] = {
        "edit": frozenset({"source_url"}),
    }

    def _edit_args(
        self, ref_id: int, args: dict[str, Any] | None, *, dry: bool
    ) -> list[str]:
        """``edit(args={'source_url': 'https://…'})`` — the page ``WATCH:*``
        re-checks fetch (workers/cfp_watch.py), stored in ``meta.source_url``."""
        url = (args or {}).get("source_url")
        if url is None:
            return []
        url = str(url).strip()
        if not re.match(r"https?://[^\s/]+", url):
            raise BadInput(
                "source_url must be an http(s) URL",
                next="edit(kind='cfp', id=<slug|id>, "
                "args={'source_url': 'https://example.org/call'})",
            )
        if not dry:
            self.store.stamp_ref_meta(ref_id, {"source_url": url})
        return [f"source_url={url}"]


__all__ = ["CfpHandler"]
