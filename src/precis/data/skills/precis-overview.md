---
id: precis-overview
title: precis — seven verbs, one address scheme
status: phase-10
tier: 1
floor: any
applies-to: all
last-updated: 2026-05-01
---

# precis-overview — seven verbs, one address scheme

> **Versioning:** `serverInfo.version` is the canonical release marker
> (e.g. `6.0.0a0` ↔ the 6.0 / phase-7 line).  Skill front-matter
> `status: phase-N` labels are the **build phase** that wired the
> skill — they don't change the release version.  When in doubt,
> trust `serverInfo.version`.

## Verbs

| Verb     | Use when                                            |
|----------|-----------------------------------------------------|
| `get`    | You know the **name** (slug, id, file path) — or you're calling a tool. |
| `search` | You're looking for **content** by topic or phrase.  |
| `put`    | You want to **create** a new ref (and optionally tag/link it on creation). |
| `edit`   | You want to **rewrite a region** of an existing file (`find-replace`, `append`, `insert`, `replace`). |
| `delete` | Soft-delete a numeric ref, or delete a region from a file kind by selector. |
| `tag`    | Add and/or remove tags on an existing ref (`add=[...]`, `remove=[...]`). |
| `link`   | Add or remove a cross-link to another ref (`target=`, `mode='add'\|'remove'`, `rel=`). |

Address by **`id=` for names, `q=` for content**.

For `get`/`put`/`edit`/`delete`/`tag`/`link`, `kind=` is required.

For `search`, `kind=` is conventionally required, but **omitting
it defaults to the most recently touched search-supporting kind**
and the response prepends `(searched kind='X')` so you can see
the choice. This is a 7B-model affordance — cross-kind fan-out
(`kind='all'` or comma-lists like `kind='paper,memory'`) is not
yet implemented. To pick a different kind, name it explicitly.
Use `get(kind='skill', id='precis-help')` to discover the full
set of search-supporting kinds.

## Kinds — refs

Content you address by id, drill into chunks, link, and tag.  Two id
shapes — slug for canonical/named refs, integer for agent scratch:

| Kind      | Example id            | What                              | Needs |
|-----------|-----------------------|-----------------------------------|-------|
| `paper`   | `abazari2024design`   | Ingested research paper           | store |
| `skill`   | `precis-overview`     | Agent how-to (you're reading one) | — |
| `oracle`  | `precis-glossary`     | Wiki-like reference               | store |
| `quest`   | `ship-v2`             | A long-running goal               | store |
| `conv`    | `2026-04-26-spec`     | Past conversation                 | store |
| `markdown`| `notes--meeting`      | A `.md` file under the configured root | `PRECIS_MARKDOWN_ROOT` |
| `plaintext`| `notes--log`         | A `.txt` / `.log` file under the configured root | `PRECIS_PLAINTEXT_ROOT` |
| `python`  | `precis::precis.cli.main` | A symbol / file in a configured Python repo (alias before `::` matches `PRECIS_PYTHON_ROOTS`) | `PRECIS_PYTHON_ROOTS` |
| `todo`    | `122` (int)           | A task                            | store |
| `memory`  | `47` (int)            | Agent note / scratchpad           | store |
| `gripe`   | `9` (int)             | Annoyance / niggle — log freely, filter later | store |
| `fc`      | `204` (int)           | Flashcard (SM-2 spaced rep)       | store |

The **active** set varies by build — rows whose *Needs* column names
an env var are only registered when that var is set. Use
`get(kind='skill', id='precis-help')` to enumerate the kinds that
are live **right now** in this server.

## Kinds — tools

Stateless or cache-backed; pass a query in `q=` (or `id=`), get text
back. No agent-side slugs, no chunks, no links — results are cached
on a `(provider, request_hash)` key.

| Kind        | What                                | Example `q=`                 | Cost |
|-------------|-------------------------------------|------------------------------|------|
| `calc`      | Local SymPy: arithmetic, algebra    | `2+3*4`, `integrate(sin(x), x)` | free |
| `math`      | Wolfram Alpha: facts, world data    | `population of Ireland`      | paid |
| `youtube`   | Fetch a transcript                  | `dQw4w9WgXcQ`                | free |
| `web`       | Fetch + extract one URL             | `https://example.com/page`   | free |
| `websearch` | Perplexity Sonar: fast factual      | `latest perovskite results`  | paid |
| `think`     | Perplexity Sonar Reasoning Pro      | `compare DAC and BECCS`      | paid |
| `research`  | Perplexity Sonar Deep Research      | `mechanism of NOxRR`         | paid |

Paid tools cache automatically.  Pro subscribers can also
`put(mode='import')` a free web-UI answer into any of the three
Perplexity kinds at $0 — see `precis-perplexity-help`. See
`precis-cache` for TTLs and freshness.

## Examples

```python
# Find a paper, read its abstract.
search(kind='paper', q='photocatalytic NOx reduction', top_k=5)
get(kind='paper', id='abazari2024design', view='abstract')

# Make a todo, mark a different one done.
put(kind='todo', text='Review section 3 of abazari2024design.',
    tags=['PRIO:high'])
tag(kind='todo', id=122, add=['STATUS:done'])

# Quick calculation; real-world fact.
get(kind='calc', q='42 * 365')                # → 15330        (free)
get(kind='math', q='speed of light in km/h')  # → 1.079e9 km/h (paid)
```

## See also

- `precis-relations` — link vocabulary (`related-to`, `blocks`, `contradicts`)
- `precis-tags` — three namespaces by case, six closed prefixes
- `precis-cache` — paid-tool caching, freshness, force-refetch
- `precis-paper-help` — paper views, citation export
- `precis-todo-help` — todo lifecycle, priority, due dates, blocking
- `precis-memory-help` — memory sub-kinds via `kind:`
- `precis-python-help` — Python code navigation, callgraph + runtrace, AST-gated edits
- `precis-files-help` — shared address grammar for file-backed kinds (markdown, plaintext, python)
