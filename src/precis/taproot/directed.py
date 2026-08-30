"""Directed claim minting — "we need to be able to say X — does this source
license it?" Design: ``docs/backlog/taproot-directed-claim-minting.md``.

Demand-driven minting (:mod:`.backfill`, over already-authored prose with
existing paper citations) stays the default front door for turning prose
into claim hubs. This module is the other front door: a **proposed** claim
(from a quest gap, a draft "can I say this?", a nanopub negative-results
check, …) that has no citation yet, argued against one specific passage.
Undirected harvest — mining every micro-claim a paragraph could yield — is
deliberately out of scope (see the design doc's "Never an undirected
harvest"): the caller already knows the claim it needs; this module either
grounds it or tells the caller exactly why it can't.

Two argument steps, only the first is new here:

1. :func:`qualify_claim` — BIG tier, the new step. The LLM negotiates, not
   extracts: rewrite the *proposed* claim into the strongest version the
   *passage* actually licenses (adding qualifiers — material/method/
   quantity/regime/comparator/conditions — the passage requires), or decide
   no honest version survives (``supported=False``). Direction of fit is
   one-way: the claim bends to the evidence, never the reverse. The
   qualified claim must obey the same self-contained/world-claim/specific/
   plain-text rules :func:`~precis.taproot.canon.extract_claim` enforces
   (:data:`~precis.taproot.canon.CLAIM_FORM_RULES`, shared verbatim so the
   two prompts never drift), plus a minimal verbatim grounding quote,
   mechanically checked against the passage (whitespace-collapsed
   substring) — the anti-hallucination backstop: a quote the model
   fabricated, even a paraphrase, invalidates the whole result to
   ``unsupported``.
2. :func:`directed_mint` — the qualified atom runs through the exact SAME
   cascade tail :mod:`.backfill` uses (``block`` -> ``dedup_judge`` ->
   :func:`~precis.taproot.canon.place`) and the same write door
   (:func:`~precis.taproot.hub.apply_placement`) — attach / new /
   new_contradicts / needs_review, same convergence gates, idempotence, and
   published-hub hard-stop as every other mint. No new SQL write path: this
   module only orchestrates the existing cascade + the existing hub.py
   doors.

**Strict dispatch posture** (mirrors
:func:`~precis.taproot.canon.extract_claim_strict`): a dispatch error
raises :class:`QualifyUnavailable` rather than degrading to
``supported=False`` — an LLM outage must never read as "the passage doesn't
support this" (the exact silent-noclaim failure mode
``docs/backlog/taproot-backfill-llm-outage-silent-noclaim.md`` names). An
unparseable-but-successful response *does* degrade to unsupported: the
model responded, so that really is (or looks like) a semantic judgment,
not an infra failure.

**Provenance of demand.** ``directed_mint(..., demand=...)`` stamps the
minted/attached hub's ``meta.demanded_by`` (via :meth:`Store.update_ref`,
the same generic meta-patch primitive :func:`~precis.taproot.hub.
refine_claim_sentence` uses, never a bespoke write) so a directed mint is
never unowned — the requirement the design doc calls out first.

**Dry-run by default.** ``apply=False`` (the default) makes zero
claim-data writes: :func:`qualify_claim` still calls the LLM (BIG tier,
budget-metered like every other taproot dispatch), but nothing is written
to ``refs``/``chunks``/``links``/``ref_tags``. ``apply=True`` opts into the
real mint/attach through :func:`~precis.taproot.hub.apply_placement`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.store.types import ActorSlug
from precis.taproot.canon import (
    CLAIM_FORM_RULES,
    CanonicalClaim,
    MergeCandidate,
    Placement,
    Verdict,
    _parse_claim_item,
    _parse_json_object,
    block,
    claim_sha,
    dedup_judge,
    merge_confirm,
    place,
)
from precis.taproot.hub import _DEFAULT_ROLE, EVIDENCE_SRC_KINDS, apply_placement
from precis.utils.llm.router import LlmRequest, Tier, route

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "DirectedMintReport",
    "QualifyResult",
    "QualifyUnavailable",
    "directed_mint",
    "qualify_claim",
    "render_report",
]

# ── qualify_claim — BIG tier, one-way fit ───────────────────────────────

#: Caps the passage fed to the qualify prompt — generous relative to
#: extract_claim's per-chunk excerpt cap since a directed mint's caller has
#: already pointed at one specific chunk (not a corpus sweep), so there's
#: no per-call cost pressure to trim harder.
_QUALIFY_EXCERPT_CHARS = 3000

_QUALIFY_SYS = (
    "You are a skeptical scientific claim auditor. A caller PROPOSES a "
    "claim they want to cite; your job is to check whether the PASSAGE "
    "alone licenses it, and if not, find the strongest honest weakening it "
    "does license. Reply with ONLY the requested JSON object, no prose."
)

_QUALIFY_PROMPT = """\
A caller wants to cite the PASSAGE below as support for the PROPOSED CLAIM.
Direction of fit is ONE-WAY: the claim bends to the evidence, never the
reading of the evidence to the claim. Rewrite the proposal into the
STRONGEST version the passage ALONE fully licenses — adding the qualifiers
it requires (material, method, quantity, regime, comparator, conditions) —
or decide no honest version survives.

