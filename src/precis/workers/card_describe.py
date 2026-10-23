"""card_describe — LLM narrative ``card_description`` chunks (gr497688, S1-S3).

Non-text refs (structures first; cad/pcb/figure land in slices S2/S3)
carry geometry plus a terse template ``card_combined`` — composition and
counts, no functional narrative — so hybrid search over them is starved.
This pass writes one derived ``card_description`` chunk per ref (ord=-2)
from the card text, the CLaC-style synthetic-narrative fix (pa497686):
describe function, context and application, not only attributes.

S3 (figure): a figure has no ``card_combined`` — its grounding is the
``figure_node`` SVG source rasterised to PNG (the shared ``_svg_to_png``
in ``utils/figure_source``, not a second rasteriser) and sent through the
router as an image content block, grounded in the caption (title) plus
the ``figure_notes`` text. Same watermark: only figures whose source node
was minted after the ``card_description`` kind row landed are claimed —
existing canvases are the Devin-agent backfill path.

S4 (budget): a per-day dollar cap on this pass's LLM spend —
``PRECIS_CARD_DESCRIBE_DAILY_USD`` (default per
``docs/conventions/thresholds.md``'s $25 checkpoint), metered off
``llm_call_log`` ``source='card_describe'`` over the last 24 h. Over cap
the pass defers with ``reason='budget'``, the same shape as a
``cli_unavailable`` pause. ``precis card-describe --dry-run`` prints the
claimable count per kind without spending.

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
connections, writes wrapped in :func:`retry_locked`. The claim carries a
claim-time attempt lease (:mod:`precis.workers.ref_lease`, stamped in the
claim transaction): this pass's only "done" marker is the success-written
chunk, so without the brake a persistently-failing ref — e.g. a model
reply whose JSON never carries ``description`` (gr508688) — is re-claimed
and re-billed every sweep instead of once per cooldown.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable

from precis.store.advisory import try_xact_advisory_lock
from precis.utils.db_retry import retry_locked
from precis.utils.figure_source import _svg_to_png
from precis.utils.llm.router import DispatchError
from precis.workers import ref_lease

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

#: :mod:`precis.workers.ref_lease` marker namespace — the claim-time
#: attempt lease that brakes a failing ref for
#: :data:`ref_lease.ATTEMPT_COOLDOWN_MIN` (gr508688).
_LEASE_NS = "CARDDESCRIBE"

#: Ref kinds this pass describes — S1 ``structure``, S2 ``cad`` + ``pcb``,
#: S3 ``figure`` (vision prompt). The card kinds' grounding is their one
#: ``card_combined`` chunk (``handlers/<kind>.py``'s card builder —
#: ``card_text`` / ``CadHandler._card_text`` / ``_pcb_ops._pcb_card_text``);
#: a figure's grounding is its ``figure_node`` SVG source rasterised to PNG.
_DESCRIBED_KINDS: tuple[str, ...] = ("structure", "cad", "pcb", "figure")

#: Per-kind display label for the prompt's subject line.
_KIND_LABEL: dict[str, str] = {
    "structure": "atomistic structure",
    "cad": "parametric CAD design",
    "pcb": "PCB design",
    "figure": "figure",
}

#: Drawable-element check on a figure's SVG source — the birth canvas is a
#: comment-only document (``figure_source._DRAWABLE_RE``); a figure with no
#: drawable element has nothing for vision to describe, so the claim skips
#: it until real markup lands (the source edit UPDATEs ``figure_node`` in
#: place — the row stays watermark-eligible and re-claims then).
_FIGURE_DRAWABLE_SQL = (
    "<([a-zA-Z0-9_]+:)?(path|rect|circle|ellipse|line|polyline|polygon|text"
    "|image|use)[ />]"
)

#: Daily LLM spend cap for this pass (S4). Env override
#: ``PRECIS_CARD_DESCRIBE_DAILY_USD``; the default matches the $25
#: paid-run checkpoint in ``docs/conventions/thresholds.md`` — the global
#: budget breaker still applies on top (this is a per-pass guard, not a
#: replacement).
_DEFAULT_DAILY_USD = 25.0

#: ``llm_call_log.source`` label the CLI pass registers under — the spend
#: query keys on it.
_SOURCE = "card_describe"

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
    "such rather than inventing measured values. No markdown. Respond "
    "with EXACTLY ONE JSON object, nothing else: "
    '{"description": "<the narrative>"}'
)

#: Vision variant for figures (S3): the grounding is the rendered image
#: itself, with the caption (ref title) + ``figure_notes`` as context.
_FIGURE_SYSTEM = (
    "You write search-index descriptions for a materials knowledge base. "
    "Given a figure's rendered image plus its caption and implementation "
    "notes, write a plain-text narrative of two to four sentences: what "
    "the figure depicts and communicates (its marks, axes or shapes), and "
    "the function or application context it plausibly serves. Ground every "
    "claim in the image and the supplied text; mark plausible-but-unshown "
    "interpretation as such rather than inventing values. No markdown. "
    "Respond with EXACTLY ONE JSON object, nothing else: "
    '{"description": "<the narrative>"}'
)

#: The card-kind claim leg (structure/cad/pcb): grounding is the ref's one
#: ``card_combined`` chunk at ord=-1. The ref_lease exclusion brakes a
#: leased ref (mid-work or post-failure — gr508688).
_CARD_CLAIM_SQL = """
    SELECT r.ref_id, r.kind, r.title, card.text
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
     WHERE r.kind = ANY(%(kinds)s)
       AND (
            d.chunk_id IS NULL
         OR (d.meta->>'source_sha') IS DISTINCT FROM
            encode(digest(card.text, 'sha256'), 'hex')
         OR (d.meta->>'version') IS DISTINCT FROM %(ver)s
       )
