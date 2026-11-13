---
id: remember
family: memory
title: precis — file one durable fact in repo-dev memory
summary: the write recipe for the repo-dev memory graph — search and reconcile before writing, then put or anchored-edit one claim per node with the right hub, type tag and links so memory-lint stays quiet
answers:
  - how do I file a durable fact into repo-dev graph memory?
  - a recalled memory is wrong — do I edit it, gripe it or add a node?
  - how do I update a thread node's resume state?
  - how do I retire a finished or superseded memory node?
  - where does a coined repo-dev term go?
applies-to: writes to the repo-dev memory graph (memory and taxon kinds)
status: active
tags: [workflow]
kinds: [memory, taxon]
governs: factory
---

# remember — keep one fact in the repo-dev graph

**Memory shape card**
- One claim per node; no second topic.
- The title IS the claim: at most 12 words, the sūtra, with its scope.
- A subject hub's or sub-hub's title is `<Name> hub: <scope list>`, set explicitly; `Belongs here:` / `Not here:` stay in its body. Spec and programme roots keep `Spec:` / `Programme:` titles.
- The body is the gloss: why, when it bites, what to do, one live anchor, a UTC date.
- Edges carry roles: `part-of` the hub, `cites`/`derived-from` for evidence, `qualifies` for scope.
- Name entities by handle; their current state belongs on their refs, never restated here.
- Expiry follows shape (gr560763): threads/handoffs/resumes/snapshots 14 days; incidents/campaigns 90 days.
- Rulings use `review_by`; reference/gotchas/runbooks have no age expiry; DREAM never expires by age.
- Expiry means review and compact before retirement, preserving still-true claims.
- No landed history or children lists; use the graph's links.
- No plan label (slice/round/R-number/leg/wave) without its handle on the same line.
- No bare commit sha older than 30 days; cite a live `path/file.py::Symbol` instead.
- Name yourself on every write: `args={'writer': {'model': '<your model id>'}}`; add `'human': 'reto'` when the content is what Reto told you.

The graph shape (root me474312 → subject hubs → nodes, `part-of` edges, one
type tag, gotchas `qualifies` threads) is described in the root node and in
[[precis-memory-help]] §"Hubs, part-of and qualifies". This skill is the
write recipe; the `put`/`edit` response's `Next:` lines do most of it for you.

## Write recipe

Free text with several subjects/claims: use `get(kind='skill',
id='precis-make-memory')` for extraction, labelled batch
recall and reconciliation ([[precis-make-memory]]). The canonical
identity/duplicate/conflict rules are [[precis-memory-help]] §"Memory
reconciliation policy"; the gardener loads that same section. This recipe
supplies repo-dev filing conventions after judgment.

1. **Search first.** `search(kind='memory', tags=['SPACE:repo-dev'],
   q='<the fact>', view='index')`. Narrow to one hub with
   `args={'under': 'me<hub>'}`, to one type with `tags=[…, 'section:gotchas']`.
2. **Reconcile before writing.** An unchanged claim is a no-op; apply the shared
   policy to identity uncertainty and conflicting values. **Same claim with a
   supported update → edit it in place** — anchored
   edit: `edit(kind='memory', id='me…', find='<old>', text='<new>')`;
   a wrong fact takes `reason=`. No second node.
