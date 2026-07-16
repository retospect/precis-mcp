# Patent authoring — the freedom-to-operate writing loop

> Status: **proposal** (2026-07-16). This is the **dynamic authoring
> loop** that runs *on top of* the static patent genre described in
> [`patent-drafting-merge.md`](patent-drafting-merge.md). That doc says
> **what a patent draft *is*** — a `draft` with `doc_type=patent`, whose
> claims / parts / prior-art are managed chunks in styled sections
> ([ADR 0037](../decisions/0037-heading-styles-and-numbering-lock.md),
> [ADR 0033](../decisions/0033-draft-chunks-editable-document.md)). This
> doc says **how the agent *writes* one** — the iterative prior-art
> sweep, the freedom-to-operate claims view, and the scoping-decision
> ledger. Read `patent-drafting-merge.md` first; this does not restate
> the structural model.
>
> It rests almost entirely on machinery that already exists — the
> read-only `kind='patent'` EPO-OPS fetch, the ADR 0051 §6 "eye"
> (fisheye / working-set) render, the `plan_tick` planner coroutine, and
> the `plan` kind. The **only new storage** is marking ingested claim
> chunks (slice 1); the rest is orchestration and one first-use of an
> existing-but-dark kind.

## TL;DR — the loop

A patent is written against the prior art, not in a vacuum. The loop:

1. **Draft the description**, then **sweep for prior art**
   (`search(kind='patent', source='remote', …)`), **pull** the material
   hits (`get(kind='patent', id=…)` — synchronous), and **revise** so the
   prose adopts the field's patent register. Repeat 2–3 ticks until it
   stops finding new material and the language has "synced."
2. **Write the claims against a freedom-to-operate view**: the
   claim-writing agent sees **all the prior-art claims** it must design
   around — *if it is already claimed over there, we cannot claim it* —
   plus our own claim set in full.
3. **Record the scoping decisions**: when the agent declines or narrows a
   claim because a specific prior-art claim blocks it, it writes a typed
   note pointing "over there" — the exact `[pk…]` claim that precluded
   the scope. That ledger is carried into later ticks so ruled-out scope
   is not re-proposed, and it is the raw material for a later FTO /
   examiner rationale.

**pull prior art → see all their claims → write ours against them →
record why we scoped as we did → carry it forward.**

## Why a separate doc from `patent-drafting-merge.md`

`patent-drafting-merge.md` is the **noun** — the genre, the styles, the
managed leaves, the numbering, the export target. It is largely a
*static* description of a finished patent's structure. This is the
**verb** — the agent behavior that produces it, which is driven by an
external corpus (the prior art) and a legal constraint (novelty /
freedom to operate) that the static model does not capture. Keeping them
separate keeps each legible; they share the `doc_type=patent` flag as
the single hinge.

## Load-bearing facts (verified 2026-07-16)

* **Patent ingest is synchronous.** `get(kind='patent', id='us2943737a')`
  fetches from EPO OPS and commits the ref + description/claim blocks
  *inline* (`handlers/patent.py` — "ingest commits before returning").
  There is **no** stub-and-return. So "pull prior art mid-write" is just
  a `get` inside a tick; the patent's text is available the same tick.
  The only lag is that embeddings + KeyBERT keywords are filled by the
  async workers (ADR 0007), so a freshly-pulled patent is not
  *semantically searchable / cluster-eye-able* for ~a minute — raw claim
  text is there instantly. → No paper-style async stub+wait is required;
  patents are **pull-and-have**, not request-and-chase. (A light
  `patent_ingested` wait-gate is a *nice-to-have* only if a later sweep's
  semantic search must include what was just pulled.)
* **The "eye" has one live delivery path.** The fisheye / working-set
  stack (`utils/fisheye.py`, `utils/refeye.py`, `utils/eye_render.py`,
  `utils/working_set_render.py`; ADR 0051 §6) ships **dark** — *except*
  the planner's read of `refs.meta.working_set`, which renders curated
  eyes and injects them into every tick as *"## Working set — the
  author's eyes in context"* (`workers/planner_prompt.py`, "the first
  live ADR-0051 integration," flag-gated). Patents are already a
  supported doc-kind in `eye_render.py`. → The claims view ships on rails
  that exist: curate a working set over prior-art claim chunks, stash in
  `meta.working_set`, flip the flag.
