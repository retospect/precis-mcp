"""Taproot Phase 1 — flat claim canonicalization (the gate).

Build ticket: ``docs/proposals/taproot-phase1-canonicalization.md``. Design
context: ``docs/proposals/taproot.md`` §"The crux — canonicalization = flat
claim dedup". Four functions, one cascade:

1. :func:`extract_claim` — SMALL/local. A chunk of text -> a
   :class:`CanonicalClaim` (normalized sentence + light scope), or ``None``
   when the chunk asserts nothing groundable (the ``NO-CLAIM`` outcome).
2. :func:`block` — no model. ANN over the existing ``TAPROOT:claim`` hub card
   embeddings (bge-m3, same embedder as the rest of the card index) ->
   the ``k`` nearest :class:`Candidate` hubs.
3. :func:`dedup_judge` — MEDIUM. THE crux call — one bounded pairwise
   judgment: ``same`` / ``different`` / ``contradicts``. This is what the
   fixture (``tests/fixtures/taproot/``) grades; **bias hard toward
   "different"** — over-merge (a false ``same``) is the dangerous error,
   under-merge is safe and recoverable.
4. :func:`place` — deterministic branching over the judged candidates, with
   one conditional model call folded in: a low-confidence ``same`` is
   re-checked by :func:`merge_confirm` (BIG) before it is trusted, and a
   merge that still isn't confidently confirmed is **not** auto-applied —
   it comes back as ``needs_review`` (design #16 of ``taproot.md``) so a
   caller files a ``kind='todo'`` rather than risk fusing distinct claims.

Every model call routes through :mod:`precis.utils.llm.router` (ADR 0046) —
no hardcoded model, no direct subprocess/HTTP. Phase 1 persists nothing: no
migration, no hub/edge writes — see ``taproot.md`` §"Target + blast radius".
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from precis.utils.llm.router import LlmRequest, Tier, dispatch

log = logging.getLogger(__name__)

# ── the closed TAPROOT namespace (see taproot.md open #11) ─────────────────
#
# Registered here (mirrors ROLE3, ``workers/classify.py``) even though the
# Phase-1-predecessor classifier that *writes* these tags isn't built yet
# (out of scope, see the build ticket's "Explicitly NOT in Phase 1"). `block`
# reads it so the query is correct the day the classifier lands; until then
# it simply finds no tagged hubs and returns empty (brand-new claim), which
# is the correct degrade.
TAPROOT_NAMESPACE = "TAPROOT"
TAPROOT_CLAIM = "claim"
TAPROOT_REVIEW = "review"


def claim_sha(title: str) -> str:
    """Stable content hash of a claim sentence (a hub's ``title``).

    Shared between ``workers/chase_trigger.py`` (which stores it per
    ``claim_embeddings`` row so a claim edit invalidates the stale vector)
    and ``workers/hub_refine.py`` (which stamps it onto
    ``finding.meta['last_refined_sha']`` at refine time and reopens a hub
    whose title has since changed) — both must agree on the hash or the
    two passes silently disagree about what "changed" means. Mirrors
    ``classify_topics.topic_marker_value``'s blake2b idiom.
    """
    return hashlib.blake2b(title.strip().encode("utf-8"), digest_size=8).hexdigest()


Verdict3 = Literal["same", "different", "contradicts"]

#: :func:`dedup_judge` / :func:`merge_confirm` confidence below this on a
#: "same" verdict is not trusted directly — :func:`place` re-checks it via
#: :func:`merge_confirm` (dedup_judge) or treats it as unconfirmed
#: (merge_confirm itself) before ever attaching. Picked conservatively (the
#: over-merge guard is the whole point of Phase 1); tune against the fixture
#: once the live eval (``eval_canon.py``) is run.
MERGE_CONFIDENCE_THRESHOLD = 0.85


class Verdict(TypedDict):
    """One ``dedup_judge`` / ``merge_confirm`` outcome."""

    verdict: Verdict3
    confidence: float
    rationale: str


@dataclass(frozen=True)
class CanonicalClaim:
    """A claim normalized to a sentence + a light scope note.

    ``scope`` is deliberately loose — a handful of optional string keys
    (``material`` / ``method`` / ``quantity`` / ``regime``), not an
    ontology (taproot.md §Non-goals #5). May be ``{}``.
    """

    sentence: str
    scope: dict[str, str]


@dataclass(frozen=True)
class Candidate:
    """One ANN hit from :func:`block` — an existing claim hub near the
    query claim."""

    hub_ref_id: int
    claim: str
    distance: float


@dataclass(frozen=True)
class Placement:
    """The deterministic outcome of :func:`place`.

    * ``"attach"`` — merge into ``hub_ref_id`` (a confirmed ``same``).
    * ``"new_contradicts"`` — mint a new hub, linked ``contradicts`` to
      ``contradicts_hub_ref_id``.
    * ``"new"`` — mint a new hub (no matching/contradicting candidate).
    * ``"needs_review"`` — a risky (low-confidence, unconfirmed) merge.
      **Not** auto-applied (taproot.md open #16); the caller should file a
      ``kind='todo'`` rather than attach or silently drop it.
    """

    action: Literal["attach", "new_contradicts", "new", "needs_review"]
    hub_ref_id: int | None = None
    contradicts_hub_ref_id: int | None = None
    reason: str = ""


# ── extract_claim — SMALL/local ─────────────────────────────────────────

#: Cap the excerpt fed to the extractor — a claim is a sentence, not a
#: re-read of the whole chunk (mirrors ``quest/claims.py``'s ``_EXCERPT_CHARS``,
#: sized up a little since this extractor also reads for scope terms).
_EXTRACT_EXCERPT_CHARS = 1500

_EXTRACT_SYS = (
    "You are a precise scientific claim extractor. Reply with ONLY the "
    "requested JSON object, no prose."
)

_EXTRACT_PROMPT = """\
Does this passage assert a specific, citable scientific claim (a concrete
result, measurement, definition, capability, or finding), or does it only
point to other work without asserting anything itself (e.g. "See [12]", a
Related-Work sentence that is only a citation list, "several studies exist")?

PASSAGE:
{excerpt}

Rules — the claim will be read ALONE, without this passage:
1. Self-contained: resolve every "this/these/it/such" from the passage and
   inline the referent. The same goes for temporal/discourse openers that
   point outside the sentence — "Subsequent(ly)", "Previous(ly)",
   "Further", "Earlier", "In contrast", "Similarly", "However", "Also":
   inline what they refer to ("Compared to X, …") or drop the connective.
   If the referent is not in the passage, claim = null.
2. A world-claim: about materials, results, or mechanisms — not about the
   literature or the text itself. If meta-prose wraps real content
   ("properties are commonly tabulated…"), extract the underlying fact
   (the specific properties or values), never the practice; if the passage
   states only the practice, claim = null.
3. Specific: keep the numbers, materials, and conditions the passage
   states; drop empty intensifiers ("extraordinary", "remarkable").
4. Plain text, no TeX: the claim renders without a math engine — write
   formulas with UTF-8 sub/superscripts ("C60" -> "C₆₀", "g-C$_3$N$_4$"
   -> "g-C₃N₄", "cm$^2$/Vs" -> "cm²/Vs").

Examples:
- "This strategy has been pursued across the principal families of 2D
  materials."  -> BAD (dangling "This strategy"); with the referent in the
  passage: "Hybridization of fullerenes with 2D materials has been pursued
  across graphene, g-C3N4, TMDs, h-BN, and black phosphorus."
- "The properties of these materials are commonly tabulated for
  comparative reference."  -> claim = null (practice, not a world-claim).
- "Subsequent DFT-D3 calculations reduced the sidewall binding energy to
  +0.74 eV."  -> BAD (subsequent to what?); with the referent in the
  passage: "Including pairwise dispersion corrections (DFT-D3) reduces
  the calculated C₆₀-nanotube sidewall binding energy from ~+1.5 eV to
  +0.74 eV."
- "Single-wall carbon nanocones were observed with opening angles of
  approximately 19, 39, 60, 85, and 113 degrees."  -> GOOD (specific,
  self-contained).
- "Graphene exhibits extraordinary tensile strength."  -> weak; if the
  passage states the value, prefer "Graphene exhibits a tensile strength
  of ~130 GPa."

If it asserts a claim: normalize it to ONE sentence, plus any of
material/method/quantity/regime it names (omit keys it doesn't name).
If it asserts nothing groundable: return claim = null.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "claim": "<one normalized sentence>" | null,
  "material": "<optional>",
  "method": "<optional>",
  "quantity": "<optional>",
  "regime": "<optional>"
}}
"""

_SCOPE_KEYS = ("material", "method", "quantity", "regime")


def extract_claim(chunk_text: str) -> CanonicalClaim | None:
    """Extract the dominant claim from ``chunk_text``, or ``None``.

    SMALL/local tier — cheap, per-chunk. Returns ``None`` when the chunk is
    pure-pointer / meta (the ``NO-CLAIM`` outcome, taproot.md Axis A stage
    0') — including on a dispatch error or unparseable model output (fail
    safe: no claim rather than a bad one).

    v1 returns the single **dominant** claim; splitting a bundled ``X∧Y``
    chunk into atoms is deferred (taproot.md §Non-goals #4) — a bundled
    chunk simply under-merges later, which the fixture metric tolerates.
    """
    text = (chunk_text or "").strip()
    if not text:
        return None
    prompt = _EXTRACT_PROMPT.format(excerpt=text[:_EXTRACT_EXCERPT_CHARS])
    res = dispatch(
        LlmRequest(
            tier=Tier.SMALL,
            messages=[
                {"role": "system", "content": _EXTRACT_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="taproot:extract",
        )
    )
    if res.error:
        log.warning("taproot: extract_claim dispatch failed: %s", res.error)
        return None
    data = res.data or _parse_json_object(res.text)
    if not isinstance(data, dict):
        return None
    claim = data.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        return None
    scope = {
        key: str(data[key]).strip()
        for key in _SCOPE_KEYS
        if isinstance(data.get(key), str) and data[key].strip()
    }
    return CanonicalClaim(sentence=claim.strip(), scope=scope)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort JSON-object parse, tolerating surrounding prose.

    Fallback for a transport (like the SMALL/litellm path) whose
    ``LlmResult.data`` extraction missed — mirrors ``quest/claims.py``'s
    ``_extract_json``, but for a JSON *object* instead of an array.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            parsed = json.loads(text[a : b + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


# ── block — no model, ANN over the card index ───────────────────────────


def block(
    claim: CanonicalClaim,
    store: Any,
    embedder: Any,
    *,
    k: int = 10,
    embedder_name: str = "bge-m3",
) -> list[Candidate]:
    """The ``k`` nearest existing ``TAPROOT:claim`` hubs to ``claim`` — no model.

    Embeds ``claim.sentence`` with ``embedder`` (an
    :class:`~precis.embedder.Embedder`-shaped object — ``embed_one(text) ->
    list[float]``) and ANN-retrieves over the ``TAPROOT:claim``-tagged
    ``finding`` refs' ``card_combined`` (``ord=-1``) embeddings — the same
    card index every other kind embeds into. Empty when no tagged hub
    exists yet (brand-new claim; also today's degrade, since the
    classifier that writes ``TAPROOT:claim`` is a Phase-2 predecessor, not
    built here — see the build ticket).

    ``store`` / ``embedder`` are explicit, injected params (not resolved
    from a global) so this stays trivially testable with a fake store/mock
    embedder and carries no import-time DB/model dependency — the build
    ticket's skeleton signature omits infra plumbing for brevity, not to
    forbid it.
    """
    vector = embedder.embed_one(claim.sentence)
    sql = """
        SELECT r.ref_id, r.title, (ce.vector <=> %(vec)s::vector) AS dist
        FROM refs r
        JOIN ref_tags rt ON rt.ref_id = r.ref_id
        JOIN tags t ON t.tag_id = rt.tag_id
                   AND t.namespace = %(ns)s AND t.value = %(val)s
        JOIN chunks c ON c.ref_id = r.ref_id AND c.ord = -1
                     AND c.retired_at IS NULL
        JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                                AND ce.embedder = %(embedder)s
        WHERE r.kind = 'finding' AND r.deleted_at IS NULL
        ORDER BY ce.vector <=> %(vec)s::vector ASC
        LIMIT %(k)s
    """
    params = {
        "vec": vector,
        "ns": TAPROOT_NAMESPACE,
        "val": TAPROOT_CLAIM,
        "embedder": embedder_name,
        "k": k,
    }
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        Candidate(hub_ref_id=int(r[0]), claim=str(r[1]), distance=float(r[2]))
        for r in rows
    ]


# ── dedup_judge — MEDIUM, the crux ──────────────────────────────────────

_DEDUP_SYS = (
    "You are a precise scientific claim comparator. Reply with ONLY the "
    "requested JSON object, no prose."
)

# Feeds the same rubric that produced tests/fixtures/taproot/ (see its
# README): equivalent -> same, broader/narrower/orthogonal -> different,
# contradicts -> contradicts. Judge and fixture key share definitions so the
# eval harness (eval_canon.py) is measuring the real prompt, not a proxy.
_DEDUP_PROMPT = """\
Here are two scientific claims. Decide their relationship:

SAME       — they state the exact same fact under the same conditions
             (material/method/quantity/regime), differing only in wording
             or in extra descriptive detail that restates the same finding.
             This does NOT extend to a specific quantitative formula,
             numeric value, or named mechanism that one claim asserts and
             the other does not — that added specificity is a narrower
             claim (see DIFFERENT).
CONTRADICTS — they share the same scope (same material/method/regime) but
             assert opposite conclusions.
DIFFERENT  — anything else: a genuinely distinct claim, a broader/narrower
             claim, a different scope, or claims about different things
             entirely (even if they sound superficially similar or share a
             method/definition). In particular, a specific quantitative
             formula, value, or mechanism is NARROWER than the general or
             qualitative principle it instantiates: a general trend and the
             specific formula that quantifies it are DIFFERENT, not the same
             fact stated in more detail.

Default to DIFFERENT unless you are confident the claims are the SAME fact
under the SAME conditions. A merge of two claims that are not really the
same is the dangerous error here — when in doubt, DIFFERENT.

CLAIM A: {claim_a}

CLAIM B: {claim_b}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdict": "same" | "different" | "contradicts",
  "confidence": <float 0.0-1.0>,
  "rationale": "<one sentence>"
}}
"""


def _coerce_verdict(data: dict[str, Any] | None, *, default_rationale: str) -> Verdict:
    """Normalize a raw model JSON payload into a :class:`Verdict`.

    Bias-safe on any malformed/missing field: an unrecognized/missing
    ``verdict`` degrades to ``"different"`` (never a silent ``"same"``),
    confidence coerces to ``0.0`` (never a silent high-confidence merge) on
    a bad value, and clamps to ``[0.0, 1.0]``.
    """
    verdict: Verdict3 = "different"
    confidence = 0.0
    rationale = default_rationale
    if isinstance(data, dict):
        raw_verdict = data.get("verdict")
        if raw_verdict in ("same", "different", "contradicts"):
            verdict = raw_verdict
        raw_confidence = data.get("confidence")
        if isinstance(raw_confidence, int | float) and not isinstance(
            raw_confidence, bool
        ):
            confidence = max(0.0, min(1.0, float(raw_confidence)))
        raw_rationale = data.get("rationale")
        if isinstance(raw_rationale, str) and raw_rationale.strip():
            rationale = raw_rationale.strip()
    return Verdict(verdict=verdict, confidence=confidence, rationale=rationale)


def dedup_judge(a: str, b: str) -> Verdict:
    """THE crux call — is claim ``a`` the same as claim ``b``, different, or
    a contradiction? MEDIUM tier, one bounded pairwise judgment.

    Merges only on genuinely the same fact + same conditions; any real
    difference -> ``"different"``; opposite polarity at the same scope ->
    ``"contradicts"``. Biased hard toward ``"different"`` — a dispatch
    error or unparseable response also degrades to ``"different"`` at
    confidence 0.0, never a silent ``"same"`` (the over-merge guard holds
    even on infrastructure failure).
    """
    prompt = _DEDUP_PROMPT.format(claim_a=a, claim_b=b)
    res = dispatch(
        LlmRequest(
            tier=Tier.MEDIUM,
            messages=[
                {"role": "system", "content": _DEDUP_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="taproot:dedup",
        )
    )
    if res.error:
        log.warning("taproot: dedup_judge dispatch failed: %s", res.error)
        return _coerce_verdict(None, default_rationale=f"dispatch error: {res.error}")
    data = res.data or _parse_json_object(res.text)
    return _coerce_verdict(data, default_rationale="unparseable model output")


# ── merge_confirm — BIG, only on a risky same ───────────────────────────

_MERGE_CONFIRM_SYS = (
    "You are a skeptical scientific claim auditor, double-checking a "
    "proposed merge. Reply with ONLY the requested JSON object, no prose."
)

_MERGE_CONFIRM_PROMPT = """\
A cheaper model proposed merging these two claims as the SAME fact, but
was not confident. Scrutinize this proposed merge carefully: is it truly
the same fact under the same conditions (material/method/quantity/regime),
or does it differ in a way the cheaper model missed? Merging two claims
that are not really the same fuses distinct facts under one hub — the
dangerous error. If you are not clearly confident it's the same, say so.

