---
id: precis-overview
title: precis — seven verbs, one address scheme
summary: top-level orientation — seven verbs, address scheme, kinds table, skill index
applies-to: all
status: active
---

# precis-overview — seven verbs, one address scheme

Address by **`id=` for names, `q=` for content**. Seven verbs apply
to every kind that supports them.

## What are the seven verbs?
## Verb cheat-sheet
## When do I use get vs search vs put?

| Verb     | Use when |
|----------|----------|
| `get`    | You know the name (slug, id, file path) — or you're calling a tool. |
| `search` | You're looking for content by topic or phrase. |
| `put`    | You want to create a new ref (optionally tag/link on creation). |
| `edit`   | You want to rewrite a region of an existing file. |
| `delete` | Soft-delete a numeric ref, or delete a region from a file. |
| `tag`    | Add or remove tags (`add=[...]`, `remove=[...]`). |
| `link`   | Add or remove a cross-link (`target=`, `rel=`). |

For `get`/`put`/`edit`/`delete`/`tag`/`link`, `kind=` is required.
For `search`, `kind=` is optional — omitted means cross-kind fan-out.

## What kinds can I address by slug or DOI?
## Content kinds I can read and tag
## The ref kinds (id-addressable, support get/search/tag/link)

| Kind | Example id | What | Needs |
|---|---|---|---|
| `paper` | `abazari2024design` | Ingested research paper | store |
| `patent` | `ep1234567b1` | EPO OPS patent record | store |
| `skill` | `precis-overview` | Agent how-to (you're reading one) | — |
| `oracle` | `stoic` | Curated wisdom-tradition entry | store |
| `conv` | `2026-04-26-spec` | Past conversation | store |
| `pres` | `2026-06-talk-foo` | Slide deck or unpublished writeup | store |
| `markdown` | `notes--meeting` | A `.md` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `plaintext` | `notes--log` | A `.txt` / `.log` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `tex` | `chapters--intro` | A `.tex` file (section-aware blocks + `/toc`) | `PRECIS_ROOT` |
| `python` | `precis::precis.cli.main` | Symbol or file in a configured Python repo | `PRECIS_PYTHON_ROOTS` |
| `todo` | `122` (int) | A task in the hierarchical tree (Slice 1–5). Branches read as outcomes; leaves as next actions. See `precis-tasks-help`. | store |
| `memory` | `47` (int) | Agent note / scratchpad | store |
| `gripe` | `9` (int) | Annoyance / niggle | store |
| `alert` | `38260` (int) | Machine-detected ops / health condition (spin loop, orphan, stalled recurring). Raised by background passes, deduped + auto-resolved; surfaced by the `/alerts` web tab, **not** semantic search. See `precis-alert-help`. | store |
| `flashcard` | `204` (int) | Flashcard (SM-2 spaced rep) | store |
| `citation` | `18` (int) | Verified claim → source quote | store |
| `finding` | `73` (int) | Chain-of-evidence head over a citation chase | store |
| `job` | `55` (int) | Execution attempt of a todo intent. **New jobs require `parent_id` pointing at a `kind='todo'`** — see `precis-job-help` + `precis-dispatch-help`. | store |
| `cron` | `42` (int) | Push-notification scheduler — fires a payload to an external conversation (asa-bot Discord) at the scheduled time. **Different use case** from `level:recurring` todos (which pull recurring work into the doable queue); both kinds kept on purpose. See `precis-cron-help`, `precis-recurring-help`, ADR 0030. | store |
| `message` | `11` (int) | Proactive outbound (Discord post) | store |
| `provenance` | `92` (int) | Per-ref provenance audit (sources, transforms) | store |
| `tag` | `topic:co2-capture` | Discoverable tag row (`get`/`search` only) | store |

Rows with an env var in *Needs* are only active when that var is set.
For the live list use `get(kind='skill', id='precis-help')`.

## What kinds give me cached tool answers?
## Stateless / cache-backed tool kinds
## When do I reach for math, web, perplexity, youtube?

Pass `q=` (or `id=`), get text back. No agent-side slugs.

| Kind | What | Example `q=` | Cost |
|---|---|---|---|
| `calc` | Local SymPy: arithmetic, algebra | `2+3*4` | free |
| `math` | Wolfram Alpha: facts, world data | `population of Ireland` | paid |
| `youtube` | Transcript fetch | `dQw4w9WgXcQ` | free |
| `web` | Fetch + extract a URL | `https://example.com/page` | free |
| `wikipedia` | Resolve + fetch one Wikipedia article (on-demand; fenced from default search via `ORIGIN:wikipedia`) | `CRISPR gene editing` | free |
| `websearch` | Perplexity Sonar: fast factual | `latest perovskite results` | paid |
| `think` | Perplexity Sonar Reasoning Pro | `compare DAC and BECCS` | paid |
| `research` | Perplexity Sonar Deep Research | `mechanism of NOxRR` | paid |

Paid tools cache automatically. Pro subscribers can import a free
web-UI answer at $0 via `put(mode='import')` — see
`precis-perplexity-help`. See `precis-cache` for TTLs.

## What's the special discovery kind?
## How do I stumble into something I don't know to ask for?

| Kind | What |
|---|---|
| `random` | Pick a random embedded block; returns its handle + a drill-down hint |

Useful for warm-up, inspiration, sanity-checking a fresh corpus.

## What's the shared address grammar?
## How do I address a chunk or a sub-range?
## What does slug~N mean?

| Form | Meaning |
|---|---|
| `slug` | the whole ref |
| `slug~N` | chunk N |
| `slug~A..B` | chunk range A..B (inclusive) |
| `slug/toc` | TOC of the ref (= `view='toc'`) |
| `slug~A..B/toc` | sub-TOC, segments within the range |
| `slug~A..B`, `view='toc'` | same as `slug~A..B/toc` |

Currently TOC-capable: `paper`, `skill`. Other kinds pick up the
grammar as their handlers wire `chunks_for_toc`.

## How do I find the right skill?

```python
get(kind='skill', id='toc')                  # browse every skill, one-line synopsis
search(kind='skill', q='your goal')          # fuzzy lookup, e.g. 'spaced repetition'
get(kind='skill', id='precis-help')          # what kinds + verbs are live in this build
get(kind='skill')                            # list every active skill
```

`precis-toc` is the long-form alias for `id='toc'`.

## The todo tree — task substrate (Slices 1–5)

The todo tree is the unified surface for *intent*, *execution*,
*scheduling*, and *review* over the corpus:

| Skill | What it teaches |
|---|---|
| `precis-tasks-help` | Tree shape (strategic/tactical/subtask), claim/release/done, doable view rules |
| `precis-decomposition-help` | The GTD interrogation: when to split, when to block, when to wait |
| `precis-auto-tasks-help` | Wait-for-condition leaves via `meta.auto_check` |
| `precis-recurring-help` | `level:recurring` schedule format + the **Watches** umbrella |
| `precis-dispatch-help` | When to set `meta.executor` on a todo so a `kind='job'` runs under it |
| `precis-job-help` | The job substrate. New jobs require `parent_id` pointing at a todo |
| `precis-fix-gripe-help` | First concrete job_type, end-to-end recipe |
| `precis-nursery-help` | Hourly SQL-only review tier (`tier:nursery` memories) |
| `precis-wikipedia-help` | On-demand Wikipedia lookup + the `ORIGIN:wikipedia` search fence |

PRIO sort key + 1/N rotation across active strategics + dedup-aware
nursery / structural / deep reviewers are the operational discipline
on top. See `docs/design/todo-tree-plan.md` for the full design.

## Worked examples

```python
# Find a paper, read its abstract.
search(kind='paper', q='photocatalytic NOx reduction')
get(kind='paper', id='abazari2024design', view='abstract')

# Already have a DOI? Address by DOI directly.
get(kind='paper', id='10.1038/nature10352')
get(kind='paper', id='10.1038/nature10352', view='bibtex')

# Paginate.
search(kind='paper', q='photocatalysis', page=2)

# Make a todo, mark a different one done.
put(kind='todo', text='Review section 3 of abazari2024design.',
    tags=['PRIO:high'])
tag(kind='todo', id=122, add=['STATUS:done'])

# Quick calculation; real-world fact.
get(kind='calc', q='42 * 365')                # → 15330        (free)
get(kind='math', q='speed of light in km/h')  # → 1.079e9 km/h (paid)
```

## See also

```python
get(kind='skill', id='precis-search-help')   # search mechanics
get(kind='skill', id='precis-tags')          # axis vocabulary
get(kind='skill', id='precis-relations')     # link vocabulary
get(kind='skill', id='precis-cache')         # paid-tool caching, TTLs
get(kind='skill', id='precis-paper-help')    # paper views, citation export
get(kind='skill', id='precis-files-help')    # shared file-backed address grammar
get(kind='skill', id='precis-toc-help')      # TOC navigation, sub-range zoom
get(kind='skill', id='precis-random-help')   # random corpus pick
```