""" + ref_lease.exclude_clause("r.ref_id", "lease_ns")

#: The figure claim leg (S3): grounding is the ref's FIRST live
#: ``figure_node`` chunk — the SVG source document, ``figure_node`` being
#: what a figure mints instead of a ``card_combined``. Same watermark and
#: re-claim ident (``meta.source_sha`` hashes the SVG text, so an
#: ``edit(text=…)`` on the canvas re-claims); an empty birth canvas
#: (no drawable element — ``_FIGURE_DRAWABLE_SQL``) is never claimed.
_FIGURE_CLAIM_SQL = """
    SELECT r.ref_id, r.kind, r.title, src.text
      FROM refs r
      JOIN chunk_kinds ck ON ck.slug = 'card_description'
      JOIN LATERAL (
          SELECT c.text, c.created_at
            FROM chunks c
           WHERE c.ref_id = r.ref_id
             AND c.chunk_kind = 'figure_node'
             AND c.retired_at IS NULL
           ORDER BY c.ord, c.chunk_id
           LIMIT 1
      ) src ON src.created_at > ck.created_at
      LEFT JOIN chunks d
        ON d.ref_id = r.ref_id
       AND d.chunk_kind = 'card_description'
     WHERE r.kind = 'figure'
       AND src.text ~* %(drawable)s
       AND (
            d.chunk_id IS NULL
         OR (d.meta->>'source_sha') IS DISTINCT FROM
            encode(digest(src.text, 'sha256'), 'hex')
         OR (d.meta->>'version') IS DISTINCT FROM %(ver)s
       )