PROPOSED CLAIM:
{proposed}

PASSAGE:
{passage}

{rules}

Additional rules for this qualify step:
5. One-way fit only: ADD qualifiers the passage forces; never invent
   support the passage doesn't state. If the proposal already understates
   what the passage shows, that's fine — this step narrows/weakens, it
   never inflates the proposal beyond what was asked.
6. Grounding quote: when supported, also return the MINIMAL verbatim span
   copied EXACTLY from the passage (no paraphrase, no ellipsis-joining
   distant sentences) that grounds the qualified claim. This is checked
   mechanically against the passage text — any deviation fails
   verification and the whole result is discarded as unsupported.
7. If no honest version of the proposal survives — the passage doesn't
   touch the proposed claim at all, states something narrower/different,
   or contradicts it — return supported=false with a concrete reason
   naming what the passage lacks or how it conflicts.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "supported": true | false,
  "claim": "<qualified, self-contained sentence>" | null,
  "material": "<optional>",
  "method": "<optional>",
  "quantity": "<optional>",
  "regime": "<optional>",
  "quote": "<minimal verbatim span copied from the passage>" | null,
  "reason": "<what was qualified/weakened, or why unsupported>"
}}
"""

_WS_RE = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _quote_in_passage(quote: str, passage: str) -> bool:
    """The anti-hallucination check: ``quote`` must appear verbatim in
    ``passage`` after whitespace-collapsing both sides (tolerates the model
    re-wrapping/re-spacing a copied span, never a paraphrase or a span
    stitched from non-adjacent text)."""
    quote_norm = _collapse_ws(quote)
    return bool(quote_norm) and quote_norm in _collapse_ws(passage)


@dataclass(frozen=True)
class QualifyResult:
    """One :func:`qualify_claim` outcome.

    * ``supported`` — did an honest, passage-licensed version of the
      proposed claim survive?
    * ``claim`` — the qualified :class:`~precis.taproot.canon.CanonicalClaim`
      (sentence + scope), or ``None`` when unsupported.
    * ``quote`` — the minimal verbatim grounding span from the passage, or
      ``None`` when unsupported. Always verified against the passage
      (:func:`_quote_in_passage`) before ``supported`` is ever ``True`` —
      a quote that doesn't check out demotes the whole result to
      unsupported, never a supported result with an unverified quote.
    * ``reason`` — what was qualified/weakened (a supported result), or why
      no honest version survives (an unsupported one).
    """

    supported: bool
    claim: CanonicalClaim | None
    quote: str | None
    reason: str


class QualifyUnavailable(RuntimeError):
    """Raised by :func:`qualify_claim` when the dispatch itself failed
    (infra error) rather than the model judging the claim unsupported.
    Mirrors :class:`~precis.taproot.canon.ExtractionUnavailable`: conflating
    "the model never ran" with "the passage doesn't support this" is
    exactly the failure mode this strict posture exists to prevent."""


def _coerce_qualification(
    data: dict[str, Any] | None, passage: str, *, default_reason: str
) -> QualifyResult:
    """Normalize a raw model JSON payload into a :class:`QualifyResult`.

    Bias-safe on any malformed/missing field, mirroring
    :func:`~precis.taproot.canon._coerce_verdict`: an unparseable payload,
    a missing/falsy ``supported``, a missing claim, a missing quote, or a
    quote that doesn't verify against ``passage`` (:func:`_quote_in_passage`)
    all degrade to ``supported=False`` — never a silent supported result
    with a fabricated or unverified quote.
    """
    if not isinstance(data, dict):
        return QualifyResult(
            supported=False, claim=None, quote=None, reason=default_reason
        )

    raw_reason = data.get("reason")
    reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip()
        else default_reason
    )

    # `is not True`, not truthiness: a malformed `"supported": "false"`
    # (string) must read as unsupported, per the bias-safe posture.
    if data.get("supported") is not True:
        return QualifyResult(supported=False, claim=None, quote=None, reason=reason)

    claim = _parse_claim_item(data)
    if claim is None:
        return QualifyResult(
            supported=False,
            claim=None,
            quote=None,
            reason="model marked supported but returned no claim text",
        )

    raw_quote = data.get("quote")
    quote = raw_quote.strip() if isinstance(raw_quote, str) else ""
    if not quote:
        return QualifyResult(
            supported=False,
            claim=None,
            quote=None,
            reason="model marked supported but returned no grounding quote",
        )
    if not _quote_in_passage(quote, passage):
        return QualifyResult(
            supported=False,
            claim=None,
            quote=None,
            reason="quote not found in passage",
        )

    return QualifyResult(supported=True, claim=claim, quote=quote, reason=reason)


def qualify_claim(proposed: str, passage: str) -> QualifyResult:
    """Argue ``proposed`` against ``passage`` — BIG tier, the negotiate-not-
    extract step (module docstring).

    Empty ``proposed``/``passage`` short-circuits to unsupported with no
    dispatch (nothing to argue). Otherwise dispatches Tier.BIG and:

    * a dispatch error raises :class:`QualifyUnavailable` (strict posture —
      never silently "unsupported");
    * a successful-but-unparseable response degrades to ``supported=False``
      (the model *did* respond; see :func:`_coerce_qualification`);
    * a "supported" verdict is only ever returned once its ``quote`` has
      been mechanically verified against ``passage``.
    """
    proposed_s = (proposed or "").strip()
    passage_s = (passage or "").strip()
    if not proposed_s or not passage_s:
        return QualifyResult(
            supported=False,
            claim=None,
            quote=None,
            reason="empty proposed claim or passage",
        )

    prompt = _QUALIFY_PROMPT.format(
        proposed=proposed_s,
        passage=passage_s[:_QUALIFY_EXCERPT_CHARS],
        rules=CLAIM_FORM_RULES,
    )
    res = route(
        LlmRequest(
            tier=Tier.BIG,
            messages=[
                {"role": "system", "content": _QUALIFY_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="taproot:qualify",
        )
    )
    if res.error:
        log.warning("taproot: qualify_claim dispatch failed: %s", res.error)
        raise QualifyUnavailable(res.error)

    data = res.data or _parse_json_object(res.text)
    return _coerce_qualification(
        data, passage_s, default_reason="unparseable model output"
    )


# ── directed_mint — qualify, then the same cascade + write door ────────

QualifyFn = Callable[[str, str], QualifyResult]
BlockFn = Callable[[CanonicalClaim, Any, Any], list[MergeCandidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]


def _read_passage_chunk(store: Store, chunk_id: int) -> tuple[str, int, str, str]:
    """``(text, ref_id, ref_kind, ref_title)`` for a live evidence-source
    body chunk. Read-only.

    Raises:
        BadInput: no live body chunk with ``chunk_id``, or its owning ref
            isn't a live :data:`~precis.taproot.hub.EVIDENCE_SRC_KINDS` ref
            — a directed mint's grounding passage must come from an
            evidence source (open #15), same as the forward cascade.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT c.text, c.ref_id, r.kind, r.title, r.retired_at "
            "FROM chunks c JOIN refs r ON r.ref_id = c.ref_id "
            "WHERE c.chunk_id = %s AND c.ord >= 0 AND c.retired_at IS NULL",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"no live body chunk with chunk_id={chunk_id}",
            next="pass a pc<id> handle for a live paper/patent/edgar chunk",
        )
    text, ref_id, kind, title, retired_at = row
    if retired_at is not None or kind not in EVIDENCE_SRC_KINDS:
        raise BadInput(
            f"chunk_id={chunk_id} belongs to a {kind!r} ref (ref_id={ref_id}), "
            "not a live evidence source",
            next=f"evidence sources are: {'/'.join(sorted(EVIDENCE_SRC_KINDS))}",
        )
    return str(text), int(ref_id), str(kind), str(title or "")


