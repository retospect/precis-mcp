# nanobuds paper

**Status:** ends when dr173020 is submitted to a venue with every claim a
signed finding hub and its thesis (geometry, not composition, sets the
electronic structure, and almost none of it has been measured) leading the
paper. Today the poster is print-ready and the paper is a survey grown by
accretion with the thesis buried in a 233-word gap analysis; a taproot
backfill converted 13 chunks to hub cites and left two residues; both
measured-analogue papers (Huang 2011, Seiler 2024) are in as hubs and cited. Repair the
citation graph first, then restructure once the venue is picked.
**Last reviewed:** 2026-09-30
**Worktree:** `nanobuds-paper`

## Do next

1. **td458067** — six figure captions cite a claim hub where a
   reproduction-provenance pointer belongs (gr450329 code fix shipped, draft
   never repaired); every export names a claim as an image source and the
   six caption hubs are citable by other drafts. Chunk history is the only
   undo, so it gets harder with every edit.
2. **td458068** — fi449493 miscorroborated by three doping papers, fi189545 a
   third copy of the fi191132/fi211518 pair, fi449498 pinned to the wrong
   passage (gr450339 residue); wrong hubs propagate into any draft that
   searches them, and the bundled wording fixes are the only known
   unfaithful sentences in the paper.
3. **td450081** — target venue (Reto). Gates the restructure: Introduction is
   29% of the paper and the thesis sits near the end.
4. **td450082** — pa1181/42560 duplicate reference merge (Reto); the
   bibliography exports one Nature 1999 paper twice until done.
5. **td450083** — fi191281 verifier re-judge (Reto); the stale caveat argues
   against wording that no longer exists.
6. **td450087** — commit the poster README rewrite in its own gitignored
   repo (Reto); until then the README's owed-list is wrong on disk.

## Horizon

1. **read-back of the 13 chunks jo449492 converted** — waits on
   td458067 + td458068 so it sees repaired hubs; delivers a citation graph
   where every [fi] in dr173020 is faithful to its span, the gate before any
   prose moves.
2. **measured-analogue argument consolidated (dc2445946 + dc2445954)** —
   waits on 1; delivers the thesis as one body section on the Huang
   (fi457242–50) and Seiler (fi458136–44) hubs and re-decides the retired
   gap table dc2445955.
3. **restructure** — waits on td450081 (venue) and 2; delivers the
   Introduction cut from 29%, the thesis moved to the front, the abstract
   scope line drawn.
4. **nanopub approve/sign pass over dr173020's hubs** — waits on 3 (rewording
   after signing re-opens hubs); the 173020 batch in td345830–td345836.
5. **export + submission** — waits on 4 and td450082; docx/pdf via the local
   prod export path against the venue's template. 43020 stays frozen.

## Parked

- **abstract scope contradiction (dc2445850 vs dc2445883, covalent vs
  non-covalent buds)** — authors' scope decision; unparks with td450081,
  where the scope line gets drawn.
- **hexfold/se cross-cite of the seam-topology section** — unparks when the
  hexfold catalogue's measured rows are trusted (hexfold-toolkit item 6).

## No action needed

- **gr450329**, **gr450339** — code fixed and STATUS:done; the draft residue
  is td458067/td458068 above.
- **gr450123** — embedder contention; infra, owned elsewhere.
- **jo449492** — the backfill succeeded (189 scanned, 13 converted, 0
  failed); do not re-run over dr173020 before td458067 lands.
