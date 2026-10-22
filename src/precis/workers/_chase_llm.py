"""Default-off LLM hooks for the finding-chase worker.

Split out of ``workers/chase.py`` 2026-06-05. These three functions
are the *only* paths in ``chase`` that issue LLM calls:

* :func:`_verify_support_with_caveats` reads the target chunk + claim
  and records support / caveats / cited-others on the chain entry. Also
  the verify hook ``workers/hub_refine.py`` calls for every discover
  candidate (paper or patent) — always-on there, not ``with_llm``-gated —
  where ``source_kind="patent"`` swaps in patent-aware reading rules
  (patent-evidence-parity). Its batched twin
  :func:`_verify_supports_batch` judges several candidates of ONE claim
  per call (gr499038): the claim context and rubric are paid once,
  per-item verdicts come back keyed by ITEM number, and any item the
  reply misses or malforms falls back to the single call — callers see
  the same per-item verdicts (or ``None``) either way.
* :func:`_disambiguate_candidates` resolves multi-cite chunks.
* :func:`_locate_chunk_in_target` confirms the ANN's chunk pick or
  picks a better one from the shown alternates.

All three activate only when the worker is invoked with
``with_llm=True`` (or env ``PRECIS_CHASE_LLM=1``); the deterministic
default chase path never touches them. Placement and cost follow the live
``chase:verify`` operation chain; the call site pins neither a model nor a
host, and its plain completion can use the local gpt-oss lane without a
Claude agent/tool loop.

Failure mode: any of these may return ``None`` (or the proposed
input, in the ``_locate_*`` case) on LLM error; callers must
tolerate that and fall back to deterministic behaviour.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
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

#: The claim-side block every candidate chunk is judged against — shared
#: by the single and batch verify prompts (in the batch it is paid once
#: for all items).
_PROMPT_VERIFY_CLAIM_BLOCK = """\
CLAIM:
{claim}

SETUP (structured):
{scope_json}

CLAIM'S OWN SOURCE PASSAGE (what the claim was measured on):
{claim_source_text}"""

#: One candidate's source-side block — the same field layout in the
#: single prompt and under each ``ITEM n`` marker of the batch prompt.
_PROMPT_VERIFY_ITEM_BLOCK = """\
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
{patent_note}"""

#: The verdict rubric shared by the single and batch prompts — the same
#: definitions, word for word; the prompts differ only in the response
#: contract appended after it.
_VERIFY_RUBRIC = """\
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
"""

#: The per-item verdict object — the response contract appended after the
#: rubric in both prompts, verbatim either way.
_VERIFY_VERDICT_SCHEMA = """\
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
}}"""

_PROMPT_VERIFY = (
    """\
You are verifying whether a source {source_kind} chunk supports a specific
empirical claim made under a specific experimental setup. Judge support
for the claim EXACTLY AS STATED — not a looser or more general version.

{claim_block}

{item_block}
"""
    + _VERIFY_RUBRIC
    + (
        "\nRespond with EXACTLY ONE JSON object, nothing else:\n"
        + _VERIFY_VERDICT_SCHEMA
        + "\n"
    )
)

#: The batch verify prompt (gr499038): the claim context is paid once,
#: each candidate chunk rides as a labelled ITEM, and the reply carries
#: one verdict object per item. The rubric and per-item verdict fields are
#: byte-for-byte the single prompt's — batching changes how many items a
#: call judges, not what a verdict means.
_PROMPT_VERIFY_BATCH = (
    """\
You are verifying whether source chunks support a specific
empirical claim made under a specific experimental setup. Judge support
for the claim EXACTLY AS STATED — not a looser or more general version.

{claim_block}

{n_items} candidate sources follow, each labelled ITEM <n>. Judge EACH
item independently against the claim — every item gets its own verdict
under the same contract.

{items_block}
"""
    + _VERIFY_RUBRIC
    + """\

