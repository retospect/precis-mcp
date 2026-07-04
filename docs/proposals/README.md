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
- `TEMPLATE.md` and this `README.md` are never treated as proposals.

Start from [`TEMPLATE.md`](./TEMPLATE.md).
