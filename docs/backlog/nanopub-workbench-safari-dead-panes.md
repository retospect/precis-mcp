---
status: draft
title: /nanopub workbench panes still dead in Safari (works in Chrome)
prio: high
---

# /nanopub workbench panes still dead in Safari (works in Chrome)

User reports 2026-08-25: clicking claim/paper links on
https://melchior.tailded4cf.ts.net/nanopub loads the panes in **Chrome
but does nothing in Safari**. Playwright WebKit (Linux trunk, 1300-hub
forest, Basic auth via `http_credentials`) can NOT reproduce — the
failure is specific to real Safari against the deployed origin.

State of the elimination:

- **Falsified**: named-target navigation (replaced by `loadPane()` JS in
  61e12350 — still dead), `srcdoc`-initialized frames (replaced by
  about:blank + injected placeholders in 8f20c436 — still dead),
  server-side rendering, frame-blocking headers, JS errors, stale
  localStorage pane widths (Chrome works).
- **Working theory — Basic-auth credential reuse into iframe
  subnavigations**: Safari replays cached Basic credentials
  inconsistently for subframe requests and silently suppresses the auth
  prompt (blank frame) on subframe 401s. Playwright injects per-request
  credentials, which is why WebKit-under-Playwright can't reproduce.
- **Countermeasure shipped** (session after 8f20c436): the auth gate now
  mints a signed `SameSite=Lax` session cookie on Basic-authenticated
  responses and accepts it as an alternative credential
  (`precis_web/auth.py::SessionTokens`) — same-origin iframe requests
  always carry cookies. NEEDS a real-Safari retest after deploy; the
  first top-level (re)load mints the cookie, then pane clicks should
  authenticate via it.

If it's STILL dead after the cookie deploy: the decisive diagnostic is
the user's Safari Web Inspector → Network tab while clicking a claim
row — does `/claim/fi…?embed=1` fire, with what status, and does it
carry `Cookie: precis_session=…`? Also falsify the theory family by
temporarily loading the workbench in a Safari Private window (fresh
credential cache).

Local repro rig (seed scripts, Playwright-in-docker, screenshots):
auto-memory `local-web-demo-recipe` + `.claude/scratch/seed_nanopub_demo.py`,
`seed_nanopub_scale.py`, `np_dup_test.py` in worktree
abundant-herding-star.
