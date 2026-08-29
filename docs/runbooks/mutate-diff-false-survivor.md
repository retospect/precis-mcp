# mutate-diff false survivor — verify before chasing

**Symptom.** `scripts/mutate-diff` reports `SURVIVED` for a mutant that the
test suite actually kills. Cause: the tool selects, per mutant, only the
tests its recorded coverage **contexts** attribute to that line, and that
attribution is incomplete — a test that provably executes the line can be
missing from the "covering tests" list, so the mutant is never run against
its killer.

**Rule.** A `SURVIVED` line is a *lead*, not a verdict. Before writing an
assertion to chase one:

1. Apply the mutation to the source file by hand (one Edit).
2. Run the owning test module: `scripts/test <tests/test_file.py>` (~25 s).
3. **Red** → the survivor was a context-attribution artifact; revert the
   hand-edit and move on (check `git status` is clean — you just edited
   shipped code). **Green** → the survivor is real; write the assertion.

Cheap and decisive versus inventing a test for a hole that isn't there.

**Observed** 2026-08-28, shipping `draft.py` table recovery: mutate-diff
reported `SURVIVED` for `compare == -> !=` on the
`if cur.get("flag") == "needs-table-review":` line in
`src/precis/handlers/draft.py` (its covering-tests list had 5 entries).
The list omitted
`test_edit_table_recovers_grid_from_raw_latex_and_clears_flag`, which
asserts `meta.get("flag") is None` and therefore executes that line.
Applying the mutation and running the module: `1 failed, 35 passed`
(`AssertionError: assert 'needs-table-review' is None`) — killed; the
gap was in mutate-diff's context attribution, not in test coverage.
