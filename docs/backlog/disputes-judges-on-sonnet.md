---
status: draft
title: Run the two disputes judges on Sonnet instead of Haiku
pillar: memory-graph
prio: high
---

# Run the two disputes judges on Sonnet instead of Haiku

Waiting on Reto's decision (claims-and-evidence-5, decision 2, "second opinion
on a stronger model"). The tier eval below is that decision's evidence (P4 of
claims-and-evidence-6).

## Motivation / why
Both hub_refine judges route `Tier.MEDIUM` (Haiku 4.5):
`_chase_llm.py::_verify_support_with_caveats` (widen arm) and
`hub_refine.py::judge_edge_strict` (reground arm). With the fixed prompt and the
deterministic gate deployed (round 3, 929107f3), Haiku still passed the gate on a
known-false dispute. Sonnet passed none, at about half Haiku's cost per call.

## Tier eval (2026-10-03, deployed code 929107f3, dry run: no link written)
Method: a one-off script on the deployed venv (session scratch `adjudicate/tier_eval.py`). Edges: the 6 problem edges from
claims-and-evidence-5 plus 20 controls. The controls are 10 pre-rule `disputes`
links and 10 live `corroborates`/`establishes` links, sampled by `md5(link_id)`.
Each edge went through both judges with the full W1–W4 workspace, and each
verdict through the deployed gate. The 4 cases whose original call was in
`llm_blob` were also replayed through their original logged prompt. The medium
tier was re-pointed per process (`PRECIS_LLM_CHAIN_MEDIUM`). Calls are in
`llm_call_log` between 21:01Z and 21:25Z; replays carry source suffix
`-tier-eval`.

| | Haiku 4.5 | Sonnet 5 | Opus 5 |
|---|---|---|---|
| Gate would file a dispute (fixed prompt, 26 edges × 2 judges) | **2** | 0 | 0 |
| …of which known false | 1: geim07 → fi189521, single-layer device vs few-layer film, judged "same setup" | — | — |
| "Contradicts" under the OLD prompt (4 replayable cases) | 4 | 2 | 1 |
| Supporting controls turned to "contradicts" | 0 / 10 | 0 / 10 | 0 / 10 |
| Cost per judge call | ~$0.016 | $0.0078 | $0.036 |
| Mean call time | ~25 s, contended with the live worker | 5.7 s | 11.6 s |
| Errors | 0 | 0 | 2 / 56 |

Read:
- The old prompt was the main fault. Every tier called contradictions under it, Haiku most often.
- With the fix, the stronger tiers are clean and Haiku still errs on setup identity. That is exactly the error the gate cannot catch, because Haiku's own `same_setup` is what the gate trusts.
- Haiku's second filing (control 451257 → fi176775, strict judge) has unknown truth. Sonnet and Opus both PRUNE it.
- Sonnet is the cheapest tier that filed nothing false. Opus adds cost and no accuracy here.
- Sample size: 26 edges. That is enough to rank the tiers, not to estimate a rate.

## In scope
- Route both judges at `Tier.BIG` (Sonnet) rather than `Tier.MEDIUM`; the judges only, no other medium-tier caller.
- Keep the gate unchanged.

## Explicitly NOT in scope
- A second-opinion cascade (Haiku first, Sonnet on gate passes). Sonnet costs less per call than Haiku here, so a cascade saves nothing.
- Changing `llm.chain.medium`, which every medium-tier caller reads.

## Acceptance criteria
- `judge_edge_strict` and `_verify_support_with_caveats` dispatch `Tier.BIG`; tests pin it.
- The widen verifier also decides attachment (`supports` yes/partial), so the switch recalibrates attaching too. Before ship, replay 50 logged widen calls on Sonnet: the attach rate (yes+partial) stays within ±10 points of Haiku's.
- After deploy, `llm_call_log` shows `taproot:reground-judge` and `chase:verify` rows on `claude-sonnet-5`, with per-day judge spend no higher than the 7-day Haiku baseline before it.

## Target + blast radius
`workers/hub_refine.py` (both arms), `workers/_chase_llm.py`, the `taproot:reground-judge` / `chase:verify` budget lanes, and `slice_refine_eval.py`, which reuses the verifier.
