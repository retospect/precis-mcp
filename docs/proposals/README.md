# Proposals — the fixer's intake (ADR 0048)

A **proposal** is a transient, ADR-shaped spec for a repo-dev change.
It is the human's desk in the autonomous loop: you write one, run
`/ready` in tandem until both keys turn, mark it `status: ready`, and
the laptop fixer picks it up.

## Lifecycle — graduate or die

1. **Draft** (`status: draft`) — you're writing / arguing it. The fixer
   ignores it.
2. **Ready** (`status: ready`) — `/ready` passed; the fixer will pick it,
   build it on `fix/<slug>`, gate it, and (per autonomy) ship it.
3. **Graduated** — on ship, the proposal **becomes an ADR** (the durable
   "why") *or* is **deleted** (the commit + git history preserve it).
   A proposal never lingers here after it ships — a stale proposal is a
   `ready`-gate input that no longer matches reality.

## Conventions

- One file per proposal: `docs/proposals/<slug>.md`.
- Front-matter must carry `status:` (`draft` | `ready`). Optional
  `title:`; otherwise the first `#` heading is the title.
- Branch is `fix/<slug>` (derived from the filename). The fixer skips a
  proposal whose branch already exists (idempotent pick).
- Optional `model:` (`sonnet` | `opus` | `haiku`) pins the build tier;
  unset falls back to the fixer's default (`claude-sonnet-5`).
- Optional `blocked-by: <slug>` declares a real predecessor: the fixer
  won't pick this proposal while `<slug>`'s branch still exists (i.e.
  until it ships and its local branch is cleaned up).
- `TEMPLATE.md` and this `README.md` are never treated as proposals.

## Should this be more than one proposal?

There's no automated split judge yet, so this is a heuristic for the
human authoring the spec (or a manually-spawned `ready` agent) to apply:

- **Split** when the proposal names genuinely independent
  deliverables — each separately testable and shippable on its own —
  rather than one deliverable examined from several angles. Use
  `blocked-by` to declare real ordering between the resulting specs.
- **Don't split** mechanical, single-shape work (e.g. the same
  refactor applied uniformly across files) — that's one deliverable,
  not several.

Start from [`TEMPLATE.md`](./TEMPLATE.md).
