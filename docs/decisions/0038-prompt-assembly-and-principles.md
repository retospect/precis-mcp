# 0038 — Prompt assembly & prompt-engineering principles

- **Status**: proposed (2026-06-25)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0033 — Draft chunks](./0033-draft-chunks-editable-document.md) (its
    §8 "Editor prompt composition": cached system layer + variable layer,
    always-on brief/glossary, agent-selected skill menu, progressive
    disclosure).
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) (`dc…`/`pc…`;
    relative nav `^N`/`±N`/`lo..hi`; in-prose ref form `[handle]`).
  - [ADR 0037 — Heading styles](./0037-heading-styles-and-numbering-lock.md)
    (`doc_type` genre, section `meta.style` = skill, the four-axis split).
- **Validated by**:
  [`docs/design/prompt-assembly-shots.md`](../design/prompt-assembly-shots.md)
  — three fully-assembled prompts (editor / summarizer / reviewer) written
  *before* this ADR. This ADR records what the shots proved; read them first.

## Context

precis assembles LLM prompts in **~8 hand-rolled sites**, each
concatenating its own strings: `workers/planner_prompt.py` (editor /
planner tick), `workers/llm_summarize.py`, `workers/review.py` +
`structural.py` + `deep_review.py`, `dream_agent.py`, `briefing.py`,
`workers/_chase_llm.py`, `utils/tex_llm_fix.py`. That sprawl is the
"frankenmonster": no shared persona/tools/kinds, no shared cache
boundary, no consistent table format, drift between sites.

**We are not greenfielding** — three foundations already exist:

- **A compose mechanism:** personas use `{{include doc:<skill>#<anchor>}}`
  (`data/skills/personas/*`). The "prompt language" is **markdown +
  frontmatter + `{{include}}`** — *do not invent a DSL.*
- **Per-target runners:** `utils/claude_agent.py` (agentic),
  `claude_p.py` (one-shot judge), and the summarizer's **litellm** proxy.
- **A working instance of the target design:** `llm_summarize.py` already
  does stable-system + stable-doc-header **prefix** + volatile-user, with
  the doc *kind* deliberately in the header so the instruction layer is
  genre-agnostic and the prefix caches. Shot 2 is transcribed from it.

The goal: **one assembler + one module library**, so the editor, asa, the
reviewers, the summarizer, and the judges share a cacheable, inspectable
surface instead of eight bespoke ones.

## Decision

### 1. Layer by cache-volatility

A prompt is a list of **modules** grouped into two layers:

- **Cached layer** (static across ticks → one cache prefix): mechanics,
  `tools` table, `kinds` table, the skill *menu*, few-shot *examples*, the
  global admonition core.
- **Variable layer** (per tick): the persona-specialization line, the
  `doc_context` table, the `glossary` table, the brief, and the 1–2 loaded
  skill *bodies*.

The cached/variable boundary *is* the prompt-cache boundary. (0033 §8
already splits this for the editor; the summarizer already orders stable
blocks first for llama.cpp prefix reuse.)

### 2. Two kinds of module — static (markdown) and computed (code → TOON)

The assembler concatenates an ordered list of **blocks**. A block comes
from one of two module kinds:

- **Static module — a markdown file.** Body is prose; frontmatter carries
  machine pointers; compose with the **existing**
  `{{include doc:<id>#<anchor>}}`. These are `persona`, `mechanics`,
  `skill` (how-to / style, JIT-loaded), `examples` (few-shot), and
  `admonition`. Adding one = writing a markdown file; no parser.

  ```yaml
  id: <slug>
  flavor: persona | mechanics | reference | examples | admonition
  layer: cached | variable
  applies_when: <predicate>     # optional — conditional inclusion (§8)
  ```

