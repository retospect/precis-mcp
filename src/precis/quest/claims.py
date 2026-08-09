"""Claims-v0 extractor — a paper's own assertions, grounded to source chunks.

Design: ``docs/backlog/paper-writing-pipeline.md`` §Claims. **"A claim already
exists: the ``citation`` kind."** ``handlers/citation.py`` already stores claim
text + ``source_handle`` + ``source_quote`` + verifier fields and embeds the
claim. What's missing is the *extractor* that turns a paper's own contribution
into candidate claims in the first place.

**v0 = inline, no infra.** This module is a callable — ``extract_claims``
returns claims in memory; it does not mint ``citation`` rows and is not a
background pass (that's the v1 promotion the design defers). The rung-6d weave
consumes this function's output directly and does the citation-minting +
verification itself.

Selector: ``own_chunks`` reads the ``ROLE3:own`` chunk tags controlled chunk tagging's
``classify`` cascade already writes (``workers/classify.py``) — "the paper's
own contribution" is exactly the signal claims need, and it's sparse by
design (most of a paper is background/furniture, not asserted here). A paper
never run through the cascade, or one with no ``own`` paragraphs, yields no
chunks and therefore no claims — the caller (the weave) falls back to
abstract composition for it.

Model call shape mirrors ``workers/classify_topics.py`` exactly: an
injectable ``client`` (``client.complete(messages) -> SimpleNamespace(text=...)``,
same shape ``LlmClient`` exposes), a prompt builder, and the same
robust ``_extract_json`` parse-or-``None`` approach. Which tier/model backs
``client`` is the weave-tick's concern (rung 6e), not this module's.
"""

from __future__ import annotations

import json
from typing import Any

from precis.utils import handle_registry

_SYS = (
    "You are a precise scientific claim extractor. Reply with ONLY the "
    "requested JSON array, no prose."
)

#: Cap the excerpt fed per chunk — a claim is a sentence, not a re-read of
#: the whole paragraph; keeps the prompt small even for a long ``own`` chunk.
_EXCERPT_CHARS = 800


def _extract_json(text: str) -> list[Any] | None:
    """Parse ``text`` as a JSON list, tolerating surrounding prose.

    Mirrors ``workers/classify_topics.py``'s ``_extract_json``, but the
    extractor's payload shape is a JSON *array* of claim objects (not a
    JSON object), so this looks for ``[`` / ``]`` instead of ``{`` / ``}``.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        pass
    a, b = text.find("["), text.rfind("]")
    if 0 <= a < b:
        try:
            parsed = json.loads(text[a : b + 1])
            return parsed if isinstance(parsed, list) else None
        except Exception:
            return None
    return None


def own_chunks(store: Any, paper_ref_id: int) -> list[dict[str, Any]]:
    """The ``ROLE3:own`` selector — this paper's own-contribution chunks.

    Returns ``[{"ord": int, "handle": str, "text": str}, ...]`` in document
    order. ``handle`` is the computed ``pc<chunk_id>`` universal handle
     — chunks carry no stored ``handle`` column, so it's formed
    from ``chunk_id`` via ``handle_registry.format_handle``. Empty when the
    paper has no ``ROLE3:own`` tags (unclassified, or genuinely no
    own-contribution paragraphs).
    """
    sql = """
        SELECT c.chunk_id, c.ord, c.text
        FROM chunks c
        JOIN chunk_tags ct ON ct.chunk_id = c.chunk_id
        JOIN tags t ON t.tag_id = ct.tag_id
        WHERE c.ref_id = %(ref_id)s
          AND c.retired_at IS NULL
          AND t.namespace = 'ROLE3'
          AND t.value = 'own'
        ORDER BY c.ord
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, {"ref_id": paper_ref_id}).fetchall()
    return [
        {
            "ord": int(r[1]),
            "handle": handle_registry.format_handle("paper", int(r[0]), chunk=True),
            "text": str(r[2] or ""),
        }
        for r in rows
    ]


def _build_prompt(chunks: list[dict[str, Any]]) -> str:
    lines = [
        "Below are numbered excerpts from a paper's OWN contribution "
        "(background/boilerplate paragraphs have already been filtered out).",
        "Extract the paper's own novel assertions/claims as a JSON list.",
        "A handful of high-value claims, not an exhaustive paraphrase — skip "
        "boilerplate ('we conclude', 'future work') and anything not a "
        "concrete assertion. Each claim must be traceable to exactly one "
        "excerpt below.",
        "",
    ]
    for i, chunk in enumerate(chunks):
        lines.append(f"[{i}] {chunk['text'][:_EXCERPT_CHARS]}")
        lines.append("")
    lines.append(
        'Return JSON: [{"claim": "<one-sentence assertion>", "source": <excerpt '
        "number>}, ...]. Empty list if no genuine own-contribution claims."
    )
    return "\n".join(lines)


def extract_claims(
    store: Any, client: Any, paper_ref_id: int, *, max_chunks: int = 12
) -> list[dict[str, Any]]:
    """Extract the paper's own claims from its ``ROLE3:own`` chunks.

    Returns ``[{"text": str, "source_ord": int, "source_handle": str}, ...]``
    — no citation minting, no DB writes. Empty (and ``client`` never called)
    when the paper has no ``ROLE3:own`` chunks; empty on unparseable model
    output. A returned claim whose ``source`` excerpt index is out of range
    is dropped rather than raising.
    """
    chunks = own_chunks(store, paper_ref_id)[:max_chunks]
    if not chunks:
        return []

    try:
        out = client.complete(
            [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": _build_prompt(chunks)},
            ]
        )
    except Exception:
        return []

    parsed = _extract_json(out.text)
    if parsed is None:
        return []

    claims: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        text = item.get("claim")
        source = item.get("source")
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(source, int) or isinstance(source, bool):
            continue
        if source < 0 or source >= len(chunks):
            continue
        chunk = chunks[source]
        claims.append(
            {
                "text": text.strip(),
                "source_ord": chunk["ord"],
                "source_handle": chunk["handle"],
            }
        )
    return claims


__all__ = ["extract_claims", "own_chunks"]
