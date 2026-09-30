---
status: ready
pillar: memory-graph
title: dr448178 DFT-hbond draft — 27 paywalled sources to fetch by hand
prio: normal
---

# 27 papers to fetch by hand

**Status 2026-09-27:** Reto retrieved all 26 obtainable papers by hand into
the ingest inbox and they landed — **41 of 42 sources fetched**. `448244`
(Boys–Bernardi 1970, Mol Phys) is **not available** — pre-digital, no scan
obtainable. It stays a permanent stub, so its citing chunk keeps a legacy
`[pa448244]` cite and must not be counted as pending.

## Conversion result (jobs 453381 + 453483)

Two `taproot_backfill` passes ran on `dft-accuracy-hydrogen-bonding`. Pass 1
regrounded `[pa]`→`[pc]` (64 scanned, 27 converted, 0 failed); pass 2
promoted to `[fi]` hubs (64 scanned, 25 converted, 0 failed). Final cite
state over the draft's 64 body chunks — chunks can carry more than one
marker kind, so these do not sum to 64:

| marker | chunks |
|---|---|
| `[fi]` finding hubs | 24 |
| `[pc]` chunk-level grounding | 16 |
| `[pa]` legacy whole-paper | 14 |

Pass 2 outcome tally (82 cite-level outcomes):

| outcome | n | meaning |
|---|---|---|
| `new` | 43 | hub minted and cited |
| `reground` | 3 | matched an existing hub |
| `no-claim` | 17 | the draft's own passage yields no extractable claim |
| `reground-nomatch` | 12 | **the cited paper contains no supporting passage** |
| `stub-fetch-first` | 7 | supporter has no body text yet |

### The 12 `reground-nomatch` are a groundability signal, not a fabrication count

`reground-nomatch` (`taproot/backfill.py` `_plan_reground`) means the locate
step read the fetched paper and found nothing supporting the claim the draft
attributes to it. It fails safe: no rewrite, the prose keeps its `[pa]`, so
the weak cite stays visibly legacy rather than being laundered into a hub.

**Read on 2026-09-27; the first reading of them was wrong.** An earlier
revision of this file called the 12 "a floor on the fabrication" by analogy
to the fabricated bylines. That does not survive checking the papers. All 9
implicated refs are real papers with real body text (43–487 body chunks,
except `448225` at 8), so nothing here is an invented source. The 12 split
into three causes with different remedies:

1. **Evidence lives in a table → locate cannot ground it.** `dc4091165`
   cites `pa4737` (deep-learning XC functional, 2025) for "on WATER27 the
   same functionals spread from 0.77 to 7.27 kcal/mol". That paper *does*
   contain WATER27, S66 and the literal `7.27` — inside a markdown table
   with GGA / meta-GGA / Hybrid columns. The citation is correct; the
   reground step cannot match a claim to a table row. This is a mechanism
   limit, and the likely largest bucket.
2. **The draft's framing is an inference past the paper's sentence.**
   `dc4091164` cites `pa448237` for "the reference data itself carries about
   0.1 kcal/mol of uncertainty". The paper contains "0.1 kcal" three times
   but never the word "uncertainty" — a defensible paraphrase with no
   locatable supporting sentence. Needs a human call, not a fix.
3. **Genuine over-attribution.** `dc4089804` cites `pa448241` (r2SCAN) for
   "hydrogen-bond lengths within about 0.02 to 0.05 Å". That paper never
   mentions "hydrogen bond" at all and contains no "0.05". The r2SCAN paper
   is a general functional paper, not an H-bond geometry benchmark — the
   claim is attributed to a source that does not make it. **This is the
   bucket that actually needs correcting in the prose.**

So `reground-nomatch` is not a fabrication detector. It is a
*groundability* signal, and only cause 3 is a citation defect. Triage by
checking whether the cited paper contains the claim's numbers at all before
touching the prose — a cheap SQL `ILIKE` over the supporter's chunks
separates cause 3 from causes 1 and 2 in one query.

The 17 `no-claim` are a separate and more benign thing: prose too general to
host a falsifiable finding.

### Remaining work

