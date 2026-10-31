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

Claim/write shape mirrors :mod:`precis.workers.chunk_keywords`, minus
its whole-pass hold: the single-runner ``pg_try_advisory_xact_lock``
scopes only the decide-and-claim transaction (gr524231 — held for the
whole pass it kept a backend idle-in-transaction across every LLM call,
and prod lanes queued behind it). Per-ref mutual exclusion for the rest
of the pass is the claim-time attempt lease
(:mod:`precis.workers.ref_lease`, stamped in the claim transaction,
claim-commit-first): this pass's only "done" marker is the
success-written chunk, so without the brake a persistently-failing ref —
e.g. a model reply whose JSON never carries ``description`` (gr508688) —
is re-claimed and re-billed every sweep instead of once per cooldown.
Per-ref work runs on pooled connections, writes wrapped in
:func:`retry_locked`.

gr543828 (the ``claude_p`` chat-agent failure, ~50% of calls): the
``claude_p`` rung drops the system prompt outright —
:class:`~precis.utils.llm.router.DispatchClient` folds the ``system``
turns onto ``LlmRequest.system_prompt``, which ``call_claude_p`` never
sends — so the JSON contract that lived only in :data:`_SYSTEM` never
reached the model. A bare ``<label>: <title> / Card:`` prompt met
Claude Code's own agentic persona and the replies came back as chat
("no file attached", clarification asks) instead of JSON. The contract
now lives in the user message — the one channel every transport reads —
with the schema and a minimal example last; :data:`DESCRIBE_EXTRA_ARGS`
pins ``--max-turns 1`` so the rung stays one-shot; and a reply that
still yields no usable ``description`` buys exactly one retry with a
JSON-only nudge (:data:`_JSON_NUDGE`) before the ref fails — never a
loop. Bare prose is no longer stored as the description: after two
explicit JSON-only asks it is a contract break, not a narrative.

The lease's value counts consecutive attempts (gr523669): once a ref
reaches :data:`_PARSE_FAIL_BENCH` failed attempts with no success since,
the pass re-stamps it under :data:`_BENCH_COOLDOWN_MIN` — the repeat-
offender stops re-claiming for a day instead of an hour, so a reply
shape the parser can never accept stops burning the daily spend cap.
The same gripe's remainder: the model *did* emit ``description`` — the
replies were truncated mid-string (an output cap, or the model's own
limit on the uncapped claude lane). The pass now pins
:data:`DESCRIBE_MAX_TOKENS` on its dispatch so a local/openai-compat
rung stops inheriting the 220-token summarizer cap, and the reply parse
salvages a truncated ``description`` (recover the string to the cut,
trim to the last complete sentence, ≥ :data:`_MIN_SALVAGE_CHARS` or
retry) — each salvage logged at info so prod can count it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
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

#: A description recovered from a truncated reply must still read as a
#: real narrative — a fragment below this (or a value the cut reached
#: before it started) is refused and the ref retries, never indexed.
_MIN_SALVAGE_CHARS = 80

#: Completion cap the caller pins on the describe dispatch
#: (``cli._worker_passes`` / ``cli.card_describe``). Unpinned, a
#: local/openai-compat rung inherits ``LlmConfig``'s 220-token summarizer
#: default, which can cut the 2–4-sentence narrative the prompt asks for
#: mid-string — the truncated-JSON shape prod logged ~5/hour (gr523669).
#: 1600 leaves the instructed length wide margin plus room for a
#: reasoning rung's thinking, matching
#: ``llm_summarize.SUMMARIZE_DISPATCH_MAX_TOKENS``'s fix for the same
#: defect class (gr489474). Inert on the claude lanes: ``claude -p`` has
#: no generation-time cap and ``claude_agent`` truncates post-hoc only —
#: there the salvage below is the mitigation, not the cap. A pinned cap
#: wins over a chain rung's ``max_tokens``, so keep it at or above the
#: largest rung value in ``llm.op.card_describe`` / ``llm.chain.big``.
DESCRIBE_MAX_TOKENS = 1600

