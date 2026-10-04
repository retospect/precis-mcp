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
**Resume (2026-10-03 22:10Z):** round 3 deployed at 929107f3. This thread's round-3 lands were thread-file edits only, so there is no code to dogfood. The prod work was data edits (items 27–29 applied, td450082 merged), each verified when it was applied.
- **Open, waiting on others:**
  - item 31, the hero pillar: Reto redirected it to hexfold's spec-driven shapes capability. The review session re-asks after hexfold answers. The hero figure stays as it is. The bonded (12,0) pillar is at hexfold's cycle 3.
  - claims-and-evidence-9 is with its owner.
  - **Round-4 dogfood, 2026-10-04 04:45Z (prod 727728cc):** `scripts/prod-precis draft export 173020 --bib-style rsc` (the numeric id; `dr173020` gives "no draft") ran clean. It printed 37 pin warnings, as measured; 9 "pinned pa… but derived originator is …" notes; and 4 placeholder figures (dc3015722, dc3015723, dc3015729, dc3015730).
    - Output is in `nanobud-fidelity/export-r4/`. Triage table: `printed-read/pin-triage-r4.md` (read-only agent).
    - pa1181 exports right (Yao et al., Nature 1999, doi 10.1038/46241) under the old key `humphreys99a`. The key is invisible in RSC numeric style; cosmetic.
  - **Pin triage done 04:50Z:** 13 noise, 22 re-pins, 2 rewords.
    - The 22 re-pins were applied on prod; each adds the passage that names the method (`printed-read/apply_pins_r4.py`, log `apply-pins-r4.log`, pre-flight and post-check clean).
    - Re-export: 37 → 20 warnings. The remaining ones are noise (the acronym against its spelled-out form, signed numbers, NanoBud vs CNB, generic words), sent to claims-and-evidence for their noise sweep.
    - Open with Reto: **nanobuds-paper-32**, four sentences:
      - A, "CVD": keep;
      - B, Ahangari "corroborated these MD results": reword;
      - C, the lithium clause, which no hub carries: cut;
      - D, uncited background numbers in the Novoselov sentence: leave.
    - Background for the queued item below.
    - Measured before deploy: 37 of 89 pins warn, none for G4. In a sample about 6 in 10 are real gaps, e.g. DFT, DFTB or HOMO/LUMO missing from the pinned chunk. Known noise: negated terms, words glued by extraction, numeric locants, range endpoints.
    - Each warning suggests up to 3 better chunks: re-pin to one if it carries the term; if none does, file the gap.
    - fi189535 (anchored) now flags TEM and STS as uncovered and suggests pc209502 and pc209508. Its fix is still a supersede, which waits on the unbuilt supersede door. This thread owns it.
- **Reto-run:**
  - `scripts/prod-precis tools delete --kind draft --id dc3015729`, and the same for dc3015722;
  - enrich-rearm over `export-tex/cited-paper-ids.txt` (pa1181 still lacks volume and pages);
  - pillar delete + `pillar/regen.py` + re-export;
  - rename the stale `~/.claude/projects/-Users-reto-precis-mcp/nanobud-fidelity/merge_1181.py` (the classifier blocked the session).
- **Off-thread requests from this session, each with its owner:**
  - local-compute: MeluXina bumped up, with uses widened to MLIP and DFT; a local tuned-model plan; `docs/backlog/greenfield-cluster-layout.md`;
  - knowledge-mesh: rxn_values folded into measures; LLM specs as a consumer; range + conditions search with SI storage (in `measures-substrate.md`);
  - orchestrator: the fleet's default model is sonnet.
**Deployed vs landed (2026-10-02):** everything this thread shipped is on
prod (fleet 7242d4c9 includes 974e3a20 hygiene fix and 1f2324a4); nothing
is landed-only. **Traps:** the session classifier blocks direct prod-DB
Python scripts (Reto runs those) but allows `scripts/prod-precis`
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

