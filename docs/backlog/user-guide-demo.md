---
status: draft
title: User-guide demo — in-app tour + annotated screenshots + narrated guide on GitHub Pages + YouTube
prio: normal
---

# User-guide demo — in-app tour + annotated screenshots + narrated guide on GitHub Pages + YouTube

## Motivation / why

Someone who knows nothing about precis has no way to see, in two minutes,
what the thing is. The repo README explains the MCP surface for agents;
the in-app manual (`src/precis_web/manual/`) explains workflows to someone
already logged in; nothing shows the web UI to an outsider. The deliverable
is a guided demo: annotated screenshots of the real UI with callouts
("arrow → box → here you can do xyz"), a synthesized voice narrating a
marketing-toned walkthrough, a left nav to jump between tools — published
where a stranger lands (GitHub), later converted to a YouTube video. The
tour is also built into the site itself, so real users get the same
callouts in-app and the published screenshots can never drift from what
the app shows.

Decisions already made with the user: captures come from **prod, curated
pages, human-reviewed before publish**; the tour is **built into
precis_web**, not screenshot-only.

## In scope

**One source of truth: per-section tour manifests.** New
`src/precis_web/manual/tour/<nn>-<slug>.json`, one per guide section:
`{title, route, steps: [{selector, heading, text, placement}]}`. Section
order/slug from filename, like manual chapters. Everything else renders
from these.

1. **In-app tour overlay.** `src/precis_web/static/tour.js` + one
   injection in `templates/base.html.j2` before `</body>` (the confirmed
   single shared base — reaches every page). `?tour=<slug>` (and
   `&step=N` for deep links) activates: highlight box on the step's
   selector, arrowed callout card, next/prev/close. Read-only; no
   mutations. Manifests served via the existing manual route module
   (`routes/manual.py` grows a `/manual/tour/<slug>.json` endpoint).
   A "take the tour" link on `/manual` chapter pages.
2. **Manual chapter 00 — the underlying model.** New
   `src/precis_web/manual/00-what-is-precis.md`: the concept model no
   user-facing doc currently states (papers → chunks → embeddings →
   claims/hubs → drafts → quests/todos; kinds; the seven verbs; the
   worker loop that works between your visits; the LLM ladder — small
   local models routed up to Claude). Doubles as the guide's opening
   section and the narration's spine for "the underlying model".
