---
status: ready
title: skill graph — tags/kinds axes, wikilink edges, serve ledger, graph surfacing
prio: high
---

# skill graph — tags/kinds axes, wikilink edges, serve ledger, graph surfacing

Spec finalized with the user in conversation 2026-09-13 (skip `ready`
review per flow §3). Four slices; this file tracks all of them and is
deleted when the last one ships.

## Motivation / why

Skills have an index (`toc`), RAG (`FileCorpusIndex`), and a hand-
maintained tree (`_SKILL_CATEGORIES` in `skill.py`) — but the lateral
layer ("that is another skill you should know about") is prose-only:
86/160 files carry `## See also` sections and 24 use `[[slug]]`
wikilinks, none parsed, validated, or surfaced in search. Dangling
targets rot silently; a search hit never shows its neighbors. Kind
applicability is inferred by regex from free-text `applies-to:`
(`skill.py` `_availability_gap` feeder, near line 2416).

## In scope

### Slice 1 — graph machinery + gates + surfacing (code only)

- **Frontmatter axes.**
  - `tags:` list — checked-in vocabulary tuple next to `VALID_FLAVORS`
    in `_skill_common.py`. Seed: `orientation, verbs, addressing,
    drafting, workflow, external-sources, troubleshooting, design`.
    A tag equal to a registered kind name is REJECTED (kinds are their
    own axis). Singleton-tag lint = warning.
  - `kinds:` list — kinds the skill's recipes operate on. Validated
    against the kind registry. Short codes derived from
    `utils/handle_registry.py`, never authored. Drives availability
    gating (replaces the `applies-to:` regex inference); `applies-to:`
    stays as human prose.
- **Edge extraction.** `[[slug]]` wikilinks parsed from bodies in
  `ingest/skill_ingest.py` scan stage; dangling link / unknown tag /
  empty `kinds:` = static gate failure (same pattern as
  `invokes_personas`). Gates ship in WARN mode; flipped to hard-fail at
  the end of slice 2.
- **Graph build.** Symmetrized link graph (outbound + inbound), tag
  groups, kind groups — built alongside `FileCorpusIndex`, exposed as a
  queryable API at the server seam (slice 4 consumes it from outside
  `skill.py`).
- **Surfacing.**
  - Four-line footer on served skills via `render_next_section`:
    linked (out + in, inbound CAPPED with a search pointer for hub
    skills), tags (→ tag-filtered toc), kinds (`paper (pa)` → kind-
    filtered toc), tree siblings.
  - `related:` line on top search hits.
  - `tag=` / `kind=` filters on the toc — HONORED, never swallowed.
  - Footer is serve-time assembly, NEVER embedded into chunks (no
    `CHUNKER_VERSION` bump, no embedding pollution, no chunk-budget
    interaction).
  - `[[x]]` render-expands to `get(kind='skill', id='x')` in full and
    `~N` section serves; the convention is documented once in
    `precis-addressing-help` (covers raw `[[x]]` in search snippets).
- **Serve ledger (tier-2 state).** Session-keyed
  `slug → (file_sha256, when-served)` at the server seam — keyed off
  the MCP session (FastMCP per-request context), NOT module globals
  (the HTTP surface would leak hints across agents). Uses:
  - "(read this session)" annotations on footer / search related lines;
  - soft-dedup stub on a repeat get of an unchanged skill: section toc
    + one line "unchanged since you read it this session —
    `get(kind='skill', id='<slug>', full=true)` to resend". `full=true`
    overrides; sha change mid-session serves full automatically.
  - Hard rules: never gates a write or correctness decision; every stub
    carries its own re-fetch line; loss (restart) degrades to full
    serves. Hint always, suppress never.
- **DB-side parity.** `skill_ingest` emits `KIND:` + topic tags on the
  ref alongside the existing `FLAVOR:` tags.
- **Tests.** Gate unit tests, graph symmetry, ledger stub/override,
  round-trip teaching-surface pins updated for the new footer.

