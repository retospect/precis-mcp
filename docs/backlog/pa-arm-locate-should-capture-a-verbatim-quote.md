---
status: draft
title: the [pa] arm's locate confirms a passage but captures no verbatim quote
prio: normal
---

# the `[pa]` arm's locate confirms a passage but captures no verbatim quote

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

That quote is exactly what the nanopub mint gate requires (verbatim,
contiguous within one chunk). So the `[pa]` arm currently uses the weaker of
two verifiers this codebase already ships, and the `[pc]`→`[fi]` promote has
to re-derive the pinpoint later.

Partially mitigated (this session): a re-ground now persists a `citation`
audit record with the claim and the located passage, tagged
`origin:draft-backfill`. Its `source_quote` is the **whole chunk**, honestly
labelled — narrowing it is this item.

## In scope

- Switch the fetched-`[pa]` locate to a quote-returning verifier, reusing
  `reground.py`'s validation rather than reimplementing it.
- Store the validated quote as the `citation` record's `source_quote`.

## Explicitly NOT in scope

- Changing the two-step `[pa]`→`[pc]`→`[fi]` shape.
- The promote's own extraction cascade.

## Acceptance criteria

- A re-ground's `citation` record holds a verbatim excerpt that passes
  `reground.py`'s substring + uniqueness validation, not the full chunk.
- A quote that fails validation leaves the site `reground-nomatch` rather than
  falling back to a fabricated or whole-chunk quote.

## Target + blast radius

`taproot/backfill.py` (`_default_locate`, `_plan_reground`,
`_record_reground_citations`), reusing `taproot/reground.py`. Raises per-site
LLM cost — price it before running corpus-wide.

## Open questions / decisions log

- Is a per-site cost increase acceptable on a bulk arm, or should the quote be
  captured only at promote time? Undecided.
