# Draft section styles — catalogue & drafts

> Status: **proposal** (2026-06-22). Companion to
> [ADR 0037](../decisions/0037-heading-styles-and-numbering-lock.md). This
> file defines the **section styles** (= skills) we want and drafts their
> bodies, across four genres — patents, research papers, review papers,
> animation scripts, books — plus shared cross-genre styles and silent
> separators.
>
> **Provisional.** The frontmatter schema is ADR 0037 open-Q3 and may
> change. These live here as one reviewable catalogue while the heading-
> style machinery is unbuilt; when it lands (ADR 0037 §0 v1) each block
> migrates to its own `src/precis/data/skills/precis-style-<slug>.md`
> file, which the MCP server serves like any skill.

## How a style works (recap of ADR 0037)

- A draft heading carries `meta.style` = **one skill slug**. Authoring
  that section surfaces that skill as the prompt.
- Styles are **self-contained** — no cascade. Each body bakes in
  everything the author needs, **including the document voice**. Shared
  phrasing is repeated, not inherited (intentional — fixable per-section).
- **Genre is `meta.workspace.doc_type`** (the shipped document-type
  picker): it leads the brief and selects a thin **scaffold skill** that
  lists the sections to create (as prose) and their styles, which the
  planner lays down (0033 genesis-from-brief).
- **v1 is prose only.** Every style below is a prose prompt the LLM
  follows; the LLM writes numbers (claim n, FIG. n, [n]) as prose.
  Behavior **render** code and the numbering engine are **additive
  expansions** (ADR 0037 §0). Correctness checks are **not** per-style
  code — they are a **review pass** (ADR 0037 §3a, reusing 0033's
  `Reviewer`) plus the style prompt's own discipline; available early,
  not an expansion.

## Frontmatter schema (provisional)

A style describes a **section** (a heading + its subtree). A *leaf*
(figure, claim, part, character) is **not** a style — it is a
`chunk_kind` (see "Chunk kinds" below). ADR 0037 §3, the test: has
children → section (style); a single thing you point at → leaf
(chunk_kind).

```yaml
style: <slug>            # the dispatch key; matches meta.style on a heading
role: root | section     # root = scaffold; section = a section prompt
archetype: prose | managed | separator   # ADR 0037 §3
manages: [<chunk_kind>…] # managed only — the leaf chunk_kind(s) this section owns
silent: true             # separator only — heading carries no title text
# behavior: <module>     # EXPANSION ONLY — names a render module (FIG. n, numeral subst.); omit for v1
```

There is **no `numbering:` on a section style** — a numbering series binds
to the **leaf (chunk_kind)** it counts, not the section (ADR 0037 §5). So
one managed section may own leaves of several kinds feeding several series
(the patent drawings registry `manages: [figure, part]` → `figures` and
`parts` series). Likewise there is **no `validate:`** — correctness is a
**review pass** (ADR 0037 §3a), not per-style code.

The body is the complete authoring prompt. The **standard-section list of
a root is prose in the body**, not a frontmatter field (ADR 0037 §1).

## Silent headings & separators

A heading may be **silent** — `archetype: separator`, `silent: true`, no
title text. It is a real heading chunk (owns the subtree after it, has a
`dc…`), but it contributes no TOC title and renders as a divider
glyph. The canonical case is the book `scene-break` (`* * *`); a
`part-divider` is the same shape. No payload, no number, no review — the
minimal archetype.

---

## Shared styles (cross-genre)

Reused by several genres; defined once. (The existing `term` glossary
style from ADR 0033 §9 is also reused as-is for technical glossaries.)

**Citation is *not* a style — it is an inline token.** A bibliographic
reference to a source elsewhere is the `[[pc…]]` marker (ADR 0033 §8)
dropped into *any* prose; the numbered `[n]` and the reference list are
**generated at export** from the `rel='cites'` links — there is no
"references section" to author and nothing to set on a heading. The skill
for *how to cite well* (cite the precise chunk, capture the supporting
quote, find-and-fetch a missing source — its slow keyword/summary
indexing runs as a background subtask) is a **task-skill**
(`precis-citation-help`), not a section style. A **verbatim quotation** —
a Mark-Twain epigraph, a pulled quote — is different again: inline, use
`\citequote{key}{verbatim}` (the token carrying the source words, 0033
§8); displayed, use a `blockquote`/`aside` chunk (0033 §9) or the book
`book-front-matter` epigraph. The only *section* in this family is a
curated **disclosure list** like `patent-prior-art` (below) — authored
content that selects which references to surface and how to render them.

