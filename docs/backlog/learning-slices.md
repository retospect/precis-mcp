---
status: draft
title: Bounded learning follow-ups
pillar: personal
---

# Bounded learning follow-ups

## Motivation / why

Turn the remaining work in [quest-learning-plan.md](quest-learning-plan.md)
into eight reviewable implementation slices. The private check-in foundation
does not record scored answers, estimate forgetting, order continuing study,
or deliver private preparation and audio. This document specifies those gaps;
it does not enable them. Product choices below are proposals for review, not
new rulings. Resolve each slice's blocking questions before marking it ready.

## Foundation and shared contract

**Land `feat/learning-foundation` first.** The source dependency is
`b41a89d93398693d98f1a47605bad109ab01019c`; the inspected remote branch also
contains the follow-up tracking update in `quest-learning-plan.md`. Inspection
used `git show origin/feat/learning-foundation:<path>` and the foundation
commit's stat/code, not an assumption that the foundation is on main.
Source availability is not proof of deployment or a passed privacy gate.

Assumed foundation seams, all read on that branch:

- `src/precis/server.py::_install_token_auth` validates personal bearer
  credentials and binds SDK session ownership; install/slice credentials do
  not identify a learner. Personal access requires the supported streamable
  HTTP session-owner capability.
- `src/precis/mcp_identity.py::resolve_user_token` and
  `src/precis/user_context.py::require_user` recheck a live stable
  `web_users.id` and token. `user_scope` resets request context on exit;
  revision attribution is separate from authorization.
- `src/precis/learning/__init__.py::LearningStore` stores only settings and
  append-only daily self-reports. `LearningStore._tx` sets transaction-local
  ownership for forced row policies; explicit owner predicates remain
  necessary. These policies trust the server and database operators, not an
  arbitrary client able to set the same database setting.
- `src/precis/handlers/learning.py::LearningHandler` supports private
  `/activity`, `/settings`, `/checkin` and `le<N>` addresses.
  `src/precis/runtime/learning.py::dispatch_learning` bypasses shared content
  logs, result caches and continuations, with a 3,500-byte response bound.
  `src/precis/learning_privacy.py::is_learning_call` recognizes malformed
  learning envelopes for the same redaction.
- `src/precis_web/routes/learning.py::index` and
  `LearningPrivacyMiddleware` provide authenticated activity/settings and
  no-store responses, including failures. Reuse this self-service surface
  and its Account link rather than adding roster-management UI.

**Proposed common MCP contract (Q1).** Extend the non-ref `kind='learning'`
through existing `get`, `put` and `edit` verbs. All addresses below except
the foundation addresses are **new**. Addresses select resources, never
authorize them. Define typed, per-address payload validators and `ArgsSpec`
keys; reject unknown fields and invalid enum values. Keep existing check-in
semantics. Do not repurpose the agent reasoning `plan` kind or quest mutation.
No new verb or public ref kind is needed.

Every new mutation carries an owner-scoped opaque `idempotency_key`; the same
key/payload returns its original result and a changed payload conflicts.
New preference/queue operations also require the revision they read;
stale writes conflict rather than silently undo another session. Evidence
corrections append. Existing `/checkin` and `/settings` calls remain valid
without additional required fields; new settings fields use the versioned
extension, and legacy edits touch only their original settings columns.
Validate all referenced private IDs against the same
owner before linking; missing and foreign IDs have identical refusals.
Return actionable `next=` hints without echoing private input.

Use bounded owner-only pages and authenticated opaque cursors for large
records/lists, including answers and transcripts; never fall back to shared
`more` storage. Until a private query exists, refuse that retrieval path.
No generic search, graph neighbors, counts, summaries, exports, embeddings,
Drive files or repo-dev memories may contain private learning payloads.
All supported reads, including filters and aggregates, scope before ranking.
Shared source identities remain shared; personal source selections and
annotations remain private. A private source excerpt also needs source
authorization: a learning grant does not authorize arbitrary corpus content.

Implementation changes include runtime learning help, the learning manual,
module rationale and affected tests. Update `docs/codebase.md` only for a
changed seam. Put module detail in module docstrings; do not grow the capped
handlers/web package docstrings. New verb kwargs/modes must be declared in
the tool signature and forwarded payload, with mode/kwarg parity tests;
never extend the `_ARGS_ONLY` exception ledger. Schema changes below are
described in words; implementation uses forward-only migrations.

## Order and bounds

The order below is topological, not a claim that independent work must wait.
Every row depends on the foundation. M means one subsystem plus its private
API/UI and tests; L means a storage/security or integration change needing
separate implementation and review checkpoints. These are scope estimates,
not elapsed-time promises.

| Order | Gripe | Recorded blockers | Conditional dependency |
|---|---|---|---|
| 1 | gr564500 | Foundation | gr556002 decision on delegated authority (Q2) |
| 2 | gr564501 | Foundation | gr564500 before background import/repair |
| 3 | gr564502 | gr564501 | Q4–Q6 before model/pilot/automation respectively |
| 4 | gr564503 | Foundation | gr564500 for learner-bound research agents |
| 5 | gr564504 | gr564501 | gr564502 for adaptive ordering; gr564503 only nominates |
| 6 | gr564505 | gr564500, gr564501, gr564504 | Delivery/privacy adapter gate and Q9 |
| 7 | gr564506 | gr564500, gr564501 | gr564504 supplies dated context later |
| 8 | gr564510 | gr564501, gr564502, gr564504 | Reviewed tasks and forecast promotion gate |