Respond with EXACTLY ONE JSON object, nothing else — one verdict object
per ITEM shown, exactly {n_items} entries (any order, each tagged with
its "item" number):
{{
  "verdicts": [
    {{
      "item": <int — the ITEM number>,
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
  ]
}}
"""
)


def _verify_claim_block(
    *, claim: str, scope: dict[str, Any], claim_source_text: str | None
) -> str:
    """The shared claim-side block of the verify prompts (claim, structured
    scope, the claim's own source passage)."""
    return _PROMPT_VERIFY_CLAIM_BLOCK.format(
        claim=claim,
        scope_json=json.dumps(scope, sort_keys=True),
        claim_source_text=(claim_source_text or "")[:3000] or "(not available)",
    )


def _verify_item_block(
    *,
    source_kind: str,
    cite_key: str,
    chunk_ord: int,
    chunk_text: str,
    section_path: str | None,
    neighbours: list[str] | None,
    source_identity: SourceIdentity | None,
) -> str:
    """The per-candidate source block — the same field layout and caps
    (chunk 4000, neighbours :data:`NEIGHBOUR_CHARS`, abstract
    :data:`ABSTRACT_CHARS`) in the single prompt and under each ``ITEM n``
    of the batch prompt."""
    return _PROMPT_VERIFY_ITEM_BLOCK.format(
        source_kind=source_kind,
        target_cite_key=cite_key,
        target_chunk_ord=chunk_ord,
        section_path=section_path or "(not available)",
        identity_note=SOURCE_IDENTITY_NOTE,
        source_identity=render_source_identity(source_identity),
        target_chunk_text=chunk_text[:4000],  # cap context cost
        neighbours=render_neighbours(neighbours, what="chunk"),
        patent_note=_PATENT_VERIFY_NOTE if source_kind == "patent" else "",
    )


def _verify_batch_prompt(claim_block: str, item_blocks: list[str]) -> str:
    """Compose the batch verify prompt from pre-rendered blocks — the
    claim block once, each item under an ``ITEM <n>`` marker (1-indexed)."""
    n = len(item_blocks)
    joined = "\n\n".join(
        f"ITEM {i}\n\n{block}" for i, block in enumerate(item_blocks, 1)
    )
    return _PROMPT_VERIFY_BATCH.format(
        claim_block=claim_block, n_items=n, items_block=joined
    )


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
    timeout_s: float | None = None,
) -> dict[str, Any] | None:
    """Run the verifier LLM hook. Returns the parsed JSON dict or None.

    ``timeout_s`` pins the call's wall clock (``LlmRequest.timeout_s``);
    ``None`` leaves the transport default. A pass with its own deadline
    (``conflict_search``) sets it so one slow call can't outlast the pass.

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
        source_kind=source_kind,
        claim_block=_verify_claim_block(
            claim=claim, scope=scope, claim_source_text=claim_source_text
        ),
        item_block=_verify_item_block(
            source_kind=source_kind,
            cite_key=target_cite_key,
            chunk_ord=target_chunk_ord,
            chunk_text=target_chunk_text,
            section_path=section_path,
            neighbours=neighbours,
            source_identity=source_identity,
        ),
    )
    res = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt=prompt,
            source="chase:verify",
            timeout_s=timeout_s,
        )
    )
    if res.error:
        log.warning("chase: verify hook failed: %s", res.error)
        return None
    request_hash = getattr(res, "request_hash", None)
    if with_request_hash and res.data is not None and isinstance(request_hash, str):
        return {**res.data, "_llm_request_hash": request_hash}
    return res.data


@dataclass(frozen=True)
class VerifyItem:
    """One candidate citation to judge against a shared claim — the
    source-side kwargs of :func:`_verify_support_with_caveats` (field names
    kept identical so an item dict maps straight onto a single call)."""

    target_cite_key: str
    target_chunk_ord: int
    target_chunk_text: str
    source_kind: str = "paper"
    section_path: str | None = None
    neighbours: list[str] | None = None
    source_identity: SourceIdentity | None = None


def _verify_item_kwargs(item: VerifyItem) -> dict[str, Any]:
    """The item as per-item kwargs for the single verify call (shallow —
    ``source_identity`` stays a :class:`SourceIdentity`)."""
    return {f.name: getattr(item, f.name) for f in fields(item)}


#: Upper bound on items per batch call. The win saturates quickly (the
#: shared claim block is amortized) while the per-item contract grows
#: more fragile with N — a malformed reply costs per-item fallbacks.
VERIFY_BATCH_MAX_ITEMS = 6

#: Completion budget per verdict the batch reply must hold — a single
#: verify's ~1.5k output tokens plus headroom, so a rung-level
#: ``max_tokens`` (e.g. a local rung's small cap) can never silently
#: truncate the per-item verdicts. The request pins it: a caller-pinned
#: cap is a correctness constraint the rung must not shrink.
_VERIFY_BATCH_TOKENS_PER_ITEM = 2048
_VERIFY_BATCH_TOKENS_BASE = 1024


def _verify_batch_max_tokens(n_items: int) -> int:
    return _VERIFY_BATCH_TOKENS_BASE + _VERIFY_BATCH_TOKENS_PER_ITEM * n_items


def _parse_batch_verdicts(data: Any, n_items: int) -> list[dict[str, Any] | None]:
    """Per-item verdict dicts from a batch reply, 0-indexed like the items.

    A missing, malformed or out-of-enum item comes back ``None`` — the
    caller falls back to a single call for it; a verdict is never guessed.
    Accepts the documented ``{"verdicts": [{"item": n, ...}]}`` shape and,
    leniently, a ``{"verdicts": {"<n>": {...}}}`` map.
    """
    out: list[dict[str, Any] | None] = [None] * n_items
    if not isinstance(data, dict):
        return out
    raw = data.get("verdicts")
    entries: list[tuple[Any, Any]]
    if isinstance(raw, dict):
        entries = list(raw.items())
    elif isinstance(raw, list):
        entries = [(v.get("item"), v) for v in raw if isinstance(v, dict)]
    else:
        return out
    for key, verdict in entries:
        try:
            idx = int(str(key).strip()) - 1
        except (TypeError, ValueError):
            continue
        if (
            0 <= idx < n_items
            and out[idx] is None
            and isinstance(verdict, dict)
            and verdict.get("supports") in ("yes", "partial", "no")
        ):
            out[idx] = {k: v for k, v in verdict.items() if k != "item"}
    return out


def _verify_supports_batch(
    *,
    claim: str,
    scope: dict[str, Any],
    items: list[VerifyItem],
    claim_source_text: str | None = None,
    with_request_hash: bool = False,
    timeout_s: float | None = None,
) -> list[dict[str, Any] | None]:
    """Verify several candidate citations of one claim in a single call
    (gr499038) — the claim context and the rubric are paid once instead of
    per item. Returns one verdict (or ``None``) per item, in item order;
    the per-item verdict dicts are the single call's shape.

    Fallbacks keep the single call's contract exactly:

    * a batch of 0 or 1 IS the single call;
    * a router error behaves exactly like today — every item gets its own
      :func:`_verify_support_with_caveats` call (each may independently
      return a verdict or ``None``);
    * an item missing from the reply, or whose verdict is malformed
      (``supports`` outside the enum), falls back to a single call — a
      verdict is never guessed.

    ``with_request_hash`` attaches the dispatch's ``_llm_request_hash`` as
    in the single call — the batch call's hash on batch verdicts, the
    fallback call's own hash on fallback verdicts.
    """

    def _single(item: VerifyItem) -> dict[str, Any] | None:
        return _verify_support_with_caveats(
            claim=claim,
            scope=scope,
            claim_source_text=claim_source_text,
            with_request_hash=with_request_hash,
            timeout_s=timeout_s,
            **_verify_item_kwargs(item),
        )

    if len(items) <= 1:
        return [_single(it) for it in items]

    prompt = _verify_batch_prompt(
        _verify_claim_block(
            claim=claim, scope=scope, claim_source_text=claim_source_text
        ),
        [
            _verify_item_block(
                source_kind=it.source_kind,
                cite_key=it.target_cite_key,
                chunk_ord=it.target_chunk_ord,
                chunk_text=it.target_chunk_text,
                section_path=it.section_path,
                neighbours=it.neighbours,
                source_identity=it.source_identity,
            )
            for it in items
        ],
    )
    res = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt=prompt,
            source="chase:verify",
            max_tokens=_verify_batch_max_tokens(len(items)),
            timeout_s=timeout_s,
        )
    )
    if res.error:
        log.warning("chase: verify batch hook failed: %s", res.error)
        verdicts: list[dict[str, Any] | None] = [None] * len(items)
    else:
        verdicts = _parse_batch_verdicts(res.data, len(items))
    request_hash = getattr(res, "request_hash", None)
    out: list[dict[str, Any] | None] = []
    for item, verdict in zip(items, verdicts):
        if verdict is None:
            out.append(_single(item))
        elif with_request_hash and isinstance(request_hash, str):
            out.append({**verdict, "_llm_request_hash": request_hash})
        else:
            out.append(verdict)
    return out


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
    "VERIFY_BATCH_MAX_ITEMS",
    "VerifyItem",
    "_disambiguate_candidates",
    "_locate_chunk_in_target",
    "_parse_batch_verdicts",
    "_verify_batch_max_tokens",
    "_verify_batch_prompt",
    "_verify_claim_block",
    "_verify_item_block",
    "_verify_item_kwargs",
    "_verify_support_with_caveats",
    "_verify_supports_batch",
    "is_corroborating",
    "render_neighbours",
    "render_source_identity",
]
