# `source-backfill` — find the sources you missed, and build the workspace to weave them in

> Status: **design / for review** (2026-07-13). No code yet — review
> this first. This is the *application* that drives the ADR-0051 "eyes /
> working set" machinery (built, shipping dark) toward one concrete job:
> given a draft section, find sources **already in the corpus** that it
> *should* cite but doesn't, assemble a rich editing workspace grounded in
> them, and integrate them into the prose.
>
> It is the **recall** mirror of the citation **verifier** (`citation`
> kind, `verifier_confidence`): the verifier asks *"is what I cited
> true?"* (precision); source-backfill asks *"did I miss anything?"*
> (recall). Opposite directions, complementary, never conflated.

## Motivation — the quest

You (or the planner) are working a section of a draft. It cites a few
papers. But the corpus holds *other* papers that bear on the same
claims and were never cited — because the search that would have found
them was never run, or ran before those papers were ingested. The quest:
**surface those missed sources and integrate them**, and give the LLM a
*ready-made* (or LLM-assisted, with an extra search) **workspace** to do
the work in.

The deliverable is not a nicer reading context and not a report — it is
**found-and-integrated missed citations**. The eyes/working-set render is
just the substrate the work happens on. So the one distinction the whole
flow turns on is **cited vs uncited**: the uncited-but-relevant hits are
the product.

## What already exists (build on, don't rebuild)

- **The eyes / working set (ADR 0051 §6/§15) — built, dark.**
  `workers/working_set.py` (the `Eye`/`WorkingSet` model: extent ladder
  `kwd < summary < verbatim < fisheye < fisheye+1hop`, persistence
  `transient/normal/pinned`, provenance `requested/inferred`, decay ladder
  + bunched `crunch`), `utils/fisheye.py` (reading-order neighbourhood for
  tree kinds), `utils/refeye.py` (the 1-hop **reference ring**: Cited /
  Cross-refs / Notes, edges-only), `utils/eye_render.py` (per-kind render —
  doc kinds → keyword-cluster TOC), `utils/working_set_render.py` (compose
  N eyes into **one deduplicated context**, demanded-extent map, gap
  closing). None of it is imported on the live path yet.
- **The render path is wired for one caller already.** The draft reader
  (`precis_web/draft_eyes.py`) writes `meta.working_set = {eyes:
  [{handle, extent}], edit_hint: […]}` onto a change-request todo, and
  `workers/planner_prompt._render_reader_working_set` (line ~1346) renders
  it via `render_working_set`. So *writing an eye list and having it
  rendered works today* — source-backfill supplies the eye list, not the
  render plumbing.
- **The filtered search — live.** `search(kind='paper', queries=[…],
  answers=[…HyDE], per_paper=N, folder=, since=, until=)` RRF fusion
  (`tools/core.py:369`, `store.search_blocks_multi`); `good=True` mints an
  async deep campaign (see `good-search-coordinator.md`).
- **Draft→paper `cites` edges — materialized.** The reference ring's
  "Cited" group; "what does this section already cite" is an exact query.
- **Per-chunk KeyBERT keywords with scores** (`chunks.keywords TEXT[]` +
  `chunks.keywords_meta` `{short,long,score}`, F20) — the raw material for
  section-level keyword rollups; `toc_db` already rolls a doc-level
  `Topics:` line.

**Not yet materialized:** paper→paper citation edges. `ingest/citations.py`
fetches S2 `references` + `cited_by`, but only `chase.py`/`watch_poll.py`
use it, as an *acquisition trail* — never stored as queryable in-corpus
edges. This is the biggest new build for the strongest recall lens (below).

## Shape

```
source-backfill  =  FIND  →  WORKSPACE  →  INTEGRATE
                    (recall) (eyes)        (weave + follow-up)
```

**FIND** — a multi-lens recall sweep, deduped against cited ∪ dismissed,
ranked by gap-value. **WORKSPACE** — the survivors + the cited context +
the draft, assembled as one self-describing TOC-with-fisheye render.
**INTEGRATE** — the model weaves held sources into the prose now; not-held
candidates are requested and parked on a self-resolving follow-up.

## FIND — the recall lenses (multi-modal sweep)

Each lens finds *different* misses; no single angle catches everything.
Run in parallel, union + RRF, and **lens-agreement is the confidence
signal** (a paper found by three lenses is a strong miss; one from a lone
fuzzy semantic leg is weak). The section **programs its own recall** — the
filter values are derived from the section at prep time, so the sweep is
"ready-made."

| Lens | Seed (from the section) | Verb / filter | Finds |
|---|---|---|---|
| **text** (semantic) | claims → HyDE `answers=`; questions → `queries=` | live | topically-adjacent papers |
| **keyword** | `chunks.keywords` | `keywords @>` (GIN) — *SQL today* | exact keyword co-occurrence |
| **number** | `chunks.numerics` | `numerics @>` (GIN) — *SQL today* | "who else reports 1.2 mS/cm" — a *provable* quantitative gap |
| **finding** | — | `ROLE3:own` soft-boost | ranks papers on their *contribution* chunk, not furniture (soft-boost only — 91%-precision caveat forbids a lone hard gate) |
| **citation-graph** | the section's cited papers | S2 `references`/`cited_by` | papers your citations cite / are cited by, which you **hold but skipped** — the unarguable omission. *Biggest new build* (materialize the edges). |
| **intra-doc recurrence** | the section's topic | `search` over the same draft | where else the topic is discussed → place foci (see Multi-focus) |

Scoping filters (all live): `kind='paper'` (only sources become
candidates), `per_paper=1` for the breadth sweep (20 papers, not 20 chunks
of one — coverage, not depth; raise to 2–3 when *drilling* an adopted
candidate), `folder=project`, `since=`last-sweep.

## FIND — flood control as a cascade (ADR 0047's shape)

