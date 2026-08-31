---
id: precis-overview
title: precis — seven verbs, one address scheme
summary: top-level orientation — seven verbs, address scheme, kinds table, skill index
answers:
  - what verbs does precis support?
  - when do I use get vs search vs put?
  - how do I address a specific chunk or section of a ref?
  - what's the difference between id= and q=?
  - which skill do I need for a given kind?
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
paste back. File-backed kinds
(`skill`, `python`, `md`, and the `markdown`/`plaintext`/`tex` file mirror) address by
name/path.

| Kind | Example id | What | Needs |
|---|---|---|---|
| `paper` | `pa5` | Ingested research paper | store |
| `patent` | `pt40` | EPO OPS patent record | store |
| `cfp` | `cf3` | Call-for-proposal / requirements doc — read-only, spec role only, never cited as evidence. Link a proposal with `link(rel='has-requirement')`. See `precis-proposal-help`. | store |
| `skill` | `precis-overview` | Agent how-to (you're reading one) | — |
| `oracle` | `or7` | Curated wisdom-tradition entry | store |
| `conv` | `co12` | Past conversation | store |
| `pres` | `pr5` | Slide deck or unpublished writeup | store |
| `markdown` | `notes--meeting` | A `.md` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `plaintext` | `notes--log` | A `.txt` / `.log` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `tex` | `chapters--intro` | A `.tex` file (section-aware blocks + `/toc`) | `PRECIS_ROOT` |
| `draft` | `dr3` | Editable, chunk-native document — the living source of a project's write-up; exports to LaTeX/PDF/Word. See `precis-draft-help`. | store |
| `plan` | `po3` | A thread's reasoning outline — hierarchical todo-list + notes, never exported. One per project (`plan-of` link). See `precis-plan-help`. | store |
| `figure` | `fg7` | Interactive SVG canvas you draw *with* the model — a chunk-tree, never exported, rendered in the browser (`/figure`). See `precis-figure-help`. | store |
| `mermaid` | `mm7` | A mermaid diagram you draw *with* the model (flowchart / sequence / state / class …), never exported. Web editor `/mermaid`; each diagram type has a `precis-mermaid-<type>` skill. See `precis-mermaid-help`. | store |
| `cad` | `cd7` | Parametric solid-model design — a boolean DAG of placed analytic primitives authored as a text node-list (`cyl:r3h12`, `box:w40d20h10`); no meshing in the design loop. See `precis-cad-help`. | store |
| `structure` | `st7` | Atomistic cell + bond-graph design for DFT / molecular modelling — typed ops (`add_atom`, `add_bond`, `constrain`, `relax`), relaxed on a fidelity ladder, exported POSCAR/extXYZ/CIF. See `precis-structure-help`. | store |
| `nm` | `nm12` or `rotax1` | Nanomachine — hierarchical building blocks with envelopes/ports/connects/threading/DOF over `structure` atoms (rotaxanes, molecular motors). **Dark**, gated by the `nm.enabled` setting. See `precis-nm-help`. | store, `nm.enabled` setting |
| `pcb` | `pb7` | Electronics/PCB design — netlist + placement graph, read as a traversable graph, never pixels. Parts via `kind='part'`, datasheets via `kind='datasheet'`. See `precis-pcb-help`. | store |
| `material` | `ma7` | Engineering material properties store — sourced values per property, canonical-units-only; `search(property=, min=, max=)` filters by range. See `precis-material-help`. | store |
| `component` | `cp7` | General procurable-part store (bolt/hose/pipe/beam/gasket/bearing/adhesive/electronic part) — sourced per-spec values, canonical-units-only. Distinct from `part` (the JLCPCB/LCSC ingest-only catalog). See `precis-component-help`. | store |
| `python` | `precis::precis.cli.main` | Symbol or file in a configured Python repo | `PRECIS_PYTHON_ROOTS` |
| `md` | `docs/backlog/some-item.md~Motivation` | Read-only, DB-free hybrid search over configured markdown roots (docs, backlog, skills prose) | `PRECIS_MD_ROOTS` |
| `folder` | `fo12` | Organizational container for authored artifacts — single-parent placement via `link(rel='parent')`; `search(folder=...)` scopes to the subtree. See `precis-folder-help`. | store |
| `todo` | `td122` | A node in the hierarchical todo tree. Branches read as outcomes; leaves as next actions. See `precis-todo-tree-help`. | store |
| `memory` | `me47` | Agent note / scratchpad | store |
| `gripe` | `gr9` | Annoyance / niggle | store |
| `alert` | `al38260` | Machine-detected ops / health condition, deduped + auto-resolved, surfaced by the `/alerts` web tab — not semantic search. See `precis-alert-help`. | store |
| `agentlog` | `ag38312` | Run-attribution record — one per agentic run that touched the corpus; `touched` links to every chunk it wrote. GC'd past a retention window; not semantic search. See `precis-agentlog-help`. | store |
| `anki` | `ak204` | Spaced-repetition cloze card (`{{c1::…}}`) that lives in the corpus and syncs to AnkiWeb. See `precis-anki-help`. | store |
| `concept` | `cn88` | A node in the learner's personal knowledge graph — a term with a continuous mastery field and typed edges (`has-prerequisite`/`analogy-of`/`contrasts-with`) to other concepts, promoted from paper glossaries. | store |
| `quest` | `qu7` | A perpetual, unachievable striving that pulls work + knowledge into its service — never `done`; work `serves` it (a DAG above the todo tree). See `precis-quest-help` (mechanics), `precis-quest-writing-help` (writing a good striving). | store |
| `llm` | `lm7` (model slug `claude-opus-4-8` also resolves) | A model catalog card — one ref per model, capability prose embedded so `search(kind='llm', q='careful SQL')` matches on capability. Read-only, machine-maintained. See `precis-llm-help`. | store |
| `citation` | `ci18` | Verified claim → source quote | store |
| `finding` | `fi73` | Chain-of-evidence head over a citation chase; a `TAPROOT:claim`-tagged finding is a cross-paper claim hub (see `precis-taproot-help`) | store |
| `orcid` | `orcid:0000-0002-1825-0097` | Researcher identity (ORCID): resolves + stores an author dossier, links held works + reports missing ones (LLM-gated `enqueue=`), and is the `authored` link hub. See `precis-orcid-help`. | `ORCID_CLIENT_ID` |
| `job` | `jo55` | Execution attempt of a todo intent. **New jobs require `parent_id` pointing at a `kind='todo'`** — see `precis-job-help` + `precis-minter-help`. | store |
| `message` | `ms11` | Proactive outbound (Discord post) | store |
| `email` | *(no handle — live IMAP adapter)* | Live, read-only mailbox browse over IMAP — mirrors nothing. `get(kind='email')` lists recent mail; `id='INBOX'` a folder; `id='INBOX/<uid>'` reads one message; `account='addr@host'` picks among configured accounts. Never marks mail `\Seen`. | store |
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
| `calc` | Local SymPy: exact arithmetic, calculus (integrals/derivatives/ODEs), solve, linear algebra; trig in degrees by default; **+ local unit conversion** (`3 ft to m`, disambiguates ton/gallon/oz). See `precis-calc-help`. | `2+3*4` · `1 ton to kg` | free |
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

## How do I inspect a ref's raw state, links, or event log?
## What views work on todo/memory/gripe/finding/job/... refs?

Three inspection views work on the **numeric-ref kinds** (`todo`,
`memory`, `gripe`, `finding`, `job`, `anki`, `citation`, `folder`,
`alert`, `agentlog`, `message`): `view='links'` (the link graph),
`view='log'` (the event trail), and `view='raw'` (the verbatim record
— every column **plus the full `meta` JSON**). Reach for `raw` to
debug behaviour the default render hides — e.g. a todo's
`meta.executor` / `meta.schedule` / `meta.auto_check`. Slug/file/compute
kinds (`paper`, `draft`, `cad`, `structure`, `pcb`, `tex`, `markdown`, …)
don't inherit these — each exposes its own view set instead (e.g. `paper`
has `view='log'` but not `links`/`raw`; a bad view returns the kind's
option list).

