# ADR 0013 — MCP session context as three env vars, not one config block

- **Status**: accepted (2026-05-26)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0003 — shared tool registry (`docs/decisions/0003-shared-tool-registry.md`)
- **Plan artefact**: `docs/design/mcp-cold-start-token-budget.md`

## Context

The MCP session-ergonomics rollout introduced three orthogonal
operator-controlled inputs to the cold-start banner and dispatch
table:

1. **`PRECIS_STARTUP_SKILLS`** — pin specific skill docs at session
   start so the agent gets project context without an exploratory
   `search(kind='skill')` call.
2. **`PRECIS_KINDS_DISABLED`** — prohibit specific kinds at boot so
   handlers a deployment doesn't want to expose are hidden from
   dispatch, banner, and prompt list alike.
3. **`PRECIS_DEFAULT_TAGS`** — merge a fixed list of tags into every
   `put` on note-like kinds so refs created during a session
   accumulate consistent project / context metadata.

All three could plausibly have lived in a single structured
configuration block — a YAML `session_context:` stanza, or a
single `PRECIS_SESSION=...` env var carrying a packed shape — but
this ADR records the choice to keep them as three flat env vars
with shared parse semantics.

Three design forks needed pinning while implementing the rollout,
none of them obvious from the public banner / dispatch shape:

- **F1.** Naming for the kind-prohibition env var: `_DISABLED` vs
  `_DENY` vs `_PROHIBITED`.
- **F2.** Behaviour when a `PRECIS_KINDS_DISABLED` entry names a
  kind whose env requirements are also missing: which reason
  surfaces in the `Kinds unavailable:` banner line?
- **F3.** Dispatch-hook location for `PRECIS_DEFAULT_TAGS`: the
  per-handler boundary (each handler reads the config) or the
  runtime boundary (runtime mutates args before dispatch).

## Decision

### Three flat env vars, shared parse semantics

Each env var is a plain comma list. Whitespace is tolerated.
Empty entries are dropped. Duplicates are deduped first-occurrence-
wins. The same parse function shape (and the same docstring
language) lives in three modules — `precis.startup_skills`,
`precis.kind_gate`, `precis.default_tags` — and the cross-cutting
test in `tests/test_token_budget.py` pins them as a triple so a
future tweak to one parser surfaces as a test failure rather than
a silent desync.

Rationale: each axis has a different lifetime (skills are bytes
on the wire, kinds are dispatch-table membership, tags are per-ref
metadata) and a different operator workflow (skills get tuned
per-agent, kinds get tuned per-deployment, tags get tuned per-
project). A single structured block would force every operator
through every axis even when only one is relevant.

### F1: `PRECIS_KINDS_DISABLED` — neutral verb, parallel reading

Chose `_DISABLED` over `_DENY` / `_PROHIBITED`:

- `_DENY` reads as access-control language (the agent is "denied"
  the kind). Inaccurate — the kind is genuinely absent from
  dispatch, not access-gated.
- `_PROHIBITED` is the noun a programmer reaches for after looking
  at the verdict enum but reads strangely as an env-var suffix.
- `_DISABLED` reads cleanly alongside the existing
  `PRECIS_STARTUP_SKILLS_CAP_KB` (operator-tunable knob) and
  `PRECIS_NO_*` boolean toggles. Neutral verb, no embedded policy.

### F2: prohibition wins over resource-missing

When `PRECIS_KINDS_DISABLED=patent` is set on a deployment that
also lacks `EPO_OPS_KEY`, the patent kind is reported as
`patent (prohibited)` in the `Kinds unavailable:` banner line, not
`patent (missing EPO_OPS_KEY)`. The kind-gate `_try` consults the
prohibition predicate **before** invoking the handler constructor,
so the resource-missing path is never reached for a prohibited
kind.

Rationale: the operator's stated intent (turn this off) is more
informative than the deployment incident (you also lack creds).
If both states change — the operator un-disables the kind **and**
provides creds — the kind comes back; if only one changes, the
correct verdict surfaces automatically on next boot.

Symmetric: when only the env var is missing (no prohibition), the
banner correctly reports `patent (missing EPO_OPS_KEY)` — distinct
reason string so an operator scanning the banner can tell the two
states apart at a glance.

