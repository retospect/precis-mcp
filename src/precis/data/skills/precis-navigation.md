---
id: precis-navigation
title: precis — recipes for common flows
status: aspirational
tier: 1
floor: any
applies-to: cross-cutting (all kinds)
last-updated: 2026-04-28
---

> **Heads up:** the MCP critic (Apr 2026) flagged 8 of the 9 recipes
> below as containing at least one non-functional call — `limit=`
> instead of `top_k=`, `kind='all'` (cross-kind not implemented),
> `kind='ask'` (not a real kind), `due='friday'` on `put(kind='todo')`
> (no `due=` parameter), `view='representatives'`/`'methods'`/
> `'coverage'`/`'today'`/`'overdue'` (none implemented),
> `tags=['DENSITY:*']`/`['CONFIDENCE:*']` (not registered prefixes).
> This skill is now `status: aspirational` and filtered from the
> default index. The recipes describe the *intended* shape; pick
> apart whichever sub-step you need from a working skill
> (`precis-paper-help`, `precis-memory-help`, `precis-todo-help`,
> `precis-relations`, `precis-tags`) before running.

# precis-navigation — recipes for common flows

Concrete sequences for the things agents do most.  Each recipe is three
to six calls.  Use these as templates; adapt freely.

## Find a paper by topic

```python
search(kind='paper', q='photocatalytic NOx reduction', limit=5)
get(kind='paper', id='wang2020state', view='abstract')
get(kind='paper', id='wang2020state', view='toc')          # if interesting
```

Variants: search by `q='Ru(bpy)3'` (keyword exact match), by author
(`q='Wang 2020'`), by DOI (`get(id='doi:10.1002/...')`).

## Read a paper, end-to-end

```python
get(kind='paper', id='wang2020state', view='abstract')
get(kind='paper', id='wang2020state', view='representatives')   # compressed
get(kind='paper', id='wang2020state', view='toc')               # navigate
get(kind='paper', id='wang2020state', view='methods')           # drill
get(kind='paper', id='wang2020state~38..42')                    # specific chunks
```

`representatives` saves time on long papers — you see the contribution
without the lit-review padding.

## Capture a thought as a memory

```python
put(kind='memory',
    text='Z-scheme heterojunction in Wang2020 §3 looks transferable to NOxRR.',
    tags=['kind:idea', 'topic:noxrr', 'CONFIDENCE:tentative'],
    link='paper:wang2020state~38')
```

One call, three slots: content, classification (tags), provenance (link).

## Make and manage a todo

```python
put(kind='todo', text='Review section 3 of wang2020state.',
    tags=['PRIO:high', 'project:noxrr-review'],
    due='friday')
# → returns integer id (e.g. 122)

get(kind='todo', view='today')                              # what's due today
get(kind='todo', view='overdue')                            # what slipped

put(kind='todo', id='122', tags=['STATUS:done'])            # mark complete
```

## Find information — corpus first, fresh `ask` last

```python
# 1. Broad search across the whole corpus.
#    Already surfaces papers, memories, and any prior ask answers.
search(q='mechanism of NOxRR', kind='all', limit=5)

# 2. Thin on primary literature?  Diagnose paper coverage.
get(kind='paper', view='coverage', q='mechanism of NOxRR')

# 3. No usable hit and you want a fresh synthesis?  Ask.
get(kind='ask', q='mechanism of NOxRR')
# Checks cache first; only paid if the query is genuinely new.
```

Cite the answer from a memory if worth keeping past TTL.

## Find what's novel on a topic

```python
search(kind='paper', q='photocatalytic Z-scheme',
       tags=['DENSITY:sparse'], limit=10)
# → distinctive chunks, not the commonplace echo
```

Combine with `SRC:primary` if you want only original literature, not
review papers.

## Build an evidence-backed claim

```python
# 1. Find supporting and dissenting evidence.
search(kind='paper', q='Z-scheme efficiency', limit=10)

# 2. Capture the claim with confidence and links.
put(kind='memory',
    text='Z-scheme heterojunctions consistently outperform single-component '
         'systems on NOxRR by ~2x quantum efficiency.',
    tags=['kind:summary', 'topic:noxrr', 'CONFIDENCE:moderate'],
    link='paper:wang2020state')
# → returns integer id (e.g. 89)

# 3. Add a contradicting source if one exists.
put(kind='memory', id='89', link='paper:chen2021critique', rel='contradicts')
```

Now `get(kind='memory', id='89', view='links')` shows the evidence both
ways.

## Verify an external claim against the corpus

```python
# Claim: "MOF catalysts always outperform metal oxides for NOxRR."
search(kind='paper', q='MOF NOxRR comparison', limit=10)
search(kind='paper', q='metal oxide NOxRR efficiency', limit=10)

# Look for explicit contradictions in your existing notes.
search(kind='memory', q='MOF metal oxide comparison',
       tags=['CONFIDENCE:strong'])

# Coverage check: do we have enough material to judge?
get(kind='paper', view='coverage', q='MOF vs metal oxide NOxRR')
```

If coverage is thin, reach for `ask` or flag a gap with a `kind:question`
memory.

## Triage todos for a project

```python
search(kind='todo', tags=['project:precis-v2', 'STATUS:active'])
get(kind='todo', view='blocked', tags=['project:precis-v2'])  # what's stuck
get(kind='todo', view='overdue', tags=['project:precis-v2'])  # what slipped

# Bump priority on a key one:
put(kind='todo', id='141', tags=['PRIO:urgent'])

# Resolve a blocker:
put(kind='todo', id='158', tags=['STATUS:done'])
# Anything blocked-by 158 now exits view='blocked'.
```

## Promote an `ask` cache to durable knowledge

```python
get(kind='ask', q='mechanism of NOxRR')   # generates cache

# It's a useful answer.  Pin against decay:
put(kind='ask', id='mechanism-of-noxrr', tags=['pinned'])

# Or distill into a memory that survives cache deletion:
put(kind='memory',
    text='Three-electron pathway, see §2 of cached answer.',
    tags=['kind:summary', 'topic:noxrr', 'CONFIDENCE:moderate'],
    link='research:mechanism-of-noxrr')
```

## See also

- `precis-overview` — verbs and kinds
- `precis-relations` — `related-to`, `blocks`, `contradicts`
- `precis-tags` — `STATUS:`, `PRIO:`, `CONFIDENCE:`, `topic:`
- `precis-cache` — when an `ask` answer is worth pinning vs distilling
- `precis-density` — `representatives`, `sparse`, `coverage`
- `precis-paper-help`, `precis-todo-help`, `precis-memory-help`