`figure`, `character`, and `setting` are **not styles** — they are
**leaf chunk_kinds** (a figure, a character entry, a place entry are
things you *point at*, not sections). Their authoring guidance lives in
the **Chunk kinds** section below. The only shared *style* here is the
separator:

```markdown
---
style: scene-break
role: section
archetype: separator
silent: true
---
You are inserting a **scene break** — a silent structural divider between passages within a chapter (the `* * *` separator). It is a heading with **no title text**: it begins a new sibling passage but contributes nothing to the table of contents. Use it for a hard cut in time, place, or POV that does not warrant a new chapter. It carries no prose, no number, and no content of its own — the passages before and after are ordinary `scene` sections; this only marks the boundary. At render it becomes a centered break glyph (`* * *` in print/web, a `\bigskip` + asterism in LaTeX). Do not write anything in it; if you want to, you want a `scene` section, not a break.
```

---

## Chunk kinds (leaves — not styles)

A **leaf** is a thing you point at, addressed by a handle (`dc…`), typed
by a `chunk_kind` column, and governed by its enclosing section's style.
Leaves are **never** styles. The authoring guidance for a leaf is
surfaced by its **managing section's style** (or, for an inline figure,
the figure chunk_kind's default). v1 needs **no new chunk_kinds** —
`figure` and `term` already exist; the rest reuse `paragraph`/`term`.

> **`figure` is the umbrella — image *and* graph are one kind.** Per the
> shipped 0034 model, image / graph / plot **collapse into one `figure`
> chunk_kind** discriminated by `meta.figure.origin` (`original` /
> `third_party` = image; `own_graph` = graph + recipe). **Do not** use
> "figure" to mean "image only" — that's just `origin ∈ {original,
> third_party}`. (This is the live model on `main`; resolves the
> figure-vs-image thread clash.)

| chunk_kind | v1 storage | managed by / inserted | authoring note |
|---|---|---|---|
| `figure` | **exists + shipped** (0034 / migration `0035_chunk_blobs`: caption face + image blob in `chunk_blobs`, `meta.figure`) | inline in a paper; `patent-image-part` in a patent | the **umbrella** kind — *image and graph are the same `figure`*, distinguished by **`meta.figure.origin ∈ {original, own_graph, third_party}`** (live; `put(kind='draft', …, image=<base64>, origin=…)`, clearance gate `utils/figure_clearance.py`). An **image** = `original`/`third_party`; a **graph** = `own_graph` (+ a data/code recipe, ADR 0035). caption stands alone; asset attached not authored; `[[dc…]]` to reference; "Figure n"/"FIG. n" computed at render |
| `part` | reuse **`term`** (name + surface_forms + `meta.numeral`) | `patent-image-part` | a reference numeral = a named part; referenced from the description by `[[dc…]]` + noun phrase; numeral substituted at export |
| `claim` | reuse **`paragraph`** (v1) | `patent-claim` | one claim per leaf; antecedent basis; dependent claims reference `[[dc…]]`; number from order |
| `character` | reuse **`term`** | the cast section (book/script) | name + surface_forms + essence/appearance/voice/motivation/flaw/relations; a reference sheet, not a scene; referenced by `[[dc…]]` |
| `setting` | reuse **`term`** | the worldbuilding section (book/script) | name + surface_forms + sensory signature + function + rules; a reference sheet; referenced by `[[dc…]]` |
| `term` | **exists** (0033 §9) | a glossary section | short/long + surface_forms; the glossary entry |

**Expansion:** dedicated `claim`/`part` (and `character`/`setting`)
chunk_kinds, with type-specific managed render, when prose-over-`term`
stops sufficing (ADR 0037 §0 expansions). v1 stays on `paragraph`/`term`.

---

## Patents

Root + section styles for a utility patent application. (Drafting detail
from the patent application of ADR 0037 —
[`patent-drafting-merge.md`](./patent-drafting-merge.md).)

```markdown
---
style: patent
role: root
archetype: prose
---
You are scaffolding a **utility patent application**. The voice throughout is formal and impersonal: present tense for the invention ("the apparatus comprises…"), strict antecedent-basis discipline (introduce a thing with "a/an", refer back with "the/said"), the open transitional "comprising", and no marketing language, no first person, no hedging. Do not draft prose here; instead lay down the standard section headings in order, each with its style, then return so they can be filled:

