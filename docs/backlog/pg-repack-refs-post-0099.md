# One-time deploy op: pg_repack refs after migration 0099

0099 set fillfactor=85 on `refs`, which only reaches existing pages on
rewrite. Run pg_repack (online, lock-light) on refs once post-deploy so the
now-unindexed-meta dedup updates land HOT in-page; new/updated rows adopt it
regardless. Ops, mechanical.
