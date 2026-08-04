---
status: built
title: Trust surfaces for unverified claims — export marking, unacquirable override, editor badges
model: opus
blocked-by: finding-acquisition-mode (RESOLVED — substrate shipped f2b2bf20, 2026-08-04)
---

# Trust surfaces for unverified claims — export marking, unacquirable override, editor badges

> **Built 2026-08-04** — both stages shipped as specced: stage (a)
> `src/precis/taproot/trust.py::claim_trust` (single shared derivation,
> reuses `_chase_llm.is_corroborating`), docx/latex export marking +
> end-matter list + `export_override` ref_events record
> (`src/precis/export/_trust_marks.py`), and the
> `edit(kind='finding', unacquirable_note=)` override door; stage (b)
> smartdraft badge overlays (`claim_trust` field in
> `review_payloads_for`, `sd-trust-*` `::after` marks per the
> `sd-integrity` precedent) + `render_claim_evidence.status`
> populated. Implementation notes: `by` on the override is always
> `"agent"` (no caller-identity channel exists yet); a non-dict/legacy
> chain-verification blob degrades to the clean bucket and export-side
> trust resolution is best-effort (a store hiccup renders the citation
> unmarked rather than aborting the export). The substrate defect this
> works around (chase flips to established despite a contradicts
> verdict) is gripe gr191353.

## Motivation / why

`finding-acquisition-mode` gives a claim a life *before* its evidence
is verified (`STATUS:acquiring`, and the pre-existing `tracing`). We
don't trust a claim until it is actually resolved — and that distrust
must propagate to every surface a human sees, or the acquiring state
quietly launders unverified claims into finished prose. Split out of
`finding-acquisition-mode.md` per its 2026-08-04 readiness review
(disjoint file set, independently testable, one-way dependency:
substrate first).

Human-facing vocabulary (decided, Reto 2026-08-04) collapses the
machine states to two labels. The mapping is **total** — distrust by
default; only a confirmed finding renders clean (readiness-review
blockers 1+2 resolved 2026-08-04):

- **clean (no mark)** — `STATUS:established` whose terminal chain
  verification (`meta.chain[-1]["verification"]`, when present)
  satisfies `workers/_chase_llm.py::is_corroborating` — the codebase's
  single source of truth for the attach decision (`supports=="yes"`,
  or `supports=="partial"` without `contradicts`). Absent verification
  (no LLM ran) still counts clean: the chain traced to ground, which
  is today's bar.
- **unsupported — verification failed** — `STATUS:established` whose
  terminal chain verification is present and
  `not is_corroborating(verification)` (i.e. `supports=="no"`, or
  `supports=="partial"` with `contradicts=True`): the paper arrived
  and does not say what the claim says. Renders louder than pending.
  The predicate is REUSED from `_chase_llm.py`, never re-derived —
  the attach decision and the trust label cannot fork. Derived at read time from the chain meta — deliberately NO
  new machine STATUS: trust surfaces are read/render-time only, and
  changing chase's flip-to-established behavior is a substrate change
  out of scope here. (That chase flips to `established` even on a
  contradicts verdict is a substrate defect filed separately — see
  decisions log.)