#: CLI flags this source pins on its dispatches
#: (``DispatchClient.extra_args`` → ``LlmRequest.extra_args`` → the
#: ``claude_*`` providers; the other transports ignore them).
#: ``--max-turns`` is the same flag the agentic lane passes
#: (``utils/claude_agent.py``); ``1`` caps the ``claude -p`` rung at a
#: single turn so it cannot go agentic (gr543828). ``--disallowedTools *``
#: already makes a second turn useless, but the cap makes the one-shot
#: contract a CLI guarantee, not only a prompt hope.
DESCRIBE_EXTRA_ARGS: tuple[str, ...] = ("--max-turns", "1")

#: How much of a bad reply's text reaches the ValueError/log reason —
#: enough to name the wrong shape (truncated JSON, a wrong key, an
#: envelope) straight off the log line (gr523669).
_REPLY_EXCERPT_CHARS = 200

#: Consecutive failed attempts after which a ref is benched: its attempt
#: lease is re-stamped at the same ordinal with
#: :data:`_BENCH_COOLDOWN_MIN` instead of the per-attempt
#: ``ref_lease.ATTEMPT_COOLDOWN_MIN`` — one retry a day, not one an hour.
#: Without it a ref whose reply never carries ``description`` re-bills an
#: LLM call every cooldown forever (gr523669: 123+37 failures/24 h
#: burning the $25 daily cap).
_PARSE_FAIL_BENCH = 3

#: The benched ref's brake window.
_BENCH_COOLDOWN_MIN = 24 * 60

#: Hard cap on the stored description. The prompt asks for ~3 sentences;
#: this only guards against a runaway completion.
_MAX_DESC_CHARS = 4000

_SYSTEM = (
    "You write search-index descriptions for a materials knowledge base. "
    "Each request is self-contained — the card text is the complete input "
    "and there is no file or tool to consult. Reply with only the "
    "requested JSON object, never prose or questions."
)

#: Vision variant for figures (S3): the grounding is the rendered image
#: itself, with the caption (ref title) + ``figure_notes`` as context.
_FIGURE_SYSTEM = (
    "You write search-index descriptions for a materials knowledge base. "
    "Each request is self-contained — the image, caption and notes are "
    "the complete input and there is no other file or tool to consult. "
    "Reply with only the requested JSON object, never prose or questions."
)

#: The describe instruction, carried in the USER message — the one
#: channel every transport reads. ``claude_p`` drops the system prompt
#: entirely (:class:`~precis.utils.llm.router.DispatchClient` folds it
#: onto ``LlmRequest.system_prompt``, which ``call_claude_p`` never
#: sends), so an instruction living only in :data:`_SYSTEM` never reaches
#: the model on that rung — gr543828's chat-agent replies ("no file
#: attached", clarification asks) were a bare ``<label>: <title> /
#: Card:`` prompt meeting Claude Code's own agentic persona. Keep the
#: instruction colon-free: the ``<Label>: <title>`` line's colon must
#: stay the message's first.
_CARD_INSTRUCTION = (
    "The card text below is the complete input — there is no file, tool "
    "or working directory to consult and nothing to clarify. Write a "
    "plain-text narrative of two to four sentences for a search index — "
    "what the {label} is, its character (composition, dimensionality, "
    "structure), and the function or application context it plausibly "
    "serves. Ground every claim in the card; mark plausible-but-"
    "unmeasured interpretation as such rather than inventing measured "
    "values."
)

#: The output contract — placed LAST in the prompt, right where the
#: completion begins (gr543828): the schema, then one minimal example.
_CARD_CONTRACT = (
    "Reply with a single JSON object of this schema and nothing else\n"
    '{"description": "<the narrative>"}\n'
    'Example: {"description": "A palladium oxide slab with in-plane '
    "periodicity, plausibly a model catalyst surface for oxidation "
    'chemistry studies."}'
)

