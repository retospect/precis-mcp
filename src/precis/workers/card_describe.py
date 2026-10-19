"""card_describe — LLM narrative ``card_description`` chunks (gr497688, S1).

Non-text refs (structures first; cad/pcb/figure land in slices S2/S3)
carry geometry plus a terse template ``card_combined`` — composition and
counts, no functional narrative — so hybrid search over them is starved.
This pass writes one derived ``card_description`` chunk per ref (ord=-2)
from the card text, the CLaC-style synthetic-narrative fix (pa497686):
describe function, context and application, not only attributes.

Reto's rulings (gr497688 comment 2) this encodes:

* **Sonnet tier for ongoing generation.** The caller injects a
  ``DispatchClient`` bound to ``Tier.BIG`` — no model id pinned here.
* **Regenerate on edit by replacement.** A ref is re-claimed when its
  card text hash moves off the stored ``meta.source_sha``; the write is
  one DELETE+INSERT in a transaction
  (:meth:`ChunkStore._replace_card_chunk`), never an UPDATE, never a
  second chunk.
* **The backfill is not this worker.** The claim only admits
  ``card_combined`` chunks minted after the ``chunk_kinds`` row for
  ``card_description`` was created — that row's ``created_at`` is the
  feature's deploy stamp, so the pass covers new and edited refs and
  never sweeps the pre-existing corpus at Sonnet cost. Existing refs
  are S4's Devin-agent MCP path.

Embedding is the embed worker's job (``card_description`` is not a skip
kind there); this worker never touches an embedder.

Claim/write shape mirrors :mod:`precis.workers.chunk_keywords`: a
single-runner ``pg_try_advisory_xact_lock`` held for the whole pass on a
dedicated connection, claim-commit-first, per-ref work on pooled
connections, writes wrapped in :func:`retry_locked`.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable

from precis.store.advisory import try_xact_advisory_lock
from precis.utils.db_retry import retry_locked
from precis.utils.llm.router import DispatchError

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Bump when the prompt or derivation changes so existing descriptions
#: get regenerated (version mismatch re-claims, same lazy-upgrade idiom
#: as ``chunk_keywords.KEYWORDS_VERSION``).
CARD_DESCRIBE_VERSION = "1.0"

#: Fixed signed-bigint key for the single-runner advisory lock (same
#: idiom as ``chunk_keywords`` — arbitrary constant, namespaced away
#: from its ``"chnk_kw\x00"`` key). ``"crd_dsc\x00"`` mapped signed.
_LOCK_KEY = 0x63_72_64_5F_64_73_63_00 - 2**63

#: Ref kinds this pass describes. S1 covers ``structure``; S2/S3 extend
#: the tuple (cad/pcb, then figure with a vision prompt).
_DESCRIBED_KINDS: tuple[str, ...] = ("structure",)

#: A generation shorter than this is a model miss (refusal fragment,
#: stray whitespace) — count it failed rather than indexing junk.
_MIN_DESC_CHARS = 40

#: Hard cap on the stored description. The prompt asks for ~3 sentences;
#: this only guards against a runaway completion.
_MAX_DESC_CHARS = 4000

_SYSTEM = (
    "You write search-index descriptions for a materials knowledge base. "
    "Given a structure's computed card (title, composition, atom/bond "
    "counts, periodicity, fragment groups, and any author description), "
    "write a plain-text narrative of two to four sentences: what the "
    "structure is, its dimensionality and bonding character, and the "
    "function or application context it plausibly serves. Ground every "
    "claim in the card; mark plausible-but-unmeasured interpretation as "
    "such rather than inventing measured values. Output only the "
    "description — no preamble, no markdown."
)


def claim_refs_without_description(
    conn: Connection, *, limit: int
) -> list[tuple[int, str, str]]:
    """Return up to ``limit`` ``(ref_id, title, card_text)`` rows to describe.

    A ref is claimed when its ``card_combined`` chunk was minted after
    the ``card_description`` kind was registered (the deploy stamp — see
    the module docstring) AND either no ``card_description`` chunk
    exists yet or the stored ``meta.source_sha`` no longer matches the
    card text's sha256 (the card was edited since).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    sql = """
        SELECT r.ref_id, r.title, card.text
          FROM refs r
          JOIN chunk_kinds ck ON ck.slug = 'card_description'
          JOIN chunks card
            ON card.ref_id = r.ref_id
           AND card.chunk_kind = 'card_combined'
           AND card.ord = -1
           AND card.created_at > ck.created_at
          LEFT JOIN chunks d
            ON d.ref_id = r.ref_id
           AND d.chunk_kind = 'card_description'
         WHERE r.kind = ANY(%s)
           AND (
                d.chunk_id IS NULL
             OR (d.meta->>'source_sha') IS DISTINCT FROM
                encode(digest(card.text, 'sha256'), 'hex')
             OR (d.meta->>'version') IS DISTINCT FROM %s
           )
         ORDER BY r.ref_id
         LIMIT %s
           FOR UPDATE OF r SKIP LOCKED
    """
    rows = conn.execute(
        sql, (list(_DESCRIBED_KINDS), CARD_DESCRIBE_VERSION, limit)
    ).fetchall()
    return [(int(r[0]), str(r[1] or ""), str(r[2])) for r in rows]


def describe_card(client: Any, *, title: str, card_text: str) -> tuple[str, str]:
    """Generate one description; return ``(text, model)``.

    ``client`` is the injected ``DispatchClient`` (or a test fake with the
    same ``.complete(messages) -> LlmResult`` shape) — the tier/model pick
    is the caller's, never a literal here.
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": f"Structure: {title}\n\nCard:\n{card_text}",
        },
    ]
    res = client.complete(messages)
    text = (res.text or "").strip()
    if len(text) < _MIN_DESC_CHARS:
        raise RuntimeError(
            f"card_describe: generation too short ({len(text)} chars) for {title!r}"
        )
    return text[:_MAX_DESC_CHARS], str(getattr(res, "model", "") or "")


