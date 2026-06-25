# Prompt-assembly shots (validation for ADR 0038)

> Status: **scratch / validation artifact** (2026-06-25). These are
> *design-by-example*: three fully-assembled prompts as the proposed
> infra would emit them, written **before** ADR 0038 so the examples pin
> the assembler API, the module schema, and the table shapes — and surface
> fixes we'd miss writing the ADR blind. The shots span the profile space
> (agent / helper) and three runners (claude_agent, litellm, claude_agent
> review).
>
> `‹module · layer›` annotations show where each block comes from. They are
> **not** in the emitted prompt.

## What already exists (so 0038 extends, not greenfields)

- **An include/compose mechanism** — personas already use
  `{{include doc:precis-common-reviewer#picky-reviewer-stance}}`
  (`data/skills/personas/precis-adversarial-reviewer.md`). The "prompt
  language" is **markdown + frontmatter + `{{include doc:#anchor}}`** — real
  already; no DSL to invent.
- **Persona modules** — `data/skills/personas/*` (`flavor: persona`).
- **A working instance of the layered design** — `llm_summarize.py`
  (`7234fca`) already does stable-system + stable-doc-header *prefix* +
  volatile-user, with the doc *kind* deliberately in the header so layer 1
  is genre-agnostic for prefix-cache reuse. Shot 2 is transcribed from it.
- **Per-target runners** — `utils/claude_agent.py` (agentic),
  `claude_p.py` (one-shot judge), and the summarizer's **litellm** proxy.
- **In-prose reference form is single-bracket `[handle]`** (`6350bee`) —
  not `[[…]]`/`¶`. Shots use `[dc40]`.
- **The frankenmonster** — ~8 hand-rolled sites (`planner_prompt`,
  `llm_summarize`, `review`/`structural`/`deep_review`, `dream_agent`,
  `briefing`, `_chase_llm`, `tex_llm_fix`) each assembling independently.

---

## Shot 1 — Draft editor on a patent claim (agent profile · claude_agent)

The richest case: exercises persona, the three tables, conditional glossary,
handles, progressive disclosure, a JIT skill.

```
══ CACHED LAYER (static; one cache prefix across ticks) ═══════════════════

‹mechanics · cached›
You edit a chunk-native draft. Everything is a handle: dc<id> a draft chunk,
pc<id> a paper passage, pa<id> a paper, sk<name> a skill. Default view is
keywords; get(id=…) to pull verbatim where you act. Numbers (claim n, FIG. n,
[n]) are computed at render — write the prose and refer to chunks by [handle];
the number appears at export.

‹tools · cached›
tools{verb,example,what}:
  get,    get(id=dc41+1),                      read a row deeper / a window
  put,    "put(kind=dr, at={after:dc41}, …)",  insert a chunk after
  put,    "put(kind=dr, at={before:dc41}, …)", insert a chunk before
  edit,   "edit(id=dc41, text=…)",             rewrite this chunk
  search, "search(kind=sk, q='write a claim')",find a skill / how-to

‹kinds · cached›   (legends handles AND kind=)
kinds{code,name,what,ops}:
  dr, draft,               a document we're writing,  get put edit search
  dc, (draft chunk),       a chunk in a draft,         get edit · address-only
  pa, paper,               an ingested paper,          get search
  pc, (paper chunk),       a paper passage,            get · address-only
  pt, patent,              an EPO-OPS patent,          get search
  —,  perplexity-research, live web research,          search · no handle

‹skill-menu · cached›  load with get(id=sk<name>):
  patent-claim · patent-description · precis-draft-structure · precis-citation-help

══ VARIABLE LAYER (per tick) ══════════════════════════════════════════════

‹persona · doc_type=patent + style=patent-claim›
You are drafting the Claims of a utility patent
("Paperclip with a non-piercing ball end"). Patent register.

‹brief · meta.workspace.brief (doc_type guidance leads)›
Patent application: formal, present tense, antecedent basis, "comprising".
Invention: a paperclip whose free end carries a ball so it can't pierce paper.

‹skill: patent-claim · JIT (applies_when: in a claims section)›
One claim per chunk, one sentence: preamble + "comprising" + elements;
introduce each element "a/an", refer back "the/said". Dependent claims open
"The <noun> of [dc…], further comprising…". The claim number is rendered
from order — refer to other claims by [handle].

‹doc_context · window (^/±N) + references; how = disclosure level›
doc_context{id,what,how,details}:
  dc40^, Claims (root: Paperclip…), path,     "Claims ▸ independent + dependents"
  dc40,  prev claim,                keywords, "paperclip; sprung wire body; bends; retains sheets"
  dc41,  current,                   verbatim, "The paperclip of [dc40], wherein the second end has a ball larger than the wire gauge."
  dc42,  next claim,                keywords, "ball; polymer over-mould; colour-code"
  pc233, cites (prior art),         gist,     "Acme 1998: wire clip, rounded end; no ball-Ø limit"

‹glossary · enabled(patent) × has-scope-terms; region-scoped›
glossary{term,short,long}:
  ball,      ball,      "spherical free-end feature (part dc7, numeral 12)"
  wire body, wire body, "elongated sprung wire (part dc6, numeral 10)"

══ TASK ════════════════════════════════════════════════════════════════════
edit(id=dc41): tighten claim 2's dependency; make "ball" antecedent-correct.
```

