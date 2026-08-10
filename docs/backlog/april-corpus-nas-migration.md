# April-era paper corpus never migrated to the NAS canonical corpus

5,335 PDFs + extracts from `caspar:/opt/nfs/shared/data/papers` (citekey
layout) predate the NAS canonical corpus and were never merged into it —
only 253/5,335 match byte-identical on NAS. Five files appear truncated
vs. their archived originals (same citekey, different size/hash):
`x/xu2025research.pdf` (250KB NAS vs 11.5MB archived),
`s/sharma2018electronic.pdf`, `a/apatru2019design.pdf`,
`l/li2023electrocatalytic.pdf`, `j/jiao2020when.pdf`. The April tree is
archived to
`/opt/nas/botshome/papers/archive-caspar-2026-04/tree/` with a checksum
ledger (`papers-verify2.log`).

Follow-ups: decide merge/re-ingest the archived PDFs into the canonical
corpus vs. leave as cold archive; identify + repair the five truncated
copies from the archive.

Promoted from gr194396.