gr564500 explicitly blocks both gr564505 and gr564506. gr556002 remains an
open, broader access-control question; no slice here resolves it. The
learning-only paths may be built without declaring generic refs private.
Any use of generic private-source, card or transcript readers remains blocked
until that path enforces the required boundary or uses the dedicated adapter
described here.

## gr564500 — Scoped maintenance grants

**Scope.** Authorize one authenticated agent/job to read or maintain a bounded
subset of one learner's data. Validate owner, actor/job, operation, resource
subset, expiry and revocation on every call and again before committing a
write or publishing output. Recheck after external/model work. Audit owner and
actor separately. Child jobs require distinct, equal-or-narrower grants whose
expiry cannot exceed the parent's; revoking a parent invalidates descendants.

**Explicit non-goals.** General corpus ACLs, impersonation via `owner=`, giving
agents personal user tokens, unrestricted SQL/shell credentials, changing
install-token integrations, or implementing the gr556002 policy for all kinds.

**Existing code / target.** Extend
`src/precis/user_context.py::Principal`, `require_user` and `user_scope`,
`src/precis/server.py::_install_token_auth`,
`src/precis/tools/core.py::_ctx_user_scope`, and
`src/precis/runtime/learning.py::_dispatch_learning`. The present principal
has user/token identity only. `LearningStore._tx` scopes an owner, not an
operation or resource subset. Grant validation, job credential issuance and
resource-aware store authorization are **new**; owner-only RLS by itself
cannot constrain a grant to selected observations.

**Data model.** New grant records contain stable owner ID, independently
authenticated actor and job IDs, issuer, finite operation allowlist, typed
resource selectors, creation/expiry/revocation timestamps, optional parent
grant and credential digest. New private audit records contain grant,
owner/actor/job, operation, resource IDs, result and request ID, without answer
or prompt text. Never retain a reusable raw grant credential in generic jobs.

**MCP surface.** Proposed `put /grants` accepts actor/job target, operations,
resource selectors, expiry, reason and retry key; only the authenticated
owner may issue initially (Q2). `get /grants` and `/grants/<id>` return owned
metadata; `edit /grants/<id>` with `mode='replace', action='revoke'` is
idempotent. Scope expansion requires a fresh grant. Possession of a grant ID
in tool args grants nothing: transport authentication binds the credential
to the acting job. Jobs cannot issue grants or broaden their own selectors.

**Privacy boundary.** Carry a restricted request-local authorization object
through store and worker adapters; never turn a grant into an unrestricted
human principal. Ordinary delegated processes receive only the restricted
service interface. Reset context and release connections/artifacts on success,
exception and cancellation. Generic job readers expose status/IDs only;
private job results use the learning surface. Revoked or expired credentials
must also stop reads of cached results.

**Acceptance tests.** Two owners; forged owner/grant/job; expiry and revocation
between read and commit; allowed read versus denied correction; permitted
subset versus another record of the same owner; nested scope narrowing and
parent revocation; concurrent jobs with duplicate request IDs; disabled owner;
pooled connection/thread cleanup after errors/cancellation; redacted prompts,
logs and results. Repeat install/anonymous refusal and existing integration
tests. Test under a non-BYPASSRLS role as well as through the service; prove
delegated workers cannot obtain unrestricted database access.

**Size / blocks.** L: grant store, authentication/dispatch, resource checks and
restricted job adapter. Foundation and Q1–Q2 block implementation. Reto must
settle issuer/dispatcher authority and lifetime policy under gr556002; this
slice must not infer it from revision actors or existing slice credentials.

## gr564501 — Private concept and evidence ledger

**Scope.** Store observed answers/reviews separately from assessments and
rebuildable concept, relationship and task-class projections. Start with
owner-submitted, rubric-scored tasks and explicit source imports. A check-in,
read, generated explanation or listening event never becomes successful
recall. Background import/repair additionally requires gr564500.

**Explicit non-goals.** Forecasts, automatic grading/rerouting, a review
scheduler, shared mastery changes, or reconstructing raw reviews from aggregate
Anki statistics.

**Existing code / target.** Extend `LearningHandler` and private runtime;
reuse `LearningStore.checkin`'s retry/supersession pattern and
`LearningStore._tx`'s owner isolation. Its daily `learning_events` are constrained
to self-reports, so do not widen them into ambiguous scored events.
`src/precis/reading/mastery.py::run_mastery_pass` mutates shared concept state
and is not the projection builder.
`src/precis/anki/sync.py::read_precis_stats` and `read_all_cards` expose
aggregates, not chronological reviews. Observation, assessment, coverage and
projection stores/reducers are **new**.

