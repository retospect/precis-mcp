# 0068 — Per-topic `classify_topics` gating + enabled-set marker backfill

- **Status**: accepted (implemented together with this ADR)
- **Deciders**: Reto + agent
- **Amends**: [ADR 0060 — topic dossiers](./0060-topic-dossiers.md) — refines its single `TOPICCASCADE:<version>` marker and single `classify_topics` toggle.

## Context

`classify_topics` (ADR 0060) runs one multi-label pass over the curated topic taxonomy in `src/precis/data/topics/*.yaml`, writing `topic:<slug>` open tags plus a `TOPICCASCADE:<version>` done-marker. All topics shared ONE `service_config` service (`classify_topics`) — a single toggle on `/categorizers` flipped every topic at once; an operator could not enable just one topic. The only backfill lever was hand-bumping `CLASSIFY_TOPICS_VERSION`.

## Decision

### 1. Per-topic gating, one pass

Each topic gets its own `service_config` service named `topic:<slug>`. These are consulted INSIDE the single `classify_topics` pass to filter the topic list fed to tier-0/tier-1 down to the enabled subset. Topics do NOT register their own worker passes — one pass still does one multi-label LLM call per paper (N passes would mean N calls/paper). This mirrors the `axis:<id>` service-gating pattern, minus per-axis pass registration.

### 2. Enabled-set marker = backfill mechanism

The done-marker value becomes `TOPICCASCADE:<version>-<hash>` where `<hash>` is a short stable digest (blake2b, 4-byte) of the sorted, de-duplicated enabled-slug set. A change to the enabled set changes the marker value, so the claim predicate (`NOT EXISTS ... value = <current marker>`) re-claims the whole corpus lazily and the newly-enabled topic gets a chance to tag already-processed papers. Reverting to a prior enabled set yields the prior marker value, so papers already carrying it are skipped.

### 3. Pass gate = "any topic enabled"

`classify_topics` remains a global kill-switch. The pass keeps `service_name = "classify_topics"`; its gate `default_on` is computed as "any `topic:<slug>` enabled". With no explicit `classify_topics` row the pass runs iff >=1 topic is on; an explicit `prio 0` row force-kills all topic classification globally, `prio >=1` force-runs — the standard `service_config` override contract. No separate master toggle row in the UI.

### 4. No schema migration

`service_config` rows and marker values are data.

## Consequences

- Toggling ANY topic re-sweeps the whole corpus lazily (new marker value), and the `/categorizers` coverage "done" count resets after a toggle — both correct; cheap while the pass is capacity-gated.
- Disabling a topic stops new tagging but does NOT retract that topic's historical `topic:<slug>` tags.
- The marker-value function `topic_marker_value(enabled_slugs)` is the single source of truth, shared by the worker (`workers/classify_topics.py`) and the web route (`src/precis_web/routes/categorizers.py`) so the coverage count matches what the worker claims against.
- `PRECIS_TOPICS_ENABLED` (csv of slugs) seeds per-topic default_on at deploy time, mirroring `PRECIS_AXES_ENABLED`; a live `service_config` row always wins.
