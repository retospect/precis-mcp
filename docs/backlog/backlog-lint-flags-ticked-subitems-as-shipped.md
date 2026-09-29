# backlog-lint tells every session to delete open specs

`scripts/backlog-lint` prints "N item(s) marked done but still in
docs/backlog — verify shipped, then DELETE (git log keeps it)" for `- [x]`
lines that are **inside still-open specs**, not completed items. It already has
the concept — the same run prints "(3 done-marked sub-part(s) inside still-open
specs — normal, not gunk)" — so the classifier exists and these two escape it.

Verified 2026-09-29, both flagged items:

- `docs/backlog/precis-surface-kernel.md:45` — `status: draft`, `prio: high`,
  843 lines. The `[x]` is a progress tick under a heading that says "this list
  is ticks only".
- `docs/backlog/se-3d-viewer-ux-batch.md:22` — `status: idea`. The `[x]` marks
  a **design decision** ("DECIDED (Reto, 2026-09-29)", chosen over two
  alternatives, with the approved mockup inline) — a record of a ruling, not
  shipped work.

Deleting either, as instructed, destroys an open spec and a same-day ruling.

## Why it matters more than the noise

It fires on the SessionStart hook and in `scripts/ship`, so every session in
the fleet is told to delete these, every run. An advisory that is wrong every
time trains sessions to ignore it — which is the failure mode, because the real
case it was built for (a shipped item left behind as gunk) then also gets
ignored. It has been outstanding across this whole session.

## Fix sketch

Gate the "marked done" check on the item's own frontmatter `status:`, not on
the presence of a `- [x]` anywhere in the body. A file whose status is `draft`
or `idea` cannot be a shipped item regardless of how many sub-parts are ticked.
Only `status: done` (or an all-ticked checklist in a file with no open `- [ ]`)
should reach the DELETE advice.

While there: the message should name *why* it thinks the item is done, so the
next reader can falsify it in one look instead of opening the file.
