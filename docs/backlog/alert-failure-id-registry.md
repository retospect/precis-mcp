---
status: ready
title: stable, registered failure ids for alerts — enumerable rule catalogue + addressable get + agent-facing health panel
prio: normal
---

# Stable failure ids for alerts (the PCB-DRC property, applied to health)

## Motivation / why

On 2026-09-24 an agent was asked "is the embedder healthy?". It answered by
checking for a running process, found none, and reported
`DEGRADED — embedder completely offline`. The embedder was fine: prod had
produced 595 embeddings in the previous hour. The service idle-unloads its
model weights by design (`embedder_service.py`, `PRECIS_EMBEDDER_IDLE_S`),
so process-absence and healthy-idle are indistinguishable. Five agents and
~40 minutes produced a false verdict.

The capability to answer correctly already existed, and was better than what
the agent improvised:

* `health_digest.py` `_BACKLOG_CHECKS` carries
  `("embed", "discovery", 2.0, _WARN, "chunks embedded")` — an idle-aware
  throughput budget on exactly this question;
* `_diagnose_embed_pipeline` names the first stuck stage in the
  materializer → `embed_batch` → `job_inproc` chain when that check goes
  stale;
* `capability_probe` probes the embedder's `/readyz` every heartbeat, and
  that endpoint is already idle-aware (200 when loaded *or* idle, 503 only
  when warming/wedged);
* `/status` renders all of it for humans.

Nothing was missing. The agent could not *address* any of it, and nothing
told it to try. Three distinct defects:

**1. Failure identity is real but unregistered.** `raise_alert` dedups on
`(alert_source, fingerprint)` and the docstring states the contract:
*"`fingerprint` is the caller's stable identity for the condition"*
(`alerts.py`). An audit of all ~23 call sites found **zero** volatile
fingerprints — none embed a timestamp, count, duration or message string.
The property we want largely holds already. But the ids are *implicit*:
scattered across a dozen modules as local f-string conventions, with no
catalogue. You cannot enumerate what failures this system can emit without
reading every worker — so there is no per-id runbook, no suppression, no
documentation surface, and no test that the set is stable.

**2. Identity is derived, not pinned.** `health_digest.py` uses
`fp = c.name`. Renaming a check silently mints a new id *and* auto-resolves
the old one (its fingerprint vanishes from the live set, so
`resolve_stale_alerts` closes it). A pure refactor therefore reads as
"failure fixed, new failure appeared" when nothing changed. That is exactly
the determinism break stable ids exist to prevent, and it is the same
reasoning that made us adopt stable failure ids for pcb DRC.

**3. Agents cannot ask by id.** `open_alert_severity(store, source=…,
fingerprint=…)` exists in `alerts.py` but is Python-only. `AlertHandler`
accepts `/open`, `/recent` and a numeric ref id — there is no way to ask
*"is `nursery:dead-worker/dead-worker:melchior:job_inproc` open right now?"*
and get a deterministic yes/no. Agents can only browse `/open` and
pattern-match, which is how one ends up improvising a `ps aux` check.

## The model: rule id + subject

DRC's property is that a violation has a stable *rule* id, and an instance
is that rule plus a located subject. Alerts already have both halves; they
are just not named as such.

The source is itself derived at several call sites (`f"watchdog:{group}"`,
`f"nursery:{category}"`, `f"review:empty:{reviewer.name}"`), so the unit to
register is the **family**, written as a pattern with named subject
variables:

```
  rule id (registered, enumerable)   instance (rule + subject)
  nursery:dead-worker              → dead-worker:{host}:{process}
  watchdog:discovery               → embed
  disk_check                       → {host}:{path}
  quota_check:auth                 → {host}:claude-oauth
  budget:quota                     → quota-{window}
```

A canonical joined form already exists in the code — `health_digest.py`
does `source, _, fingerprint = condition.partition("/")` — so
`<source>/<fingerprint>` is the natural spelling, and `OracleHandler`
(`_parse_oracle_id`, `id_str.split("/", 1)`) is the precedent for a
slash-composite `get()` id. Sources and fingerprints use `:` internally, so
`/` is unambiguous.

**Recurrence is already modelled and merely unaggregated.** Dedup is among
*open* alerts only and resolved rows are retained, so a resolve → re-raise
mints a new row. The history of a failure id is therefore the set of rows
sharing `(alert_source, fingerprint)` — queryable today, surfaced nowhere.
This is what separates "flaky" from "fixed", and it is the payoff of
stability: a fix is verified when a specific id stops *opening*, not when
the current row happens to be closed.

