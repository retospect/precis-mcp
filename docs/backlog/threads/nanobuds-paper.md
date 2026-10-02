# nanobuds paper

**Status:** ends when dr173020 is submitted to a venue with every claim a
signed finding hub and its thesis (geometry, not composition, sets the
electronic structure, and almost none of it has been measured) leading the
paper. Today the poster is print-ready and the paper is a survey grown by
accretion with the thesis buried in a 233-word gap analysis; a taproot
backfill converted 13 chunks to hub cites and left two residues, both repaired
on 2026-09-30; both
measured-analogue papers (Huang 2011, Seiler 2024) are in as hubs and cited,
the 13 converted chunks are read back and repaired, and the Gap Analysis
section now opens with the thesis and carries both analogues (2026-09-30).
Venue decided 2026-10-01 (Reto, td450081): *Nanoscale* (RSC) Review article,
10,000+ words, so the restructure reorders rather than cuts; scope is covalent
and non-covalent buds. RSC reviews go through a proposal form first.
**Deployed vs landed (2026-10-02):** everything this thread shipped is on
prod (fleet 7242d4c9 includes 974e3a20 hygiene fix and 1f2324a4); nothing
is landed-only. **Traps:** the session classifier blocks direct prod-DB
Python scripts (td450082 is Reto's to run) but allows `scripts/prod-precis`
CLI calls and read-only `scripts/prod-psql`; `verify-edges` judges each
edge against the whole claim, so never attach a methods-only passage to
"name the method".
**Last reviewed:** 2026-10-02 (pillar review 2026-09-30 same day absorbed the nanobud
library items and the hexfold seam-figure want; pruned gr450329/gr450339,
both STATUS:done)
**Worktree:** `nanobuds-paper`

## Do next

State 2026-10-02 14:00Z (hold lifted 13:00Z). Review items answered:
5–13 (13: `report` + `definition` artifact types approved, web sources
cited by URL + content sha; claims-and-evidence builds them, then this
thread signs the Canatu hubs fi191169/fi191260, grounded on Canatu's own
DOI'd SID papers pa689/pa1771). Open: 14 (source audit: remove 20 weak
edges + pin every hub cite; claims-and-evidence also offered a fallback
fix in `taproot/cite.py::hub_cite_keys`). Edit-verb year=/journal= fix landed
4cf8b684e (round 1). In this order:

0. **Round-1 deploy (567f207f) checks, done 2026-10-02 ~21:00Z:**
   pa2615 now year 2007 / Nature Nanotechnology (`view='bibtex'`
   verified; citekey still `wang22c`, no rename door on paper edit).
   `nanobud-review-figs` regenerated with the placement fix (old
   structures retired): closest non-bonded pair 1.34–1.38 Å, none
   < 1.25 Å; only the two [9-6] necks carry geom.clash WARNs (1.49–1.72
   Å). Rendered (`nanobud-fidelity/figs/render_buds.py`, contact sheet
   in `figs/render/`) and filed as look-at item 17; nothing goes into the
   draft before Reto's ok. Still to do on the deploy:
   `precis enrich-rearm --refs <cited paper ids> --apply` on prod, then
   export with `bib_style='chem-rsc'`.
1. **Figures** (items 5/6): the five kept third-party figures are in
   (dc4291496–dc4291500, permission `requested`, Reto files RightsLink at
   submission; image door = `put(args={'image': b64, …})`, scripts in
   `nanobud-fidelity/figs/`). Own redraws left: dc3015722 as `own_graph`
   from pc388398/pc388407 (cap1–7 binding −0.158 to −0.476 eV vs sidewall
   +0.736/+0.685; DFT-D3 barriers 1.92/0.77 sidewall, 1.52/1.46 cap4; needs
   a table chunk for `plots=`), dc3015729 as a 2D sublattice schematic,
   and dc3015720/23/30 from the regenerated structures (no
   structure→figure door, gr461790; local route viz3d `stick_scene` →
   `render_svg` → `image=`).
2. **Citations** (items 7/14, done 2026-10-02 14:40Z except item 15):
   Sharma cut; ASE/pymatgen cited (pa4423, pa1944). `bib_style` +
   Crossref volume/issue/pages + `precis enrich-rearm` landed 6d92b2ba3
   (undeployed): after the deploy, `enrich-rearm --refs <cited ids>
   --apply` on prod, then export with `bib_style='chem-rsc'`. Item 14:
   16 weak edges removed (patent edge by link id, backup
   `nanobud-fidelity/removed-link-2397876.json`), Hamoudi/Liu/Dai sentences
   cut, Antonenko sentence re-grounded on pc13280, 32 hub cites pinned to
   their verified originating passage (`apply_pins.py`). The 150 unpinned
   cites wait for claims-and-evidence's ruled fallback (Reto,
   review-queue answered/reto-cite-standard-1.md). Item 15 open: HOMA/NICS
   primaries not held, ref 893 (Tans 1998 inside an issue scan), the
   phosphorene preprint, and the td450082 script restage.
   **Printed-source read (orchestrator verdict, reviews/
   nanobuds-paper.review.md §1, accepted):** once claims-and-evidence's
   ruled fallback has a stable before/after key set for dr173020 (their
   note §3, after the orchestrator has looked at their samples), read
   only the ~150 sources it prints. One row per (hub, source chunk):
   does the passage state the sentence's claim as its own result —
   yes / no / cites-another / partial, with the passage quoted, in the
   `evidence-edge-verification` row shape. Hand-check 15 rows across the
   four answers before trusting totals. Report "n of 150 printed sources
   do not carry their sentence" plus method in the fidelity pack. Over
   $25 → review item first.
