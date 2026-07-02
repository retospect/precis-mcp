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

The **Example id** column shows the canonical **handle** (`<2-char type
code><decimal id>`) for store-backed kinds — what get/search output hands you to
paste back. Legacy slugs / numeric ids still resolve on input. File-backed kinds
(`skill`, `python`, and the `markdown`/`plaintext`/`tex` file mirror) address by
name/path.

| Kind | Example id | What | Needs |
|---|---|---|---|
| `paper` | `pa5` (slug `abazari2024design` still resolves) | Ingested research paper | store |
| `patent` | `pt40` (DOCDB `ep1234567b1` still resolves) | EPO OPS patent record | store |
| `cfp` | `cf3` (slug `nsf-25-501` still resolves) | Call-for-proposal / requirements doc — a read-only ingested PDF a proposal must satisfy. Same reader as `paper` but **spec role**: NEVER cited as evidence. Link to a proposal project with `link(rel='has-requirement')`. See `precis-proposal-help`. | store |
| `skill` | `precis-overview` | Agent how-to (you're reading one) | — |
| `oracle` | `or7` (slug `stoic` still resolves) | Curated wisdom-tradition entry | store |
| `conv` | `co12` (slug `2026-04-26-spec` still resolves) | Past conversation | store |
| `pres` | `pr5` (slug `2026-06-talk-foo` still resolves) | Slide deck or unpublished writeup | store |
| `markdown` | `notes--meeting` | A `.md` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `plaintext` | `notes--log` | A `.txt` / `.log` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `tex` | `chapters--intro` | A `.tex` file (section-aware blocks + `/toc`) | `PRECIS_ROOT` |
| `draft` | `dr3` (slug `nanotrans` still resolves) | Editable, chunk-native document — the living source of a project's write-up; chunks reorder/edit in place, exports to LaTeX/PDF/Word. Chunks addressed by `¶<handle>`. See `precis-draft-help`, ADR 0033. | store |
| `cad` | `cd7` (slug `flange` still resolves) | Parametric solid-model design — a boolean DAG of placed analytic primitives authored as a text node-list (`cyl:r3h12`, `box:w40d20h10`). Probed analytically (point/ray/arc/section) and related (clearance/interference/translational DOF); no meshing in the design loop. Nodes addressed by `ca<handle>`. See `precis-cad-help`, ADR 0041. | store |
| `structure` | `st7` (slug `pd111` still resolves) | Atomistic cell + bond-graph design for DFT / molecular modelling — a periodic cell filled with atoms (`a<El><n>` labels) + an explicit bond graph, authored as typed ops (`add_atom`, `add_bond`, `constrain`, `relax`). Probed as a graph + numbers (neighbours / coordination / line / plane / sphere / path / rings / fragments / diff / pov), relaxed on a fidelity ladder (`clean`→`ml`→DFT), exported POSCAR/extXYZ/CIF; no pixels. Atoms addressed `st<id>#a<El><n>`. See `precis-structure-help`, ADR 0043. | store |
| `pcb` | `pb7` (slug `sensor-node` still resolves) | Electronics/PCB design — a netlist + placement graph authored in batch and read as a traversable graph, never pixels. Pick JLCPCB-assemblable parts (`kind='part'`), place to minimise crossed wires, export BOM/CPL/DSN + route with Freerouting; datasheets via `kind='datasheet'`. Instances addressed `pb<id>#U1` (→ pins → nets → neighbours). See `precis-pcb-help`, ADR 0042. | store |
| `python` | `precis::precis.cli.main` | Symbol or file in a configured Python repo | `PRECIS_PYTHON_ROOTS` |
| `folder` | `fo12` (int `12` still resolves) | Organizational container for authored artifacts (draft / structure / cad / todo roots / folders) — single-parent placement via `link(rel='parent')`; `search(folder=...)` scopes to the subtree. Folders organize what you MAKE; papers / memories / alerts stay out. See `precis-folder-help`, ADR 0045. | store |
| `todo` | `td122` (int `122` still resolves) | A task in the hierarchical tree (Slice 1–5). Branches read as outcomes; leaves as next actions. See `precis-tasks-help`. | store |
| `memory` | `me47` (int `47` still resolves) | Agent note / scratchpad | store |
| `gripe` | `gr9` (int `9` still resolves) | Annoyance / niggle | store |
| `alert` | `al38260` (int `38260` still resolves) | Machine-detected ops / health condition (spin loop, orphan, stalled recurring). Raised by background passes, deduped + auto-resolved; surfaced by the `/alerts` web tab, **not** semantic search. See `precis-alert-help`. | store |
| `agentlog` | `ag38312` (int `38312` still resolves) | Run-attribution record — one per agentic run (plan_tick / operator / chat) that touched the corpus. Carries the assembled prompt + `touched` links to every chunk it wrote; walk a suspicious chunk back to its run. GC'd past a retention window; **not** semantic search. See `precis-agentlog-help`. | store |
| `flashcard` | `fc204` (int `204` still resolves) | Flashcard (SM-2 spaced rep) | store |
| `citation` | `ci18` (int `18` still resolves) | Verified claim → source quote | store |
| `finding` | `fi73` (int `73` still resolves) | Chain-of-evidence head over a citation chase | store |
| `orcid` | `orcid:0000-0002-1825-0097` | Researcher identity (ORCID): resolves + stores an author node (dossier), links held works + reports missing ones (LLM-gated `enqueue=`), and is the `authored` link hub. See `precis-orcid-help`, ADR 0039. | `ORCID_CLIENT_ID` |
| `job` | `jo55` (int `55` still resolves) | Execution attempt of a todo intent. **New jobs require `parent_id` pointing at a `kind='todo'`** — see `precis-job-help` + `precis-dispatch-help`. | store |
| `cron` | `cr42` (int `42` still resolves) | Push-notification scheduler — fires a payload to an external conversation (asa-bot Discord) at the scheduled time. **Different use case** from `level:recurring` todos (which pull recurring work into the doable queue); both kinds kept on purpose. See `precis-cron-help`, `precis-recurring-help`, ADR 0030. | store |
| `message` | `ms11` (int `11` still resolves) | Proactive outbound (Discord post) | store |
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
| `calc` | Local SymPy: exact arithmetic, calculus (integrals/derivatives/ODEs), solve, linear algebra; trig in degrees by default. See `precis-calc-help`. | `2+3*4` | free |
| `math` | Wolfram Alpha: facts, world data | `population of Ireland` | paid |
| `youtube` | Transcript fetch | `dQw4w9WgXcQ` | free |
| `web` | Fetch + extract a URL | `https://example.com/page` | free |
| `wikipedia` | Resolve + fetch one Wikipedia article (on-demand; fenced from default search via `ORIGIN:wikipedia`) | `CRISPR gene editing` | free |
| `websearch` | Perplexity Sonar: fast factual | `latest perovskite results` | paid |
| `perplexity-reasoning` | Perplexity Sonar Reasoning Pro | `compare DAC and BECCS` | paid |
| `perplexity-research` | Perplexity Sonar Deep Research | `mechanism of NOxRR` | paid |

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
| `pa<id>` | the whole ref by handle (e.g. `pa5`); legacy `slug` still resolves |
| `pc<id>` | one chunk by handle (e.g. `pc40`) — what output now shows |
| `slug~N` | chunk N (legacy form; still resolves on input) |
| `slug~A..B` | chunk range A..B (inclusive) — ranges keep the slug form |
| `slug/toc` | TOC of the ref (= `view='toc'`) |
| `slug~A..B/toc` | sub-TOC, segments within the range |
| `slug~A..B`, `view='toc'` | same as `slug~A..B/toc` |

A single chunk is now addressed by its handle (`pc<chunk_id>`); ranges
stay `slug~A..B` since a handle names one chunk, not a span. See
`precis-addressing-help` for the full scheme.

Currently TOC-capable: `paper`, `skill`. Other kinds pick up the
grammar as their handlers wire `chunks_for_toc`.

Three views work on **every** id-addressable ref kind: `view='links'`
(the link graph), `view='log'` (the `ref_events` trail), and
`view='raw'` (the verbatim record — every column **plus the full `meta`
JSON**). Reach for `raw` to debug behaviour the default render hides —
e.g. a todo's `meta.executor` / `meta.schedule` / `meta.auto_check`.

## How do I find the right skill?

```python
get(kind='skill', id='toc')                  # browse every skill, one-line synopsis
search(kind='skill', q='your goal')          # fuzzy lookup, e.g. 'spaced repetition'
get(kind='skill', id='precis-help')          # what kinds + verbs are live in this build
get(kind='skill')                            # list every active skill
```

`precis-toc` is the long-form alias for `id='toc'`.

## How are things addressed?

Input still accepts a slug (papers, drafts, …) or a numeric id (memories,
todos, …); content is found with `q=`. **Output now shows one universal
**handle** (ADR 0036):** `<2-char type code><decimal id>` — `pa5` a paper,
`pc10` a paper chunk, `me42` a memory, `td158` a todo — with the 2-char prefix
telling you the kind (so `get(id='pa5')` needs no `kind=`). It is the thing to
copy back into `get` / `link` / `like` / `source_handle`. See
`get(kind='skill', id='precis-addressing-help')` for the handle format, the
relative grammar (`+1`/`-1` sibling, `^` parent, `lo..hi` span), and the full
2-char type-code table.

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
| `precis-proposal-help` | Write a proposal against a `kind='cfp'` call — intake, requirement link, section-by-section drafting, word-count checks |
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
get(kind='skill', id='precis-folder-help')   # folders, placement, folder= search scope
```
