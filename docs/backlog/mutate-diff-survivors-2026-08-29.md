# mutate-diff survivors 2026-08-29 (advisory, verify-first)

**Status:** open ·low· — test-gap candidates from the /go gate's advisory
mutation pass on the integrated `main` squash (~`61317f11`), 2026-08-29.
Neither line is hub-tagline code; both belong to sibling ships that day
(`git blame` to attribute).

- `SURVIVED src/precis/workers/job_types/__init__.py:610` — `compare == -> !=`
  survived its covering test
  `tests/test_cast_job_types.py::test_specs_registered_on_claude_inproc`.
- `SURVIVED src/precis_web/routes/drafts.py:1683` — `unary: remove not`
  survived all four covering delete-draft tests
  (the four `tests/precis_web/test_drafts.py::test_delete_draft_wrong_name_does_nothing`-family tests).

`mutation-summary: total=2 run=2 killed=0 survived=2`.

**Verify FIRST** (gotcha `mutate_diff_false_survivor`): a SURVIVED line can
be a coverage-context attribution artifact — apply the mutation by hand and
run the owning module's tests before writing any new assertion. If the
mutant really passes, add the missing assertion to the covering tests;
then delete this file.