3. **New → put it** — `put(kind='memory', tags=['SPACE:repo-dev',
   'section:<type>'], meta={'hook': '<one line: status + what you need to
   resume>'}, text=…)`.
   Type: `threads` (one campaign's resume state; see below), `gotchas` (trap), `runbooks`
   (how to do X), `workflow` (how agents/Reto work), `reference` (stable fact).
   Set `title=` explicitly. A thread's first body line is
   `Left: <what remains>` (`Left: none` when finished).
   The programme lives in the linked file under the backlog threads dir; the
   node holds only where this campaign stopped. Update `Left:` in the same
   session an item finishes or appears. AGENTS.md §"Repo-dev graph memory"
   owns the rule.
4. **Run the `Next:` lines the response prints**: the `part-of` link to the
   suggested hub (check it fits; the root lists all hubs), the `qualifies`
   links from relevant gotchas to a new thread, the type tag if missing.
   No `Next:` lines = the node is in place.
5. **Size.** Keep the gloss under the fisheye's 4000 characters. Split
   independent claims into atomic nodes; detail nodes take unique claim
   titles, the parent's type (`section:reference` for a result), one
   `part-of` to the summary only, and no `qualifies` links of their own —
   those live on the summary and cover its details.
6. **Cite durably.** Code as `path/file.py::Qual.name`
   (`docs/conventions/code-anchors.md`), dates in UTC. Replace commit
   shas older than 30 days with live anchors. A thread's landed history
   is not a fact — git has it.

## Retire a finished or superseded node

A thread at `Left: none` with no open linked due-item, or untouched 14
days once anything that matters has moved → retire it: move any still-true
gotcha or ruling into a live node, dry-run the delete, fix the listed nodes,
then retire. Start with `delete(kind='memory', id='me…', dry_run=True)`; use
each prefilled sentence-anchored edit to write the current truth. An inbound
`supersedes` edge or `args={'superseded_by': '<full handle>'}` supplies
the successor (any kind). Live claims without one refuse retirement
unless `args={'force': True}`; dated history is listed but exempt.

## A coined term is a taxon

A coined or overloaded term is a taxon, not a memory line and not a
file (the glossary file is retired, Reto 2026-11-03, gr559340). Look it
up with `search(kind='taxon', q='<term>', args={'under': 'tn559460'})`. Add
it in the session that coins it — `put(kind='taxon', text='<term> —
<gloss> → <start-here anchor> · skill <name>', meta={'pointer':
'<anchor>', 'aliases': [<legacy names>]}, link='taxon:<hub tn>',
rel='specialises')`; the hub nodes are the children of `tn559460`, one
per memory hub plus Projects & quests for nicknames. A rename makes the
old name an alias; a retired term is deleted.

## With evidence edit, on suspicion gripe

A recalled node is wonky, inconsistent or unclear → first file
a `put(kind='gripe', args={'project': 'memory'}, link='meN',
rel='raises-concern-about', text='memory: <symptom>\nGoal: <goal>\nRoute:
<query or link> -> meN said <claim>; <query or link> -> meM said <claim>')`.
Name every involved handle in the body and explicitly link each additional
ref with `link(kind='gripe', id=G, target='meM', rel='raises-concern-about')`.
Learned the truth with evidence → anchored `edit(…, reason='misled:
<evidence>')` and full readback, following [[precis-memory-help]] "A recalled
memory misled me" and [[precis-fix-memory-gripe-help]]. Do not edit a
human-origin node; hold and ask. Suspicion alone goes to the gardener, never
a coder.

Policy (what belongs in memory at all, authority of old nodes): AGENTS.md
§"Repo-dev graph memory". Periodic cleanup: [[reconsolidate]].

## Incident memories

Tag `incident` (not `prod-incident`), `section:reference` and
`SPACE:repo-dev`; add `release:r<N>` when a release is involved. Link
`part-of` the owning sub-hub. The body records what happened, cause and
confidence, UTC timeline, impact, why detection was slow, and follow-up
handles. One incident may need several narrative sections:
`AUDIT:grab-bag` exempts `incident` nodes; the other shape audits still apply.

Once the cause and fix are verified, compact the incident to a concise
dated cause, the verified fix and any open uncertainty. Never invent a fix.
Move still-true claims into gotchas or rulings before retiring the incident.
`scripts/memory-lint` reports incidents older than 90 days for review;
it never retires them. First examples: incident me564175, ruling me564176.

## See also

- [[reconsolidate]] — the periodic pass that catches what writes missed.
- [[precis-memory-help]] — graph semantics, reconciliation policy, misled-me route.
- [[precis-make-memory]] — extraction and batch recall for free text.
- [[precis-fisheye-help]] — the fisheye read that sets the 4000-char gloss cap.
