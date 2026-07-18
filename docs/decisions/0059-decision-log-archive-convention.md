# ADR 0059 — Decision-log archive convention ("Rest in Git")

- **Status**: accepted (2026-07-15)
- **Deciders**: Reto + agent
- **Governs**: `docs/decisions/` (the ADR log) and its `README.md` index.
- **Backlog**: OPEN-ITEMS.md / "Architecture review / P1 — compaction and
  modularization" / "Compact ADRs with a 'Rest in Git' archive".

## Context

The ADR log has grown to ~58 files. Most stay live, but several chains are
fully superseded: their outcome is captured by a later head ADR that already
names them as predecessors (e.g. the Dockerfile / image-split chain
`0004 → 0009 → 0012 → 0019 → 0020 → 0021`, where `0020`/`0021` are the live
heads). Keeping every superseded predecessor in the top-level listing dilutes
"what is authoritative *now*" — the exact "ADR/documentation sprawl" the
2026-07-15 architecture review flagged.

Two hard constraints shape the fix:

- **Sealed ADRs.** AGENTS.md: "Don't edit a decision log entry retroactively —
  supersede with a new entry that names the predecessor." Bodies must not be
  rewritten.
- **Relative links.** Live ADRs and design docs cross-reference predecessors by
  relative path (`./0004-multi-stage-dockerfile.md`). Any relocation MUST
  update every referrer or it breaks navigation.

## Decision

Introduce `docs/decisions/archive/` and a small, mechanical convention for
retiring a fully-superseded ADR into it. The full history always stays in git
("Rest in Git"); the archive is a *discoverability* move, not a deletion.

**An ADR is archivable only when all of these hold:**

1. A **live successor ADR exists** and names it (in `Supersedes` / `Extends` /
   `Builds on`). The archive never invents new technical prose — the successor
   already carries the decision. (If a chain has *no* single live head, write
   the condensed head ADR first; that is a separate, reviewed change.)
2. Its filename is **kept unchanged** on move (`0004-…md` →
   `archive/0004-…md`), so git history follows and the number is never reused.
3. A **one-line archive banner** is prepended — the single sanctioned edit to
   an otherwise-sealed file:
   `> **Archived 2026-07-15 — superseded by [ADR 00NN](../00NN-…​.md). Kept for history (ADR 0059).**`
   The body below the banner is left byte-for-byte intact.
4. **Every referrer is updated** in the same change: the `README.md` index row,
   the `Supersession graph`, and any relative link in a live ADR or
   `docs/design/*` doc (`./0004-…` → `./archive/0004-…`; from `docs/design/`,
   `docs/decisions/0004-…` → `docs/decisions/archive/0004-…`).

**The `README.md` index stays the single source of "current authoritative
ADR".** Archived ADRs move to a dedicated "Archived (superseded)" table at the
bottom of the index, out of the by-topic table.

This ADR establishes the convention and the `archive/` directory. It does **not**
move any ADR — each chain condensation is its own reviewed change so referrer
updates are auditable one chain at a time.

### Corrections to the backlog item

While scoping this, two errors in the OPEN-ITEMS proposal were found and are
recorded here so the follow-up condensations start from correct facts:

- **Numbering.** The item proposes new ADR numbers `0064–0069`, but the ADR log
  only reaches `0059` (this ADR — `0058` was already taken by the figure-medium
  ADR; migration files reach `0065`; the two were conflated). Condensed head
  ADRs take the *next free ADR number* (`0060+`) — not `0064`.
- **Chain grouping.** `0019-second-greenfield` is **not** part of the
  image/embedder chain; it is a migration-baseline decision superseded by
  `0031` (baseline-snapshot dual-track). It belongs to the migration chain, not
  `image/embedder split`.

## Alternatives considered

- **Leave a redirect stub at the original path instead of moving + updating
  referrers.** Rejected: stubs re-clutter the top level (the sprawl this
  fights) and drift from the index. One index, updated links, is cleaner.
- **Delete superseded ADRs (rely purely on git).** Rejected: a superseded ADR is
  still the best explanation of *why* a reversed decision was made; `archive/`
  keeps it one click away, not one `git log` away.
- **Do nothing / keep all ADRs top-level.** Rejected: the by-topic table is
  already hard to skim; the review flagged it explicitly.

## Consequences

- **Positive**: the top-level ADR listing shrinks to live decisions; superseded
  chains stay discoverable under `archive/`; the move recipe is mechanical and
  auditable per chain.
- **Negative**: each condensation touches several files (banner + link updates);
  must be done carefully so no relative link dangles. A link-check over
  `docs/` is recommended after each chain move.
- **Neutral**: no code, schema, or API impact — documentation only.

## See also

- `docs/decisions/README.md` — the ADR index (current authoritative + archived).
- `docs/decisions/archive/README.md` — what lives in the archive and why.
- AGENTS.md §"On-demand pointers" / "Decisions (ADR log)".
