---
status: idea
title: Citation matcher can attach a wrong title to a correctly-matched reference
---

# Citation matcher can attach a wrong title to a correctly-matched reference

## The defect

Discovered during a manual taproot reground pass over draft 173020, by two
independent scout agents that hit the same artifact separately — which is why
this is filed as a real defect rather than a one-off.

In paper `ref_id=783` (Torrens & Castellano 2014, *J Mol Model* 20:2263,
"Cluster solvation models of carbon nanostructures"), the ingested bibliography
entry for reference **[15]** carries author list, journal, volume and page range
that correctly identify **Krishnan, Dujardin, Treacy, Hugdahl, Lynum & Ebbesen
(1997), "Graphitic cones and the nucleation of curved carbon surfaces", Nature
388:451–454, DOI 10.1038/41284** — but its **title field reads "Photoisomerization
in dendrimers by harvesting of low-energy photons"**, an entirely unrelated
paper. The bibliography chunk is `pc64792`.

## Why it matters more than a cosmetic metadata wart

Reference [15] is the load-bearing citation for two taproot claim hubs
(fi189542, fi189543) whose only grounding is a proxy passage in ref 783 that
defers to [15] for the actual evidence. Resolving those hubs down to their true
primary depends on that bibliography entry being trustworthy. A wrong title
means:

- **Title-based dedup/lookup against Crossref or S2 will either miss or match
  the wrong record.**
- **An agent chasing the citation chain can be led to ingest an unrelated paper
  while believing it has found the primary.**
- **Any claim grounded through that chain inherits a silent provenance error.**

This is exactly the failure mode the taproot evidence graph exists to prevent.

## Scope is unknown and should be measured before designing a fix

One confirmed instance, found incidentally. The obvious first question is
whether this is a marker/GROBID parse slip local to this PDF's reference list,
or a systematic mismatch introduced when a parsed reference is reconciled
against an external metadata source. A cheap detector: for ingested bibliography
entries, cross-check the title against the DOI/volume/pages-derived record and
flag disagreements — the corrupted rows are self-inconsistent, so they are
mechanically findable without human review.

**Cross-check that raised confidence:** the same Krishnan 1997 reference is
cited correctly, with the right title, in at least five other papers'
bibliographies in the corpus. So the underlying reference data is fine in
general; the corruption is specific to this entry in this paper.
