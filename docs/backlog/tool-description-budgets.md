---
status: draft
title: Assemble MCP verb descriptions from handler-declared fragments, with a per-kind budget report
pillar: platform
prio: normal
---

PROPOSED, not built; Reto decides.

# Assemble MCP verb descriptions from handler-declared fragments

## Motivation / why

`src/precis/tools/core.py` holds the seven verb functions
(`get search put edit delete tag link`). Each verb's MCP description is its
docstring, passed through `server._verb_description` (`inspect.cleandoc`).
Every new kind or argument is therefore a hand edit to a shared docstring,
and a shared byte cap catches the overrun only after the fact.

Evidence (repo-churn report 2026-10-17, item 5, plus a measurement today):

- core.py: 34 commits in the 14-day window, 20 of them fixes (139 commits
  in its whole history).
- Budget fixes after the fact: b5fb48063 (edit description over the
  cold-start budget), 3786db3ac (an alert line pushed `tag` over 1 KB),
  609a1463b (tag doc budget, r23 test drift). Neighbouring budget reds:
  d68fd9987 (se skill under the cap), f5e18b678 (thread Resume word budget,
  red main), f953a5fae (job-help chunk budget; the fix put that test in the
  pre-qland hygiene set).
- Headroom against `tests/test_token_budget.py::test_per_verb_description_under_budget`
  (cap 1024 bytes, per verb), measured 2026-10-17 on main:
  get 971, tag 966, edit 833, delete 665, put 503, link 492, search 465.
  `get` and `tag` have 53 and 58 bytes left: the next kind that wants one
  line in either breaks the test.
- The failure message says "trim the docstring" and nothing else. It does not
  say which kind's clause to trim, so the fixer reads the whole docstring and
  guesses.
- `tests/test_token_budget.py` is not in `scripts/ship`'s `CODE_HYGIENE`
  (checked: the `--quick` list holds type-ignore, posix, encoding, extra-only,
  store-mixin, UTC and skill-corpus tests). A `--quick` landing that touches
  core.py reaches main red.

Root cause: the description is one hand-written string that many kinds
contribute to, with no record of who contributed which bytes.

## In scope

1. **Fragment declaration.** Each handler (or kind module) declares its own
   short per-verb clauses, e.g. `DESCRIPTION_FRAGMENTS = {"tag": "...",
   "get": "..."}`, next to the code that implements them. Verbs that a kind
   does not touch declare nothing.
2. **Assembler.** A function in `tools/` builds each verb's description from
   a verb-owned preamble (the stable first sentences in core.py today) plus
   the fragments of registered kinds, in a fixed order (sorted by kind name),
   deduplicated. `server._verb_description` calls it instead of reading
   `fn.__doc__`. The result for the current docstrings is byte-identical to
   today's, proven by a golden comparison before any fragment moves.
3. **Per-kind budget test.** One test builds every verb description, fails
   over the existing 1024-byte cap, and on failure (and with `-s`) prints a
   table: verb, total bytes, preamble bytes, then bytes per contributing
   kind, largest first. Failure text names the kind to trim.
4. **Fast hygiene set.** The test is DB-free and joins `CODE_HYGIENE` in
   `scripts/ship` so `--quick` runs it. It also joins impacted-tests'
   always-run list if that list exists for DB-free guards.
5. **Optional reserve per kind.** A soft per-kind share (default: cap divided
   by registered kinds, floor 40 bytes) reported as a warning, not a failure.
   Hard failure stays the per-verb total, so no existing ceiling moves.

## Explicitly NOT in scope

- Raising the 1 KB cap or any other budget (Reto's earlier rulings: no raised
  ceilings without evidence).
- The `precis` command-profile tool (`_COMMAND_TOOL_DESCRIPTION`, its own
  1 KB cap) and the `instructions` banner (2 KB / 4 KB caps). Same method
  could apply later; not here.
- Skill documents (`src/precis/data/skills/`, `test_skill_size.py`). The
  argument-table-from-`ARGS_KEYS` idea in churn item 7 is a separate item.
