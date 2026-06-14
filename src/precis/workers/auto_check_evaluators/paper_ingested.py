"""``type='paper_ingested'`` — wait for a DOI to land + embed.

Resolves to ``True`` when a live ``paper`` ref with the supplied
external identifier exists *and* has at least one embedded chunk
(``chunk_embeddings.status='ok'``). The two conditions matter
separately: a freshly-minted stub passes the first but not the
second; we want to release the leaf only when the consumer can
actually run semantic search against the paper.

Spec
====

```json
{
  "type": "paper_ingested",
  "doi": "10.1234/abcdef"
}
```

``doi`` is the canonical key; arXiv ids work too (pass them as
``arxiv: "<id>"`` instead). Any registered ``ref_identifiers.id_kind``
is accepted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput

if TYPE_CHECKING:
    from precis.store import Store


def evaluate(store: Store, spec: dict[str, Any], **_kw: Any) -> bool | None:
    """Return ``True`` when the paper is ingested + embedded.

    ``None`` is never returned (no "uncertain" state); the leaf
    stays open until the SQL match flips.
    """
    # Accept any registered identifier kind so callers don't have
    # to translate arxiv ids back into DOI form.
    id_kind, id_value = _select_identifier(spec)
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1
              FROM ref_identifiers ri
              JOIN refs r ON r.ref_id = ri.ref_id
             WHERE ri.id_kind = %s
               AND ri.id_value = %s
               AND r.kind = 'paper'
               AND r.deleted_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM chunks c
                     JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                    WHERE c.ref_id = r.ref_id
                      AND ce.status = 'ok'
               )
             LIMIT 1
            """,
            (id_kind, id_value),
        ).fetchone()
    return row is not None


def _select_identifier(spec: dict[str, Any]) -> tuple[str, str]:
    """Pick the identifier kind + value from the spec.

    Single-source: the spec specifies exactly one identifier. We
    probe a small set of known kinds; the first hit wins. Specs that
    name no identifier are rejected with the recovery hint.
    """
    for id_kind in ("doi", "arxiv", "s2", "pubmed"):
        value = spec.get(id_kind)
        if value:
            if not isinstance(value, str):
                raise BadInput(
                    f"paper_ingested.{id_kind} must be a string",
                    next=f"meta.auto_check.{id_kind}='<{id_kind}-value>'",
                )
            return id_kind, value
    raise BadInput(
        "paper_ingested needs an identifier (doi / arxiv / s2 / pubmed)",
        next="meta.auto_check.doi='10.x/y' (or .arxiv='2401.00001')",
    )
