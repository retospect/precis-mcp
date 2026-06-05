# review-paper — multi-pass paper review via `claude -p`

A `claude -p` harness that fans out a set of reviewer personas over a
paper in the precis corpus. Each persona produces a structured
findings report; the orchestrator (manual today, agent-driven
eventually) consolidates them into a single ship/revise/don't-ship
verdict.

Same pattern as `scripts/exercise-mcp/`, but targeting paper review
instead of MCP-surface review.

## Where the prompts live

The reviewer personas are **shipped skills** — `*.md` files under
`src/precis/data/skills/personas/`. Authored once, served both:

- **Via this harness** (call `_load_skill(slug)` from the precis
  package — same code path the MCP server uses — then substitute
  `<handle>` for the target paper and pipe to `claude -p`).
- **Via the MCP server** (`get(kind='skill', id='precis-adversarial-reviewer')`),
  so any agent talking to precis can discover and adopt the persona
  on its own.

Shared discipline (reviewer stance, output format, cleanup, MCP
cold-start preamble) lives in
`src/precis/data/skills/precis-common-reviewer.md` and gets pulled
in by each persona via `{{include doc:precis-common-reviewer#…}}`.
The skill handler expands directives at load time so both consumers
above see a fully flat prompt — no raw `{{include}}` tokens, no
HTML-comment markers, just inline content.

## Run a review

```bash
# All personas, serial:
scripts/review-paper/run.sh paper:smith2024whatever

# Single pass:
scripts/review-paper/run.sh paper:smith2024 precis-adversarial-reviewer

# Different model:
MODEL=claude-sonnet-4-6 scripts/review-paper/run.sh paper:smith2024
```

Reports land at `scripts/review-paper/out/<stamp>-<handle>-<persona>.md`
alongside debug logs and the rendered prompt that was actually sent
(useful for verifying the includes expanded correctly).

## Add a persona

1. `cp src/precis/data/skills/personas/precis-adversarial-reviewer.md \
       src/precis/data/skills/personas/precis-<your-name>-reviewer.md`
2. Edit:
   - `id:`, `title:` in frontmatter to match the new slug.
   - `## Adopt this persona` body — what role does this reviewer
     play, what's the scope.
   - `## What to look for in this pass` — the checklist that's
     unique to this pass.
   - `## Categories for this pass` — finding categories.
   - Keep the `{{include doc:precis-common-reviewer#…}}` directives
     unchanged for the shared blocks.
3. Add the new slug to
   `src/precis/data/skills/precis-polish-paper.md`'s
   `invokes-personas:` frontmatter list so the runbook knows about
   it.

The harness automatically picks up any
`personas/precis-*-reviewer.md` file when called without a specific
persona argument.

## Aggregate

`precis-polish-paper.md` is the runbook skill that describes the
consolidation step. Today it's manual — run the passes, read the
per-persona reports, write the consolidated report by hand. A
future agent-driven mode (using the in-flight `claude_p` util) will
do this automatically.
