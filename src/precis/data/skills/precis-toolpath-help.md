---
id: precis-toolpath-help
title: precis — canonical call sequences per scenario
summary: toolpaths — the canonical get/search/put/edit/tag/link sequence for each common goal, with the skill to read for depth
answers:
  - I know what I want to accomplish but not which verbs to call — where do I start?
  - what's the call sequence to mint a claim hub?
  - how do I run an async paper-search campaign?
  - how do I wire a job under a todo?
  - a search(kind='skill') reply looks like an error — should I reword the query?
applies-to: all
status: active
---

# precis-toolpath-help — the canonical call sequence per goal

Start here when you know *what you want to accomplish* but not *which
verbs, in what order*. Each row below is a **toolpath**: the minimal
canonical sequence for one scenario, plus the skill to read for the
full surface. Seven verbs — `get` / `search` / `put` / `edit` /
`delete` / `tag` / `link` — apply to every kind that supports them; the
kinds table + address grammar live in `precis-overview`.

Rule of thumb: **`id=` addresses a name/handle, `q=` searches content.**
For `get`/`put`/`edit`/`delete`/`tag`/`link`, `kind=` is required; for
`search`, `kind=` is optional (omit it for cross-kind fan-out).

**Before a native tool, check for a precis kind.** External content that
precis already fetches + caches goes through a `kind`, never a native library
or ad-hoc web scrape: a **YouTube** URL → `get(kind='youtube', id=…)` (not
`youtube_transcript_api`); an arbitrary **web page** → `get(kind='web', …)`;
**Wikipedia** → `kind='wikipedia'`; a **web search** → `kind='websearch'`.
When unsure, `search(kind='skill', q='<the thing>')` first — reaching for a
native tool and only falling back to precis on failure is the slow path.

**Reading a `search(kind='skill')` reply — three outcomes, three responses.**
A ranked table → pick a slug and `get` it. `no skills mention '…'` means the
search *ran* and matched nothing: reword **once** if the phrasing was unusual,
then stop rewording and `get(kind='skill', id='toc')` — the skill is likely
named differently, or the thing isn't a skill. A *permission* or *error* reply
(`… haven't granted it yet`, `[error:…]`, a timeout) means the tool **didn't
run at all**: do **not** reword — a reworded query hits the same block and
wastes the turn. Report the tool as unavailable and move on. Rewording only
ever helps the middle case; it never unblocks a blocked or errored call.

## Find things

| Goal | Toolpath | Depth |
|---|---|---|
| Find content by topic | `search(q='...')` (cross-kind) or `search(kind='paper', q='...')` | `precis-search-help` |
| Search this repo's own docs/backlog/skills prose | `search(kind='md', q='...')` | `precis-md-help` |
| Read a thing you can name | `get(kind='paper', id='wang2020state')` / `get(kind='todo', id=122)` | `precis-get-help` |
| Read one section / chunk | `get(id='pa5~40')` or a chunk handle `get(id='pc890282')` | `precis-addressing-help` |
| Read a chunk + its neighborhood | `get(kind='draft', id='dc41', view='fisheye')` | `precis-fisheye-help` |
| Browse a paper's structure | `get(kind='paper', id='wang2020state', view='toc')` | `precis-toc-help` |
| Broad paper retrieval | `search(kind='paper', queries=[...], answers=[...HyDE], per_paper=N)` | `precis-search-help` |
| Deep async paper campaign | `search(kind='paper', q='...', good=True)` → poll the job handle | `precis-search-help` |
| Which skill do I need? | `get(kind='skill', id='toc')` / `search(kind='skill', q='your goal')` | `precis-overview` |
| Stumble into something new | `get(kind='random')` | `precis-random-help` |
| Debug a ref's hidden state | `get(kind='todo', id=N, view='raw')` (dumps full `meta` JSON) | `precis-todo-help` |
| Inspect a ref's link graph | `get(kind='todo', id=N, view='links')` | `precis-relations` |
| Read a ref's event trail | `get(kind='todo', id=N, view='log')` | — |

`view='raw'` / `view='links'` / `view='log'` work on the **numeric-ref
kinds** (`todo`, `memory`, `gripe`, `finding`, `job`, `anki`,
`citation`, `folder`, `alert`, `agentlog`, `message`).
Slug/file/compute kinds (`paper`, `draft`, `cad`, `structure`, `pcb`,
`tex`, `markdown`, `python`, `md`, …) each expose their own view set
instead — a bad `view=` returns that kind's option list.