@dataclass(frozen=True)
class DirectedMintReport:
    """The full result of one :func:`directed_mint` call — what
    :func:`render_report` formats for a human/CLI reviewer.

    ``placement`` / ``hub_ref_id`` are ``None`` when :attr:`qualify` came
    back unsupported (the cascade never runs). ``hub_ref_id`` is the
    matched candidate for an ``attach`` placement even in a dry run (known
    at plan time); for ``new``/``new_contradicts`` it is ``None`` until
    ``applied=True`` actually mints it (mirrors
    :mod:`~precis.taproot.backfill`'s ``GroupPlan``). ``applied`` is
    ``True`` only once a real write attempt through
    :func:`~precis.taproot.hub.apply_placement` was made — never on an
    unsupported qualify, regardless of the caller's ``apply=`` flag (zero
    claim-data writes either way).
    """

    proposed: str
    chunk_id: int
    passage_ref_id: int
    passage_ref_kind: str
    passage_ref_title: str
    demand: str | None
    qualify: QualifyResult
    placement: Placement | None = None
    hub_ref_id: int | None = None
    applied: bool = False


def _file_review_todo(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    chunk_id: int,
    demand: str | None,
    set_by: ActorSlug,
) -> None:
    """A ``kind='todo'`` for a risky (``needs_review``) directed-mint merge
    — mirrors :func:`precis.taproot.backfill._file_review_todo`, keyed on
    the passage chunk instead of a draft chunk. Its own ``store.tx()`` (no
    hub/edge write on this path to stay atomic with)."""
    from precis.store.types import Tag

    title = f"taproot: review directed-mint merge for pc{chunk_id}"
    meta: dict[str, Any] = {
        "source": "taproot:directed",
        "chunk": f"pc{chunk_id}",
        "claim_sentence": claim.sentence,
        "placement_reason": placement.reason,
        "candidate_hub_ref_id": placement.hub_ref_id,
    }
    if demand:
        meta["demanded_by"] = demand
    with store.tx() as c:
        todo = store.insert_ref(
            kind="todo", slug=None, title=title[:200], meta=meta, conn=c
        )
        store.add_tag(
            todo.id,
            Tag.closed("STATUS", "open"),
            set_by=set_by,
            replace_prefix=True,
            conn=c,
        )


