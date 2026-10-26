"""Re-home a stored PDF between refs — the shared primitives behind
``edit(kind='paper', args={'move_pdf_to': …})`` / ``{'attach_pdf': …}``
and the ``pdf_attach`` job (gr517423 slice 5).

Two doors, one write:

- ``move_pdf_to`` re-homes a sha from a *bodiless* donor ref — the repair
  for a file parked on the wrong record when its body already went (a
  ``keep_pdf`` drop, a dedup fold). A donor that still has a body takes
  ``move_body_to`` instead: body and PDF move together there.
- ``attach_pdf`` adopts a ``pdfs`` row no live ref owns — the orphan a
  plain ``drop_body`` leaves behind.

Both land the same one-transaction move (:func:`apply_pdf_move`): the
donor's pointer columns clear, the ``pdf_sha256`` identifier row
re-homes onto the target (minted when absent), the target's
``refs.pdf_sha256``/``pdf_role`` set, ``pdf_moved`` ref_events journal
on both refs, and the caller then queues ``paper_reextract`` so the
target's body comes from the moved file itself.

The identity gate is the same :func:`pdf_identity.identity_verdict`
``paper_pull``/``paper_reextract`` run: the file must look like the
*target*'s title/DOI before anything moves. The edit doors can only
judge text they already have — chunks the donor stored on the file's
leading pages (:func:`leading_texts`); when none exist the MCP layer
never touches bytes, so the verdict is deferred to the ``pdf_attach``
job, which reads the file with ``read_leading_pages`` on a corpus
host and applies the same write only on ``doi``/``title``.
"""

from __future__ import annotations

from typing import Any

from precis.ingest.pdf_identity import IDENTITY_PAGES, identity_verdict
from precis.store.revision_context import trust_door

__all__ = [
    "apply_pdf_move",
    "leading_texts",
    "live_pdf_sha_owner",
    "ref_identity",
]


def leading_texts(
    conn: Any, ref_id: int, *, pages: int = IDENTITY_PAGES
) -> list[str] | None:
    """The text the ref's stored chunks carry for the file's first
    ``pages`` pages — the identity evidence an edit door can judge
    without reading bytes.

    Per page, live ``ord >= 0`` chunks win; a page whose chunks are all
    retired (a re-extracted/re-ingested donor keeps them as history)
    still describes the same file, so they stand in. ``None`` when no
    chunk addresses the leading pages at all — the verdict then belongs
    to the ``pdf_attach`` job's ``read_leading_pages``.
    """
    rows = conn.execute(
        "SELECT page_first, text, (retired_at IS NULL) AS live "
        "FROM chunks WHERE ref_id = %s AND ord >= 0 "
        "AND page_first BETWEEN 1 AND %s "
        "ORDER BY page_first, (retired_at IS NULL) DESC, ord",
        (ref_id, pages),
    ).fetchall()
    by_page: dict[int, dict[str, list[str]]] = {}
    for page, text, live in rows:
        slot = by_page.setdefault(int(page), {"live": [], "retired": []})
        slot["live" if live else "retired"].append(str(text))
    texts = [
        "\n".join(parts["live"] or parts["retired"])
        for _, parts in sorted(by_page.items())
    ]
    return texts or None


def ref_identity(conn: Any, ref_id: int) -> tuple[str, str | None]:
    """``(title, doi)`` for the identity gate — the ref's display title
    and first ``doi`` identifier value."""
    row = conn.execute(
        "SELECT r.title, (SELECT d.id_value FROM ref_identifiers d "
        " WHERE d.ref_id = r.ref_id AND d.id_kind = 'doi' LIMIT 1) "
        "FROM refs r WHERE r.ref_id = %s",
        (ref_id,),
    ).fetchone()
    if row is None:
        return "", None
    return str(row[0] or ""), (str(row[1]) if row[1] else None)


def live_pdf_sha_owner(conn: Any, sha: str) -> int | None:
    """The live ref that owns ``sha`` — via the ``refs.pdf_sha256``
    pointer or a ``pdf_sha256`` identifier row — or ``None`` (the sha is
    an orphan ``attach_pdf`` may adopt)."""
    row = conn.execute(
        "SELECT r.ref_id FROM refs r WHERE r.pdf_sha256 = %s "
        "AND r.retired_at IS NULL "
        "UNION ALL "
        "SELECT ri.ref_id FROM ref_identifiers ri "
        "JOIN refs r ON r.ref_id = ri.ref_id "
        "WHERE ri.id_kind = 'pdf_sha256' AND ri.id_value = %s "
        "AND r.retired_at IS NULL LIMIT 1",
        (sha, sha),
    ).fetchone()
    return None if row is None else int(row[0])


@trust_door("computed")
def apply_pdf_move(
    conn: Any,
    *,
    store: Any,
    sha: str,
    target_id: int,
    donor_id: int | None,
    verdict: str,
    reason: str,
    source: str,
) -> dict[str, Any]:
    """The one-transaction re-home both doors share.

    Caller has already proven (and re-checked under its row locks): the
    sha is attached to ``donor_id`` / is an adoptable orphan, the target
    is live+bodiless with no ``pdf_sha256``, and ``verdict`` is
    ``doi``/``title``. Clears the donor's pointer columns, re-homes (or
    mints) the identifier row onto the target, sets the target's
    pointer, and journals ``pdf_moved`` on both refs. ``pdf_pages``
    stays NULL on the target — the numbered span belongs to an
    extraction the queued ``paper_reextract`` produces.

    Returns ``{"from": <prior owner ref_id|None>, "to": target_id}``
    for the ack.
    """
    moved_from = donor_id
    if donor_id is not None:
        conn.execute(
            "UPDATE refs SET pdf_sha256 = NULL, pdf_pages = NULL, "
            "pdf_role = NULL, updated_at = now() WHERE ref_id = %s",
            (donor_id,),
        )
    else:
        # attach: the identifier row may still name a retired former
        # owner — record it for the event before the upsert re-homes it.
        row = conn.execute(
            "SELECT ref_id FROM ref_identifiers "
            "WHERE id_kind = 'pdf_sha256' AND id_value = %s",
            (sha,),
        ).fetchone()
        moved_from = int(row[0]) if row else None
    conn.execute(
        "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, source) "
        "VALUES ('pdf_sha256', %s, %s, %s) "
        "ON CONFLICT (id_kind, id_value) DO UPDATE SET "
        "ref_id = EXCLUDED.ref_id, source = EXCLUDED.source",
        (sha, target_id, source),
    )
    conn.execute(
        "UPDATE refs SET pdf_sha256 = %s, "
        "pdf_role = COALESCE(pdf_role, 'main'), updated_at = now() "
        "WHERE ref_id = %s",
        (sha, target_id),
    )
    payload = {
        "sha": sha,
        "from": moved_from,
        "to": target_id,
        "verdict": verdict,
        "reason": reason,
    }
    store.append_event(
        target_id, source=source, event="pdf_moved", payload=payload, conn=conn
    )
    if moved_from is not None and moved_from != target_id:
        store.append_event(
            moved_from,
            source=source,
            event="pdf_moved",
            payload=payload,
            conn=conn,
        )
    return {"from": moved_from, "to": target_id}


def target_gate_verdict(
    texts: list[str], *, title: str, doi: str | None
) -> tuple[str, str | None]:
    """Thin alias over :func:`identity_verdict` so the door and the job
    name one entry point for the gate."""
    return identity_verdict(texts, title=title, doi=doi)