**Surfaced:**
- **Dedup rule needed.** The part `ball` (dc7) would appear both as a
  `doc_context` reference *and* a glossary row. → the assembler must show a
  referenced part **once**: keep it in the glossary, drop the duplicate
  `refers-to` row (or vice-versa). A concrete fix we'd have missed.
- The `how` column maps onto **already-computed** data — keywords (F20
  KeyBERT), gist (`llm_summarize`), verbatim (`text`). No new compute.
- `tools` + `kinds` + `skill-menu` are **identical** across agent shots →
  belong in the cached prefix.

---

## Shot 2 — Summarizer (helper profile · litellm/Qwen, prefix-KV-cache)

> **This one is real — transcribed from the shipped `llm_summarize.py`
> (`7234fca`), not invented.** It is a *working instance of the proposed
> layering*, which is why it's the strongest validation of the design.

It writes the **navigation gloss** (BRIEF + DETAIL) that the editor's
`doc_context` later consumes in its `how=gist` rows — a closed loop.

```
‹adapter: litellm "summarizer" (Qwen3-Next-80B) · cache = longest prompt PREFIX›

══ system message — STABLE PREFIX (KV-cache reuse across a doc's chunks) ════
‹persona · helper · deliberately doc-KIND-AGNOSTIC (never mentions paper/patent)›
You summarize a single passage from a larger document, as a navigation gloss.
Output EXACTLY two lines and nothing else:
BRIEF: <self-contained gist in one clause, at most N words>
DETAIL: <1-3 terse fragments adding specifics NOT in BRIEF — quantities,
named entities, method, caveats>
…be faithful; telegraphic; drop articles/pronouns; number-space-unit;
non-prose → a parenthetical tag ((tabular data), (reference list), …).

‹examples · cached module (7 few-shot BRIEF/DETAIL pairs, style only)›
PASSAGE: …cobalt complex…  BRIEF: cobalt catalyst triples turnover…  DETAIL: …
… (×7) …

‹doc header · Layer 2 — stable per doc; the KIND lives HERE, not in layer 1›
--- Document for context (a patent; do not summarize this header) ---
Title: Paperclip with a non-piercing ball end

══ user message — VARIABLE (per chunk) ═════════════════════════════════════
‹mini doc_context · section path + keywords + quantities›
Section: Claims
Keywords: paperclip, ball, wire gauge, dependent claim
Passage to summarize:
The paperclip of [dc40], wherein the second end has a ball larger than the
wire gauge.
```

**Surfaced / confirmed (by real code):**
- **The layering is already real.** Layer 1 (instruction) is *doc-kind-
  agnostic on purpose* — the kind ("a patent") sits in Layer 2's header so
  layer 1 never changes between paper/patent/conv → maximal prefix-cache
  reuse. This is exactly "model-agnostic modules; the variable bit is a
  line of data," shipped.
- **Persona specialization is task-dependent.** The *editor* specializes
  its persona by genre (Shot 1); the *summarizer* deliberately does **not**
  (cache stability). So "persona = genre" is an editor choice, not a law.
- **Examples are a cached module** (7 few-shots in the system layer) — a
  module type the ADR should name.
- **The helper profile genuinely drops** tools / kinds / skill-menu /
  glossary — system(instruction+examples+header) + user(passage+mini-ctx).
- **The adapter owns caching.** Here it's llama.cpp **prefix** reuse
  (stable blocks first); Claude would split system/user with breakpoints.
  Same modules, different packaging.