3. **Capture pipeline.** `scripts/guide-capture`: Playwright official
   container on the compose network (proven recipe — see memory
   `local-web-demo-recipe`: host Playwright can't launch on macOS),
   driving a local `precis web` (GET-only navigation) on the prod
   tunnel DSN, one **clean** full-page PNG per tour step per section →
   `guide/assets/`. Avoids identity/infra pages entirely (`/account`,
   `/secrets`, `/settings`, `/env`, `/console`, `/status?tab=services`).
   **Human review checkpoint: every frame is eyeballed before any
   publish/commit** — nav badges and sidebars can carry private titles.
4. **Annotation renderer.** `scripts/guide-annotate`: from each clean
   PNG + its manifest, emit (a) a static annotated PNG (callouts burned
   in — for the video and fallback) and (b) an **animated SVG** (PNG
   embedded as image layer, callout boxes/arrows fading in sequence via
   SMIL/CSS — GitHub README renders these natively, no JS). Pure-python
   SVG templating; no new heavy dependency.
5. **Narration.** `guide/narration/<nn>-<slug>.md`, marketing register
   (source prose: `docs/mission.md`; craft rules:
   `src/precis/data/skills/precis-voice.md`; numbers through
   `precis.draft.verbalize`). Rendered to per-section mp3 via the
   existing stack: `markdown_segments()` → `render_episode()`
   (`src/precis/draft/narrate.py`, `src/precis/tts/render.py`, container
   backend `docker/tts/`). One voice throughout (default `bm_george`,
   the existing brief voice).
6. **Guide site + Pages.** `guide/` at repo top level: a small
   `guide/build.py` assembles `index.html` — fixed left nav of sections,
   each section = animated SVG (or step-through player) + audio element
   + transcript — AND a committed, GitHub-flavored `guide/README.md`
   (`build_markdown`, same sections/assets input, no audio) so the guide
   renders **in the repo itself** with no Pages dependency; this is the
   primary in-repo artifact, `index.html`/Pages is the narrated (audio)
   counterpart. New `.github/workflows/pages.yml` publishes `guide/`.
   README gets a teaser: one animated SVG inline + links to `guide/README.md`
   and (later) the YouTube video. `docs/README.md` "where truth lives"
   table gains one row for `guide/` (marketing/demo surface — neither dev
   docs nor in-app manual).
7. **Video for YouTube.** `scripts/guide-video`: ffmpeg — per section,
   annotated step PNGs timed across that section's mp3, concat, one
   `guide.mp4` (1080p). Upload is manual (user's account); the workflow
   just produces the file.
8. **Bug intake.** Any defect found while touring/capturing is filed as
   a gripe (`put(kind='gripe')`) as encountered, not batched.

**Guide sections (the left nav — full-system walk):**
00 What is precis · 01 Drive (search + create) · 02 Writing a paper
(`/smartdraft/{id}` — fisheye TOC, pen/eye steering, request box, live
citations) · 03 Reading papers (`/papers/{id}` two-pane reader) ·
04 Claims & nanopubs (`/nanopub`, `/claim/{head}`) · 05 Figures &
diagrams (`/figure`, `/mermaid`) · 06 3D (`/structure`, `/cad`) ·
07 The loop (`/todo`, quest dashboard, `/status` health) · 08 Attention
(`/needs-you`, `/gripes`, `/alerts` — "and this is where you file bugs").

**Ship slices** (independently landable, in order):
- **Slice 1** — tour manifests + `tour.js` + base injection + chapter 00
  + `/manual/tour/…` endpoint + tests (manifest schema, endpoint, tour
  param renders overlay).
- **Slice 2** — `scripts/guide-capture` + `scripts/guide-annotate`;
  curated captures; **user reviews frames** before they're committed.
- **Slice 3** — narration scripts + TTS render + `guide/build.py` +
  Pages workflow + README teaser. (User enables Pages in repo settings.)
- **Slice 4** — `scripts/guide-video` → `guide.mp4`; user uploads to
  YouTube; README/guide link the video.

## Explicitly NOT in scope

- No screen-recorded live interaction video (slideshow + narration only,
  v1). No per-user tour state ("seen it" cookies), no tour authoring UI.
- No staged demo DB; no redaction tooling beyond page curation + human
  review. No YouTube API upload automation.
- The tour does not cover ops/identity pages, and mutation affordances
  are pointed at, never exercised, during capture.
- No mkdocs/docs-site migration — `guide/` is a single generated page,
  not a documentation system.

## Acceptance criteria

- `?tour=writing-a-paper` on `/smartdraft/{id}` shows the step-1 callout
  in-app; next/prev walk all steps; `PRECIS_WEB_AUTH` untouched; a page
  without a manifest is unaffected (no JS errors, no fetch spam).
- Every manifest selector resolves on its live page at capture time
  (capture script fails loudly on a missing selector — that's UI drift).
- `scripts/guide-capture && scripts/guide-annotate && guide/build.py`
  reproduces the site from a clean tree; animated SVG plays inline on
  the rendered GitHub README.
- Per-section mp3s exist, narration mentions the underlying model
  (chapter 00 content) and total runtime is 4–8 minutes; `guide.mp4`
  plays with synced audio/frames.
- Pages URL serves the guide with working left nav and audio; README
  links to it. Zero private data in any published frame (user sign-off
  recorded in the PR/ship message).
- Bugs found during the walk exist as gripes.

## Target + blast radius

- `src/precis_web/templates/base.html.j2` (one injection),
  `src/precis_web/static/tour.js` (new), `src/precis_web/routes/manual.py`
  (+tour endpoint), `src/precis_web/manual/` (+chapter 00, +tour/),
  `tests/precis_web/test_manual.py` neighbors.
- New: `scripts/guide-capture`, `scripts/guide-annotate`,
  `scripts/guide-video`, `guide/`, `.github/workflows/pages.yml`.
- README.md, `docs/README.md` (one table row).
- No DB schema, no worker, no MCP surface changes. Capture path is
  GET-only against prod data via the local-web recipe.

## Open questions / decisions log

- **Read-only DB role for the capture web process?** DECIDED (slice 2):
  no separate role — `scripts/guide-web` sets `PGOPTIONS="-c
  default_transaction_read_only=on"` before launching `precis web`, for
  both `--db prod` and `--db test`. `PGOPTIONS` is a standard libpq
  environment variable honored by every libpq-linked client (incl.
  `psycopg[binary,pool]`, this repo's driver), applied at connection time
  as `-c` GUC settings — same mechanism as `psql -c`. Chosen over
  splicing `options=` into the DSN string because the stored prod tunnel
  DSN may already carry its own `options=` (pgbouncer tuning) that a
  naive string append would clobber; `PGOPTIONS` composes independently.
  AMENDED (09-04, hit live): pgbouncer (`pool_mode=transaction`) rejects
  `options` startup params ("unsupported startup parameter in options"),
  and per-connection `SET` is unsafe under transaction pooling — so
  `--db prod` swaps the DSN user to `agent_ro` instead (grants-based
  read-only role, SELECT on all app tables, password already in pgpass):
  server-side per-statement enforcement, pooling-mode-proof. `PGOPTIONS`
  remains the mechanism for `--db test` (direct postgres honors it).
- **Voice**: default `bm_george`; user may prefer another of the 54
  (`src/precis/tts/voices.py`). Cheap to re-render — not a blocker.
- **Pages enablement** is a repo-settings toggle only the user can flip;
  slice 3 lands the workflow regardless.
- **CDN note**: htmx/Alpine load from unpkg — the capture container
  needs outbound network or dropdown/overlay states won't render.
- **Repo-rendered guide (09-03)**: `guide/build.py` also emits committed
  `guide/README.md` (GFM, SVGs inline, transcripts, no audio); Pages/
  `index.html` optional. Decided user-side via sibling session; rebase
  overwrites main's placeholder `guide/README.md`.
