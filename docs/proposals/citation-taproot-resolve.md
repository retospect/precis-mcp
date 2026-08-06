---
status: draft
title: Inline [N] markers → chunk_citations + resolve_citation, wired into hub-refine as a citation-following Discover source
model: opus
blocked-by: citation-bib-parse
---

# Inline markers + taproot resolution (`chunk_citations`, `resolve_citation`)

Consumer slice of the citation-resolution work; requires
`citation-bib-parse.md`'s `paper_bib_entries` table. Today a claim reading
"X is true [34]" is verified by hub-refine against whatever corpus-wide
embedding similarity surfaces — never against what [34] actually *is*. This
slice extracts the inline markers, gives taproot a resolution API, and wires
citation-following into hub-refine's existing verify loop, so a claim gets
checked against the paper its author actually cited — and "we read the cited
paper and the claimed content isn't there" is recorded as a **citation miss**
on the hub and rendered red on the claim page (the "flag it red" ask).
Hub-level *trust* is untouched: by decided design
(`docs/proposals/finding-trust-surfaces.md`, hub-unsupported deferral) a
hub's derived trust stays `unverified`/`clean` — the miss is its own
surfaced fact, not a trust-state flip.

## Motivation / why

- Body chunks carry `[126]`, `[129,130]`, `<sup>[126]</sup>` markers
  (verified on pc1556733) that nothing detects or resolves.
- Hub-refine's Discover step is semantic-only; the strongest evidence
  pointer a paper offers — its own citations at the claim site — is unused.
- Verifying against the *cited* paper's chunks reuses the whole existing
  pipeline (STANCE-first verify, grounding `source_handle`, evidence edges,
  trust ladder) — only discovery changes.

## In scope

- **Migration**: `chunk_citations` — `chunk_id, marker, bib_entry_id`.
  Populated by a versioned sweep over embedded body chunks of papers that
  have `paper_bib_entries` rows; swept chunks are tagged
  **`BIBMARK:<version>`** (own tag, decided — same drain-and-converge
  pattern as `CHASETRIG`, independently bumpable).
  - Regex: `[N]`, `[N,M]`, `[N–M]` (ranges expanded), `<sup>`-wrapped
    included. **False-positive guard**: only numbers that exist as parsed
    bib markers for that paper are accepted.