0. **Round-2 deploy (63301c5c, 13:49Z 10-03), dogfood:**
   - **Export with the new cite fallback** (`export-tex-r2/`, chem-rsc) works: 120 bib entries.
     The fallback prints fewer keys, but the pins add their papers back.
   - **All 120 entries lack volume and pages.** The RSC bibliography needs
     `enrich-rearm` over the 170 cited papers (`export-tex/cited-paper-ids.txt`), then a worker cycle, then a re-export.
   - **Two Reto-run commands, both denied to the session by the auto-mode check:**
     - the enrich-rearm (dry run, then `--apply`);
     - the pillar regeneration: soft-delete structure `hexa-nanobud-pillar-bud` (459564), then
       `nanobud-fidelity/pillar/regen.py`. The old record is in `pillar/old-generated.json` and its atoms in `before.extxyz`.
     - After both: `ztable.py <new id> after` and the RSC re-export.
   - **New export advisory:** "[fiX] pinned {paY} but derived originator is {paZ} — reconsider", on 9 hubs:
     fi189535, fi189536, fi190976, fi191314, fi191016, fi191021, fi191123, fi191164 and fi272040.
     fi190976 is already known (pa2615 SI vs pa2069). Under the adopted rule a non-originator pin is fine when it supports the sentence.
     Checked: one real case, fi191314 in dc2445942 ("the original synthesis" pinned to pa1483, not pa2069). Filed as item 28 §A7. The rest are fine.
   - **Frozen hubs (14:42Z, at the review session's request):** dr173020 cites 15 hubs whose nanopub is `anchored` (td345840 done). A reword of one of those hubs needs a supersede, and the supersede door is unbuilt.
     - Item 28 touches three: A3 fi191270, B1 fi189544 and B10 fi189548.
     - A3 and B1 change draft prose only.
     - B10 is re-proposed as B10′, which uses bare pc cites so that no link is added to the anchored hub.
     - Check script: `nanobud-fidelity/printed-read/frozen_check_28.py`. Before applying any edit to a cited hub, re-run it; a fix that touches an anchored hub's claim sentence waits for the supersede door.
   - **Item 28 applied 15:05Z (10-03): all as proposed, B10 as B10′, and the four extras.**
     `printed-read/apply_28.py` ran 3 link adds, 20 draft edits and 5 link removals, all ok (`apply-28.log`). `verify_finds_28.py --after` read back clean.
     - A1 removed three pa345 links (pc22030, pc22031, pc22037), not the two the item listed.
     - fi192819: the matched-mat comparison went in as the bare cite [pc209519]; no hub carries it.
     - fi189527 and fi189535 are re-pinned to their signed groundings.
     - fi458948 now says "argues against".
     - claims-and-evidence re-ran its cite-fallback pass: printed pairs went from 114 to 107, all expected (`scratch/cite-standard/dr173020-printed-pairs-apply28-diff.tsv`).
     - fi191144 no longer appears in the draft because A2 deleted it.
     - fi189548's two bare uses are now pinned inside its signed grounding: dc2445881 >pc40252, dc2445916 >pc40243,pc40252.
   - **Item 29 answered 16:06Z, applied 16:10Z.**
     - Item 26's pins are restored: dc2445860 `[fi189535>pc209495,pc209502,pc209505]` and dc2445859 `[fi189527>pc452187,pc452198]`. All pinned passages are linked.
     - Pin rule: a pin stays within the signed grounding's papers and names the passage that carries the sentence.
     - Reto's follow-up, how to stop this recurring and catch it automatically, is filed as **claims-and-evidence-9** (`review-queue/open/claims-and-evidence-9.md`), owned by claims-and-evidence. My three proposals are in it as G3, G1 and G5; the pin rule is G4. I sent:
       - (f) pin-vs-sentence term coverage and a narrowing warning;
       - (g) a sign-time check for evidence edges added after review;
       - (h) absence-claim search discipline.
     - Case facts: fi189535's publish row was created 09-19 16:06Z; its methods edges pc209502 and pc209509 were linked at 16:29Z; signed 09-22. Of the 15 anchored hubs, fi189535 is the only confirmed front-matter grounding. fi236369 and fi236370 are Nature-letter candidates, unread (`printed-read/grounding-depth.out`).
   - **Item 29 history, corrected 15:39Z after Reto pushed back.** fi189535's claim is supported by its papers; no supersede is needed.
     - STS is in pa2069: pc209508 and pc209509, and pc209505 (Fig. 2e). TEM is in pa2069 pc209502 (Fig. 1). The graphene buds are shown by HRTEM in pa1120 pc99780.
     - The signed grounding picked an abstract and a definition instead of these passages.
     - My first search needed the literal phrase "tunnelling spectroscopy"; pa2069 writes "microscopy (STM) and spectroscopy (STS)". Log: `printed-read/sts-recheck-29.out`.
     - Item 28 extra 2 (my proposal) removed the TEM/STS passages from the fi189535 pin and "for the first time" from fi189527.
     - Recommended: restore item 26's pins. The rule becomes that a pin stays within the grounding's papers, not its exact passages.
0. **Round-1 deploy (567f207f) checks, done 2026-10-02 ~21:00Z:**
   pa2615 now year 2007 / Nature Nanotechnology (`view='bibtex'`
   verified; citekey still `wang22c`, no rename door on paper edit). (fi189540, the SI-only hub, was retired 08-28 and dr173020 does not cite it. The only draft hub on pa2615 is fi190976, which also has pa1483 and pa2069.)
   `nanobud-review-figs` regenerated with the placement fix (old
   structures retired): closest non-bonded pair 1.34–1.38 Å, none
   < 1.25 Å; only the two [9-6] necks carry geom.clash WARNs (1.49–1.72
   Å). Rendered (`nanobud-fidelity/figs/render_buds.py`, contact sheet
   in `figs/render/`) and filed as look-at item 17; nothing goes into the
   draft before Reto's ok. Still to do on the deploy:
   `precis enrich-rearm --refs <cited paper ids> --apply` on prod, then
   export with `bib_style='chem-rsc'`.
   **Item 17 answered 21:21Z:** structures decent, perspective needs work →
   persp + bud-up framing + fog rendered (`figs/render_persp.py`,
   `persp-compare.png`), camera choice is look-at item 18; viz3d persp
   scaling gripe gr462672. New ask: one composite hero figure on a single
   sheet (front-left tiny bump, behind it a tube with C60 on top, to the
   right a bud and a pill protrusion), built with the smooth-space/hexfold
   tooling; feasibility asked of hexfold-toolkit; preview to Reto before
   the draft.
   hexfold-toolkit: one spec today (`hexfold-corner/hero2.hx`, copied to
   `nanobud-fidelity/figs/specs-hero/`). Local build is clash-clean
   (2850 atoms, min 1.26 Å) but mis-placed: the pillar's C60 sits inside
   the (12,0) tube top (a peapod), the sheet bud hangs under the sheet,
   and the cap-on-hole bump is flat. Sent back to hexfold-toolkit 21:55Z.
   No Reto preview until the ball sits on top (`figs/render_hero.py`
   re-renders).
   **Hero figure:** hexfold-toolkit fixed the placement (now 15b943888, rebased from 3d17b6179; branch
   only, awaits orchestrator design review C4); preview with it applied
   uncommitted = look-at item 19 (`figs/render/hero-az-110.png`, spec
   `figs/specs-hero/hero3.hx`). Mint on prod only after that sha deploys.
   **Items 18/19 answered 22:35Z.** 18: camera c (az −75°, el 12°, fov 28°)
   for tubes; no sheet camera yet → new options on a 55 Å render-only sheet
   (`figs/render_sheet_opts.py`, `specs-sheet/`, `sheet-compare.png`) =
   look-at item 23; picking one means regenerating the two minted sheet
   structures at that size. 19: change it (rear-left pillar thinner + shorter,
   diameter < C60; rear-right wider neck; gentler flanges; features closer)
   → hexfold-toolkit sent `hero4.hx` (copied to `figs/specs-hero/`;
   `render_hero.py hero4`, 3d17b6179 applied uncommitted then restored):
   2748 atoms, min 1.05 Å at the pillar's fused neck (9 WARNs). Pillar is
   now (6,0) with a fused-neck C60 ([9-6] impossible on a (6,0) lid); gentle
   flange not producible (parked smooth collar). Look-at item 24.
   **C4 approved (orchestrator, ~23:40Z):** landed as main af16a39ef
   (15b943888 + review changes; hero4 rebuilt unchanged on it). After
   round 2 deploys: regenerate `hexa-nanobud-pillar` block `bud` (459564,
   same spec; remove_block + generate), run
   `nanobud-fidelity/pillar/ztable.py <new id> after`, and fill the "After
   (prod)" table in `reviews/nanobuds-paper.md` (release item). Before
   table recorded: ball 19.5–25.6 Å under a 26.1–27.3 Å lid; expected
   after: ball 27.9–33.7 Å on the lid. Orchestrator accepted the tables
   as release-2 evidence (12:05Z, 10-03). If prod relaxation drops the
   ball more than ~1 Å toward the lid, report it to hexfold-toolkit as a
   finding; it is not a regeneration error.
   **Items 23/24 answered 06:44Z (10-03).** 23: camera d (el 18°), 55 Å
   sheet kept → done: gsheet22/96 regenerated on prod (st463286/7; round-1
   prod hexfold builds them identically to main, so no wait on round 2;
   prod atoms = local build, `figs/prod_vs_local.py`), and both structure
   figures put into the draft as `original` (`figs/render_final.py`,
   `upload_own.py`): dc4300876 tubes (camera c, bud22/87/96 = st462601-3)
   replaced placeholder dc3015720; dc4300877 sheets after dc2445859.
   dc3015729 (magnetic seam cases A–D) is replaced by dc4306940 (item 27). 24: wider necks + rounder transitions →
   hexfold-toolkit sent hero5/hero5a (`figs/specs-hero/`, compare
   `figs/render/hero5-compare.png`; necks only come in (6k,0) steps, so
   pill/bump went (6,0)→(12,0)).
   **Item 25 answered 11:09Z (10-03): neither.** The hero waits for
   hexfold's smooth work, so nothing is minted from hero4/5/5a. Reto's ask:
   - first an ideal smooth surface: sheet, a fillet radius into the tube,
     a radius into the ball;
   - then tile it, working backwards from the ideal shape (the
     hexa-smooth-drum smooth surface looks co-optimized by the tiler);
   - also standard fullerenes generated from a sphere of the right
     diameter.
   **S4 hero delivered 15:47Z (10-03), filed as look-at item 30 (pill12 vs pill24).**
   - Source: `hexfold-corner/s4_handoff.md`. Rendered by `figs/render_s4.py` (positions from the tethered extxyz, not `stick`) into `figs/render/s4-compare.png`.
   - The pillar's (6,0)→C₆₀ join has 8 atoms in non-bonded pairs at 1.05–1.33 Å (`figs/s4_closepairs.py`, close-up `s4-neck-az0.png`). This is common to both variants, and hero5/5a have it too: the (6,0) stick joint has had it since the pillar went to (6,0).
   - **Route (a) failed, 16:00Z.** Under MACE-MP, 4 of the 6 fused (6,0)→C₆₀ seam bonds open to 4.7–4.9 Å, also on a free tube+ball. The fused thin neck is not a bonded minimum.
     - The `s4_pill12_top*` files are diagnostics only; do not render them.
     - Item 30 answered 16:06Z: pill12 at azimuth −110, top-only relax, caption says so.
     - **Correction 16:25Z: the "fused neck tears" result is not established.** The same MACE-MP-small protocol also tears the literature-stable [9-6] (10,10) control. Only the (6,0) [2+2] bud failure is credible, because its control holds.
     - **GFN2-xTB, 16:33Z, also tears the neck** (4 of 6 seam bonds at 4.6–4.8 Å). The [2+2] control holds (1.578 Å) and the [9-6] control tears, as under MACE. The top-only relax is therefore off. hexfold files the [9-6] construction against itself; the stick start's crowded pairs may force the tear. Wording: "could not relax", never "unstable".
     - **Lid pillar, 17:40Z:** the isolated (12,0) pillar (flat lid, C₆₀ bonded [2+2] to the sidewall under the lid) passes MACE and xTB; [2+2] on top of the lid is unreadable (the methods disagree). In the pill12 scene, the tethered relax tears the sidewall bud; that is a planner fix, one dev cycle behind S4b, owned by hexfold. `s4_pill12_*_FAIL`, `_band`, `_footonly`, `_v5`, `_v6` are diagnostics; do not render them. Fused (9,0) cannot be built (`port.mismatch`).
     - **20:41Z:** Reto did not pick A/B/C. He redirected to a capability ask (spec-driven shapes, bearings, nested shells), now with hexfold-toolkit. Item 31 stays open; keep the hero figure as it is. hexfold, 20:50Z: the bonded pillar is 3 dev cycles out behind Reto's ball-top-first pick. The in-scene tear is root-caused: the tethered relax stops at pass ~8 because its settle test ignores the bud, and pass 16 has 0 close pairs. Correction 21:14Z: the clash-aware relax clears the close pairs (pass 14), but the bud seam still opens in the scene (MACE 2.58/3.55 Å, xTB 1.65/2.14 Å). There is no early slot: the bonded pillar stays at hexfold's cycle 3 (buds on tethered tubes). No files.
     - Item 31 revised 17:42Z. Recommended: A, the stick-neck hero now, captioned "idealised stick model; the pillar–ball junction is unrelaxed", swapped for the bonded (12,0) hero when hexfold's planner fix passes. B: wait. C: A plus an isolated-pillar close-up.
   - The figure is not final until that is fixed and Reto picks a variant.
   - A prod se record of this spec is not this scene until S4b.
   **Items 15/16 (answered 21:34Z):** 15 done; item 20 done 19:39Z: pa1181 merged with 42560, journal Nature, all cite keys on pa1181, ref 893 retired
   (Reto ran the stale nanobud-fidelity/merge_1181.py, so the cite-key move was done by hand). HOMA/NICS stubs
   pa462694/pa462695 in the fetch queue → then re-ground fi449540.
   Phosphorene: fi191297 reworded to Carbon 2021 body values, preprint
   (ref 50777) edges removed. 16 is NOT a ruling: remove nothing; a
   read-only analysis (`nanobud-fidelity/prune-analysis.md`, agent
   running) feeds a re-filed item answering cost / reasons / recoverable /
   pruning-on, with the four disputes edges. → DONE: re-filed as item 21
   (fix 21 needed edges, prune 78 free, pruning stays off until the judge
   checks the citing draft sentence; claims-and-evidence told).
   **Item 21 answered 22:26Z: fix-then-prune approved**, plus the 4 disputes
   edges; Reto wants pruning ON afterwards → answered (it needs
   claims-and-evidence's draft-sentence check, `slice_refine_eval`, and an
   orchestrator deploy of `PRECIS_TAPROOT_REGROUND_PRUNE=eval-passed`);
   item 22 ruled 22:44Z (option 1): pruning stays off until the
   draft-sentence check + `slice_refine_eval` are in, then the orchestrator
   opens the interlock (round 3 est.); owned by claims-and-evidence (told,
   with `prune-edges.tsv` as the test case: prune 78, keep 21). Apply plan
   `nanobud-fidelity/fix-plan-21.md`, backups taken. **Applied 23:10Z**
   after the read-only incident (gr462726): 11 split hubs minted
   (fi462728, fi462732–fi462741), 7 draft chunks re-cited (dc2445859/96,
   dc2445904, dc2445930, dc2445932/34, dc2445940; dc2445904 dropped
   "and bias fields", no source carries it), 95 edges removed
   (`apply_removals.py`, `removals.log`; the 3 review-finding disputes
   edges had to be removed from the finding side). Read-back
   (`readback_21.sql`) clean: every draft pin resolves; W48 (Canatu) kept
   for the report-type reword; a pre-existing broken pin
   `[fi190976>pc279174]` (pa2615 passage, edge lost earlier) re-attached.
   Left from it: dc2445940's "nonlinear optical response" clause has no
   hub carrying it (find a source or cut it before signing). A `verify-edges` re-judge was
   classifier-denied (LLM spend); stale `partial` labels are legacy-valid
   for the preflight, so re-judging is optional.
1. **Figures** (items 5/6): the five kept third-party figures are in
   (dc4291496–dc4291500, permission `requested`, Reto files RightsLink at
   submission; image door = `put(args={'image': b64, …})`, scripts in
   `nanobud-fidelity/figs/`). **Item 27 answered 14:46Z (10-03): both redraws, with the proposed captions.** They went in at 14:47Z as `original` figures through `figs/upload_own.py sublattice|pathways` (log `upload-27.log`):
   - dc4306940, the sublattice bond-formation rule, after dc3015729;
   - dc4306941, the cap vs sidewall paths, after dc3015722. The source itself is inconsistent here: it says "0.46 eV lower", but its stated barriers give 0.40.

   **Reto-run (classifier denied the delete):** retire the two placeholders, so the export does not print each figure twice:
   `scripts/prod-precis tools delete --kind draft --id dc3015729`, then the same for `dc3015722`.
   dc3015723 (pyrene-tethered C60) and dc3015730 (MD laddering) are images
   of computed structures, outside hexfold, so they stay reproduced
   unless Reto drops them (item 27 proposed keeping them; the answer did not object). dc3015720 is done (dc4300876). The
   dc2445940 NLO clause now cites the new hub fi463611>pc1050540 (pa5887
   hyperpolarizability, highest in configuration F).
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
   **Printed-source read: DONE 2026-10-03 ~07:40Z.** For 61 of 159
   printed sources, no single linked passage individually carries the
   full sentence. This is not a miscitation count (Reto 11:56Z ruling).
   Report: `~/.claude/projects/-Users-reto-precis-mcp/nanobud-fidelity/
   printed-read/REPORT.md`. Wrong-paper ingest: gr463414.
   **Item 26 ruled 11:56Z after a Fable audit**
   (`reviews/citation-rigour-audit-2026-10-03.md`). Adopted citation rule:
   - support is judged against the paper's full text, and passages may
     combine;
   - the pin is provenance, not the test;
   - polarity is a separate verdict;
   - secondary sources are allowed for background, primary preferred;
   - a finding credited to a group cites that group.
   **Item 26 applied on prod, done by 12:10Z:**
   - backup taken;
   - 24 links removed (the 16 removals, the 4 swaps, and the fi236137,
     fi191317 and fi192819 misstatements);
   - 15 evidence links added;
   - 22 hubs pinned;
   - fi191152 softened and the fi189535 edit made;
   - polarity pass run: 88 agree, 10 overstated.
   Log: `printed-read/APPLY-26.md`.
   **Open: review item nanobuds-paper-28** (filed 12:10Z): 15 wording
   fixes. §A: 5 held hubs. fi189545 swaps to Baowan pa692; fi191144,
   fi191270, fi449588 and fi449580 lose a clause. §B: the 10 overstatements.
   Then the fi192819 Nasibulin yes/no. After the answer, apply it.
   Then re-read the pairs that claims-and-evidence's re-run of
   measure.py/pairs.py touches; "link set final" was sent 12:11Z.
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
3. **backlog/se-nanobud-graph.md** — `status: draft/high`; re-scoped
   2026-10-02: geo rung, embed, registration and assembler mode shipped;
   generator, sublattice parity, chirality and nomenclature overtaken by
   hexfold. Left: a rigidity screen and a Cases A–D test of hexfold's
   `annot.sublattice`, both via the hexa session.
4. **backlog/nanobud-claim-remediation.md** — `status: in-progress/high`;
   brings the 139 claim hubs behind the nanobud draft above board — direct
   input to this thread's own claim-hub-signing end state. 2026-10-02 pass
   done (fi189536 grounded, dc2445930 cut); open = the non-empirical
   artifact type (`/go`), ref 2615, Phase 5 after Reto's findings check.

## Horizon

1. **nanopub approve/sign pass over dr173020's hubs** — waits on Reto's
   findings check (Do next 2; rewording after signing re-opens hubs); the 173020 batch in
   td345830–td345836.
2. **export + submission** — waits on 1 (td450082 done 2026-10-03 19:39Z). The
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