**Data model / typed submission.** New immutable observations carry owner,
event/source identity, observed-at UTC, ingested-at UTC, task ID/version,
task class, concept IDs or explicitly exercised relationship IDs, context/cues,
answer, hints/attempts, confidence, optional measured duration and feedback
exposure. Keep raw source payload and source account/card/review identity in
private storage. New task/rubric versions retain prompt, scoring scale,
authorized source passages and difficulty provenance. Assessments reference
an observation and rubric version, score/outcome, grader/model/prompt version,
uncertainty and review/dispute state. Corrections append a reason and same-owner
superseded assessment/event ID; they never invent a new practice attempt.

Unique owner/source/event keys deduplicate imports. An exact retry returns
the original; different content with the same source identity conflicts for
explicit correction. Coverage records identify source, covered interval,
as-of watermark and whether evidence is raw, partial or a bootstrap snapshot.
Projection keys include owner, subject, task class, reducer version and input
watermark. Rebuild in observed-time/source-event order with stable tie-breaking;
evaluate corrections separately at their recorded availability time. Replace
overlapping bootstrap contributions when covered raw history arrives; if
coverage is unknown, select one representation and expose the limitation.

**MCP surface.** New `put /task-definitions` registers an owner-authored prompt,
task class, exercised subjects, authorized sources and versioned scoring
rubric before any answer. This supports manual probes independently of
gr564510's later generation/review workflow. New `put /observations`,
`/assessments`, `/corrections` accept the typed records above plus retry keys;
validate task/rubric/source references.
`get /observations/<id>`, `/observations`, `/state` and `/coverage` return owned
history/projections, with subject/task/source filters and private pagination.
The server stamps owner/actor/ingest time. Imported source claims require a
verified adapter or are labelled self-reported. No caller may label its own
grade reviewed merely by choosing a payload value (Q3/Q11).

**Privacy boundary.** New tables inherit owner predicates, forced policies and
same-owner composite references. Answers, confidence and misconceptions never
enter refs/chunks/embeddings or shared job artifacts. Keep only safe source
concept identities shared. Provide a private pilot-log view of these records;
an ordinary draft cannot serve as that log.

**Acceptance tests.** Deterministic replay after correction and out-of-order
imports; duplicate, overlapping snapshot/raw history and ambiguous coverage;
unknown cold starts versus known-but-old evidence; relationship evidence only
from a task exercising that relationship; no shared mastery mutation.
Two-owner direct/list/filter/count/result/log tests, forged cross-owner
references and actor cleanup. Failed attempts may count as engagement without
passing a rubric; unknown minutes remain unknown. Check-ins retain their
existing date semantics; imports retain original observation dates.

**Size / blocks.** L: typed ledger, private reads and deterministic projections.
Foundation and Q1/Q3 block schema/API work. gr564500 blocks background import
or repair, not the owner-operated ledger. gr556002 blocks any attempt to reuse
unprotected generic readers for private source context.

## gr564502 — Forgetting forecasts and calibration

**Scope.** A versioned deterministic estimator and chronological evaluation in
shadow mode, plus the private NO → NH₃ pilot. Forecast concept recall and
explanation/application/relationship tasks separately. Store each prediction
before its answer, with horizon, task/cue class, evidence watermark, model
version and uncertainty. Show predicted versus observed outcomes and challenge
feedback. Keep retention maintenance and new-learning targets distinct.

**Explicit non-goals.** Declaring calibration from fourteen days, training on
future events, updating Anki scheduling, inferring mastery from exposure, or
automatic route changes in this slice.

**Existing code / target.** `src/precis/reading/mastery.py::card_strength`
uses interval-derived strength without elapsed time; `concept_mastery` averages
shared cards. Neither supplies this estimator. Build on gr564501's **new**
observations/projections and the existing `LearningHandler`/private web page.
Forecasting, calibration and prediction stores are **new**.

**Estimator proposal (Q4).** First implement a finite, versioned posterior
over initial accessibility and positive half-life per owner/subject/task class,
with `p(t) = p0 * 2^(-elapsed / half_life)`. Use an observation likelihood that
records cueing, guessing, slips and grading uncertainty; explanation scores
remain ordinal with probabilities for 0/1/2, not a silent binary conversion.
Version the grid, prior weights, likelihood and post-practice transition table
together. Freeze their numeric values after review and before coding. No
automatic fit during replay. Missing evidence produces a broad labelled prior,
never perfect recall at elapsed zero; no propagation of observed mastery across
corpus edges. Compare the candidate with recency/last-interval baselines and
a stability-curve candidate on held-out later events before selecting a
production estimator. The recommendation is a starting experiment, not a
validated forgetting law.

**Data / MCP.** New immutable prediction rows and evaluation runs reference
exact evidence/model/policy versions; store uncertainty, scoring coverage and
per-task metrics. New `get /forecasts`, `/calibration` and `/predictions/<id>`
return bounded owned reports. New `put /predictions` requests a server-computed
pre-answer forecast; duplicate task-attempt prediction keys cannot overwrite
the original. Extend `edit /settings` with challenge preference and separate
maintenance/new-learning targets once Q4 is resolved. Outcomes/corrections use
gr564501; an LLM may assess a versioned rubric, never write model state directly.

