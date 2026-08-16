---
status: draft
title: hearsay mint gate misses intro-section secondhand grounding
prio: normal
---

# hearsay mint gate misses intro-section secondhand grounding

## Motivation / why
The Layer-A primary-source gate rejects grounding chunks whose
`section_path` matches `HEARSAY_SECTION`
(`src/precis/nanopub/evidence.py`: reference / bibliograph / related.work
/ prior.art / background / state.of.the.art). But **intro sections are
where papers actually cite prior work**, and the regex doesn't cover
them. Live examples (user-spotted on `/nanopub/tree`, 2026-08-16):
fi19981 ("Moore's Law…") is grounded in Xia 2019's intro citing Moore
1965; fi19987 ("Frank 2001: physical scaling limits…") in Schranghamer
2020's intro citing Frank 2001. Both carry `section:intro` tags, both
say "Paper not in corpus — needs acquisition" in the claim body, both
have `derived-from` edges to the *citing* paper — pure hearsay that the
gate as written would let through to mint.

Related: these attribution-prefixed titles ("Frank 2001: …") are also
crispness-gate material — see `retire-fi-go-nanopub.md`.

## In scope
Detect secondhand grounding in intro-like sections without banning all
intro grounding (a paper's own contribution summary in the intro is
legitimate primary grounding). Candidate signals, pick at spec time:
- claim body / hub meta carrying an explicit "not in corpus" or
  needs-acquisition marker → hard reject;
- grounding quote whose sentence carries an external citation marker
  (`[N]`, `(Author, Year)`) attributing the asserted fact → reject or
  demand signoff;
- possibly extend `HEARSAY_SECTION` with `introduction` behind a
  softer path (signoff-able rather than hard reject).

## Explicitly NOT in scope
- Blanket-adding `intro` to `HEARSAY_SECTION` (would reject legitimate
  own-work grounding).
- Fixing the two live findings' evidence edges (that's the
  retire-fi-go-nanopub migration sweep's job).
- Title-crispness enforcement (separate gate, same umbrella).

## Acceptance criteria
- A mint attempt for a claim grounded only in another paper's intro
  citation of the primary work is rejected (or forced through explicit
  signoff) with a violation message pointing at the primary-source hunt.
- A claim grounded in a paper's intro describing its *own* results still
  mints.
- fi19981/fi19987-shaped fixtures covered in `tests/test_nanopub_gates_mint.py`.

## Target + blast radius
`src/precis/nanopub/gates.py`, `src/precis/nanopub/evidence.py`
(`HEARSAY_SECTION` / `ChunkInfo.is_hearsay_section`); mint paths (web
approve door + CLI). No schema change.

## Open questions / decisions log
- Which signal(s) above are cheap enough to compute at gate time from
  the grounding chunk alone?