- **Field of the Invention** — style `patent-description`
- **Background** — style `patent-description`
- **Summary** — style `patent-description`
- **Brief Description of the Drawings** — style `patent-image-part`
- **Detailed Description** — style `patent-description`
- **Claims** — style `patent-claim` (one chunk per claim)
- **Abstract** — style `patent-abstract`
- **Prior-Art / IDS Disclosures** — style `patent-prior-art`

Create all eight headings first as empty styled chunks, then fill them. Claims feed the `claims` numbering series; drawings/parts feed `figures` and `parts`. Cross-refer sections by `[[dc…]]`.
```

```markdown
---
style: patent-description
role: section
archetype: prose
---
You are writing a **descriptive section** of a patent (Field, Background, Summary, or Detailed Description). Voice: formal, impersonal, present tense for the invention; use the open transitional "comprising"; observe strict antecedent-basis — introduce each element with "a"/"an" on first mention, then "the" or "said" thereafter. No first person, no marketing claims.

Field: one or two sentences naming the technical domain. Background: the prior problem and its shortcomings, no admissions of obviousness. Summary: what the invention is and the advantages it confers, mirroring the broadest claim language. Detailed Description: walk every embodiment, describing each drawing part by handle plus its noun phrase — e.g. "the widget [[dc149]] is coupled to the housing [[dc151]]" — and state alternatives ("in some embodiments…").

Number paragraphs as prose in the bracketed form [0001], [0002], … incrementing in order. Refer to figures as "FIG. n" in prose and to other sections by `[[dc…]]`. Cite external corpus material as `[[pc…]]`. Managed numbering is an expansion (ADR 0037 §5); correctness is checked by a patent review pass (ADR 0037 §3a), not by you.
```

```markdown
---
style: patent-abstract
role: section
archetype: prose
---
You are writing the **Abstract** of a patent. Produce a single concise paragraph of no more than 150 words that states the technical disclosure: what the invention is and what it does. Voice: formal, impersonal, present tense ("An apparatus comprises…"). Begin with a noun phrase naming the invention, not with "This invention" or "The present disclosure". Avoid legalese, claim-style language, and "means for" beyond necessity; do not use "comprising" merely for ceremony. Include no reference numerals, no citations, and no drawing references. Summarise the broadest embodiment only — do not enumerate dependent features or list every element. Use plain technical nouns and a single uninterrupted paragraph. Do not reference other sections by handle; the abstract stands alone as a self-contained synopsis suitable for search and classification.
```

```markdown
---
style: patent-claim
role: section
archetype: managed
manages: [claim]
---
You are writing **one patent claim** — exactly one independent or dependent claim per chunk, as a single grammatical sentence. Structure: a preamble naming the category (e.g. "A method for…", "An apparatus comprising…"), the open transitional word "comprising", then the elements, each introduced with "a"/"an" on first appearance and referred back to with "the"/"said" thereafter — never break antecedent basis.

For an **independent** claim, recite the complete combination standalone. For a **dependent** claim, open by referencing its antecedent claim by handle and noun phrase — "The method of [[dc…]], further comprising…" — and add only the narrowing limitation. Indent elements as a list within the single sentence; end the whole claim with one period.

Use formal, impersonal, present-tense language; no marketing. The claim entity feeds the `claims` series — write it as positioned but do not assign a literal number (managed numbering is an expansion, ADR 0037 §5). Antecedent basis is **your** discipline while writing; residual breaks are caught by a patent review pass (ADR 0037 §3a), not by a validator you call. Cite corpus material, if ever, as `[[pc…]]`.
```

```markdown
---
style: patent-image-part
role: section
archetype: managed
manages: [figure, part]
---
You are writing the **drawings registry** — a single unified section holding two kinds of entity, the **figures** and the **reference numerals (parts)** shown on them. They belong together: a part exists *because* it is labelled on a drawing.