**Pilot and promotion.** Verify the complete owner-only ledger first. Confirm
the month-end capability (Q5), then fix three baseline prompts and a 0–2
explanation rubric, with separate 0–2 confidence. Day-seven/day-fourteen probes
use matched new examples of material studied at least three days earlier.
Record predicted probabilities before answers. Report binary Brier score and
success bands separately from ordinal calibration. Chronological replay must
use only evidence/assessments available at prediction time. Repeated
easy-but-failed and hard-but-easy outcomes produce a visible model-review
recommendation and versioned correction, not an invisible state overwrite.

The **proposed** automation gate is Q6: reviewed chronological evaluation by
task class, adequate delayed evidence, baseline comparison, bounded calibration
error and an explicit promotion record for the exact model/policy versions.
Missing or disputed evidence fails closed. The default remains curated; a
promotion for terminology cannot authorize mechanism or relationship rerouting.

**Privacy / tests.** Forecasts and evaluation records inherit evidence ownership.
Test decay under fixed assumptions, uncertainty and cold starts, deterministic
replay, pre-answer persistence, correction without future leakage, separate
task classes/targets, both mismatch directions and foreign-owner isolation.
Read/check-in/audio events do not reset clocks. Gate tests cover insufficient
coverage, disputes, changed versions and revoked promotion.

**Size / blocks.** L: estimator/evaluator, private reports and instrumented pilot.
gr564501 and Q4 block model implementation; Q5 blocks the real pilot; Q6 blocks
automation. Manual owner operation does not require background grants.

## gr564503 — Private research activity and topic nomination

**Scope.** Capture new owner-bound access history with trusted origin/purpose;
nominate paper-backed NO → NH₃ topics with inspectable source passages. Keep
direct learner activity, authorized research-agent activity and shared project
context distinct. Provide pins/dismissals and explicit coverage/uncertainty.

**Explicit non-goals.** Backfilling personal history from shared counters,
inferring reading completion/mastery, observing unrelated browser activity,
or making popularity the queue's priority policy.

**Existing code / target.**
`src/precis/runtime/dispatch.py::DispatchMixin._bump_ref_read` records concrete
gets using actor/session debounce identity.
`src/precis/store/_refs_ops.py::RefsMixin.bump_ref_read_for` feeds per-ref
aggregate counters and last-reader stamps. Neither is a private longitudinal
source. Add **new** private telemetry and nomination reducers at authenticated
read boundaries; preserve the existing aggregate integration.

**Data / policy proposal (Q7).** Append owner, source ref/passage, observed UTC,
trusted session, actor/job, origin, purpose, project and idempotent request ID.
Record capture-start and missing intervals. Purpose comes from server-bound
request/job context, never a tag or arbitrary caller claim. Unknown purpose
is ineligible for personal ranking. Always exclude selector, card-preparation,
podcast and housekeeping reads; their nested source fetches retain that purpose.
Debounce to one contribution per source/session/day and cap a source's daily
contribution, so repeat days dominate bursts. Apply a versioned monotone age
decay; keep human and research-agent scores separately inspectable. Proposed
initial decay half-life: fourteen days, pending Q7. Nominees require an
authorized paper/passage chain, not merely a popular memory claim.

**MCP surface.** New `get /research-activity`, `/topics` and `/topics/<id>` show
coverage, provenance and ranking reasons. `edit /topics/<id>` accepts
`mode='replace'`, pin/dismiss/clear action, revision and retry key. Telemetry is
server-produced; no public `put` can assert that a human performed a read.
An explicitly submitted interest remains manual steering, labelled as such.

**Privacy boundary.** Own history is stored separately from `ref_reads` and
excluded from global analytics/search. Shared research can appear as labelled
project context only, never as someone's reconstructed access history. Agent
activity needs both authenticated research-job purpose and gr564500 scope;
until then support direct authenticated learner reads only. gr556002 remains
the boundary for any underlying private source retrieval.

**Acceptance tests.** Two users reading the same ref stay separate; shared
counter migration creates no personal rows; replay/idempotency; one-session
burst versus returns across days; deterministic time decay; maintenance and
recursive generator reads contribute zero; forged purpose cannot qualify;
missing history is visible. Every nominee exposes accessible paper provenance;
pins/dismissals survive replay. No learning observation/mastery update occurs.

**Size / blocks.** M: telemetry and candidate list, no queue scheduler.
Foundation and Q7 block personal nomination policy; gr564500 blocks the agent
source only. gr564501/gr564502/gr564504 are not prerequisites.

## gr564504 — Continuing importance-first queue

**Scope.** Persistent Continue Learning/Upcoming views with stable item IDs,
explicit progress, accepted next item, pins, snoozes, prerequisites and
preparation blockers. Support reading sections, probes, prerequisite sessions
and synthesis. Preserve unfinished work across sessions and missed days.

**Explicit non-goals.** Midnight resets, catch-up quotas, completion inferred
from opens, activating research quests, or treating thirty minutes as a cap.
No adaptive ordering until gr564502 promotion; gr564503 nominations are optional.