3. **Reground batches: DONE 2026-10-02 ~20:15Z** (461608, 461803,
   462063, 462112 … 462390). Cited hubs now: 168, 696 evidence edges, 61
   still withheld on 56 hubs. OPEN.md items 5 and 7 settled: dc2445904
   re-pinned `[fi191135>pc417923,pc417910]` (its old pin pc417846 was not
   an edge), dc2445957 `[fi272040>pc209495]`. Left from td351821 NEXT:
   (b) the PRUNE-judged withheld edges → Reto keep/remove; (c) the
   spurious disputes (pa44589 on fi191150; prior-review records
   pa255164/pa192706/pa255165 on fi191315/fi191316/fi191329; pc396933 on
   fi269543). The (a) fidelity re-check folds into the printed-source read
   above.

The items below are all Reto's; each is a `waiting-for:reto` todo under
td173019.

1. **td461160 — Nanoscale review proposal** — draft at
   `~/work/projects/poster/nanobuds-paper/nanoscale-review-proposal.md`
   (outside this repo; authors filled from the draft); Reto fills the
   "importance now" lines and the key references, checks byline order,
   then sends it to the editorial office.
2. **Reto's read-through** — td461161 findings check (first case:
   dc2445908); figures sourced by Reto; export read on the
   placeholder-figure export (jo461157, queued 2026-10-02 — see Horizon 2).
   Then td461162: the Phase 5 adversarial review from the precis-web
   review block.
3. **td450082** — pa1181/42560 duplicate reference merge. Reto approved
   2026-10-01; the auto-mode classifier still blocks the session's direct
   prod-DB script (re-tried 2026-10-01 after Reto's "prod is authorized"),
   so Reto runs it: the script staged at
   `/tmp/nanobuds-merge-1181-claude.py` on melchior was LOST in the 2026-10-02 11:31Z reboot and needs restaging (rehearsal rolls back,
   `--apply` commits; runs `merge_duplicate`, adds a correct cite_key
   alias beside humphreys99a, sets journal Nature).
4. **backlog/se-nanobud-graph.md** — `status: draft/high`; re-scoped
   2026-10-02: geo rung, embed, registration and assembler mode shipped;
   generator, sublattice parity, chirality and nomenclature overtaken by
   hexfold. Left: a rigidity screen and a Cases A–D test of hexfold's
   `annot.sublattice`, both via the hexa session.
5. **backlog/nanobud-claim-remediation.md** — `status: in-progress/high`;
   brings the 139 claim hubs behind the nanobud draft above board — direct
   input to this thread's own claim-hub-signing end state. 2026-10-02 pass
   done (fi189536 grounded, dc2445930 cut); open = the non-empirical
   artifact type (`/go`), ref 2615, Phase 5 after Reto's findings check.

## Horizon

1. **nanopub approve/sign pass over dr173020's hubs** — waits on Reto's
   findings check (Do next 2; rewording after signing re-opens hubs); the 173020 batch in
   td345830–td345836.
2. **export + submission** — waits on 1 and td450082. The
   `placeholder_figures` waiver is deployed (gr454753, on prod since
   2026-10-02), so a reading export no longer needs the ten figures;
   jo461157 is that export (params `placeholder_figures: true`). Submission
   still needs every figure imaged and cleared. docx/pdf via the local prod
   export path against the RSC template. 43020 stays frozen.
3. **preprint slot blockers** — the repo-side blockers for the monthly
   preprint (the ten image-less figures, gr459050) are
   this thread's; the posting step itself (arXiv/Zenodo submission) is
   Reto's: td459586, qu459585's October todo.
4. **backlog/nanobud-campaign.md**
5. **backlog/nanobud-nomenclature-paper.md** — blocked-by
   se-nanobud-graph (Do-next 5).
6. **backlog/nanobud-magnetomechanical-memory.md**
7. **backlog/chern-domain-memory-in-the-sheet-generator.md**
8. **backlog/berry-phase-and-topological-defects-in-precis-models.md**
9. **backlog/bond-critical-points-in-structure-model.md**

## Parked

- **hexfold seam-figure want** — a bud neck closing on a sheet with a
  smooth transition + tilt-boundary contrast is not producible today;
  parked on hexfold's smooth-collar horizon (hexfold-toolkit thread,
  `spec.md` §28.5–28.6). Peer session nanobuds, 2026-09-30.
