---
status: idea
title: BEAST DB import adapter — grand-canonical electrocatalyst numbers as an external DFT source
---

# BEAST DB import adapter

Steal identified by the capability-landscape comparison (draft
`capability-landscape`, 2026-08): BEAST DB (beast-echem.org/beastdb,
paper doi:10.1021/acs.jpcc.4c06826, corpus pa254046) is a grand-canonical
DFT database of electrocatalyst properties — HER/OER/CO2R/**NRR** on 2,000+
catalysts, with explicit applied-potential and continuum-solvation effects.

Why it matters here: the NO→NH3 quest (qu164903) applies potential via the
closed-form CHE lever over MACE energies; BEAST DB carries *actual* GC-DFT
potential-dependent energetics for the same reaction family. As an external
evidence source it can sanity-check (or seed) catpath explorations at a
fidelity the quest never buys itself.

Shape: one more adapter in the ADR 0053 registry (`raw_record → (Scene,
ExternalRun, ExternalId)`), same rules as Catalysis-Hub — external runs
never serve compute cache hits, external designs refuse edit. Open
questions: bulk download format/licence (JPCC SI vs site API), and whether
per-potential rows map onto one ExternalRun or a family.
