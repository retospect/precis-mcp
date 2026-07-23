---
status: draft
title: Fixer — right-size the model per proposal, allow declared sequencing, make the builder delegate
---

# Fixer — right-size the model per proposal, allow declared sequencing, make the builder delegate

## Motivation / why

The laptop fixer (ADR 0048, `src/precis/fixer/`) takes a `docs/proposals/*.md`
file and hands it, whole, to one `claude -p` call in one worktree
(`src/precis/fixer/tick.py::_spawn_claude`, prompt built by
`src/precis/fixer/tick.py::_compose_prompt`). A three-line fix and a
twelve-file refactor get identical treatment: one shot, one model, one
undifferentiated context that plans, edits, tests, and doc-syncs itself.
Three concrete costs fall out of that:

1. **Fixed top-tier model regardless of size.** `PRECIS_FIXER_CLAUDE_MODEL`
   defaults to `claude-opus-4-8` for every build (`src/precis/fixer/tick.py::FixerConfig`).
   CLAUDE.md's own Agent-sizing table already says a "decided change or bounded
   op" — which is exactly what a proposal is supposed to be, since the (not yet
   built) `ready` gate is specified to reject anything under-specified or
   overreaching — belongs on the Sonnet tier, with Opus reserved for
   architecture/novel-judgment work. The fixer doesn't apply that distinction to
   itself.
2. **No declared ordering between proposals.** Two related proposals (e.g. "add
   the field" then "backfill it") have no way to say so; the human either
   crams both into one spec (defeating the point of a proposal being bounded)
   or manually times when to mark the second `ready`.
3. **No delegation inside the build.** The worktree the builder runs in has
   the full `.claude/agents/*` roster (`coder`, `tidy`, `test-runner`,
   `documenter`, `navigator` — confirmed tracked in git, so present in every
   fixer worktree), but `_compose_prompt` never tells the builder to use them.
   It does lint-fixing, test-running, and doc-sync inline in the same
   top-tier context instead of handing mechanical substeps to a cheaper named
   subagent — the exact discipline CLAUDE.md's Agent-sizing section already
   prescribes for interactive sessions.

This proposal is scoped to the **fixer as a Claude Code tool** — how it
builds — not to precis-mcp's own product-level task substrate. (See
"Explicitly NOT in scope".)

There is currently **no automated, wired-in `ready` judge** in the repo. A
`ready` subagent (`.claude/agents/ready.md`, added in this same change — not
prior infrastructure) can vet a single proposal against ADR 0048's rubric
when spawned by hand, but nothing calls it automatically — `status: ready` is
still a human-set front-matter flag, and there is no `/ready` skill or
fixer-side invocation yet. Everything below is therefore scoped to be
buildable without depending on that judge being wired in: mechanical fields +
a written heuristic a human (or a manually-spawned `ready` agent) applies
when authoring a proposal, not a new automated gate.

## In scope

