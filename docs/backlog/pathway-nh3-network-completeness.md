---
status: draft
title: NO→NH3 screening runs a network that can reach NH3 by every known route and lose to N2 and N2O
pillar: 3d-design
prio: high
---

# NO→NH3 screening runs a network that can reach NH3 by every known route and lose to N2 and N2O

## Motivation / why

pw455722 (Pd, mace, screening tier) ran `network: branching` with the
parked template. That network is deliberately minimal
(catpath `network.py::build_branching_network`): NO dissociation,
oxidation, and NO+H → HNO / NOH, with no N–O cleavage after
hydrogenation and no N–N coupling. So every associative branch dead-ends
at NH₂OH, and the selectivity scorecard reads the hydrogenation of NO as
"leaving the target route" (margin −0.23 eV at `NO@N`). On Pd the
associative route to NH₃ and the N₂ / N₂O channels are both real, so the
screen is measured against the wrong competitors and favours catalysts
that split NO early. Every screening ranking feeding qu164903 inherits
this.

The full network exists: `network: ammonia` (catpath default;
`build_coadsorbed_ammonia_network`) has HNO → NH+O, NOH → N+OH,
N+NO → N₂O and N+N → N₂, with fragments coadsorbed. Missing everywhere:
NO+NO coupling ((NO)₂ → N₂O+O), the main N₂O channel at high NO
coverage.

## In scope

1. **Check what the parked `ammonia` template keeps.** Does
   `network: ammonia, template: parked` carry the associative N–O
   cleavage continuations and the N₂ / N₂O steps, or only the coadsorbed
   template? Record the answer in this file.
2. **Point NH₃ screening at the complete network.** The quest/tier
   config that dispatches screening runs for NO→NH₃ uses
   `network: ammonia` (parked if 1 says it is complete, else add the
   missing steps to the parked variant in catpath). The verify tier
   already uses coadsorbed.
3. **NO+NO coupling in catpath.** A dimer state (NO)₂ and the step
   (NO)₂ → N₂O + O*, with N₂O desorption, in the ammonia network.
4. **Required-leaf set.** N₂, N₂O and NH₂OH are declared side sinks so
   the span-over-required-leaves and the selectivity scorecard compare
   NH₃ against all of them.
5. **Re-run pw455722's config on the complete network** and record the
   new selectivity branch points beside the old ones (the delta is the
   size of the error the minimal network was making).
6. **Re-check pw455722's trap and poison verdicts** on the re-run. On the
   minimal network the scorecard's limiting factor is `trap`: `NO@par`
   needs 0.96 eV to escape against the best route's 0.38 eV span, and
   CO out-binds NO by 0.40 eV (`poison` margin −0.40). Either kills Pd
   regardless of selectivity. Record whether each survives the complete
   network in qu164903's logbook.

## Explicitly NOT in scope

- Barriers (NEB) for the new steps — `pathway-kinetics-promotion-gate.md`.
- HER as a competitor — `pathway-selectivity-u-ph-window.md` (pH routes).
- Other substrates/targets (nitrate, N₂ reduction).

## Acceptance criteria

- A preview (`mode='preview'`) of the screening config for NO→NH₃ on Pd
  lists HNO → NH+O, NOH → N+OH, an NH₂OH N–O cleavage, N+N → N₂,
  N+NO → N₂O and (NO)₂ → N₂O.
- Every hydrogenated intermediate has at least one route to NH₃ in the
  graph (test over the built network: no associative dead-end except the
  declared NH₂OH sink).
- The re-run of pw455722's config reports branch points that include an
  N₂ and an N₂O competitor.
- catpath tests cover the dimer step's stoichiometry.
- qu164903's logbook records pw455722's trap and poison verdicts on the
  complete network.

## Target + blast radius

catpath `network.py` (ammonia builder, dimer step), `config.py`
(template validity for `ammonia`); precis side: the quest/tier config
that sets `network:` for screening dispatch (`src/precis_pathway`,
qu164903's config). Catpath version bump + wheel redeploy. Screening
rankings on qu164903 shift once runs re-execute — note it in the quest
logbook.

## Open questions / decisions log

- Whether old screening candidates are re-run or only new ones use the
  complete network: default re-run the frontier only, as a cost cap.
