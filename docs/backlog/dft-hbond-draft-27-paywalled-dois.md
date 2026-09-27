---
status: ready
title: dr448178 DFT-hbond draft — 27 paywalled sources to fetch by hand
prio: normal
---

# 27 papers to fetch by hand

**Status 2026-09-27:** Reto retrieved 26 of the 27 by hand into the ingest
inbox; they should land within the hour. `448244` (Boys–Bernardi 1970, Mol
Phys) is **not available** — pre-digital, no scan obtainable. That one stays
a permanent stub, so its citing chunk keeps a legacy `[pa448244]` cite and
must not be counted as pending. Expected end state: 41 of 42 sources fetched.

Sources cited by draft `dr448178` ("DFT Accuracy for Hydrogen Bonding") that
the OA fetch pass cannot get. All 27 have a DOI and are already `prio=1`;
every one has returned `no_oa_version`, 23 of them five or more times. There
is no open-access PDF to find — the pass is working correctly and reporting
that. Until each has text, it cannot be chunked, cannot host findings, and
its citing chunks keep legacy `[pa]` cites.

Provenance caveat: these refs were minted by a `DREAM:acquire` pass on
2026-09-24 with **fabricated author bylines** (Bader is credited on three
papers published after his 2012 death). Titles+DOIs below are the
Crossref-repaired values; treat any stored byline as suspect until
`paper_meta_enrich` has run against a real fetch.

## By publisher

ACS (16) — `10.1021/*`

| ref | year | DOI | title |
|---|---|---|---|
| 448184 | 2011 | `10.1021/ct2002946` | S66: A Well-balanced Database of Benchmark Interaction Energies |
| 448189 | 2011 | `10.1021/ct100469b` | Assessment of the Performance of DFT and DFT-D Methods |
| 448190 | 2017 | `10.1021/acs.jctc.6b01046` | Conventional and Explicitly Correlated ab Initio Benchmark Study |
| 448217 | 2014 | `10.1021/ct400149j` | Comparing Counterpoise-Corrected, Uncorrected, and Averaged Binding |
| 448218 | 1995 | `10.1021/j100024a016` | Characterization of C-H-O Hydrogen Bonds |
| 448222 | 2010 | `10.1021/ja100936w` | Revealing Non-Covalent Interactions |
| 448226 | 2001 | `10.1021/cr000021b` | Dihydrogen bonding: structures, energetics, and dynamics |
| 448227 | 1996 | `10.1021/ar950150s` | A new intermolecular interaction: unconventional hydrogen bonds |
| 448228 | 2004 | `10.1021/jp048562i` | Properties of the C−H···H Dihydrogen Bond |
| 448229 | 2000 | `10.1021/jp002302t` | The CO/Pt(111) puzzle |
| 448238 | 2012 | `10.1021/ct300647k` | Benchmark Calculations of Noncovalent Interactions of Halogenated |
| 448239 | 2020 | `10.1021/acs.jctc.9b01265` | Non-Covalent Interactions Atlas Benchmark Data Sets |
| 448240 | 2020 | `10.1021/acs.jctc.0c00715` | Non-Covalent Interactions Atlas Benchmark Data Sets 2 |
| 448241 | 2020 | `10.1021/acs.jpclett.0c02405` | Accurate and Numerically Efficient r2SCAN |
| 448245 | 1991 | `10.1021/cr00005a013` | A quantum theory of molecular structure and its applications |
| 448246 | 2011 | `10.1021/ct100641a` | NCIPLOT: a program for plotting non-covalent interaction regions |

Wiley (4) — `10.1002/*`

| ref | year | DOI | title |
|---|---|---|---|
| 448196 | 2011 | `10.1002/jcc.21759` | Effect of the damping function in dispersion corrected DFT |
| 448219 | 2018 | `10.1002/chem.201705163` | Why Bond Critical Points Are Not "Bond" Critical Points |
| 448220 | 2014 | `10.1002/chem.201402177` | Toward a consistent interpretation of the QTAIM |
| 448221 | 2006 | `10.1002/chem.200500850` | Hydrogen-hydrogen bonding in planar biphenyl |
| 448235 | 2018 | `10.1002/jcc.25532` | Bond paths between distant atoms do not necessarily indicate bonding |

AAAS / Science (2) — `10.1126/*`

| ref | year | DOI | title |
|---|---|---|---|
| 448224 | 2017 | `10.1126/science.aah5975` | Density functional theory is straying from the path |
| 448233 | 2013 | `10.1126/science.1242603` | Real-Space Identification of Intermolecular Bonding with AFM |

Other (4)

| ref | year | DOI | publisher | title |
|---|---|---|---|---|
| 448182 | 2006 | `10.1039/b600027d` | RSC | Benchmark database of accurate MP2/CCSD(T) CBS interaction energies |
| 448193 | 2015 | `10.1103/physrevlett.115.036402` | APS | SCAN meta-GGA — **also on arXiv: 1504.03028** |
| 448244 | 1970 | `10.1080/00268977000101561` | Taylor & Francis | Calculation of small molecular interactions (Boys–Bernardi) — **NOT AVAILABLE, permanent stub** |
| 448247 | 2009 | `10.1088/0953-8984/21/8/084204` | IOP | A grid-based Bader analysis algorithm without lattice bias |

## Bulk DOI list

```
10.1021/ct2002946
10.1021/ct100469b
10.1021/acs.jctc.6b01046
10.1021/ct400149j
10.1021/j100024a016
10.1021/ja100936w
10.1021/cr000021b
10.1021/ar950150s
10.1021/jp048562i
10.1021/jp002302t
10.1021/ct300647k
10.1021/acs.jctc.9b01265
10.1021/acs.jctc.0c00715
10.1021/acs.jpclett.0c02405
10.1021/cr00005a013
10.1021/ct100641a
10.1002/jcc.21759
10.1002/chem.201705163
10.1002/chem.201402177
10.1002/chem.200500850
10.1002/jcc.25532
10.1126/science.aah5975
10.1126/science.1242603
10.1039/b600027d
10.1103/physrevlett.115.036402
10.1080/00268977000101561
10.1088/0953-8984/21/8/084204
```

## Open question — 448193 has an arXiv id and still failed

`448193` (SCAN, PRL 115 036402) carries `arxiv:1504.03028` in
`ref_identifiers` yet still logged `no_oa_version`. arXiv is
unconditionally open, so either the fetch pass does not consult the arXiv
identifier on a `no_oa_version` fallback, or it consulted it and failed for
another reason. Worth a look independent of this draft — it would affect
every paper whose only OA route is a preprint.