## Capture and edit

| Goal | Toolpath | Depth |
|---|---|---|
| File a task | `put(kind='todo', text='...', tags=['PRIO:high'])` | `precis-todo-help` |
| File a bug in precis itself | `put(kind='gripe', text='...')` (search first) | `precis-gripe-help` |
| Keep a note for later | `put(kind='memory', text='...')` | `precis-memory-help` |
| Rewrite a region of a file | `edit(kind='markdown', id='notes/x.md', find='...', text='...')` | `precis-edit-help` |
| Delete a matched span | `edit(..., find='...', text='')` | `precis-edit-help` |
| Rewrite a todo in place | `edit(kind='todo', id=122, mode='replace', text='...')` | `precis-todo-help` |
| Soft-delete a ref | `delete(kind='gripe', id=42)` | `precis-delete-help` |
| Classify / prioritise | `tag(kind='todo', id=122, add=['STATUS:done'])` | `precis-tag-help`, `precis-tags` |
| Connect two refs | `link(kind='todo', id=141, target='todo:158', rel='blocked-by')` | `precis-link-help`, `precis-relations` |
| Page a long response | `more(cursor='...')` (from a `Next: more(...)` footer) | — |

`STATUS:` / `PRIO:` / `SRC:` / `CACHE:` are closed UPPERCASE prefixes —
adding a new value replaces the old within that prefix atomically.

## Tool answers (no slugs, pass `q=`)

| Goal | Toolpath | Cost |
|---|---|---|
| Exact / symbolic math | `get(kind='calc', q='integrate(sin(x)**2, x)')` | free |
| Unit conversion | `get(kind='calc', q='3 ft to m')` · `q='1 ton to kg'` (local, exact, disambiguates ton/gallon/oz) | free |
| Real-world fact | `get(kind='math', q='speed of light in km/h')` | paid |
| Fetch + extract a URL | `get(kind='web', q='https://example.com')` | free |
| One Wikipedia article | `get(kind='wikipedia', q='CRISPR gene editing')` | free |
| Fast factual web search | `get(kind='websearch', q='latest perovskite results')` | paid |
| Video transcript | `get(kind='youtube', q='<video id>')` | free |
| Semantic Scholar lookup | `get(kind='semanticscholar', q='single-atom catalyst')` | free |

`calc` reads numeric trig in degrees by default (`sin(30)`=1/2) but keeps
symbolic arguments (`sin(x)` inside `integrate`/`diff`) in radians so
calculus comes out clean; pass `view='rad'` to force radians everywhere. A
`to`/`in`/`->` clause makes `calc` a **local unit converter** (`3 ft to m`,
`100 degC to degF`) — reach for it, not paid `math`/Wolfram, for any
ordinary conversion. Paid tools cache automatically (`precis-cache`).

## The todo tree (intent → execution → review)

| Goal | Toolpath | Depth |
|---|---|---|
| See project dashboard | `search(kind='todo', view='projects')` | `precis-tasks-help` |
| Drill into one project's tree | `get(kind='todo', id=N, view='tree')` | `precis-tasks-help` |
| Doable leaves in a subtree | `search(kind='todo', view='doable', args={'under': N})` | `precis-tasks-help` |
| What needs my attention | `search(kind='todo', view='attention')` | `precis-tasks-help` |
| Split a task | children via `put(..., parent_id=N)` | `precis-decomposition-help` |
| Sketch a thread's reasoning outline | `put(kind='plan', id='x-plan', title='…', project=N)`, then add `pe<id>` nodes | `precis-plan-help` |
| Wait on a condition | leaf with `meta.auto_check` | `precis-auto-tasks-help` |
| Recurring work | `meta.schedule` set | `precis-recurring-help` |
| Run a job under a todo | set `meta.executor`; `dispatch` mints a `kind='job'` | `precis-dispatch-help`, `precis-job-help` |
| Auto-fix a gripe | `put(kind='job', job_type='fix_gripe', link='gripe:42', rel='fixes')` | `precis-fix-gripe-help` |

## Authoring artifacts