1. **Per-proposal `model:` front-matter** (`sonnet` | `opus` | `haiku`,
   optional). `src/precis/fixer/intake.py::WorkItem` gains a `model:
   str | None` field, parsed the same way `status`/`title` are today
   (`parse_front_matter`). `src/precis/fixer/tick.py::FixerConfig` keeps a
   `default_model`, but **the default changes from `claude-opus-4-8` to
   `claude-sonnet-5`** — a deliberate behavior change, called out again in
   Acceptance criteria. A small tier→id map (mirroring the existing
   `Autonomy` `StrEnum` pattern) resolves `sonnet`/`opus`/`haiku` to concrete
   model ids; `_spawn_claude` uses `item.model or cfg.default_model`.
   **Pinned literals** (the repo currently has two divergent haiku ids in
   different subsystems — `utils/claude_p.py`'s `claude-haiku-4-5` vs.
   `utils/llm/router.py`'s `claude-haiku-4-5-20251001` — reconciling those is
   a separate, pre-existing inconsistency, not this proposal's job): this
   map uses `sonnet`→`claude-sonnet-5`, `opus`→`claude-opus-4-8`,
   `haiku`→`claude-haiku-4-5-20251001` (the more specific, current id).
2. **`blocked-by: <slug>` front-matter.** `ready_proposals`/`pick_next`
   (`src/precis/fixer/intake.py::ready_proposals`,
   `src/precis/fixer/intake.py::pick_next`) gain a skip: a proposal naming a
   `blocked-by` slug is not pickable while that slug's branch still exists
   (local, worktree, or remote), via the existing `branch_exists` predicate.
   **This requires `run_tick` to actually make the local branch go away on a
   real ship** — `scripts/ship` only deletes the *remote* copy (`git push
   origin --delete "$BRANCH"`) and resets the *local* one to shipped `main`
   (deliberate, for interactive `/land`/`/go`, which reuse the worktree); it
   never deletes it, and `branch_exists` checks local first, so an unmodified
   fixer would never see a shipped predecessor's branch disappear.
   `src/precis/fixer/tick.py::run_tick` gains a local `shipped: bool`, set
   `True` only once `ship_ok` is confirmed (autonomy `ship`/`full`, after
   `scripts/ship` succeeds) — never in `report` mode, where the branch is
   deliberately pushed and kept for a human `/go`. The existing `finally`
   block, after `_worktree_remove`, additionally runs `git branch -D
   <item.branch>` in `cfg.repo_root` **iff `shipped`** (force-delete: a
   squash-merged branch's commits aren't ancestors of the squash commit, so
   `-d` would refuse). This is scoped entirely to the fixer's own worktree
   lifecycle in `tick.py` — `scripts/ship` itself is unchanged, so the
   interactive case (branch reset, not deleted, so the worktree keeps
   working) is untouched. Proposals with no `blocked-by` are unordered
   relative to each other, as today.
3. **Builder delegates mechanical substeps.**
   `src/precis/fixer/tick.py::_compose_prompt` gains a paragraph instructing
   the builder to hand off mechanical substeps — running the test suite,
   lint/format cleanup, doc-sync, code lookup — to the matching named
   subagent (`test-runner`, `tidy`, `documenter`, `navigator`) via the Agent
   tool, rather than doing them inline in its own (possibly Opus-priced)
   context. This is a prompt-text change only; no new plumbing.
4. **A written split heuristic in the proposals README/TEMPLATE.** Since
   there's no automated judge to carry this, add a short "should this be more
   than one proposal?" section to `docs/proposals/README.md` for the human
   authoring a spec: split when the proposal names genuinely independent
   deliverables (each separately testable/shippable) rather than one
   deliverable touched from several angles; use `blocked-by` for real
   ordering; don't split mechanical, single-shape work. This is intentionally
   a documented convention, not new code — it's the cheapest version of the
   `precis-decomposition-help` discipline that fits without a judge.

## Explicitly NOT in scope

- **Real concurrent builds.** ADR 0048 deliberately runs one item at a time
  under one lock (no concurrent ships/deploys). This proposal doesn't touch
  that — "splitting" here means smaller, separately-gated, sequentially-built
  proposals, not parallel worktrees or parallel `claude -p` processes. If
  fixer throughput becomes the bottleneck later, that's a separate proposal.
- **An automated `ready`/split judge.** Building the (currently unbuilt)
  `ready` gate, or an LLM step that decides to split a proposal for you, is
  out of scope — item 4 above is a written heuristic a human applies, not a
  new judge. A future `ready` judge can absorb this heuristic once it exists.
- **precis-mcp's own product-side task decomposition** — `plan_tick`, the
  `todo` kind, `precis-decomposition-help.md`. That's the content lane's
  planner discipline for precis-mcp's own worker fleet, a different substrate
  that ADR 0048 §3 explicitly keeps repo-dev off of. Not touched here.
- **ADR 0046's LLM router.** The fixer calls the `claude` CLI directly with
  `--model`, bypassing the in-process Python router entirely; this proposal's
  `model:` field is a raw tier→id map local to the fixer, not a new router
  call site.
- **Gripe intake.** Still off by default per ADR 0048; `blocked-by` and
  `model:` are only wired for proposals in this pass.

## Acceptance criteria

- A `status: ready` proposal with no `model:` field builds with
  `claude-sonnet-5`, not today's `claude-opus-4-8` — a unit test on
  `FixerConfig`/`WorkItem` resolution asserts this directly. **This is a
  deliberate default change, not a bug** — flag it loudly in the PR/commit
  message so it isn't mistaken for a regression.
- A proposal with `model: opus` (or `haiku`) resolves to the matching model
  id regardless of `cfg.default_model`.
- A proposal with `blocked-by: <slug>` is skipped by `pick_next` while
  `<slug>`'s branch exists (local/worktree/remote) and becomes pickable once
  it doesn't — unit-testable against `intake.py` alone with a fake
  `branch_exists`, no live git required beyond what's already faked today.
- **After a real ship** (autonomy `ship`/`full`, `scripts/ship` succeeds),
  `run_tick` deletes the local `fix/<slug>` branch in `cfg.repo_root` — a
  `run_tick`-level test (not just the `intake.py`-level fake above) asserts
  `branch_exists(repo, item.branch)` is `False` immediately after a
  successful tick, closing the gap the `ready` review found (remote-only
  deletion made the criterion above pass on a fake while staying
  gate-green-but-wrong in production).
- **In `report` mode, the local branch is deliberately left behind** — a
  `run_tick` test on the report-autonomy path asserts `branch_exists` still
  returns `True` afterward, so a successor's `blocked-by` correctly keeps
  blocking until an actual ship happens.
- `_compose_prompt`'s output includes the delegate-to-subagents paragraph
  (covered by whatever test already snapshots/asserts on prompt content, or a
  new one if none exists).
- `docs/proposals/README.md` states the split heuristic in a few lines, next
  to the existing lifecycle section.
- Existing fixer tests stay green; new front-matter keys are additive
  (absent ⇒ current behavior except the model default).

## Target + blast radius

- `src/precis/fixer/intake.py` — `WorkItem` gains `model`, `blocked_by`;
  `pick_next` gains the blocked-by skip.
- `src/precis/fixer/tick.py` — `FixerConfig` gains `default_model` (renamed
  from the current single `claude_model`, default flipped to
  `claude-sonnet-5`) plus the tier→id map; `_spawn_claude` resolves
  `item.model`; `_compose_prompt` gains the delegation paragraph; `run_tick`
  gains the `shipped`-gated local `git branch -D` cleanup in its `finally`.
- `docs/proposals/TEMPLATE.md` — document the new `model:`/`blocked-by:`
  optional front-matter keys.
- `docs/proposals/README.md` — split heuristic + a one-line mention of
  `blocked-by` in the conventions list.
- Test coverage wherever the fixer is already tested (mirror existing test
  file layout — no new test infra).

## Open questions / decisions log

- ~~Is `claude-sonnet-5` the right blanket default...~~ **Resolved:** keep
  `claude-sonnet-5` as the default (no forced explicit statement) —
  friction-minimizing, and matches CLAUDE.md's own tiering default ("start
  cheap, hand a decided change to the sonnet tier"). The risk the
  alternative was hedging against (a spec that actually needs Opus silently
  gets downgraded and nobody notices) is closed a different way: `ready`'s
  own rubric now includes a check for exactly this mismatch (see
  `.claude/agents/ready.md`) — a proposal whose content reads as
  architectural/novel-judgment without declaring `model: opus` is flagged
  advisory. That makes the default safe to keep rather than something to
  route around with mandatory ceremony on every proposal.
- Should `blocked-by` support more than one slug (a real DAG) or is a single
  predecessor enough for the cases that actually come up? Starting with a
  single slug (simplest thing that covers "do A, then B") — revisit if a
  real proposal needs more.
- Naming: `default_model` vs. keeping `claude_model` and just changing its
  default — either is fine; flagged only so the rename doesn't get bikeshedded
  mid-build.

### `ready`-agent findings (auto-vetted against ADR 0048 §1)

- **blocker** — Scope item 2's central premise is false against the real code:
  `scripts/ship` (`git push origin --delete "$BRANCH"`, line ~286) deletes only
  the **remote** branch on squash-merge; it resets the local branch to
  freshly-shipped `origin/main` (step 5) but never deletes the local ref, and
  neither `scripts/ship` nor `tick.py::_worktree_remove` (which only does
  `git worktree remove --force`, not `git branch -d/-D`) ever removes a `fix/*`
  branch locally. `branch_exists` (`src/precis/fixer/tick.py::branch_exists`)
  checks the local branch first and returns `True` immediately if found,
  never reaching the remote check. So in the fixer's own repo clone, a
  predecessor's `fix/<slug>` ref persists forever after it ships, and
  `blocked-by` would never unblock — even though the acceptance criterion's
  own unit test (fake `branch_exists`) passes green. This is a gate-green-but-
  wrong build for the one load-bearing behavior this scope item promises.
  **Resolved:** scope item 2 above now specifies the `shipped`-gated local
  `git branch -D` cleanup in `run_tick`'s `finally`, scoped to `tick.py` only
  (not `scripts/ship`, which stays correct for the interactive case);
  Acceptance criteria gained the two tests that would have caught this.
- **blocker** — Motivation's claim "A `ready` subagent (`.claude/agents/ready.md`)
  now exists" is not supported by the repo: the file is untracked (`git status`
  shows `??`), absent from `git ls-files`, and absent from `git log --all` /
  every local and remote branch swept (`main`, `devin-port`, all
  `worktree-*`/`worktree-agent-*` branches). It exists only as a throwaway
  file in one worktree, not a durable repo asset the Motivation can lean on
  for the "no automated gate, but a manual one already exists" framing that
  motivates In-scope item 4's design.
  **Resolved:** Motivation now says the `ready` subagent was "added in this
  same change — not prior infrastructure," true regardless of ship order.
