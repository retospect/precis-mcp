-- precis_se/0012_se_measure_datum.sql
--
-- `se_measures.datum` — which feature of the block a measure is declared
-- against (docs/backlog/se-datum-measure-eval.md, open item 10 of
-- multiscale-optimisation-method.md §4). A COLUMN, not a JSONB key in
-- `relation`, because the datum is addressable state: the `datums` view
-- answers "which measures hang off datum A" and a stale resolution is a
-- DRC finding — things a JSON key can neither index nor constrain.
-- NULL means the default: the block's pose frame (3-2-1 on a prismatic
-- envelope, axis + base face on a rotational primitive).
--
-- The stored text is the *declared* selector (`frame` · `port:<name>` ·
-- `face:<instance>.<tag>` · `axis:<instance>` · `face:largest` ·
-- `face:normal=<±x|±y|±z>` · `face:perp=assembly`) — predicate declares,
-- name pins. What a predicate resolved to is provenance, recorded per
-- evaluation by `precis_se.datums.evaluate_measure`, never persisted
-- here.

ALTER TABLE se_measures ADD COLUMN IF NOT EXISTS datum TEXT NULL;

COMMENT ON COLUMN se_measures.datum IS
    'Declared datum selector for the measure (precis_se.datums grammar); '
    'NULL = the block pose frame. Strict on shape at write, lenient on '
    'existence — an unresolvable selector is a read-time finding.';