For handle format, sibling/parent navigation (`+1`/`-1`/`^`), and the
full 2-char type-code table, see `precis-addressing-help`.

## How do I find the right skill?

```python
get(kind="skill", id="toc")  # browse every skill, one-line synopsis
search(kind="skill", q="your goal")  # fuzzy lookup, e.g. 'spaced repetition'
get(kind="skill", id="precis-help")  # what kinds + verbs are live in this build
get(kind="skill")  # list every active skill
```

`precis-toc` is the long-form alias for `id='toc'`.

## The todo tree — task substrate

The todo tree is the unified surface for *intent*, *execution*,
*scheduling*, and *review* over the corpus:

| Skill | What it teaches |
|---|---|
| `precis-todo-tree-help` | Tree shape (strategic/tactical/subtask), claim/release/done, doable view rules |
| `precis-decomposition-help` | The GTD interrogation: when to split, when to block, when to wait |
| `precis-auto-todo-help` | Wait-for-condition leaves via `meta.auto_check` |
| `precis-recurring-help` | `meta.schedule` format + the **Watches** umbrella |
| `precis-minter-help` | When to set `meta.executor` on a todo so a `kind='job'` runs under it |
| `precis-job-help` | The job substrate. New jobs require `parent_id` pointing at a todo |
| `precis-fix-gripe-help` | First concrete job_type, end-to-end recipe |
| `precis-proposal-help` | Write a proposal against a `kind='cfp'` call — intake, requirement link, section-by-section drafting, word-count checks |
| `precis-nursery-help` | Per-minute SQL-only review tier — incoherence + worker-health, `critical` pages |
| `precis-health-digest-help` | Hourly slow-rot liveness digest — curated/derived checks, daily/on-degradation push |
| `precis-wikipedia-help` | On-demand Wikipedia lookup + the `ORIGIN:wikipedia` search fence |

