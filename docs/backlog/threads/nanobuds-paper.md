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
**Last reviewed:** 2026-10-01 (pillar review 2026-09-30 same day absorbed the nanobud
library items and the hexfold seam-figure want; pruned gr450329/gr450339,
both STATUS:done)
**Worktree:** `nanobuds-paper`

## Do next

1. **Nanoscale review proposal** — draft at
   `~/work/projects/poster/nanobuds-paper/nanoscale-review-proposal.md`
   (outside this repo); Reto fills authors, the "importance now" lines and
   the key references, then sends it to the editorial office.
2. **restructure** — to the proposal's outline: thesis and scope into the
   Introduction, topology compressed to its tail, properties each stating
   their evidence level, gap analysis as its own section before the
   Outlook; abstract dc2445850 rewritten to the thesis (it already says
   "covalently or non-covalently", so dc2445883 is the chunk to align);
   fold in pa4365.
3. **td450082** — pa1181/42560 duplicate reference merge. Reto approved
   2026-10-01; the auto-mode classifier blocks the session from the prod
   DSN, so Reto runs it: script staged at
   `/tmp/nanobuds-merge-1181-claude.py` on melchior (rehearsal rolls back,
   `--apply` commits; runs `merge_duplicate`, adds a correct cite_key
   alias beside humphreys99a, sets journal Nature).
4. **td450083** — fi191281 re-judge, approved 2026-10-01, same blocker:
   `precis taproot verify-edges --hub fi191281` once with
   `--unverified-stamped` (link 992065, the stale 330-cycle caveat) and once
   without (link 2483302, never verified); reword the claim only if the
   verdict is not corroborating.
5. **backlog/se-nanobud-graph.md** — `status: in-progress/high`; graph-first
   sp2 construction (geo rung, spectral embed, nanobud generator,
   nomenclature) — the library this thread and nanobud-nomenclature-paper
   both need to exist.
6. **backlog/nanobud-claim-remediation.md** — `status: in-progress/high`;
   brings the 139 claim hubs behind the nanobud draft above board — direct
   input to this thread's own claim-hub-signing end state.

## Horizon

1. **nanopub approve/sign pass over dr173020's hubs** — waits on the
   restructure (rewording after signing re-opens hubs); the 173020 batch in
   td345830–td345836.
2. **export + submission** — waits on 1, td450082 and gr454753: the
   draft_export job refuses on the ten image-less reproduced figures ("no
   image yet", jo459047, 2026-10-01) and accepts no placeholder waiver, so
   either every figure gets an image + clearance or the job grows the waiver
   the skill already documents. docx/pdf via the local prod export path
   against the RSC template. 43020 stays frozen.
3. **backlog/nanobud-campaign.md**
4. **backlog/nanobud-nomenclature-paper.md** — blocked-by
   se-nanobud-graph (Do-next 5).
5. **backlog/nanobud-magnetomechanical-memory.md**
6. **backlog/chern-domain-memory-in-the-sheet-generator.md**
7. **backlog/berry-phase-and-topological-defects-in-precis-models.md**
8. **backlog/bond-critical-points-in-structure-model.md**

## Parked

- **hexfold seam-figure want** — a bud neck closing on a sheet with a
  smooth transition + tilt-boundary contrast is not producible today;
  parked on hexfold's smooth-collar horizon (hexfold-toolkit thread,
  `spec.md` §28.5–28.6). Peer session nanobuds, 2026-09-30.
- **hexfold/se cross-cite of the seam-topology section** — unparks when the
  hexfold catalogue's measured rows are trusted (hexfold-toolkit item 6).

## No action needed

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