- **blocker** — Open-questions entry 1 ("Is `claude-sonnet-5` the right
  blanket default…?") still reads as an open judgment call for Reto ("not
  something to decide silently in code"), but In-scope item 1 and Acceptance
  Criteria have already silently decided it ("the default changes from
  `claude-opus-4-8` to `claude-sonnet-5` — a deliberate behavior change").
  The spec can't both treat this as settled-and-testable and as pending a
  human decision; per the template's own rule, this blocker-severity entry
  can't remain open at `status: ready`.
  **Resolved:** the open-questions entry now records the actual decision
  (keep the default) plus the specific mitigation (`ready`'s new
  model-tier-vs-content check) instead of relitigating it — no more
  contradiction between "settled" and "open."
- advisory — The tier→id map's `haiku` resolution is ambiguous: the repo
  already carries two different canonical haiku ids in different subsystems
  (`claude-haiku-4-5` in `utils/claude_p.py::_DEFAULT_MODEL` vs.
  `claude-haiku-4-5-20251001` in `utils/llm/router.py`'s
  `TIER_MODELS[Tier.CLOUD_SMALL]`), and the spec never states which literal
  string `model: haiku` should resolve to, though Acceptance Criteria requires
  testing exactly that resolution.
  **Resolved:** scope item 1 now pins the literal (`claude-haiku-4-5-20251001`)
  and notes the pre-existing repo-wide divergence as a separate concern.
- advisory — `blocked-by: <slug>` behavior on a typo'd/nonexistent slug is
  unstated: since a made-up branch name makes `branch_exists` return `False`,
  a typo would silently produce "not blocked" (fail-open) rather than
  surfacing the mistake.
  **Resolved:** pushed into `ready`'s own rubric ("Dangling `blocked-by`") —
  it now checks the named slug actually resolves to a proposal file, rather
  than adding runtime validation machinery for a mistake `ready` should catch
  before the proposal ever reaches the fixer.
- advisory — Acceptance criterion for item 3 (delegation) only checks that
  `_compose_prompt`'s output text contains a delegation paragraph; it can't
  (and by its own "prompt-text change only" scoping doesn't try to) verify
  that a host `claude -p --dangerously-skip-permissions` build actually
  invokes the named subagents via the Agent tool, or that doing so is
  materially cheaper.
  **Accepted, not fixed:** whether a prompt instruction is obeyed isn't
  deterministically testable short of an empirical A/B run, which is out of
  this proposal's scope (see "Explicitly NOT in scope") — the criterion
  stays textual; real delegation behavior is an observability question for
  later, not a blocker on this spec.
- advisory / split signal — the proposal bundles three independently
  testable/shippable deliverables (per-proposal model tiering, `blocked-by`
  sequencing, builder-delegation prompt text) plus a doc-convention item,
  under one build. No real dependency links them (each has its own target
  file and acceptance criterion); ADR 0048's proposal substrate asks for a
  bounded single deliverable per spec.
  **Left open, your call:** now that blocker 1 no longer concentrates all the
  risk in item 2, splitting is lower-value than it was — but it's a real
  option (item 2 could ship on its own timeline from 1/3/4) if you'd rather
  land the uncontroversial pieces sooner. Not done here since it restructures
  the file; say the word and I will.
