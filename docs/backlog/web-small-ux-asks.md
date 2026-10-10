---
status: idea
title: Small web UX asks — /drive control explanations, multiline /secrets values, todo/smartdraft related-item rendering
pillar: platform
---

# Small web UX asks

Bundled in the 2026-10-10 gripe triage. These are independent small
`precis_web` changes, batched so one session can land them together.

1. **/drive Kinds and Filters have no explanations (gr477859).** Reto,
   2026-10-10. Give each option a hover tooltip, or a one-line caption,
   saying what it filters.
2. **/secrets multiline values (gr467562, absorbed gr467559).** A field must
   accept a multiline value such as an SSH key and keep its line breaks
   when saved. The masked summary shows the character count and the line
   count.
3. **/r/todo/<id> "Strategic" section (gr477174, bug).** For a draft's
   project todo (td173019), it names an unrelated recurring item (#161192,
   a cast watch) as a bare id that cannot be clicked. Fix the selection, and
   render related ids as links.
4. **smartdraft Collaborate "Needs · in-flight" (gr477173, bug).** It lists
   the draft's own project todo, with a stale "succeeded" job status and no
   asks. Hide it or label it.

## Acceptance

Each numbered item is visible on dev with a browser check, and items 3–4
have a route test.