## In scope

1. **A failure-id registry** (`src/precis/alert_ids.py`), modelled on
   `workers/registry.py`'s `ServiceSpec`: a frozen slotted dataclass in a
   module-level tuple plus a by-id dict. Per decision D2 the entry is:

   ```python
   @dataclass(frozen=True, slots=True)
   class AlertRule:
       rule: str            # key; source pattern — "nursery:dead-worker", "review:empty:{reviewer}"
       subject: str         # fingerprint pattern — "dead-worker:{host}:{process}", "" for a singleton
       one_line: str        # what condition it detects
       severity: str        # worst severity this rule raises
       service: str | None  # owning ServiceSpec.name; None for utils (admit, budget)
       triage: str = ""     # one line: first thing to check
   ```

   `service` is the runbook pointer *by proxy* — the read renders
   `SERVICES_BY_NAME[service].one_line` / `.doc_skill` / `.log_handler`,
   so there is no second pointer to rot (D5).
2. **A call-site test**, both directions, modelled on
   `tests/test_worker_registry.py` (`test_every_wired_pass_has_a_spec` /
   `test_every_ref_pass_spec_is_wired`): every `raise_alert` /
   `notify_critical_alert` site resolves to a registered rule, and every
   registered rule has a raise site. This is what turns a rename into a
   deliberate, reviewed id change. Note the test can only match the
   **constant or function** supplying `source=`, not the runtime f-string —
   `review.py` builds its source via `_empty_alert_source(reviewer)`, so
   `rule` must be allowed to be a pattern too.
2b. **Ban `/` in sources** — `raise_alert` has no source validation today.
   Add `if "/" in source: raise ValueError(...)` there; it is the single
   choke point every raiser passes through, and it is the invariant that
   makes the composite id round-trip (D1).
3. **Pin derived ids.** Replace `fp = c.name` in `health_digest` with an
   explicit registered id per check, so renaming a check is orthogonal to
   its failure identity. Same for any other derived-from-code-symbol id
   the audit turns up.
4. **`get(kind='alert', id='<source>/<fingerprint>')`** on `AlertHandler` —
   deterministic single-id read returning: open/resolved, severity,
   seen_count, first-seen, last-seen, and **recurrence count** (rows sharing
   the id). Answers "is X broken right now?" and "has X come back?" in one
   call. Follows the `OracleHandler` slash-parse precedent.
5. **A split aggregate health panel** in `health_checks.py`, plus an
   agent-facing read over it. **Not** a single synchronous all-checks call —
   measurement (D3) shows the three backlog aggregates cost ~29.5 s
   (inherent full-table scans over 3.97M `chunks`; no index helps), which is
   why `status.py` already quarantines them behind a lazy `GET
   /status/backlog`. So:
   * **backlog counts** come from a *snapshot*: `health_digest` already
     computes them hourly, so persist the result + `computed_at` via
     `app_settings` (the pattern it uses for `health_digest:last_push`) and
     have the aggregate read the row with its age. Both the web and MCP
     processes read one row; no cross-process cache. Hourly staleness is
     fine — the checks' own budgets are 6–24 h.
   * **freshness + activity** compute live, uncached (~0.5 s total).
   `/status/backlog` keeps computing live for a human who clicks it.
6. **`acknowledged`, stored as a TTL on a still-open row.** Today the only
   way to silence a known-failing id is `tag(add=['alert-state:resolved'])`,
   which asserts something false and is undone the next time the producer
   runs. Per D4 the ack is **not** a third state: the row keeps
   `alert-state:open` and `resolved_at IS NULL`, and an unindexed
   `meta.acked_until` (+ `acked_by`) is filtered by *readers*. Ack is
   orthogonal to state, so dedup keeps bumping the row and
   `resolve_stale_alerts` still auto-closes it when the condition clears.
7. **Skill routing**: one owning skill that says *health question → call
   this read first, never improvise*, plus a warning label that for
   lazily-loaded services process-absence ≠ down. Cross-link from
   `precis-status-help`, which currently teaches `ps aux` + bind-mount
   inspection as a liveness habit (correct for build-staleness, wrong as a
   liveness reflex).
8. **Fix the dispatch prompts** — the `cluster-ops` agent remit and our own
   health-check prompt templates encode the process-liveness methodology.
   A perfect surface does not help if the instructions point elsewhere.

## Explicitly NOT in scope

* **No new detectors or checks.** This is identity, addressability and
  routing over the existing nursery / health_digest / health_checks
  machinery. Coverage gaps are a separate question.
