---
status: in-progress
title: qu164903 halt + reset — salvage rulings, wipe logbook/dossier, re-arm on trusted substrate
prio: high
---

# qu164903 reset runbook (operator: this session + Reto)

Decision (Reto, 2026-08-27): halt the campaign, retire all previous
calculations, DELETE the logbook entries ("loopy things from the past"),
wipe/re-seed the dossier so it exemplifies the dialectic form. Substrate
now deployed fleet-wide: digest render + `view='logbook'`, tick handle
emission, STATUS:refuted lifecycle + red UI, `pw` step selectors,
relevance-ranked serving, per-step trust consumer (barrier/selectivity
split), site-symbolic `add_atom_site`, `estimate` kind slices 1–2,
autocatpath 0.18.0 everywhere.

## Ordered steps — deletion NEVER precedes salvage

1. **HALT — DONE 2026-08-27.** `tag(kind='quest', id=164903,
   add=['STATUS:dormant'])` applied; the loop reconciler self-rests the
   tick loop the moment a quest is non-active (`quest/loop.py` docstring).
   Reversible: re-tag `STATUS:active` re-arms.
2. **SALVAGE — COMPLETE 2026-08-27.** Two passes: 58 rulings (dossier tree
   + frontier + logbook→Aug-2), then a full tail drain (ALL 4,767 entries)
   adding 31 items + corrections. Artifacts:
   `~/precis-experiments/qu164903-reset/salvage-rulings-2026-08-27.md` (v1,
   inline-corrected) + `salvage-tail-addendum-2026-08-27.md` (items 59–94).
   Key corrections: rulings 15/32/33 invalidated; real noise floor ~1 eV
   same-crystal (not 0.111 eV); z=0.66 rule inverted (correct ≈0.46–0.47);
   live Ir lead 0.994 eV (addendum 79); umbrella qu202467 will cite this
   quest's findings (mint MUST precede wipe).
3. **MINT — APPROVED (Reto, 2026-08-27), graded policy:**
   (a) **established** = process/trust/geometry tier only (~15: rulings
   45–49, 52, 53, 55, 58 + addendum 62, 67, 73, 89–93) — claims that
   survive every barrier number being wrong;
   (b) **hypothesis** (`hypothesis=True` + `testable_by` = discriminating
   re-measurement under 0.18.0) = big-effect chemistry (~15–20: 13/14,
   1–9 qualitative, 74–78, narrow-44…) — never citable as evidence,
   upgraded per-claim by one trusted confirmation;
   (c) refuted pairs where a dead conjecture earns a do-not-repropose
   ledger entry (15-as-stated, 32, 33-mechanism);
   (d) **Ir lead = fresh campaign candidate #1** (unmeasured Ir-adatom
   barrier first); (e) inside-noise rulings + era-1 map (addendum 94) NOT
   minted — era-1 stays in soft-delete; (f) provenance (operator
   interventions, subsurface-H hint) → dossier settled-history section.
   Serve minted findings to the quest (`serves`). Pre-trust caveat goes IN
   the finding body ("qualitative; re-measure under 0.18.0 before citing
   the number"). ⚠ Gate risk: hypothesis mint historically requires ≥2
   motivators across ≥2 source papers — pilot-mint 2 items first, decide
   fallback (motivate from served papers / gate change) before batch.
4. **RETIRE CALCULATIONS.** Engine-version idem keys already re-key
   (0.18.0 pin bump) so nothing dedups onto stale jobs. Mark superseded:
   old pathway refs meta status (`superseded` mechanism already exists in
   precis_pathway/persist.py) — or simply leave; frontier repopulates from
   new measurements only after the dossier/serves reset. Decide with Reto
   whether old structure refs stay (linked history) or get pruned serves.
5. **DELETE LOGBOOK** (explicit operator override of append-only
   convention): delete qu164903's quest_log chunks (DELETE cascades
   embeddings — never UPDATE). Keep ref_events (the generic audit ledger,
   `view='log'`) — it is cheap and separate.
6. **WIPE + RE-SEED DOSSIER** in dialectic form: striving header; one
   section per LIVE hypothesis (hub statement — support handles w/ one
   why-clause — steelman counterargument — discriminating experiment with
   pre-registered BEP branch from `estimate`); settled-rulings section =
   links to the minted fi handles (one linked sentence each); open
   questions (the 3) as the live sections' seeds. Cite forms now
   available: [fi…] [ql…] [pw…~step] [es…] [st…].
7. **PRUNE + RETYPE SERVES**: drop off-topic serves (medical papers on a
   Pd-catalysis quest); keep ~30 load-bearing papers; minted findings
   served. Relevance-ranked serving handles the rest at tick time.
8. **RE-ARM**: re-tag `STATUS:active`. Recommendation: re-arm AFTER the
   estimate validation gate (slice 3: reproduce campaign knowns) or at
   least verify the first fresh tick's prompt renders the new handle-rich
   sections correctly (one manual tick eyeball).

