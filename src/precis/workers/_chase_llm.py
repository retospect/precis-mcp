"""Default-off LLM hooks for the finding-chase worker.

Split out of ``workers/chase.py`` 2026-06-05. These three functions
are the *only* paths in ``chase`` that issue paid LLM calls:

* :func:`_verify_support_with_caveats` reads the target chunk + claim
  and records support / caveats / cited-others on the chain entry.
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
from typing import TYPE_CHECKING, Any

from precis.utils.llm.router import LlmRequest, Tier, dispatch

if TYPE_CHECKING:
    from precis.workers.chase import _NextHopTarget

log = logging.getLogger(__name__)


_PROMPT_VERIFY = """\
You are verifying whether a source paper chunk supports a specific
empirical claim made under a specific experimental setup. Be
CONSERVATIVE: hedged or conditionally-supportive language → record
a caveat, do not claim full support.

CLAIM:
{claim}

SETUP (structured):
{scope_json}

SOURCE: paper {target_cite_key}, chunk ord {target_chunk_ord}

CHUNK TEXT:
{target_chunk_text}

Definitions:
  supports = "yes"      : chunk states the claim under the setup
                          (verbatim or close paraphrase)
  supports = "partial"  : chunk supports the claim with conditions
                          listed in caveats
  supports = "no"       : chunk does not support the claim
  caveats               : conditions, regimes, applicability limits
                          that qualify the support
  cited_others          : inline citation tokens that the chase
                          should follow (e.g. "[12]", "(Lin 1998)").
                          Empty if the chunk is the original source.
  terminal              : true iff the chunk DESCRIBES the
                          measurement itself; false if it merely
                          restates a value from elsewhere.

Respond with EXACTLY ONE JSON object, nothing else:
{{
  "supports": "yes" | "partial" | "no",
  "support_reason": "<one sentence>",
  "caveats": ["<caveat 1>", ...],
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
) -> dict[str, Any] | None:
    """Run the verifier LLM hook. Returns the parsed JSON dict or None."""
    prompt = _PROMPT_VERIFY.format(
        claim=claim,
        scope_json=json.dumps(scope, sort_keys=True),
        target_cite_key=target_cite_key,
        target_chunk_ord=target_chunk_ord,
        target_chunk_text=target_chunk_text[:4000],  # cap context cost
    )
    res = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt=prompt))
    if res.error:
        log.warning("chase: verify hook failed: %s", res.error)
        return None
    return res.data


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
    res = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt=prompt))
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
    res = dispatch(LlmRequest(tier=Tier.CLOUD_SMALL, prompt=prompt))
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
]