Figure entities: describe each figure in one sentence, "FIG. n shows…" / "FIG. n is a cross-sectional view of…", in order. Figure entities feed the `figures` series.

Part entities: register every reference numeral as a named entity — a noun phrase plus a brief description ("housing — the enclosure that retains the assembly"). Part entities feed the `parts` series; their numerals are display labels assigned at export. The Detailed Description refers to each part by `[[dc…]]` plus its noun phrase, so name parts here once, consistently, with antecedent-basis discipline. (Series bind to the entity, not this section — ADR 0037 §5 — which is why both live here cleanly.)

Voice: formal, impersonal, present tense. Drawing assets are stored per ADR 0034; actual CAD/figure generation is out of scope here — describe and register only, do not produce images. Managed numbering is an expansion (ADR 0037 §5); numeral/part-name consistency is checked by a patent review pass (ADR 0037 §3a).
```

```markdown
---
style: patent-prior-art
role: section
archetype: managed
manages: [reference]
---
You are writing the **Prior-Art / IDS Disclosures** section. List the documents material to patentability — external patents, published applications, and non-patent literature (papers). Reference each one directly as a corpus chunk via `[[pc…]]`; do not paraphrase a source you cannot cite. For each entry give the standard bibliographic handle: for patents, the publication number, inventor/assignee, and issue/publication date; for literature, authors, title, venue, and date — drawn from the cited chunk.

Voice: formal, neutral, factual; make no admission that any listed reference is prior art beyond the duty to disclose, and characterise relevance only sparingly if at all. Order patents before non-patent literature, each group chronologically.

This section is the source of record; the **Information Disclosure Statement (IDS) is a view rendered over these `[[pc…]]` references**, not a separately maintained list. Keep one disclosure per entry so the view can deduplicate cleanly. Rendering of the formal IDS form is an expansion (ADR 0037 §5).
```

---

## Research papers

```markdown
---
style: paper-research
role: root
archetype: prose
---
You are scaffolding an **original-research paper**. Do not write prose at this level; produce the section skeleton, then let each section's own style fill it. Create these headings in order, each with the named style: **Abstract** (`sci-abstract`), **Introduction** (`sci-introduction`), **Related Work** (`sci-related-work`), **Methods** (`sci-methods`), **Results** (`sci-results`), **Discussion** (`sci-discussion`), **Conclusion** (`sci-conclusion`). Cite sources with the inline `[[pc…]]` token (ADR 0033; the bibliography is generated at export — not a style), and use the shared `figure` style for figures.

The whole paper is written in scientific voice: formal, third person, precise, with claims hedged to the evidence — never overclaim. Established facts take present tense; what this work did takes past tense. Define each term on first use. Cross-reference the paper's own sections by `[[dc…]]` and cite corpus papers by the precise chunk `[[pc…]]`, capturing the supporting quote.

Workflow: first **outline** — draft a one-line intent for each section and confirm the through-line (problem → contribution → evidence → interpretation) is coherent — then fill the sections, ensuring the contributions claimed in the Introduction are exactly those demonstrated in Results.
```

```markdown
---
style: paper-review
role: root
archetype: prose
---
You are scaffolding a **review / survey paper**. Do not write prose here; build the section skeleton and let each section's style fill it. Create, in order: **Abstract** (`sci-abstract`), **Introduction** (`sci-introduction`), **Scope & Method** (`sci-methods`, adapted: state the survey's selection criteria, databases queried, inclusion/exclusion rules, and the time window, rather than an experimental protocol), several **thematic synthesis** sections (`sci-survey-section`, one per major theme or sub-area), an **Open Problems / Synthesis** section (`sci-survey-section`, turned toward unresolved questions and a forward agenda), and a **Conclusion** (`sci-conclusion`). It uses the inline `[[pc…]]` citation token (ADR 0033) and the shared `figure` style.

