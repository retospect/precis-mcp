# Vocab compaction C+D+E — post-ship residuals (c6c386a3)

Harvested at the /go of stages C+D+E (2026-08-30). Delete items as they ship.

## Mutation survivors in quest/* (advisory, unverified)

`scripts/mutate-diff` on the C+D+E squash reported 9 SURVIVED, all in the
quest fidelity-key read paths (catalyst_seed.py:214 `is not→is`,
compute.py:1863 `or→and`, figures.py:292 `or→and`, frontier.py:1163
`in→not in`, gaps.py:119 `and→or`, rulings.py:206 `or→and`, + 3 more in the
run log). Mostly boolop flips on `meta.get('fidelity_*') or default`
fallback chains — plausible real assertion gaps on the renamed keys, but
mutate-diff SURVIVED can be a context-attribution artifact: apply the
mutation + run the module's tests before believing any of them
(auto-memory `mutate_diff_false_survivor`). If real → add the missing
assertions to the quest tests.

## vocab-lint markdown blind spot

`tests/test_vocab_lint.py` retired-name/phrase scans cover `src/**/*.py`
only. The C+D+E reviewer caught stale `precis-tasks-help` /
`precis-dispatch-help` references in README.md and docs/glossary.md that
the lint could not see — fixed by hand that time. Extend the lint: scan
`README.md`, `docs/*.md`, and `src/precis/data/skills/**/*.md` for retired
SKILL IDS at minimum (skill ids are deterministic, no false-positive risk —
unlike banning bare English words in prose).

## Quiesce protocol observation (for the next column-rename stage, if any)

The drain flag stops CLAIMS but not heartbeats: melchior's old binary's
heartbeat hit `column r.deleted_at does not exist` at 19:22:44 UTC — 23s
after 0149 applied, ~2min before that host's bounce. One failed local-llm
advertise, self-healed on restart. If a future stage renames a column read
by the heartbeat path, either bounce workers before the migration play or
accept this bounded blip knowingly.

## Migration fixture tests (accepted gap, optional)

None of 0143–0149 has a seed-legacy-row → assert-post-shape test; accepted
at ship time (matches repo convention; all 7 files line-reviewed + probed
against live pg17 by the reviewer). If a future stage adds jsonb-rewrite
migrations of similar shape (0145/0146/0147 were the risky ones), write the
fixture harness then and cover these retroactively.