PRIO sort key + 1/N rotation across active strategics + dedup-aware
nursery / structural / deep reviewers are the operational discipline
on top.

## Worked examples

```python
# Find a paper, read its abstract.
search(kind="paper", q="photocatalytic NOx reduction")
get(kind="paper", id="abazari2024design", view="abstract")

# Already have a DOI? Address by DOI directly.
get(kind="paper", id="10.1038/nature10352")
get(kind="paper", id="10.1038/nature10352", view="bibtex")

# Paginate.
search(kind="paper", q="photocatalysis", page=2)

# Make a todo, mark a different one done.
put(kind="todo", text="Review section 3 of abazari2024design.", tags=["PRIO:high"])
tag(kind="todo", id=122, add=["STATUS:done"])

# Quick calculation; unit conversion (both local + free); real-world fact.
get(kind="calc", q="42 * 365")  # → 15330        (free)
get(kind="calc", q="3 ft to m")  # → 0.9144 m     (free, local — not Wolfram)
get(kind="math", q="speed of light in km/h")  # → 1.079e9 km/h (paid)
```

## Overloaded words — which one do you mean?

A few short tokens carry several unrelated meanings across the system. If one
shows up in output and is ambiguous:

- **tier** — how *broad* a paper search is (`good=True` = deep, `precis-search-help`),
  OR a reviewer class (nursery/structural/deep), OR an LLM capability band.
- **card** — a searchable **embedding** of a ref (why a `quest`/`concept`/`llm`
  ref shows up in `search`), OR an **Anki** flashcard (`precis-anki-help`).
- **role** — `ROLE3:*` tags classify a *chunk's* content (own/background/
  furniture, the citation-grounding filter); a kind's *citability* is a separate
  thing. See `precis-tags`.
- **lane** — as a `todo`/`job` scheduling concept it's legitimate contract
  vocabulary (`precis-job-help`, `precis-minter-help`); any other, ad-hoc
  sense of "lane" isn't part of the API.

## See also

```python
get(kind="skill", id="precis-search-help")  # search mechanics
get(kind="skill", id="precis-tags")  # axis vocabulary
get(kind="skill", id="precis-relations")  # link vocabulary
get(kind="skill", id="precis-cache")  # paid-tool caching, TTLs
get(kind="skill", id="precis-paper-help")  # paper views, citation export
get(kind="skill", id="precis-files-help")  # shared file-backed address grammar
get(kind="skill", id="precis-addressing-help")  # handle format, relative grammar, type codes
get(kind="skill", id="precis-toc-help")  # TOC navigation, sub-range zoom
get(
    kind="skill", id="precis-fisheye-help"
)  # view='fisheye'/'fisheye+1hop' — read a chunk with its neighborhood
get(kind="skill", id="precis-random-help")  # random corpus pick
get(kind="skill", id="precis-folder-help")  # folders, placement, folder= search scope
get(kind="skill", id="precis-taproot-help")  # cross-paper claim hubs, living citation
get(
    kind="skill", id="precis-taproot-mint-help"
)  # author/mint/sharpen/merge a claim hub
get(
    kind="skill", id="precis-taproot-backfill-help"
)  # batch-convert [pc]/[pa] cites into hub cites
get(
    kind="skill", id="precis-notation-canon"
)  # how to spell numbers/units in a claim sentence (blocks at approve)
get(
    kind="skill", id="precis-gripe-help"
)  # hit a bug / tool friction? file a gripe (search existing first)
get(
    kind="skill", id="precis-audio-help"
)  # narrate a draft to audio: voice score + pronunciation lexicon
get(
    kind="skill", id="precis-lab-help"
)  # in-silico lab: chain route/protein/structure/literature toward a research goal
```
