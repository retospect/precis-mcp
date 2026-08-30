---
status: draft
title: A compound claim hub's posture ignores its conjuncts' evidence
prio: medium
---

# A compound hub reads as unsupported when every atom is supported

`nanopub/overview.py::hub_rows` computes `supported_count` /
`verified_count` from evidence edges pointing **at the hub itself**. A
compound hub carries none by construction — its evidence hangs off the
atoms it is `conjunct-of`-linked to (migration `0126`).

Worked example, prod 2026-08-29 — `fi211522` ("Graphene is the strongest
material ever measured (Young's modulus ~1 TPa, intrinsic strength
~130 GPa"):

| hub | evidence |
|---|---|
| `fi211522` (compound) | **none** — 3 inbound `conjunct-of`, 1 `cites` |
| `fi211519` (atom) | corroborated by `pc42017` |
| `fi211520` (atom) | corroborated by `pc42017` |
| `fi211521` (atom) | corroborated by `pc42017` |

So the compound reads "0 verdicts" while all three atoms are corroborated
by Lee et al. 2008 — the canonical graphene-strength measurement.

## Why it matters beyond display

- `handlers/finding.py::_passes_trust` resolves `'verified'` as
  `supported_count > 0 and not disputed`. **`trust='verified'` therefore
  hides every compound hub**, however well-supported its atoms.
- That lands directly on the design resolved in
  `claim-layer-absent-from-cross-kind-search.md`: the ranking lever there
  boosts verified-and-unopposed hubs. Compounds would never boost —
  the search would systematically rank a compound *below* its own atoms.
- Any "hubs with zero verdicts" audit over-counts. This is how `fi211522`
  entered the nanobud remediation cohort as an evidence gap when it is
  not one.

## Note the mint path already handles this correctly

Do not "fix" this by copying the mint logic — the two need different
answers. `gates.py::check_mint_order` (#15) requires every atom to carry
a *signed artifact* before the compound mints, and `gates.py:627` blocks
a compound whose atom has a live `contradicts`. Both walk
`bundle.conjunct_atoms`. The gate answers "may this compound mint?";
posture answers "how well-evidenced is this claim right now?" — a
compound whose atoms are corroborated-but-unsigned should read as
supported for search, yet still be mint-blocked.

## Options

1. **Roll up in `hub_rows`** — union the atom evidence when the hub has
   inbound `conjunct-of`. Truest, but changes an existing count's meaning;
   check every `supported_count` reader first.
2. **Add a separate derived column** (`atom_supported_count`) and teach
   `_passes_trust` to accept either. Additive, no silent redefinition.
3. **Render the rollup only in the claim-graph eye**, leave `trust=`
   alone. Cheapest, but leaves the search-ranking hole open.

Prefer 2 unless a reader audit shows 1 is safe.

## Related

`docs/backlog/contradicts-conflates-evidence-and-prose-misuse.md` — the
other place a hub's posture misreports what is actually known about it.
