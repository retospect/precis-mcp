# LLM-facing prose — repo-dev docs

**Audience**: anyone writing/editing docs an agent reads to *develop this
repo* — `CLAUDE.md`, `docs/codebase.md`, `state-map.md`, `glossary.md`,
`AGENTS.md`, `docs/design/`, `docs/decisions/`, and files under
`.claude/{agents,skills,commands}/`.

**These arch/design docs are LLM-guidance-first, human-second.** Written to
be *acted on* by an agent, not read for narrative. So: no filler, no
executive summary, no motivational preamble — the internals are the payload.

**Sibling, opposite cut-list**: `docs/design/skill-authoring-style.md`
governs the **product** surface (`src/precis/data/skills/`) — docs read by a
cluster agent that can only call the seven verbs. This is a deliberate
**fork**, not a shared kernel: the tone rules match, but the cut-list
**inverts**. There, internal names are noise (cut them). Here, **internals
are the payload** — name `chunks`, worker names, ADR numbers, migration
files precisely, with a pointer to the code. Don't apply the product
cut-list to a dev doc; it deletes the point.

## The rule

> **Write for the LLM. Terse, precise, no warm-up.**
> **Name the invariant + point to the code; don't restate the code.**

- One sentence per concept. Two is suspicious. Three needs cutting.
- No transitions, no soft framing, no reassurance ("by design", "note that").
- Tables and inline `# comments` beat prose paragraphs.
- If a pointer carries the meaning, drop the prose and just point.
- **Don't justify tool/design choices.** The choice is made — state what the
  thing *does*, not why it beat the alternative. Naming a rejected or legacy
  tool ("plain git, not `git town`"; "instead of the old X") just burns
  tokens; cut it. Rationale worth keeping lives once, in an ADR.

## Point, don't copy

Every fact lives at exactly **one** altitude; elsewhere you link to it.
Duplication is N rot sites for one fact.

| Altitude | File | Holds |
|---|---|---|
| **Router** | `CLAUDE.md` | what-before-first-tool-call + where everything is |
| **Orientation** | `docs/codebase.md` | invariants, lifecycle, subsystem map, seams |
| **Reference** | `state-map.md`, product skills | present-state per subsystem |
| **Rationale** | `docs/decisions/` (ADRs) | why a decision, what was rejected |
| **Vocabulary** | `glossary.md` | coined term → best entry-point file |

Put a fact in the wrong altitude and it rots: status in the orientation doc,
rationale in the router, etc.

## Present-tense, invariant-biased

Describe what **survives a refactor** (seams, contracts, the append-only
chunk rule) — not current status (belongs in `state-map.md`) and not the
dated story (that's `git log`; there is no CHANGELOG). A doc pinned to
invariants rots slowly; one pinned to status rots on the next commit.

## Freshness contract

- **Update in the same commit** that changes what the doc describes.
- Orientation/reference docs carry a `_Verified @ <sha>._` stamp; bump it
  when you re-verify against the tip.
- `/endsession` and `/go` re-check touched-subsystem docs before shipping;
  the `map-staleness-reminder` hook nudges on the paths that usually drift.
- **Terse is a freshness mechanism**: a short doc gets re-read and
  re-verified; a long one rots unread. Cut before you add.