---

## Shot 3 — Structural reviewer (agent profile · claude_agent, emits issues)

Proves persona-as-skill (with the existing `{{include}}`), read-only tools,
subtree-scoped context, and output = anchored issues (0037 §3a/§3b).

```
══ CACHED LAYER ════════════════════════════════════════════════════════════
‹persona · skill precis-structural-reviewer (flavor: persona)›
You are a structural reviewer of a draft section.
{{include doc:precis-common-reviewer#picky-reviewer-stance}}
{{include doc:precis-common-reviewer#ground-rules-for-read-only-work}}

‹tools · cached (read-only subset)›
tools{verb,example,what}:
  get,    get(id=dc40-1..1),  read a chunk / window
  search, "search(q=…)",      find related material

‹kinds · cached›   (same legend as Shot 1)

══ VARIABLE LAYER ══════════════════════════════════════════════════════════
‹doc_context · scope = the section subtree under dc40; keywords›
doc_context{id,what,how,details}:
  dc40^,  Claims (root: Paperclip…), path,     "Claims ▸ …"
  dc40,   claim 1,                   keywords, "paperclip; wire body; bends; retains sheets"
  dc41,   claim 2,                   keywords, "ball Ø > wire gauge; depends on 1"
  dc42,   claim 3,                   keywords, "ball; polymer over-mould; colour-code"

‹brief · meta.workspace.brief›
Patent application … (as Shot 1).

══ TASK ════════════════════════════════════════════════════════════════════
Review the section under dc40 against the brief: drift, sibling
contradictions, gaps, overlong sections. File one anchored issue per finding.

‹output schema · becomes anchored change-requests (0037 §3a/§3b)›
issues[]{anchor: dc<id>, severity, what, suggested_fix}
```

**Surfaced:**
- The **same assembler + tables** serve review; only the persona, the tool
  subset (read-only), and the *output* (issues) differ. Review is not a
  separate stack.
- Output **is** the §3b issue (origin=reviewer) — closing the loop:
  reviewers feed the same attention surface as agent queries and user
  change-requests. "Proposals/findings" ride this too (a reviewer can file
  a `proposes: sk<skill>` issue — discoverable the same way).
- `{{include doc:#anchor}}` is the **module mechanism, already shipped** —
  0038 formalizes layering/tables/profiles *around* it.

---

## What these shots pin down for ADR 0038

1. **Assembler API:** `assemble(persona, context, skills, profile) → modules`,
   then `adapter(target) → messages/prompt`.
2. **Modules = markdown + frontmatter + `{{include doc:#anchor}}`** (exists).
   Frontmatter carries `layer` (cached|variable), `flavor`
   (persona|reference|…), `applies_when` (predicate), optional `behavior`.
3. **Profiles** (load-bearing): `agent` (persona + tools + kinds +
   skill-menu + `doc_context` + glossary) · `helper` (persona + input +
   schema; the rest dropped).
4. **Adapters own packaging + caching**, per target: `claude_agent`
   (system/user split, cache breakpoints) · `claude_p` (one-shot) ·
   `litellm/summarizer` (prefix-stable ordering for llama.cpp KV-cache).
   Modules stay model-agnostic.
5. **Context tables (TOON):** `doc_context` (window `^`/`±N` + references,
   `how`-column disclosure) · `tools` · `kinds` (code↔name legend) ·
   `glossary` (conditional). **Rules surfaced:** dedup a referenced part
   that's also a glossary term; bound window/refs (log truncation);
   region-scope the glossary (full registry only in the off-model linkify
   pass).
6. **`kind=` accepts code or name**; the `kinds` table is the single legend
   for handles *and* `kind=` (chunk codes address-only; provider kinds no
   handle).
7. **Admonitions:** tiny global core (cached) · task rules JIT in skills ·
   hard rules in code (length-cap example = soft-in-prompt + hard-in-parser).
8. **Conditional modules:** `applies_when` predicate gates capability and
   data together (glossary = `enabled × has-scope-terms`; claim skill =
   in-claims-section).
9. **One surface for surfaced items:** reviewer issues = agent queries =
   user change-requests = proposals, all on the §3b attention surface.

**Build-one-first target:** refactor `planner_prompt.py` (Shot 1) onto this
assembler; then `llm_summarize` (Shot 2, helper) and the reviewers (Shot 3)
fold in. The shots are the test; 0038 is the conclusion.