* **No new evaluator.** `health_checks.py` stays the single truth; item 5
  aggregates existing functions, it does not recompute anything.
* **No change to severity semantics** or to what pages vs. what digests —
  see `health-digest-degraded-severity-gate` for the adjacent open question
  about `degraded` being set by info-severity findings.
* **Not replacing `/status` or `/alerts`.** Both keep rendering; `/status`
  is refactored onto the aggregate function but its output is unchanged.
* **No notification/Discord changes.**
* **No migration of existing alert rows.** Ids are stable already in
  practice; the registry describes what is emitted, it does not rewrite
  history. (If item 3 changes a `health_digest` id string, that check's open
  alert closes and reopens once — acceptable, one-time, and noted at ship.)
* **Not embedding alerts** — they stay out of semantic search by design.

## Acceptance criteria

1. A registry module enumerates every rule id; the registry length matches
   the audited count, and each entry carries a description + default
   severity + subject-variable names.
2. A test fails if a `raise_alert` call site uses an unregistered rule id.
   Verified by adding an unregistered raise in a scratch branch and watching
   it go red.
3. `get(kind='alert', id='watchdog:discovery/embed')` returns state,
   seen_count, first/last seen and recurrence count for that id — on a
   healthy system it returns a well-formed "not open" answer rather than
   NotFound, because "is it broken?" must be answerable "no".
4. Renaming a `health_digest` check function/name does **not** change its
   emitted failure id (regression test for defect 2).
5. `/status` renders identically to today — its live sections on the new
   aggregate, `/status/backlog` still computing live. Confirmed by diffing
   a page render before/after.
6. An agent-facing health read returns the same per-subsystem states
   `/status` shows, including the embed backlog check (from the snapshot,
   with its age) and its `_diagnose_embed_pipeline` culprit string when
   stale. The read's own DB time stays under 500 ms p95.
7. Acking an alert silences it in `list_open_alerts`, the `/alerts` badge,
   `_check_alert_backlog_rot` and the remediation router, **without**
   setting `resolved_at` or removing `alert-state:open`; the next producer
   pass bumps `seen_count` as usual and does not re-page. A resolve →
   re-raise does *not* carry the ack forward (condition cleared and came
   back is new information) — assert this, don't "fix" it.
8. The owning skill exists and `precis-status-help` links to it; the
   `cluster-ops` remit no longer instructs process-liveness checks as the
   first move for "is X up".
9. **The original incident is replayable**: asked "is the embedder
   healthy?", an agent following the skills reaches the correct answer in
   one call, on a system where the embedder is idle-unloaded.

## Target + blast radius

* `src/precis/alerts.py` — registry import, validation on raise.
* `src/precis/alert_ids.py` — **new**, the catalogue.
* `src/precis/handlers/alert.py` — id parsing (dispatch ~111-140), new
  render path; `fingerprint` is already exposed in the detail view so the
  output shape is a superset of today's.
* `src/precis/health_checks.py` — new aggregate function; existing exported
  functions unchanged (other callers keep working).
* `src/precis_web/routes/status.py` — refactored onto the aggregate;
  `/status/backlog` already calls `compute_backlog_counts`.
* `src/precis/workers/health_digest.py` — pinned ids in place of `fp = c.name`
  (`_FRESHNESS_CHECKS`, `_BACKLOG_CHECKS`).
* `src/precis/workers/nursery.py` — registry constants for the 18 detector
  categories; fingerprint composition itself is already correct.
* ~20 further producer call sites (see audit) — registry constant swap only.
* `src/precis/data/skills/` — new routing skill + `precis-status-help` edit.
* `.claude/agents/cluster-ops.md` — remit wording.

Web `/alerts` and the Discord digest are read-through and should need no
change; verify at post-deploy.

## Audit — the id catalogue as it stands (registry seed)

~23 call sites, ~60 distinct (source, fingerprint-family) pairs. **No
volatile fingerprints found** — none embed timestamps, counts, durations or
message text.