- **`resolve_citation(store, chunk_id: int, marker: int) → BibResolution`**
  (bib entry + doi/s2_id/held_ref_id) in `src/precis/taproot/` — the one
  shared API. NB: unrelated to the pre-existing worker-pass slug
  `resolve_citation:s2` (S2 metadata enrichment for stub refs, seed data in
  migration 0001) — same words, different namespace; don't conflate. `chunk_id` is a raw int per codebase convention; callers
  holding a `pc<id>` handle strip the prefix. `BibResolution.bib_entry_id`
  FKs `paper_bib_entries.id` (the base slice's serial PK).
- **Hub-refine integration (decided — resolves the first `ready` pass's
  trigger-point blocker): citation-following is a second Discover source
  *inside* `_refine_one_hub`, not a new pass.** Concretely, in
  `workers/hub_refine.py`'s existing Claim→Discover→Filter→Verify→Write
  loop:
  1. Discover (existing): semantic top-K, unchanged.
  2. Discover (new): for each of the hub's existing evidence edges'
     grounding chunks (`src_chunk_id` / `source_handle`), read
     `chunk_citations` → resolve → **held** cited papers; for each, a
     paper-scoped search (`store.search_blocks(...,
     scope_ref_id=<paper>)`) with the hub title yields that paper's top
     candidate passages.
  3. Both discover sources merge into **one per-hub candidate set, deduped
     by paper before the Filter→Verify→Write tail** — a shared seen-papers
     set spans both sources within `_refine_one_hub`, so a paper surfaced
     by both gets exactly one verify call (citation-following's candidate
     passages win the slot when both surface it). The already-attached
     drop and `taproot_rejected` memo apply unchanged, so the loop's
     convergence guarantees (rejection memo + `last_refined_*` stamps +
     per-pass hub limit) are inherited, not re-implemented.
  - Verdicts write through the existing machinery: supports →
    `corroborates` with a passage-level `source_handle` inside the cited
    paper. A citation-followed `supports=no` additionally lands in the
    rejection memo **marked `via: 'citation'`** and is appended to the
    hub's `meta.citation_misses` (`{marker, cited_ref, from_chunk}`) —
    the queryable red-flag record. Hub trust derivation is **not**
    modified (hub-unsupported is a decided deferral in
    `finding-trust-surfaces.md`; revisiting it would be its own
    proposal).
- **Claim page**: `citation_misses` render as a red "cited source does not
  support this claim" line per miss (claim/view template + claim_render) —
  the minimal visible form of the flag.
- **Not-held** resolutions are surfaced, not fetched: the refine pass
  records them on the hub (`meta`, e.g. `unresolved_citations:
  [{doi, marker, from_chunk}]`) so the claim page can show "cites a paper
  we don't hold" — acquisition stays on existing explicit paths.

## Explicitly NOT in scope

- **Auto-fetching resolved-but-not-held papers** (decided: narrow with
  fetches). A later proposal may add a budgeted fetch channel.
- **Chase following the cite tree outward** (cited paper's citations, …) —
  fan-out/cost implications; separate proposal.
- **Chunk-level precision from the citation string itself** — `2016, 8,
  2866` is a first page, not a passage; passage-level landing comes from
  the scoped verify, not parsing.
- **Reader UI for markers** (hyperlinks/popovers) — later; this slice only
  persists the data it would need.
- No changes to `chase_trigger`, the forward bridge, or the trust ladder.

## Acceptance criteria

Worked example `pa42553` / `pc1556733` plus units:

1. **Markers**: after the sweep, `chunk_citations` for pc1556733 contains
   markers 126 and 127 → the right bib entries; `[129,130]` expands to two
   rows; a bracketed number above the paper's max marker is not extracted;
   the chunk carries `BIBMARK:<version>` and is not re-swept at the same
   version.
2. **Resolution API**: `resolve_citation(store, 1556733, 126)` (the int id
   behind `pc1556733`) returns the ChemCatChem 2020 identity —
   `held_ref_id` when held, `doi` either way.
3. **Reachability, end-to-end** (the load-bearing one): integration test
   drives `run_hub_refine_pass` (not the internals) on a fixture hub whose
   existing evidence grounds in a chunk carrying a marker that resolves to
   a held paper; the pass attaches a `corroborates` edge whose
   `source_handle` is a passage **inside the cited paper**. Companion case:
   cited held paper does not contain the content → no edge, rejection memo
   entry marked `via: 'citation'`, a `meta.citation_misses` record on the
   hub, and the claim page renders the red miss line. (No trust-state
   assertion — hub trust is unchanged by design.)
4. **Convergence + no double-spend**: a second `run_hub_refine_pass` over
   the same state makes no new LLM verify calls for the same (hub, paper)
   pairs; and within a single `_refine_one_hub`, a paper surfaced by both
   discover sources gets exactly one verify call (LLM spy-asserted).
5. Gate green; state-map taproot section updated; `precis-citation-help`
   updated in the same ship.

## Target + blast radius

- **Migration**: one new table (`chunk_citations`) — forward-only.
- **Workers**: the `BIBMARK` sweep (either its own registry entry or a
  sub-step of `bib_parse`'s cadence — builder's choice, both converge);
  `workers/hub_refine.py` Discover step gains the citation-following
  source.
- **Taproot**: `src/precis/taproot/` gains `resolve_citation` +
  `BibResolution`.
- **Web**: claim page renders `citation_misses` (red miss line) —
  `src/precis_web/claim_render.py` + `templates/claim/view.html.j2`.
  (Display of not-held `unresolved_citations` may ride along but is not
  required by the ACs.)
- **Untouched**: trust ladder (`taproot/trust.py`), `chase_trigger`,
  forward bridge, `citation` kind.

## Open questions / decisions log

Decided 2026-08-06 (discussion with Reto + first `ready` pass):

- Trigger point: citation-following is a second Discover source inside
  `hub_refine`'s existing loop, sharing its Filter/Verify/Write tail and
  convergence machinery — resolves the `ready` blocker; AC 3 pins
  reachability from `run_hub_refine_pass` so it can't ship inert. ✓
- ~~"Flag it red" = existing unsupported ✗ via scoped STANCE verify~~
  **Revised after round 2**: hub trust never reads `unsupported` — that's a
  decided deferral (`finding-trust-surfaces.md`), which round 1 missed.
  "Flag it red" = a `meta.citation_misses` record on the hub + a red
  "cited source does not support this claim" line on the claim page; trust
  ladder untouched. Flipping hub trust on citation misses would be a
  deliberate revisit of that deferral — its own proposal if wanted. ✓
- Sweep marker: own `BIBMARK:<version>` tag. ✓
- No auto-fetch; not-held resolutions recorded on the hub for display. ✓
- Dropped the invented "taproot paper-checking" term per `ready` advisory —
  this is hub-refine's verify loop. ✓
- Split from `citation-resolution.md`; `blocked-by: citation-bib-parse`;
  stays `model: opus` per `ready`'s sizing note. ✓

Round-2 findings resolved 2026-08-06:

- Red-flag blocker → citation-miss record + claim-page line, trust
  untouched (see revised line above); Motivation, In-scope, AC 3, and
  blast radius all updated to match. ✓
- Intra-pass double-spend blocker → both discover sources merge into one
  per-paper-deduped candidate set inside `_refine_one_hub`
  (citation-following wins the slot); AC 4 now spy-asserts single verify
  per paper within one pass. ✓
- `scope=` → `scope_ref_id=`. ✓
- `chunk_citations.bib_entry_id` FK target → `paper_bib_entries.id`
  (serial PK added in the base slice). ✓
- `resolve_citation` takes a raw int chunk_id per codebase convention;
  AC 2 example updated. ✓

## ready agent findings (2026-08-06, round 2)

- blocker: the central "flag it red" claim (Motivation's "surfaces as the
  existing unsupported ✗ trust state") and AC 3's companion case ("on
  terminal verification, trust reads unsupported") are contradicted by
  `taproot/trust.py::_hub_trust` and its own design doc
  (`docs/proposals/finding-trust-surfaces.md`, "blocker 3 resolved", line
  ~88-96): a `TAPROOT:claim` hub's derived trust is **only**
  `unverified`/`clean` — "Hub 'unsupported' is deferred — contradictors
  alongside support is normal science and already surfaced on the claim
  page." `unsupported` is reachable only via `_lifecycle_trust` (a
  non-hub, `STATUS:established` finding's chain verification), a code
  path `hub_refine` never touches — every hub this pass claims is already
  `STATUS:canonical`. So a citation-following (or the existing
  semantic-source) rejection can never flip a hub's trust label to
  `unsupported` under current code; AC 3's companion case is unverifiable
  as written, and "Target + blast radius"'s own "Untouched: trust ladder
  (`taproot/trust.py`)" forecloses fixing it inside this proposal's scope.
  This also means the file's own "Decided" line above ("'Flag it red' =
  existing unsupported ✗ via scoped STANCE verify; no new trust state ✓")
  was made without checking `_hub_trust` and does not hold.
- blocker: the "no double-spend" convergence claim is verified only
  cross-pass (AC 4: a second `run_hub_refine_pass` makes no new calls for
  the same (hub, paper) pair), not intra-pass. In `_refine_one_hub`,
  `attached` is computed once before discovery and `seen_papers` is a
  loop-local set; nothing in the spec requires the citation-following
  source to share that paper-level dedupe with the existing semantic
  source *within one call*. `attach_evidence`'s underlying uniqueness is
  `(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)` — a
  same-paper hit from a *different* grounding chunk in each stream would
  not collide on write, so an implementation that runs the two sources as
  two sequential loops (a natural reading of "second Discover source")
  rather than one truly merged candidate list would silently verify (and
  bill) the same (hub, paper) pair twice in a single pass — gate-green
  under the stated ACs, but violating the module's own bounded-spend
  design goal (`HUBS_PER_PASS x TOPK` calls/pass). Needs an explicit
  statement that the paper-level dedupe set is shared across both
  discover sources before the shared Filter/Verify/Write tail runs.
- advisory: `store.search_blocks(..., scope=<paper>)` — the paper-scoping
  capability exists, but the actual keyword is `scope_ref_id`
  (`store/_blocks_ops.py::search_blocks`/`search_blocks_semantic`/
  `search_blocks_fused`/`search_blocks_lexical`), not `scope`. Directionally
  correct, wrong parameter name.
- advisory: `chunk_citations.bib_entry_id` references an identity column
  that `citation-bib-parse.md`'s `paper_bib_entries` migration never names
  — its column list (`ref_id, marker, raw_text, authors, journal, year,
  volume, first_page, doi, s2_id, held_ref_id, parse_conf, match_conf,
  parse_version`) plus "Unique on `(ref_id, marker)`" states no serial/PK
  column. Likely resolved by the repo's usual `<table>_id`
  `GENERATED ALWAYS AS IDENTITY` convention (most new tables have one;
  `s2_neighbors` is the counter-example, composite-PK-only), but neither
  split file states it, so this proposal's own migration can't cite an
  exact FK target yet.
- advisory: `resolve_citation(store, chunk_id, marker) → BibResolution`
  doesn't state `chunk_id`'s type. AC 2's own example calls
  `resolve_citation(store, 'pc1556733', 126)` — the `pc<id>`-prefixed
  handle string used elsewhere for display (`source_handle`) — while the
  rest of the codebase (`block.id`, `links.src_chunk_id`) treats chunk ids
  as raw ints. Worth pinning down whether the API takes the handle string,
  the raw int, or both.

## ready agent findings (2026-08-06, round 3)

Confirmation pass — verified each round-2 resolution against the current code,
not just the prose.

- Trust claim holds: `taproot/trust.py::_hub_trust` (lines 110-137) returns
  only `"unverified"`/`"clean"`, exactly matching its own docstring ("Hub
  'unsupported' is deferred") and `finding-trust-surfaces.md` (`status:
  built`, line 93, same wording verbatim). `"unsupported"` is reachable only
  through `_lifecycle_trust` (line 156), a non-hub path `hub_refine.py` never
  calls. The revised "flag it red" = `meta.citation_misses` + claim-page red
  line, trust untouched, is accurate and consistent with both files.
- `claim_render.py::render_claim_evidence` → `_render_one` (lines 390-441)
  already threads `hub_ref` (the fetched `Ref`, with `.meta`) and builds the
  template context dict (`coverage_note` pulled from `evidence.coverage_note`
  the same way `citation_misses` would be pulled from `hub_ref.meta`) —
  `templates/claim/view.html.j2` has no existing "citation miss" section but
  its structure (conditional sections gated on truthy context vars, e.g.
  `{% if coverage_note %}` at line 102) is a directly viable place to add one.
  No mismatch between the stated blast radius and the actual render path.
- Double-spend fix is architecturally sound: `hub_refine.py::_refine_one_hub`
  (lines 321-448) already declares `seen_papers: set[int] = set()` as a
  loop-local set (line 372) scoped to the single existing Discover loop,
  checked against `paper_ref_id` before verify. Extending that set to be
  declared once and shared across both discover sources before the
  Filter→Verify→Write tail (as the revised text now states explicitly) is a
  small, well-defined change consistent with the existing code shape — not a
  restructure. `attach_evidence`'s uniqueness constraint
  (`src_ref_id/src_chunk_id/dst_ref_id/dst_chunk_id/relation`) is no longer
  load-bearing for the dedupe now that the guard is at candidate-merge time.
- `paper_bib_entries.id` serial PK confirmed added in `citation-bib-parse.md`
  (In scope migration bullet + its own round-2 "resolved" log entry) — the FK
  target this proposal's `chunk_citations.bib_entry_id` cites now exists in
  the sibling file. `scope_ref_id` (not `scope`) confirmed as the actual
  `store.search_blocks`/`search_blocks_semantic`/etc. parameter name
  (`src/precis/store/_blocks_ops.py`), matching this proposal's now-corrected
  usage. `resolve_citation(store, 1556733, 126)`'s raw-int AC 2 example
  matches codebase convention (`block.id`, `links.src_chunk_id`).
- `blocked-by: citation-bib-parse` resolves to a real sibling file
  (`docs/proposals/citation-bib-parse.md`) — not dangling. `BIBMARK:<version>`
  as "same drain-and-converge pattern as `CHASETRIG`" checks out:
  `CHASETRIG:<version>` is a real, established sweep-tag convention
  (`src/precis/workers/chase_trigger.py`, `CHASETRIG_VERSION`), so the
  analogy is accurate, not invented.
- advisory (new, minor): `hub_refine.py`'s module docstring (line 58) states
  the pass bounds spend to "(at most) `HUBS_PER_PASS x TOPK` calls" — this
  proposal adds a second discover source whose candidate count isn't itself
  bounded by `TOPK`, so the per-hub worst case technically grows beyond that
  stated bound once citation-following is wired in. In practice the new
  source's candidate count is bounded by the hub's *already-attached*
  evidence-edge grounding chunks (typically small), so this is unlikely to
  matter operationally, and it's a stale code comment rather than a spec
  defect — but neither the proposal's Motivation/In-scope nor its blast
  radius mentions touching that docstring, so it's worth a one-line update
  alongside the implementation rather than left silently inaccurate.

Round 3: no blockers, gate-clean.