""" + ref_lease.exclude_clause("r.ref_id", "lease_ns")

#: Shared trailer for both claim legs: oldest-first, claim-count cap, and
#: the row lock (claim-commit-first — same idiom as ``chunk_keywords``).
_CLAIM_TAIL = (
    "     ORDER BY r.ref_id\n     LIMIT %(limit)s\n       FOR UPDATE OF r SKIP LOCKED\n"
)


def claim_refs_without_description(
    conn: Connection, *, limit: int
) -> list[tuple[int, str, str, str]]:
    """Return up to ``limit`` ``(ref_id, kind, title, grounding)`` rows to
    describe — ``grounding`` is the ``card_combined`` text for the card
    kinds and the ``figure_node`` SVG source for ``figure``.

    A ref is claimed when its grounding chunk was minted after the
    ``card_description`` kind was registered (the deploy stamp — see the
    module docstring) AND either no ``card_description`` chunk exists yet
    or the stored ``meta.source_sha`` no longer matches the grounding
    text's sha256 (the card/figure was edited since). A ref under an
    unexpired claim-time attempt lease (:mod:`precis.workers.ref_lease`)
    is excluded — it is mid-work or braked after a failure.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    lease_ns = ref_lease.attempt_ns(_LEASE_NS)
    rows: list[tuple[Any, ...]] = []
    card_kinds = [k for k in _DESCRIBED_KINDS if k != "figure"]
    if card_kinds:
        rows.extend(
            conn.execute(
                _CARD_CLAIM_SQL + _CLAIM_TAIL,
                {
                    "kinds": card_kinds,
                    "ver": CARD_DESCRIBE_VERSION,
                    "lease_ns": lease_ns,
                    "limit": limit,
                },
            ).fetchall()
        )
    if "figure" in _DESCRIBED_KINDS:
        rows.extend(
            conn.execute(
                _FIGURE_CLAIM_SQL + _CLAIM_TAIL,
                {
                    "drawable": _FIGURE_DRAWABLE_SQL,
                    "ver": CARD_DESCRIBE_VERSION,
                    "lease_ns": lease_ns,
                    "limit": limit,
                },
            ).fetchall()
        )
    rows.sort(key=lambda r: int(r[0]))
    return [(int(r[0]), str(r[1]), str(r[2] or ""), str(r[3])) for r in rows[:limit]]


def claimable_counts(conn: Connection) -> dict[str, int]:
    """Per-kind count of claimable refs right now — the ``--dry-run`` view
    (``precis card-describe``). Same claim predicates as
    :func:`claim_refs_without_description`, unbounded and lock-free."""
    counts: dict[str, int] = {}
    legs: list[tuple[str, dict[str, Any]]] = []
    card_kinds = [k for k in _DESCRIBED_KINDS if k != "figure"]
    if card_kinds:
        legs.append(
            (
                _CARD_CLAIM_SQL.replace(
                    "SELECT r.ref_id, r.kind, r.title, card.text", "SELECT r.kind"
                ),
                {
                    "kinds": card_kinds,
                    "ver": CARD_DESCRIBE_VERSION,
                    "lease_ns": ref_lease.attempt_ns(_LEASE_NS),
                },
            )
        )
    if "figure" in _DESCRIBED_KINDS:
        legs.append(
            (
                _FIGURE_CLAIM_SQL.replace(
                    "SELECT r.ref_id, r.kind, r.title, src.text", "SELECT r.kind"
                ),
                {
                    "drawable": _FIGURE_DRAWABLE_SQL,
                    "ver": CARD_DESCRIBE_VERSION,
                    "lease_ns": ref_lease.attempt_ns(_LEASE_NS),
                },
            )
        )
    for sql, params in legs:
        for kind, n in conn.execute(
            f"SELECT t.kind, count(*) FROM ({sql}) t GROUP BY t.kind",
            params,
        ).fetchall():
            counts[str(kind)] = int(n)
    return counts