### Slice 2 — 160-file sweep + drift prevention

- PRE-FLIGHT: diff the stranded ↑1 commit in worktree
  `agent-a0a2c671962db3aff` ("docs(skills): 2026-09-04 audit doc-drift
  sweep, gr311347") — salvage or discard deliberately before rewriting
  the same files.
- Documenter batches of ~20, ONE touch per file: populate `kinds:` +
  `tags:`, rewrite `## See also` → `[[slug]]` wikilinks, drift-check
  recipes against the current runtime, flag suspect/executable examples
  into a ledger (feeds the slice-3 decision).
- Personas get frontmatter for gate uniformity; stay OUT of toc/footer
  surfacing (`invokes_personas` already covers their edge type).
- Scaffold skill template + skill-authoring convention doc updated with
  the new axes. Gates flipped WARN → hard-fail.

### Slice 3 — recipe pinning (user-gated; approved as "flag during
sweep, pin selectively")

From the slice-2 flag ledger, pin high-traffic executable examples as
round-trip tests. Write-path examples run against the dev DB
(`scripts/dev`), never the session MCP.

### Slice 4 — graph consumers outside the skill handler

Kind-help + error-path injection: a failed `put(kind='se')` or a
`get(kind='se')` help read lists that kind's skills via the graph API,
through the teaching-surface hooks (680d cf. commit 680c8d4f).

## Explicitly NOT in scope

- `verbs:` axis — deferred; reconsidered in slice 4 only if kind-
  filtered injection proves too coarse.
- Directed edge types (`builds-on:` / prerequisite ordering) — lateral
  see-also + tags only; `invokes_personas` remains the sole directed
  edge.
- Executing every skill's recipes as tests (slice 3 pins selectively,
  not exhaustively).
- Generalizing the serve ledger beyond skills (design permits it; not
  built now).
- Moving skill authority into the DB — markdown files stay canonical,
  the graph is derived at ingest.

## Acceptance criteria

- A dangling `[[slug]]`, unknown tag, kind-named tag, or empty `kinds:`
  in any shipped skill file reddens the gate (hard-fail after slice 2).
- `get(kind='skill', id='<slug>')` renders the four-line footer; a hub
  skill's inbound line is capped.
- Search hits carry `related:`; toc honors `tag=` and `kind=` filters
  (filtered result set actually filtered — verified by test).
- Availability gating driven by `kinds:` frontmatter, not `applies-to`
  regex; hidden-skill behavior unchanged for unwired kinds.
- Repeat get of an unchanged skill in one session returns the stub with
  a working `full=true` override; different sessions never share ledger
  state (test covers two concurrent sessions).
- All 160 files pass the hard gate; `## See also` sections use wikilink
  syntax; sweep flag-ledger exists for slice 3.
- Round-trip teaching-surface tests green.

## Target + blast radius

`handlers/skill.py`, `handlers/_skill_common.py`,
`ingest/skill_ingest.py`, `skill_index/` (graph build; NOT chunker
output), `server.py` (session-keyed ledger seam), all of
`data/skills/*.md`, scaffold template, `precis-addressing-help`.
Post-deploy: session MCP restart required before verifying (stale-
server trap); verify against deployed CLI/web, not the session MCP.

## Open questions / decisions log

- 2026-09-13 all design decisions above finalized with user; approved
  "good to go". Execute-testing resolved: flag during sweep, pin
  selectively (slice 3).
- 2026-09-13 slice-2 pre-flight DONE: stranded audit commit 052545ad
  (worktree agent-a0a2c671962db3aff) verified superseded — 8/10 files
  re-covered by main's a50cbdee (same gripe batch gr311325-47, later
  same day), precis-markdown-help identical on main, and the sole
  precis-stubs-help delta is main being newer (tags-only paper search
  shipped in 45750371, contradicting the stranded text). Nothing to
  salvage; tree is reap-eligible via the user's reap loop.
