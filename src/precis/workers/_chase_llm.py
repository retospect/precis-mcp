"""Default-off LLM hooks for the finding-chase worker.

Split out of ``workers/chase.py`` 2026-06-05. These three functions
are the *only* paths in ``chase`` that issue paid LLM calls:

* :func:`_verify_support_with_caveats` reads the target chunk + claim
  and records support / caveats / cited-others on the chain entry. Also
  the verify hook ``workers/hub_refine.py`` calls for every discover
  candidate (paper or patent) — always-on there, not ``with_llm``-gated —
  where ``source_kind="patent"`` swaps in patent-aware reading rules
  (patent-evidence-parity).
* :func:`_disambiguate_candidates` resolves multi-cite chunks.
* :func:`_locate_chunk_in_target` confirms the ANN's chunk pick or
  picks a better one from the shown alternates.

All three activate only when the worker is invoked with
``with_llm=True`` (or env ``PRECIS_CHASE_LLM=1``); the deterministic
default chase path never touches them. Cost: ~$0.05–$0.10 per
established finding under Haiku.

Failure mode: any of these may return ``None`` (or the proposed
input, in the ``_locate_*`` case) on LLM error; callers must
tolerate that and fall back to deterministic behaviour.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.utils.llm.router import LlmRequest, Tier, route

if TYPE_CHECKING:
    from precis.workers.chase import _NextHopTarget

log = logging.getLogger(__name__)

#: Per-neighbour excerpt cap in the verify and judge prompts — a neighbour
#: is context only. ``hub_refine._JUDGE_NEIGHBOUR_CHARS`` is this value.
NEIGHBOUR_CHARS = 600
#: Abstract excerpt cap for the source-identity block (both prompts).
ABSTRACT_CHARS = 600

#: One sentence of instruction shared by the verify and judge prompts'
#: source-identity block.
SOURCE_IDENTITY_NOTE = (
    "use it to tell which system the paper studies; it is not evidence for or "
    "against the claim"
)


@dataclass(frozen=True)
class SourceIdentity:
    """Who the candidate source is — title, year and the opening of its
    abstract — shown to the verifier/judge so a chunk is never read without
    knowing which system its paper studies. Any field may be ``None``."""

    title: str | None = None
    year: int | None = None
    abstract: str | None = None


def render_source_identity(identity: SourceIdentity | None) -> str:
    """The prompt text for a source-identity block: a title/year line plus
    the first :data:`ABSTRACT_CHARS` of the abstract. A missing abstract
    renders "(no abstract on record)"; a missing title/year "(not available)"."""
    title = (identity.title or "").strip() if identity else ""
    year = identity.year if identity else None
    abstract = (identity.abstract or "").strip() if identity else ""
    head = f"{title or '(title not available)'} ({year or 'year unknown'})"
    return f"{head}\nABSTRACT (opening): " + (
        abstract[:ABSTRACT_CHARS] or "(no abstract on record)"
    )


def render_neighbours(neighbours: list[str] | None, *, what: str) -> str:
    """The prompt text for the prev/next neighbour block; ``None`` (caller
    could not look them up) renders "(not available)", an empty list "(none…)"."""
    if neighbours is None:
        return "(not available)"
    parts = [n[:NEIGHBOUR_CHARS] for n in neighbours if n]
    if not parts:
        return f"(none — this {what} has no live neighbours in the source)"
    return "\n\n".join(parts)


#: Appended into ``_PROMPT_VERIFY`` only when the candidate source is a
#: patent (patent-evidence-parity) — a patent chunk reads
#: differently from a paper chunk in three ways the verifier must know
#: about before it judges support.
_PATENT_VERIFY_NOTE = """
PATENT-SPECIFIC READING RULES (the source above is a patent):
  - Background / prior-art recitations describe what OTHERS did or knew
    before the invention — they are NOT the patentee's own support for the
    claim even when the wording sounds affirmative. Do not credit
    prior-art narration as the patentee's support.
  - A worked example may be written in the present tense purely as a
    matter of US patent-drafting convention, even when it was never
    actually carried out ("prophetic example"). That is a CAVEAT to record
    (e.g. "example is stated in prophetic present tense, not confirmed as
    reduced to practice") — not by itself a reason to mark supports="no".
  - Legal-claim language states legal SCOPE, not an empirical result.
    Claims-section text is filtered out before you ever see it, but if
    this chunk otherwise paraphrases claim-style scope language rather
    than describing an actual composition/process/measurement, treat that
    as weak-to-no empirical support.
"""

_PROMPT_VERIFY = """\
You are verifying whether a source {source_kind} chunk supports a specific
empirical claim made under a specific experimental setup. Judge support
for the claim EXACTLY AS STATED — not a looser or more general version.

CLAIM:
{claim}

SETUP (structured):
{scope_json}

CLAIM'S OWN SOURCE PASSAGE (what the claim was measured on):
{claim_source_text}

SOURCE: {source_kind} {target_cite_key}, chunk ord {target_chunk_ord}, section {section_path}

SOURCE IDENTITY ({identity_note}):
{source_identity}

CHUNK TEXT:
{target_chunk_text}

NEIGHBOURING CHUNKS in the same source — context only, to tell a
recitation or background sentence from the source's own result. Support is
never judged off a neighbour alone: a CHUNK that carries none of the claim
cannot borrow it from a neighbour.
{neighbours}
{patent_note}
First decide the chunk's STANCE toward the claim as stated: does it give
evidence FOR the claim, evidence AGAINST it (an opposite result or
tendency), or neither (on-topic but doesn't test the claim)?

Definitions:
  supports = "yes"      : the chunk affirmatively states or demonstrates
                          the claim as stated (verbatim or close paraphrase).
  supports = "partial"  : the chunk affirmatively supports the claim but
                          only under conditions/regimes listed in caveats —
                          REAL support that is merely scoped — or it
                          carries part of the claim and is silent on the
                          rest (other passages of the same source may carry
                          the rest; passages combine). A chunk that
                          reports a result RUNNING COUNTER to the claim, or
                          that only shares the topic without testing the
                          claim, is NOT "partial".
  supports = "no"       : the chunk does not support the claim — it only
                          shares the topic, tests something else, or reports
                          a result that runs COUNTER to the claim.
  caveats               : conditions, regimes, applicability limits that
                          qualify genuine support.
  claim_setup           : the system/sample, material, method and
                          conditions the claim is about (read it off the
                          claim and its own source passage above).
  passage_setup         : the same for the result THIS chunk reports.
  same_setup            : true iff claim_setup and passage_setup are the
                          same system, material, method and conditions;
                          false iff they differ; "unclear" when the chunk
                          does not say enough to tell. Decide this BEFORE
                          contradicts.
  contradicts           : true iff the chunk reports, for the SAME quantity
                          in the SAME system under comparable conditions
                          and method, a result or tendency OPPOSITE to the
                          claim's direction, or a materially different
                          strength or value. Decide this INDEPENDENTLY of
                          the supports label (a polarity verdict, not the
                          bottom of the supports scale) and be decisive
                          even when the chunk is on the same topic.
                          EXAMPLE: the claim asserts the method yields SMALL
                          (~7 nm) crystals; a chunk stating the method yields
                          BIGGER / LARGER crystallites reports the opposite
                          outcome -> contradicts=true, supports="no".
                          NOT a contradiction: a different value measured
                          on a different sample, material, device, method
                          or condition (a later single-layer device at
                          5,000 cm2/Vs does not contradict a few-layer film
                          at 10,000 cm2/Vs; a Raman estimate does not
                          contradict what nanoindentation measures) -- that
                          is a different finding: supports="no",
                          contradicts=false.
                          A chunk that supports PART of the claim and is
                          merely SILENT on the rest (no opposite result)
                          stays "partial" with contradicts=false.
                          contradicts may be true ONLY when same_setup is
                          true; otherwise contradicts=false.
  cited_others          : inline citation tokens that the chase
                          should follow (e.g. "[12]", "(Lin 1998)").
                          Empty if the chunk is the original source.
  terminal              : true iff the chunk DESCRIBES the
                          measurement itself; false if it merely
                          restates a value from elsewhere. A recitation
                          of another work's result is terminal=false.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "supports": "yes" | "partial" | "no",
  "support_reason": "<one sentence>",
  "caveats": ["<caveat 1>", ...],
  "claim_setup": "<system/sample, material, method, conditions the claim is about>",
  "passage_setup": "<the same for the result this chunk reports>",
  "same_setup": true | false | "unclear",
  "contradicts": true | false,
  "cited_others": ["<token 1>", ...],
  "terminal": true | false
}}
"""


def _verify_support_with_caveats(
    *,
    claim: str,
    scope: dict[str, Any],
    target_cite_key: str,
    target_chunk_ord: int,
    target_chunk_text: str,
    source_kind: str = "paper",
    claim_source_text: str | None = None,
    section_path: str | None = None,
    neighbours: list[str] | None = None,
    source_identity: SourceIdentity | None = None,
    with_request_hash: bool = False,
) -> dict[str, Any] | None:
    """Run the verifier LLM hook. Returns the parsed JSON dict or None.

    ``claim_source_text`` is the passage the claim itself was established
    from (capped at 3000 chars); the verifier compares the candidate's
    setup against it. ``None`` renders as "(not available)".

    ``section_path``, ``neighbours`` (the chunk's live ``ord`` ± 1 siblings,
    each capped at :data:`NEIGHBOUR_CHARS`) and ``source_identity`` (title,
    year, abstract opening) keep the chunk from being judged in isolation —
    the strict judge (``hub_refine.judge_edge_strict``) gets the same.
    ``None`` renders as "(not available)" (an empty ``neighbours`` list as
    "(none …)").

    ``with_request_hash=True`` adds the logged call's ``llm_call_log.
    request_hash`` to the returned dict under ``_llm_request_hash`` (only when
    one was logged), for a caller that stores it beside the verdict. Off by
    default: the returned shape is otherwise unchanged.

    ``source_kind`` names the candidate source ref's kind (``"paper"`` or
    ``"patent"``) — when it's a patent, :data:`_PATENT_VERIFY_NOTE` is
    spliced into the prompt so the verifier reads background/prior-art
    recitations, prophetic worked examples, and legal-claim scope language
    the way a patent (not a paper) requires (docs/backlog/
    patent-evidence-parity).
    """
    prompt = _PROMPT_VERIFY.format(
        claim=claim,
        scope_json=json.dumps(scope, sort_keys=True),
        source_kind=source_kind,
        target_cite_key=target_cite_key,
        target_chunk_ord=target_chunk_ord,
        section_path=section_path or "(not available)",
        identity_note=SOURCE_IDENTITY_NOTE,
        source_identity=render_source_identity(source_identity),
        neighbours=render_neighbours(neighbours, what="chunk"),
        target_chunk_text=target_chunk_text[:4000],  # cap context cost
        claim_source_text=(claim_source_text or "")[:3000] or "(not available)",
        patent_note=_PATENT_VERIFY_NOTE if source_kind == "patent" else "",
    )
    res = route(LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="chase:verify"))
    if res.error:
        log.warning("chase: verify hook failed: %s", res.error)
        return None
    request_hash = getattr(res, "request_hash", None)
    if with_request_hash and res.data is not None and isinstance(request_hash, str):
        return {**res.data, "_llm_request_hash": request_hash}
    return res.data


def is_corroborating(verification: dict[str, Any]) -> bool:
    """True iff a verify verdict should attach as corroborating evidence.

    A ``yes``, or a ``partial`` whose caveats *scope* the support rather than
    negate it (``contradicts`` false). A ``no``, or a ``partial`` flagged
    ``contradicts`` (the chunk reports a result counter to the claim), is NOT
    corroboration. The single source of truth for the attach decision, shared
    by ``hub_refine``'s write door and ``taproot.slice_refine_eval``'s
    would-attach replay so the pass and its eval harness never diverge.
    """
    supports = verification.get("supports")
    if supports == "yes":
        return True
    if supports == "partial":
        return not bool(verification.get("contradicts"))
    return False


_PROMPT_DISAMBIGUATE = """\
A chunk in a paper cites multiple references inline. Pick which
single reference most plausibly grounds a specific claim.