| source (pattern) | fingerprint (pattern) | sev |
|---|---|---|
| `watchdog:{group}` | freshness/backlog check name — `papers_ingested`, `chunks_classified`, `news`, `morning_brief_cast`, `cast_audio`, `taproot_edges`, `embed`, `chunk_keywords`; **but** `watchdog:cadence` takes `{cadence}` / `{cadence}/never-seeded` and `watchdog:coherence` takes `{service}` — not all watchdog fingerprints are fixed names | per-check |
| `nursery:{category}` | `{category}:{ref_id}`, or explicit key | per-detector |
| `nanopub-demote` | `contradicted-frozen:{hub_ref_id}` | warn |
| `nanopub_mirror` | `concurrence:{artifact_code}:fi{claim_ref_id}` | info |
| `nanopub_ots` | `stuck-pending:{batch_id}` · `audit-mismatch` | warn · critical |
| `admit:oversize` | `{model}:{source}` | warn |
| `watch:elsevier_truncation` | `elsevier-truncation:{ref_id}` | critical |
| `ingest:marker-fallback` | `marker-fallback` | warn |
| `inject_scan` | `{account}:{folder}:{uidvalidity}:{uid}` | warn |
| `llm_reconcile:drift` | `proxy-missing:{model_id}` | warn |
| `quota_check:auth` | `{host}:claude-oauth` | critical |
| `review:empty:{reviewer}` | `{reviewer}:empty-pass` | warn |
| `review:tool-starved:{reviewer}` | `{reviewer}:tool-starved` | warn |
| `disk_check` | `{host}:{path}` | critical · warn |
| `fetch_oa:openalex_balance` | `openalex-content-credits-low` | warn |
| `orcid_enrich` | `orcid_enrich:missing_credentials` | warn |
| `scheduler` | `unschedulable:{ref_id}` | warn |
| `quest_tick` | `quest:dry-rest/{quest_id}` | warn |
| `budget:quota` · `budget` | `quota-{window}` · `cap-{window}` | critical |

`nursery` detector categories (18): `spin-loop`, `plan-tick-spin`,
`quest-loop-failing`, `orphaned-coordinator`, `orphan`, `stale-claim`,
`long-wait`, `stuck-doable`, `child-failed-parked`, `child-failed-final`,
`stalled-recurring`, `worker-restart`, `dead-worker`, `nas-denied`,
`host-dark`, `dispatch-stall`, `embed-lane-stalled`, `lane-skipping`.
Eight set an explicit `fingerprint_key` (`child-failed-final:aggregate`,
`worker-restart:{host}:{process}`, `dead-worker:{host}:{process}`,
`nas-denied:{host}`, `host-dark:{host}`, `dispatch-stall`,
`embed-lane-stalled`, `lane-skipping:{job_type}`) — these are the model the
rest should follow.

**`/` inside fingerprints is established prod convention, not an outlier.**
Beyond `quest_tick`'s `quest:dry-rest/{quest_id}` (no prod rows yet), the
`conditions.py` families carry several — `pass-dead:{host}/{process}/{handler}`,
`pass-wedged:{host}/{process}`, `llm-degraded:{model}/{transport}/{tier}` —
which surface as `watchdog:condition` fingerprints: **307 live prod rows**
(e.g. `llm-degraded:z-ai/glm-4.7-flash/openai_compat/cloud`). `disk_check`'s
default watch path is `"/"`, giving `caspar:/` (17 live rows), and
`watchdog:cadence` emits `{cadence}/never-seeded`. Latent sources of `/`:
catalog model ids (`deepseek/deepseek-v4-pro`) via `admit:oversize` and
`llm_reconcile:drift`, and IMAP folder names via `inject_scan`.

Reserving `/` would therefore churn ~325 live alerts across 4+ code sites.
Split-on-first is the only viable grammar — see D1.

## Decisions log

All five open questions resolved 2026-09-25 by investigation against the
code and prod. Three overturned the leaning they were filed with.

**D1 — id grammar: split on the FIRST `/` only** (`id.split("/", 1)`);
nothing is renamed. Filed as "quest_tick is the only offender"; that was
wrong — `/` in fingerprints is prod convention with ~325 live rows (see the
audit note above). The invariant that makes it round-trip is that *sources*
never contain `/`: 39 distinct `alert_source` values in prod, **zero rows
ever** with a slash. The repo already depends on this — `health_digest`
writes `f"{source}/{fingerprint}"` as a gripe marker and parses it back with
`partition("/")`, in prod, against those slash-bearing fingerprints.
*Implementer must:* add the `/`-in-source guard to `raise_alert` (item 2b);
never strip or normalise the fingerprint half (`caspar:/` legitimately ends
in a slash); reject an empty fingerprint; branch on `"/" in id` **before**
`_coerce_id` in the handler (the existing `id.startswith("/")` view-path
check cannot collide, since a composite never starts with `/`);
percent-encode if this shape ever lands in a URL path segment.

**D2 — registry granularity: one `subject` pattern string, not structured
variable names.** Filed leaning "declare the variables"; overturned because
nothing parses a fingerprint and nothing planned needs to — the new `get` is
string equality on `(alert_source, fingerprint)`, recurrence is the same
predicate over resolved rows, and the AST call-site test can only check the
`source=` constant. A `subject_vars` tuple would be a second field kept in
sync with the first for no consumer. The pattern string stays
machine-recoverable via `string.Formatter().parse` if a consumer ever
appears.