Recall is a flooding problem; cull with the same cascade the classifier
uses — cheap model does the coarse high-volume calls, strong model sees
only the residual. **The search filters are the first, cheapest stage of
flood control** (filter-then-judge beats judge-everything).

- **Tier 0 — free/deterministic.** Dedup against **cited ∪ dismissed**
  (paper identity — reuse `paper_reconcile`; see backlog), drop non-papers,
  drop chunk-less stubs.
- **Tier 1 — lesser/local model** (`summarizer` alias / haiku via
  `utils/claude_p.py` one-shot JSON judge). Batched relevance gate: per
  candidate×claim, score 0–3, keep ≥2. Turns ~200 hits into ~15.
- **Tier 2 — strong model, in the workspace.** Only survivors become eyes;
  opus reasons about *integration*, never triage.

Gap-rank is **model-free**: centrality (embedding similarity to the claim)
× uncited × lens-count. The model is the relevance *gate*, not the ranker.

## WORKSPACE — the render contract

**One principle: every document (draft and paper) renders as its full TOC,
everything present, with fisheye expansion wherever we work or wherever the
worked region points.** Nothing is a detached fragment; a referenced chunk
expands *in its own outline position*. Collapsed sections stay as
self-describing TOC lines. It spans to end-of-doc.

### Grammar — content is sacred, meta is keyed

```
<glyph> <handle> · <extent> · <status>
    "verbatim bytes are always quoted"        ← extent=verbatim (the document, exact)
    a plain sentence is a summary             ← extent=summary
    kw: term · term · term                    ← extent=kwd
    cites / gaps / under / refs:  …           ← META, always keyed — never prose
```

Content is **quoted**; meta is **keyed**; nothing else is prose; no
box-drawing. This is not only for legibility — it is the **sacred-content
invariant** that makes editing safe: on a whole-source rewrite, anything
inside the quotes round-trips as document bytes, so *all scaffolding lives
outside the quotes*. Indent = abstraction level (no `H1`/`H2` labels — a
line with a Title + child-count is a heading; a line with `· <extent>` +
body is content).

### Glyph families = the deterministic / suggested seam

The render is **both** deterministic and model-authored, in layers, and the
glyph family tells you which — so you know what to trust:

- **Facts** `✓ ★ ← · ⋯ +N` — store rows, reproducible: cited-status, cited
  chunks, back-refs, TOC skeleton, keyword rollups. A fact-glyph never lies.
- **Suggestions** `○ → ✎` — the model's proposals: a gap, "would support
  dc…", an edit candidate. Always safe to reject.

Model-authored inputs: HyDE `answers=` seeds, the Tier-1 relevance cull, and
the `→ would-support` claim-mapping. Everything structural is deterministic.

| glyph | means | family |
|---|---|---|
| `▸` | cursor — the block we're working on | fact |
| `·` | collapsed bookmark, drillable — `focus` to expand | fact |
| `⋯ N more ⋯` / `+N` | folded run / span size — never a silent omission | fact |
| `↑` | ancestor breadcrumb (section path) | fact |
| `✓` | cited — source already in the draft's references | fact |
| `★` | a chunk of *this* paper the draft already cites | fact |
| `←` | back-ref: which of *your* draft chunks cite this (scoped to this draft) | fact |
| `◦` | inferred/transient eye — from search, fades next crunch unless adopted | fact |
| `○` | new — candidate, not yet cited (the gap) | suggestion |
| `→` | which claim a candidate would support | suggestion |
| `✎` | edit candidate / matched candidate chunk (expanded) | suggestion |
| `⚠` | coverage warning — **a claim WE assert** that is uncited / single-source | fact (about our text) |

### Extents — bare handles are the norm, extent is the exception

An eye is a statement of **attention** (*what* to look at); **fidelity**
(*how much*) is resolved by the system from three levels:

1. **kind-default** (`draft_eyes._default_extent`): doc-kinds → `summary`;
   draft-chunk/note → `fisheye+1hop`.
2. **mode policy** (source-backfill): cited paper → `summary`; candidate
   paper → `summary` **with its matched chunk force-expanded to verbatim**;
   edit target → `verbatim`/`fisheye`.
3. **explicit `extent`** in the eye dict → overrides both.

So the eye list is mostly **bare handles** (`['dc3243x', 'pa234',
'pc7710']`), reading as *intent*, not render config. `extent` is a
deliberate override, used only to (a) open deeper than policy, (b) **pin
against the crunch** (extent + `pinned`), or (c) collapse what policy would
expand. This is load-bearing: the **size-crunch governor demotes eyes down
the ladder to fit the budget** — an explicit extent pins an eye against it,
so bare handles are *what lets "the context window is the cutoff" work*.
`★`/`✓`/`←` are auto facts, never specified.

> **Seam to fix:** the kind-aware default lives in
> `draft_eyes._default_extent`, but `working_set.Eye.from_json` defaults to
> `FULL`. Reconcile: the canonical "handle with no extent" resolution must
> be **kind-aware and mode-aware**, centralized, so a bare `'pa234'` never
> resolves to `verbatim`.

### Collapsed nodes are self-describing — no bare counts

Every collapsed thing = **how to get it (handle) · how much (+N) · what
(keywords)**. Never `+3 clusters`. For a draft section `+N` is its
subheadings (the drill targets), not its raw chunk count; the computed
keyword label rescues a vague author heading. For a paper cluster the
keywords *are* the label. Even a capped residual is labelled
(`+40 more · [rolled keywords]`), never bare.

**Section-keyword rollup (net-new, cheap, deterministic — fact layer):**
- *Ship first:* score-weighted union of the subtree's `chunks.keywords`
  (rank by summed KeyBERT score, dedup via short/long forms, top-K).
- *Upgrade to:* **c-TF-IDF** — terms distinctive to this subtree vs the rest
  of the doc, so sibling sections ("Methods" vs "Results") don't both roll
  up to the same bag. (Centroid-keywords is an equivalent-cost alternative.)