#: The figure variant's instruction — its grounding is the rendered
#: image, not a card (same self-contained/JSON-only defect class as
#: :data:`_CARD_INSTRUCTION`, gr543828).
_FIGURE_INSTRUCTION = (
    "The image above plus the caption and notes below are the complete "
    "input — there is no file, tool or working directory to consult. "
    "Write a plain-text narrative of two to four sentences for a search "
    "index — what the figure depicts and communicates (its marks, axes "
    "or shapes) and the function or application context it plausibly "
    "serves. Ground every claim in the image and the supplied text; "
    "mark plausible-but-unshown interpretation as such rather than "
    "inventing values."
)

#: The one nudge a JSON-contract-broken reply buys (gr543828): appended
#: as a second user turn on the single retry. Never more than once — a
#: second failure propagates to the per-ref failure path, it does not
#: re-nudge.
_JSON_NUDGE = (
    "Reply with only the JSON object — "
    '{"description": "<the narrative>"} — no prose, no questions.'
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


#: A reply that is exactly one code fence — the fence wraps the answer
#: (JSON or prose), it isn't part of it. Anything less clean (two fences,
#: prose around the fence, a truncated closer) is left alone.
_WHOLE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```\s*", re.DOTALL)

#: ``"description": "`` key prefix inside a (possibly truncated) reply —
#: the salvage anchor once strict parsing has failed.
_DESC_KEY_RE = re.compile(r'"description"\s*:\s*"')

#: A trailing escape the output cap cut through (a lone ``\`` or a
#: partial ``\uXXXX``) — dropped before the salvage decode so the tail
#: doesn't poison ``json.loads``.
_PARTIAL_ESCAPE_RE = re.compile(r"\\(?:u[0-9a-fA-F]{0,3})?$")


def _reply_excerpt(text: str) -> str:
    """The reply's head flattened to ONE printable line — whitespace runs
    collapse and remaining control chars drop, so the excerpt can't break
    the shipped log line's shape."""
    flat = re.sub(r"\s+", " ", text)
    return "".join(ch for ch in flat if ch.isprintable()).strip()[:_REPLY_EXCERPT_CHARS]


def _reply_telemetry(res: Any) -> str:
    """``reply_chars=N[ stop=M]`` for the failure/salvage lines: the full
    reply length (the log excerpt is capped) plus the wire's finish/stop
    reason when the client reports one — ``length``/``max_tokens`` names
    an output-cap cut outright (gr523669)."""
    text = getattr(res, "text", None)
    out = f"reply_chars={len(text) if isinstance(text, str) else 0}"
    stop = getattr(res, "stop_reason", None) or getattr(res, "finish_reason", None)
    return f"{out} stop={stop}" if stop else out


def _decode_json_string(src: str) -> str | None:
    """Decode a raw JSON string body that may end mid-escape (the cap cut
    it): ``json.loads`` — strict first, control-char-tolerant second,
    the same two passes :func:`extract_json_object` runs — then, only if
    both fail, drop a partial trailing escape and retry (a complete
    ``\\\\`` tail still decodes; a cut ``\\`` or ``\\u12`` doesn't)."""
    candidates = [src]
    trimmed = _PARTIAL_ESCAPE_RE.sub("", src)
    if trimmed != src:
        candidates.append(trimmed)
    for candidate in candidates:
        for strict in (True, False):
            try:
                obj = json.loads(f'"{candidate}"', strict=strict)
            except Exception:
                continue
            if isinstance(obj, str):
                return obj
    return None


def _trim_to_last_sentence(text: str) -> str:
    """Cut ``text`` back to the end of its last complete sentence; with
    no sentence boundary at all (the prod cut is usually mid-clause), cut
    at the last word boundary instead — never mid-word."""
    stripped = text.rstrip()
    if not stripped or stripped.endswith((".", "!", "?")):
        return stripped
    cut = max(
        (stripped.rfind(marker) for marker in (". ", "! ", "? ", ".\n", "!\n", "?\n")),
        default=-1,
    )
    if cut > 0:
        return stripped[: cut + 1]
    word = stripped.rfind(" ")
    return stripped[:word] if word > 0 else ""


def _salvage_description(text: str) -> tuple[str, str] | None:
    """Recover a ``description`` value from a reply the strict parse
    rejected — an output cap cutting the JSON mid-string or a wrapper
    dropping the closer (gr523669). Take the string up to the cut,
    decode the escapes that survived, trim to the last complete
    sentence, and accept only a real narrative (≥ ``_MIN_SALVAGE_CHARS``
    — a fragment or a value the cut reached before it started is
    refused). Returns ``(description, reason)`` or ``None``."""
    m = _DESC_KEY_RE.search(text)
    if m is None:
        return None
    src = text[m.end() :]
    end = -1
    i = 0
    while i < len(src):
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            end = i
            break
        i += 1
    if end >= 0:
        reason = "unterminated-object"
        value_src = src[:end]
    else:
        reason = "cut-mid-string"
        value_src = src
    decoded = _decode_json_string(value_src)
    if decoded is None:
        return None
    desc = decoded.strip() if end >= 0 else _trim_to_last_sentence(decoded)
    if len(desc) < _MIN_SALVAGE_CHARS:
        return None
    return desc, reason


def _extract_description(res: Any) -> tuple[str, str | None]:
    """Pull the narrative out of the reply; return ``(text, salvage)``.

    ``CLAUDE_P`` demands a JSON block (``res.data``); the local/OpenAI
    wires return the same JSON as text, which is parsed here. A reply
    that yields no usable ``description`` — wrong key, cut before the
    value, or bare prose — raises so the caller's one nudge retry can
    take a second sample (gr543828: prose used to be *stored* as the
    description, which is exactly how a chat-agent clarification ask
    became index text). The reason carries the reply's head plus its
    length and stop reason so the log names the wrong shape (gr523669).

    ``salvage`` names the recovery path when the strict parse failed but
    the reply still carried a usable ``description`` — a truncated value
    trimmed to its last complete sentence — or ``None`` on a clean parse.

    Loose only where the strictness was incidental (gr523669): a whole-
    reply code fence is unwrapped, ``res.data`` that parsed but lacks the
    field falls back to the text, and the text parse is the shared
    tolerant one — a fenced block, prose before the object, extra keys
    alongside ``description``, and a raw (unescaped) control char inside
    a string all still yield the field. A wrong key, a reply truncated
    before a usable value, or a reply with no JSON object at all still
    fails."""
    from precis.utils.llm.json_reply import extract_json_object

    raw = (res.text or "").strip()
    m = _WHOLE_FENCE_RE.fullmatch(raw)
    text = (m.group(1) if m else raw).strip()
    data = getattr(res, "data", None)
    if not (isinstance(data, dict) and isinstance(data.get("description"), str)):
        alt = extract_json_object(text)
        if isinstance(alt, dict):
            data = alt
    desc = data.get("description") if isinstance(data, dict) else None
    if isinstance(desc, str):
        return desc.strip(), None
    salvage = _salvage_description(text)
    if salvage is not None:
        return salvage
    raise ValueError(
        "card_describe reply carried no usable 'description' field; "
        f"reply={_reply_excerpt(raw or repr(data))}; {_reply_telemetry(res)}"
    )


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
    ref_id: int | None = None,
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

    ``ref_id`` only names the ref on the info/warning lines (a salvaged
    reply, gr523669; an unparseable one taking its nudge retry,
    gr543828); ``None`` when the caller has no ref in hand.

    Retry contract (gr543828): a dispatch that fails ``unparseable``
    outright — the ``claude -p`` rung's own JSON extraction rejecting a
    prose/chat reply — or a reply that yields no usable ``description``
    is re-dispatched exactly once with :data:`_JSON_NUDGE` appended. A
    second failure propagates unchanged — one retry, never a loop.
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
                "text": (
                    f"{_FIGURE_INSTRUCTION}\n\n"
                    f"Figure: {title}\n\n"
                    f"Notes:\n{notes or '(none)'}\n\n"
                    f"{_CARD_CONTRACT}"
                ),
            },
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _FIGURE_SYSTEM},
            {"role": "user", "content": content},
        ]
    else:
        label = _KIND_LABEL.get(kind, kind)
        messages = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{_CARD_INSTRUCTION.format(label=label)}\n\n"
                    f"{label.capitalize()}: {title}\n\n"
                    f"Card:\n{card_text}\n\n"
                    f"{_CARD_CONTRACT}"
                ),
            },
        ]
    nudged = [*messages, {"role": "user", "content": _JSON_NUDGE}]
    retried = False
    try:
        res = client.complete(messages)
    except DispatchError as exc:
        if exc.reason_class != "unparseable":
            raise
        retried = True
        log.warning(
            "card_describe: ref_id=%s dispatch failed unparseable (%s); "
            "retrying once with a JSON-only nudge",
            ref_id,
            str(exc)[:_REPLY_EXCERPT_CHARS],
        )
        res = client.complete(nudged)
    try:
        text, salvage = _extract_description(res)
    except ValueError as exc:
        if retried:
            raise
        log.warning(
            "card_describe: ref_id=%s unparseable reply (%s); "
            "retrying once with a JSON-only nudge",
            ref_id,
            str(exc)[:_REPLY_EXCERPT_CHARS],
        )
        res = client.complete(nudged)
        text, salvage = _extract_description(res)
    if salvage is not None:
        log.info(
            "card_describe: ref_id=%s description salvaged (%s; %s)",
            ref_id,
            salvage,
            _reply_telemetry(res),
        )
    if len(text) < _MIN_DESC_CHARS:
        raise RuntimeError(
            f"card_describe: generation too short ({len(text)} chars) "
            f"for {title!r}; {_reply_telemetry(res)}"
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


def _claim_describe_batch(
    store: Store, *, batch_size: int
) -> tuple[list[tuple[int, str, str, str]], dict[int, int]]:
    """Claim up to ``batch_size`` refs and stamp each one's attempt lease
    in the SAME committed transaction — claim-commit-first (the bib_parse
    shape, gripe 202116): the lease is the durable brake, the FOR UPDATE
    row lock dies with this commit. The stamp carries the
    consecutive-attempt ordinal (the lease's value — gr523669): a
    success's clear_attempt resets it, so it counts failed attempts back
    to back across cooldown windows.

    Returns the claimed rows plus the attempt ordinal each was stamped
    at, for the caller's bench check. Runs inside the caller's
    single-runner advisory lock (gr524231): the transaction ends here —
    nothing DB-side stays open into the per-ref LLM work.
    """
    with store.pool.connection() as conn:
        rows = claim_refs_without_description(conn, limit=batch_size)
        attempts: dict[int, int] = {}
        for claimed_id, *_ in rows:
            n = ref_lease.attempt_count(conn, claimed_id, _LEASE_NS) + 1
            attempts[claimed_id] = n
            ref_lease.stamp_attempt(store, claimed_id, _LEASE_NS, conn=conn, attempts=n)
        conn.commit()
    return rows, attempts


def _run_card_describe_batch(
    store: Store,
    client: Any,
    rows: list[tuple[int, str, str, str]],
    attempts: dict[int, int],
) -> dict[str, Any]:
    """Per-ref describe + write over already-claimed ``rows``. Runs
    AFTER the claim lock and transaction have both ended — no backend
    sits idle-in-transaction across an LLM call (gr524231); per-ref
    arbitration is the stamped attempt lease, and a concurrent pass
    claims only refs this one did not. Every DB access is a short pooled
    transaction; writes go through :func:`retry_locked`."""
    claimed = len(rows)

    ok = failed = 0
    for i, (ref_id, kind, title, card_text) in enumerate(rows):
        n = attempts[ref_id]
        try:
            # Renew the lease at the SAME ordinal with a fresh TTL in its
            # own committed tx, just before the LLM call: the claim stamp
            # only brakes for ATTEMPT_COOLDOWN_MIN, a slow batch can
            # outlive it, and an expired lease lets a second host
            # re-claim and re-bill a ref this pass is still working
            # (ref_lease docstring — a pass whose work outlasts the
            # cooldown must re-stamp mid-work).
            with store.pool.connection() as conn:
                ref_lease.stamp_attempt(store, ref_id, _LEASE_NS, conn=conn, attempts=n)
                conn.commit()
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
                    ref_id=ref_id,
                )
            else:
                text, model = describe_card(
                    client, kind=kind, title=title, card_text=card_text, ref_id=ref_id
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
            # The reason goes in the line itself, not only exc_info —
            # the shipped log record is the message line, and a bare
            # ``ref_id=N failed`` gives the cause nowhere (gr523669).
            log.exception(
                "card_describe: ref_id=%s dispatch failed: %s: %s",
                ref_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            failed += 1
        except (LockNotAvailable, DeadlockDetected) as exc:
            # Residual contention outlasting retry_locked's budget —
            # unwritten, so it re-claims cleanly next pass (not failed).
            log.warning(
                "card_describe: ref_id=%s deferred after residual lock contention: %s",
                ref_id,
                exc,
            )
        except Exception as exc:
            log.exception(
                "card_describe: ref_id=%s failed: %s: %s",
                ref_id,
                type(exc).__name__,
                str(exc)[:300],
            )
            failed += 1
            if n >= _PARSE_FAIL_BENCH:
                # Nth consecutive failure with no success since — a repeat
                # offender (gr523669: an unparseable-shape reply re-billed
                # an LLM call every cooldown and ate the daily cap).
                # Re-stamp the SAME ordinal with the bench window: the ref
                # stays out of the claim set for a day, not an hour.
                with store.pool.connection() as conn:
                    ref_lease.stamp_attempt(
                        store,
                        ref_id,
                        _LEASE_NS,
                        conn=conn,
                        attempts=n,
                        ttl_minutes=_BENCH_COOLDOWN_MIN,
                    )
                    conn.commit()
                if n == _PARSE_FAIL_BENCH:
                    # Log the bench once — on the transition; a re-bench
                    # after the window is implied by the failure line.
                    log.warning(
                        "card_describe: ref_id=%s benched after %d "
                        "consecutive failures; backing off %d min",
                        ref_id,
                        n,
                        _BENCH_COOLDOWN_MIN,
                    )
    return {"claimed": claimed, "ok": ok, "failed": failed}


def run_card_describe_pass(
    store: Store, client: Any, *, batch_size: int = 16
) -> dict[str, Any]:
    """One pass over the card_describe queue.

    Single-runner gate, claim-scoped (gr524231): the transaction-scoped
    advisory lock (``try_xact_advisory_lock``, pgbouncer-safe) is held
    only while this worker decides it runs the pass and stakes its
    claim — decide + claim + lease-stamp, then the lock and its
    transaction both end. The pass's earlier shape held it for the whole
    pass, which pinned one backend idle-in-transaction across every LLM
    call and blocked other lanes behind a 30-minute holder. Beyond the
    claim, per-ref exclusion is the stamped attempt lease: a second host
    that takes the gate after this one's commit claims only refs this
    pass did not. A lock-acquire miss is a graceful ``claimed=0`` skip.
    """
    idle: dict[str, Any] = {"claimed": 0, "ok": 0, "failed": 0}
    if not store.dsn:
        return idle
    # S4 daily cap: read this pass's own 24 h spend first — over cap the
    # whole pass defers (reason ``budget``, the same defer shape as a
    # ``cli_unavailable`` pause) rather than claiming rows it must not
    # spend on. A plain read; it needs no exclusivity.
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
    with try_xact_advisory_lock(store.dsn, _LOCK_KEY) as got:
        if not got:
            return idle
        rows, attempts = _claim_describe_batch(store, batch_size=batch_size)
    # The lock's transaction ended on the block above; the per-ref loop
    # (LLM calls included) runs holding no transaction and no lock.
    return _run_card_describe_batch(store, client, rows, attempts)


__all__ = [
    "CARD_DESCRIBE_VERSION",
    "DESCRIBE_EXTRA_ARGS",
    "DESCRIBE_MAX_TOKENS",
    "claim_refs_without_description",
    "claimable_counts",
    "describe_card",
    "run_card_describe_pass",
    "write_card_description",
]
