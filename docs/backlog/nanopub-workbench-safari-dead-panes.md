---
status: draft
title: /nanopub workbench panes still dead in Safari (works in Chrome after 61e12350)
prio: high
---

# /nanopub workbench panes still dead in Safari (works in Chrome after 61e12350)

User report 2026-08-25: after 61e12350 (pane links driven by the page's own
`loadPane()` JS — `removeAttribute('srcdoc')` + `frame.src = …` — instead of
named-target resolution), clicking claim/paper links on
https://melchior.tailded4cf.ts.net/nanopub loads the panes in **Chrome but
still does nothing in Safari**. Playwright WebKit (Linux trunk, 1300-hub
forest, Basic auth via `http_credentials`) could NOT reproduce — all click
paths worked — so the failure is specific to real Safari against the
deployed origin.

Ranked suspects (from the 2026-08-25 session's elimination work):

1. **Basic-auth credential reuse into iframe subnavigations.** Playwright
   injects credentials per-request; real Safari replays them from its
   credential cache after the top-level prompt, and has a history of
   silently suppressing the auth prompt (blank frame) for subframe 401s.
   Diagnostic: Safari Web Inspector → Network while clicking a claim row —
   if `/claim/fi…?embed=1` shows 401 (or never fires), this is it.
   Candidate fix: fetch()-then-`srcdoc`/blob render, or a same-page
   (non-iframe) claim pane, or cookie-based session auth for the web UI.
2. **Programmatic `frame.src` navigation on a `srcdoc`-initialized iframe**
   quirk in shipping Safari (trunk WebKit has the 2022 fix for
   webkit.org/b/243385; shipping Safari should too, but unverified).
   HARDENED 2026-08-25 (same ship as the pane-routing fix): panes now
   start `about:blank` with JS-injected placeholders — no `srcdoc`
   anywhere on the page. If Safari still fails after that deploy, this
   suspect is falsified; suspect 1 (auth) becomes the working theory.
3. Stale localStorage pane widths (`np-pane-pane-tree` / `np-pane-pane-review`)
   squeezing panes — user can falsify in seconds via
   `localStorage.removeItem(...)`; Chrome working makes this unlikely.

Ruled out already: server-side rendering (fast, correct at 1279-hub scale),
frame-blocking headers (SAMEORIGIN / frame-ancestors 'self' verified live),
JS errors (user console clean), named-target resolution (no longer used).

Local repro rig recipe (seed scripts, Playwright-in-docker, screenshots):
auto-memory `local-web-demo-recipe` + `.claude/scratch/seed_nanopub_demo.py`,
`seed_nanopub_scale.py`, `np_visual_test.py` in worktree
abundant-herding-star.
