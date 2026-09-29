# Draft hub coverage — which cited papers are chunked, which sections have no finding hub

**When.** You are mining findings (`fi` hubs) for a large prod draft and need
the remaining work measured, not remembered. Stored counts go stale within a
day on a live draft; re-measure with the two queries below over
`scripts/prod-psql` (read-only; the `cluster-ops` agent returns a digest
instead of raw rows). Replace `<ref_id>` with the draft's numeric id
(`dr437628` → `437628`).

**1. Cited papers: how many are chunked and therefore mineable.** A cited
paper with zero body chunks (`ord >= 0`) cannot ground a hub; those are the
`want`/`no_oa_version` residue, not a mining gap.

```sql
WITH cited AS (
  SELECT DISTINCT (m.g)[1]::int AS pid
  FROM chunks p, regexp_matches(p.text, 'pa([0-9]{4,})', 'g') AS m(g)
  WHERE p.ref_id = <ref_id> AND p.retired_at IS NULL)
SELECT count(*) AS cited,
       count(*) FILTER (WHERE (SELECT count(*) FROM chunks c
                               WHERE c.ref_id = cited.pid AND c.ord >= 0) > 0) AS chunked
FROM cited;
```

**2. Sections that cite papers but carry no hub** — the actual gap list,
ordered by how many paragraphs cite a paper without a `[fi…]` handle.

```sql
SELECT h.chunk_id, left(h.text, 45),
       count(*) FILTER (WHERE p.text ~ 'pa[0-9]{4,}') AS pa_paras
FROM chunks h
JOIN chunks p ON p.parent_chunk_id = h.chunk_id
             AND p.chunk_kind = 'paragraph' AND p.retired_at IS NULL
WHERE h.ref_id = <ref_id> AND h.chunk_kind = 'heading' AND h.retired_at IS NULL
GROUP BY 1, 2
HAVING count(*) FILTER (WHERE p.text ~ 'pa[0-9]{4,}') > 0
   AND count(*) FILTER (WHERE p.text ~ 'fi[0-9]{5,}') = 0
ORDER BY 3 DESC;
```

**Reading the output.** Survey paragraphs of the "the holdings are…" kind
cite papers bibliographically and fail the finding admissibility test; they
show up in query 2 but are not gaps. For the bulk of the remaining chunked
papers route through the `taproot_backfill` cluster job rather than
hand-minting: under load from concurrent minters a hand mint degraded from
40 s to 20 min, and backfill spends cluster tokens rather than session
context. Mint mechanics and the per-claim loop: skills `precis-finding-help`
and `precis-taproot-mint-help`.
