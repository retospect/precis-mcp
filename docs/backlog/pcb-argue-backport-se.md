---
status: draft
title: back-port the handle-accumulation argue box from pcb to se
prio: low
model: sonnet
blocked-by: pcb-argue-with-design.md
---

# Back-port the argue box to se

se shipped comment-on-selection first (`c46b91cd`, slice 2 of
`se-topology-cloud-and-surface-notes.md`): a 3D pick opens a
selection-scoped comment box, the comment is LLM-rewritten into an
`se_notes` interview note. pcb then got the better interaction
(`pcb-argue-with-design.md`): ONE always-present text box that
accumulates handles as you click, so a single note can carry several
anchors in the user's own sentence order.

Once that is proven on pcb, replace se's per-selection box with it:
a 3D pick inserts the block name (or `block.port` / `block.measure`)
as a handle at the caret rather than opening a scoped box, and se
gains multi-anchor arguments for free.

Keep se's **rewrite step**: se notes are interview capture, where a
spoken one-liner genuinely needs sharpening into intent. pcb
deliberately has no rewrite (handles supply the precision there). The
two surfaces converge on the input widget, not on the processing.

Files: `src/precis_web/static/blocktree-3d.js`,
`src/precis_web/templates/blocktree/detail3d.html.j2`. No new storage
(`se_notes` already takes multi-anchor `about[]`). Cross-document
wiring is NOT needed here — the se viewer is same-document canvas, not
an `<object>` embed, so the pcb slice's `contentDocument` listener has
no analogue.
