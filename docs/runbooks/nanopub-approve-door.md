# Nanopub approve door — pace batch submissions

**Symptom.** Rapid back-to-back `precis nanopub approve` (or `/nanopub` web
surface) POSTs hang the web process — every route wedges until restart.

**Rule.** Serialize approve submissions when working through a batch of
hubs; sleep ~6 s between POSTs.

**502 is not reliably a no-write.** `evidence/add` has landed its write
behind a 502 — verify before retrying, or you duplicate the edge. `approve`
fails closed: its gates run before any write, so a 502 there is safe to
retry.