| Goal | Toolpath | Depth |
|---|---|---|
| Chunk-native document | `kind='draft'` (chunks addressed `¶<handle>`) | `precis-draft-help` |
| `.tex` file store | `kind='tex'` (section-aware blocks) | `precis-tex-help` |
| Parametric solid model | `kind='cad'` (node-list, analytic probes) | `precis-cad-help` |
| Atomistic cell + bonds | `kind='structure'` (DFT ladder) | `precis-structure-help` |
| PCB netlist + placement | `kind='pcb'` + `kind='part'` / `kind='datasheet'` | `precis-pcb-help` |
| Sourced material property (density, yield strength, ...) | `put(kind='material', id='<slug>', property='<prop_id>', value=..., unit='<canonical unit>')` — entity first, canonical units only | `precis-material-help` |
| Materials with property in a range | `search(kind='material', property='thermal_conductivity', max=0.05)` | `precis-material-help` |
| Sourced component spec (bolt/hose/bearing/...) | `put(kind='component', id='<slug>', spec='<spec_id>', value=..., unit='<canonical unit>')` — entity first (`category=` required), canonical units only | `precis-component-help` |
| Component made of a material | `put(kind='component', id='<slug>', made_of='material:<slug>')` | `precis-component-help` |
| Components with spec in a range | `search(kind='component', spec='max_working_pressure', min=20, category='hose')` | `precis-component-help` |
| Organize artifacts | `kind='folder'` + `link(rel='parent')`; `search(folder=...)` | `precis-folder-help` |
| Verified claim → source | `kind='citation'` / `kind='finding'` | `precis-citation-help`, `precis-finding-help` |
| Find/cite a cross-paper claim hub | `search(kind='finding', tags=['TAPROOT:claim'])` → cite `[fi<id>]` (living) or pin `[fi<id>>pa5]` | `precis-taproot-help` |
| Only claims that are actually settled | `search(kind='finding', q=…, trust='signed'\|'verified')` — read the `state`/`support`/`flags` columns; `trust='disputed'` for what's opposed | `precis-finding-help` |
| What is this claim's shape in the graph? | `get(kind='finding', id='fi<id>', view='fisheye+1hop')` — trust posture, then evidence/`refines`/`conjunct-of` one edge out | `precis-finding-help`, `precis-fisheye-help` |
| Mint a claim hub | **`search(kind='finding', q='<the sentence>', status='*', mode='semantic')` first** — attach to an existing hub rather than mint a near-duplicate — then `put(kind='finding', supporters=…)` | `precis-taproot-mint-help`, `precis-notation-canon` |
| Convert a draft's legacy `[pc]`/`[pa]` cites into hub cites | `put(kind='job', job_type='taproot_backfill', params={'scope': '<slug>'})` or `precis taproot backfill --draft <slug>` | `precis-taproot-backfill-help` |
| Reusable reasoning step beside a draft | `kind='memory'` tagged `kind:lemma`/`kind:inference`, `entails`/`derived-from` edges → `get(view='argument')` | `precis-argument-help` |
| Spaced-repetition cards (Anki) | **`search(kind='anki', q=…)` first (dedup)** → `put(kind='anki', text='… {{cN::…}} …', tags=['deck-<topic>'])` → syncs to AnkiWeb | `precis-cloze` (craft), `precis-anki-help` (ref) |
| Cards I keep forgetting | `get(kind='anki', id='/leeches')` → fix the cloze (tag `precis-fix` in Anki) or study more | `precis-anki-help` |

## Chemistry / biology (the in-silico lab)

Compute kinds — the engine runs off the request path (mint → poll), the IR is
what you read. Plugin kinds (`route`/`protein`), on where the tool-pack is enabled.

| Goal | Toolpath | Depth |
|---|---|---|
| Plan a synthesis to a target | `put(kind='route', id='<slug>', target='<SMILES>', engine='aizynth')` → `get(kind='route', id='<slug>')` / `view='metrics'` | `precis-route-help` |
| Fold a protein from its sequence | `put(kind='protein', id='<slug>', sequence='<AA>', engine='alphafold3')` → `get(kind='protein', id='<slug>')` | `precis-protein-help` |
| See a fold in 3D | `get(kind='protein', id='<slug>', view='structure')` → `get(kind='structure', id='<slug>-fold')` | `precis-structure-help` |
| Compose them toward a research goal | search prior art → mint route/fold → read metrics/pLDDT → iterate | `precis-lab-help` |

## See also

```python
get(kind="skill", id="precis-overview")  # kinds table + address scheme
get(kind="skill", id="precis-help")  # verb table from the live registry
get(kind="skill", id="toc")  # every skill, one-line synopsis
```

---
Read `precis-overview` for the full kinds catalogue and the handle /
address grammar; this file is the *sequence* index, that one is the
*surface* map.