The paper is written in scientific voice: formal, third person, precise, claims hedged to the evidence. A survey's value is synthesis, not enumeration — the thematic sections must integrate sources into a coherent map of the field, not march paper-by-paper. Cross-reference your own sections by `[[dc…]]` and cite the precise chunk of each corpus paper by `[[pc…]]` with its supporting quote.

Workflow: first **outline** the thematic decomposition (the themes must be mutually distinct and jointly cover the scope you declared), then fill each section.
```

```markdown
---
style: sci-abstract
role: section
archetype: prose
---
You are writing the **Abstract** of a research paper. Produce a single structured paragraph of at most 250 words that is fully self-contained — a reader who sees only the abstract should grasp the work. Move through four beats without subheadings: (1) the **problem** and why it matters; (2) the **approach** taken; (3) the **key result**, stated with the most important concrete number or finding; (4) the **significance** — what this changes or enables. Do not cite anything: no `[[pc…]]`, no `[[dc…]]`, no references, no figures. Do not include information that appears nowhere in the body. Avoid undefined acronyms; if an abbreviation is unavoidable, expand it on first use. Formal, third person; present tense for the problem and significance, past tense for what this work did. No hype words ("novel", "revolutionary"); let the result carry the weight. Write the abstract last, after the body is settled, so it reflects what the paper actually demonstrates.
```

```markdown
---
style: sci-introduction
role: section
archetype: prose
---
You are writing the **Introduction** of a research paper. Open by motivating the problem: establish the broader context and why it matters, citing the precise chunks of foundational corpus papers as `[[pc…]]` with supporting quotes. Narrow steadily to the specific **gap** — what prior work leaves unresolved — and state it plainly; this gap is the paper's reason to exist. Then state the paper's **contributions** as a compact, near-bulleted enumeration ("This paper makes the following contributions: first, …; second, …"), each contribution concrete and verifiable, and each matched later by evidence in Results `[[dc…]]`. Close with a one-paragraph **roadmap** of the paper, referencing each downstream section by `[[dc…]]`. Formal, third person; present tense for established facts, future or present for what the paper will show. Hedge claims to the evidence and do not preview results you cannot deliver. Keep it to roughly three to five paragraphs.
```

```markdown
---
style: sci-related-work
role: section
archetype: prose
---
You are writing the **Related Work** section of a research paper. Organise the prior literature **thematically**, not as a serial annotated list — group works by the problem they address or the approach they take, and give each theme a coherent narrative that explains how its members relate, build on, or contradict one another. Cite the precise supporting chunk of each corpus paper as `[[pc…]]`, capturing the exact quote that grounds your characterisation; never attribute a claim you cannot quote. Be fair to prior work: state what it achieved before what it lacked. Close the section by **positioning this work against the gap** — make explicit which limitation of the existing literature this paper addresses and how it differs from the nearest prior approaches, without yet presenting results. Formal, third person; present tense for what other works do, past tense for what they did. Connect back to the gap framed in the Introduction `[[dc…]]` so the motivation stays continuous.
```

```markdown
---
style: sci-methods
role: section
archetype: prose
---
You are writing the **Methods** section of a research paper. Provide enough detail that a competent reader could **reproduce** the work: the materials, data, procedures, parameters, and analysis, in the order they were performed. Define all notation on first use, and present mathematics with LaTeX — inline as `$…$` and displayed as `$$…$$` — numbering displayed equations if you reference them later. State assumptions, hyperparameters, and any preprocessing explicitly; omit nothing a replicator would need and include nothing extraneous. Where a method follows or adapts prior work, cite the precise chunk `[[pc…]]` with its quote and say exactly what was changed. Refer to procedural figures or tables by `[[dc…]]`. Use past tense throughout — this describes what was done — and the passive or third person as is conventional. Do not report results or interpret them here; confine this section to how the work was carried out. Formal and precise; prefer concrete values to vague qualifiers.
```

```markdown
---
style: sci-results
role: section
archetype: prose
---
You are writing the **Results** section of a research paper. Report the findings plainly and in a logical order — typically following the structure of the Methods `[[dc…]]`. Tie every quantitative claim to the figure or table that shows it, referenced by `[[dc…]]`, and state numbers with their **units, uncertainty, and precision** (significant figures, error bars, confidence intervals, or p-values as appropriate). Report what was observed, including negative or null findings, without spin. Do **not** interpret, speculate, compare to prior work, or draw conclusions here — that belongs in the Discussion `[[dc…]]`; results are the evidence, kept separate from its reading. Avoid new citations unless naming a standard statistical method. Formal, third person, past tense for what was measured and present tense for what a figure shows ("Figure X shows…"). Let the data speak; do not editorialise with words like "remarkably" or "surprisingly". Ensure each contribution promised in the Introduction has corresponding evidence here.
```

```markdown
---
style: sci-discussion
role: section
archetype: prose
---
You are writing the **Discussion** section of a research paper. **Interpret** the results reported in `[[dc…]]` — what they mean, why they came out as they did, and how they answer the question posed in the Introduction `[[dc…]]`. Compare your findings against prior work, citing the precise chunks `[[pc…]]` with supporting quotes, noting both agreement and divergence and offering plausible explanations for the latter. State the **threats to validity and limitations** honestly: confounds, scope of generalisation, sample or measurement constraints, and assumptions that may not hold. Calibrate every claim to the strength of the evidence — distinguish what the data establish from what they merely suggest, and avoid overclaiming or extrapolating beyond the study. You may identify implications and open questions, but do not introduce new results. Formal, third person; present tense for interpretation and established facts, past tense for what this work found. Aim for a balanced reading that a sceptical reviewer would accept.
```

```markdown
---
style: sci-conclusion
role: section
archetype: prose
---
You are writing the **Conclusion** of a research paper. Restate the problem and the paper's central contribution in 1–2 sentences — no new results, no new citations. Summarise what was shown, name the principal limitation honestly, and end with one concrete direction for future work. Keep it to a paragraph or two. Formal, third person; present tense for established facts ("the method achieves…"), past tense for what this work did ("we trained…"). Synthesise; do not re-list every result. Reference the paper's own sections by `[[dc…]]` if needed.
```

```markdown
---
style: sci-survey-section
role: section
archetype: prose
---
You are writing one **thematic synthesis section** of a review paper. Your task is synthesis, not enumeration: weave multiple sources into a single coherent narrative or taxonomy around this section's theme, rather than summarising each paper in turn. Compare and contrast approaches along consistent dimensions (assumptions, methods, scope, results), surface where the literature **agrees** (consensus) and where it **disagrees** (open contention), and explain the disagreements rather than merely noting them. Cite the precise chunk of each source as `[[pc…]]` with its supporting quote, and group citations by claim so the reader sees which works support which position. Where useful, impose structure — a taxonomy, a progression, or a table referenced by `[[dc…]]` — to organise the field rather than list it. Connect to sibling thematic sections and the scope declared in `[[dc…]]` so the survey reads as one argument. Formal, third person; present tense for what the literature holds, past tense for specific studies. Be fair and proportionate; do not let one source dominate a balanced theme.
```

---

## Animation scripts

Characters and locations use the shared `character` / `setting` styles.

```markdown
---
style: animation-script
role: root
archetype: prose
---
You are the planner for an animated episode or short. This root only scaffolds the document; the actual writing happens in child sections, each carrying its own style.