def _extract_description(res: Any) -> str:
    """Pull the narrative out of the reply. ``CLAUDE_P`` demands a JSON
    block (``res.data``); the local/OpenAI wires return the same JSON as
    text, which is parsed here. A reply with no JSON at all is taken as
    plain prose (a model that ignored the JSON instruction); one that looks
    like JSON but yields no ``description`` (truncated, malformed, another
    shape) raises so the card is retried rather than storing junk."""
    from precis.utils.claude_p import _parse_last_json_block

    text = (res.text or "").strip()
    data = getattr(res, "data", None)
    if not isinstance(data, dict):
        data = _parse_last_json_block(text)
    desc = data.get("description") if isinstance(data, dict) else None
    if isinstance(desc, str):
        return desc.strip()
    if data is not None or "{" in text:
        raise ValueError("card_describe reply carried no usable 'description' field")
    return text


def _figure_notes(store: Store, ref_id: int) -> str:
    """The figure's ``figure_notes`` text (the model's design log), or ""
    when none — context grounding for the vision prompt."""
    for c in store.drafts.reading_order(ref_id, kind="figure"):
        if c.chunk_kind == "figure_notes":
            return str(c.text or "")
    return ""


def describe_card(
    client: Any,
    *,
    kind: str,
    title: str,
    card_text: str,
    image: bytes | None = None,
    notes: str = "",
) -> tuple[str, str]:
    """Generate one description; return ``(text, model)``.

    ``client`` is the injected ``DispatchClient`` (or a test fake with the
    same ``.complete(messages) -> LlmResult`` shape) — the tier/model pick
    is the caller's, never a literal here.

    ``image`` is the PNG raster for ``kind='figure'`` (required there):
    it rides the user message as an Anthropic ``image`` content block —
    ``DispatchClient`` lifts it onto ``LlmRequest.images`` so the
    Sonnet-class rungs attach it to the wire. ``notes`` is the figure's
    ``figure_notes`` text, grounding context next to the caption.
    """
    if kind == "figure":
        if not image:
            raise ValueError("figure describe needs a rendered PNG")
        content: Any = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(image).decode("ascii"),
                },
            },
            {
                "type": "text",
                "text": f"Figure: {title}\n\nNotes:\n{notes or '(none)'}",
            },
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _FIGURE_SYSTEM},
            {"role": "user", "content": content},
        ]
    else:
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{_KIND_LABEL.get(kind, kind).capitalize()}: {title}\n\n"
                    f"Card:\n{card_text}"
                ),
            },
        ]
    res = client.complete(messages)
    text = _extract_description(res)
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
    re-claim key). Success also clears the ref's attempt lease in the
    SAME transaction, so a legitimate re-trigger (version bump, edited
    card) is never blocked by a stale lease."""
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
        ref_lease.clear_attempt(store, ref_id, _LEASE_NS, conn=conn)
        conn.commit()


def _source_sha(card_text: str) -> str:
    return hashlib.sha256(card_text.encode("utf-8")).hexdigest()


def _daily_cap_usd() -> float:
    """``PRECIS_CARD_DESCRIBE_DAILY_USD`` or the $25 default; a malformed
    value reads as the default, never as unlimited."""
    raw = os.environ.get("PRECIS_CARD_DESCRIBE_DAILY_USD")
    if not raw:
        return _DEFAULT_DAILY_USD
    try:
        return float(raw)
    except ValueError:
        log.warning(
            "card_describe: bad PRECIS_CARD_DESCRIBE_DAILY_USD=%r — using %.2f",
            raw,
            _DEFAULT_DAILY_USD,
        )
        return _DEFAULT_DAILY_USD


def _daily_spend_usd(conn: Connection) -> float:
    """USD this pass has billed over the last 24 h — ``llm_call_log``
    ``source='card_describe'``, billable rows only (``placement`` 'local'
    is priced-but-free, matching the ``llm_call_log_billable_ts_idx``
    predicate)."""
    row = conn.execute(
        """
        SELECT coalesce(sum(cost_usd), 0)
          FROM llm_call_log
         WHERE source = %s
           AND ts > now() - interval '24 hours'
           AND cost_usd IS NOT NULL
           AND placement IS DISTINCT FROM 'local'
        """,
        (_SOURCE,),
    ).fetchone()
    return float(row[0]) if row else 0.0


def _run_card_describe_batch(
    store: Store, client: Any, *, batch_size: int
) -> dict[str, Any]:
    """Claim + per-ref describe + write. Runs under the caller's
    single-runner advisory lock; every DB access goes through ``store``'s
    pool in short transactions (claim-commit-first, like
    ``chunk_keywords``)."""
    with store.pool.connection() as conn:
        rows = claim_refs_without_description(conn, limit=batch_size)
        # Stamp the claim-time attempt lease for every claimed ref in the
        # SAME transaction as the claim SELECT, before it commits (the
        # bib_parse shape — gripe 202116) — the lease is the durable
        # brake, the FOR UPDATE row lock dies with this commit.
        for claimed_id, *_ in rows:
            ref_lease.stamp_attempt(store, claimed_id, _LEASE_NS, conn=conn)
        conn.commit()
    claimed = len(rows)

    ok = failed = 0
    for i, (ref_id, kind, title, card_text) in enumerate(rows):
        try:
            if kind == "figure":
                # S3: describe the RENDERED figure, not its markup — reuse
                # the export path's rasteriser, never a second one.
                png = _svg_to_png(card_text)
                if png is None:
                    raise RuntimeError("figure_node SVG failed to rasterise")
                text, model = describe_card(
                    client,
                    kind=kind,
                    title=title,
                    card_text=card_text,
                    image=png,
                    notes=_figure_notes(store, ref_id),
                )
            else:
                text, model = describe_card(
                    client, kind=kind, title=title, card_text=card_text
                )

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
                # ``cli_unavailable`` names the host-config defect
                # (gr500371) so the defer is distinguishable in logs from a
                # self-clearing window pause.
                reason = "cli_unavailable" if exc.cli_unavailable else str(exc)
                log.warning(
                    "card_describe: dispatch paused (%s); "
                    "deferring %d remaining refs this pass",
                    reason,
                    claimed - ok,
                )
                # Rows never attempted hand their leases back so they
                # re-claim next pass instead of sitting out the cooldown
                # (the bib_parse RateLimited shape); the paused ref keeps
                # its brake.
                with store.pool.connection() as conn:
                    for skipped_id, *_ in rows[i + 1 :]:
                        ref_lease.clear_attempt(store, skipped_id, _LEASE_NS, conn=conn)
                    conn.commit()
                return {
                    "claimed": 0,
                    "ok": 0,
                    "failed": 0,
                    "deferred": reason,
                }
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
) -> dict[str, Any]:
    """One pass over the card_describe queue, single-runner across the
    fleet — same lock shape as :func:`run_chunk_keywords_pass` (the
    pass runs on every system-profile host on the same tick; the
    transaction-scoped advisory lock keeps the whole pass on one host's
    backend, pgbouncer-safe). A lock-acquire miss is a graceful
    ``claimed=0`` skip."""
    idle: dict[str, Any] = {"claimed": 0, "ok": 0, "failed": 0}
    if not store.dsn:
        return idle
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle
        # S4 daily cap: read this pass's own 24 h spend first — over cap
        # the whole pass defers (reason ``budget``, the same defer shape
        # as a ``cli_unavailable`` pause) rather than claiming rows it
        # must not spend on.
        cap = _daily_cap_usd()
        with store.pool.connection() as conn:
            spent = _daily_spend_usd(conn)
        if spent >= cap:
            log.warning(
                "card_describe: budget — 24 h spend $%.2f ≥ cap $%.2f "
                "(PRECIS_CARD_DESCRIBE_DAILY_USD); deferring pass",
                spent,
                cap,
            )
            return {**idle, "deferred": "budget"}
        return _run_card_describe_batch(store, client, batch_size=batch_size)


__all__ = [
    "CARD_DESCRIBE_VERSION",
    "claim_refs_without_description",
    "claimable_counts",
    "describe_card",
    "run_card_describe_pass",
    "write_card_description",
]
