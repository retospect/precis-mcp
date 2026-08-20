---
status: draft
title: "nanobud draft (dr173020): batch-3 nanopub queue + remaining citation lifecycle"
model: sonnet
---

# Nanobud paper (dr173020) — batch-3 open queue

Scope rule (Reto): **true nanobuds only** — covalently bonded
fullerene–tube/graphene hybrids; no vdW/deposited/mixed systems in the
claim tree. Cited-finding count and re-approval state for this cohort are
owned by `claim-review-mechanism.md`'s worked-example section — point
there rather than restate (all `nanopub_publish` rows, including
fi191146, reset to `candidate` by the title/body reconciliation pass; the
sign-off queue below is a full re-review, not just the withheld edges).

## Sign-off queue

Withheld-edge sign-offs + re-sign for the full reviewed cohort (state
reset — see above). Nuance to eyeball at sign: fi211520/21 quotes say
"for bulk graphite" (bulk-equivalent units) while their titles say
monolayer graphene. Open keep-or-drop calls surfaced during batch review,
still Reto's: fi191307/fi191308 (near-duplicate hubs, both ground pa5887 —
design vs electronic-results); fi191323 (methodological-capability claim —
asserts about method, not nature); fi189526 (literature-scope claim —
about the literature, not nature).

## Seven blocked adjudications (Reto's call, each)

- **fi191169** — OEM-supply claim unsupported: paper says "prototyping
  phase with >30 customers," not OEM supply; no epistemic method.
- **fi191282** — source ref 3838 (Terzyk) has no DOI row; needs DOI
  backfill before a passage is mintable.
- **fi191286** — only grounding chunk is hearsay citing Mpourmpakis;
  needs the primary imported or the claim stays unminted.
- **fi191316** — live `contradicts` edge from fi192706 (claim-strength
  inflation: "will ultimately require" vs source's "could be used");
  adjudicate before approve.
- **fi189536** — evidence chunk is an SCC-DFTB adsorption study that
  never asserts bilayer-deposition/blending; only secondary review pa4801
  says so, secondhand.
- **fi189542** — sole chunk is an intro recap with a `[15]` marker + a
  live `contradicts` edge from ref 5828.
- **fi189549** — bibliographic meta-claim about a micro-review; no
  epistemic mode.

## Staged n1–n9 go/no-go

9 non-covalent own-result hubs staged outside the nanobud claim tree
(covalency-rejected papers), awaiting Reto's explicit go/no-go.
Scratchpad session `e26f279b`, `specs3/`: `mint-*.json` + `approve-*.json`
(hub=MINT-PENDING) + `submit3.sh` paced runner. Contents: pa39796 — n1
optimum 25±8% filling / n2 50% collapse / n3 GCMC LJ vdW-stacked; pa40723
— n4 Li energies −1.917…−2.642 eV / n5 no-clustering −1.863 vs −1.030 eV /
n6 metallic-character; pa170590 — n7 dihedral switch / n8 HOMO–LUMO / n9
α-β-μ. All titles carry explicit non-covalent/adsorptive/fullerene–
fullerene framing; pa40723 + pa170590 are grounded in own-abstract text
(first-page-only ingest — non-hearsay, but shallow; see re-ingest list).

## Un-cited weave-in: fi211848–54 / h1–h5

Minted and approved (`reviewed`) but cited nowhere in dr173020: fi211848–
54 (Tier-2 — pa4365, pa948, pa206485, pa1797) and h1–h5 (`specs4` —
pa4365, pa948). Need weaving into the electronic-structure/
characterization sections, including the pa948-vs-pa4365 band-gap
disagreement. Reto may retire fi211852/53 (pa206485 passed covalency
review only WITH caveat — spray-coated fullerenol clusters,
linker-mediated covalency cited to a secondary ref, "nanobud" appears
nowhere in-paper). (H1's original grounding hit the `<sup>N</sup>`
citation-marker-residue gate hole, fixed in `5d1ef498`.)

## dc-level scope collisions and suspects

Found pre-scope-rule, dc2445932 (energy-storage ¶):

- Koh et al. cited as "Li adsorption on graphene–fullerene nanobuds"
  [fi191280], but pa40723's own system is adsorptive C60-on-SWCNT
  (non-covalent; covalency is hearsay).
- Hernández Mendoza clause [fi191286] — "reinforcing the convergent
  picture that bud-site curvature favours gas adsorption across
  independent computational studies" overclaims: pa1638 is experimental
  with zero adsorption data (H2 deferred to future work); supportable
  only as synthesis route + FTIR bonding.
- Terzyk fullerene-intercalated nano-containers [fi191282] is non-covalent
  intercalation inside the nanobud narrative.

Suspected, not fixed (Reto review): dc2445908 uncited "soluble
derivatives" first sentence + downstream solubility thread; dc2445887
induction-plasma specifics have no corpus source; dc2445902 5-vs-6
heptagon distinction could be stated; dc2445888 −0.48 eV end unverified;
dc2445939 says B3LYP-D3 but pa3941 used CAM-B3LYP.

**pc436382 on dc2445855 — Reto's call**: confirmed wrong source
(biomass-catalysis review, secondhand Nobel mention). Drop the handle
(paragraph already cites Kroto + Novoselov/Geim) or name the intended
bibliography entry.

## Re-ingest list

pa199068, pa40723, pa170590 — abstract+intro-only ingests, results not in
store; re-ingest before mining either harder. pa1797's chunk pc175737 is
OCR-corrupted beyond grounding (separate defect, not an ingest depth
issue). pa1638 stays genuinely DOI-less (Sway-distributed congress paper,
`cite_key` tecnia22) — its passage (h11, mint-only, `candidate`) stays out
of the payload under the one-DOI rule; edge visible internally only.

## Mining queue

The ten ranked tier-2 mint candidates from the 2026-08-17 survey — each with
its grounding chunk — plus the marginals and the draft-integrity flags now
live in `nanobud-claim-mining-candidates.md`. None are minted.
