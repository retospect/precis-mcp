---
id: precis-draft-review-help
family: drafting
title: precis — editing, reviewing and auditing a draft
summary: the review side of a draft — chunk edits and sign-off (review=/verdict=/authoring=), retire modes, hygiene + the uncited-gap finder, and how to steer prose vs correct an unsupported claim
answers:
  - how do I edit, review or retire a draft chunk?
  - how do I check a draft for undefined abbreviations, unresolved or drifted citations?
  - how do I steer someone's draft prose vs fix a wrong claim myself?
applies-to: get/put/edit/delete (kind='draft')
status: active
tags: [drafting]
kinds: [draft]
---

# precis-draft-review-help — edit, review, audit a draft

The change side of `kind='draft'` once content exists — authoring
(create, sections, prose) lives in [[precis-draft-help]]; lookup
tables in [[precis-draft-reference-help]].

## Edit, review & retire a chunk

```python
edit(id="dc12", text="Nanoscale transistors, defined as …")  # whole-chunk rewrite
edit(id="dc12", find="60°C", text="65°C")  # find= implies find-replace
edit(id="dc12", text="… big rewrite …", dry_run=True)  # PREVIEW the diff, write nothing
edit(
    kind="draft", id="dc12", review="human", verdict="needs-rework"
)  # record a sign-off
edit(
    kind="draft", id="nanotrans", authoring="on"
)  # let review personas author fixes inline
delete(id="dc12")  # retire a chunk (un-delete restores)
delete(id="dc20", mode="promote")  # remove heading, keep contents (lift to parent)
delete(id="dc20", mode="cascade")  # delete heading AND its contents
delete(id="nanotrans")  # ref-level id → soft-delete the WHOLE draft
```

Every text edit's ack ends with `sha:<12>`, the sha of the text that
landed. Pass it as `base_sha=` on the next edit to chain without a
re-read; a stale sha is refused.

`find=` is located **literally**; every occurrence is swapped for
`text=` (pass `text=''` to delete a span). If `find=` isn't present the
edit is **refused**, chunk untouched. `dry_run=True` gives a unified
diff; `dry_run='full'` shows the whole post-edit chunk. (For a regex
substitution across a whole section, use `edit(sub=…)` — see
[[precis-draft-reference-help]].)

`review=` names the checker (`'human'` is the single human identity; an
automated checker like `'cites'`/`'flow'` records the same way from a
worker). `verdict=` free text, default `'approved'`. Upsert keyed on
`(chunk_id, checker)` — metadata only, no re-embed; a later text edit
makes the chunk "dirty" for that checker again. Web reader's ✓ gutter
button drives this; no un-review verb — re-review overwrites the prior
row. `authoring=` is a per-document flag, default off — on, the
`cites`/`structure` review personas edit the draft inline (mint a
grounded citation, extend/add a chunk stamped
`authored_by='review:<persona>'`) instead of only filing a change-request
todo, whenever they can ground the fix; `flow`/`adversarial` never
author regardless. Web reader toolbar carries the same switch.

A heading with children requires `delete(mode=…)` — `promote` (keep
contents) or `cascade` (delete the section) — no default for that
destructive choice. Retired chunks drop out of the document but their
history (and any anchor to them) survives; you cannot delete the last
live chunk — a draft is never empty.

`delete` reads its granularity off the id: a chunk address (`dc<id>` /
`¶<base58>`) retires that chunk, a **ref-level** id (the slug, or the
numeric ref id) soft-deletes the **whole document** — ref plus every
chunk, one transaction, recoverable. The owning project todo is left
intact: that deletes the document, not the project.

## Audit the draft — hygiene checks & the gap-finder

Before export, runtime flags undefined abbreviations, unresolved citations
(cite grounded `[fi<id>]` hubs, not papers), and **drifted cites**: a hub's
claim changed after the passage was written. Its old `pub_id` remains an
alias, so the warning quotes old/new text. Rewrite the citing chunk to re-pin;
other edits do not clear it. Drift blocks export; pre-version-pin cites are
unknown, not drift. No bibliography footer is needed. Start with the outline
(`get(kind='draft', id=…)`); its hygiene footer shows up to 8 items per list.
For complete lists without the body, use
`get(kind='draft', id=…, view='hygiene')`.

**Missed a source?** `get(kind='draft', id=<scope>, view='backfill')`
sweeps the corpus for relevant-but-**uncited** papers and assembles an
eyes workspace around the candidates — semantic + citation-graph recall,
deduped against everything the draft already cites (including the
supporting papers behind every cited `[fi<id>]` claim hub, so a hub's
own evidence never resurfaces as a false gap). `id=` is a `dc<id>`
section (full per-candidate detail) or a draft slug (a slimmer roll-up).
A topic-precision gate keeps candidates on-domain when the cited papers
carry `topic:` tags — a no-op when they don't. Text-driven — it programs
its own recall from the section's own keywords. For a query *you* phrase
("what does the corpus say about X that this draft hasn't already
grounded?"), use `search(q=…, uncited=<draft>)` instead — the same
already-cited closure, applied to a hand-typed search. See
`precis-search-help`.

**Some drafts are machine-owned and refuse your edits.** A draft linked
`dossier-of` (a quest's dossier, [[precis-quest-help]]) or `paper-of` is
written by the process that owns it; `put`/`edit`/`delete` on one raises
`Unsupported`. Read it freely — if such a draft looks wrong, the fix
belongs in the process that writes it, not in the document.

## Steer prose changes; correct unsupported claims directly

Prose craft (structure, diction, LLM tells to avoid) lives in
[[precis-write-paper-help]]. Here, steering:

**Steer for craft; correct directly for evidence.** A claim a held
source contradicts is a defect, not authorial intent — reword it to what
the source states (or delete an unsupported quantity; write both when two
held sources disagree), cite the `[fi<id>]` hub grounded on the passage
you read, and never substitute a number you did not read in a source.
Taste-level changes — voice, structure, framing — you steer instead:

```python
edit(id='nanotrans', meta={'workspace': {'brief': '…updated brief…'}})
put(kind='todo', parent_id='<project>', text='tighten this paragraph',
    meta={'anchor': 'dc12'}, ...)        # a change request, anchored
link(src='dc12', rel='derived-from', dst='memory:7x2')  # provenance
```

To propose a rewrite for someone else, put it in `meta.proposed_text` on
that anchored todo; `get(id='dc12', view='proposals')` diffs each open
one. Nothing applies it: the reviewer runs an ordinary `edit`, then
closes the todo.

An anchored change-request `todo` flows through the normal todo tree →
dispatch → jobs. **Can't complete one? Ask clearly**, naming chunks by
`dc<id>` (drafts have no numeric "chunk 0"): `ask-user: '"remove this
para" is anchored at dc5; did you mean dc5 or dc12?'`. The ask shows on
the draft block as a 🔔.

## See also

- [[precis-draft-help]] — author a living document: create, search, write.
- [[precis-draft-reference-help]] — the lookup tables (verbs, views, params).
- [[precis-write-paper-help]] — prose craft: structure, diction, LLM tells.
- [[precis-draft-export-help]] — LaTeX/PDF/Word/reMarkable export.
