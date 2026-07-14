---
id: precis-toolpath-help
title: precis — canonical call sequences per scenario
summary: toolpaths — the canonical get/search/put/edit/tag/link sequence for each common goal, with the skill to read for depth
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

## Find things

| Goal | Toolpath | Depth |
|---|---|---|
| Find content by topic | `search(q='...')` (cross-kind) or `search(kind='paper', q='...')` | `precis-search-help` |
| Read a thing you can name | `get(kind='paper', id='wang2020state')` / `get(kind='todo', id=122)` | `precis-get-help` |
| Read one section / chunk | `get(id='pa5~40')` or a chunk handle `get(id='pc890282')` | `precis-addressing-help` |
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
`citation`, `folder`, `alert`, `agentlog`, `cron`, `message`).
Slug/file/compute kinds (`paper`, `draft`, `cad`, `structure`, `pcb`,
`tex`, `markdown`, …) each expose their own view set instead — a bad
`view=` returns that kind's option list.

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
| Real-world fact | `get(kind='math', q='speed of light in km/h')` | paid |
| Fetch + extract a URL | `get(kind='web', q='https://example.com')` | free |
| One Wikipedia article | `get(kind='wikipedia', q='CRISPR gene editing')` | free |
| Fast factual web search | `get(kind='websearch', q='latest perovskite results')` | paid |
| Video transcript | `get(kind='youtube', q='<video id>')` | free |
| Semantic Scholar lookup | `get(kind='semanticscholar', q='single-atom catalyst')` | free |

`calc` reads numeric trig in degrees by default (`sin(30)`=1/2) but keeps
symbolic arguments (`sin(x)` inside `integrate`/`diff`) in radians so
calculus comes out clean; pass `view='rad'` to force radians everywhere.
Paid tools cache automatically (`precis-cache`).

## The todo tree (intent → execution → review)

| Goal | Toolpath | Depth |
|---|---|---|
| See project dashboard | `search(kind='todo', view='projects')` | `precis-tasks-help` |
| Drill into one project's tree | `get(kind='todo', id=N, view='tree')` | `precis-tasks-help` |
| Doable leaves in a subtree | `search(kind='todo', view='doable', args={'under': N})` | `precis-tasks-help` |
| What needs my attention | `search(kind='todo', view='attention')` | `precis-tasks-help` |
| Split a task | children via `put(..., parent_id=N)` | `precis-decomposition-help` |
| Wait on a condition | leaf with `meta.auto_check` | `precis-auto-tasks-help` |
| Recurring work | `level:recurring` + `meta.schedule` | `precis-recurring-help` |
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
| Organize artifacts | `kind='folder'` + `link(rel='parent')`; `search(folder=...)` | `precis-folder-help` |
| Verified claim → source | `kind='citation'` / `kind='finding'` | `precis-citation-help`, `precis-finding-help` |
| Spaced-repetition cards (Anki) | **`search(kind='anki', q=…)` first (dedup)** → `put(kind='anki', text='… {{cN::…}} …', tags=['deck-<topic>'])` → syncs to AnkiWeb | `precis-cloze` (craft), `precis-anki-help` (ref) |
| Cards I keep forgetting | `get(kind='anki', id='/leeches')` → fix the cloze (tag `precis-fix` in Anki) or study more | `precis-anki-help` |

## See also

```python
get(kind='skill', id='precis-overview')   # kinds table + address scheme
get(kind='skill', id='precis-help')       # verb table from the live registry
get(kind='skill', id='toc')               # every skill, one-line synopsis
```

---
Read `precis-overview` for the full kinds catalogue and the handle /
address grammar; this file is the *sequence* index, that one is the
*surface* map.
