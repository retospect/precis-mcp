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
paste back. File-backed kinds
(`skill`, `python`, and the `markdown`/`plaintext`/`tex` file mirror) address by
name/path.

| Kind | Example id | What | Needs |
|---|---|---|---|
| `paper` | `pa5` | Ingested research paper | store |
| `patent` | `pt40` | EPO OPS patent record | store |
| `cfp` | `cf3` | Call-for-proposal / requirements doc — a read-only ingested PDF a proposal must satisfy. Same reader as `paper` but **spec role**: NEVER cited as evidence. Link to a proposal project with `link(rel='has-requirement')`. See `precis-proposal-help`. | store |
| `skill` | `precis-overview` | Agent how-to (you're reading one) | — |
| `oracle` | `or7` | Curated wisdom-tradition entry | store |
| `conv` | `co12` | Past conversation | store |
| `pres` | `pr5` | Slide deck or unpublished writeup | store |
| `markdown` | `notes--meeting` | A `.md` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `plaintext` | `notes--log` | A `.txt` / `.log` file under `PRECIS_ROOT` | `PRECIS_ROOT` |
| `tex` | `chapters--intro` | A `.tex` file (section-aware blocks + `/toc`) | `PRECIS_ROOT` |
| `draft` | `dr3` | Editable, chunk-native document — the living source of a project's write-up; chunks reorder/edit in place, exports to LaTeX/PDF/Word. Chunks addressed by `dc<chunk_id>`. See `precis-draft-help`, ADR 0033. | store |
| `plan` | `po3` | A thread's reasoning outline — a hierarchical todo-list + notes on the same chunk-tree substrate as `draft`, but a **distinct kind that is NEVER exported** (`corpus_role='none'`). Rendered whole with `[open]`/`[wip]`/`done:` status markers, `?`/`⚠` belief flags, and a model-owned `▸` you-are-here cursor. Nodes addressed `pe<chunk_id>`. One plan per project (`plan-of` link). See `precis-plan-help`, ADR 0051 §2b. | store |
| `figure` | `fg7` | Interactive **SVG canvas** you draw *with* the model — a slug-addressed chunk-tree on the `draft` substrate, but a **distinct kind that is NEVER exported** (`corpus_role='none'`). Three model-owned documents: the SVG source (a `figure_node` chunk, `fn<id>`), a shared vocabulary (`figure_vocab`, embedded — high-level, human-facing), and implementation notes (`figure_notes`, private, not embedded); chat persists as `figure_turn`. Whole-source-rewrite edits with two mechanical lints (compile + out-of-bounds); model-authored SVG is sanitized (no script/foreignObject/external-href). Rendered in the browser (`/figure`), never rasterized server-side yet. See `precis-figure-help`, migration 0057. | store |
| `mermaid` | `mm7` | A **mermaid diagram** you draw *with* the model (flowchart / sequence / state / class …) — the **second instance of the shared diagram core** beside `figure` (ADR 0057): same draw-with-me turn loop + three docs (`mermaid_node`/`mermaid_vocab`/`mermaid_notes` + `mermaid_turn`), addressed `mm<ref>`/`mn<chunk>`, never exported (`corpus_role='none'`). Validated / rendered / exported **pure-Python via `mermaidx`** (real mermaid.js in embedded QuickJS + resvg — no Node/Chromium/container). **Nodes bind to the chunks they depict** (`depicts` link, ADR 0057) so the model edits with the linked sources in hand. Web editor `/mermaid`. A first-class kind (the `[mermaid]` extra provides the engine). Renders flowchart / sequence / class / state / ER / journey / quadrant / requirement / gitGraph / timeline / xychart / mindmap (gantt/pie/sankey/C4/block not yet) — each type has a discoverable `precis-mermaid-<type>` skill. See `precis-mermaid-help`, migration 0066. | store |
| `cad` | `cd7` | Parametric solid-model design — a boolean DAG of placed analytic primitives authored as a text node-list (`cyl:r3h12`, `box:w40d20h10`). Probed analytically (point/ray/arc/section) and related (clearance/interference/translational DOF); no meshing in the design loop. Nodes addressed by `ca<handle>`. See `precis-cad-help`, ADR 0041. | store |
| `structure` | `st7` | Atomistic cell + bond-graph design for DFT / molecular modelling — a periodic cell filled with atoms (`a<El><n>` labels) + an explicit bond graph, authored as typed ops (`add_atom`, `add_bond`, `constrain`, `relax`). Probed as a graph + numbers (neighbours / coordination / line / plane / sphere / path / rings / fragments / diff / pov), relaxed on a fidelity ladder (`clean`→`ml`→DFT), exported POSCAR/extXYZ/CIF; no pixels. Atoms addressed `st<id>#a<El><n>`. See `precis-structure-help`, ADR 0043. | store |
| `pcb` | `pb7` | Electronics/PCB design — a netlist + placement graph authored in batch and read as a traversable graph, never pixels. Pick JLCPCB-assemblable parts (`kind='part'`), place to minimise crossed wires, export BOM/CPL/DSN + route with Freerouting; datasheets via `kind='datasheet'`. Instances addressed `pb<id>#U1` (→ pins → nets → neighbours). See `precis-pcb-help`, ADR 0042. | store |
| `material` | `ma7` | CRC-handbook-style engineering material properties store — a slug entity (name/aliases/class) plus per-property **sourced** values in a typed, growable property registry (`core` curated + `proposed` mintable at write time). **v1 is canonical-units-only**: `put(property=, value=, unit=)` rejects a unit that isn't the property's canonical one (named, no conversion). `get(id=)` is the handbook page grouped by property; `view='properties'` lists the registry. `search(property=, min=, max=, maturity=)` is the range-filter read ("materials with thermal_conductivity < 0.05"). See `precis-material-help`. | store |
| `component` | `cp7` | General procurable-part store (bolt/hose/pipe/beam/gasket/bearing/adhesive/electronic part) — a slug entity (name/**category**/mpn/manufacturer) plus per-spec **sourced** values in a typed, growable, category-scoped spec registry (universal specs like `unit_cost`/`mass` apply to any component; category-scoped ones like `bore_diameter` don't). `category=` required on create (mints a `proposed` category if unknown). `put(spec=, value=, unit=)` is canonical-units-only, same rule as `material`; `made_of='material:<slug>'` links the material it's made of. `get(id=)` is the component page; `view='specs'`/`'categories'` list the registries. `search(spec=, min=, max=, maturity=, category=)` is the range-filter read ("hoses with max_working_pressure >= 20 MPa"). Deliberately distinct from `part` (the JLCPCB/LCSC ingest-only catalog). See `precis-component-help`. | store |
| `python` | `precis::precis.cli.main` | Symbol or file in a configured Python repo | `PRECIS_PYTHON_ROOTS` |
| `folder` | `fo12` | Organizational container for authored artifacts (draft / structure / cad / todo roots / folders) — single-parent placement via `link(rel='parent')`; `search(folder=...)` scopes to the subtree. Folders organize what you MAKE; papers / memories / alerts stay out. See `precis-folder-help`, ADR 0045. | store |
| `todo` | `td122` | A task in the hierarchical tree (Slice 1–5). Branches read as outcomes; leaves as next actions. See `precis-tasks-help`. | store |
| `memory` | `me47` | Agent note / scratchpad | store |
| `gripe` | `gr9` | Annoyance / niggle | store |
| `alert` | `al38260` | Machine-detected ops / health condition (spin loop, orphan, stalled recurring). Raised by background passes, deduped + auto-resolved; surfaced by the `/alerts` web tab, **not** semantic search. See `precis-alert-help`. | store |
| `agentlog` | `ag38312` | Run-attribution record — one per agentic run (plan_tick / operator / chat) that touched the corpus. Carries the assembled prompt + `touched` links to every chunk it wrote; walk a suspicious chunk back to its run. GC'd past a retention window; **not** semantic search. See `precis-agentlog-help`. | store |
| `anki` | `ak204` | Spaced-repetition **cloze** card (`{{c1::…}}`) that lives in the corpus and syncs to AnkiWeb. Anki owns scheduling — no SM-2 here. Supersedes the retired `flashcard`. See `precis-anki-help`. | store |
| `concept` | `cn88` | A node in the learner's personal knowledge graph (reading-prep loop): a term with a continuous mastery field, embeddable definition, and typed edges (`has-prerequisite`/`analogy-of`/`contrasts-with`) to other concepts. Promoted from paper glossaries. Ships dark. See `docs/design/reading-prep-loop.md`. | store |
| `quest` | `qu7` | A **perpetual, unachievable striving** (the medieval Grail sense) that pulls work + knowledge into its service. Never `done` — lifecycle `active/dormant/abandoned`. Work `serves` it (a DAG of strivings above the todo tree); an append-only WORM logbook is the deed + cost ledger. Priority (`PRIO:` tag) flows down the `serves` DAG to reweight rotation / acquisition / reading (slice 2, live). See `precis-quest-help` (verbs/mechanics) and `precis-quest-writing-help` (judgment: writing a good striving). | store |
| `llm` | `lm7` (model slug `claude-opus-4-8` also resolves) | A **model catalog card** — one ref per model (`emits_card`, so the capability prose is a vector; `search(kind='llm', q='careful SQL')` matches on capability). `meta` carries the facts: `model_id`, `tier_floor`, `offerings` (operating points), coarse 1–5 `capability` axes, `provenance`. A `llm_reconcile` pass keeps facts true against the live OpenRouter feed + flags proxy drift. Read-only, machine-maintained (`precis llm seed` / `reconcile`); never exported. Ships dark — empty catalog ⇒ today's behaviour (`Tier` stays the floor). See `docs/proposals/llm-catalog.md`, migration 0071. | store |
| `citation` | `ci18` | Verified claim → source quote | store |
| `finding` | `fi73` | Chain-of-evidence head over a citation chase | store |
| `orcid` | `orcid:0000-0002-1825-0097` | Researcher identity (ORCID): resolves + stores an author node (dossier), links held works + reports missing ones (LLM-gated `enqueue=`), and is the `authored` link hub. See `precis-orcid-help`, ADR 0039. | `ORCID_CLIENT_ID` |
| `job` | `jo55` | Execution attempt of a todo intent. **New jobs require `parent_id` pointing at a `kind='todo'`** — see `precis-job-help` + `precis-dispatch-help`. | store |
| `message` | `ms11` | Proactive outbound (Discord post) | store |
| `email` | *(no handle — live IMAP adapter)* | **Live, read-only mailbox browse** over IMAP — mirrors nothing (IMAP is source of truth). `get(kind='email')` lists recent mail; `id='INBOX'` a folder; `id='INBOX/<uid>'` reads one message; `account='addr@host'` picks among configured accounts. Never marks mail `\Seen`. Accounts configured via the `precis email` CLI (password in the vault). Send + injection-scan land in later slices. See `docs/design/email-kind.md`. | store |
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

## What's the shared address grammar?
## How do I address a chunk or a sub-range?
## What does slug~N mean?

| Form | Meaning |
|---|---|
| `pa<id>` | the whole ref by handle (e.g. `pa5`) |
| `pc<id>` | one chunk by handle (e.g. `pc40`) — what output now shows |
| `slug~A..B` | chunk range A..B (inclusive) — ranges keep the slug form |
| `slug/toc` | TOC of the ref (= `view='toc'`) |
| `slug~A..B/toc` | sub-TOC, segments within the range |
| `slug~A..B`, `view='toc'` | same as `slug~A..B/toc` |

A single chunk is now addressed by its handle (`pc<chunk_id>`); ranges
stay `slug~A..B` since a handle names one chunk, not a span. See
`precis-addressing-help` for the full scheme.

Currently TOC-capable: `paper`, `skill`. Other kinds pick up the
grammar as their handlers wire `chunks_for_toc`.

Three inspection views work on the **numeric-ref kinds** (`todo`,
`memory`, `gripe`, `finding`, `job`, `anki`, `citation`, `folder`,
`alert`, `agentlog`, `message`): `view='links'` (the link
graph), `view='log'` (the `ref_events` trail), and `view='raw'` (the
verbatim record — every column **plus the full `meta` JSON**). Reach for
`raw` to debug behaviour the default render hides — e.g. a todo's
`meta.executor` / `meta.schedule` / `meta.auto_check`. Slug/file/compute
kinds (`paper`, `draft`, `cad`, `structure`, `pcb`, `tex`, `markdown`, …)
don't inherit these — each exposes its own view set instead (e.g. `paper`
has `view='log'` but not `links`/`raw`; a bad view returns the kind's
option list).

## How do I find the right skill?

```python
get(kind="skill", id="toc")  # browse every skill, one-line synopsis
search(kind="skill", q="your goal")  # fuzzy lookup, e.g. 'spaced repetition'
get(kind="skill", id="precis-help")  # what kinds + verbs are live in this build
get(kind="skill")  # list every active skill
```

`precis-toc` is the long-form alias for `id='toc'`.

## How are things addressed?

Content is found with `q=`; refs are addressed by one universal
**handle** (ADR 0036): `<2-char type code><decimal id>` — `pa5` a paper,
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
- **lane / dispatch / plan** — internal task-engine words; you address work
  through todos + jobs (`precis-tasks-help`, `precis-dispatch-help`), not these.

(Developers: `docs/architecture/glossary.md` maps every coined/overloaded term to
its source file.)

## See also

```python
get(kind="skill", id="precis-search-help")  # search mechanics
get(kind="skill", id="precis-tags")  # axis vocabulary
get(kind="skill", id="precis-relations")  # link vocabulary
get(kind="skill", id="precis-cache")  # paid-tool caching, TTLs
get(kind="skill", id="precis-paper-help")  # paper views, citation export
get(kind="skill", id="precis-files-help")  # shared file-backed address grammar
get(kind="skill", id="precis-toc-help")  # TOC navigation, sub-range zoom
get(
    kind="skill", id="precis-fisheye-help"
)  # view='fisheye'/'fisheye+1hop' — read a chunk with its neighborhood
get(kind="skill", id="precis-random-help")  # random corpus pick
get(kind="skill", id="precis-folder-help")  # folders, placement, folder= search scope
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