- Read-time, cached per ref; drafts invalidate on edit, papers are static.
- Deterministic (not an LLM label) so a collapsed label is *trustworthy*.

### Papers — clusters by default, real sections when we have them

A paper has no heading tree, so its "TOC" is the F20 keyword-cluster
grouping (`toc_db.cluster_blocks`) — cited chunks expand verbatim in their
cluster, near clusters get a summary line, far ones a keyword line + count.
**But if the paper was ingested with structure** (JATS/GROBID real
sections), render *that* heading tree instead — identical shape to the
draft, with real titles. Keyword-clustering is the *fallback* for
structure-less PDFs. (Open question — see backlog: does the Marker pipeline
retain section structure for any sources today?)

### Multi-focus — positional, document order

Multiple eyes are native (`WorkingSet.eyes` is a handle-keyed dict; the
composer merges via a demanded-extent map). Foci may be **spread across the
document, not just consecutive** — this is the more powerful mode, and it is
the right shape for the common case where **the same topic recurs across
sections with different angles**. The LLM is good at synthesizing across
those; a linear read is not.

- **Consecutive** foci → fisheyes overlap → one continuous verbatim span.
- **Spread** foci → *islands* of expansion, each in its true outline slot,
  the collapsed TOC skeleton between them. The always-present skeleton is
  **what makes spread multi-focus safe**: the model never assumes two
  islands are adjacent — each keeps its `§`-position.
- **Render is positional (document order)**, never regrouped-by-topic —
  regrouping relocates handles out of their real slots and reintroduces the
  adjacency-confusion we designed away. Preserve local context.