**Existing code / target.** Extend
`src/precis_web/routes/learning.py::_render` and `LearningHandler` beside the
existing `LearningStore.activity` calendar and settings.
`src/precis/reading/briefing_cast.py::_lane_reading` supplies recent-open context,
not explicit learner progress. Queue store, reducer, ordering and web actions
are **new**; consume gr564501 evidence without changing shared quest status.

**Data / ordering proposal (Q8).** New owner-scoped plan/goal versions, queue
items, prerequisite edges and append-only actions. Store subject/source sections,
importance/reason, curated order, estimated effort, progress marker, acceptance,
pin/snooze, optional planned date and readiness state. Derive current state
with revision checks. Keep preparation status distinct from learning completion.
Retain accepted work; a blocked pin stays visible while prerequisite repair is
offered immediately before it. Detect prerequisite cycles/missing sources and
show a blocker rather than silently completing or dropping an item.

For unaccepted work, apply prerequisite eligibility first, then explicit goal
importance and curated priority, then oldest outstanding item and stable ID.
Duration is display guidance only. This is the proposed deterministic starting
order; forecasts may recommend a different order in shadow mode, never apply
it. Source-access nominations cannot alter accepted work or create feedback
popularity. Preserve learner-selected upcoming items as well as the current one.

**MCP surface.** New `get /queue`, `/queue/<id>` and `/upcoming` expose reasons,
progress and blockers. `put /queue` creates typed items with prerequisites and
source targets. `edit /queue/<id>` with `mode='replace'` accepts
start/continue/pause/complete/skip/snooze/pin/unpin actions, progress, revision and
retry key. Reading completion requires explicit confirmation of covered
sections; partial work remains resumable. Extend settings with selected quest,
objective/priorities and paused state; no call writes the quest's execution
state. Activity uses evidence IDs to avoid counting an imported review twice.

**Privacy boundary.** Plans, priorities, progress and learning reasons use
private tables/views, not quest tags or public plan refs. Every item and edge
has same-owner checks; foreign prerequisite IDs reveal nothing. Calendar
engagement and memory estimates remain separate. Rest/blank days add no lapse.

**Acceptance tests.** Two-owner isolation and deterministic action replay;
two-minute and hour-long sessions resume the same work; suggested minutes
removed or exceeded; pause, timezone/midnight and missed-day transitions;
concurrent progress edits; stable accepted next item; explicit reading
completion; important long work before short low-value work; prerequisite
repair/cycle handling and a two-source synthesis item. Selecting a dormant
quest leaves its status/jobs untouched; rest does not penalize mastery.

**Size / blocks.** M: queue store/API and self-service views. gr564501 and Q8
block curated queue work. gr564502 gates adaptive ordering; gr564505 later
supplies delivery readiness. gr556002 matters only if an adapter proposes
unprotected private source access.

## gr564505 — Owner delivery and Anki preparation

**Scope.** Prepare necessary source-grounded terms for queued readings;
deduplicate against that learner's cards, reserve actual review-card slots and
confirm first availability in the intended Anki account. Start with at most
five new review cards per learner/day across the queue; zero is valid. Retain
Anki's scheduler and existing account safeguards.

**Explicit non-goals.** Using the global forge or shared mastery as personal
evidence, bulk corpus extraction, a second review scheduler, counting notes
as cards, or enabling live writes before privacy/delivery tests pass.

**Existing code / target.**
`src/precis/reading/flashcards.py::_cardless_concepts` deduplicates globally;
`_minted_today` counts concepts and `author_card` writes ordinary refs/chunks.
Use their source/card-quality work only through a **new** owner-scoped adapter.
`src/precis/workers/anki_sync.py::_sync_one_user` filters by login and locks
per account, but writes sync metadata before raising an aborted-sync error:
`anki_synced_at` is not a delivery receipt.
`src/precis/anki/sync.py::sync_tick` refuses FULL_UPLOAD and closes its mirror;
preserve both safeguards. `src/precis/anki/project.py::project_cards` creates
searchable generic refs; it is not a private review-history importer.

**Data / delivery.** New private preparation proposals reference owner, queue
item, concept, source passage/version and explanation. New account/card mappings
bind stable owner ID to verified legacy login/account identity and note GUID/card
IDs. New budget reservations count distinct cloze indices per note (c1 repeated
counts once; c1/c2/c3 counts three); other supported templates count actual
generated cards. Serialize per-owner/date reservation across jobs. Deduplicate
reuse/imports by owner/account/card identity before reserving. Keep pending
proposals outside published Anki refs until release. Unknown sync outcomes
retain reservations and trigger reconciliation, not a duplicate issue.

Proposed release rule (Q9): planned reading local date minus the selected
one/three calendar days, moved to the nearest earlier active study day if
needed. An undated item keeps proposals pending until the learner dates it.
Overdue work is merely eligible under today's allowance; it gains no missed-day
quota. At every sync dispatch, today's allowance subtracts confirmed new cards
today **and all outstanding unconfirmed cards**, including earlier reservations.
Carry unresolved reservations forward until reconciled, without refunding a
possibly sent card. Serialize this check with dispatch so delayed batches plus
new work cannot exceed five first deliveries in a day. Retries resend the same
GUIDs, never mint replacements. Active-day rules govern new releases; late
confirmation remains visible and never pretends delivery happened earlier.