- **unverified — source pending** — *everything else*: `acquiring`,
  `tracing`, `multi_candidate`, `cycle`, and every `dead_chain`
  reason. The author asserts it; the system hasn't confirmed it. The
  mark's note derives from the machine state
  (`dead_chain(unacquirable)` → "no OA copy obtainable;
  hand-download queued"; `multi_candidate` → "ambiguous citation
  awaiting pick"; other `dead_chain` reasons → the reason slug). None
  of these mean contradicted, so none bucket as unsupported.

**Taproot claim hubs** (blocker 3 resolved): a `TAPROOT:claim` hub is
permanently `STATUS:canonical` and never carries the lifecycle above —
its trust derives from its evidence set (`finding_cite_keys`): an
empty print set (no originators AND no corroborators) → **unverified**
("claim hub has no print-visible supporter yet"); any print-visible
supporter → clean. Hub "unsupported" is deferred — contradictors
alongside support is normal science and already surfaced on the claim
page. `render_claim_evidence`'s dormant `"status": None` placeholder
is populated with this derived label.

("speculative" rejected as the default label — it mis-attributes the
doubt to the author; reserved for a possible future author-chosen
conjecture marker.)

## In scope

### 1. Export marking (docx / latex)

**Export always works, always marks** (decided — no refusing/strict
mode; marking *is* the mechanism):

- An unverified-backed citation renders with a visible inline mark
  (e.g. `[unverified: Smith 2021 — source pending]`) plus an
  end-matter "Unverified claims" list (claim, state, what it's
  waiting on).
- An unsupported-backed claim renders louder:
  `[UNSUPPORTED — cited source does not back this claim]`.

### 2. Author override for unacquirable sources

A print-only / undigitized source is legitimately citeable even when
no digital copy is obtainable. An explicit per-claim override
suppresses the unverified mark:

- Stored durably on the finding
  (`meta.unacquirable_override = {by, at, note}`).
- **Recorded in the export record**: at export time, a `ref_events`
  row is appended to the draft ref (existing `append_event` machinery
  — no schema change) listing the overridden claims, so the trust
  decision is visible in the audit trail, never silent.

### 3. Editor badges (smartdraft)

The live surface is the **already-built** per-paragraph review-status
indicator (`docs/proposals/smartdraft-review-status-ui.md`, shipped:
four-state grey/hollow-blue/green/amber + tooltip matrix,
`src/precis_web/routes/smartdraft.py` +
`templates/smartdraft/view.html.j2`). **Decided (2026-08-04): overlay
mark on the same widget, following the shipped `sd-integrity`
precedent — NOT a fifth/sixth dot state.** The four dot states track
the prose review lifecycle at a sha; claim trust is a property of the
block's *citations* — orthogonal axes (a paragraph can be
human-approved green AND lean on an unverified claim; a fold-in can't
represent that cross product). The shipped widget already renders
exactly this category of orthogonal fact: `integrity_ok is false` →
a red `!` `::after` overlay (`.sd-integrity`) + tooltip suffix,
dot color untouched.

Concretely:

- `precis_web/smartdraft.py::review_payloads_for` computes a
  `claim_trust` field per block (`None` / `"unverified"` /
  `"unsupported"`, worst-of across the block's cite heads), alongside
  the existing `integrity_ok` scan and sharing its one-cache-per-render
  pattern. Head → finding ref_id resolution reuses
  `precis_web/claim_render.py::cite_heads_in` +
  `_resolve_head_ref_id` (already the canonical cite-head grammar).
- Template (`_block.html.j2::sd_review_widget`): unverified → a muted
  `?` overlay; unsupported → a loud red mark in the `sd-integrity`
  weight class; tooltip gains a line naming the offending head(s) and
  state. Dot color / state machine untouched — the shipped four-state
  tests must pass unmodified (AC 5).

## Explicitly NOT in scope

- The substrate itself (`STATUS:acquiring`, mint mode, chase bridge,
  give-up) — that is `finding-acquisition-mode`.
- Any refusal/strict export mode (explicitly rejected).
- W2/W3 workflow templates (repair / review drafts) — separate
  follow-on; this proposal only renders states, it doesn't drive
  verification.
- Blocking or altering draft *editing* — trust surfaces are
  read/render-time only.

## Acceptance criteria

1. Exporting a draft citing an `acquiring`- or `tracing`-backed claim
   yields the inline unverified mark and the end-matter list, in both
   docx and latex paths.
2. An unsupported-backed claim renders the louder mark, visually
   distinct from pending.
3. A finding with `meta.unacquirable_override` that would otherwise
   render **unverified** renders as a clean citation instead, and the
   export appends a `ref_events` row on the draft naming the
   overridden claim(s), author, and timestamp. The override does NOT
   suppress an **unsupported** mark — a negative terminal verification
   still renders loudly (decided precedence: the paper was read; an
   override doesn't unread it).
4. A `dead_chain(unacquirable)` claim without override renders as
   unverified-pending-with-note, not unsupported.
5. The smartdraft paragraph indicator reflects
   unverified/unsupported state per the decided integration shape,
   without regressing the shipped four-state review behavior (its
   existing tests pass unmodified).
6. Clean-bucket citations (`STATUS:established`, no negative terminal
   verification) render with identical content to today — same
   extracted paragraph/run text for docx, same emitted source for
   latex, the bar the existing export suites already use — and no
   event row is appended.

## Target + blast radius

- **`src/precis/taproot/trust.py` (NEW)** — the single shared
  derivation: `claim_trust(store, finding_ref_id) -> TrustState`
  (label ∈ clean/unverified/unsupported, note, overridden flag),
  branching hub vs lifecycle finding exactly as `finding_cite_keys`
  does. Both export and web import THIS — the mapping lives in one
  place so the surfaces cannot drift.
- `src/precis/export/docx.py`, `src/precis/export/latex.py` — citation
  render + end-matter list + override handling + export-record event.
- `src/precis/handlers/finding.py` — `meta.unacquirable_override`
  write path: the `unacquirable_note=` kwarg on the existing `edit`
  verb (decided — see decisions log).
- `src/precis_web/smartdraft.py` — `review_payloads_for` gains the
  `claim_trust` field (worst-of across the block's cite heads).
- `src/precis_web/claim_render.py` — populate the dormant
  `"status": None` with the hub-derived label.
- `src/precis_web/routes/smartdraft.py`,
  `templates/smartdraft/_block.html.j2`,
  `templates/smartdraft/view.html.j2` — badge overlay + CSS.
- Tests across all of the above.

Implementation is two bounded stages sharing stage 0 (the trust
module): **(a)** trust module + export marking + override write-path;
**(b)** editor badges. Independently testable; single proposal because
the label vocabulary + derivation function is the genuine coupling.

## Open questions / decisions log

- **Decided (Reto, 2026-08-04):** always-mark, no refusal mode;
  override recorded, never silent; vocabulary as in Motivation.
- **Decided (2026-08-04):** export record = `ref_events` row on the
  draft ref, written at export time via existing `append_event`.
- **RESOLVED (2026-08-04):** badge integration shape — overlay mark on
  the shipped widget per the `sd-integrity` precedent, not a fold-in.
  Rationale + concrete shape in §3 above (orthogonal axes: prose
  review lifecycle vs citation trust).
- **RESOLVED (2026-08-04):** override door — extend
  `edit(kind='finding', id=N, unacquirable_note='<why>')` (the handler
  already exposes an interactive `edit` verb for `pick_candidate`, and
  its signature accepts extension kwargs). Sets
  `meta.unacquirable_override = {by, at, note}`; `note` required
  (a silent override defeats the audit purpose), `by` from the caller
  identity the handler context carries (fallback `'agent'`), `at`
  server-stamped. NOT a tag axis — the payload is structured
  ({by, at, note}), which a tag can't carry.

### Readiness review (ADR 0048, 2026-08-04)

- **blocker** — "unsupported" has no backing machine state. `chase.py`
  (`advance_finding`/`_snapshot_chain`) flips a finding to
  `STATUS:established` on ANY terminal hop regardless of
  `verification.get("supports")`/`contradicts` — there is no
  `STATUS:contradicted` or `dead_chain(reason=contradicted)` in
  `_CLOSED_VOCAB`. The only place a "contradicted / none-fit" verdict
  lives is the per-hop `meta.chain[-1]["verification"]` dict, populated
  only when `PRECIS_CHASE_LLM` ran that hop. This directly collides with
  AC 6 ("`STATUS:established`-backed citations render byte-identically
  to today, no mark") — per this proposal's own definition, an
  "unsupported" claim (contradicted after grounding) IS an established
  finding, so AC 2 and AC 6 can't both hold without specifying the
  actual field/condition that distinguishes them, which neither
  Motivation, In-scope, nor Target + blast radius names.
- **blocker** — mapping coverage hole. The Motivation's two-label
  scheme only addresses `acquiring`/`tracing` → unverified,
  contradicted/none-fit → unsupported, and `dead_chain(unacquirable)` →
  unverified-with-note. Left unmapped: `STATUS:multi_candidate`,
  `STATUS:cycle`, and 7 of the 8 `dead_chain` reasons chase.py actually
  writes (`empty_chain`, `target_deleted`, `abandoned_waiting`,
  `no_target_chunk`, `no_resolvable_cite`, `no_external_id`,
  `no_stubs`) — none of which mean "the paper arrived and does not say
  what the claim says" (the Motivation's own definition of
  "unsupported"), so they can't default into that bucket either. An
  implementer has no stated behavior for these reachable states.
- **blocker** — taproot claim-hub citations are unaddressed. Citation
  resolution (`precis/taproot/cite.py::finding_cite_keys`, the resolver
  this proposal's own §3 cites for cite-head→finding resolution)
  branches first on `is_claim_hub` — a `TAPROOT:claim` hub never
  carries `acquiring`/`tracing`/`established`/`dead_chain` STATUS at
  all (permanently `STATUS:canonical`); its trust state is a distinct
  originators/corroborators/contradictors/inflight evidence model
  (`precis_web/claim_render.py::render_claim_evidence`, whose
  `"status": None` field is a pre-existing, never-populated placeholder
  for exactly this). Motivation, In-scope, and Target + blast radius
  never mention hub findings, `is_claim_hub`, or `claim_render.py` —
  yet any export/badge trust-marking pass reaches hub cites through the
  same resolver path this proposal already depends on.
- **blocker** — Target + blast radius names
  `src/precis_web/routes/smartdraft.py` for the §3 badge work, but the
  function §3 says to extend (`review_payloads_for`, computing the new
  `claim_trust` field) lives in `src/precis_web/smartdraft.py` — a
  different file, missing from Target + blast radius entirely.
- **blocker** — AC 6's "render byte-identically to today" is not
  verifiable as literally worded for the docx path. A `.docx` is a zip
  container; python-docx's generated core-properties/zip metadata
  aren't guaranteed byte-stable across independent export runs even
  with zero code changes, and the existing `tests/test_export_docx.py`
  suite already verifies via extracted paragraph/run text, never raw
  bytes. Either the AC means "identical rendered content" (the
  testable bar the existing suite actually uses) or it can't be gated
  on as written.
- **advisory** — unclear whether `edit(kind='finding',
  unacquirable_note=...)` is gated to a finding currently
  `STATUS:dead_chain(reason=unacquirable)`, or settable pre-emptively
  on any finding state (e.g. before the chase ever attempts
  acquisition) — not specified.
- **advisory** — §3's `claim_trust` computation doesn't say whether a
  finding carrying `meta.unacquirable_override` should suppress the
  badge's unverified mark the way §1 says it suppresses the export
  mark — cross-surface consistency between §1 and §3 on the override
  is unstated.
- **advisory (split signal)** — the proposal bundles two
  separately-testable/shippable deliverables: export marking +
  override write-path (`export/docx.py`, `export/latex.py`,
  `handlers/finding.py`) and editor badges
  (`precis_web/smartdraft.py`, `routes/smartdraft.py`, the template).
  No genuine blocking dependency between them beyond the
  already-decided shared label vocabulary — worth splitting into two
  proposals.

### Resolutions (2026-08-04, Opus)

- **Blockers 1+2 (no machine state / coverage hole) → RESOLVED** by
  the total read-time mapping now in Motivation: clean =
  established + non-negative terminal `meta.chain` verification;
  unsupported = established + negative verdict (derived at read time,
  NO new STATUS — substrate untouched); unverified = every other
  reachable state, note derived from status/reason. AC 2/6 no longer
  collide: the discriminator is the terminal verification verdict,
  named explicitly. The underlying substrate defect (chase flips to
  `established` even when the verifier said contradicts) is filed
  separately as a gripe — fixing it is a chase-behavior change, not a
  trust-surface render.
- **Blocker 3 (hubs) → RESOLVED**: hub arm added to Motivation —
  empty print set → unverified, else clean; hub-unsupported
  deferred; `render_claim_evidence.status` populated. The shared
  `trust.py` branches on `is_claim_hub` exactly as `finding_cite_keys`
  does, so no caller can hit the wrong arm.
- **Blocker 4 (file mismatch) → RESOLVED**: Target + blast radius
  rewritten — `precis_web/smartdraft.py`, `claim_render.py`, the
  `_block.html.j2` macro, and the new shared
  `src/precis/taproot/trust.py` all named.
- **Blocker 5 (byte-identical unverifiable) → RESOLVED**: AC 6
  reworded to identical rendered *content* (extracted text bar the
  existing suites use).
- **Advisory (override gating) → DECIDED**: `unacquirable_note=` is
  settable pre-emptively on any lifecycle state (the author may know
  a source is print-only before the chase burns its grace window). It
  suppresses only the **unverified** mark; an **unsupported** verdict
  always renders — a negative verification outranks the author's
  override (the paper was read; the author saying "trust me" doesn't
  unread it).
- **Advisory (cross-surface consistency) → DECIDED**: the override
  suppresses the badge mark exactly as it suppresses the export mark —
  guaranteed structurally, since both surfaces read the one
  `trust.py::claim_trust` result (which carries the overridden flag).
- **Advisory (split) → DECIDED**: single proposal, two implementation
  stages (see Target + blast radius) — the shared derivation module is
  the genuine coupling; splitting would force one side to ship a
  vocabulary the other can drift from.

### Readiness re-review (ADR 0048, 2026-08-04, follow-up)

- **blocker — Blocker 1 only PARTIALLY resolved.** The location claim
  is now correct (`meta.chain[-1]["verification"]` is confirmed the
  terminal-hop verdict, written in `chase.py::advance_finding` right
  before `is_terminal` is computed off the same dict, and persisted via
  `_snapshot_chain`'s `meta_patch={"chain": chain}`), but "negative
  verdict (contradicts / none-fit)" is still prose, not a named
  predicate. The verifier's actual JSON contract
  (`workers/_chase_llm.py::_PROMPT_VERIFY`) is `{"supports": "yes" |
  "partial" | "no", "contradicts": bool, "caveats": [...],
  "cited_others": [...], "terminal": bool}` — there is no field
  literally called "negative" or "none-fit". The codebase already has
  the canonical predicate for this exact question —
  `workers/_chase_llm.py::is_corroborating(verification)`, its own
  docstring calling it "the single source of truth for the attach
  decision" (`supports=="yes"` → True; `supports=="partial"` → `not
  contradicts`; else False) — but `trust.py`'s spec doesn't name or
  reuse it. Two implementers could reasonably diverge: one might code
  "negative" as `supports=="no"` only, the other as `not
  is_corroborating(verification)` (which also catches
  `partial`+`contradicts=True`) — a real behavioral difference AC 2
  can't be gated on until the predicate is named. Target + blast radius
  should say `trust.py`'s unsupported branch is `not
  is_corroborating(chain[-1]["verification"])` (or explicitly a
  different rule, if that's the intent) — not left for the builder to
  infer.
- **blocker — new contradiction introduced by this edit round.** AC 3
  ("A finding with `meta.unacquirable_override` renders as a clean
  citation" — unconditional) was not updated alongside the new
  Resolutions-log decision ("[the override] suppresses only the
  **unverified** mark; an **unsupported** verdict always renders — a
  negative verification outranks the author's override"). As AC 3
  currently reads, an override always yields a clean render; the
  decided precedence rule says it doesn't when the terminal
  verification is negative. AC 3 needs the same carve-out the
  Resolutions entry states, or the two sections gate different
  behavior.

### Re-review resolutions (2026-08-04, Opus)

- **Blocker 1 residual → RESOLVED**: the unsupported predicate is now
  pinned in Motivation to `not is_corroborating(chain[-1]
  ["verification"])`, reusing `workers/_chase_llm.py::is_corroborating`
  verbatim (never re-derived), with the clean bucket its complement
  (or verification absent). The attach decision and the trust label
  share one source of truth by construction.
- **New AC 3 contradiction → RESOLVED**: AC 3 reworded with the
  carve-out — override converts would-be-unverified to clean;
  unsupported always renders regardless of override.

**Status: READY** — all readiness blockers resolved; proceeding to
implementation (stage a: trust module + export + override write-path;
stage b: editor badges).