def directed_mint(
    store: Store,
    embedder: Any,
    *,
    proposed: str,
    chunk_id: int,
    demand: str | None = None,
    apply: bool = False,
    qualify_fn: QualifyFn = qualify_claim,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = merge_confirm,
    role: str = _DEFAULT_ROLE,
    set_by: ActorSlug = "agent",
    todo_fn: Callable[[CanonicalClaim, Placement], Any] | None = None,
) -> DirectedMintReport:
    """Argue ``proposed`` against ``chunk_id``'s passage, then (if
    supported) fit it into the claim tree — the directed-mint front door
    (module docstring).

    1. Read the passage: ``chunk_id``'s live text + its owning evidence-
       source ref (:func:`_read_passage_chunk`; raises :class:`BadInput`
       on a non-evidence-source or dead chunk).
    2. :func:`qualify_fn` (default :func:`qualify_claim`, BIG tier) — argue
       the proposal against the passage. Unsupported -> the report stops
       here (``placement``/``hub_ref_id`` stay ``None``, ``applied=False``
       always): the cascade never runs over a claim the passage doesn't
       license.
    3. Supported -> the qualified atom runs the SAME cascade tail
       :mod:`.backfill` uses: ``block_fn`` (ANN over existing hubs) ->
       ``judge_fn`` per candidate -> :func:`~precis.taproot.canon.place`
       (with ``merge_confirm_fn`` for a risky merge).
    4. ``apply=False`` (default): stop here — a full plan report, zero
       claim-data writes (the LLM dispatch in step 2 still runs; it's
       budget-metered telemetry only, never claim data).
    5. ``apply=True``: :func:`~precis.taproot.hub.apply_placement` mints/
       attaches through the existing write door, with the grounding
       ``quote`` carried in the evidence edge's ``meta``
       (``source_handle=pc<chunk_id>`` grounds it to this exact passage —
       :func:`~precis.taproot.hub._grounding_chunk_ord` resolves it same
       as every other evidence edge). Because the qualify step is itself
       a claim-vs-passage verification, the edge is born verified:
       ``support: "yes"`` rides with ``support_reason`` (the qualify
       note), ``verified_by='directed-mint'``, ``verified_at``, and
       ``verified_claim_sha`` of the qualified sentence — never a bare
       mint-time default. A ``needs_review`` placement never
       auto-attaches (open #16): it files a ``kind='todo'`` via
       ``todo_fn`` (default :func:`_file_review_todo`) and contributes no
       hub. When a hub *did* land and ``demand`` was given, the hub's
       ``meta.demanded_by`` is stamped via ``store.update_ref`` as an
       accumulating **list** of demanders (an attach onto an
       already-demanded hub appends, never overwrites) — so a directed
       mint is never unowned (the design doc's first requirement). The
       stamp is non-fatal: it runs after the mint's own transaction, and
       a stamp failure logs a warning rather than masking the landed
       mint.

    Idempotent by construction, same as the cascade it reuses: a re-run
    over the same ``(proposed, chunk_id)`` re-derives the same qualified
    claim, ``block``/``dedup_judge`` converge onto the hub the first run
    minted (``attach``), and :func:`~precis.taproot.hub.attach_evidence`
    skips an evidence edge that already exists.
    """
    passage, ref_id, ref_kind, ref_title = _read_passage_chunk(store, chunk_id)
    qr = qualify_fn(proposed, passage)

    if not qr.supported or qr.claim is None:
        return DirectedMintReport(
            proposed=proposed,
            chunk_id=chunk_id,
            passage_ref_id=ref_id,
            passage_ref_kind=ref_kind,
            passage_ref_title=ref_title,
            demand=demand,
            qualify=qr,
        )

    candidates = block_fn(qr.claim, store, embedder)
    judged = [(cand, judge_fn(qr.claim.sentence, cand.claim)) for cand in candidates]
    placement = place(qr.claim, judged, merge_confirm_fn=merge_confirm_fn)

    if not apply:
        return DirectedMintReport(
            proposed=proposed,
            chunk_id=chunk_id,
            passage_ref_id=ref_id,
            passage_ref_kind=ref_kind,
            passage_ref_title=ref_title,
            demand=demand,
            qualify=qr,
            placement=placement,
            hub_ref_id=placement.hub_ref_id,
            applied=False,
        )

    def _default_todo_fn(claim: CanonicalClaim, placement_: Placement) -> None:
        _file_review_todo(
            store, claim, placement_, chunk_id=chunk_id, demand=demand, set_by=set_by
        )

    # A real verdict, not a mint-time default: qualify_claim IS a
    # claim-vs-passage judgment (BIG tier, one-way fit, and the grounding
    # quote was mechanically verified against this exact passage), so the
    # edge is born verified — support_reason is the qualify note,
    # verified_claim_sha binds the verdict to the qualified wording (an
    # attach onto a differently-worded hub reads as unverified there, which
    # is honest: qualify never saw that hub's sentence).
    edge_meta: dict[str, Any] = {
        "support": "yes",
        "support_reason": qr.reason,
        "caveats": [],
        "source_handle": f"pc{chunk_id}",
        "origin": "directed-mint",
        "quote": qr.quote,
        "verified_by": "directed-mint",
        "verified_at": datetime.now(UTC).isoformat(),
        "verified_claim_sha": claim_sha(qr.claim.sentence),
    }
    hub_ref_id = apply_placement(
        store,
        qr.claim,
        placement,
        paper_ref_id=ref_id,
        role=role,
        meta=edge_meta,
        todo_fn=todo_fn if todo_fn is not None else _default_todo_fn,
        set_by=set_by,
    )
    if hub_ref_id is not None and demand:
        # Accumulate (list semantics): an attach onto an already-demanded
        # hub records every demander, never overwrites the first. Non-fatal:
        # the mint/attach above is the authoritative write — a failed stamp
        # must not read as a failed mint (it would strand a landed hub).
        try:
            ref = store.get_ref(kind="finding", id=hub_ref_id)
            existing = (ref.meta or {}).get("demanded_by") if ref is not None else None
            if isinstance(existing, list):
                demands = existing if demand in existing else [*existing, demand]
            elif isinstance(existing, str) and existing:
                demands = [existing] if existing == demand else [existing, demand]
            else:
                demands = [demand]
            store.update_ref(hub_ref_id, meta_patch={"demanded_by": demands})
        except Exception:
            log.warning(
                "directed-mint: demanded_by stamp failed for hub %s (mint stands)",
                hub_ref_id,
                exc_info=True,
            )

    return DirectedMintReport(
        proposed=proposed,
        chunk_id=chunk_id,
        passage_ref_id=ref_id,
        passage_ref_kind=ref_kind,
        passage_ref_title=ref_title,
        demand=demand,
        qualify=qr,
        placement=placement,
        hub_ref_id=hub_ref_id,
        applied=True,
    )