CHUNK TEXT:
{chunk_text}

CANDIDATE REFERENCES (0-indexed):
{candidates_table}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "pick_index": <int> | null,
  "reason": "<one sentence>"
}}

Use null only when NO candidate plausibly grounds the claim.
"""


def _disambiguate_candidates(
    chunk_text: str, candidates: list[_NextHopTarget]
) -> int | None:
    """Pick the most plausible candidate via LLM. Returns index or None."""
    table = "\n".join(
        f"  [{i}] {c.title or '(no title)'} ({c.year or '?'}) "
        f"doi={c.doi or '-'} s2={c.s2_id or '-'}"
        for i, c in enumerate(candidates)
    )
    prompt = _PROMPT_DISAMBIGUATE.format(
        chunk_text=chunk_text[:3000],
        candidates_table=table,
    )
    res = route(
        LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="chase:disambiguate")
    )
    if res.error:
        log.warning("chase: disambiguate hook failed: %s", res.error)
        return None
    pick = (res.data or {}).get("pick_index")
    return int(pick) if isinstance(pick, int) else None


_PROMPT_LOCATE = """\
You are confirming whether a proposed chunk in a paper is the right
place to find evidence for a specific claim. A lexical-overlap
ranker proposed the "main" chunk; three alternates from the same
paper are listed.

