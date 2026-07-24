# context-audit

Precis assembles a lot of context for LLMs — interactive tool-call renders
(`get`/`search` output an agent reads) and agentic prompts (planner ticks,
review-tier digests) it builds for itself. This harness is the periodic check
on whether those contexts are actually *good*: reachable, sufficient,
correctly cross-referenced, disclosed at the right depth, honest about what
the surface can do, and not silently missing a field a classifier/pre-worker
pass was supposed to fill in. It exists so that check happens on a cadence
instead of only when something breaks loudly enough to notice.

The **catalog** (which contexts to sample, why each matters) and the **six-dimension
rubric** used to judge them live in `docs/design/context-quality-eval.md` — that
document is the source of truth; this directory is only the runnable half.

## How to run

1. `uv run scripts/context-audit/capture.py` — deterministic sampler. Connects
   read-only to a Store (`PRECIS_DATABASE_URL`), pulls one real sample per
   catalog row, and writes `out/NN-<slug>.md` + `out/manifest.json`.
   `--list` prints the catalog without touching a DB; `--only <slug>` /
   `--limit N` narrow a run.
2. Walk `PROCEDURE.md` — for each artifact in `out/manifest.json`, read it,
   apply `RUBRIC.md`, dedup-check and file any findings as `gripe`s, and
   record a per-context verdict. This is the step a Sonnet agent runs
   unattended, occasionally.
3. Optionally, `scripts/context-audit/run.sh` chains step 1 into a `claude -p`
   pass over `RUBRIC.md` for a fully unattended run (mirrors
   `scripts/exercise-mcp/run.sh`'s shape).

See `RUBRIC.md` for the judging rubric and `PROCEDURE.md` for the exact,
mechanical steps.