**D3 — caching: split the aggregate** (measured, prod, 2026-09-25). Backlog
aggregates are 6.5 s / 8.0 s / 14.9 s — Seq Scans over 3.97M `chunks`, no
index can help; `chunk_keywords` also pulls `text` for `length()`. Freshness
is 7 serial round-trips ≈310 ms (it cannot batch: each probe is
caller-supplied opaque SQL). `activity_by_handler` is **not** the expensive
one — 186 ms @7d / 16 ms @24h, already served by
`0124_worker_logs_handler_ts.sql`; the `worker_logs` index wanted by
`doctor-report-and-alert-channel-quality` is a different predicate. So:
backlog from the hourly `health_digest` snapshot via `app_settings`,
everything else live and uncached. *Revisit triggers:* aggregate DB time
>500 ms p95, or the MCP read sustained >10 calls/min, or `worker_logs`
handler rows past ~2M/7d. Cheap wins if needed: run `activity_by_handler`
on the shared conn instead of its own pool checkout, and replace the
`max(created_at)` probes with `ORDER BY <pk> DESC LIMIT 1` (~310 ms → ~30 ms;
check composite-pk semantics on `chunk_embeddings`/`chunk_summaries` first).

**D4 — ack: `meta.acked_until` TTL on a still-open row; no third state, no
migration.** Filed as "tag value vs new column"; both rejected. A third
`alert-state:` value falls between two predicates that disagree about
"open": `raise_alert` dedups on the **tag**, the unique index
`uq_alert_open_source_fingerprint` keys on the **column** (migration 0099).
An acked row would go invisible to dedup, so the next sighting either
(i) trips `_after_tag_mutation` → `sync_resolved_at_with_tags`, silently
stamping `resolved_at` and re-paging as a fresh `is_new` row — precisely the
failure this item exists to remove — or (ii) raises `UniqueViolation` inside
the nursery pass. Making it work means widening ~10 reader predicates and
teaching a two-state boolean three states, while growing the `alert-state:`
open namespace that `open-namespace-teardown` plans to remove. A real column
costs a migration + `store/types.py` + three mapper SELECT lists + baseline
regen, and buys nothing: no read path needs it indexed. *Implementer must:*
ack via `update_ref(meta_patch=…)`, never via `tag`; never add
`alert-state:acknowledged` to `_LIFECYCLE_TAGS`; keep `raise_alert` and
`resolve_stale_alerts` NOT filtering on the ack (acked rows still need dedup
and auto-resolve); apply the filter in `list_open_alerts`, the `nav` badge,
`_check_alert_backlog_rot` (else an acked alert >7d reads as response-loop
rot) and the remediation router (else it files a gripe for an acked
condition); show acked rows dimmed with their expiry on `/alerts` rather
than hidden; ensure no producer's `extra_meta` uses the key `acked_until`;
update `precis-alert-help` §Triage, which currently teaches
resolve-as-dismiss.

**D5 — no per-id runbook pointer; carry `service` + an optional `triage`
line instead.** Filed leaning "add the pointer"; overturned by precedent.
`ServiceSpec.doc_skill` is filled on 74/74 rows, has **zero consumers**
anywhere in src, **zero validation**, and has already decayed — `quota_check`
and `disk_check` both point at `precis-nursery-help`, which mentions
neither. A per-alert pointer would repeat that: the same slug for ~14 rows,
`precis-alert-help` for 2, and empty for ~40 of ~60. Runbook coverage today
is thin and group-level anyway (nursery documents 14 of 18 detectors;
~14 whole families have no skill text at all), and what exists says "what
fires it", not "what to check" — which is what the embedder incident
actually needed. `service` (test-validated against `SERVICES_BY_NAME`) gives
the read `.one_line`, `.doc_skill` and `.log_handler` for free, maintained
once. *Acceptance rule:* `triage` filled for every `critical` rule (~12 —
the six critical nursery categories, `disk_check`, `quota_check:auth`,
`budget` ×2, `watch:elsevier_truncation`, `nanopub_ots` audit-mismatch),
empty elsewhere. That line lives next to the code that raises it, and it is
the one that would have caught the incident.

Residual, non-blocking: `severity` is one value per rule while `disk_check`
raises critical-or-warn by threshold — record the worst and note the split
in `one_line` rather than adding a `severities` tuple.
