# Automations index — make recurring agent tasks discoverable

> **Status: shipped (v8.23.0).** All three slices landed: the `automation`
> tag convention, the `cron` `/automations` list view, the Status web panel,
> and the `precis-automations` skill. Additive + default-safe (no migration).

## Motivation

The morning/evening podcasts (and the news briefing before them) are not
produced by any dedicated Python producer. They are **`cron` refs that drive
Asa**: a recurring cron fires `pg_notify('precis.cron')` at `next_fire_at`,
`asa_bot._deliver_cron_prompt` synthesises a user turn from the cron's `text=`
payload, drives Claude against it, and Asa composes + publishes the cast (see
`src/precis/handlers/cron.py`, `src/asa_bot/bot.py:351`).

This is the right architecture — but it is **undiscoverable**. A cron is "a
scheduled prompt"; nothing distinguishes a standing automation (the thing that
runs *me* every morning) from a one-shot reminder, and nothing ties a cron to
the artifacts it produces. Reto's question — *"we need an index for these sorts
of things, or a skill to find them?"* — is the trigger.

Concretely, three gaps:

1. **No label.** `get(kind='cron', id='/upcoming')` lists every scheduled cron
   flat; a daily-podcast cron and a "ping me in 10m" reminder look identical.
2. **No link to output.** From the podcast cron you cannot reach the episodes
   it produced; from an episode you cannot reach the editable prompt.
3. **No overview.** The Status web tab has liveness for *news briefing* but no
   panel that answers "what recurring agent behaviours are configured, when did
   each last fire, and what did it produce?"

## Non-goals

- **No new kind.** The `cron` kind *is* the registry; we make it legible.
- **No scheduler change.** Firing, catch-up, recurrence are untouched.
- **No paper-classifier axis.** The `data/axes/*.yaml` machinery is paper
  auto-tagging; it is not the mechanism for marking a cron.
- Not building the morning-brief producer here (separate work; this unblocks
  *finding + editing* the cron whose payload already drives it).

## Design

### 1. Mark a standing automation — the `automation` tag

Use a curated **open tag** `automation`, not a new closed axis (no migration,
no classifier coupling). Mechanism note: `Tag.parse_strict('automation')`
resolves a bare word to the OPEN namespace — there is no flag registry wired
into `parse_strict`, so bare words are open tags unless they collide with a
reserved closed value (`automation` doesn't). This is a *documented,
skill-taught* convention tag, not folksonomy drift, so it is the curated kind
of open tag ADR 0047 leaves alone.

- A cron tagged `automation` is a *standing recurring behaviour* (as opposed to
  a one-shot reminder or ad-hoc scheduled prompt).
- Sub-typing is a second open tag, free-form, e.g. `cast-morning`,
  `cast-evening`, `briefing`. `automation` answers "is this an automation?";
  the subtype answers "which one?". Kept open — the set of automations is
  small and human-curated, not a validated vocabulary.

Query surface (already supported — open tags are filterable):

```python
search(kind='cron', tags=['automation'])              # all standing automations
search(kind='cron', tags=['automation', 'cast-morning'])
```

`cron` has no restricted-axis gate, so `add=['automation', 'cast-morning']`
validates with **no schema or validation change**. The `/automations` list
view filters via `list_refs(tags=['automation'])`, whose `build_tag_filter`
expansion matches the tag in either the OPEN or FLAG namespace — so the view
is namespace-agnostic and keeps working even if `automation` is later
registered as a true flag.

### 2. Link a cron to what it produces — reuse `derived-into`

A cron that produces a draft/episode links to it with the existing
**`derived-into`** relation (inverse `derived-from`), both already in the
`Relation` literal and the `relations` seed — **no migration**:

```python
link(kind='cron', id=42, target='draft:cast-reading-2026-07-16',
     rel='derived-into')
# surfaced from the draft end too, via the auto-mirrored derived-from
```

Rationale for reuse over a new `produces`/`produced-by` pair: `derived-into`
already means "this ref generated that one" and is bidirectionally queryable;
adding a near-synonym relation costs a migration + literal edit + a sealed ADR
for no semantic gain. If a future need distinguishes "derived" from "produced
by a run", add it then. **(Open question A below.)**

Because Asa composes the cast on each fire, the cron→draft link should be added
**by Asa in the cron payload's instructions** (one `link(...)` call after it
publishes). The skill (below) documents this so the pattern is followed
consistently; we do not auto-create the link in the handler (the handler never
sees the produced artifact).

