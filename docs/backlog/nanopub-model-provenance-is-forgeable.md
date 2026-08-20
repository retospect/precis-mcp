---
status: draft
title: "the signed artifact's llmModel field is hand-typed, and the web door emits none — derive it or drop it"
---

# A provenance field nothing derives

The nanopub artifact already has a place for model attribution.
`src/precis/nanopub/assemble.py:223-224` emits one `precis:llmModel` triple per
entry of `MintInput.software["llm_models"]` into the **pubinfo graph**, which is
inside the signed bytes and covered by the OTS anchor. No format change and no
migration are needed to populate it.

Nothing populates it from reality. There are exactly two callers:

| caller | value |
|---|---|
| `src/precis/cli/nanopub.py:332` | `llm_models=args.llm_model` — a hand-typed CLI flag |
| `src/precis_web/routes/nanopub.py:261` | `llm_models=[]` — **the web door emits nothing** |

So the field is an honor-system assertion, and the door most likely to be used
in practice asserts nothing at all.

## Why this is worse than leaving it empty

A hand-typed model id inside a **signed, timestamped** artifact is a false
witness waiting to happen. `--llm-model claude-opus-5` makes the artifact attest,
under signature, that opus produced a claim that `z-ai/glm-4.7-flash` actually
wrote, and no part of the system can contradict it. A missing field is an honest
gap; a forgeable one invites a reader to trust something no one verified.

Either derive it or remove it. Do not leave it typed.

## What is actually true today, measured 2026-08-20

The taproot claim path, as routed in prod (`app_settings` `llm.chain.*`):

| stage | tier | model in prod |
|---|---|---|
| `taproot:extract` — **the claim sentence** | SMALL | `z-ai/glm-4.7-flash` |
| `taproot:dedup` | MEDIUM | `claude-haiku-4-5-20251001` (earlier: `z-ai/glm-4.7`, 1,409 calls) |
| `merge_confirm` | BIG | `claude-sonnet-5`, and only when confidence is low |
| `_verify_support_with_caveats` — **does this passage support this claim** | MEDIUM | `claude-haiku-4-5-20251001` |

**FRONTIER (`claude-opus-5`) is never called anywhere in the taproot path.** The
`nursery`/`structural`/`deep_review` tiers — including the opus rung — operate on
the **todo tree**, not on findings, so "an opus reviewed this claim" is not a
thing the system can do today regardless of what an operator runs.

Caveat on the numbers: only 150 `taproot:extract` calls exist all-time against
~1,244 hubs, so the logged pipeline cannot account for every hub. Many were
likely minted through the agent/MCP direct-mint path, which tags nothing as
`taproot`. Do not state that every hub was glm-minted — it is not established.

## Where the model must be recorded

Not on the artifact — on the **claim and the edge**, at write time, with the
artifact rendering it. Derive-never-duplicate: the DB row is the source, the
pubinfo triple is the projection. You cannot run "show me every claim whose
evidence was only ever haiku-verified" over signed RDF blobs, and that query is
the entire point.

Nothing is recorded today: `refs.set_by` is NULL for all 126 hubs in the
dr173020 cohort, `refs.meta` holds only `{"scope": …, "source": "taproot"}`, and
evidence edges carry `support` / `support_reason` / `caveats` / `source_handle`
with no model. `llm_call_log` has `tier`/`model`/`source`/`ts` and reaches back
to 2026-07-14 unpruned, but joining it to a claim is inference by timestamp, not
provenance.

Record per step, because the stages ran on different tiers and the highest-stakes
judgment is per-edge:

- extract → model, tier, prompt/rubric version (no rubric version constant exists
  yet — that gap is already tracked);
- dedup/placement verdict → model, tier, confidence;
- **each evidence edge's support verdict** → model + tier, alongside the `support`
  and `caveats` already stored there. Smallest change, highest value;
- human acts → who and when, typed *differently* from machine acts. PROV-O
  distinguishes `prov:SoftwareAgent` from `prov:Person`; the artifact currently
  cannot express that a human read a passage.

Use the exact versioned id (`claude-haiku-4-5-20251001`), never a family name —
"Claude" is not machine-comparable and ages badly.

## Also worth emitting: the verdict itself

`support` and `caveats` are stored on the edge and **deliberately dropped at the
publish boundary**. The artifact therefore tells a reader "derived from DOI X,
role `corroborates`, here is the quote" but never "and our check judged support
qualified, with these caveats." The caveats are precisely what a reader needs to
weigh the claim. The "universal anchors only" rule that (correctly) strips chunk
and ref ids does not apply to a model id or a caveat — both are universal.

## Do not backfill

The existing hubs have no recorded model. A forensic `llm_call_log` join by
timestamp is inference, and baking an inferred model into a signed artifact is
the same species of error as "correcting" a claim to match a corrupt passage.
Mark them `unattributed` and move on.

## The payoff is a gate, not a disclosure

Once the model is on the edge, this becomes writable: *refuse to sign a claim
whose evidence was never verified above MEDIUM.* Today that rule cannot be
expressed because the data does not exist. Provenance is what turns a preference
into an enforceable gate — that, not transparency, is the reason to build it.
