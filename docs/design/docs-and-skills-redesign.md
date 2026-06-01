# Skills redesign — full discovery-layer integration, three-layer source-of-truth

**Status**: draft (pre-review), 2026-06-01
**Owner**: `src/precis/handlers/skill.py` (refactor),
`src/precis/handlers/_skill_common.py` (new, shared helpers),
plus boot-time scan-and-ingest worker hooks and a template-include
preprocessor extension covering both content and code sources.
**Predecessors**:
- [`storage-v2.md`](./storage-v2.md) — refs / chunks / links substrate.
- [`mcp-cold-start-token-budget.md`](./mcp-cold-start-token-budget.md) —
  the small-model tool-call surface that this redesign explicitly
  *does not change*.
- [`finding-chase.md`](./finding-chase.md) — the in-flight `finding`
  kind; out of scope here, but reviewer-persona findings (when
  authored) flow into it.
**Related ADRs**:
- [0018 — persistent discovery layer + sibling ref-level workers](../decisions/0018-persistent-discovery-layer.md)
  (papers ride this; we extend the same path to skills).
- [0017 — derived queue family + `artifact_kinds` registry](../decisions/0017-derived-queue-family.md)
  (boot-time scan-and-ingest is a worker pass over shipped content).
- [0003 — shared tool registry](../decisions/0003-shared-tool-registry.md)
  (the canonical schemas + docstrings small models read at cold
  start; same content drives the embedded arg tables in skills).

## Problem

Today's `kind='skill'` content lives in `src/precis/data/skills/*.md`
and is served via a parallel, file-backed path:

- `SkillHandler` (`src/precis/handlers/skill.py:327-340`) sets
  `supports_get=True, supports_search=True` but **bypasses the
  discovery layer**.
- Search uses a lazy `FileCorpusIndex` (in-memory, not DB-backed) —
  `_get_index` at `skill.py:659-686`.
- Chunks come from `chunk_by_h2()` on the fly
  (`skill.py:735-776`), not from stored `ref_segments`.

Papers, by contrast, ride the full discovery layer (`chunks` +
`ref_segments` + `ref_segment_sentences`) with **sentence-level rerank
against the query embedding** — the headline feature of ADR 0018.
Skills get none of that.

Two consequences:

1. **Search results for skills are coarse.** No sentence rerank,
   no excerpts in result lines, no `slug~A..B/toc` recursion.
