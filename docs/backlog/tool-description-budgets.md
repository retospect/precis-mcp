---
status: draft
title: Verb descriptions carry cross-kind mechanics only; kind clauses move to skills
pillar: platform
prio: normal
---

PROPOSED, Reto approved direction 2026-10-17; build after M1-M3.

# Verb descriptions carry cross-kind mechanics only

## Motivation / why

`src/precis/tools/core.py` holds the seven verb functions
(`get search put edit delete tag link`). Each verb's MCP description is its
docstring, passed through `server._verb_description` (`inspect.cleandoc`).
Kind-specific clauses accumulate in those shared docstrings, so every new
kind or argument is a hand edit to core.py, and the shared byte cap catches
the overrun only after the fact.

Evidence (repo-churn report 2026-10-17, item 5, plus a measurement today):

- core.py: 34 commits in the 14-day window, 20 of them fixes.
- Budget fixes after the fact: b5fb48063 (edit description over the
  cold-start budget), 3786db3ac (an alert line pushed `tag` over 1 KB),
  609a1463b (tag doc budget). Neighbouring budget reds: d68fd9987 (se skill
  under the cap), f5e18b678 (thread Resume word budget, red main), f953a5fae
  (job-help chunk budget; the fix put that test in the pre-qland hygiene set).
- Headroom against `tests/test_token_budget.py::test_per_verb_description_under_budget`
  (cap 1024 bytes per verb), measured 2026-10-17 on main: get 971, tag 966,
  edit 833, delete 665, put 503, link 492, search 465. `get` and `tag` have
  53 and 58 bytes left.
- `tests/test_token_budget.py` is not in `scripts/ship`'s `CODE_HYGIENE`, so a
  `--quick` landing that touches core.py can reach main red.

Direction (Reto, 2026-10-17): the description is not the place to teach each
kind. Skills already are (`get(kind='skill')`, discovered via
`search(kind='skill', ...)`). Move kind-specific text there and let the
errors point agents to it. Authoring rules live in
`docs/conventions/mcp-authoring.md` (being written by M3; this item cites it
and does not restate it).

## In scope

1. **Verb descriptions keep cross-kind mechanics only:** `kind=`, `id=`,
   `args={}`, the modes, and how to find a skill. Kind-specific clauses move
   into each kind's skill.
2. **Each kind's `BadInput` / `Unsupported` message names its skill**, so an
   agent that misuses a kind learns where the rule is without it being in
   the description.
3. **Move rule, per clause:**
   - If misuse raises an error, the clause moves to the skill.
   - If misuse would succeed silently, or is destructive or irreversible, it
     stays as a short line in the verb description until the code refuses it.
     Builders M1 and M2 under gr494831 are adding those refusals; each
     refusal frees its clause to move.
4. **Keep the 1024-byte per-verb cap test** and add
   `tests/test_token_budget.py` (or its per-verb test) to `CODE_HYGIENE` in
   `scripts/ship` so `--quick` runs it. The test must stay DB-free.
5. **Measurement.**
   - Per-kind error rate from `tool_calls`, two weeks before and two weeks
     after each move.
   - Before a move lands, a Haiku check: 5-10 typical tasks over the moved
     kinds, run with and without the clauses, comparing first-try success.
     Put back any kind whose rate rises.

## Explicitly NOT in scope

- Raising the 1 KB cap or any other budget.
- A handler-declared fragment registry or description assembler (considered
  in the first draft, dropped by Reto 2026-10-17).
- The `precis` command-profile tool (`_COMMAND_TOOL_DESCRIPTION`) and the
  `instructions` banner caps.
- Parameter schemas, `_TOOL_KW`, and per-property description suffixes in
  `server.py` (mode-conditional coupling).
- Generating skill argument tables from `ARGS_KEYS` (churn item 7).
- Writing the refusals in M1/M2 or the authoring convention in M3; this item
  waits on them.

## Slices

1. **Hygiene.** Put the per-verb cap test into `CODE_HYGIENE`. Independent of
   M1-M3; stops `--quick` reddening main. Size: small.
2. **Inventory.** List every kind-specific clause in the seven docstrings and
   classify each as errors-on-misuse, silent, or destructive (rule 3). No
   behaviour change; output is the move list, attached to this item. Size:
   small.
3. **Error messages name skills.** Each kind's `BadInput`/`Unsupported` text
   gets its skill name, per the convention. Size: medium, mechanical.
4. **Moves, one kind per commit.** For each kind whose clauses are all
   errors-on-misuse (or whose refusal M1/M2 has landed): run the Haiku check,
   move the clauses into the skill, trim the docstring, record the before
   numbers. Size: medium, mechanical.
5. **After-measure.** Two weeks later, compare per-kind error rates from
   `tool_calls`; revert any kind that regressed.

## Acceptance criteria

- Verb descriptions in core.py contain only cross-kind mechanics plus the
  short lines that rule 3 keeps; `git grep` on core.py docstrings finds no
  kind-specific clause that rule 3 allows to move.
- Every kind's `BadInput`/`Unsupported` message names its skill.
- The 1024-byte cap test passes and is in `CODE_HYGIENE`;
  `scripts/ship --quick` on a core.py change runs it.
- For each moved kind, the Haiku check shows first-try success no lower with
  the clauses removed, and the two-week after-rate in `tool_calls` is no
  higher than the two-week before-rate. A kind that fails either is restored.
- Churn check, 30 days after slice 4: zero fix commits touching core.py with
  a budget subject (`budget`, `over 1 KB`, `description`).

## Target + blast radius

`src/precis/tools/core.py` docstrings, kind skills under
`src/precis/data/skills/`, handler error messages in `src/precis/handlers/*`,
`scripts/ship` (`CODE_HYGIENE`). Every MCP client sees the descriptions on
cold start, so a removed clause changes the product surface for all agents;
the Haiku check and the before/after error rates are the guards. Skill edits
are subject to `test_skill_size.py` and the skill-corpus gate. No migration,
no worker, no deploy-role change.

## Open questions / decisions log

- Decided (Reto 2026-10-17): drop the fragment registry; move kind clauses to
  skills; keep the 1024-byte cap and its test; measure with `tool_calls` and a
  Haiku check.
- **Deferred:** descriptions currently list kinds regardless of build (1a1ec7611
  shows a build can disable kinds). Whether the descriptions should omit
  disabled kinds changes the surface; decide after the moves.
