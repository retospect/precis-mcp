---
status: draft
title: Attach-or-mint at cite time — the claim picker has no verdict, only a list
prio: medium
---

# Attach-or-mint at cite time

## Motivation / why

A writer citing a paper wants one of three things: cite an existing claim
hub this paper grounds, cite the passage directly, or **mint a new hub**
because none of the existing ones says what they mean. Today only the
first two are reachable. `_draft_lint.py::pc_cite_claim_hub_hint` lists
the hubs a cited paper grounds; nothing decides whether any of them is
the *right* one for the sentence being written, and nothing offers "none
of these — mint".

The decision vocabulary already exists and is already correct:
`taproot/canon.py::place` returns a `Placement` with
`action: attach | new_contradicts | new | needs_review`, and it is
**purely deterministic — no DB writes, no transaction**, so a read-only
cite-time caller can get the verdict without minting anything. This is
the same question the dedup blocker asks at mint time, asked at the other
end of the pipe.

## In scope

- Scope `canon.py::block` to a candidate hub set. It ANN-retrieves over
  *all* live hubs today with no way to restrict; the cite-time question
  is "which of the hubs THIS paper grounds fits my sentence", a set of
  ~1–20, not the whole corpus. Prefer an optional
  `hub_ref_ids: Sequence[int] | None = None` filtering the `WHERE`, so
  the existing unscoped call keeps working.
- A judge to produce the `list[tuple[Candidate, Verdict]]` `place()`
  consumes. **This is the cost decision and the reason this item is not
  built yet** — it puts an LLM call on an interactive write path.
  Options, cheapest first: run only on explicit request (a `view=` or a
  verb, not on every keystroke); cache per `(claim_sha, paper_ref_id)`;
  restrict to a low tier with a confidence floor and fall back to
  `needs_review` rather than guessing.
- Rendering `new` as an actionable next step, not a dead end — if the
  verdict is "mint", the writer needs the mint door named.

## Out of scope

- The listing half — shipped: `hubs_grounded_by_paper`, the cite nudge,
  and `get(kind='paper', view='claims')`.
- The write direction (ground *incoming* papers against the claim set) —
  that is `taproot-inbound-grounding.md`, complementary and separate.

## Open questions

- Does a cite-time `new` verdict mint immediately, or queue a candidate?
  Minting on a writer's behalf mid-sentence adds hubs at drafting speed,
  and hub dedup is already its own backlog item
  (`claim-hub-dedup-sweep.md`).
- `needs_review` is the honest answer when the judge is unsure, but a
  writer mid-sentence cannot act on it. Does it degrade to "cite the
  passage" (always safe) rather than surfacing as a third option?
- Should a `refuted`/`disputed` hub be offered as an `attach` target at
  all? It is a real hub and hiding it is its own failure mode (the
  gr263023 argument), but `attach` is a recommendation in a way a listing
  is not.

## Notes

Seams verified 2026-08-28: `place()` write-free;
`block()` unscoped (`taproot/canon.py::block`); `links` indexed on both
`src_ref_id` and `dst_ref_id`, so the reverse read is cheap.