- **hexfold/se cross-cite of the seam-topology section** — unparks when the
  hexfold catalogue's measured rows are trusted (hexfold-toolkit item 6).

## No action needed

- **td450083** — done 2026-10-01 via `scripts/prod-precis`: fi191281's
  links 992065 (`--unverified-stamped`) and 2483302 both verified
  `supports: yes` and stamped against pc391145 (1600 mAh/g at 8 A/g over
  300 cycles, 99.01% CE); no reword.

- **restructure** — done 2026-10-01 in two passes. Pass 1: Scope
  (dc2445882) ahead of the topology section, Gap Analysis (dc2445953)
  top-level before Future Perspectives, thesis line dc2445855, roadmap
  dc2445884, abstract dc2445850, pa4365 hubbed as fi460165 and cited in
  dc2445899, Conclusion dc2445958 no longer counts field emission and
  composites as tested. Pass 2: every property subsection now states its
  evidence level (dc2445920, dc2445922, dc2445924, dc2445926); Gap Analysis
  dc2445954 no longer claims an NLO measurement the draft never cites.
  "Bridging Theory and Experiment" (dc2445945) stays: Gap Analysis states
  the gap, it names the measurements.

- **td450087** — done 2026-10-01: the README rewrite was already committed in
  the poster repo; Reto moved the poster to the posters folder
  (`~/work/projects/poster/poster-cmd2026-nanobuds`, a clone with history;
  the copy under `pres/` stays until Reto removes it).
- **pa4365** — cite candidate from the gap-finder (first-principles nanobud
  states 0.3–0.8 eV above the Fermi level, matching dc3015724's STS
  features); folded into the restructure (Do next 2).

- **td458280** — done 2026-09-30 (Reto: heading stays "Gap Analysis", the
  disclination clause stays, go): dc2445954 rewritten to lead with the thesis,
  dc4262976 (Huang grain-boundary analogue) and dc4262977 (Seiler bilayer
  benchmark) added under it, dc2445946 trimmed of the sentences that moved;
  the gap table dc2445955 stays retired, the prose carries every row.
- **dogfood 2026-09-30 (fleet 7c915f79)** — the three cites that drifted when
  fi449540/fi449588/fi449590 were retitled (dc3824813/15/16) re-pinned by
  rewriting each chunk unchanged; export no longer blocked. Three legacy pc cites
  became hubs: fi458947 (twelve-pentagon rule, dc2445873; grounded on pc2580141 plus the two primary sources review note fi188551 named, pc244266 and pc281158, attached 2026-10-01), fi458948 (AFM
  5-7 kinks, dc2445877) and fi458952 (schwarzon shielding, dc2445916). Filed gr458941 (every hub shows "no originator
  derived yet") and gr458943 (hygiene view and windows refuse forms the skill
  documents). The ten caption cites stay `Reproduced from [pc…]` by policy.
- **gr459050** — dr173020 is bound draft-of two project todos (td43019 carried
  over by the fork, td173019 its own); filed 2026-10-01, fix belongs in the
  fork; do not drop the edge by hand, 43020's binding must stay.
- **gr450123** — embedder contention; infra, owned elsewhere.
- **jo449492** — the backfill succeeded (189 scanned, 13 converted, 0
  failed); read back in full by td458276 (done 2026-09-30) — a re-run over
  dr173020 is safe.
