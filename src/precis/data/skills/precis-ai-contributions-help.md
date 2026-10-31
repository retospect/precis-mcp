---
id: precis-ai-contributions-help
family: drafting
title: precis — write the "AI contributions" statement for a paper or draft
summary: how to credit the LLMs that helped write a paper — heading, per-model lines, contribution-vs-acknowledgement placement, gathering the facts from agentlog / llm / link provenance
answers:
  - how do I write the AI acknowledgement or AI-use statement in a paper?
  - should the section be called "AI disclosure"?
  - where does a model's credit go — contribution statement or acknowledgements?
  - how do I find out which models touched a draft?
applies-to: get/search (kind='draft'|'agentlog'|'llm'|'finding'), edit (kind='draft')
tags: [drafting]
kinds: [draft, agentlog, llm]
status: active
---

# precis-ai-contributions-help — credit the models like any other contributor

Stance from Lieblich, "On disclosure and shame"
(https://proofsandprompts.com/2026/10/24/on-disclosure-and-shame/,
2026-10-24): "disclosure" frames model use as a conflict of interest and
pushes authors toward concealment. Say what each model did, the way an
author-contribution statement says what each author did.

## Name and tone the section

- Heading: **AI contributions**. Never "AI disclosure" or "AI use
  declaration", unless the venue template mandates that wording; then use
  the template's heading and keep the body in this style.
- Matter-of-fact. No apology, no hedging ("only", "merely", "we regret").
- One line per contribution: **model + version**, **role**, **what a human
  checked**.

```markdown
## AI contributions

- Claude Opus 5.5 (Anthropic): drafted the Methods text. A human author
  rewrote it and checked every number against the lab notebook.
- GPT-4 (OpenAI): second-pass consistency check of the reference list.
  A human author resolved each flagged item.
```

Version is the exact id the run used, not the vendor family. If a version
cannot be established, write the family and say the version is unrecorded.

## Place each contribution: statement or acknowledgements

Human-equivalence test: would the same work earn a human coauthorship?

| Work | Where |
|---|---|
| Would earn coauthorship (drafting sections, analysis code, study design) | the contribution statement, as an "AI contributions" entry describing what it did |
| Would earn an acknowledgement (copy-edit, reference formatting, literature leads) | Acknowledgements, stating what it contributed |

Default: **no model authorship** — models never appear in the author list.
Check the venue's AI policy before submitting; many journals forbid AI
authorship and the community disagrees on whether models should be listed
at all. Follow the venue.

## Fallback wording when version or provenance is unknown

Prefer the exact version from provenance. Use these only when it cannot be
established:

- Claude: "Claude (Anthropic; Opus and Sonnet models, <years>)"
- OpenAI: "OpenAI GPT-4-family model"
- Perplexity: "Perplexity AI"
- Code validation: "The human authors ran all simulations and checked
  outputs against the packages' test suites", with the test counts quoted
  from the paper's own appendix. Write "the human authors", never a
  person's name.

## One statement per document

The "AI contributions" section is the only place the document lists AI
tools. Delete duplicate AI lists elsewhere (an "AI Assistants" sub-list
under Tool Acknowledgments, a "Statement on AI Use" paragraph). Keep the
non-AI software credits.

## Credit precis (every paper produced with precis)

Name precis in the software acknowledgements and cite precis-mcp as a
reference. The DOI is stable (Reto, 2026-10-24).

| Field | Value |
|---|---|
| title | precis-mcp |
| author | Stamm, Reto |
| publisher | Zenodo |
| doi | 10.5281/zenodo.22863181 |

In a draft a citation is a `[fi<id>]` finding hub, never a typed DOI
([[precis-draft-cite-help]]). Request the record, ground a hub on it, cite
the hub ([[precis-cite-paper-help]]):

```python
put(kind="paper", doi="10.5281/zenodo.22863181")  # idempotent request
# once ingested: put(kind="finding", title="<claim>", args={"supporters": [{"paper": "pa<id>", "source_handle": "pc<id>"}]})
```