2. **The 43 skill files are a pile.** Per-file content is
   inconsistent: some are *behavioral runbooks* ("audit your
   manuscript citations"), some are *reference manuals* ("how to
   invoke `put`"), some are *conceptual overviews*. They share a
   filename pattern but answer different questions at different
   times. Cross-cutting content (address grammar, tag semantics,
   TOON shape, etc.) is duplicated by hand across many files. The
   per-verb argument tables (e.g., `precis-put-help § Arguments`)
   are hand-maintained and *can drift from the Python schemas
   they describe*.

## Goal

Make skills first-class citizens of the discovery layer, and make
the per-verb reference content **derived from code** so it cannot
drift:

- **Skill kind refactored** to ride the discovery layer — same
  ingest path, same chunk/segment/sentence machinery as papers.
- **One kind, multiple flavours.** `flavor:` frontmatter
  (`reference` / `persona` / `runbook` / `concept`) distinguishes
  content types. No new `doc` kind — directory layout handles
  human-side organisation; runtime stays single-kind.
- **Header-tree segmentation**: H2 = segment, with the heading
  text itself serving as the chunk's user-vocab description.
- **Three-layer source-of-truth architecture** (see below):
  code is canonical for schemas; generated surfaces feed both
  small-model cold-start and embedded skill content; human-
  authored prose surrounds the generated parts.
- **Template includes** for cross-cutting content and code-derived
  content, resolved at ingest.
- **Boot-time scan-and-ingest** so shipped skills auto-re-ingest
  when the source files change.

## The three layers

```
[ code: src/precis/tools/core.py + handler kindspecs ]
      │   canonical schemas + verb docstrings + type signatures
      │
      ├─ (1) FastMCP → tools/list → cold-start banner
      │      (small models read this; what they need, pushed)
      │
      └─ (2) Ingest preprocessor: {{include schema:put#arguments}} →
             arg table rendered inline into skill markdown
             (browsing models read embedded chunks)

[ skill markdown: src/precis/data/skills/*.md ]
      │   human-authored prose around (2)
      │
      ├─ examples ("here's how put-a-memory looks in practice")
      ├─ personas ("you are a citation reviewer")
      ├─ runbooks ("polish a paper using these personas")
      └─ when-to-use prose, gotchas, conventions
```

The same code drives both (1) and (2), so the small-model surface
and the browsing surface cannot drift from each other or from the
implementation. Authors who change a verb edit `tools/core.py`;
the next boot re-ingests skills and the embedded arg tables
update. The cold-start banner updates the same boot.

## Decisions (settled)

1. **Phase 2 only — no Phase 1 file-backed shim.** Skills go
   DB-backed through `chunks` + `ref_segments` +
   `ref_segment_sentences`. The `FileCorpusIndex` path is
   retired.

2. **Full re-ingest on code ship.** No backwards-compat hacks; the
   content is shipped with code, so any change re-ingests on next
   boot.

3. **H2 is the description, written in active goal-voice.**
   Headings answer "what is the user trying to do?" in the form
   the user would say it. The implicit template is `I want to <X>`
   and the H2 carries `<X>`:
   - `## Find a paper by topic when you don't know the title`
   - `## Check that a manuscript's citations are good`
   - `## Save a quick scratch thought to revisit later`

   Not nominalisations like `## Finding a paper…` or
   `## Citation audit procedure`, and not bare nouns like
   `## Search`.

   Why: agent queries naturally take the goal-statement form
   ("I want to find a paper for a topic but I don't know the
   vocabulary"). H2s in the matching form embed in the same
   semantic space — the query lands on the right section
   without paraphrase. Authors writing H2s read them aloud
   after "I want to…" and check the sentence is well-formed.

   The segmenter already stores headings
   (`ref_segments.heading TEXT`, migration 0005); no new column.
   *Validated by retrieval experiment 2026-05-31 (re-run with
   active-voice variant 2026-06-01) — see
   `scripts/_h2_experiment.py`. P@1 by config on a 9-section /
   15-query fixture:*
   - *A (short H2 + body): 0.87, MRR 0.933*
   - *B (long nominal H2 + body): 0.93, MRR 0.967*
   - *C (long nominal H2 only): 0.93, MRR 0.956*
   - *D (long active goal-voice H2 + body): 0.93, MRR 0.967*

   *D ties B on retrieval; adoption of active goal-voice is
   justified by ergonomics (author intuition, alignment with
   agent query phrasing, alias-group affinity), not by a raw
   precision gain at this corpus size. Re-run at scale post-
   migration to confirm at higher corpus volume. The persistent
   miss in B/D ("link a new memory to a paper I am citing" →
   ranked 'Creating a memory' top-1) is exactly the alias-group
   case from decision 4 — adding `## Save a memory that already
   cites a paper` would close it.*

4. **Multi-description per chunk via alias-group convention (v1).**
   Consecutive H2 headings with no body between them form an
   *alias group*: the chunker buffers the headings, then attaches
   the next body to all of them. Each H2 produces its own chunk
   carrying the shared body, embedded independently.

   ```markdown
   ## Check that citations are valid
   ## Run an in-depth check of citations
   ## Verify sources are real

   <body content describing the operation>
   ```

   yields three chunks differing only in heading. Each angle
   embeds under its own goal-statement; any of the three queries
   ("how do I check citations?", "verify sources?", "in-depth
   audit?") lands on the operation. Gripe feedback flows by
   adding more H2s to the group — no special syntax, pure
   markdown.

   Storage cost is N× chunk and sentence rows for affected
   sections; accepted (re-ingest wipes and rewrites; storage
   minimum). Render-side dedup collapses near-duplicate hits by
   `chunks.text` hash, showing one row with "also matches: …".
   H2-anchor addressing (`slug#check-citations-valid` vs
   `slug#verify-sources`) disambiguates by intent. Implementation
   is ~20 lines extending `chunk_by_h2`; no schema change.

5. **Header-tree segmentation rule.** Walk H1 → H2 → H3. Each
   section becomes a segment if its body fits the chunk budget.
   If not, recurse into sub-headers or DP+KeyBERT *within* the
   section (carrying the parent header as a label prefix). **DP
   never crosses a header boundary.** Replaces the paper-only
   "h2 vs embedding" binary in `segment_toc.py`.

   Alias-group H2s (decision 4) preserve the chunk-per-H2
   invariant: each H2 in the group is its own segment, each
   sharing the same body content. From the segmenter's
   perspective they're independent segments that happen to
   carry identical body chunks.

6. **Two-tier addressing.** Numeric `~N` for casual references;
   H2-anchor `slug#h2-slug-text` for stable deep links. H2-slug
   derived from heading text at ingest. No sidecar manifest
   needed for wisdom-tradition content.

7. **One kind, multiple flavours.** `kind='skill'` is the only
   runtime kind. The flavour distinction is carried entirely by
   a `FLAVOR:<value>` tag — the frontmatter field is a
   convenient authoring shortcut that the ingest emits as the
   literal tag string. So `flavor: persona` in frontmatter →
   tag `FLAVOR:persona` on the ref. Defined values:
   ```
   FLAVOR:reference   # "how to invoke `put`"
   FLAVOR:persona     # "you are a citation reviewer"
   FLAVOR:runbook     # "audit a manuscript before release"
   FLAVOR:concept     # "what precis is, the seven verbs"
   ```
   Uppercase prefix is deliberate: per the existing tag
   convention (`UPPERCASE:value` *replaces within prefix*),
   setting a new `FLAVOR:` value displaces the old one. A
   skill carries exactly one flavour at a time, enforced by
   the existing tag-replace machinery — no extra validation
   needed. Default search fans out across all flavours; agents
   filter with `tags=['FLAVOR:persona']` when they want one
   slice.

8. **Persona skills** carry an `## Adopt this persona` H2 whose
   body is the second-person prompt agents read to *become* the
   persona.

9. **Runbook skills** that orchestrate personas declare them in
   frontmatter:
   ```
   invokes_personas:
     - precis-adversarial-reviewer
     - precis-citation-reviewer
     - precis-flow-reviewer
   ```
   The orchestrating agent spawns one sub-agent per persona and
   aggregates findings. Reviewer findings flow into
   `kind='finding'` (out of scope here).

10. **Template includes resolved at ingest, two sources.**
    Syntax: `{{include <source>:<id>#<section>}}`.
    - `{{include doc:precis-common#address-grammar}}` — content
      include from another skill file.
    - `{{include schema:put#arguments}}` — code include; the
      preprocessor introspects the verb's signature, type hints,
      and docstring `Args:` block in `tools/core.py` and emits a
      markdown table.
    Each substitution preserves an HTML-comment marker
    (`<!-- inlined-from: schema:put#arguments -->`) for
    traceability. Duplication is accepted; embedding rerank
    handles ranking.

11. **Boot-time scan-and-ingest, unified across shipped corpora.**
    On server boot, one top-level scanner walks every
    subdirectory under `src/precis/data/` and dispatches per
    kind:

    ```
    src/precis/data/
      skills/   → kind='skill'   (FLAVOR: tag + alias-group chunker + persona/runbook conventions)
      oracle/   → kind='oracle'  (wisdom-tradition conventions)
      axes/     → tag taxonomies (axis-vocabulary structure)
    ```

    For each file: hash (including expanded includes), compare
    to the stored `ref_sha256`, skip if unchanged, otherwise
    dispatch to the per-kind ingest handler. **Shared at the
    top level**: scanner loop, hash-cache invariant, claim /
    swap protocol. **Per-kind**: chunking, segmenting, tag
    emission, frontmatter rules. Adding a new shipped corpus
    is a per-handler change; the scaffold stays put.

    Re-ingest is race-safe via an advisory lock + transactional
    swap, mirroring the pattern in `segment_toc.py:797-811`:

    ```sql
    BEGIN;
      -- Claim this slug for the duration of the tx; returns
      -- false (and we skip) if another worker holds it.
      SELECT pg_try_advisory_xact_lock(hashtext(slug));
      -- Re-check the stored hash hasn't moved since we decided
      -- to ingest (another worker may have just won the race).
      DELETE FROM ref_segment_sentences WHERE ref_id = $rid;
      DELETE FROM ref_segments          WHERE ref_id = $rid;
      DELETE FROM chunk_embeddings      WHERE chunk_id IN (...);
      DELETE FROM chunks                WHERE ref_id = $rid;
      INSERT INTO chunks ...;
      INSERT INTO chunk_embeddings ...;
      INSERT INTO ref_segments ...;
      INSERT INTO ref_segment_sentences ...;
      UPDATE refs SET file_sha256 = $new WHERE ref_id = $rid;
    COMMIT;
    ```

    Embedding (the slow part — calling bge-m3) runs *before* the
    transaction so we never hold a lock across slow I/O. Readers
    see old state via MVCC until commit; partial mid-write state
    is impossible.

12. **Doc-writer persona is an authoring-time tool.** A persona
    skill (`precis-doc-writer`) exists so humans / agents can run
    it when *writing* skills — the output (long descriptive H2s,
    eventual multi-angle alternates) is checked into git as plain
    markdown. Ingest stays deterministic; no LLM at ingest.

13. **Availability gating via tags.** Skills that only apply when
    an env-var is set declare it in frontmatter:
    ```
    available-when: PRECIS_EPO_KEY
    ```
    Ingest mirrors this as a tag the search-time filter respects.

14. **Personas ship with code, alongside other shipped corpora.**
    Personas live under `src/precis/data/skills/personas/`,
    parallel to existing shipped content like
    `src/precis/data/oracle/` (wisdom traditions) and
    `src/precis/data/axes/` (paper-tag taxonomies). The
    boot-time scan-and-ingest pipeline (decision 11) is the
    same for all of them — what differs is the destination
    `kind` (`skill` for personas, `oracle` for wisdom
    traditions, etc.) and the chunker/segmenter conventions
    each corpus follows. No user-authorable personas in v1.

15. **`precis-overview` dissolves.** Verbs table, kinds table,
    address grammar, examples distributed to per-skill
    destinations or pulled into `precis-common` for cross-cutting
    inclusion. A thin "tour" skill remains for the curious; it is
    no longer a load-bearing entry point.

16. **Small-model tool-call surface is unchanged.** FastMCP
    exposes the seven verbs' docstrings + JSON schemas via
    `tools/list`; small models read property `description`s
    directly. This redesign only affects the *browsing* surface
    (`get(kind='skill', ...)`, `search(kind='skill', ...)`). The
    one intersection: `PRECIS_STARTUP_SKILLS` pins skills into
    the cold-start banner; longer H2 headings add a few tokens
    per pinned skill. Operator-controlled, generally negligible.

## Non-goals (this design)

- **No changes to the `finding` kind** or the chase worker.
  Reviewer personas link *into* findings but don't redesign them.
- **No changes to the MCP tool-call surface.** `tools/core.py`
  docstrings + JSON schemas stay as-is; FastMCP keeps serving
  them via `tools/list` unchanged.
- **No user-authorable docs/skills.** Everything ships with code,
  ingested at boot. User scratch content stays in `memory`,
  `markdown`, etc.
- **No new doc kind.** `FLAVOR:` tag distinguishes reference vs
  persona vs runbook vs concept within the existing `skill` kind.

## Schema

**No new tables. No new columns. No new migration.**

- Existing `ref_segments.heading TEXT` (0005) carries the long
  descriptive H2.
- Existing `kind='skill'` row in `kinds` covers everything.
- Frontmatter additions (`flavor`, `invokes_personas`,
  `available-when`) parse into existing columns or tags — no
  schema delta.

## Module layout

```
src/precis/
  handlers/
    skill.py              # refactored — discovery-layer-backed
    _skill_common.py      # new — shared frontmatter + ingest helpers
  data/
    skills/
      personas/           # new — shipped reviewer / orchestration personas
      refs/               # optional organisational subdir for flavor:reference
      runbooks/           # optional organisational subdir for flavor:runbook
      precis-common.md    # new — shared content for {{include doc:...}} expansion
      *.md                # existing files migrated to the new conventions
  ingest/
    text_chunker.py       # extended — header-tree segmentation
    skill_ingest.py       # new — boot-time scanner + template preprocessor
    skill_template.py     # new — {{include doc:...}} and {{include schema:...}} resolution
  workers/
    segment_toc.py        # extended — recursive H2 segmentation with DP fallback inside oversized sections
```

Subdirectories under `data/skills/` are for human organisation; the
runtime walks recursively and treats every `*.md` as a skill of
whatever `flavor:` it declares. Flat layout is also valid.

## Migration steps (order)

Single-PR sweep, ordered internally so each step is independently
testable:

1. **Foundation.** Frontmatter parser in `_skill_common.py`
   (consolidates the existing hand-rolled YAML in
   `skill.py:1337-1357`). Flavour vocabulary defined, validation
   added. Smoke test: parse a sample frontmatter, verify field
   extraction.
2. **Template preprocessor.** `{{include doc:...}}` and
   `{{include schema:...}}` resolution. Schema-source reads
   verb signatures + docstring `Args:` blocks from
   `tools/core.py` (and equivalent for handler kindspecs).
   Standalone unit tests.
3. **Ingest path.** Extend `chunk_by_h2` with (a) header-tree
   recursion (oversized sections fall back to DP+KeyBERT
   *within* the section) and (b) alias-group detection
   (consecutive H2s share the following body, one chunk per
   H2). Extend `segment_toc` for header-tree label propagation.
   Wire the kind-generic boot-time scanner with the
   advisory-lock + transactional-swap pattern. Verify one
   hand-authored skill round-trips through ingest → search →
   `slug/toc` view, including an alias-group section.
4. **Skill refactor.** Retire `FileCorpusIndex`. `SkillHandler`
   now queries DB-backed discovery layer. Frontmatter `flavor:`
   parsing wired into the tag-emission path.
5. **`precis-common.md` authoring.** Extract the ~10 cross-
   cutting blocks identified in the audit (address grammar, arg
   grammar, tag semantics, TOON shape, link-target form, env-
   gating boilerplate, "check if ingested" idiom, cache TTLs,
   failure-mode signatures, result-shape TOON).
6. **Content migration.** Split the 13 hybrids into two skills
   each (one `flavor:reference`, one `flavor:runbook` or
   `flavor:concept`). Rewrite H2s as long descriptive headings.
   Replace hand-written verb argument tables with
   `{{include schema:...}}` markers. Dissolve `precis-overview`
   into per-skill content + `precis-common` includes + a thin
   tour skill.
7. **Persona authoring.** Resurrect reviewer personas from old
   repos (or write fresh). Ship under `data/skills/personas/`
   with `flavor:persona` and adoption-prompt H2s.

## Tests

- **Unit:** frontmatter parser, template include expansion (both
  content and schema sources), header-tree segmentation (including
  recursion into oversized sections), alias-group chunker
  (consecutive H2s share following body, one chunk per H2),
  render-time dedup of near-duplicate alias hits, flavour-tag
  emission.
- **Integration:** boot-time scan-and-ingest produces correct
  `ref_segments` rows for a sample skill; re-ingest is a no-op
  when the file hash is unchanged; re-ingest replaces segments
  when content changes.
- **Retrieval:** the bench at `scripts/_h2_experiment.py`,
  re-run on the migrated corpus, asserting P@1 ≥ 0.85 against a
  hand-labelled query set. Regression floor.
- **Schema-drift backstop:** scan every `flavor:reference` skill;
  for each example code block, extract the verb + argument
  names; assert each argument exists in the current `tools/core.py`
  schema. Catches the case where someone renames a verb arg but
  forgets to update an example (the auto-generated table updates,
  the hand-written example doesn't).

## Quality gates

Authoring conventions only work if drift gets caught. Two flavours
of gate, both shipping with v1.

### Static gates (deterministic, hard-fail at ingest)

Each is cheap to compute and produces a clear pass/fail. A skill
with a failing static gate **does not ingest**; the boot scan logs
the failure with file + line, and the previous version (if any)
stays live.

- Frontmatter parses; `flavor:` is one of the four defined
  values.
- `FLAVOR:` tag emission matches the frontmatter (sanity check
  on the ingest pipeline).
- For `FLAVOR:persona`: body contains an `## Adopt this persona`
  H2.
- For `FLAVOR:runbook`: `invokes_personas:` entries resolve to
  existing persona files at ingest time.
- Every `{{include doc:...}}` and `{{include schema:...}}`
  directive resolves.
- Every `[[skill:X]]` link resolves.
- H2 headings are non-empty and don't start with bare-verb
  nominalisations (`## Search`, `## Get`, `## Put`, …) — a
  heuristic for the old style we're moving away from.
- Schema-drift backstop (cf. Tests § "Schema-drift backstop"):
  example code blocks in `FLAVOR:reference` skills reference
  arguments that exist in the current `tools/core.py` schemas.

### LLM gates (judgment, soft-fail as gripes)

These don't block ingest. They run on a schedule (or in CI when
skill files change) and emit findings as `kind='gripe'` records
linked to the offending skill. Gripes already carry the right
shape — text + link target — and they don't pollute hard tests.
Maintainers triage the gripe stream the way they do today.

- **H2 voice check.** "Does this heading read naturally after
  'I want to …'?" Per-H2 yes/no with one-line rationale.
  Catches drift back to nominal phrasing.
- **Alias-group spread.** "Are the H2s in this consecutive-H2
  group genuinely different user angles, or paraphrases of the
  same angle?" Catches low-diversity alias groups.
- **Persona authenticity.** "Does the body actually teach the
  persona declared in `## Adopt this persona`?" Catches drift
  between identity and method.
- **Example-prose agreement.** "Does each worked example
  demonstrate what the surrounding prose claims?" Catches drift
  between assertion and demonstration.
- **H2 self-sufficiency.** "Reading just this H2 (no body),
  would you know what operation the section covers?" Catches
  H2s too terse to function as the description we're claiming
  they are.

### The authoring / review triplet (shipped skills)

Three skills form the loop that keeps conventions living rather
than rotting:

| Skill                                  | Flavour     | Role |
|----------------------------------------|-------------|------|
| `precis-skill-author-best-practices`   | reference   | The rules — H2 voice, alias-group conventions, persona shape, runbook orchestration, include syntax. Embedded once; linked from everywhere. |
| `precis-doc-writer`                    | persona     | "You are a skill author writing to the conventions in `[[skill:precis-skill-author-best-practices]]`." Used at *authoring time* (humans + Claude run it when adding/editing skills). |
| `precis-skill-reviewer`                | persona     | "You are a reviewer auditing skills against `[[skill:precis-skill-author-best-practices]]`. Produce a gripe per finding." Used at *review time* — runs the LLM gates and emits gripes. |

The reviewer persona is invoked via `src/precis/utils/claude_p.py`
(in-flight on this branch). Run as a periodic worker pass or as a
`precis lint skills` one-shot command. Findings become gripes,
linked to the offending skill via the normal `link=` mechanism.

Implementation cost: ~half a day for the static-gate test code
(reuses frontmatter parser + ingest preprocessor); 1–2 days for
the LLM gates (reviewer persona authoring + a thin worker harness
around `claude_p`).

## Open issues (deferred)

- **Multi-description authoring conventions.** When v2 adds
  sibling alt-descriptions per chunk, what does the syntax look
  like? (Frontmatter list per H2? `### alt:` sub-blocks?) Defer
  until gripe-feedback data motivates the shape.
- **Reviewer-finding wiring.** When the polish-paper runbook
  ships, how exactly do persona findings tag into
  `kind='finding'`? Revisit when polish-paper is authored.
- **Stable refs for wisdom-tradition deep-links across editing.**
  Two-tier addressing (`~N` + `#h2-slug`) is the v1 answer; if
  heading text gets edited frequently and breaks `#h2-slug`
  references, revisit. Probably never an issue in practice.
- **Schema include for handler-side kindspecs.** v1 covers verbs
  in `tools/core.py`. Kind-specific args (e.g., `citation`'s
  named kwargs) live on handler kindspecs; extending
  `{{include schema:...}}` to read those is a small follow-up
  once the verb path works.
