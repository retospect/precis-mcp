---
status: idea
title: guard-piped-exit-code — decide whether it earns its false-positive rate
---

# `guard-piped-exit-code` — decide whether it earns its false-positive rate

`scripts/hooks/guard-piped-exit-code.py` (shipped 2026-09-08) denies
`scripts/ship|deploy|bump` piped into a filter, because a pipeline reports the
**last** command's status — so `scripts/ship … | tail` exits 0 even when the
gate printed `✖ gate is RED — not shipping`.

**The bug is real and it fired for real**, twice in the session that introduced
the guard: once before the guard existed (a red gate reported as a successful
background task, nearly acted on), and once after, where the redirect form
correctly surfaced `EXIT=1`.

## The problem

In that same session the guard produced **four false positives against one true
catch**:

1. `grep -n <pattern> scripts/ship scripts/deploy | head` — the script named as
   an **argument**, i.e. being read, not run. *(Fixed: the pattern now requires
   command position — start of command, or after `;` `&&` `||` `|` `(` newline,
   allowing leading `VAR=val`.)*
2. A **heredoc** whose body contained the guarded shape as test data.
3. `grep -E 'ansible-playbook|scripts/deploy'` — the name inside a **quoted
   regex**, sitting right after a `|`, which is indistinguishable from command
   position.
4. As (3), on a second host probe.

2–4 share one root cause: the hook matches the raw command string, and a regex
cannot tell code from a string that looks like code without a shell parser.
That limit is documented in the hook, with `ALLOW_PIPED_EXIT=1` as the hatch.

## Why this needs a decision rather than more patching

A guard that fires wrongly more often than rightly trains the operator to reach
for the escape hatch reflexively — and the hatch is coarse, disabling the check
for the whole command including any *real* invocation in it. That is strictly
worse than a narrower guard, because it fails silently in exactly the case the
guard exists for.

Reto to pick:

* **Narrow to `scripts/ship` only.** `deploy` and `bump` are usually run
  interactively where the exit code is visible anyway; `ship` is the one whose
  status gets consumed programmatically and misread. Cuts the collision surface
  most, since `deploy` was involved in 3 of the 4 false positives.
* **Downgrade deny → warn.** Keep full coverage, surface the advice, let the
  command run. Loses enforcement; a hurried operator ignores warnings.
* **Add shell-aware parsing.** Correct, and disproportionate for one line of
  advice — a real parser (or `bashlex`) for a lint rule.
* **Drop it and rely on the CLAUDE.md convention line.** Honest option if the
  measured catch rate stays this low.

Do **not** simply widen `ALLOW_PIPED_EXIT` usage; that hollows out the guard
while keeping its cost.
