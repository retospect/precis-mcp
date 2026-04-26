---
id: precis-overview
title: precis — four verbs, one address scheme
status: draft
tier: 1
floor: any
applies-to: all
last-updated: 2026-04-26
---

# precis-overview — four verbs, one address scheme

## Verbs

| Verb     | Use when                                            |
|----------|-----------------------------------------------------|
| `get`    | You know the **name** (slug, id, file path) — or you're calling a tool. |
| `search` | You're looking for **content** by topic or phrase.  |
| `put`    | You want to **write** (content, note, link, tag).   |
| `move`   | You want to **reorder** within one document.        |

Address by **`id=` for names, `q=` for content**.

For `get`/`put`/`move`, `kind=` is required.  For `search`, `kind=` is
optional and defaults to `'all'`; pass a single kind or a comma-list
(`kind='paper,memory,ask'`) to narrow.

## Kinds — refs

Content you address by id, drill into chunks, link, and tag.  Two id
shapes — slug for canonical/named refs, integer for agent scratch:

| Kind      | Example id            | What                              |
|-----------|-----------------------|-----------------------------------|
| `paper`   | `wang2020state`       | Ingested research paper           |
| `book`    | `wittgenstein-pi`     | Ingested book                     |
| `skill`   | `precis-overview`     | Agent how-to (you're reading one) |
| `oracle`  | `precis-glossary`     | Wiki-like reference               |
| `quest`   | `ship-v2`             | A long-running goal               |
| `conv`    | `2026-04-26-spec`     | Past conversation                 |
| `todo`    | `122` (int)           | A task                            |
| `memory`  | `47` (int)            | Agent note / scratchpad           |
| `gripe`   | `9` (int)             | Annoyance / niggle — log freely, filter later |
| `fc`      | `204` (int)           | Flashcard (SM-2 spaced rep)       |

Files on disk are refs too: `report.docx`, `paper.tex`, `notes.md`,
`plain.txt`, `sketch.rmk`.

## Kinds — tools

Stateless; pass a query in `q=`, get text back.  No slugs, no chunks, no links.

| Kind        | What                                | Example `q=`             | Cost |
|-------------|-------------------------------------|--------------------------|------|
| `calc`      | Local SymPy: arithmetic, algebra    | `2+3*4`, `integrate(sin(x), x)` | free |
| `math`      | Wolfram Alpha: facts, world data    | `population of Ireland`  | paid |
| `clock`     | Time, dates, durations              | `now`, `friday`          | free |
| `rng`       | Random numbers, sampling            | `1..100`, `choice(a,b,c)`| free |
| `plot`      | Render a matplotlib plot from JSON  | (spec)                   | free |
| `youtube`   | Fetch a transcript                  | `dQw4w9WgXcQ`            | free |
| `websearch` | Web search                          | `latest perovskite results` | paid |
| `ask`       | Perplexity (Sonar): research depth  | `mechanism of NOxRR`     | paid |

Paid tools cache automatically.  See `precis-cache`.

## Examples

```python
# Find a paper, read its abstract.
search(kind='paper', q='photocatalytic NOx reduction', limit=5)
get(kind='paper', id='wang2020state', view='abstract')

# Make a todo, mark a different one done.
put(kind='todo', text='Review section 3 of wang2020state.',
    tags=['PRIO:high'], due='friday')
put(kind='todo', id='122', tags=['STATUS:done'])

# Quick calculation; real-world fact.
get(kind='calc', q='42 * 365')                # → 15330        (free)
get(kind='math', q='speed of light in km/h')  # → 1.079e9 km/h (paid)
```

## See also

- `precis-relations` — link vocabulary (`related-to`, `blocks`, `contradicts`)
- `precis-tags` — three namespaces by case, six closed prefixes
- `precis-cache` — paid-tool caching, freshness, force-refetch
- `precis-density` — novelty-finding, corpus coverage
- `precis-paper-help` — paper views, citation export
- `precis-todo-help` — todo lifecycle, priority, due dates, blocking
- `precis-memory-help` — memory sub-kinds via `kind:`
- `precis-navigation` — recipes for common flows
