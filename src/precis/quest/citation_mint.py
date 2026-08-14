"""Code-callable citation minter — rung 6d-1.

The paper-writing weave (rung 6d-2) mints ``citation`` refs from worker/tick
code, not from an agent's MCP ``put`` call. Citation-minting's primitives
(fabricated-bib-key guard, ``source_handle`` normalization, the
``card_combined`` embed, the ``cites`` link) live only in
:class:`precis.handlers.citation.CitationHandler` today. Rather than
reimplement those three store calls here (and risk losing the bib-key
guard the hard way), :func:`mint_citation` constructs the handler
in-process and drives it through its own ``put`` — the same entrypoint the
MCP surface uses, so a code caller and an agent caller get identical
validation.

This module mints the ``citation`` ref only (claim + ``cites -> paper``
link + embedded card). The weave's *disposition* edge — ``paper
--cited-in--> dossier(section)`` — is a separate ``store.add_link`` the
weave does itself; it does not belong here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.citation import CitationHandler

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.store.types import ActorSlug

#: Matches CitationHandler.put's create-ack, e.g. "created citation id=42 (...)".
_ID_RE = re.compile(r"\bid=(\d+)\b")


def mint_citation(
    store: Store,
    *,
    claim: str,
    paper_ref_id: int,
    source_handle: str | None = None,
    source_quote: str | None = None,
    verifier_confidence: float | None = None,
    set_by: ActorSlug = "weave",
) -> int:
    """Mint a ``citation`` ref for ``claim``, sourced from ``paper_ref_id``.

    Drives :meth:`CitationHandler.put` in-process (constructed fresh over
    ``store``) so the fabricated-bib-key guard, ``source_handle``
    normalization, the ``card_combined`` embed, and the ``cites`` link all
    run exactly as they do for an agent's ``put(kind='citation', ...)``
    call. Returns the new citation ref's ``id``.

    ``source_handle`` accepts either form the handler already normalizes:
    a universal chunk handle (``pc<id>``, the form
    :func:`precis.quest.claims.own_chunks` hands back) or the canonical
    ``slug~ord`` / ``slug~a..b`` form. When omitted, defaults to the bare
    paper slug — enough for the handler's paper-must-exist check to pass,
    but without a precise chunk anchor; pass an explicit handle when one
    is known.

    ``source_quote`` is the verbatim excerpt the claim is grounded in.
    When omitted, defaults to ``claim`` itself — a weaker guarantee than
    the citation-fill workflow's human/verifier-checked quote, but a
    reasonable fallback for a weave that already knows *which* paper and
    chunk a claim came from but hasn't carried the raw excerpt text
    through. Callers that have the excerpt (e.g. ``own_chunks()``'s
    ``"text"`` field) should pass it explicitly.

    ``set_by`` is accepted for provenance/API symmetry with the weave's
    other code-callable minters but is not yet persisted anywhere —
    ``CitationHandler.put`` has no ``set_by`` slot (``refs.set_by`` is
    left ``NULL`` by ``insert_ref`` for every numeric-ref kind today, not
    just citations). Flagged here rather than invented a bespoke tag
    namespace to carry it; revisit if 6d-2 needs it queryable.

    Raises:
        NotFound: ``paper_ref_id`` does not resolve to a live
            ``kind='paper'`` ref. (A missing/malformed ``source_handle``
            or an unknown paper embedded in an explicitly-passed
            ``source_handle`` still surfaces as the handler's own
            ``BadInput``.)
    """
    paper_ref = store.get_ref(kind="paper", id=paper_ref_id)
    if paper_ref is None or not paper_ref.slug:
        raise NotFound(
            f"mint_citation: paper_ref_id={paper_ref_id!r} does not resolve "
            "to a live kind='paper' ref",
            next=f"get(kind='paper', id={paper_ref_id!r})",
        )

    handler = CitationHandler(hub=Hub(store=store))
    resp = handler.put(
        text=claim,
        source_handle=source_handle or paper_ref.slug,
        source_quote=source_quote or claim,
        verifier_confidence=verifier_confidence,
        link=f"paper:{paper_ref.slug}",
    )

    m = _ID_RE.search(resp.body)
    if m is None:  # pragma: no cover - defensive; put()'s ack always has it
        raise RuntimeError(
            f"mint_citation: could not parse citation id from ack: {resp.body!r}"
        )
    return int(m.group(1))


__all__ = ["mint_citation"]
