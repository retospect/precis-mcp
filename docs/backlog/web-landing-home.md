---
status: draft
title: Web landing page as a state router (Home), with a request log underneath
pillar: platform
---

# Web landing page as a state router (Home), with a request log underneath

## Resume

- **Next:** Reto's three decisions (see decisions log), then step 1 below.
- **Mockup:** Figma file `wAfi74X5svrmVPSPCjjg6s`
  (https://www.figma.com/design/wAfi74X5svrmVPSPCjjg6s). Root frame
  "Home — proposed" `3:4`; Notes panel `3:261`; System-purpose panel
  `6:129`; "Since your last visit" block `12:129`; components holder
  `2:28` (Kind badge `2:29`, Item row `2:31`, Continue card `2:39`,
  Attention card `2:46`); Tailwind colour variables
  `VariableCollectionId:2:2`.
- **Worktree:** `figma` (was `unified-floating-cocke`, 2026-10-07; no
  template code changed, tree clean).
- **Builds:** request log 1 build; Home page 2 builds; general chat 2–3
  builds (unknown until the write-guardrail decision).

## Motivation / why

`/` redirects to `/drive` (`precis_web/app.py`), a search-and-browse page.
The mission (`docs/mission.md`) says the person is the scarce part of the
loop, so the landing page's job is to answer, in order: what is blocked on
you; what the agents did since your last visit; what you were in the middle
of; then find. Today "blocked on you" lives behind the Needs-you badge in
the nav, there is no "since your last visit" view at all, and the Drive
filter/kind controls sit in a collapsed `<details>`. Reto endorsed the
night-shift block ("Since your last visit") and the "Continue your thing"
block explicitly (2026-10-07).

Nothing about current usage is measured: there is no request log, so the
redesign is a reading of the templates, not of behaviour.

## In scope

Three sequenced slices, each independently shippable:

1. **Request log** (`/go` — adds a migration). Table + middleware in
   `precis_web/app.py` next to the existing GZip/BasicAuth/SecurityHeaders
   middleware: route pattern, `web_users.id`, UTC timestamp, previous route,
   `/drive` facet usage (scope, kind chips, sort). No query text, no titles.
   Gives (a) a two-week baseline before/after the redesign and (b) the
   per-user last-visit timestamp the night-shift block needs.
2. **Home page** per the mockup: Needs-you cards (asks, proposed hypotheses,
   triage — sourced as `needs_you/index.html.j2` does) → "Since your last
   visit" (jobs finished, drafts/sections changed, papers ingested, since
   the request log's last visit) → Continue (recently touched items) →
   Find (scope Mine/Sources/Machine/Everything as the primary control, kind
   chips, one-line rows with a `•••` menu) → Recent → slim folder rail.
   Reuses the Drive picker contract described in `precis_web/__init__.py`.
3. **General chat** on every page: the nav search box accepts a question
   or instruction, backed by a generalised `precis_web/ask.py`
   (`call_claude_agent` + precis MCP, `conv` thread history), turn-as-job
   with polling, read-only tools first, writes behind a per-turn confirm.

## Explicitly NOT in scope

- Restyling `/drive` itself (decision a, below, may change this).
- Figma annotations (`node.annotations` is not permitted on this file;
  purpose notes live in panel `6:129`).
- Streaming chat responses; the first chat slice is blocking + poll.
- Any logging of search text or item titles.

## Acceptance criteria

- Slice 1: every authenticated page view inserts one row; a query for
  "last visit before now, per user" returns in <10 ms; no row contains a
  query string or title; `tests/` cover the middleware with auth on and off.
- Slice 2: `/` renders Home; Needs-you count on Home equals the nav badge;
  the night-shift block lists only events after the user's previous visit
  and says "first visit" when none; the Find block reproduces the Drive
  facets via URL params so `/drive?…` links keep working.
- Slice 3: a question typed in the nav box returns an answer with precis
  citations; a write-capable turn shows the proposed writes and does
  nothing until confirmed.

## Target + blast radius

`precis_web/app.py` (middleware, `/` route), new `precis_web/home.py` +
`templates/home/`, `templates/base.html.j2` (nav search → chat), `ask.py`,
one migration under `src/precis/migrations/`. Workers untouched. Related
drive items: `drive-filter-hierarchy.md` (Return = personal viewing
history — the same log feeds it), `drive-presenter-completeness.md`.

## Open questions / decisions log

- (a) New **Home** page next to Drive, or restyle `/drive` in place?
  Recommendation: new Home; Drive stays the browse page.
- (b) Instrument first (request log, two-week baseline) or redesign first?
  Recommendation: request log first — it is also the night-shift block's
  data source.
- (c) Build the general chat, and with which write guardrails? Does the
  Discord bridge (`asa_bot`) already cover the use case?
- Open mockup gaps, not yet redrawn: Needs you should sit above Find;
  alerts and gripes are nav-only and belong in Needs you.
- No before/after capture exists: `generate_figma_design` needs a running
  local stack (prod is behind auth).
