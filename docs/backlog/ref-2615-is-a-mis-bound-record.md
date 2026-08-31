---
status: draft
title: ref 2615 binds a NanoBud paper's chunks to a mining-journal DOI
prio: high
---

# `ref 2615`: chunks and identity belong to two different papers

Found 2026-08-31 while sourcing an epistemic mode for **fi269509**.

The ref's identity says one thing:

```
title    Dynamic mechanical characteristics and application of constant
         resistance energy-absorbing supporting material
doi      10.1016/j.ijmst.2022.03.005   (Int. J. Mining Sci. & Tech., 2022)
cite_key wang22c
pub_id   7opgni
```

Its chunks say another. `pc279168`, `pc279174`, `pc279175`, `pc279177`
are the **supplementary information of an aerosol-CVD carbon-NanoBud
synthesis paper** — ferrocene reactor schematics, water-vapour
concentration series, fullerene-evaporation controls, and
"investigated with a field emission transmission electron microscope
(Philips CM200 FEG)". Nothing about energy-absorbing bolts.

## The smoking gun

The ref carries **two different `pdf_sha256` identifiers**:

```
ffbb7735fae873e55d7b0eae4459cc8294d6c13d9f5474044e36eeafab32e4fe
38689275060281104ef372b59ff8333d3cbc57ffd87e8136e682d2cedc5a717f
```

One ref, two PDFs. A second, unrelated document was folded into an
existing ref rather than minted as its own — so the chunks come from
one paper and the citable identity from the other.

## Why it matters

`fi269509` is a live claim hub whose only evidence edge points here. Any
citation it renders sends a reader to a mining-engineering paper for a
nanotube-growth result. The claim sentence is fine and its passage is
genuine; the **citation target** is wrong. Publishing the hub in this
state would emit a false citation, so this blocks fi269509's route to
`signed` even though it is now lint-clean.

Check whether other hubs cite ref 2615 before repairing.

## Open questions

1. Which ingest path folds a second PDF into an existing ref, and does it
   guard against a content mismatch? The two-`pdf_sha256` shape is
   mechanically detectable — a corpus sweep for refs with more than one
   would size the blast radius.
2. Is the mining paper or the NanoBud paper the "real" 2615? The chunks
   are NanoBud, so the cheapest repair is probably to re-point the
   identity at the NanoBud paper and re-ingest the mining paper
   separately — but that rewrites a `cite_key` other rows may reference.

## Related

`docs/backlog/nanobud-claim-remediation.md` — fi269509 is one of its
hubs. `docs/backlog/pdf-extraction-drops-micro-sign-in-units.md` is a
different defect in the same "the evidence is not what it claims to be"
family.