New delivery receipts record verified account, note/card identity, content
version, first confirmed availability UTC and confirmation evidence. A local
note or attempted sync is insufficient. Confirm via the successful guarded
sync plus account-bound reconciliation; Q9 selects the exact receipt contract.
After timeouts, inspect the existing GUID in that account before retrying.
One/three-day readiness uses local calendar dates from first confirmed
availability; take the latest required card's readiness. Delayed/failed sync
moves readiness and stays visible. A changed lead affects unpublished work;
delivered cards keep their history. Timezone changes cannot grant a second
daily allowance; count already reserved/emitted cards conservatively.

**MCP surface.** New `get /preparation` and `/deliveries/<id>` expose owned
plans, card counts, workload, confirmation and blockers. `put /preparation`
requests preparation for owned queue IDs; jobs execute only under gr564500.
`edit /queue/<id>` gains explicit `read-anyway`, preserving the override and
actual preparation interval. Extend settings with one/three-day lead (proposed
default three), active study days and card cap (initial ceiling five).
Only a trusted sync adapter can record confirmed delivery; no public `put`
accepts a caller's assertion that another account received cards.

**Privacy boundary.** Resolve owner ID to the intended account at the adapter,
never through the single-account unowned-card fallback. Scope dedup, source
history, status and error payloads. No personal answers/misconceptions in shared
card refs or source explanations. Generic card access gaps remain gr556002;
the dedicated adapter must prove owner isolation before personal publication.
Keep released personal card bodies and learner-to-card mappings private too:
the adapter supplies sync inputs from owned storage and excludes these GUIDs
from generic `project_cards` imports, shared mastery and ref/search output.
Reusing existing account locks/sync mechanics does not authorize the ordinary
generic-ref inventory to expose personal cards.
Account for current review workload before issuing cards; unknown/excessive
workload offers zero new cards and an inspectable pause (Q9).

**Acceptance tests.** Forged foreign card/account/receipt refusals; two owners
prepare the same concept independently; frozen one/three-calendar-day clocks,
including delayed sync and timezone changes; multi-cloze counts; concurrent
reservations, retries, timeout reconciliation and import overlap; source
history and card reuse; no backlog burst after missed days. FULL_UPLOAD refusal
and collection cleanup survive. No card is sent before release, no receipt
follows failed sync, and `read-anyway` does not fabricate delivery or mastery.
Test planned-date/active-day release and a multi-day uncertain sync followed
by reconciliation: carried reservations prevent a delayed-plus-new burst.
Round-trip imported personal GUIDs never appear in shared refs or mastery.

**Size / blocks.** L: private preparation, budget ledger and confirmed-sync
adapter. gr564500, gr564501 and gr564504 are hard blockers; Q9 and the relevant
gr556002 boundary review block live personal writes.

## gr564506 — Private learning podcasts

**Scope.** Owner-bound episode manifests, transcripts, generation/render keys,
RSS and direct-media access. Morning uses curated upcoming material or supported
forecasts; evening revisits familiar concepts with supported connections.
Consume dated queue context when gr564504 is available. Podcasts remain extra
listening; linked submitted probes are focused study.

**Explicit non-goals.** Changing voices, schedules, durations, selected models
or integrations; recall prompts in nidra; downloaded-audio recall; treating
publication/download/listening as successful retrieval.

**Existing code / target.**
`src/precis/reading/cast_common.py::CAST_PROFILES` holds existing profiles;
`create_cast_draft` makes generic drafts/Drive entries and keys by cast/date.
`src/precis/reading/briefing_cast.py::build_reading_briefing` and
`src/precis/reading/meditation.py::_load` consume shared context; the latter uses
shared mastery. `src/precis/workers/cast_audio.py::narrate_cast_ref` renders
cast/date media. `src/precis/audio_feed.py::Episode` has no owner, while
`publish_episode`/`list_episodes` use a common directory.
`src/precis_web/routes/podcast.py::_require_listener` authenticates without
returning an owner; `feed` and `audio` then resolve shared episodes. Reuse
composition/render mechanics through **new** private context/storage adapters.

**Data / MCP.** New manifests hold stable owner, source session or curated
assignment, concepts/relationships, passage provenance, purpose, projection/
model versions, script/audio versions and planned/composed/published status.
Listening is a separate explicit exposure report. Private transcript/media
records use owner-inclusive composition and render keys, including cast,
assignment/date and relevant versions. Retry the same key without cross-owner
reuse. New `put /episodes` requests a scoped composition; `get /episodes`,
`/episodes/<id>` and `/episodes/<id>/transcript` return owned manifests and
paged transcript. An episode observation records exposure only; linked probe
answers deduplicate with the same gr564501 source event identity.