# ── report rendering ─────────────────────────────────────────────────────


def render_report(report: DirectedMintReport) -> str:
    """Render a :class:`DirectedMintReport` as markdown, in the style of
    :func:`precis.taproot.migrate.render_report`."""
    lines = ["# Directed claim mint report", ""]
    lines.append(f"**Proposed**: {report.proposed}")
    lines.append(
        f"**Passage**: pc{report.chunk_id} ({report.passage_ref_kind} "
        f"ref_id={report.passage_ref_id} — {report.passage_ref_title})"
    )
    if report.demand:
        lines.append(f"**Demand**: {report.demand}")
    lines.append("")

    qr = report.qualify
    if not qr.supported or qr.claim is None:
        lines.append(f"**Qualify**: UNSUPPORTED — {qr.reason}")
        return "\n".join(lines)

    lines.append(f"**Qualified claim**: {qr.claim.sentence}")
    if qr.claim.scope:
        scope_bits = ", ".join(f"{k}={v}" for k, v in sorted(qr.claim.scope.items()))
        lines.append(f"**Scope**: {scope_bits}")
    lines.append(f'**Grounding quote**: "{qr.quote}"')
    lines.append(f"**Qualify note**: {qr.reason}")
    lines.append("")

    placement = report.placement
    if placement is not None:
        if placement.action == "attach":
            lines.append(f"**Placement**: attach -> fi{placement.hub_ref_id}")
        elif placement.action == "new_contradicts":
            lines.append(
                f"**Placement**: new, contradicts fi{placement.contradicts_hub_ref_id}"
            )
        elif placement.action == "new":
            lines.append("**Placement**: new hub")
        else:
            lines.append("**Placement**: needs_review — filed for human review")
        lines.append(f"**Placement reason**: {placement.reason}")
    lines.append("")

    if report.applied:
        if report.hub_ref_id is not None:
            lines.append(f"**Applied**: minted/attached fi{report.hub_ref_id}")
        else:
            lines.append("**Applied**: no hub written (needs_review, filed for review)")
    else:
        lines.append("**Applied**: NO (dry-run — zero claim-data writes)")

    return "\n".join(lines)