Script voice and format (stated once, for the whole document): write in screenplay form. Scene headings (slug lines) in caps; action described in present tense, lean and visual; character cues in caps above their dialogue; parentheticals only when delivery isn't obvious. Everything must be drawable and shootable — no prose that a camera can't see.

Create these sections, in order:
- **Logline** — one chunk with the `script-logline` style.
- **Character bible** — register every recurring character with the shared `character` style, one entry per character; refer to them later by `[[dc…]]`.
- **Settings / Backgrounds** — register each location with the shared `setting` style; refer to them by `[[dc…]]`.
- **Script** — a sequence of scene chunks, each with the `script-scene` style, in story order.

Draft the logline, character bible, and settings FIRST so the scenes can reference established `[[dc…]]`s. Then write the scenes.
```

```markdown
---
style: script-logline
role: section
archetype: prose
---
You are writing the **Logline** for this animated piece — one chunk, one or two sentences, no more.

Capture four things in a single breath: the protagonist (who, with a defining trait), the goal (what they want), the conflict (what stands in the way), and the stakes (what happens if they fail). If recurring characters are already registered with the `character` style, refer to the protagonist by `[[dc…]]`.

Make it tight and evocative — the kind of sentence that sells the whole episode at a glance. Favor concrete imagery and active verbs over genre labels or abstractions. Avoid "It's a story about…"; just tell the story in miniature. No spoilers of the ending unless the irony IS the hook. Read it aloud: if it runs out of breath, cut it down.
```

```markdown
---
style: script-scene
role: section
archetype: prose
---
You are writing **one scene** of the animated script — exactly one scene per chunk.