**Privacy boundary.** Resolve Basic/feed credentials to stable owner and active
account, then filter RSS and authorize direct MP3 requests before locating
media. Personalized output never enters generic drafts, Drive, shared render
caches or public episode directories. Scratch, descriptions, citations,
errors and model prompts inherit the owner and scoped job grant. Shared
episodes need explicit shared classification; unlabeled legacy files are not
automatically safe. Q10 determines legacy feed compatibility and client auth.
Generation grants are rechecked at composition/render/publication; revocation
prevents further grant-backed access. Listener credential revocation stops
future server fetches. Already downloaded bytes cannot be recalled.

**Acceptance tests.** Two owners with the same cast/date cannot receive each
other's RSS item, transcript or direct MP3 by ID; anonymous/auth-off access
fails for private media; revoked credentials/grants block future respective
access; retries/concurrent renders remain owner-specific; failures/cancellation
clean private scratch and never log payloads. Inspect shared caches and generic
draft/export paths for leakage. Preserve profiles and schedules. Nidra has no
recall prompts, unsupported connections stay excluded, and all passive delivery
events leave recall/mastery unchanged.

**Size / blocks.** L: private composition/render storage and listener delivery.
gr564500 and gr564501 block implementation; Q10 and relevant gr556002 source/
media decisions block personalized publication. gr564502/gr564504 are optional
context suppliers while curated context remains available.

## gr564510 — Reviewed adaptive tasks and route changes

**Scope.** Source-grounded terminology, mechanism/figure explanation,
connection/application and two-paper synthesis tasks; versioned grading,
learner corrections and prerequisite-route proposals. Preserve accepted next
work. Automatic changes to unaccepted work require the exact gr564502
model/policy promotion, supported task-class evidence and no active dispute.

**Explicit non-goals.** LLM-written permissions or model state, automatic
mastery certification, unreviewed prompts/grades driving routes, inferred
relationship competence from adjacency, or moving pinned work automatically.

**Existing code / target.** The existing `LearningHandler`/private web surface
is the entry point. gr564501 task/rubric/assessment records, gr564502 forecasts
and gr564504 queue are **new dependencies**, not features of the foundation.
Task generation/review, rubric evaluation and route-proposal reducer are
**new**. Shared concepts/papers supply authorized evidence, not learner state.

**Data / MCP.** Extend private task versions with prompt/model/source versions,
task class, exercised concepts/relationships, expected rubric evidence and
review status/reviewer identity. Store grades with uncertainty and disputes;
append learner corrections and re-evaluate projections without erasing the
original answer. New route proposals name supporting observations, prerequisite
item, before/after queue revisions, policy version and explanation.
Extend gr564501's manual task definitions with generated proposals.
New `put /tasks` requests proposals; `get /tasks/<id>` and `/route-proposals`
show reviews and reasons. New `put /task-reviews` records authorized review;
`put /corrections` retains the ledger contract. `edit /queue/<id>` accepts or
rejects a route proposal with revision/retry checks. A reviewer status sent by
an unqualified caller is refused.

**Reviewer/evaluation gate (Q11).** Proposed first release: human acceptance of
each task/rubric before delivery; deterministic scoring where possible, LLM
grades labelled provisional until accepted or corrected. Freeze an evaluation
set with correct, partial, incorrect and ambiguous answers for every task class,
including misleading citations and unsupported relationship claims. Require
source support, rubric coverage and no private-data leakage on every fixture;
record reviewer agreement and disagreements before enabling any automated
review. Repeated terminology failure should recommend definitions/examples;
mechanism failure should identify the missing causal step and supporting
source, with a prerequisite proposal only when evidence supports it. Synthesis
must compare two cited sources rather than concatenate their summaries.

**Privacy boundary.** Task prompts, answers, feedback and route evidence retain
the owner. A model receives only authorized context and cannot issue grants,
approve its own policy promotion or write access rules. Background generation
also needs gr564500; owner-interactive tasks use the authenticated principal.

**Acceptance tests.** Matched terminology/mechanism failures yield different
useful feedback; repeated confusion proposes a sourced prerequisite; grade and
route corrections replay without history loss; synthesis compares two sources;
two-owner direct/result/log isolation; unsupported or disputed evidence cannot
reroute; stale queue revisions conflict; pinned next work remains fixed.
Exercise disabled/uncalibrated promotion and policy-version changes, plus a
valid promoted change affecting only the unaccepted horizon.

**Size / blocks.** L: reviewed generation/feedback, evaluator and route proposals.
gr564501, gr564502 and gr564504 are hard blockers. Q6/Q11 block automatic route
changes; gr564500 additionally blocks background jobs. A reviewed manual route
proposal does not itself establish forecast calibration.

## Acceptance and handoff

Use synthetic accounts/answers until each exposed retrieval path passes its
two-owner privacy tests. In implementation, extend the foundation's learning
store/handler/dispatch, identity and web test families; add slice-specific
replay, policy and integration tests. Keep existing Anki, ref-read, cast and
podcast regression coverage. Source-only spec review checks anchors, dependency
order and consistency; it cannot prove delivery, privacy at runtime or
calibration. The coordinator runs implementation tests and lands changes.