### F3: dispatch hook at the runtime boundary, not per-handler

`PRECIS_DEFAULT_TAGS` merging happens in
`PrecisRuntime._invoke_handler` before the handler sees its args,
not inside each note-like handler's `put` method.

Rationale considered:

- **Per-handler:** each handler reads the runtime's resolved
  default-tags tuple inside its own `put`. Local but requires
  every note-like handler to remember to opt in; an audit-by-grep
  for the policy is painful (nine handlers today, more later).
- **Runtime hook (chosen):** one decision point, gated by
  `KindSpec.note_like`. Adding a new note-like kind is a one-line
  spec edit; no handler-side work required. Symmetric: the
  reverse (turning the policy off for a specific kind) is also a
  one-line spec edit.

The `note_like` flag is a `KindSpec` field rather than a hard-
coded kind list in the runtime so the policy-relevance of each
kind is visible on the spec itself when reading the handler. A
future operator-controlled policy that targets a different kind
slice would carry its own flag rather than expanding `note_like`'s
semantics.

The `tag` verb is the asymmetric case: it emits a non-mutating
**suggestion** hint listing defaults not yet present rather than
auto-mutating the args. The reasoning is that `tag` is a verb the
operator (or agent) explicitly invoked with explicit tags; silent
mutation would surprise. `put` is the path where session-context
should layer onto every-ref-by-default; `tag` is the path where
intent is already explicit.

## Consequences

**Followed from this ADR:**

- All three env vars live in `PrecisConfig` as flat fields with
  parallel docstrings.
- `kind_gate.py`, `startup_skills.py`, and `default_tags.py`
  share parse semantics. Cross-cutting parser test pins the
  triple in `tests/test_token_budget.py`.
- `KindSpec.note_like` (`docs/user-facing/seven-verb-surface-migration.md` D7
  contract additive) carries the policy-relevance flag. Handlers
  flipped: memory, todo, gripe, flashcard, quest, conversation,
  markdown, plaintext, tex (nine of seventeen handlers in the
  repository today).
- Default-tag merging fires from
  `PrecisRuntime._invoke_handler` via
  `_apply_default_tags_policy`, gated on `note_like` + verb. Per-
  call cost is `O(1)` tuple-truthiness when defaults are empty.
- Hint topics `default_tags.merged` (info; emitted on `put` when
  defaults change `tags=`) and `default_tags.suggested` (info;
  emitted on `tag` when defaults are missing) surface the policy
  state to the agent.

**Open at the time of writing:**

- **Workspace auto-tag layering** (OQ-17 in the plan artefact):
  the prose-file handlers (markdown / plaintext / tex) already
  auto-stamp every ref with the `workspace` flag tag on ingest.
  When `PRECIS_DEFAULT_TAGS=fbproj` is also set, the resulting
  ref carries **both** `workspace` and `fbproj`. Tentatively
  correct (`workspace` identifies file-rooted-ness; `fbproj`
  identifies project context; both true simultaneously is the
  right semantics) but no test pins the layering today. Followup
  ticket in `OPEN-ITEMS.md` if/when a deployment reports the
  combined behaviour is wrong.
- **`requires_env` convergence** (OQ-16): only the patent
  handler's inline env gate was moved onto
  `KindSpec.requires_env` in this rollout. Other handlers
  (oracle, math, web, youtube) still read their env vars inline
  in `__init__`. Followup ticket in `OPEN-ITEMS.md`; each
  conversion is small but wants its own review.

## Alternatives considered

1. **Single `PRECIS_SESSION_CONTEXT` env var carrying a packed
   shape.** Rejected: operators wanting only one axis would have
   to learn the packing rules. The flat env-var triple matches
   how every other `PRECIS_*` knob is shaped today.

2. **Per-handler default-tag opt-in via subclass override.**
   Rejected: see F3 above. The audit-by-grep cost is real and
   the per-handler boilerplate adds nothing the `note_like` flag
   doesn't.

3. **Resource-missing wins over prohibition.** Rejected: see F2
   above. The operator's stated intent is more informative than
   the deployment incident.

4. **Auto-mutate `tag` to merge defaults silently.** Rejected:
   see F3 above. `tag` is the explicit-intent path; `put` is the
   layer-defaults path. Mixing the two on `tag` would surprise.
