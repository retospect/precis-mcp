---
name: bug
description: >-
  A map of this repo's bug-intake triage — three buckets a defect falls into,
  and which one is worth a read-only root-cause investigation before any
  patch gets written. Not a new pipeline; a reminder to classify before
  coding, and a guard against a symptom-patch that hides a live defect.
  Reach for it when a bug report or gripe lands and you're about to fix it.
  Repo-dev tool for developing precis-mcp; NOT a precis product skill.
---

# bug — triage before you patch

**The problem this isn't.** Most bugs don't need ceremony — see it, fix it,
ship it. This skill exists for the minority where the tempting fix is a trap:
it makes the symptom go away while the real defect stays live, now harder to
find because the loud signal is gone. `bug` is the triage gate that catches
that case before code gets written, not a mandatory process for every fix.

## The three buckets

1. **No root cause.** "Add a green button," a typo, a genuinely-missing
   check. The symptom *is* the defect. Fix it directly.
2. **Root cause == symptom.** The obvious fix and the real defect are the
   same thing. Fix it directly.
3. **Masked root cause.** The obvious/tempting fix patches the symptom but
   hides a deeper defect — which stays live, harder to find, until it
   resurfaces somewhere else. **This is the bucket the skill exists for**: the
   root-cause analysis is worth more than the fix, because shipping the wrong
   fix here is worse than shipping no fix.

Triage is an Opus-loop judgment call, not mechanical — it's the same kind of
call as "does this need a spec" in `flow`. When in doubt whether bucket 3
applies, treat it as if it might: the cost of a wasted `root-cause` dispatch
is far lower than the cost of shipping a masking patch.

## The sequence

1. **Triage.** Classify the bug into one of the three buckets above.
   Buckets 1/2 → skip straight to coding.
2. **Root-cause investigation (bucket 3 only), before any patch.** Spawn the
   `root-cause` agent (read-only, sonnet) with the bug/gripe. It reproduces
   the failure, traces symptom→true defect through the call graph
   (`scripts/coderef`, `search_code`, git log/bisect), and explicitly answers
   whether the tempting fix would mask something deeper. It returns a
   dossier — root cause + evidence, blast radius, masking risk, fix strategy,
   the regression test to write — and does not itself patch anything.
3. **Regression test.** Hand the dossier's test description to `test-author`
   — write the test that would have caught the real defect, not just the
   symptom.
4. **Fix.** Hand the dossier's recommended strategy to `coder` (a decided,
   well-scoped change) — or keep it on Opus if the dossier surfaced a genuine
   architecture/API/domain call the agent escalated back.
5. **Ship.** `/land` or `/go`, per `flow`'s stage 5 — nothing new here.

## Where durable findings go

A root cause worth remembering beyond this fix — a pattern likely to recur, an
incident worth a runbook — lands in the owning package docstring's "why" lines or
`docs/runbooks/`. It never goes in a "completed log": this repo has no
done-log by design (see `docs/README.md`'s delete-on-ship rule and
memory's landed-work convention) — `git log` plus the regression test is
already the record that the bug is fixed.

## When NOT to reach for this

A trivial fix with no root cause to chase — buckets 1 and 2 above, or
anything where "what's the real defect" isn't even a coherent question — just
do the work. `bug` is a gate for the case where a wrong-but-tempting fix would
hide something, not a mandatory step in front of every patch.