- **Recurrence overlay** (annotate, don't relocate): a cross-link line
  `recurs: X at dc23 (§2), dc88 (§5), dc140 (§6)` *points* to the
  positional foci. Side effect: surfaces **internal claim consistency** —
  `⚠ same claim, §2 cited / §5 uncited — inconsistent grounding` — a real
  editorial win adjacent to the coverage goal.
- **"How many at once" = the budget, not a fixed cap.** Consecutive foci
  are near-free (dedup); spread foci each cost an island; the (N+1)th that
  blows the budget demotes the lowest-salience island first.

### Handle legibility — grounding line, not `pa:pc`

`pc<id>` is keyed on the chunk's globally-unique PK; a chunk belongs to
exactly one paper (`chunks.ref_id`), so `pc2342` **already** identifies its
paper. `pa234:pc2342` is pure redundancy as a token — never emit it. The
citable handle stays terse.

Legibility (which paper, at the cite site) is a **label, not part of the
key**. Solve it with a per-block **grounding line** that groups the block's
cites *by paper* — bare `pc` stays inline in the prose, the paper picture
shows once per block:

```
grounded in  ✓ Wang'20 [pc2342, pc2344]  ·  ✓ Chen'19 [pc881]
gaps         ○ Kumar'21 [pc7710] · matched: text, keyword, finding
```

This is the diagnostic source-backfill runs on (is a section leaning on one
source? is the primary source missing?) — invisible with bare chunk ids.
It's also the natural home for the cited/uncited badge and candidate
suggestions. Needs a small `author-year` short-cite helper (byline + year,
fallback slug/title) — none exists today.

**Bidirectional back-annotation — yes, scoped to the current draft.** The
store links are symmetric; render the reverse edge *only for this draft's
pointers* (a popular paper is cited by 50 drafts — global inbound is
noise). `★ pc2342 ← dc41` in a cited paper's TOC is the "citations
highlighted in the outline" this whole thread started from. Same edge shown
from both ends (grounding line on the draft side, `←` on the paper side) is
intentional bidirectional salience — the two scans happen at different
moments, each wanting the fact locally. **Suppress the paper↔paper mesh in
this mode**: foreground draft→paper cited-ness, hide see-also rings — a
clean bipartite "draft vs the field," not a web. (The full ring stays
general-eye-mode's feature.)

## The mock (centerpiece)

```
— working set · source-backfill · dr17 · ~19k / 32k tok · glyphs: ✓★← = fact · ○→✎ = suggestion —

DRAFT  dr17 — Toward a Stable SEI
  dc234   Abstract                +4 · SEI stability · garnet · overview
  dc235   The thing about stuff   +3 · interface characterization · EIS
    dc2342   Blah
      dc3243 · verbatim   ← used by dc3243x (our edit region)
         "some detail our edit region points to, expanded in place where it lives"
      dc3242 · summary    (its neighbour, for context)
    dc6435   Other blah            +8 · cell prep · coin cell · assembly
  dc2346  Other section           +6 · dendrite · critical current · cycling · morphology
  dc5349  Our section
    dc2341   Our subsection
      dc2344   Our sub-subsection
        dc2334 · summary
        dc6563 · verbatim     "the previous paragraph, in full"
   ▸    dc3243x · verbatim · cursor · ✎ EDITING
           "The garnet electrolyte shows a room-temperature ionic conductivity of 1.2 mS/cm [pc2342], comparable to the liquid baseline [pc881]. Grain-boundary resistance dominates below 30 °C [pc2344]."
           cites  ✓ Wang'20 [pc2342, pc2344]  ·  ✓ Chen'19 [pc881]
           gaps   ○ Kumar'21 [pc7710] · matched: text, keyword, finding
                  ○ Roht'22  [pc9012] · matched: number (1.2 mS/cm)
           refs   → dc3243 (used above), → dc8891 (§5.1, expanded below)
        dc3250 · verbatim     "the next paragraph, in full"
    dc7777   Our next subsection  +12 · Arrhenius · activation energy
  dc5310  Discussion
    dc8890   Reconciling transport regimes
      dc8891 · verbatim   ← used by dc3243x
         "We attribute the sub-30 °C rollover to grain-boundary blocking, contra [pc2342]."
  dc8888  Conclusion              +6 · outlook · scale-up

CITED  pa234 · Wang 2020 — Garnet solid electrolytes · ✓ cited ×3
  ( keyword clusters — no heading tree ingested )
  pc2340  ionic conductivity · EIS · 1.2 mS/cm         · 6 chunks
    ★ pc2342 · verbatim  ← dc3243x
       "Room-temperature ionic conductivity reached 1.2 mS/cm for the cubic garnet, by EIS with Au blocking electrodes over 20–80 °C…"
    ★ pc2344 · verbatim  ← dc3243x, dc42
       "Grain-boundary resistance dominated below 30 °C, ~60% of total impedance in the Arrhenius fit…"
    · pc2347 · summary   Activation energy and temperature dependence.
  pc2350 +4 · synthesis · sol–gel · LLZO · sintering · >95% dense
  pc2361 +3 · XRD · Rietveld · cubic garnet phase
  pc2372 +2 · dopant · Al · Ta · stabilization

CANDIDATE  pa889 · Kumar 2021 — Li-stuffed garnets · ◦ ○ NEW
  pc7710  SEI · grain-boundary · cryo-EIS              · 4 chunks
    ✎ pc7710 · verbatim  → dc3243x, dc42 (would support)
       "Below 30 °C the grain-boundary contribution to total impedance rises to 68%, consistent with a blocking, self-limiting SEI that passivates within the first cycle…"
  pc7725 +1 · shear modulus · dendrite suppression     → dc44 (fills the uncited claim)
  pc7702 +3 · ionic conductivity · 1.4 mS/cm

— coverage · §3.2 (claims WE assert) —
  dc3243x  backed by 2 papers / 3 chunks     ○ 2 sources we could add
  dc42     backed by 1 paper                 ⚠ single-source     ○ 1 could add
  dc44     backed by 0 papers                ⚠ uncited assertion  ○ 2 could add   ← top gap
  ⋯ 6 blocks folded (adequately sourced) ⋯
```

## INTEGRATE — children, lanes, and the follow-up loop

The parent is a **planner coroutine** (`LLM:source-backfill`-tagged
strategic todo → `plan_tick`) — not a plain executor task — for two
load-bearing reasons: `child_job_succeeded` is guarded to never
auto-close an `LLM:*` parent, and the **working set lives on each tick's
`job.meta` snapshot** (§15), so the assembled context persists tick-to-tick
and reconstructs if a tick is killed. Its brood maps onto three existing
tree mechanisms — orchestration is *not* invented here:

```
source-backfill  (strategic todo, LLM:* → plan_tick, owns working_set)
│
├─ A. recall sweep            → COMPUTE-LANE jobs (idempotent, cacheable by filter-config hash)
│     ├─ good_search campaign      (requested→job, block via derived_job_succeeded)
│     └─ Tier-1 cull judges        (batched lesser-model relevance gates)
│
├─ B. integrate HELD sources  → INTENT-LANE child todos (do-now)
│     └─ change-request todo → draft edit job   (child_job_succeeded, guarded)
│
└─ C. pending acquisitions    → SELF-RESOLVING LEAVES (detached, revisit-later)
      └─ paper-request todo
            meta.auto_check = paper_ingested  (+ time_past ~1wk backstop)
            meta.working_set = <saved eyes>   ← resumes cold-start-free
            └─ on fire → spawns a small integration follow-up tick
```

- **A — compute-lane (ADR 0044).** Derived, content-addressed,
  cache-fillable; the parent links `requested`→job and blocks via
  `derived_job_succeeded` (migration 0046). Reuse the `good_search`
  coordinator as the recall child.
- **B — intent-lane.** A change-request child todo carrying
  `meta.working_set` (the reader's exact vehicle). Guarded
  `child_job_succeeded` closes it; a real edit failure raises the
  `child-failed:<job_id>` bubble.
- **C — this is where "do what we can now + revisit" lives.** It is not a
  feature — it is the **tree splitting**: the parent finishes the now-work
  (held sources) and can complete, while not-held candidates become
  **detached self-resolving leaves** (`paper_ingested` auto-check +
  `time_past` backstop) carrying the saved working set. The parent does not
  babysit acquisitions; the evaluator closes each leaf when its PDF lands,
  spawning a tiny follow-up tick that resumes with the exact eyes.

**Guardrails the tree enforces:**
- **Soft children by default.** A recall miss or an un-gettable paper is a
  *result*, not a failure — report, don't bubble. Reserve `child-failed` for
  genuine breakage. One un-gettable paper must not stall the parent.
- **Converge, or the nursery kills it.** The plan-tick-spin detector bubbles
  a parent minting > `PLAN_TICK_REMINT_24H` (16) ticks/24h. Each tick makes
  **monotonic progress over a bounded candidate set** (cull N, integrate
  one, request one) then yields `verdict: done` (or `ask-user:`). **Never
  re-open a rejected candidate** — hence the ledger below.
- **Parent detaches after dispatch** (does not block on every edit) — keeps
  it short-lived (90-min lease, dodges the spin detector), fully resumable
  from tree state.

### `dismissed-source` ledger — convergence, and free suppression

A rejected candidate that re-surfaces every sweep is exactly the
non-convergence the spin detector kills ("dedup vs *seen*, not vs
*confirmed*"). So Tier-0 dedup excludes **cited ∪ dismissed** — the sweep
surfaces only the *undecided*:

```
candidates  =  recall_hits  −  cited  −  dismissed
                              (decided yes) (decided no)
```

Dismissal is a scoped edge — `link(dc3243x → pa889, rel='dismissed-source',
meta={reason})` — written when the model or Tier-1 cull rejects a
candidate. Scoped to the target/draft (a paper irrelevant to §3.2 may fit
§5), reversible + reasoned (auditable "why not cited"). This is **not**
YAGNI — it is the convergence guarantee — and it hands you suppression for
free: the human dismissing in the reader writes the *same edge*. No bespoke
suppress control. (Future, YAGNI now: stale-expire a dismissal when the
target section is heavily re-edited.)

## MCP surface

**Kickoff (async, ready-made).** Mints the coroutine; its first tick does
the sweep + assembly. `targets` is **a list, always** (never str-or-list —
multi-section audits are real, and a cross-target candidate is a stronger
gap; a single target is a list of one). The call **validates synchronously
and returns a receipt** — every target resolves to a live node, lenses
known, budget sane — or a rejection that **names the bad handle** (the
handle-write-guard: catch the typo now, not a cycle later in a worker log).
Assembly defers to the next dispatch cycle.

```python
put(kind='todo',
    title='Source-backfill — dr17 §3.2', tags=['LLM:source-backfill'],
    link='draft:dr17', rel='plan-of',
    meta={'source_backfill': {
        'targets':  ['dc3243x', 'dc5349'],   # list; each handle sets its own span
        'lenses':   ['text','keyword','number','finding','citation-graph'],
        'budget_tok': 32000,
        'integrate': 'weave',                 # report | link | weave
    }})
# → accepted · td9001 · 2/2 targets live · runs next dispatch cycle
```

**Explicit assembly (the working_set write — render path live today).**

```python
edit(kind='todo', id='td9001', meta={'working_set': {
    'eyes': [
        'dc3243x',                                  # edit target → mode policy: verbatim/fisheye
        'dc8891',                                   # cross-ref → opens IN-PLACE, doc order
        'pa234',                                     # cited paper → summary/cluster-TOC, ★ auto-expand
        {'handle': 'pc7710', 'extent': 'verbatim'}, # candidate chunk — force-open (a deviation)
    ],
    'edit_hint': ['dc3243x'],
}})
```

**Minimal (intent in, workspace out).** Point at 2–3 paragraphs; the
assembly pulls in *everything* — full-doc TOC skeleton, their fisheye
neighbourhoods, every paper they cite (`★`-expanded), linked notes,
recall candidates, positional cross-refs, the coverage tail:

```python
edit(kind='todo', id='td9001', meta={'working_set': {
    'eyes': ['dc3243x', 'dc3250', 'dc3251'],   # three paragraphs — the rest assembles
    'edit_hint': ['dc3243x'],
}})
```

**Sync-inline** is the alternative to async kickoff: an interactive read
that runs recall + assembly *within the call* and returns the rendered
workspace now (search + a lesser-model cull is seconds) — same
`targets`-validation front door, the work just doesn't defer. Single-eye
drill primitive (proposed — wires the dark eye-render live via `get`'s
`args` passthrough): `get(kind='draft', id='dc3243x',
args={'extent': 'fisheye+1hop'})`.

## The reader-asymmetry rule (belongs prominently in the skill)

The **writer** (the LLM) is context-rich — it sees `pc7710`'s full text,
the coverage map, the cross-refs. The **reader** of the printed paragraph
is context-poor — they get the sentence and a citation marker `[12]`,
nothing else. Rich context *creates* the failure mode: the writer
under-explains because it already knows the grounding. So the paragraph
under construction must:

1. **State the claim in full** — the reader can't hover `pc7710`; the
   substance the citation points *to* must be *in the prose*. The citation
   is **provenance, not content**.
2. **Never leak scaffolding** — no "as shown above," no handle in the prose
   (`pc7710`/`dc…` are workspace *addresses*; the export cite is
   `\cite{kumar21}` / `[12]`); the glyphs and coverage tail don't exist for
   the reader.
3. **Survive a cold read** — cut from all context, it must still make sense.

> ✗ "This aligns with the 68% grain-boundary contribution below 30 °C."
> ✓ "Below 30 °C, grain-boundary resistance dominates total impedance —
>   Kumar et al. report a 68% contribution [12] — consistent with a
>   blocking SEI."

The `precis-source-backfill` skill body is prepended to the turn prompt
(editing the skill edits the prompt), so this is a standing instruction
with a one-line **cold-read test**: *"Would this paragraph make sense to
someone holding only the paragraph and a bibliography? If not, you're
writing to your context window, not to the reader."* It rhymes with the
sacred-content rule — the content side of the boundary is exactly what the
reader inherits. **General principle** (hoist toward the shared
draft-authoring skill — the planner, figure, and edit paths all live in
rich context and export to context-poor readers): *the working set is a
lens for the writer, never a substrate for the reader; the richer the
writer's context, the more discipline the prose needs.*

## Slices

1. **Semantic recall + Tier-0/1 cull + read-only workspace.** `text`
   lens (live search) → dedup vs cited → lesser-model gate → assemble eyes
   → wire the dark render live (`get(args={'extent'})` +
   `render_working_set`) with the cited/candidate annotations. No writes.
   Proves the workspace. **— DONE** (`src/precis/backfill/`; `handlers/draft.py`
   `get(extent=…, view='backfill')`; `tests/test_backfill.py`). Lesser-model
   gate deferred into slice 4's cascade — slice 1 surfaces raw text-lens hits.
2. **Section-keyword rollup (union) + self-describing collapsed nodes +
   grounding line + scoped bidir.** The render contract in full. **— DONE**
   (`utils/section_keywords.py` roll-up wired into the composer's collapse
   marker; `utils/short_cite.py`; `render_backfill` grounding block ✓/⚠; the
   folded-in source roles `★ cited ← <section>` / `○ candidate` via
   `render_working_set(marks=…)`). Deferred: `←` *inline* on the citing draft
   chunk (redundant — the `paper:<id>` shows in the verbatim text) and per-`pc`
   stars inside a cluster-TOC (citations are ref-level, so the star is on the
   paper eye, not a chunk row).
3. **Citation-graph lens.** Materialize S2 `references`/`cited_by` as
   queryable in-corpus edges → the provable-omission lens. **— DONE**
   (`src/precis/backfill/citation_lens.py`; `tests/test_citation_lens.py`).
   No migration — the `cites`/`cited-by` relations already exist, so edges
   land in `links` (idempotent, one-direction `cites`; `cited-by` is the
   read-time rewrite). **Lazy + corpus-internal**: when the lens runs on a
   cited paper it fetches that paper's neighbours once (TTL-gated by a
   `citation_edges` ref_event, default 30d), resolves each against
   `ref_identifiers`, and writes an edge **only** when the neighbour is a held
   ref — neighbours not in the corpus are ignored (acquisition is chase/watch's
   job). Then `citation_neighbor_degrees` is pure SQL, ranking held-but-uncited
   neighbours by co-citation degree; body-less stubs are filtered out.
   Merged into `find_candidates` as lens `citation`: a paper both lenses find
   gets an agreement badge (`text+citation`), citation-only neighbours fill the
   remaining slots. The S2 call sits behind a monkeypatchable
   `fetch_citations` seam (tests need no `[paper]` extra); the whole lens
   self-disables on any failure or via `PRECIS_BACKFILL_CITATION_LENS=0`, so
   the text lens always carries the workspace. Deferred: a pre-warm worker pass
   (edges materialize lazily on backfill runs today; a corpus-wide crawler that
   fills them ahead of time is an upgrade, not a prerequisite).
4. **INTEGRATE — the coroutine + lanes.** `LLM:source-backfill` parent,
   compute-lane recall child, intent-lane weave, `paper_ingested` follow-up
   loop, `dismissed-source` ledger. **— IN PROGRESS.** Leading edge landed: the
   **dismissed-source ledger** (`src/precis/backfill/dismissed.py`;
   `tests/test_dismissed.py`) — `dismiss_source` / `dismissed_ref_ids` over a
   controlled `DISMISSED_SOURCE:<ref_id>` draft tag (migration-free; upper-case
   per `tags_namespace_check`), folded into `assemble`'s Tier-0 exclude
   (`cited ∪ dismissed`) so a rejected hit never resurfaces, while a dismissed
   paper is kept **out** of the citation-graph seed (suppression ≠ citation).
   **The coroutine is now wired** — and, crucially, it needed **no new
   job_type and no dispatch change**. Reviewer-mode is the precedent: a
   specialised tick is just a `meta` marker + gated variable-layer modules under
   the same `plan_tick`. So a source-backfill run is a todo tagged `LLM:<model>`
   \+ `meta.backfill={'targets':['dc…']}` (falls back to `meta.anchor`); dispatch
   already runs any `LLM:*` todo as `plan_tick`. A new `has_backfill` predicate
   (`utils/prompt/predicates.py`) gates a `_m_backfill` variable module
   (`workers/planner_prompt.py`) that injects the `render_backfill` workspace
   (★ cited / ○ candidates / ✓⚠ grounding) plus the **weave / dismiss / request**
   instructions:
   - **weave** — cite the supporting chunk by handle `[pc<id>]` (the existing
     planner contract already documents cite-by-handle + writing for the human
     reader);
   - **dismiss** — `tag(kind, id=<draft>, add=['DISMISSED_SOURCE:<ref>'])`, read
     back by `dismissed_ref_ids` into the next run's exclude set;
   - **request** — the existing `paper_ingested` wait-leaf flow the contract
     spells out.
   The plan-tick-spin detector guards convergence; the `paper_ingested`
   evaluator already exists. Tests: `tests/test_prompt_assembly.py`
   (`test_has_backfill_predicate`,
   `test_planner_backfill_todo_gets_workspace_and_instructions`). So the whole
   FIND → WORKSPACE → INTEGRATE loop runs end-to-end today via a plain
   `put(kind='todo', tags=['LLM:opus'], meta={'backfill':{…}})`. The recall
   **semantic** leg is wired too: `recall_embedder(store)`
   (`backfill/workspace.py`) builds the **remote** HTTP embedder inside the tick
   when `PRECIS_EMBEDDER_URL` is set (never pulling torch into the agent worker),
   degrading to lexical + citation-graph otherwise. **Deferred (enhancements, not
   blockers):** a compute-lane recall *job* that caches the sweep as a derived,
   content-addressed artifact (idempotent, cache-fillable — the design-of-record
   home for the heavy recall + S2 work, instead of running it synchronously in
   prompt-build), and a first-class kickoff affordance (an MCP verb / web button
   that mints the marked todo).
5. **Upgrades.** c-TF-IDF section labels; real-section paper TOCs; the
   `search`-verb filter promotion; multi-focus recurrence overlay + internal
   consistency findings. **— c-TF-IDF DONE** (`utils/section_keywords.py`):
   the collapsed-run/section label now ranks by `tf(term, run) × idf(term, doc)`
   (sklearn-smoothed `idf = ln((1+N)/(1+df)) + 1`) instead of raw cross-chunk
   frequency, so a run's label is what *distinguishes* it from the rest of the
   document, not the doc's ambient vocabulary — sibling sections stop rolling up
   to the same bag. No new query: the composer already hands `rollup_label` the
   **whole-doc** `block_views`, so document-frequency is a free by-product; the
   smoothing keeps idf strictly positive (a run with terms never labels empty)
   and degrades to frequency order on a tiny doc where every term spans the run
   (the honest v1 preserved on small inputs). Tests: `tests/test_section_keywords.py`
   (`test_ctfidf_suppresses_doc_generic_terms` + the four slice-2 cases, all
   still green). **— multi-focus recurrence overlay DONE**
   (`backfill/candidates.py`): with more than one target, the text lens now runs
   *per section* (each programs its own recall) and the hits merge **by source
   ref** (`merge_recurrence`, pure/unit-tested) — every candidate carries which
   section(s) surfaced it (`Candidate.support`, the field slice-1 reserved) and a
   source recalled across several sections **ranks first**: a cross-cutting gap is
   the stronger omission than a higher-scoring single-section hit. Surfaced in the
   render as `○○ · recurs across <a> <b>` (vs `○ · supports <a>`) in both the
   candidate list and the folded-in `_backfill_marks` (`_support_overlay`). A
   single target stays one sweep attributed to it; the doc-level citation lens
   carries no per-section support (bare `○`). Tests:
   `test_merge_recurrence_ranks_cross_cutting_first`,
   `test_candidate_list_render_shows_recurrence_and_support`. **Still ahead in
   this slice:** real-section paper TOCs (gated on the "verify ingest section
   structure" backlog probe — keyword-cluster fallback holds until then), the
   `search`-verb filter promotion (also a standalone backlog item), and
   **internal-consistency findings** (deferred — flagging contradictory claims
   across sections is a model-authored *finding*, not a deterministic roll-up;
   it belongs with the slice-7 review pass, not the recall layer).
6. **Beyond papers — other source kinds + provenance tiering.** Slices 1–5
   assume `kind='paper'`. But `patent`, `memory`, `datasheet`, `web`, `cfp`
   are searchable/viewable too and bring **angles a paper won't** — a patent
   is prior-art / practitioner framing, a memory is your own synthesis, a
   datasheet is a hard spec. They are *not* interchangeable evidence, so this
   slice broadens the recall lens (`kinds=[…]`) **and** attaches a
   **provenance tier** to every candidate that ranking and the skill respect:
   - `paper`/`cfp` — peer-reviewed / external evidence (strongest; citable as
     support for a scientific claim).
   - `patent`/`datasheet` — external but *not peer-reviewed*: prior-art /
     practitioner / spec angle; cite for "this exists / was built / is
     specified," not for scientific consensus.
   - `memory` (and other **own-authored** kinds) — your *own* thinking, **not
     external evidence at all**: it can surface an angle or a connection, but
     it is never a citation — weaving it in means writing the claim yourself
     and finding a real source, or flagging it as an open thread.

   The tier rides on the candidate (a `● paper / ◐ patent / ○ note` glyph or a
   `[peer-reviewed] / [prior-art] / [own-note]` tag), down-weights lower tiers
   in the gap-rank, and — crucially — the **skill carries a standing
   admonition** on how to treat each: *a memory is a lead, not a source; a
   patent supports existence/priority, not truth*. (Best-practice wording per
   tier is a small research task — venue norms differ.) Belongs after the
   paper flow is proven so the tiering has a solid baseline to contrast
   against.

   **— v1 DONE** (`src/precis/backfill/provenance.py`; `tests/test_provenance.py`).
   A `Tier` ladder (`PEER_REVIEWED` w=1.0 / `PRIOR_ART` w=0.7 / `LEAD` w=0.4),
   `tier_for(kind)` (unknown/own-authored → `LEAD`, the conservative
   "never silently evidence" default), and `tier_tag` → `[peer-reviewed]` /
   `[prior-art]` / `[own-note]`. The recall sweep now scopes across
   `SOURCE_KINDS = (paper, cfp, patent, datasheet)` via `search_blocks_multi(kinds=)`
   (the store already supported the multi-kind arg), each hit's score is
   **down-weighted by `tier_for(kind).weight`** in `_text_lens` so a peer-reviewed
   paper outranks an equally-matched prior-art datasheet, the render carries the
   `[tier]` tag on every candidate (list + `_backfill_marks`), and the planner
   instructions grow a **provenance-tier admonition generated from the tier ladder**
   (`planner_prompt._render_backfill_workspace`, so the prompt can't drift from the
   tags). Tag form (not glyph) chosen so it composes with the slice-5 recurrence
   glyph (`○`/`○○`) instead of overloading `○`. **Deferred (the `LEAD` tier +
   `web`):** `memory` and `web` are tiered but held out of the default sweep —
   `memory` has **no chunk handle** (`format_handle('memory', …, chunk=True)`
   raises) and `web` isn't in the handle registry at all, so a candidate can't be
   opened as a `pc`-style chunk eye. Surfacing own-notes-as-leads needs a
   **ref-level-candidate path** (address the source by its ref handle `me<id>` and
   render it as a flat eye) — a clean, self-contained follow-up; the tier + weight
   + admonition machinery is already in place for it.
7. **The stateful edit→extend→review loop (carried-forward working set).**
   Slice 4 makes *a* tick integrate; this makes the working set **persist and
   grow across ticks** rather than rebuild each time — the natural rhythm:
   *tick 1* open lenses + edit; *tick 2* open more lenses + edit; *tick N*
   **review**, with everything still in context, judging the text the earlier
   ticks just wrote. The substrate already exists — the §15 per-tick
   `job.meta` working-set snapshot (each tick reads the prior snapshot, applies
   curation deltas, writes a fresh one) — so this is *loop wiring*, not new
   storage. What it adds:
   - **Review-in-context is the payoff.** Reviewing new prose *with its
     sources still open* is far stronger than a cold re-read — the reviewer
     checks each claim against the source in the same window, and applies the
     **cold-read test** (does it read for the context-poor human?) as an
     explicit review dimension. This is a *review* pass, distinct from the
     citation *verifier*: it asks "did the weave land — accurate, well-sourced,
     reads standalone?", not "is a quote byte-true?".
   - **Freshly-edited text re-reads live.** Chunks are DELETE+INSERT on edit,
     so the next tick's `reading_order` render already carries the new text —
     no special plumbing to "show what I just wrote."
   - **Convergence, again.** The loop must terminate on a clean review, not
     spin "open more / edit more" forever (the plan-tick-spin detector). Phase
     progression is monotonic: find → edit → extend → review → done.
   - **Cacheability cost (accepted, bounded).** Reto's note: it's "mostly
     non-cacheable" — both the eyes and the prose change each tick, so the
     Anthropic prefix cache (5-min TTL) mostly misses on the working-set block.
     Bound it the way `planner_prompt` already does: the **stable system layer
     (skill + instructions) stays cached**; only the **variable working-set
     block** is the cache-break. So the churn is one block, not the whole
     prompt — the price of a living workspace.
8. **The structural layer — document-graph rollup + heading-intent notes.**
   Slices 1–7 are all *local* (find/write/review a spot); this is *global
   structure* — "does the whole thing hang together." Two separable parts:

   **8a — visibility-scoped link/document-graph rollup.** Per section, roll up
   *all* its links and summarize where they go — `§2 → 3 links to §1.2 · 5 to
   pa1234 · …` — at a granularity that **follows the target's visibility**
   (this is the elegant rule): a link resolves to the *coarsest visible
   ancestor* of its target. If §2 and §3 are both collapsed, `2→3` and `3→2`
   show as a **section-level aggregate** ("N links between §2 and §3"), not
   which paragraph; if the target para is **open**, the link points **right at
   it**. Same for papers: those we **hold and are pointed at** get named, the
   rest **summarized** ("30 links → 8 other papers") with an optional
   long-tail cutoff. Deterministic (fact layer) — it's a roll-up of existing
   `links_for` edges + a visibility-aware target resolver. This is the missing
   "how do the parts interconnect" view the local fisheye can't give.

   **8b — heading-intent notes (the cohesive story).** Maintain a note *on each
   heading*: **what is supposed to be under it, and why it exists** — a
   *teleological* tree: the book exists *because* X; this section supports
   *that* part of the book; this chapter supports *xyz* of the section. These
   are `memory` refs with a `heading-intent:hard|soft` tag (hard = a structural
   commitment; soft = a revisable intent), **linked to the heading chunk**.
   Shown with the heading as the document renders (open heading → the note;
   collapsed → its presence). The **planner seeds** them when it scaffolds
   structure; a **skill admonition keeps them current** (update the intent when
   the section's job changes — anti-drift). They are the **structural memory**
   that stops a many-tick / many-agent edit process from losing the plot: an
   agent editing §5 that can *see* "§5 exists to support §2's argument" won't
   drift the way one editing blind does. The counterpart, at document scale, to
   the working set's local context and the reader-asymmetry rule's discipline.

## Backlog items (file separately)

- **`paper_reconcile` prep-time freshness.** Source-backfill's Tier-0
  cited-vs-candidate dedup is only as trustworthy as paper identity. Ensure
  `paper_reconcile` is running / freshness-gated before an audit trusts it —
  a "missed source" that is actually the cited paper under a different ref /
  stub / DOI-case is a false gap that erodes trust.
- **Promote SQL-only filters into the `search` verb** — `keywords=` /
  `numerics=` / `role=` (and an `exclude_cited=` / `exclude_dismissed=`
  convenience). The recall *worker* can drop to direct SQL for slice 1, but
  the LLM-assisted expand tier can't reach these from the verb.
- **Verify ingest section structure.** Does the Marker pipeline retain
  real section headings for any sources? Decides the paper real-sections
  path vs. keyword-cluster fallback.
- **`author-year` short-cite helper** — byline first-author + year (fallback
  slug/title). Needed for the grounding line; none exists today.
- **Citation prose style — integral vs non-integral (drives *all* text
  generation, not just backfill).** Whether a woven citation names the author
  in the sentence grammar — *integral/narrative* ("Kumar et al. report a 68%
  contribution [12]") — or stays out of it — *non-integral/parenthetical*
  ("…a 68% contribution [12]"). This is **orthogonal** to the export citation
  *format* (numeric `[12]` vs author-year `(Kumar 2021)`, a CSL/export
  concern): all four combinations exist. It is a **document-level** parameter
  (consistency: don't weave "Kumar et al." into a doc that otherwise uses bare
  `[n]`), so source-backfill's weave **consumes it, never improvises it**.
  **Home: the "about the doc" block — the `Workspace` (`meta.workspace`,
  `utils/workspace.py`), next to the existing `style` field** (which is the
  *format* axis — `ieee-numeric` etc., currently informational). Add a
  `cite_prose` field (`auto | integral | non-integral`). The cascade +
  prompt-injection plumbing already exists — a `Workspace` field flows to every
  descendant at `put` time and is surfaced into the authoring prompt exactly
  like `brief` (`planner_prompt._render_project_brief`), so this is "add a
  field + surface it," not new machinery. Default `auto` = **detect from the
  draft's existing prose** (does it currently say "X et al." or bare `[n]`?);
  `integral`/`non-integral` are explicit overrides. (While here: `style` is
  marked *informational* — this axis should become *effective*, driving both
  export format and prose, not just documentation.) Note the *when* is itself rhetorical (integral for
  attribution/contrast — "Unlike Wang, Kumar finds…" — non-integral for
  background support), and integral phrasing **aids the cold-read** (the
  reader learns *whose* finding it is without resolving `[12]`) — a reason it
  leans toward the reader-asymmetry rule, though some venues forbid narrative
  citations as informal, so it stays a policy, not a universal.

## Open questions

- **Whole-draft tiling.** A whole-draft audit won't fit context → a
  coordinator campaign that tiles section-by-section (like `good_search`).
  Slice-4+.
- **Cross-*draft* cross-refs.** Open a linked chunk in *another* draft with
  its surrounds? Default off (large, off-topic); opt-in per-ref.
- **Human-vs-LLM audience for the "aura."** The web reader could *visualize*
  the LLM's live eye-set as a lit-up overlay (an observability surface) —
  distinct from extending the LLM's context assembly. Decide if in scope.