- Changing the parameter schemas or `_TOOL_KW`, or per-property description
  suffixes (`server.py` mode-conditional coupling).
- Moving prose out of descriptions into skills. That trimming is a content
  call per verb, done by whoever owns the kind.

## Slices

1. **Measure and pin (no behaviour change).** Add the per-kind report as a
   helper that attributes bytes by parsing today's docstrings for
   `kind:`-led clauses; a test that prints the table and keeps the current
   cap. Land in `CODE_HYGIENE`. Value alone: the next overrun names its kind.
   Size: small (about 1 file + 1 test).
2. **Assembler with golden.** Introduce the assembler and fragment registry
   with all text still in core.py as the preamble; golden test proves the
   seven descriptions are byte-identical. Size: small to medium.
3. **Move kind clauses to handlers.** One kind family per commit (the kinds
   that already appear in the docstrings: todo, gripe, quest, alert, draft,
   and the rest the measurement shows). Golden test updated per move only
   when a clause is deliberately reworded. Size: medium; mechanical, a cheap
   agent can do it.
4. **Retire hand edits.** Docstrings in core.py shrink to the preamble;
   `AGENTS.md` / `docs/conventions/` gets the rule "a kind adds its verb
   text in its own handler, never in core.py". Size: small.

Slice 1 is useful without 2-4; stopping after any slice leaves main green.

## Acceptance criteria

- With the assembler on, `get/search/put/edit/delete/tag/link` descriptions
  are byte-identical to main's at the commit that introduced slice 2
  (golden test), and the existing token-budget tests pass unchanged.
- Adding a fragment that pushes `tag` over 1024 bytes fails the new test with
  a message naming the verb and the contributing kinds in size order.
- The new test runs without a DB and is listed in `CODE_HYGIENE`;
  `scripts/ship --quick` on a core.py or handler-fragment change executes it.
- After slice 3, `git grep` shows no kind-specific clause left in core.py
  docstrings beyond the preamble; a new kind needs no core.py edit to appear
  in the descriptions.
- Churn check, 30 days after slice 3: fix commits touching core.py with a
  budget subject (`budget`, `over 1 KB`, `description`) are zero.

## Target + blast radius

`src/precis/tools/core.py`, `src/precis/server.py` (`_verb_description`),
`src/precis/handlers/*` (fragment declarations, slice 3), `scripts/ship`
(`CODE_HYGIENE`), new test beside `tests/test_token_budget.py`. Every MCP
client sees the descriptions on cold start, so a wrong assembly changes the
product surface for all agents; the golden test is the guard. No migration,
no worker, no deploy-role change. Plugin-split (`docs/backlog/threads/plugin-split.md`)
overlaps: handlers loaded from an extra package must register fragments the
same way, and kinds disabled in a build must contribute nothing.

## Open questions / decisions log

1. **Do it at all? (Reto decides.)** Alternative: only slice 1 plus the
   hygiene-set move, leaving docstrings hand-written. It fixes the "red main
   on `--quick`" part and the "which kind" diagnosis for a few commits of
   work, but not the shared-edit churn in core.py.
2. **Fragment location (Reto decides).** Handler class attribute, a
   per-kind data file under `src/precis/data/`, or a sidecar next to each
   handler. A class attribute is simplest; a data file lets a doc-only change
   adjust text. Recommendation: class attribute.
3. **Ordering of fragments (Reto decides).** Sorted by kind name is stable
   but buries high-traffic kinds. Alternative: an explicit priority per kind
   so cold-start readers see paper/memory/gripe first.
4. **Soft per-kind share (Reto decides).** Warning only, as written, or drop
   it and report only. A hard per-kind cap would pre-empt kinds that cost
   more because they have more modes.
5. **Disabled kinds.** Descriptions currently list kinds regardless of build
   (1a1ec7611 shows the build can disable kinds). Should the assembler omit
   fragments for disabled kinds? This changes the surface; Reto decides.