## Status 2026-08-28 — hold lifted + executed; open: tick-4 review, re-arm

Done: logbook deleted (4,767→0, cascades verified, ref_events kept);
dialectic dossier installed verbatim (seed:
`~/precis-experiments/qu164903-reset/dossier-seed-dialectic.md`); 27
off-topic serves pruned (1,241→1,214; 25 finding serves intact). Mint
done earlier: fi263178 + fi263593–617 (minus 263613), 2 refuted-tagged.
Tick-zero replication DISPATCHED pre-hold: 50 valid jobs jb263259–263823
(5×10 structures, unique idem keys `tick0-<st>-r<n>`), results accrue on
job meta.partial; jb263279 invalid (transcription, replaced) — cancel
pending. GPU-slot token missing on MCP put → infra:child-killed attrition
expected; re-put same idem key staggered.

**HOLD LIFTED (Reto, 2026-08-27 late eve) — all held items executed:**
1. ✓ Ledger wipe: 290 live pinned chunks retired (1 ledger container +
   288 nodes + 2 stale frontier-tree containers, dossier ref 164905) via
   `retire_chunk` cascade; both containers ensure-recreate empty on the
   next tick. Verified 0 live pinned remain.
2. ✓ `meta.rubric_objectives` → `[span_at_Uopt, U_L_abs, energy, P_side]`
   all sense=min (the axes compute.py's trusted pipeline emits; U_L_abs
   per the "rubric minimizes |U_L|" contract). Closes the DATA half of
   gripe 263257 — comment there when verified on a live frontier read.
3. ✓ jb263279 was already terminal (`failed`) — no cancel needed.
4. ✓ Fleet deployed ed08d4a9 (all hosts green, 13m54s).
5. OBSERVED HAND TICKS RUN (2026-08-28 early, reason-only, local CLI
   against prod). Four attempts, three latent bugs found+fixed
   (SHIPPED 8e66df95 + deployed fleet-wide 2026-08-28):
   - Tick 1: died `error_max_budget_usd` — claude_p's $0.10 default can't
     fit a dialectic-dossier rewrite. FIX: tier-aware `_tick_llm_max_usd`
     (frontier $2.50 / big $1.50 / else $0.50, env
     `PRECIS_QUEST_TICK_MAX_USD` overrides all tiers).
   - Tick 2: "succeeded" as a SILENT NO-OP — claude_p's `_JSON_BLOCK_RE`
     matches ≤2-deep braces; site-symbolic proposals nest 5 deep, so
     `res.data` came back as the last shallow fragment and shadowed the
     text fallback. FIX: `raw_decode`-based scanner + `_PAYLOAD_KEYS`
     guard in `tick._payload_from_result`. Every armed tick would have
     no-op'd this way. Also found: the "Tried:" dedup line leaked
     pre-trust ≈numbers (model built a "disappeared leads" theory from
     them). FIX: provisional entries render name-only.
   - Tick 3: escalated to FRONTIER (2 dry ticks → "stalled"), opus died
     at the flat $0.50 → the tier map above.
   - Tick 4 SUCCEEDED (opus frontier review, $1.74): 5 logbook entries,
     dossier rewritten, 6 ledger nodes, 1 proposal (Ir-pair 2/9 ML
     coverage test, correct subsurface set_element, parent-linked,
     motivated by fi263612). Writing quality high: self-downgraded H1's
     unsourceable 0.994 headline, "trust-anchor before breadth" decision,
     caught the degenerate-NEB row, novel systemic finding (poison_margin
     negative on EVERY row; SO2 unscreened — pinned as ledger gap).
     Gates all engaged ([unverified model claim] prefix, [buildable]
     lead, review decision entry). CAVEAT: the rewrite flattened the
     seed's `###`-sectioned dialectic into prose paragraphs — content
     survived, form drifted; consider tightening the dossier-format
     prompt if the skeleton matters. searches_run=0 is by design
     (compute=False gates lit-search).
6. NEXT: Reto reviews tick 4's writing (dossier readback + raw payload
   sent in-session) → optional second observed tick with `--compute`
   (would dispatch the Ir-pair sim) → re-arm decision. Fixes must ship+
   deploy before re-arm (worker ticks run deployed code).
Reto's parallel queue: 24 hypothesis approve/sign payloads at
/claim/fi<id>; redlines on the seed dossier welcome (edits cheap).

## Not-too-early assessment (asked 2026-08-27)

Halt + salvage + wipe: NOT too early — substrate deployed, engine trusted.
Full re-arm: slightly early; missing pieces are (a) estimate slice 3's
validate-against-knowns gate, (b) agentic tick (toolset now exists), and
(c) quest layer still never MINTS findings itself (rulings minted by hand
here; hypothesis-mint-from-tick is future work). Re-arm with the
coordinator tick is acceptable; agentic tick can land during the fresh
campaign.