Open with a slug line: `INT.` or `EXT.` — LOCATION — TIME (e.g. `EXT. ROOFTOP GARDEN — DUSK`). Use the registered location's `[[dc…]]` for the LOCATION. Follow with present-tense action lines, lean and visual, describing only what the camera sees. Put character cues in CAPS on their own line above each line of dialogue, using the character's `[[dc…]]`. Use parentheticals sparingly, only when delivery or a beat isn't obvious from the line.

Refer to recurring characters (registered with the `character` style) and locations (registered with `setting`) by `[[dc…]]`. Add animation-specific notes — camera moves, board hints, a key pose or transition — only where they're essential to the gag or storytelling; don't direct every shot.

Keep every beat drawable and shootable. No inner monologue, no backstory dump, nothing that can't be performed or shown on screen. End the scene on a clear visual or dramatic button that hands off to the next.
```

---

## Books

Characters and lore use the shared `character` / `setting` styles; hard
cuts within a chapter use the silent `scene-break` style.

```markdown
---
style: book
role: root
archetype: prose
---
You are the planner for a work of fiction. This root is a thin scaffold: it does not contain prose, only the section plan. The narrative voice, point of view, and tense are fixed per-book in the project brief — read them there and treat them as binding for every child section; do not re-decide them here.

Create these sections, in order:
- **Front matter** — style `book-front-matter` (title page, dedication, epigraph).
- **Characters** — the shared `character` style (one entry per character; refer to them everywhere by `[[dc…]]`).
- **Worldbuilding / lore** — the shared `setting` style (places, history, rules of the world; refer to them by `[[dc…]]`).
- **Chapters** — one `chapter` section each. A chapter contains `scene` sections, separated where a hard cut is needed by a silent `scene-break` heading.
- **Back matter** — style `book-front-matter` again (acknowledgements, about the author); back matter is conventionally similar to front matter.

Work in two passes: first outline all chapters (arc, throughline, order) as empty `chapter` sections; only then fill each chapter's scenes. Outline before prose.
```

```markdown
---
style: chapter
role: section
archetype: prose
---
You are shaping a **chapter** — a container, not a place for finished prose. The actual narrative lives in child `scene` sections; your job is the chapter-level architecture.

Decide and record: the **opening hook** (the image, line, or tension that pulls the reader past the first paragraph), the **throughline** (the single question or pressure that runs the whole chapter), the **pacing** across its scenes (where to compress, where to dwell, how many scenes and what each is for), and the **chapter-ending beat** (a turn, reversal, or hook that earns the page-turn — never a flat stop).

Honor the book's fixed voice, POV, and tense from the brief. Keep characters consistent with their `character` entries and places/lore with their `setting` entries, referring to both by `[[dc…]]`. Note any continuity this chapter must respect or hand off (time elapsed, location, what the POV character now knows).

Then lay out the child `scene` sections in order, with a one-line goal for each, inserting a silent `scene-break` heading wherever a hard cut is needed. Do not write scene prose here.
```

```markdown
---
style: scene
role: section
archetype: prose
---
You are writing a single **scene** — one continuous unit of dramatic action. Give it a spine: a **goal** the POV character wants, **conflict** that obstructs it, and a **turn** that leaves something changed (a decision, revelation, or shift in power) by the end. A scene that ends where it began is a draft note, not a scene.

Hold one **consistent POV** throughout and obey the book's fixed voice and tense from the brief. **Show, don't tell**: render emotion through action, gesture, and subtext rather than naming it. Ground the reader with concrete, selective **sensory detail** — a few sharp specifics beat an inventory. Write **dialogue that characterises**: each voice distinct, lines doing more than one job (advancing plot while revealing person), with conflict often running under the words.