CLAIM A: {claim_a}

CLAIM B: {claim_b}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "verdict": "same" | "different" | "contradicts",
  "confidence": <float 0.0-1.0>,
  "rationale": "<one sentence>"
}}
"""


def merge_confirm(claim_a: str, claim_b: str) -> Verdict:
    """Re-check a low-confidence ``dedup_judge`` ``"same"`` — BIG tier, rare
    (only called from :func:`place` on a risky merge).

    Same bias-safe degrade as :func:`dedup_judge`: any dispatch/parse
    failure returns ``"different"`` at confidence 0.0, never a silent
    confirmation.
    """
    prompt = _MERGE_CONFIRM_PROMPT.format(claim_a=claim_a, claim_b=claim_b)
    res = dispatch(
        LlmRequest(
            tier=Tier.BIG,
            messages=[
                {"role": "system", "content": _MERGE_CONFIRM_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="taproot:merge-confirm",
        )
    )
    if res.error:
        log.warning("taproot: merge_confirm dispatch failed: %s", res.error)
        return _coerce_verdict(None, default_rationale=f"dispatch error: {res.error}")
    data = res.data or _parse_json_object(res.text)
    return _coerce_verdict(data, default_rationale="unparseable model output")


# ── place — deterministic branching ─────────────────────────────────────


def place(
    claim: CanonicalClaim,
    judged: list[tuple[Candidate, Verdict]],
    *,
    confidence_threshold: float = MERGE_CONFIDENCE_THRESHOLD,
    merge_confirm_fn: Any = merge_confirm,
) -> Placement:
    """Decide where ``claim`` lands, given its judged candidates.

    Branching (deterministic, in priority order):

    1. Any ``"same"`` at/above ``confidence_threshold`` -> **attach** to the
       first such candidate (``judged`` is expected in :func:`block`'s
       distance order, so "first" = closest).
    2. Else any ``"same"`` below threshold -> re-check the closest one via
       :func:`merge_confirm` (BIG). Confirmed (``"same"`` at/above
       threshold) -> **attach**; not confirmed -> **needs_review** (design
       #16: a risky merge is never auto-applied — the caller should file a
       ``kind='todo'``, not attach or silently drop it).
    3. Else any ``"contradicts"`` -> **new_contradicts**, linked to the
       first such candidate.
    4. Else -> **new**.

    ``judged`` may be empty (``block`` found no candidates) -> **new**.
    ``merge_confirm_fn`` is injectable for tests (default
    :func:`merge_confirm`, a real BIG dispatch).
    """
    same_high = [
        (cand, v)
        for cand, v in judged
        if v["verdict"] == "same" and v["confidence"] >= confidence_threshold
    ]
    if same_high:
        cand, _ = same_high[0]
        return Placement(
            action="attach",
            hub_ref_id=cand.hub_ref_id,
            reason="confirmed same (high confidence)",
        )

    same_low = [(cand, v) for cand, v in judged if v["verdict"] == "same"]
    if same_low:
        cand, v = same_low[0]
        confirm = merge_confirm_fn(claim.sentence, cand.claim)
        if (
            confirm["verdict"] == "same"
            and confirm["confidence"] >= confidence_threshold
        ):
            return Placement(
                action="attach",
                hub_ref_id=cand.hub_ref_id,
                reason=f"merge-confirmed: {confirm['rationale']}",
            )
        return Placement(
            action="needs_review",
            hub_ref_id=cand.hub_ref_id,
            reason=(
                f"low-confidence same (dedup={v['confidence']:.2f}) not "
                f"confirmed by merge-confirm: {confirm['rationale']}"
            ),
        )

    contradicts = [(cand, v) for cand, v in judged if v["verdict"] == "contradicts"]
    if contradicts:
        cand, v = contradicts[0]
        return Placement(
            action="new_contradicts",
            contradicts_hub_ref_id=cand.hub_ref_id,
            reason=v["rationale"],
        )

    return Placement(action="new", reason="no matching or contradicting candidate")


__all__ = [
    "MERGE_CONFIDENCE_THRESHOLD",
    "TAPROOT_CLAIM",
    "TAPROOT_NAMESPACE",
    "TAPROOT_REVIEW",
    "Candidate",
    "CanonicalClaim",
    "Placement",
    "Verdict",
    "block",
    "claim_sha",
    "dedup_judge",
    "extract_claim",
    "merge_confirm",
    "place",
]
