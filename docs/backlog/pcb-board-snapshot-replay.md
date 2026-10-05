# PCB board snapshot/replay — escape diagnosis prerequisite

Owner ewod-pcb. Authorised by Claude/Reto, 2026-10-05; escape-and-driver-chain
continues only on the dev/test copy. No new public operation or schema.

Premise: pcb_graph/load plus content_hash do not preserve a replay: the hash
intentionally excludes router state and copper; public pinout omits pad shapes.
No existing design clone was found. Reuse the stored relational rows and normal
Store readers, not generator re-expansion, provider fetching or authoring inference.

Internal version-1 JSON captures one design and its single physical board: component
and pin graph, instances/poses, nets/connections/classes, features, local/cached raw
footprints and catalog rows, canonical generator params/ledger, route sketches,
planes/swaps/measures, fixed copper including terminal claims, and derived copper.
Capture all design-scoped rows including retired rows so filtering remains faithful.
Reference title/meta are retained; unrelated refs, jobs, credentials and providers
are not copied. Latest persisted route-job parameters are evidence, not proof that
a reroute under a newer router recreates historical copper. Omitted seed/config
keys retain the worker's defaults (seed 0, default iterations, negotiation 0).
Record source revision separately from payload; no claim of historical router pin.

Exporter is one SELECT returning JSON, used through scripts/prod-psql --ro
(agent_ro and BEGIN READ ONLY). IDs are normalized per table in original row order;
foreign keys remap explicitly, timestamps irrelevant to routing are excluded.
JSON geometry/meta remain verbatim. Missing or external relational dependencies
refuse rather than silently drop. Loader refuses production databases, existing
slugs, unsupported versions/tables and conflicting shared cache rows. It inserts
in one transaction under a new slug, never invokes generators/jobs/providers and
never overwrites an existing design or cache. Re-export ignores target slug/IDs
and must equal the original payload, including the captured route configuration.

Acceptance: focused DB roundtrip covers every captured surface, rollback on cache
conflict and target refusal; the exact ewod-dogfood-6 snapshot copy must report
22 routed / 33 failed / 3 dangling via the existing route-status reader before
any reroute. Preserve raw pad dimensions/polygons and fixed terminal claims.
Reto subsequently confirmed dogfood data is public and dogfood boards may be routed/rebuilt on production (non-dogfood remains untouched). The exact snapshot is checked in as deterministic gzip JSON (245 KB) at tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz; optional PRECIS_PCB_REPLAY_FIXTURE overrides it. Resolved RealizeConfig and OptimizeConfig defaults are captured alongside explicit job params; dynamic constraints remain in the raw graph/state rows. Exporter/helper is internal scripts/pcb-snapshot, not a new public op. Latest source defaults are capture-time configuration, not attestation of the historical routing engine.
Then diagnose all 17 no_path failures on the copy, select the measured single
highest-conversion change, and add deterministic routed-count/legality regression
under pcb-escape-and-driver-chain. No speculative cause or routing-ready claim.
