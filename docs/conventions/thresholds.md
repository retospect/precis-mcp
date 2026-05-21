# Thresholds — when to stop and ask

This file defines points where an agent (or human contributor) MUST
pause and seek explicit user confirmation rather than push through
silently. The heuristic: any change whose blast radius exceeds the
local module deserves a checkpoint.

## Schema thresholds

Stop and ask before:

- Adding a column to an existing table that is already populated in a
  user's deployment. Migrations are forward-only; the choice of
  default value is sticky and visible forever.
- Renaming a table or column. Users have client code and SQL views
  referring to the old name.
- Dropping any column or table — even one believed unused. Always
  verify the agent's grep is exhaustive against `tests/`,
  `src/precis/data/skills/`, the `docs/` tree, and recent git
  history. If the column is referenced in any place, ask.
- Changing the dimension of `blocks.embedding` (or, after v2,
  `block_embeddings.vector`). This invalidates pre-computed vectors
  and forces re-embed of the whole corpus.
- Introducing a destructive migration (`DROP COLUMN`, `TRUNCATE`,
  `ALTER ... TYPE` that loses precision). Postgres will run them; the
  user may not have a backup.

## API thresholds

Stop and ask before:

- Removing a CLI subcommand or flag. The user has scripts and shell
  history that depend on it. Provide a deprecation path instead
  (warn for one minor version, remove in the next).
- Changing the JSON shape of an MCP response. Agents in flight are
  using whatever is current. Add new fields freely; never repurpose
  or delete existing ones.
- Changing the seven-verb surface (`list`, `get`, `search`, `put`,
  `edit`, `delete`, `cite`). The verb count is a constitutional
  promise. New functionality piggybacks on existing verbs through
  `kind=` or `mode=` arguments.

## Performance thresholds

Stop and ask before:

- Adding a new global model load (LLM, embedder, classifier) at
  module-import time. Modules import once per process; a careless
  import doubles startup memory.
- Introducing a synchronous network call on the request hot path.
  Lookups (S2, CrossRef, OpenAlex) are tolerable in ingest workers
  but unacceptable in `precis serve`'s search path.
- Replacing pgvector with another vector store. The store choice is
  pinned by `0001_initial.sql`; switching is an architectural
  decision deserving an ADR.

## Cross-package thresholds

Stop and ask before:

- Pinning a new minimum version of an upstream lib (`marker-pdf`,
  `sentence-transformers`, `psycopg`, `mcp`). Check the lock file's
  current pin and the ecosystem's release cadence. Major-version
  bumps deserve their own ADR.
- Adding a new optional-extra (`[paper]`, `[docx]`, …). Optional
  extras are part of the public surface; users have install scripts
  that name them.
- Merging another package into `precis-mcp` (currently planned for
  `acatome-extract`). Plan in `docs/design/`, ADR in
  `docs/decisions/`, do not rush.

## Ingest thresholds

Stop and ask before:

- Changing the `pdf_hash` algorithm. Existing `ref_identifiers`
  reference the current hash; mass re-issue is required.
- Changing the slug generation rule. Existing user docs cite by
  slug; breaking the rule strands those references.
- Changing the `pub_id` derivation rule (post-v2). Same rationale —
  external citations grow stale.

## Operational thresholds

Stop and ask before:

- Truncating or rewriting any user-owned data file (`~/work/corpus/`,
  `~/work/new_papers/`, anything under `~/.acatome/`). The agent's
  filesystem mutations are a one-way trip.
- Wiping the production DB. Even on the user's confirmation: print
  the row counts you are about to destroy, wait for explicit go.
- Force-pushing on `main`. Never. Use a feature branch and a PR.

## Default behaviour when a threshold trips

1. Stop the in-progress edit.
2. Surface the threshold in chat with the smallest possible
   description of what would change.
3. Offer 2–4 alternatives via an `ask_user_question` block.
4. Do not proceed until the user names the choice.
5. Record the resolution in `docs/decisions/` if the resolution is
   non-obvious.
