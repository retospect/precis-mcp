---
status: ready
pillar: platform
---

# R14 bounded gate repair

Repair seven source-traced failures from frozen
`53e6cb255a476619353b269ddbad85f58f3eeb43`: four PCB dead-export cases,
one encoding gate, one POSIX-test guard and the empty live gripe page.
The canonical gate log reports 11 failures; the four routing/job failures
are separate diagnosis and outside this patch.

Name UTF-8 on the three wrapper-fixture writes and thermo data read. Mark
the wrapper test module POSIX-only because it executes real bash and an
executable shell fixture. Restore the distinct live empty message without
changing concrete-status, all or wontfix copy or weakening the route test.

Register only snapshot export/load/canonical-JSON/file-read functions in
the existing known-unwired map, each with its own source-checkable reason.
The PCB package and snapshot docstrings and the owning escape spec define
an internal manual/dev/test replay tool with no public op. Keep the caller,
stale-entry and nontrivial-reason checks intact; no routing exemptions.

Validate the exact failed contracts plus wrapper, exact-status and thermo
read regressions through `scripts/test`; run scoped container types and
host Ruff/format. Use worktree scratch for temporary scripts. No full gate,
routing run, version/dependency change, integration or deployment here.
Commit and publish this task branch for root's independent review; version
remains the frozen 8.35.13.

Focused canonical evidence: 17 passed, four explicit snapshot known-unwired
skips, exit 0; stale-entry and nontrivial-reason checks passed. Scoped
container mypy passed for the three changed test modules. Host Ruff check
and format check passed. Raw commands, logs and exits are retained in
`.scratch/*-resumed.*` and `.scratch/ruff-*`; full gate remains root-owned.
