---
status: draft
title: "nanobud tier-2 mining backlog — 10 ranked mintable candidates, marginals, and draft-integrity flags"
---

# Ten mintable candidates, already grounded

Surveyed 2026-08-17 by a read-only opus agent over the tier-2 nanobud papers;
rehomed here 2026-08-20 from `nanobud-nanopub-batch3.md` when that file was
compacted to its open decisions. Nothing here has been minted. Each entry
names the paper, the claim, and the grounding chunk, so each is a
`precis taproot mint` away once the sentence is written to canon.

This is the **inward** loop's remaining queue — claims a human found by
reading. It is complementary to evidence widening (`taproot-reground.md`),
which walks the corpus outward from hubs that already exist.

| # | paper | claim | grounding |
|---|---|---|---|
| 1 | pa4365 | configuration-selected metallicity: 0.12 eV type-II vs metallic I/III vs 0.18 eV embedded | pc550457 (alt pc550458) |
| 2 | pa4365 | 3.0 Å spacing threshold + mirror-symmetry-breaking mechanism | pc550459, citation-free |
| 3 | pa948 | ¹³C CSI/CSA localized at the attachment site — the paper's headline novelty, and an NMR handle | pc82612 (tighter alt pc82603) |
| 4 | pa948 | band gaps 0.57–0.76 eV vs pristine 1.82/0.81 eV | pc82597 |
| 5 | pa206485 | rise/fall 33.53/0.934 ms → 86.42/3.35 ms at Vgs −21 V — the missing speed axis | pc2901217 |
| 6 | pa39796 | fullerene Raman **suppressed** in the stacked composite | pc1260158 (non-covalent framing) |
| 7 | pa1797 | write actuation 0.457 THz, 1.6–1.65 V/nm, 5 ps | pc175726 / pc175740 ("free C₆₀" framing) |
| 8 | pa948 | charge transfer 0.032 → 0.72 C | pc82598 |
| 9 | pa206485 | detectivity 2.34e10 Jones | abstract pc2901189 only — the body has just the formula |
| 10 | pa39796 | pillared gallery height 2.3–3.4 nm | pc1260163 (non-covalent framing) |

**Candidate 4 is worth minting first.** It puts a live *disagreement* with
pa4365's 0.12/0.18 eV on the record — finite H-capped vs periodic models —
and the corpus currently holds zero hub↔hub disagreements of any kind. It is
the cheapest real test of the opposition machinery on genuine physics rather
than a synthetic case.

Candidates 6, 7 and 10 carry **non-covalent or "free C₆₀" framing**. Per the
standing scope rule (true nanobuds only; the boundary is all-carbon, not
covalency) each needs its scope stated explicitly at mint or it will read as
a nanobud claim it is not.

## Marginals — on file, not recommended

pa4365 (5,5)-host gaps pc550465 · pa948 bond lengths pc82599 · pa206485
532 nm selectivity pc2901206 · pa1797 K@C60 −0.96e endohedral pc175732 ·
pa1797 temperature-insensitive 9 ps switching pc175729/30 · pa39796 pore-size
shift pc1260167 · pa40723 anti-clustering −1.863 vs −1.030 eV pc1307817
(overlap risk with the own-work agent) · pa170590 functional benchmark
pc2409795 (skip).

## Draft-integrity flags — Reto's calls, unresolved

- **pa1638 reports NO hydrogen-storage measurement despite its title**
  (conclusion pc155443 is "further work"). `dr173020` must not cite it as
  H₂-storage evidence — only as synthesis route + FTIR bonding.
- **pa199068 / pa40723 / pa170590 are abstract+intro-only ingests.** Results
  are not in the store; re-ingest before mining them harder.
- **pa170590 calls a C20–C40 dimer a "nanobud"** — useful for the review's
  scope-definition section precisely because it is the boundary case.
- **pc175737 (pa1797) is OCR-corrupted beyond grounding.**
- **pa206485's stability sentence straddles a chunk boundary** — unmintable as
  it stands.

## One durable schema note

`chunks` and `refs` both soft-delete via **`retired_at`** (unified by
vocab-compaction Stage E — `refs` used to spell it `deleted_at`, a frequent
source of ad-hoc-query bugs; that quirk is gone). Ad-hoc chunk/ref queries
that omit a `retired_at IS NULL` filter still silently include retired rows.