CLAIM: {claim}

MAIN proposal (ord {main_ord}):
{main_text}

ALTERNATES:
{alternates_table}

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "ok": true | false,
  "alternative_ord": <int> | null,
  "reason": "<one sentence>"
}}

ok=true: the proposal is the right chunk.
ok=false: pick alternative_ord, OR set null if NONE of the
shown chunks supports the claim (chase will tag dead_chain).
"""


def _locate_chunk_in_target(
    *,
    claim: str,
    proposed: tuple[int, int, str],
    alternates: list[tuple[int, int, str]],
) -> tuple[int, int, str] | None:
    """Confirm or correct the proposed chunk pick. Returns the chosen tuple."""
    alt_table = (
        "\n".join(f"  [ord {alt[1]}]: {alt[2][:200]}" for alt in alternates)
        or "  (none)"
    )
    prompt = _PROMPT_LOCATE.format(
        claim=claim,
        main_ord=proposed[1],
        main_text=proposed[2][:1500],
        alternates_table=alt_table,
    )
    res = route(LlmRequest(tier=Tier.MEDIUM, prompt=prompt, source="chase:locate"))
    if res.error:
        log.warning("chase: locate hook failed: %s", res.error)
        return proposed  # fall back to lexical pick
    data = res.data or {}
    if data.get("ok") is True:
        return proposed
    alt_ord = data.get("alternative_ord")
    if alt_ord is None:
        return None  # caller tags dead_chain
    match = next((a for a in alternates if a[1] == int(alt_ord)), None)
    return match or proposed


__all__ = [
    "_disambiguate_candidates",
    "_locate_chunk_in_target",
    "_verify_support_with_caveats",
    "is_corroborating",
    "render_neighbours",
    "render_source_identity",
]
