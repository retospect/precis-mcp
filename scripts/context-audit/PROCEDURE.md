# context-audit procedure

Mechanical, step-by-step. Run this occasionally (it isn't a gate — nothing
blocks on it) to check whether the contexts precis assembles for LLMs are
actually good, and to surface classifier/pre-worker gaps as a distinct,
queryable signal. Source of truth for *why* each context is in scope and the
rubric being applied: `docs/backlog/context-quality-eval.md` (catalog +
Section 2 rubric) and `RUBRIC.md` (the executable form of that rubric) in
this directory.

**Dogfood rule.** Sampling (Step 0) reads a Store directly — point it at a
read-only prod hop or a dev-DB precis, it never writes. Filing findings
(Step 2 onward) goes through a **real `precis` MCP** `put` call — that is a
write. In a dev/test run, use a **dev-DB precis** (`scripts/dev`), never the
session's own MCP connection casually pointed at prod for practice writes.
When this procedure is run for real (filing findings that should persist),
the write naturally lands on whatever `precis` MCP the running agent is
actually connected to — same as any other `gripe` filed during normal work.

## Step 0 — capture

```
uv run scripts/context-audit/capture.py
```

Point `PRECIS_DATABASE_URL` at a read-only prod hop (`127.0.0.1:6432` as
`agent_rw` — see `scripts/prod-psql`) or a local dev-DB precis
(`scripts/dev`) before running. This writes `out/NN-<slug>.md` (one file per
catalog row that sampled successfully) and `out/manifest.json` (the full
list, including any row that raised — those carry `"skipped": true` and a
`"skip_reason"`).

Read `out/manifest.json` first. It tells you exactly which artifacts exist
this run and which catalog rows were skipped (and why) — don't assume last
run's file list.

## Step 1..N — one pass per artifact

`out/manifest.json`'s top level is `{"kind_roster": {...}, "rows": [...]}` —
`kind_roster` records the sampling server's live `Hub.kinds` /
`kinds_supporting_search` (use it to tell "kind missing from a cross-kind
disclosure = real gap" apart from "this build's roster never had the kind");
walk `rows`, not the file itself.

For **each entry in `manifest.json`'s `rows` where `"skipped": false`**, in order:

1. **Read** `out/<entry.file>`. The header (`source_call`, `ref_handle`)
   tells you which real call or builder produced it and which live ref it
   was sampled from — use that if you need to cross-check against the live
   corpus (e.g. `get(kind='todo', id=<ref_handle's id>, view='tree')` on the
   *live* MCP, not the captured snapshot, if something looks stale).

2. **Apply `RUBRIC.md`** — walk all six dimensions against the artifact.
   Skip a dimension that doesn't apply to this artifact's shape (interactive
   vs. agentic) and say so in your own notes; don't force a finding.

3. **For each defect found**, first **dedup-check**, then **file**:

   a. Dedup-check — search before writing a duplicate:
      ```
      search(kind='gripe', q='<the defect, in your own words>')
      ```
      Also worth a look: `search(kind='gripe', tags=['context-audit'])` to
      see prior runs' findings for this same slug. If an open gripe already
      covers this defect, **don't** file a new one — note the existing
      gripe's id in your per-context notes instead (so the tally below can
      still count it) and move on.

   b. File — a genuinely new defect becomes one `gripe`, via the real
      `precis` MCP `put` verb:
      ```
      put(kind='gripe', text='<defect + suggested_fix, one paragraph>',
          tags=['context-audit', 'context-audit:<context_slug>',
                'severity:<MAJOR-C|MAJOR-$|MINOR-C|NIT>'])
      ```
      Every finding's dimension (`skills-reachable`, `info-sufficient`,
      `breadcrumb-correctness`, `progressive-disclosure`,
      `surface-behavior-drift`, `classifier-gap`) and severity tag
      (`MAJOR-C`/`MAJOR-$`/`MINOR-C`/`NIT` — RUBRIC.md's vocabulary, not a
      new one) belong in the gripe text itself, not just the tags, so a
      later reader doesn't need this file open to understand it. Tag every
      finding from a classifier/pre-worker gap with
      `tags=['context-audit', 'context-audit:<slug>', 'classifier-gap']` in
      addition to its severity tag, so Step 3 below can pull them
      separately.

4. **Record a per-context verdict** — `pass` / `thin` / `bad`, per
   RUBRIC.md's definitions — in your own running notes (you'll tally these
   at the end; no separate file, this run's tally is the deliverable).

## Step 3 — final tally

Once every artifact in the manifest has a verdict:

- Report the **pass/thin/bad breakdown** across all sampled contexts —
  e.g. "7 pass, 2 thin, 1 bad (11 sampled, 0 skipped)".
- List the **classifier/pre-worker gaps separately**, one line each,
  context-slug + what's missing + what pass/classifier would need to exist
  to fill it. This is the "do we need more classifiers/pre-workers" output
  the catalog doc calls out as load-bearing — don't bury it inside the
  general findings list.
- Note any **skipped** catalog rows from the manifest (sampler raised, or a
  dry-run entry doesn't exist yet) — these are gaps in the *harness*, not
  findings about precis itself; mention them so the next run's coverage is
  visible, but don't file a gripe for a harness gap unless it's durable
  (file a note in `docs/backlog/context-quality-eval.md`'s own tracking
  instead, or ask the coordinating agent).
- If this was a from-scratch or renewed pass, consider whether
  `docs/backlog/context-quality-eval.md`'s catalog (Section 1) still matches
  what `capture.py`'s registry actually samples — a catalog row with no
  corresponding sampler, or a sampler with no catalog row, is itself worth a
  one-line gripe (dimension: none of the six — file it as a plain
  maintenance note, tagged `context-audit` only).

## Optional — unattended pass

`scripts/context-audit/run.sh` chains Step 0 into a `claude -p` pass over
`RUBRIC.md` + one artifact at a time (mirrors
`scripts/exercise-mcp/run.sh`'s shape) for a fully unattended run. Prefer
walking this procedure by hand the first few times so the judgments (what
counts as MAJOR-C vs. MINOR-C, when a "gap" is really a classifier gap) are
calibrated before automating them.
