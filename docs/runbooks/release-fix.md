# Release fix: turning a red release branch green

**When.** You are the fixer a red release gate dispatched (setting
`release.fix_mode` = `owner` or `escalate`), or a person doing the same by
hand. One session owns every failure of the red run.

This file is the fixer's memory of the build process. A builder lane cannot
read or write graph memory, so what the next fixer must know lives here,
versioned with the branch it fixes. If you hit a trap that is not listed,
add one line under [Traps](#traps) in the same commit as your fix.

## What you have

- A worktree cut from `origin/release/r<N>`, not `main`.
- The brief on your todo: the failing ids with their assertion lines, the
  ranked table of files changed since the last green build, and the rule.
- You commit on the slice branch. The lander then merges the release head
  into your branch, lints it, and lands it onto `release/r<N>`; CI runs on
  the new head. A run that is still red comes back as a new digest for the
  next iteration, so anything you leave red costs a full round.

## Order of work

1. **Group by cause before fixing anything.** Ids in one test file, or
   naming one `src` module, usually share a cause, and one commit in the
   changed-files table often explains several ids.
2. **Find the commit that introduced each cause** (the table,
   `git log -S '<symbol>'`) and read what it meant to do. Then decide:
   - *Code bug* — the commit broke something it did not mean to. Fix the
     code and make sure a test fails without the fix.
   - *Stale test* — the commit changed behaviour on purpose and the test
     still asserts the old behaviour. Update the test to assert the new
     behaviour at least as strictly, and name the commit in your commit
     body. Never skip, xfail or delete a test, and never weaken an
     assertion to something both behaviours satisfy.
3. **Reproduce every id before and after:** `scripts/test <file>` on the
   files you touch, one run at a time. Do not run the whole suite.
4. **One commit for the whole digest.** Subject at most 120 characters; the
   body says, per cause, what was wrong and whether you changed code or a
   test.

## What the lander checks

A land that fails one of these is refused and the work comes back, so run
them yourself first on the files you changed:

- `uv run ruff check` and `uv run ruff format --check`.
- `uv run mypy <changed files>`, test files included.
- The fast guards, among them: every text read or write names
  `encoding="utf-8"` as a keyword; no host names, addresses or secrets in
  committed text; a POSIX-only test module carries a win32 skip.
- Commit subject length (120).

## Traps

- Tests that drive real `git` are skipped inside the `scripts/test`
  container (its git is too old). Run those files with
  `uv run pytest -q -n0 -p no:cacheprovider <file>`.
- A stubbed command proves nothing about the command. Example: `psql` does
  not expand `:'var'` inside a `-c` string, and a fake `psql` accepted it
  for weeks. Pin the shape with a test when you cannot run the real tool.
- `mypy` reads test files too: a lambda that returns `list.append(...)`
  fails with `func-returns-value`; write a small function.
- If your ids already pass on the release head, finish with
  `verified: <ids> green on origin/release/r<N>` and no commit.
- If the lander reports a release sync conflict, merge
  `origin/release/r<N>` into your branch and resolve it. Never rebase a
  release fix and never merge `origin/main` into it.
- If another fix already landed your change, the lander closes your land
  green on its own; there is nothing to redo.

## What to hand back

Your final message is your hand-off; nothing else you write reaches the
graph. Put in it:

- Per cause: what it was, the introducing commit, code or test.
- `unfixed: <test id> — <why>` for every id you leave red. Say so rather
  than loosening a test.
- `gripe: <what ran, what you expected, what happened>` on one line for
  every defect in the pipeline itself: the lander, the lint, the digest,
  CI infrastructure, or this runbook being wrong. The dispatcher files each
  line as a gripe. Keep fixing the rest; do not stop for it.

Longer background on the lanes: [`shipping`](../conventions/shipping.md).
