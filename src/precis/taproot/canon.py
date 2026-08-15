"""Taproot Phase 1 — flat claim canonicalization (the gate).

The Phase-1 canonicalizer gate. Design context: ``docs/backlog/taproot.md``
§"The crux — canonicalization = flat claim dedup". Acceptance bar (decided):
the fixture eval scores **zero over-merges** — bar = 0, no exceptions
(``tests/test_taproot_eval_canon.py``). Four functions, one cascade:

1. :func:`extract_claim` — SMALL/local. A chunk of text -> a
   :class:`ClaimExtraction`: zero or more AIDA-atomic claims, an optional
   surviving ``compound`` (the bundling summary sentence, kept only when
   decomposition actually split something), and the rejected conjuncts
   (:class:`NotClaim`) that couldn't be grounded — the ``NO-CLAIM`` outcome
   is an *empty* extraction (``ClaimExtraction.is_empty``), not ``None``.
   :func:`extract_claim_strict_big` is the identical contract at BIG tier
   — selective escalation of hubs SMALL got wrong (P2-10), not a blanket
   bump.
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

Every model call routes through :mod:`precis.utils.llm.router` —
no hardcoded model, no direct subprocess/HTTP. Phase 1 persists nothing: no
migration, no hub/edge writes — see ``taproot.md`` §"Target + blast radius".
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from precis.utils.llm.router import LlmRequest, Tier, dispatch

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

# ── the closed TAPROOT namespace ────────────────────────────────────────────
#
# Registered here (mirrors ROLE3, ``workers/classify.py``). The axis
# classifier that writes these tags is built (``data/axes/taproot.yaml`` via
# ``workers/axis_pass.py``); `block` reads it, and on an untagged corpus it
# finds no tagged hubs and returns empty (brand-new claim) — the correct
# degrade.
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


class NotClaim(TypedDict):
    """One conjunct :func:`extract_claim` rejected rather than atomized —
    forward-looking, vague, or a comparative with no comparator (the
    carbon-nanomaterials worked example in
    ``docs/backlog/taproot-atomic-claims.md``). Step 8's memo payload: these
    get folded into the surviving compound hub's meta, never minted as a
    hub nobody can ever ground."""

    text: str  # the rejected conjunct, verbatim-normalized
    reason: str  # why it can't be grounded ("forward-looking", "vague", ...)


@dataclass(frozen=True)
class ClaimExtraction:
    """The full result of :func:`extract_claim` on one chunk.

    * ``atoms`` — each AIDA-atomic (one subject-predicate fact), evidence-
      bearing. Empty on NO-CLAIM.
    * ``compound`` — the surviving bundling sentence, or ``None``. Kept
      only when decomposition genuinely split something (see
      :func:`_coerce_extraction`) — an already-atomic claim has no
      compound and mints no ``conjunct-of`` links.
    * ``not_claims`` — rejected conjuncts, kept for the compound hub's
      audit memo (step 8), never minted.

    Construct via :func:`extract_claim`, not directly — the three-way
    invariant between ``atoms``/``compound``/``not_claims`` is enforced at
    parse time by :func:`_coerce_extraction`, mirroring :func:`_coerce_verdict`'s
    bias-safe degrade philosophy (fail toward the smaller, safer claim set,
    never mint a bad or degenerate hub).
    """

    atoms: tuple[CanonicalClaim, ...]
    compound: CanonicalClaim | None
    not_claims: tuple[NotClaim, ...]

    @property
    def is_empty(self) -> bool:
        """NO-CLAIM: nothing groundable survived (today's ``None``)."""
        return not self.atoms and self.compound is None


#: The NO-CLAIM outcome — dispatch error, empty input, or unparseable/
#: all-rejected model output. A single frozen instance (all fields are
#: immutable) so every degrade path returns the identical sentinel.
_EMPTY_EXTRACTION = ClaimExtraction(atoms=(), compound=None, not_claims=())


def _coerce_extraction(
    atoms: list[CanonicalClaim],
    compound: CanonicalClaim | None,
    not_claims: list[NotClaim],
    source_sentence: str = "",
) -> ClaimExtraction:
    """Enforce the atoms/compound/not_claims invariants on parsed model
    output — the ``ClaimExtraction`` analogue of :func:`_coerce_verdict`.

    - Zero atoms -> NO-CLAIM: any compound the model still emitted is
      dropped (nothing groundable survived to bundle).
    - A lone atom with nothing rejected -> the compound is folded away
      (never mint a degenerate 1-conjunct bundle); an already-atomic claim
      has no compound.
    - A compound is kept only when decomposition did something real:
      ``len(atoms) >= 2`` (an actual split) or ``not_claims`` non-empty
      (something was rejected out of the bundle).
    - Two-plus atoms with **no** compound -> **synthesize** the compound
      from ``source_sentence`` instead of discarding (P1-8:
      ``docs/backlog/taproot-migration-extraction-quality-gates.md``). The
      source sentence *is* the bundle by construction — the model asserted
      a real split but missed the bundling-sentence field, a formatting
      miss, not evidence the split itself was wrong (fi177585 lost a good
      2-atom split this way). Only when the caller has no source text to
      fall back on (empty ``source_sentence`` — shouldn't happen once a
      caller passes it, kept as a bias-safe floor) does this still degrade
      to NO-CLAIM: without a bundle, downstream (backfill's prose rewrite
      targets the compound, else ``atoms[0]``) would silently cite only the
      first atom.
    """
    atoms_t = tuple(atoms)
    not_claims_t = tuple(not_claims)
    if not atoms_t or (len(atoms_t) == 1 and not not_claims_t):
        compound = None
    elif len(atoms_t) >= 2 and compound is None:
        synthesized = source_sentence.strip()
        if synthesized:
            log.info(
                "taproot: extract_claim returned %d atoms with no compound; "
                "synthesizing compound from the source sentence",
                len(atoms_t),
            )
            compound = CanonicalClaim(sentence=synthesized, scope={})
        else:
            log.warning(
                "taproot: extract_claim returned %d atoms with no compound "
                "and no source sentence to synthesize from; degrading to "
                "NO-CLAIM (partial-citation guard)",
                len(atoms_t),
            )
            return _EMPTY_EXTRACTION
    return ClaimExtraction(atoms=atoms_t, compound=compound, not_claims=not_claims_t)


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

#: The self-contained/world-claim/specific/plain-text rules a claim sentence
#: must obey to be read ALONE, without its source passage — shared verbatim
#: between :data:`_EXTRACT_PROMPT` (below) and
#: :mod:`precis.taproot.directed`'s qualify prompt (docs/backlog/
#: taproot-directed-claim-minting.md), so the two never drift into divergent
#: copies of the same rule text. ``claim = null`` here maps to
#: :mod:`.directed`'s ``"claim": null`` / ``supported: false`` convention —
#: the same escape hatch, worded for whichever JSON contract is reading it.
CLAIM_FORM_RULES = """\
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
   -> "g-C₃N₄", "cm$^2$/Vs" -> "cm²/Vs")."""

#: Why the prompt's "mechanism clauses stay attached" rule exists (P2-13,
#: ``docs/backlog/taproot-migration-extraction-quality-gates.md``): the only
#: relation :mod:`precis.taproot.migrate` mints between atoms of a split is
#: ``conjunct-of``, a symmetric-ish "these are peers of one bundle" edge —
#: there is no vocabulary for "Y is the mechanism/cause for X". Splitting a
#: causal clause ("because…", "enabled by…", "due to…") into its own atom
#: therefore doesn't just lose information, it *misrepresents* it: two
#: independent peer facts where the source stated one fact and its
#: explanation (pilot: fi176422, fi176399). Keeping it attached — inside
#: the explaining atom's sentence or its method/regime scope — is the only
#: shape the current relation model can carry correctly.
_EXTRACT_PROMPT = (
    """\
Does this passage assert a specific, citable scientific claim (a concrete
result, measurement, definition, capability, or finding), or does it only
point to other work without asserting anything itself (e.g. "See [12]", a
Related-Work sentence that is only a citation list, "several studies exist")?

PASSAGE:
{excerpt}

"""
    + CLAIM_FORM_RULES
    + """

Atomic, by enumeration: each claim you emit must assert exactly ONE
subject-predicate fact. Work in two steps:
  Step 1 — ENUMERATE first, into "assertions": list every distinct
  assertion the passage makes, one entry per subject-predicate fact, in
  the order they appear. If the passage bundles several conjuncts ("X has
  A, B, and C" / "X, which also does Y"), each conjunct is its own entry.
  Do this before you write a single "claim" — emitting atoms without
  enumerating first is how a bundled sentence silently loses conjuncts
  (in practice, every conjunct but the last).
  Step 2 — EMIT one outcome per enumerated entry: either one self-
  contained atom in "claims" (obeying rules 1-4 above — resolve referents
  per atom, carry its own material/method/quantity/regime, not the
  bundle's), or one entry in "not_claims" if it can't be grounded alone.
  The union of "claims" and "not_claims" must cover every entry in
  "assertions" — dropping an enumerated assertion without recording it in
  either list is an error, never a silent simplification.

A conjunct that is forward-looking ("will enable…", "paves the way for…"),
vague with no concrete referent, or a comparative with no stated comparator
("exceptional", "superior") cannot be grounded as its own atom — put it in
"not_claims" with a one-phrase reason instead of forcing it into a weak
atom.

Modality: a clause asserted only under a counterfactual, hypothetical, or
contrastive foil — "would", "could", "if", "whereas X would…", "absent
Y…" — is not something the passage asserts as true. It must NEVER become a
bare indicative atom. Either give it an explicit regime qualifier that
keeps the hypothetical visible in the claim itself, or — if it names no
factual regime worth keeping — put it in "not_claims" with reason
"counterfactual — not asserted by the source". Never drop the qualifier
and keep the clause as if it were a fact.

Mechanism clauses ("because…", "enabled by…", "due to…", "arises from…")
are not a peer conjunct in the enumeration — they explain a claim, they
don't add a second one. Fold a mechanism clause into the atom it explains
(inline in the sentence, or into that atom's method/regime scope); never
enumerate it, split it into its own atom, or file it under "not_claims".

Only return the original sentence as "compound" when it genuinely bundles
two or more groundable-or-rejected parts — an already-atomic passage has no
compound.
(*Absolute* is already handled by the material/method/quantity/regime
scope fields; *Declarative* is implied by normalizing to sentences — this
rule adds *Atomic*, the one AIDA criterion not yet enforced.)

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
- "Carbon nanomaterials have exceptional mechanical, optoelectronic, and
  physicochemical characteristics and tunability that enable
  next-generation technologies, particularly in advanced electronics." ->
  assertions = [mechanical characteristics, optoelectronic characteristics,
  "enable next-generation technologies", "exceptional"/"particularly in
  advanced electronics"]; splits into atoms for the concrete, groundable
  conjuncts ("Carbon nanomaterials exhibit tunable mechanical
  characteristics.", "Carbon nanomaterials exhibit tunable optoelectronic
  characteristics.", ...); "enable next-generation technologies"
  (forward-looking) and "exceptional"/"particularly in advanced
  electronics" (comparative/vague, no comparator) go to not_claims; the
  original sentence is the compound.
- "The tandem catalyst's two sites operate independently on the shared
  support, whereas in homogeneous solution the same sites would
  immediately neutralize each other."  -> assertions = [the sites operate
  independently on the shared support, the counterfactual homogeneous-
  solution foil]; the first is a claim ("The tandem catalyst's two sites
  operate independently on the shared support."); the "whereas ... would"
  clause is a counterfactual foil, NOT an assertion — it goes to
  not_claims with reason "counterfactual — not asserted by the source".
  Never mint "The sites neutralize each other in homogeneous solution" as
  a fact — that flips the foil into a false indicative claim.

For each claim you emit: give the normalized sentence plus any of
material/method/quantity/regime it names (omit keys it doesn't name).
material = the substance/compound the claim is about; method = the named
technique, procedure, or instrument; quantity = a numeric measure with
units ("130 GPa", "450 h⁻¹", "92% yield") — never a bare description or
label; regime = the named condition the claim holds under ("RT", "under
UV", "homogeneous solution"). If the passage asserts nothing groundable at
all, "claims" is an empty list.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "assertions": [
    "<each distinct assertion you found in step 1, one per subject-predicate fact>"
  ],
  "claims": [
    {{
      "claim": "<one normalized, atomic sentence>",
      "material": "<optional>",
      "method": "<optional>",
      "quantity": "<optional>",
      "regime": "<optional>"
    }}
  ],
  "compound": "<original bundling sentence>" | null,
  "not_claims": [
    {{
      "text": "<rejected conjunct, verbatim-normalized>",
      "reason": "<why it can't be grounded>"
    }}
  ]
}}
"""
)

_SCOPE_KEYS = ("material", "method", "quantity", "regime")

#: Sane cap on a scope value's length (P1-9). Scope is a short qualifier
#: ("RT", "450 h⁻¹"), not a second sentence — anything longer is the model
#: dumping prose into a scope key rather than the claim text, so drop it
#: rather than let it perturb hub identity (``hub.mint_hub`` feeds scope
#: into ``make_taproot_hub_paper_id``).
_SCOPE_VALUE_MAX_CHARS = 120


def _valid_scope_value(key: str, value: str) -> bool:
    """P1-9: constrain scope values so junk never reaches hub identity.

    Drop empty or absurdly long values outright. ``quantity`` specifically
    must contain at least one digit — fi176359's ``quantity: "rectangular
    outline"`` / ``"pin count conventions"`` were prose descriptions, not
    measures, and junk scope perturbs
    ``hub.mint_hub``/``make_taproot_hub_paper_id``'s identity key. Never
    raises — an invalid value is dropped, not a reason to fail the whole
    extraction."""
    if not value or len(value) > _SCOPE_VALUE_MAX_CHARS:
        return False
    if key == "quantity" and not any(ch.isdigit() for ch in value):
        return False
    return True


def _parse_claim_item(item: dict[str, Any]) -> CanonicalClaim | None:
    """Parse one ``{"claim": ..., "material": ..., ...}`` object (a
    ``claims[]`` entry, or the whole payload under the legacy single-object
    shape) into a :class:`CanonicalClaim`, or ``None`` if it carries no
    usable claim text.

    Scope values are validated per :func:`_valid_scope_value` (P1-9) — a
    key with a bad value is dropped, never the whole claim."""
    claim = item.get("claim")
    if not isinstance(claim, str) or not claim.strip():
        return None
    scope = {}
    for key in _SCOPE_KEYS:
        raw = item.get(key)
        if not isinstance(raw, str):
            continue
        value = raw.strip()
        if _valid_scope_value(key, value):
            scope[key] = value
    return CanonicalClaim(sentence=claim.strip(), scope=scope)


def _parse_not_claim(item: dict[str, Any]) -> NotClaim | None:
    """Parse one ``not_claims[]`` entry, or ``None`` if it carries no
    rejected-conjunct text."""
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    raw_reason = item.get("reason")
    reason = (
        raw_reason.strip()
        if isinstance(raw_reason, str) and raw_reason.strip()
        else "unspecified"
    )
    return NotClaim(text=text.strip(), reason=reason)


class ExtractionUnavailable(RuntimeError):
    """Raised by :func:`extract_claim_strict` when the dispatch itself
    failed (infra error — transport down, timeout, dead endpoint) rather
    than the model producing a semantic NO-CLAIM. Callers that need to tell
    "the model said nothing groundable" apart from "the model never ran"
    (e.g. a migration dry-run report) should use the strict variant and
    catch this instead of reading :func:`extract_claim`'s empty-extraction
    fail-safe as a verdict."""


def extract_claim(chunk_text: str) -> ClaimExtraction:
    """Extract the atomic claims (+ optional compound + rejected conjuncts)
    from ``chunk_text`` — a :class:`ClaimExtraction`.

    SMALL/local tier — cheap, per-chunk. Returns the empty extraction
    (``ClaimExtraction.is_empty``) when the chunk is pure-pointer / meta
    (the ``NO-CLAIM`` outcome, taproot.md Axis A stage 0') — including on a
    dispatch error or unparseable model output (fail safe: no claim rather
    than a bad one). A caller that must distinguish a real NO-CLAIM from a
    dead dispatch (infra failure) should use :func:`extract_claim_strict`
    instead, which raises on the latter.

    Parses the current ``{"claims": [...], "compound": ..., "not_claims":
    [...]}`` contract; tolerates the legacy ``{"claim": ..., "material":
    ...}`` single-object shape a SMALL-tier model may still regress to
    (degrades to one atom, no compound/not_claims — same fail-safe posture
    as the rest of this module). :func:`_coerce_extraction` enforces the
    atoms/compound/not_claims invariants either way.
    """
    return _extract_claim_impl(chunk_text, strict=False)


def extract_claim_strict(chunk_text: str) -> ClaimExtraction:
    """Like :func:`extract_claim`, but raises :class:`ExtractionUnavailable`
    on a dispatch error instead of degrading to the empty extraction.

    A dispatch error is infrastructure failure (the model never ran) —
    conflating it with a semantic NO-CLAIM is exactly the failure mode that
    produced the melchior dry-run's all-``no-claim`` garbage report when
    every call ECONNREFUSED'd. Unparseable-but-successful model output
    still degrades to the empty extraction here too: the model *did*
    respond, so that really is a semantic no-claim, not an infra failure.
    """
    return _extract_claim_impl(chunk_text, strict=True)


def extract_claim_strict_big(chunk_text: str) -> ClaimExtraction:
    """Like :func:`extract_claim_strict`, but dispatches at :data:`Tier.BIG`
    instead of :data:`Tier.SMALL` (P2-10, ``docs/backlog/
    taproot-migration-extraction-quality-gates.md``).

    Same prompt, same :func:`_coerce_extraction` contract — only the model
    tier differs. For selective escalation of the extractions the SMALL
    pass got wrong (``lossy``/``nested``/``no-claim`` verdicts) rather than
    a blanket BIG-tier bump over the whole corpus; the caller (``taproot
    migrate``'s escalation path) decides which hubs qualify, this function
    just dispatches the same contract at higher capability.
    """
    return _extract_claim_impl(chunk_text, strict=True, tier=Tier.BIG)


#: Wall-clock ceiling for one MEDIUM-tier extraction call. One-shot JSON
#: over a single sentence — generous headroom over the observed ~30-90 s
#: subagent-probe latencies, well under the BIG chain's observed multi-minute
#: stalls (fi176812's timeout).
_MEDIUM_EXTRACT_TIMEOUT_S = 240.0

#: Pause before retrying a dispatch error. The fast exits correlate with
#: host load (dense flakes exactly while a container gate saturated the
#: cores, 2026-08-15) — an immediate retry lands in the same spike. Tests
#: monkeypatch this to 0.
_FLAKE_RETRY_BACKOFF_S = 5.0


def extract_claim_strict_medium(chunk_text: str) -> ClaimExtraction:
    """Like :func:`extract_claim_strict`, but dispatches at
    :data:`Tier.MEDIUM` with an additional format-flake guard on top of the
    strict dispatch-error contract — routed through :func:`dispatch` (so it
    is budget-metered and logs an ``llm_call_log`` row like every other
    call in this module).

    History: this was a deliberate ``call_claude_p`` bypass (the
    ``fix_gripe`` pattern) pinned to haiku, because prod's
    ``llm.chain.medium`` rung at the time pointed at an OSS model that
    intermittently broke the JSON output contract (2026-08-14 4-hub A/B
    probe, ``docs/backlog/taproot-migration-extraction-quality-gates.md``
    Round 2). The 2026-08-15 chain cutover set ``llm.chain.medium`` to
    ``claude-haiku-4-5-20251001`` via ``claude_p`` — the same model this
    function used to pin to — dissolving the pin-vs-chain conflict that
    justified the bypass, so it now dispatches through the normal ladder.
    Model still steerable via the operator ``llm.chain.medium`` row
    (:func:`precis.utils.llm.router.resolve_model` at ``Tier.MEDIUM``), not
    ``PRECIS_MODEL_HAIKU``.

    Format-flake guard, two retryable shapes (each re-asked at most once):

    * **Dispatch timeout** (``res.error`` set and ``res.timed_out``):
      raises :class:`ExtractionUnavailable` immediately, never retried —
      retrying a 240 s stall would double it for nothing.
    * **Dispatch error, not a timeout**: retried once after
      :data:`_FLAKE_RETRY_BACKOFF_S`; a repeat raises
      :class:`ExtractionUnavailable`.
    * **Unparseable reply** (``res.data`` isn't a dict and the raw text
      doesn't parse as one): retried once; a repeat raises
      :class:`ExtractionUnavailable` — a model persistently off-contract is
      an infra-grade failure, never a NO-CLAIM.
    * **Empty-empty JSON** (valid payload, no atoms AND no ``not_claims``,
      on non-empty input): retried once; a repeat is accepted as a genuine
      NO-CLAIM (pure-pointer passages legitimately produce it).

    The guard lives only here, not in the per-chunk SMALL backfill path,
    where genuine no-claim chunks are common and a blanket retry would
    double their cost.

    Raises :class:`ExtractionUnavailable` when the dispatch itself fails
    (transport down, timeout, dead endpoint, persistent format-flake) —
    same strict contract as the other ``_strict`` variants.
    """
    text = (chunk_text or "").strip()
    if not text:
        return _EMPTY_EXTRACTION
    excerpt = text[:_EXTRACT_EXCERPT_CHARS]
    prompt = _EXTRACT_PROMPT.format(excerpt=excerpt)

    extraction = _EMPTY_EXTRACTION
    for attempt in range(2):
        res = dispatch(
            LlmRequest(
                tier=Tier.MEDIUM,
                messages=[
                    {"role": "system", "content": _EXTRACT_SYS},
                    {"role": "user", "content": prompt},
                ],
                prompt=prompt,
                source="taproot:extract-medium",
                timeout_s=_MEDIUM_EXTRACT_TIMEOUT_S,
            )
        )
        if res.error:
            if res.timed_out:
                raise ExtractionUnavailable(res.error)
            if attempt == 0:
                log.info(
                    "taproot: medium-tier extract dispatch failed: %s — "
                    "retrying once in %.0fs (format-flake guard)",
                    res.error,
                    _FLAKE_RETRY_BACKOFF_S,
                )
                time.sleep(_FLAKE_RETRY_BACKOFF_S)
                continue
            raise ExtractionUnavailable(res.error)

        data = res.data if isinstance(res.data, dict) else None
        if data is None:
            data = _parse_json_object(res.text)
        if not isinstance(data, dict):
            if attempt == 0:
                log.info(
                    "taproot: medium-tier extract reply had no parseable "
                    "JSON — retrying once (format-flake guard)"
                )
                continue
            raise ExtractionUnavailable("no parseable JSON")

        extraction = _extraction_from_payload(data, excerpt)
        if not (extraction.is_empty and not extraction.not_claims):
            return extraction
        if attempt == 0:
            log.info(
                "taproot: medium-tier extraction came back empty-empty on "
                "non-empty input — retrying once (format-flake guard)"
            )
    return extraction


def _extract_claim_impl(
    chunk_text: str, *, strict: bool, tier: Tier = Tier.SMALL
) -> ClaimExtraction:
    """Shared body of :func:`extract_claim` / :func:`extract_claim_strict` /
    :func:`extract_claim_strict_big` — see those for the behavioral
    contract; ``strict`` controls only whether a dispatch error raises
    (:class:`ExtractionUnavailable`) or degrades to the empty extraction;
    ``tier`` controls only which model capability the dispatch targets."""
    text = (chunk_text or "").strip()
    if not text:
        return _EMPTY_EXTRACTION
    excerpt = text[:_EXTRACT_EXCERPT_CHARS]
    prompt = _EXTRACT_PROMPT.format(excerpt=excerpt)
    res = dispatch(
        LlmRequest(
            tier=tier,
            messages=[
                {"role": "system", "content": _EXTRACT_SYS},
                {"role": "user", "content": prompt},
            ],
            prompt=prompt,
            source="taproot:extract" if tier is Tier.SMALL else "taproot:extract-big",
        )
    )
    if res.error:
        log.warning("taproot: extract_claim dispatch failed: %s", res.error)
        if strict:
            raise ExtractionUnavailable(res.error)
        return _EMPTY_EXTRACTION
    data = res.data or _parse_json_object(res.text)
    if not isinstance(data, dict):
        return _EMPTY_EXTRACTION
    return _extraction_from_payload(data, excerpt)


def _extraction_from_payload(data: dict[str, Any], excerpt: str) -> ClaimExtraction:
    """Parse a model-response payload dict into a :class:`ClaimExtraction` —
    the transport-independent half of extraction, shared by
    :func:`_extract_claim_impl` and :func:`extract_claim_strict_medium`."""
    raw_claims = data.get("claims")
    if isinstance(raw_claims, list):
        atoms = []
        for item in raw_claims:
            if isinstance(item, dict):
                parsed = _parse_claim_item(item)
                if parsed is not None:
                    atoms.append(parsed)
        not_claims = []
        for item in data.get("not_claims") or []:
            if isinstance(item, dict):
                parsed_nc = _parse_not_claim(item)
                if parsed_nc is not None:
                    not_claims.append(parsed_nc)
        compound = None
        raw_compound = data.get("compound")
        if isinstance(raw_compound, str) and raw_compound.strip():
            compound = CanonicalClaim(sentence=raw_compound.strip(), scope={})
        _log_assertion_arity_drift(data.get("assertions"), atoms, not_claims)
        return _coerce_extraction(atoms, compound, not_claims, source_sentence=excerpt)

    # Legacy single-object degrade: {"claim": ..., "material": ...}.
    single = _parse_claim_item(data)
    if single is None:
        return _EMPTY_EXTRACTION
    return ClaimExtraction(atoms=(single,), compound=None, not_claims=())


def _log_assertion_arity_drift(
    raw_assertions: Any, atoms: list[CanonicalClaim], not_claims: list[NotClaim]
) -> None:
    """P1-5 telemetry: the prompt now forces an enumerate-then-emit step —
    the model lists ``assertions`` before emitting ``claims``/``not_claims``.
    This is diagnostic only (never changes the extraction result — the
    bias-safe posture of this module is to never fail an extraction on a
    format nit): a mismatch between the enumerated count and
    ``len(atoms) + len(not_claims)`` signals the model skipped emitting an
    outcome for something it enumerated (or enumerated coarser/finer than
    it emitted), an early warning for the exact recency-drop failure mode
    P1-5 targets, without gating on a heuristic that doesn't know sentence
    semantics."""
    if not isinstance(raw_assertions, list):
        return
    enumerated = len(raw_assertions)
    emitted = len(atoms) + len(not_claims)
    if enumerated != emitted:
        log.info(
            "taproot: extract_claim enumerated %d assertion(s) but emitted "
            "%d claim(s)/not_claim(s) — possible dropped conjunct",
            enumerated,
            emitted,
        )


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
    store: Store,
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
    "CLAIM_FORM_RULES",
    "MERGE_CONFIDENCE_THRESHOLD",
    "TAPROOT_CLAIM",
    "TAPROOT_NAMESPACE",
    "TAPROOT_REVIEW",
    "Candidate",
    "CanonicalClaim",
    "ClaimExtraction",
    "ExtractionUnavailable",
    "NotClaim",
    "Placement",
    "Verdict",
    "block",
    "claim_sha",
    "dedup_judge",
    "extract_claim",
    "extract_claim_strict",
    "extract_claim_strict_big",
    "merge_confirm",
    "place",
]