This file stays draft for Reto's review. Keep open slices readable until their
implementation lands; remove completed scope then, preserving the remaining
dependencies. `docs/backlog/INDEX.md` is generated/gitignored, not hand-kept.

## Questions for Reto

1. **Q1 — Typed MCP and storage boundary (all slices).** Approve the proposed
   extension of `kind='learning'`, new private addresses, typed payloads and
   private pagination? Recommendation: keep dedicated owner-enforcing tables
   and `get`/`put`/`edit`; retain existing check-in semantics and refuse generic
   graph/search/export paths. Review this contract before schema/API coding.
2. **Q2 — Delegation authority and gr556002 (gr564500; downstream jobs).** Who
   may issue/renew grants, authenticate a job and delegate to children, and
   what lifetime is acceptable? Recommendation: initially owner-issued,
   one-job grants with explicit expiry; trusted dispatcher only narrows child
   grants, bounded by parent expiry; no automatic renewal or unrestricted
   maintenance principal. Select a maximum lifetime before implementation.
   Keep gr556002 open for generic ACLs and private source access.
3. **Q3 — Evidence schema and grading authority (gr564501).** Accept separate
   observations, assessments, coverage and projections, and which initial
   sources count as verified? Recommendation: owner-submitted rubric tasks
   first, labelled self-report/import provenance; verified Anki history only
   after inspecting the mirror's raw-review API. Never synthesize reviews from
   snapshots. Human-reviewed assessments initially; Q11 governs automation.
4. **Q4 — Estimator/priors and challenge policy (gr564502).** Which initial
   accessibility/half-life grid, observation likelihood and transition values
   should be frozen? Recommendation: review the proposed interpretable model
   using synthetic traces and broad task-specific priors before coding, then
   compare it chronologically with simple baselines. Keep maintenance floor
   separate from challenge preference; the plan's 80% challenge target is a
   pilot proposal, not an approved universal default or Anki setting.
5. **Q5 — NO → NH₃ capability and rubric (gr564502 pilot).** Confirm the
   month-end outcome and sources. Recommendation: explain competing mechanisms
   and key figures from the selected papers; retain the fixed 0–2 rubric and
   separate confidence, with baseline/day-seven/day-fourteen matched probes.
6. **Q6 — Promotion criteria (gr564502/gr564510; adaptive gr564504).** What
   evidence suffices for automation, and who promotes/revokes a model/policy?
   Recommendation: require owner approval of an evaluated version and review
   per task class. Proposed screening minimum: 30 delayed predictions per
   class across at least three elapsed-time bands with ten each, chronological
   performance no worse than both simple baselines, and absolute calibration
   error at most 0.10 in supported bands. Report uncertainty and ordinal results
   separately; these numbers are proposed review thresholds, not statistical
   proof. Insufficient precision/disputes keep shadow mode. Fourteen days alone
   never promotes; version changes or repeated mismatch suspend promotion.
7. **Q7 — Main versus sole discovery signal (gr564503).** May research-access
   history be the only passive interest input, and how should agent activity
   age? Recommendation: use it only for passive nomination, with separate human
   and research-agent evidence, one contribution/source/day and an initial
   fourteen-day decay half-life. Explicit steering, prerequisites and measured
   learning need remain independent controls. Choose history retention before
   collecting longitudinal activity; do not infer a retention duration.
8. **Q8 — Curated queue priority (gr564504).** Approve the proposed importance,
   prerequisite, age and stable-ID order? Recommendation: start with visible
   curated priorities and preserve current/accepted work; duration never ranks
   a trivial item above important work. Keep skip/snooze and prerequisite repair
   explicit; allow forecast recommendations without applying them.
9. **Q9 — Anki delivery/workload contract (gr564505).** What account-bound
   confirmation establishes availability, and what review workload pauses new
   cards? Recommendation: successful guarded sync plus reconciliation of the
   expected GUID/card IDs in that account; never trust `anki_synced_at` alone.
   Approve the exact verification API and active-study-day release rule before
   live writes. Recommendation: undated items wait for a learner-selected date;
   unresolved deliveries consume capacity until reconciled. Keep the initial
   five-card ceiling and proposed three-day default with one-day option;
   missing workload information pauses automatic creation. Choose a visible
   per-user workload threshold without turning it into a study-time cap.
10. **Q10 — Private feed and legacy episodes (gr564506).** Which authenticated
    podcast clients must remain supported, and how are old episodes classified?
    Recommendation: retain per-user feed credentials, resolve them to stable
    owner on RSS and media requests, and explicitly classify safe shared
    episodes. Do not silently expose unlabeled legacy audio or treat a generic
    feed credential as permission for all personalized episodes. Preserve
    existing voices/schedules/models; learning segments require owner selection.
11. **Q11 — Task/grade reviewer gate (gr564510).** Who accepts tasks and grades,
    and what evaluation permits automated review? Recommendation: human task/
    rubric review and provisional LLM grades first; freeze class-specific test
    cases and inspect disagreements before approving automated grading. Learner
    disputes suspend evidence-driven rerouting. Passing this task-quality gate
    does not substitute for Q6's forecast calibration gate.