- **Computed module — a builder function → a TOON table.** The **context
  tables** (`doc_context`, `tools`, `kinds`, `glossary`, §6) are *not*
  authored markdown — they are **generated at assembly time** from live
  state and rendered as TOON, then dropped into the block list at their
  slot. A computed module is registered as `{id, layer, applies_when,
  build()}` where `build()` returns rows. (This is the answer to "how do
  the tables appear" — see §6a.)

So a "module" is a static doc *or* a computed block; the assembler treats
both as ordered, layer-tagged blocks and renders the whole list per the
adapter (§3).

### 3. The assembler + the adapter

```
assemble(persona, context, skills, profile) -> [modules]     # model-agnostic
adapter(target).render([modules])           -> messages|prompt # model-specific
```

The **assembler** selects and orders modules (model-agnostic). The
**adapter** packages them for one runner and **owns caching**:
`claude_agent` (system/user split + cache breakpoints), `claude_p`
(one-shot), `litellm/summarizer` (prefix-stable ordering for KV-cache).
*Model quirks never touch a module.*

### 4. Profiles — `agent` vs `helper` (load-bearing)

- **agent** (autonomous, tools, multi-turn): persona + mechanics + `tools`
  + `kinds` + skill-menu + `doc_context` (+ glossary). The editor (Shot
  1), the reviewers (Shot 3), the dreamer, fix-gripe.
- **helper** (one-shot, no tools, structured output): persona + input +
  output-schema (+ examples, + one admonition). Everything else dropped.
  The summarizer (Shot 2), the chase judge, tex-fix.

The profile is *which modules the assembler emits* — not two codebases. It
maps onto the existing `claude_agent` vs `claude_p` choice.

### 5. Persona specialization is task-dependent

The **editor** specializes its persona by genre ("you are drafting the
Claims of a patent" — from `doc_type` + `meta.style`, 0037). The
**summarizer** deliberately does **not** — its instruction stays
genre-agnostic and the kind goes in the doc-header, for prefix-cache
stability. So *"persona = genre" is an editor choice, not a law*; a module
declares whether it specializes.

### 6. The context tables (TOON)

Rendered as TOON (ADR 0002). Four, three of them cached:

- **`doc_context`** (variable) — the working set: the **window** (ancestors
  `^N`, siblings `±N`, 0036) **+** the **references** (`cites`/`refers`/
  `uses-term` links, 0033). Columns `id | what | how | details`. The
  **`how` column is the disclosure level** — `path` / `keywords` / `gist`
  / `verbatim` — and maps onto already-computed data (KeyBERT F20; the
  `BRIEF` navigation gloss from `llm_summarize`; `text`). The agent
  deepens any row with `get(id=…)` (progressive disclosure).
- **`tools`** (cached) — `verb | example | what`, examples included.
- **`kinds`** (cached) — `code | name | what | ops`; legends handles *and*
  `kind=` (§7).
- **`glossary`** (variable, conditional §8) — `term | short | long`.

Rules surfaced by the shots:
- **Dedup**: a referenced part that's also a glossary term shows **once**
  (keep the glossary row, drop the duplicate `doc_context` ref).
- **Scope**: `doc_context` window/refs are **bounded** (top-K; `log`
  truncation). The glossary is **region-scoped** in context; the *full*
  registry is used only by the off-model linkify pass (0033) — the model
  sees a scoped list, `find(glossary)` reaches the rest.
- *Model sees scoped; server uses full.* (General rule for any registry.)

### 6a. How a table is built and inserted

A table is a **computed module** (§2): the assembler calls its `build()`,
which queries live state and returns rows; a **TOON renderer** serializes
them; the assembler drops that text into the block list at the module's
slot, tagged with its `layer`. No table is hand-written. For
`doc_context`, `build(anchor=dc41)` is a pipeline over **existing**
primitives:

```
build_doc_context(anchor):
  rows  = []
  rows += ancestors(anchor, op='^')         # 0036 relative nav  → path rows
  rows += siblings(anchor, ±N)              # 0036 ±N            → window rows
  rows += outbound_links(anchor)            # 0033 cites/refers/uses-term
  rows  = dedup(rows)                        # §6 (part-also-a-term shows once)
  rows  = budget(rows, token_floor); log_if_truncated(rows)   # §6 scope
  for r in rows:                             # pick disclosure per row
    r.how, r.details = (
      'verbatim', text(r)          if r is anchor else
      'path',     breadcrumb(r)    if r is ancestor else
      'gist',     gloss(r)         if has_gloss(r) else      # llm_summarize BRIEF
      'keywords', keywords(r))                               # F20 KeyBERT
  return toon(rows, cols=['id','what','how','details'])
```

So the table "appears" because the assembler ran a builder over the chunk
tree + link graph + already-computed derived data (keywords/gloss) and
TOON-rendered the result — it is **not** a static skill the author edits.
`tools`/`kinds` are the trivial case (rows from a fixed verb list / the
`handle_registry`, cached); `glossary` rows come from the draft's
region-scoped `term` chunks (conditional, §8).

### 7. `kind=` accepts code or name; one legend

`kind=` accepts the 2-char handle code **or** the long name (`kind='dr'` ≡
`kind='draft'`), resolved via the existing `handle_registry`. The `kinds`
table is the single legend for **reading handles** *and* **choosing
`kind=`**. Caveats in the table: chunk codes (`dc`/`pc`) are address-only,
not `put` kinds; provider kinds (`perplexity-research`, `calc`) have no
handle (search/compute targets).

### 8. Conditional modules

A module carries an `applies_when` predicate; the assembler includes it
(both its **capability** block and its **data** block, gated together)
only when true. Worked examples:

- **glossary**: `enabled` (genre-level — gates the find/add capability)
  **×** `has-scope-terms` (gates the term list). Disabled → neither;
  enabled+empty → capability only (so you can *start* one); enabled+terms
  → both.
- **claim skill**: `in a claims section` → load the body.
- **figure flow**: `adding a figure` → the drawing/plot/reused-image branch.

Rule: *never show a capability you're suppressing, nor data with no
capability.*

### 9. Admonitions are positive and verifiable, in three tiers

**Phrase every admonition as what *to do*, stated so it can be checked —
not what to avoid.** Positive instructions outperform prohibitions, and a
verifiable one is exactly what the review pass (0037 §3a) or a lint can
confirm. So "don't fabricate citations" becomes a *testable assertion*:

| ✗ prohibition | ✓ positive + verifiable | who checks |
|---|---|---|
| don't fabricate citations | Every non-obvious claim cites a specific `[pc…]` paper chunk that supports it, with the supporting quote. | citation reviewer / export lint |
| don't fabricate handles | Reference only handles present in your context or that you fetched; to point at something new, mint it first. | resolver (every `[handle]` resolves) |
| don't guess | When a fact is missing, open an issue (a query) and continue elsewhere. | the issue exists |
| respect locks | Treat a locked numeral as fixed; to change it, open an issue. | numbering engine refuses |

The three tiers:

- **A tiny global core** (cached): the 3–4 positive rules above.
- **Task rules JIT in the relevant skill** (the citation skill carries
  "cite the precise chunk + capture the quote") — surfaced only when the
  task triggers it.
- **Hard/safety rules in *code*** (append-only, locks, SSRF, numeral
  collision — the handler rejects). *The prompt is not a safety
  mechanism;* the positive prompt rule and the code guard are two layers
  of the same invariant (the summarizer's length-cap = soft-in-prompt +
  hard-in-parser).

**The win:** because each admonition is a positive, verifiable assertion,
the **same review pass that checks the draft also audits prompt
compliance** — admonitions become testable, not vibes.

### 10. One surface for surfaced items

Reviewer findings, agent questions, user change-requests, and **proposals**
(incl. "you should use/write skill X", a `proposes: sk<id>` link) are all
the same anchored `issue` on the §3b/0037 attention surface — *findable the
same way*. Prompts emit issues; they do not invent new channels.

### 11. Prompt-engineering principles (the short, enduring list)

1. **Persona-first** — orient the model before instructing it.
2. **Progressive disclosure** — keywords/gist by default; `get(id=…)` to
   deepen; never dump the whole document.
3. **Structured TOON tables** over prose for context/tools/kinds.
4. **Examples as a cached module**, not inline ad-hoc.
5. **Positive & verifiable** — say what *to do*, phrased so a reviewer or
   lint can check it ("every claim cites a `[pc…]`"), not what to avoid; a
   tiny global core + JIT + code, never a wall-of-don'ts (§9).
6. **Cache boundaries are real** — stable-first ordering / breakpoints;
   keep layer-1 genre-agnostic where caching pays (§5).
7. **Model-agnostic modules + a thin per-target adapter** (§3).
8. **Handles everywhere; numbers are render output** (0036/0037).

These are principles, not a cookbook — keep the list short; resist growth.

## Consequences

- One cacheable, inspectable surface replaces ~8 bespoke ones; adding a
  prompt = `assemble(profile=…)` + a few modules.
- The adapter localizes per-model work (Claude vs litellm/Qwen), so a new
  runner is one adapter, not eight rewrites.
- `doc_context` reuses derived data already computed (keywords, gloss,
  links) — little new compute.
- Risk — **over-abstraction before validation.** Mitigation below.

## Migration — build one first, then fold in

**Do not build the universal framework in the abstract.** Order:

1. **Refactor `planner_prompt.py` (Shot 1)** onto `assemble(...)` + the
   four tables + conditional modules + the `claude_agent` adapter. Prove
   it on a real draft (the paperclip).
2. **`llm_summarize` (Shot 2)** — already ~this shape; formalize its
   modules (persona / examples / doc-header / mini-context) + the litellm
   adapter.
3. **The reviewers (Shot 3)** — persona modules (exist) + `doc_context` +
   issue output.
4. Then `dream_agent`, `briefing`, `_chase_llm`, `tex_llm_fix`.

Each step leaves the gate green and deletes its bespoke assembly.

## Open questions

1. **Assembler home** — `utils/prompt/` (assembler + adapters) vs folding
   into the existing runner modules. Lean: a new `utils/prompt/` package,
   adapters wrapping `claude_agent`/`claude_p`/litellm.
2. **`applies_when` predicate language** — a small fixed set of named
   predicates (in-section-style, has-scope-terms, genre=…) vs free
   expressions. Lean: named predicates (totality-tested, like the handle
   registry).
3. **Examples module sourcing** — inline in a skill vs a separate
   `examples/` corpus searchable like skills. Lean: a skill `flavor:
   examples`, `{{include}}`-d.
4. **`doc_context` bound (top-K)** — fixed K vs token-budgeted. Lean:
   token-budgeted with a floor, `log` truncation.
