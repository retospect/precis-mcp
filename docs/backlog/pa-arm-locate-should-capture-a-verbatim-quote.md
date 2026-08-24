---
status: draft
title: the [pa] arm's locate confirms a passage but captures no verbatim quotes
prio: normal
---

# the `[pa]` arm's locate confirms a passage but captures no verbatim quotes

## Motivation / why

`taproot/backfill.py::_default_locate` grounds a fetched `[pa]` with
`workers/_chase_llm::_locate_chunk_in_target` — a Tier.MEDIUM confirm that
returns *which chunk* supports the span. A chunk is paragraph-sized and
routinely carries several assertions, so the result pins the claim to a
paragraph, not to words.

Sitting in the same package, `taproot/reground.py` already has the stronger
verifier: `verify_atoms_batch` returns a `GroundedRecord` carrying a
**verbatim quote**, and `_validate_quote` re-checks it in code — markup
stripped, whitespace collapsed, notation folded — requiring the quote to be a
substring of the claimed chunk *and* unique across every non-hearsay body
chunk of that paper.

So the `[pa]` arm currently uses the weaker of two verifiers this codebase
already ships, and the `[pc]`→`[fi]` promote has to re-derive the pinpoint
later.

Partially mitigated (2026-08-23): a re-ground now persists a `citation` audit
record with the claim and the located passage, tagged `origin:draft-backfill`.
Its `source_quote` is the **whole chunk**, honestly labelled — narrowing it is
this item.

## The unit is a SET of (chunk, quote) pairs, not one quote

**Corrected 2026-08-23** — an earlier revision of this item specified
capturing *a* verbatim quote, singular. That mis-states the model and would
build the wrong thing.

A claim is a **distillation**, not an extract. The support for one claim is
routinely scattered: the measurement is in the results paragraph, the method
that makes it attributable is in a different section, and a value can straddle
a chunk boundary. "DFT shows that foos are 200 mV in bloos" may be assembled
from three places that share no sentence.

The nanopub model already allows for this and the constraint is narrower than
"one quote": each **individual** quote must be verbatim and contiguous within
ONE chunk (`precis-nanopub-help.md:162`), while the **hub** may be supported by
several chunks. The title asserts what the quotes *together* support.

So the artifact this arm should produce is a **set** of validated
(chunk_id, quote) pairs — each pair independently passing `_validate_quote`'s
substring + uniqueness check — not a single winning quote. A design that
returns one quote per site cannot express the normal case and would silently
drop the method attribution that makes the claim defensible.

## In scope

- Switch the fetched-`[pa]` locate to a quote-returning verifier, reusing
  `reground.py`'s validation rather than reimplementing it.
- Return and persist **one or more** (chunk, quote) pairs per site; validate
  each pair independently against its own claimed chunk.
- Store them as the `citation` record's support set, replacing the whole-chunk
  `source_quote`.

## Explicitly NOT in scope

- Changing the `draft → nanopub (precise claim) → underlying support` shape.
  Whether `[pa]`→`[pc]`→`[fi]` stays two steps or collapses to one is a
  separate call; the graph shape is what matters, not the step count.
- The promote's own extraction cascade.
- Any relaxation of the per-quote contiguity rule — that constraint is what
  makes a quote checkable in code, and it survives intact.

## Acceptance criteria

- A re-ground's `citation` record holds one or more verbatim excerpts, each
  passing `reground.py`'s substring + uniqueness validation against the chunk
  it claims, and none holds a full chunk.
- A site whose support genuinely spans several chunks records every validated
  pair, not the best one.
- A pair that fails validation is dropped; a site left with zero valid pairs
  stays `reground-nomatch` rather than falling back to a fabricated or
  whole-chunk quote.

## Target + blast radius

`taproot/backfill.py` (`_default_locate`, `_plan_reground`,
`_record_reground_citations`), reusing `taproot/reground.py`. Raises per-site
LLM cost — price it before running corpus-wide.

## Open questions / decisions log

- Is a per-site cost increase acceptable on a bulk arm, or should the quote set
  be captured only at promote time? Undecided.
- Cap on pairs per site? An unbounded set invites an LLM to pad with weak
  support. A small cap (3-4) with the rest dropped is probably right, but
  unmeasured.