1. **Fix the cause-3 cites** — only those are prose defects.
   `dc4089804`/`pa448241` is the one confirmed case. The other nine chunks
   carrying a nomatch (`dc4091164`, `dc4091165`, `dc4089529`, `dc4089530`,
   `dc4089716`, `dc4089728`, `dc4089730`, `dc4089814`, `dc4090673`) have
   **not** each been classified into cause 1/2/3. Run the
   `ILIKE`-over-supporter check per pair first; do not rewrite prose on the
   strength of the nomatch label alone.
2. **Table-bound evidence** (cause 1) is filed as `gr453835` — if a claim's
   support exists only in a table, no amount of re-running will ground it,
   so this needs the locate step fixed rather than per-draft workarounds.
2. **Re-extract 5 papers by hand — nothing will do it for you.** `448202`,
   `448204`, `448212`, `448230`, `448231` (the water/DFT cluster) have
   `fetch_ok` but **zero body chunks**, and no event records a failure.
   Until they have text, their cite runs return `stub-fetch-first`.

   An earlier revision of this file said they would chunk "once the markup
   backlog drains." That was wrong, and the mistake is worth keeping visible
   because it is the same shape as the rest of this file: a thing that had
   given up looked like a thing that was waiting. There is **no queue these
   refs are in**:

   - `markup-backfill` (`paper_hygiene.requeue_front_matter_only_papers`)
     cannot reach them. It requires "(b) between 1 and
     `_FRONT_MATTER_MAX_CHUNKS` body chunks" plus a `fetcher:elsevier`
     event — zero chunks is excluded by construction, and these are ACS/AIP.
   - `requeue_stranded_fetches` cannot reach them either: it gates on
     `pdf_sha256 IS NULL`, and these have a `pdf_sha256`.
   - `cli.stats._query_stubs` drops them from the stub backlog outright
     ("a stub that has a PDF is no longer a stub"), while
     `routes.status._paper_summary` counts them as `held`.

   So the 914 cluster-wide fetched-but-unchunked refs are **not a backlog**.
   Nothing claims them and nothing reports them. Re-ingest these five
   directly. Tracked as a gripe — see the fetched-but-no-body item.
3. **A third backfill pass** after those five have text. A fresh job
   re-scans every chunk (`done_chunk_ids` lives on the job ref, not the
   draft), so it is safe and cheap to re-run.
4. `448244`'s chunk keeps `[pa]` permanently.

Sources cited by draft `dr448178` ("DFT Accuracy for Hydrogen Bonding") that
the OA fetch pass could not get. All 27 have a DOI and were already
`prio=1`, and every one had returned `no_oa_version`. Note on counting: a
single cascade pass writes one event per leg (~5–14), so the raw
`no_oa_version` event count is **not** an attempt count — these refs had
roughly one to two passes each, not five to seven retries. Until a paper has
text it cannot be chunked, cannot host findings, and its citing chunks keep
legacy `[pa]` cites.

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

**Answered 2026-09-27 — arXiv *is* consulted, and the guess above was
wrong.** `_try_arxiv` runs unconditionally as leg 9 of 11 in `_run_cascade`
whenever `StubRef.arxiv` is set. What actually happened to `448193` in its
one pass (2026-09-24 12:52–12:57Z):

- unpaywall / openalex / europepmc / core → `no_oa_version` (correct, APS is
  closed).
- **`fetcher:arxiv` → `fetch_failed`**, `_ssl.c:993: The handshake operation
  timed out`. Transient on the fetch host, not arXiv: 19 handshake timeouts
  across 64 refs / 324 fetcher events in that two-hour window.
- `fetcher:openalex_content` → `no_oa_version` — the **last** event, and
  therefore the verdict `precis stubs` displays.

So two defects compound, and neither is "arXiv is not tried":

1. **The verdict is the latest event.** A paid-cache miss overwrote "a known
   green route exists and its download failed transiently." The
   human-facing list then asserts "no OA copy exists", which is false — this
   paper was one GET from free and was queued for hand purchase from APS.
2. **The backoff counts events, not passes.** `claim_stubs_to_fetch` derives
   `attempts` from `count(*)` over `fetcher:%` events; one cascade writes
   10–14 of them, so `24h * 2^(attempts-1)` hits the 720h cap on the first
   pass. A single transient TLS timeout therefore costs a 30-day wait. The
   docstring describes per-pass doubling; the SQL does per-event.

This is the same per-event-vs-per-pass defect that makes the whole 13,307
stub backlog unreadable — see the `no_oa_version` gripe, which reached it
from the opposite direction. Fixing that one query fixes both.