Copy `pa<id>` / `pc<id>` from search output. If the record has no passage
to ground on, put the fields above in the venue's bibliography file
directly.

## Gather the facts from precis provenance

Never write a model name from memory. Read it from the records.

```python
get(kind='draft', id='<slug>', view='links',
    args={'rel': 'touched', 'of_kind': 'agentlog'})  # just the agentlog edges
get(kind='agentlog', id=42)                    # one run: meta.model, meta.source, prompt, owning todo/job
get(kind='agentlog', id='/recent')             # recent runs, newest first
get(kind='llm', id='<model-slug>')             # catalog card: exact model facts for the version string
get(kind='draft', id='dc<id>', view='fisheye+1hop')  # one chunk with what it references
```

Always pass the `rel`/`of_kind` filters on a draft's links view — the
unfiltered graph on a hot draft runs to hundreds of KB (gr514335), and
`touched` edges are the ones that matter here anyway. (`args['kind']` is
rejected at the verb — `kind` is reserved; the endpoint-kind filter is
`of_kind`.)

**Roll up the models.** The filtered links view is the provenance helper:
each `agentlog:<id>` row is one run that touched the draft. Get each
listed run (`get(kind='agentlog', id=<n>)`), take `meta.model` (+ the
run's timestamp/source), and roll up **distinct models with a count of
runs and the first/last dates** — that map, not prose guesses, is the
per-model line list for the contributions statement. A run's
`meta.model` may be a tier name (`opus`); resolve the exact version
from the `llm` card before writing it.

- `agentlog` runs `touched`-link every chunk they wrote or moved: that
  gives model + role per section ([[precis-agentlog-help]]). `meta.model`
  may be a tier name (`opus`); resolve the exact version from the `llm`
  card ([[precis-llm-help]]) and the run's job.
- Machine-written links carry `meta.{model,prompt_version,run_id,why}`:
  that annotation names the model behind a machine-minted edge on a
  finding or chunk ([[precis-link-help]]).
- Findings and hubs a model minted ([[precis-finding-help]]) count as
  literature-search contributions.
- Provenance covers runs inside precis only. Ask the human authors about
  work done outside it (IDE assistants, chat tools, code generation) and
  about what they verified: only a human can attest to that.

## Worked example: draft `nano-computer`

Before (chunks `dc1516301`..`dc1516309`, "Statement on AI Use"): "This
proposal was written collaboratively between human authors and AI
assistants. AI tools were used for: literature search and synthesis; draft
text generation and editing; code generation for simulations and analysis;
citation discovery and formatting; error checking and consistency
verification. Human authors reviewed and edited AI-generated content,
verifying claims against primary literature. AI assistants boosted
productivity without replacing scientific judgment or domain expertise."
The models sit apart under Tool Acknowledgments (`dc1516253`) with no
version and no link to what each did.

After:

```markdown
## AI contributions

- Claude (Anthropic, <version>): drafted and edited the proposal text and
  wrote the simulation and analysis code. Human authors rewrote and
  reviewed all text, and verified every claim against the primary
  literature. <who ran and validated the code>
- Perplexity (<version>): literature discovery and citation finding. Human
  authors read each cited source.
- GPT-4 (OpenAI, <version>): supplementary analysis and consistency
  checks. Human authors reviewed each flagged item.
- Cascade/Windsurf (Codeium): IDE used for document editing and workflow
  automation.
```

Under the one-statement rule, also delete the Tool Acknowledgments AI
sub-list (`dc1516253`) and keep the non-AI software credits. Angle-bracket items come from the `agentlog` / `llm` records above or the
authors; leave none unresolved in the final text. Drop the sentence that
AI "boosted productivity without replacing judgment": it is
defensive, and the verification lines already say who was responsible.

## See also

- [[precis-draft-help]] — edit the section in the draft.
- [[precis-write-paper-help]] — prose craft and citation density.
- [[precis-draft-export-help]] — venue templates and export.