* **Claims are not yet individually retrievable.** Ingest appends claim
  blocks after description blocks by position but does **not** mark them;
  `view='claims'` currently dumps all blocks (a documented Phase-1 TODO
  in `handlers/patent.py`). This is the one real gap (slice 1).

## Phase 1 — the description "sync" loop

Not a new coroutine: a `doc_type=patent` **branch of the existing
`plan_tick`** (which already drives draft-editing ticks). Each tick:

1. Write / revise description chunks (the `patent-description` styled
   sections).
2. Derive prior-art queries from the current description and
   **sweep**: `search(kind='patent', source='remote', q=…)` — the
   "prior-art sweep" mode returns OPS hits *minus* what we already hold.
3. **Pull** the material hits with `get(kind='patent', id=…)`
   (synchronous). Bounded by a **per-tick pull budget** — every `get`
   persists a real patent into the shared corpus, so an unbounded sweep
   is both cost and corpus-noise; the tick surfaces what it ingested in
   its `job_summary`.
4. Revise the prose to align terminology with the pulled patents (the
   "sync with patent lingo" payoff — once real patents are ingested they
   feed back as context and the writing starts to *sound* like the
   field's patents).
5. Yield; the next tick continues, its user prompt carrying the prior
   tick's summary.

Terminates when a sweep returns no new material and the language has
stabilized (agent judgment, bounded by the planner's per-todo tick/cost
caps).

## Phase 2 — claims against a freedom-to-operate view

### The view is a *comprehensive* claims digest, not a decaying fisheye

The generic doc-eye *compresses* (verbatim center, neighbors fading to
gloss then keywords). That is **wrong for a novelty check**: a prior-art
independent claim cannot be silently dropped for being "far" from the
focus — that dropped claim might be the one that blocks us. So the claims
view renders near-complete:

* **Others' claims** — every **independent** claim of each related
  patent, **verbatim** (these define the existing legal scope; never
  drop one). Dependent claims verbatim if the budget allows, summarized
  if not. Grouped by patent, independents first.
* **Our claims** — **full text, always** (the set is small; it is mostly
  already in-context as the draft chunks being edited, made explicit and
  unabridged here).

Delivered *through* the live working-set injection — eyes pinned at
`verbatim` extent — but with a **claims-aware selector** rather than the
keyword-cluster doc-eye. The selection persists in `meta.working_set`
across ticks (this is the "retain the claims" the loop needs: a stable
reference set of what has already been claimed).

### This is what makes claim-marking load-bearing

To honor "never drop an independent claim, compress the dependents," the
render must distinguish **independent from dependent** claims — which is
also what lets a scoping-decision point precisely at "claim 7 of US…".
Both fall out of slice 1.

## The scoping-decision ledger (the negative space of the claims)

When the claim-writing agent declines or narrows a scope because a
specific prior-art claim blocks it, it records **why**, pointing "over
there" at the exact `[pk…~n]` claim. This ledger:

* **stops re-litigation** — later ticks check "is this proposed scope
  already ruled out?" before re-proposing it (the retention payoff);
* is **never exported** — internal FTO reasoning, not part of the filing;
* is **raw material** for a later examiner / freedom-to-operate rationale
  and makes the scoping legible to the inventor.

**Home: the `plan` kind** (ADR 0051 §2b) — *"a thread's reasoning
outline… notes on the draft substrate… never exported
(`corpus_role='none'`); ships dark, nothing dispatches to it yet."* A
claim-scoping ledger is its **first real use**: one `plan` per patent
project, holding lightly-structured decision entries:

```
considered:  <the scope we weighed>
verdict:     dropped | narrowed
because:     [pk…~n]        # the precluding prior-art claim
instead:     <the narrowing we took>   # optional
```

(If a flat append-only log fits better than plan's todo-list shape, the
`quest_log` WORM pattern — dated, typed entries — is the alternative;
`plan` is the purpose-built home.)

The ledger and the claims digest are **two halves of one FTO context**:
the digest is injected → the agent reasons about novelty → a declined
scope becomes a decision note linking the blocking claim → the ledger is
injected back alongside the digest on the next tick. See → decide →
record why → retain.

## Export reconciliation (a real tension with `patent-drafting-merge.md`)

`patent-drafting-merge.md` §4 renders references as *"[n] + reference
list"* and treats the IDS as a rendered view. For the **specification
body of a filing**, the USPTO convention is different: prior-art patents
are cited **in-text by number** ("U.S. Patent No. 2,943,737"), with **no
bibliography** in the spec. These are not in conflict — they are two
surfaces:

* **Specification body** (this loop's output) → in-text patent numbers,
  no reference list. The exporter must branch on `doc_type=patent`:
  render a patent reference as its citation string inline, emit no
  `\cite`, suppress `\printbibliography`. (Today `export/latex.py` has a
  single shape — always `\printbibliography`, patents swept into a
  `@patent` bib entry — which is the review/paper convention, wrong for a
  filing.)
* **IDS** (a separate disclosure filing) → the reference-list view over
  the `patent-prior-art` section, per `patent-drafting-merge.md` §2.4/§4.

So the `patent-prior-art` / IDS machinery is retained for the IDS; the
**spec body** gets the in-text-no-bibliography genre switch.

### WYSIWYG + reviewer-visibility for the in-text form

The visible citation text in the reader must equal what the exporter
emits (so it can be proofread), and reviewer skills must see it (to check
patent-office compliance). Therefore the compliant string **lives in the
draft body as visible content**, authored as a **display-link** whose
text is the citation string and whose target is the patent:

```
[U.S. Patent No. 2,943,737](US2943737)
[U.S. Patent Application Publication No. 2015/0101966 A1](US20150101966A1)
```

Display-links already keep their text in the reader (they do **not**
collapse to a `Ⓟ` sigil) and already render in export — so one source
satisfies reader, export, and reviewer with no divergence. A small
`format_patent_citation(ref)` helper generates the compliant string
during authoring (granted → "U.S. Patent No. N"; published app → "U.S.
Patent Application Publication No. YYYY/NNNNNNN A1"), but it is **off the
render path** — the string is content, so nothing silently reformats what
was proofread.

### Two wiring bugs to fix along the way

* **Section skills cite patents with the wrong handle.**
  `patent-prior-art.md` and `patent-claim.md` say reference corpus
  material as `[pc…]` — but `pc` is a **paper** chunk; a patent chunk is
  `[pk…]`. A patent reference via `[pc…]` routes to the paper resolver
  and will not link.
* **The public-number autolinker is mis-wired for real patent storage.**
  `utils/mentions.py::resolve_patent_pubnum` matches `id_kind='pub_id'`,
  but ingested patents carry only `id_kind='cite_key'` (the lowercased
  DOCDB number, e.g. `us2943737a`); **zero** patents have a `pub_id` row
  (all 6,304 `pub_id`s belong to `paper`/`finding`, verified on
  `precis_prod` 2026-07-16). So a bracketed patent number never resolves
  to an ingested patent. Fix: match a patent by its `cite_key` / DOCDB
  slug.

## Slicing (MVP first; each phase leaves the gate green)

1. **Ingest: mark claim chunks + independent/dependent (+ antecedent).**
   The one storage change; forward-only. Unblocks precise "point over
   there," the independents-first digest, and `view='claims'`. **— done:**
   `handlers/_patent_claims.py` (`classify_claim` heuristic, `chunks.meta`
   marker `patent_block=description|claim` + `claim_number` /
   `claim_independent` / `depends_on`); `_patent_ingest.py` stamps each
   block; `view='claims'`/`'description'` filter and the claims view
   labels each claim's structure (legacy unmarked patents fall back to the
   full dump). No migration (`meta` is existing JSONB). Backfill of the
   ~101 already-ingested patents is a follow-up (re-parse from the raw XML
   on disk).
2. **`doc_type=patent` correctness fixes. — done:** `doc_type` is now a
   first-class `Workspace` field (`utils/workspace.py`); the section skills
   cite patents as `[pk…]` not `[pc…]`; and `resolve_patent_pubnum`
   (`utils/mentions.py`) matches a patent by its `cite_key`/DOCDB slug
   (regex-tolerant of a missing kind code) instead of the empty `pub_id`.
3. **Claims-digest working-set. — done:** `workers/patent_digest.py` —
   `build_claims_digest`/`stamp_claims_digest` (others' independents
   verbatim, dependents summary, our claims verbatim, deduped → reader-shape
   `meta.working_set`) + `related_patent_ref_ids`/`refresh_claims_digest`
   (discover the draft's linked prior-art patents + stamp in one call). The
   planner's live fisheye injection renders it. *Remaining: auto-call
   `refresh_claims_digest` from the tick path (below).*
4. **Scoping-decision ledger. — done:** the patent module instructs the
   agent to keep a project `plan` — log each declined/narrowed scope
   pointing at the blocking `[pk…~n]` claim — **and** the `has_plan`
   predicate + `plan` variable module (`_render_plan_ledger`) now
   auto-inject the project's plan outline into every tick, so recorded
   decisions are surfaced (retention) without the agent having to fetch
   them. General to any project with a plan (first live `plan`-kind
   consumer).
5. **`doc_type=patent` planner branch. — done:** the `is_patent` predicate
   (`utils/prompt/predicates.py`) + the `patent` variable module
   (`planner_prompt.py::_render_patent_authoring`) lead a patent tick with
   the sweep→ingest→sync→claim→log loop (the agent runs
   `search(source='remote')` + `get`; no new job_type).
6. **Exporter genre switch. — done (LaTeX):** `doc_type=patent` renders
   prior art in-text (display-link surface, else `format_patent_citation` /
   `paper_inline_citation`) and suppresses `build_bib` +
   `\printbibliography` (`export/latex.py`, `export/_patent_cite.py`; worker
   threads doc_type from the project workspace). **docx mirror done**
   (`export/docx.py`, same branch points, doc_type auto-detected). Per-
   authority citation strings (US / PCT-WO / EP / GB / DE / JP / CN / …).
   The IDS view stays separate.
7. **Claim-family grouping — done:** the digest emits each prior-art
   patent's claims **in document order** (independent claim + its dependents
   grouped together), independents verbatim / dependents compressed. *Still
   deferred:* a full visual **tree** render (nested indentation of
   dependents under their independent — needs a custom render surface beyond
   the working-set's per-chunk verbatim) and the **interactive web claims
   view** (same working set feeds both).

**Connective wiring:** (a) **done** — the `plan_tick` executor auto-invokes
`refresh_claims_digest` for a patent tick with a bound draft before prompt
assembly (`_refresh_patent_claims_digest`, best-effort); (b) **done** — the
docx export mirror (`export/docx.py`: patent-mode in-text cites +
References suppression, doc_type auto-detected from the draft's cascaded
workspace). (c) **done** — the `has_plan` plan-outline injection module.
(d) slice 7 claim-family grouping **done**; the visual tree render + web
claims view remain. *Deferred:* backfilling the ~101 already-ingested
patents with claim markers — needs raw-XML-on-disk (or OPS re-fetch) on the
cluster, and new prior-art sweeps self-mark, so it is completeness-only.

## Decisions locked in discussion (2026-07-16)

* **Loop lives on `plan_tick`**, keyed by `doc_type=patent` — not a new
  coroutine (reuses guardrails, cost caps, child/yield, the live
  working-set injection).
* **Prior-art pull is agent-driven with a per-tick budget** (cost +
  corpus-noise control); the tick reports what it ingested.
* **Claims view is comprehensive, not a decaying fisheye** — independent
  claims never dropped; dependents compressed under budget.
* **Prompt-injection first, web eye view later** — the same working set
  feeds both.
* **The ledger is retained reasoning that never exports** (`plan`), and
  the others'-claims set is retained in `meta.working_set` — the two
  senses of "retain the claims."
* **Spec body = in-text patent numbers, no bibliography**; IDS is a
  separate view (reconciled with `patent-drafting-merge.md` §4).

## Open questions

1. **`patent_ingested` wait-gate** — build it (to let a later sweep's
   semantic search include just-pulled patents), or accept the ~1-minute
   embed lag and rely on raw claim text in-tick? (Leaning: skip until the
   lag actually bites.)
2. **Dependent-claim compression** — when the prior-art set's dependent
   claims exceed budget, summarize how (per-patent gist? drop to
   independents only with a count marker)? Must remain legally safe
   (never imply completeness it doesn't have).
3. **US vs EP claim/citation conventions** — inherited from
   `patent-drafting-merge.md` §8.4. The **citation string** is now
   per-authority (`export/_patent_cite.py::format_patent_citation` handles
   US / PCT-WO / EP / GB / DE / JP / CN / … with an authority-name map).
   Still open: whether the **claim register** (US vs EP drafting style)
   warrants one `doc_type` with a jurisdiction sub-flag, or two.
4. **Ledger → issues** — should a "blocked scope" decision optionally
   open an ADR-0037 §3b *issue* to the inventor ("we scoped around
   US…claim 7 — accept the narrowing?") rather than only logging it?