### 3. `cron` `/automations` list view (MCP surface)

Add `automations` to `CronHandler._supported_list_views` alongside
`recent`/`upcoming`. `get(kind='cron', id='/automations')` renders the crons
carrying the `automation` flag, ordered by `next_fire_at`, each row showing:
subtype (open tags), recurrence, next/last fire, `fire_count`, and status.

Implementation mirrors `_render_upcoming`: `list_refs(kind='cron')`, filter to
those with the `automation` flag (read tags), sort by `next_fire_at`. Small,
self-contained, unit-testable against the `store` fixture.

### 4. Status web panel — "Automations"

Add a section to the Status tab (`src/precis_web/routes/status.py` +
`templates/status/…`) listing automation crons with: label (subtype),
recurrence, last fire (`meta.last_fired_at` / `fire_count`), next fire, and —
via the `derived-into` links — the most recent produced artifact (title +
link). Defensive `_safe`-wrapped section like every other, so a schema surprise
degrades to an empty panel.

This reads the **same DB the web app points at**. Note the discoverability of
*production* crons still requires the web app (or MCP) to point at the prod DB
— this workspace's sandbox DB has no crons. The panel makes them legible
*where they live*; it cannot conjure crons from a DB that has none.

### 5. Skill — `precis-automations`

A short agent-facing skill documenting the whole loop:

- Recurring agent behaviours live as `cron` refs tagged `automation`.
- Find them: `get(kind='cron', id='/automations')` /
  `search(kind='cron', tags=['automation'])`.
- Each automation's **prompt is its cron payload** — edit behaviour by editing
  the payload (delete + re-put, since cron `put` rejects `id=`; or pause via
  `STATUS:paused`).
- After producing an artifact, `link(rel='derived-into')` it back to the cron.
- Cross-link to `precis-cron-help` (mechanism) and `precis-voice` (cast craft).

Also add a "Standing automations" pointer to `precis-cron-help` so the two
skills reference each other.

## What this unblocks (the original request)

Once the podcast cron is findable and its payload is understood as the editable
prompt, Reto's actual asks become **payload edits** (not code):

- *Longer / more detail* — the payload's length + depth instructions.
- *Less meditative, dial back encouragement* — tone instructions.
- *Japanese vocab-drill with A/B call-and-response × 2–3 turns* — a drill-markup
  convention documented in `precis-voice` (and the payload references it). The
  "unknown Japanese character" bug is a **narration/markup** issue handled
  separately in the narrate layer / lexicon; noted here as a linked follow-up,
  not part of this slice.

## Slice plan

1. `automation` tag convention + `precis-automations` skill + `precis-cron-help`
   pointer. *(Docs + convention; zero code risk.)*
2. `cron` `/automations` list view + unit test.
3. Status web "Automations" panel + route test.

Each slice is independently shippable and additive.

## Open questions (for Reto)

- **A. Reuse `derived-into` vs add `produces`/`produced-by`?** Proposal reuses
  `derived-into` (no migration). *(Chosen: reuse `derived-into`.)*
- **B. Open tag `automation` vs a closed `AUTOMATION:` axis?** Proposal uses a
  curated open tag (no migration, no classifier coupling; bare words resolve to
  OPEN today). A closed axis would give validated subtypes but needs a
  migration + gate. *(Chosen: open tag.)*
- **C. Should the handler auto-detect "is recurring" as a weak automation
  signal**, so even un-tagged recurring crons show in `/automations`? Proposal:
  no — recurrence ≠ automation (a weekly reminder recurs but isn't a standing
  agent behaviour). The explicit tag is the truth. *(Chosen: explicit tag
  only.)*
