---
status: draft
title: Soak reader distinguishes a skipped matrix from an absent soak
pillar: platform
---

# Soak reader distinguishes a skipped matrix from an absent soak

**Lane constraint:** the touched files — `scripts/lib/soak_verdict.py`,
`scripts/round`, `.github/workflows/check.yml` — are deny-write on every
slice lane (`lanes.INTERIM_DENY` keeps `scripts/lib/**`, `scripts/round`
and `.github/**` denied even relaxed; strict mode only adds more). No
builder lane can ship this; it needs a coordinator or human session.
td600228 could only spec it and land the green reader coverage in
`tests/test_soak_verdict.py`.

## Motivation / why

gr586293, td599737's review (td599738): the ci_watch half landed in
986244001 — a skipped-before-expansion soak matrix ledgers `skipped`,
not absent. The check-runs reader the coordinator verbs share still
cannot. `soak_verdict.latest()` keeps only `soak*` and `VERDICT_JOB`
rows, so it discards the skipped matrix's one placeholder check-run —
the jobs/check-runs APIs name it by the literal, unrendered `name:`
expression (`${{ … 'test-linux' || 'soak' }}`), which carries no `soak`
prefix. And `classify()` drops a skipped `verdict-soak` leg, so a sha
whose soak never ran reads `none` — the same verdict as a sha with no
soak at all.

Benign today (`round cut`/`open` proceeds on `none`, and a skipped soak
is genuinely not red), but it is exactly the parity hole the ci_watch
fix closed: a skipped soak IS soak evidence that ran no verdict — never
`none`. It also leaves a real footgun: an unhandled verdict value today
would fall through `_soak_debt_check` into the red path and write an
empty debt row.

## In scope

- `scripts/lib/soak_verdict.py`:
  - `_placeholder(name)` — the `ci_watch._soak_placeholder` predicate
    verbatim: `name.startswith("${{") and "soak" in name`. It is a
    deliberate twin (scripts/lib is not importable from `src/`); the
    docstring names the sibling, as ci_watch's does.
  - `latest()` — keep placeholder-named rows beside `soak*` /
    `VERDICT_JOB`.
  - `classify()` — drop skipped `VERDICT_JOB`/placeholder rows from the
    verdict legs (today: only skipped `VERDICT_JOB`); when nothing
    remains, return `"skipped"` if soak evidence existed, `"none"` when
    there are no soak rows at all. Red/pending/green unchanged.
    Recommended full parity with `ci_watch._soak_verdict`: drop *all*
    skipped rows, so a matrix whose every expanded leg skipped also
    reads `"skipped"` (was `"green"`) — "the legs exist but did not
    run". The narrower diff (drop only `VERDICT_JOB`/placeholder)
    leaves all-skipped legs `green`; pick per reviewer.
  - `red_jobs()`/`soak_legs()` — unchanged; docstrings updated to name
    the new verdict.
- `scripts/round::_soak_debt_check` — a `"skipped"` branch beside
  `"none"`: `soak skipped on <sha9>` and proceed. Without it the new
  verdict hits the red fallthrough and `_write_soak_red` records an
  empty-job debt row.
- `.github/workflows/check.yml` — the `verdict-soak` comment claiming
  "a skipped verdict-soak reads 'no soak' to soak_verdict.classify"
  updated to name `skipped` (comment only, no behaviour).
- `tests/test_soak_verdict.py` — add the skipped/absent cases (bodies
  in Acceptance criteria); the undisputed-contract coverage already
  landed under td600228 must stay green.

## Explicitly NOT in scope

- `round`'s veto semantics: `"skipped"` proceeds exactly like `"none"`
  — a skipped soak is no verdict. It does not clear a `release.soak_red`
  debt row either (only `green` does); a skipped soak cannot certify a
  fix.
- `scripts/release::soak_red` — red-leg detection; a skipped row is
  never in `_BAD`, so the placeholder blind spot is unreachable there.
- `scripts/lib/last_gated_main_sha.py` — `verdict-soak` stays out of
  `LEG_PREFIXES`; the placeholder is not a blocking leg either way.
- ci_watch — landed in 986244001; no revisit.

## Acceptance criteria

- `classify` on `[placeholder skipped, verdict-soak skipped]` rows →
  `"skipped"`; on rows with no soak evidence → `"none"`.
- `latest` keeps the placeholder-named row.
- `scripts/round` on a last-gated sha whose matrix skipped wholesale
  prints `soak skipped on <sha9>` and proceeds; no `release.soak_red`
  row is created or cleared.
- `pytest tests/test_soak_verdict.py` green, these included:

```python
_SKIPPED_MATRIX_NAME = (
    "${{ (((needs.plan.outputs.full == 'true') && 'test-linux') || 'soak') }} "
    "(${{ matrix.python-version }}, ${{ matrix.shard }})"
)


def test_latest_keeps_the_unexpanded_matrix_placeholder() -> None:
    rows = [[_SKIPPED_MATRIX_NAME, "completed", "skipped", f"{URL}/7"]]
    assert [r[0] for r in SV.latest(rows)] == [_SKIPPED_MATRIX_NAME]


def test_classify_reads_a_skipped_matrix_as_skipped_not_none() -> None:
    rows = [
        [_SKIPPED_MATRIX_NAME, "completed", "skipped", f"{URL}/7"],
        ["verdict-soak", "completed", "skipped", f"{URL}/7"],
    ]
    assert SV.classify(rows) == "skipped"


def test_classify_none_only_when_no_soak_rows_at_all() -> None:
    rows = [["lint", "completed", "failure", f"{URL}/7"]]
    assert SV.classify(rows) == "none"


def test_all_skipped_legs_read_skipped_not_green() -> None:
    """Full-parity recommendation; drop if the narrower diff ships."""
    rows = [
        ["soak (3.13, 1)", "completed", "skipped", f"{URL}/7"],
        ["verdict-soak", "completed", "skipped", f"{URL}/7"],
    ]
    assert SV.classify(rows) == "skipped"
```

## Target + blast radius

- `scripts/lib/soak_verdict.py` (`latest`/`classify`/docstrings),
  `scripts/round` (`_soak_debt_check` skipped branch + log line),
  `.github/workflows/check.yml` (comment), `tests/test_soak_verdict.py`.
- Readers: `round cut`/`open` soak-debt check (same proceed, new line),
  `scripts/release` (`VERDICT_JOB` constant only — untouched),
  `last_gated_main_sha` (`LEG_PREFIXES` untouched), ci_watch (its own
  jobs-API reader, already fixed).
- `scripts/probes/deny_list.py` lints the deny sets — no probe change
  needed; this spec is why `scripts/lib/**` stays denied.

## Open questions / decisions log

- DECIDED (recommended): all-skipped `soak*` legs → `"skipped"`
  (ci_watch parity) rather than `"green"`. The narrow alternative keeps
  them `green`; either way `[success + skipped leg]` stays `green` and
  is pinned by the landed tests.
- `status: draft`, not `ready`: every touched file is deny-write on all
  slice lanes, so an autonomous pick bounces. A coordinator/human flips
  it when assigning the session.
