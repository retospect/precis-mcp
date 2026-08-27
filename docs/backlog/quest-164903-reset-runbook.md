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

## Not-too-early assessment (asked 2026-08-27)

Halt + salvage + wipe: NOT too early — substrate deployed, engine trusted.
Full re-arm: slightly early; missing pieces are (a) estimate slice 3's
validate-against-knowns gate, (b) agentic tick (toolset now exists), and
(c) quest layer still never MINTS findings itself (rulings minted by hand
here; hypothesis-mint-from-tick is future work). Re-arm with the
coordinator tick is acceptable; agentic tick can land during the fresh
campaign.
