---
status: draft
title: hint-lint — every advertised Next-trailer call must execute in tests
prio: normal
---

# hint-lint — every advertised Next-trailer call must execute in tests

## Motivation / why

2026-09-02 gripe: the paper chunk-read trailer advertised
`next 3 chunks → get(id='pc323387+1..3')`. The call was arithmetically
correct (relative nav off the last chunk's handle), but anchored on an
opaque chunk id it read as wrong even to a careful human — and a hint
the reader cannot verify from the response it rides on erodes trust in
*all* hints, which shows up as retry loops and re-derivation in agent
transcripts. The fix for the paper path (absolute
`pa<id>~26..28`, plus `test_chunk_trailer_hints_round_trip`) is shipped;
this item is the generalization.

Nothing systematically guards the *other* hint emitters: every handler
composes `render_next_section(nav)` strings by hand, and the existing
tests assert substring presence only. A hint that errors, 404s, or lands
on the wrong span ships green today. The same class of bug has bitten
before (critic m6: `~77..77` trained a wrong call shape; the 2026-05-04
`~N+1` linear-paging trap).

## In scope

- A shared test helper (e.g. `tests/hintcheck.py`): given a `Response`
  body, extract every `get(...)` / `search(...)` snippet from the
  `Next:` trailer, run each through the real
  `precis.tools.command_parser.parse_command`, and dispatch it against
  the same store/hub — assert it parses, executes without error, and
  returns a non-empty body. Placeholder args (`q='your query'`,
  `scope={...}` templates) are parse-checked only, not executed.
- Apply the helper to the highest-traffic read paths first: paper
  (chunk read, toc, search results), skill, finding/hub views, md,
  plaintext — one parametrized test per handler test module.
- (shipped with the paper fix) The legibility invariant lives in the
  `precis.utils.next_block` module docstring: a hint must be verifiable
  from the response it appears in — anchor on ids the caller just used,
  prefer absolute selectors over relative arithmetic on opaque ids.
  New/changed hint emitters follow it.

## Explicitly NOT in scope

- Removing `resolve_relative` / relative-nav *input* support — it stays
  a supported call shape, just not the advertised one.
- Mining prod transcripts for bad-hint follow-ups (that signal lives in
  `/whatneedsdoing`'s LLM-confusion step).
- Semantic assertions per hint (that the TOC hint returns a *good* TOC)
  — this is an executes-and-returns-something gate, not a quality gate.

## Acceptance criteria

- Hint extraction + dispatch helper exists and is used by ≥ 5 handler
  test modules.
- A deliberately broken hint (typo'd view name in a nav tuple) fails a
  test.

## Target + blast radius

Tests only, plus one convention doc line. Handlers touched only if the
harness finds live broken hints (each becomes its own fix commit).

## Open questions / decisions log

- Should the helper run in the gate for every handler module, or as one
  slower parametrized sweep? (Lean: per-module, it's cheap.)