def write_card_description(
    store: Store,
    ref_id: int,
    *,
    text: str,
    source_sha: str,
    model: str,
) -> None:
    """DELETE+INSERT the ref's one ``card_description`` chunk in its own
    short transaction (the ``_replace_card_combined`` pattern via
    :meth:`ChunkStore._replace_card_chunk`; never UPDATE, never a second
    row). ``meta`` carries the derived provenance: which pass, which
    version, which model, and the card-text hash it describes (the
    re-claim key)."""
    meta = {
        "generator": "card_describe",
        "version": CARD_DESCRIBE_VERSION,
        "model": model,
        "source_sha": source_sha,
    }
    with store.pool.connection() as conn:
        store.chunks._replace_card_chunk(
            conn,
            ref_id=ref_id,
            chunk_kind="card_description",
            text=text,
            meta=meta,
        )
        conn.commit()


def _source_sha(card_text: str) -> str:
    return hashlib.sha256(card_text.encode("utf-8")).hexdigest()


def _run_card_describe_batch(
    store: Store, client: Any, *, batch_size: int
) -> dict[str, int]:
    """Claim + per-ref describe + write. Runs under the caller's
    single-runner advisory lock; every DB access goes through ``store``'s
    pool in short transactions (claim-commit-first, like
    ``chunk_keywords``)."""
    with store.pool.connection() as conn:
        rows = claim_refs_without_description(conn, limit=batch_size)
        conn.commit()
    claimed = len(rows)

    ok = failed = 0
    for ref_id, title, card_text in rows:
        try:
            text, model = describe_card(client, title=title, card_text=card_text)

            def _write(
                ref_id: int = ref_id,
                text: str = text,
                source_sha: str = _source_sha(card_text),
                model: str = model,
            ) -> None:
                write_card_description(
                    store, ref_id, text=text, source_sha=source_sha, model=model
                )

            retry_locked(_write, label=f"card_describe ref_id={ref_id}")
            ok += 1
        except DispatchError as exc:
            # Router-side miss. ``paused`` is the expected, self-clearing
            # backoff (breaker trip / all slots busy) — defer the rest of
            # the batch uncounted rather than failing every row against a
            # down route, same shape as chunk_keywords' EmbedderUnavailable
            # branch. A genuine dispatch failure fails this one row; the
            # ref stays claimable (nothing was written).
            if exc.paused:
                log.warning(
                    "card_describe: dispatch paused (%s); "
                    "deferring %d remaining refs this pass",
                    exc,
                    claimed - ok,
                )
                return {"claimed": 0, "ok": 0, "failed": 0}
            log.exception("card_describe: ref_id=%s dispatch failed", ref_id)
            failed += 1
        except (LockNotAvailable, DeadlockDetected) as exc:
            # Residual contention outlasting retry_locked's budget —
            # unwritten, so it re-claims cleanly next pass (not failed).
            log.warning(
                "card_describe: ref_id=%s deferred after residual lock contention: %s",
                ref_id,
                exc,
            )
        except Exception:
            log.exception("card_describe: ref_id=%s failed", ref_id)
            failed += 1
    return {"claimed": claimed, "ok": ok, "failed": failed}


def run_card_describe_pass(
    store: Store, client: Any, *, batch_size: int = 16
) -> dict[str, int]:
    """One pass over the card_describe queue, single-runner across the
    fleet — same lock shape as :func:`run_chunk_keywords_pass` (the
    pass runs on every system-profile host on the same tick; the
    transaction-scoped advisory lock keeps the whole pass on one host's
    backend, pgbouncer-safe). A lock-acquire miss is a graceful
    ``claimed=0`` skip."""
    idle: dict[str, int] = {"claimed": 0, "ok": 0, "failed": 0}
    if not store.dsn:
        return idle
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle
        return _run_card_describe_batch(store, client, batch_size=batch_size)


__all__ = [
    "CARD_DESCRIBE_VERSION",
    "claim_refs_without_description",
    "describe_card",
    "run_card_describe_pass",
    "write_card_description",
]