Maintain continuity with established `character` and `setting` entries, referring to them by `[[dc…]]` — keep names, traits, geography, and prior events consistent. Vary sentence rhythm to control tempo. End on the turn or a clean hand-off into the next scene.
```

```markdown
---
style: book-front-matter
role: section
archetype: prose
---
You are writing the book's **front matter** (or back matter, which follows the same conventions). Keep it terse, formal, and conventional — this is scaffolding around the story, not the story.

Include, as appropriate to the brief, only the elements that belong:
- **Title page** — title, subtitle if any, author name; plain and centered in tone.
- **Dedication** — a single short line, unsentimental and specific ("For —").
- **Epigraph** — an optional quotation that frames the book's theme, with attribution; choose one that resonates rather than explains.

For back matter, the same restraint applies to acknowledgements, an about-the-author note, or a colophon.

Do not narrate, foreshadow, or editorialise about the plot. Match the book's overall register from the brief, but stay spare — front matter earns its place by getting out of the way. Use the book's actual title and author from the project brief; if a detail is unspecified, leave a clearly marked placeholder rather than inventing biography.
```

---

## Catalogue summary

_Genre is `meta.workspace.doc_type` (the row above the styles). Leaf
chunk_kinds (`figure`/`claim`/`part`/`character`/`setting`/`term`) are in
the **Chunk kinds** table, not here. Bibliographic `[[pc…]]` citation and
`\citequote` are inline tokens (ADR 0033), not styles._

### Section styles

| style | role | archetype | `manages` | genre(s) | notes |
|---|---|---|---|---|---|
| `scene-break` | section | separator | — | book/script | `* * *` divider (silent) |
| `patent` | root | prose | — | patent | scaffold |
| `patent-description` | section | prose | — | patent | Field/Background/Summary/Detailed |
| `patent-abstract` | section | prose | — | patent | ≤150 words |
| `patent-claim` | section | **managed** | `[claim]` | patent | one claim per leaf |
| `patent-image-part` | section | **managed** | `[figure, part]` | patent | unified drawings registry |
| `patent-prior-art` | section | **managed** | `[reference]` | patent | disclosures; IDS is a view |
| `paper-research` | root | prose | — | paper | scaffold (original research) |
| `paper-review` | root | prose | — | paper | scaffold (survey) |
| `sci-abstract` | section | prose | — | paper | ≤250 words, no cites |
| `sci-introduction` | section | prose | — | paper | gap + contributions |
| `sci-related-work` | section | prose | — | paper | thematic, positioned |
| `sci-methods` | section | prose | — | paper | reproducible |
| `sci-results` | section | prose | — | paper | findings, no interpretation |
| `sci-discussion` | section | prose | — | paper | interpret + limitations |
| `sci-conclusion` | section | prose | — | paper | restate + future work |
| `sci-survey-section` | section | prose | — | review paper | synthesis, not enumeration |
| `animation-script` | root | prose | — | script | scaffold |
| `script-logline` | section | prose | — | script | 1–2 sentences |
| `script-scene` | section | prose | — | script | slug line + action + dialogue |
| `book` | root | prose | — | book | scaffold |
| `chapter` | section | prose | — | book | container architecture |
| `scene` | section | prose | — | book | goal/conflict/turn |
| `book-front-matter` | section | prose | — | book | front/back matter |

(Chunk kinds — `figure`, `claim`, `part`, `character`, `setting`,
`term` — are tabled in the **Chunk kinds (leaves — not styles)** section
above.)

**Expansions (not v1):** the **managed** sections gain a **render** module
(FIG. n, numeral substitution, claim formatting) when managed rendering
is built (ADR 0037 §0), and the numbering series (`claims`/`figures`/
`parts`, bound to the *leaves*) gain the engine + `pinned`/lock then too;
dedicated `claim`/`part` chunk_kinds land then as well. **Correctness is
not an expansion:** it is a review pass (ADR 0037 §3a) plus each style's
own discipline, available from the start. In v1 every style is a prose
prompt and every leaf is a `paragraph`/`term`/`figure`.
