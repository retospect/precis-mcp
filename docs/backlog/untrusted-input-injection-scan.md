# untrusted-input-injection-scan

## Residuals (from OPEN-ITEMS)

Slice 1 shipped (tier-0 regex gate at every cache-backed fetch + news_poll;
verdict in cache_state.meta['inject']; suspect-banner / high-withhold ladder
in CacheBackedHandler._render). Slices 2–4 stay open per the proposal:
corpus-wide tier-1/2 model worker (generalize
`src/precis/workers/inject_scan.py` past email; per-chunk verdicts,
claim-on-version-mismatch, braked retries, raise_alert on high); papers +
search-path enforcement (tier-0 at the Marker/markup db-writer — PDF
hidden-text layers are the target; gate snippets + renders on chunk verdict);
prompt-seam fencing (shared fence_untrusted at every corpus-text→prompt
seam). Until slice 2 lands, the high-withhold branch in _render is dormant
(tier-0 only emits suspect).
