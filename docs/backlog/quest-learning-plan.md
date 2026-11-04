---
status: idea
pillar: personal
---

# Quest guided learning and personal memory model

Build a personal model of remembered concepts, connections and likely
forgetting, then use it to choose useful refresh tasks and new challenges.
Use a continuing, importance-ordered queue: one quest provides the direction,
and useful concepts reach Anki before their paper. Thirty minutes is an
adjustable starting session estimate, not a fixed daily budget or stopping rule.
The focused-study estimate is configurable in the Users tab; the existing morning
and evening podcasts are extra listening integrated with the same model.
This is a proposal
for a two-week instrumented pilot and the infrastructure to support it, extending
[reading-prep-loop.md](reading-prep-loop.md), which remains the design of record
for concept extraction, prerequisites and card quality.

Reto requested a paper a day, unfamiliar terms as Anki cards one or three days
ahead, and optional diagnostic quizzes. The starting estimate is 30 minutes
of focused study including reviews; podcasts are extra listening. The later
clarification makes continuity and importance primary over a fixed time budget.
The initial quest and month-end capability are still
to be chosen. No schedule, extraction pass or Anki delivery is enabled by this
planning change.

## Personal memory model

The central product is a digital memory twin: an evolving, uncertain estimate
of the learner's accessible concepts and connections. Paper selection, refresh
tasks and Anki cards are consumers of that estimate. Reto's clarification makes
this a foundation of the plan, replacing the earlier proposal to defer richer
learner state. A corpus concept graph describes the subject; a personal
projection estimates which parts this learner can use in particular contexts.

### What the twin estimates

| State | Evidence and meaning |
|---|---|
| Recall and stability | Probability of recalling a concept at a specified future time and cue level; how that accessibility changes between observations. |
| Understanding and application | Evidence that the learner can explain, predict or use the concept beyond a rehearsed card. Keep task-specific estimates rather than one mastered flag. |
| Connections | Evidence that the learner can explain or use a particular prerequisite, contrast, causal or analogy relationship. A corpus edge alone is not evidence that this connection exists for this person. |
| Uncertainty and provenance | Evidence quantity, age, context, source and model/rubric version. Missing observations mean unknown, not forgotten or never learned. |

Predictions also condition on task difficulty, cueing and scoring reliability.
Keep those separate from learner state: an ambiguous question or uncertain
automated grade must not be diagnosed as forgetting. Store prompt/difficulty
and rubric versions alongside the answer, and support learner corrections.

For example, remembering a definition of adsorption does not establish the
ability to explain how surface coverage affects a reaction rate. A successful
definition card strengthens the relevant recall estimate; a mechanism question
probes the connection. Passing one does not silently certify the other.

Relationship estimates refer to shared, source-grounded relationships plus any
learner-specific misconception or proposed connection. Record evidence for an
edge only from a task that actually exercises it. Do not propagate certainty
through the prerequisite graph, average a person's state with another's, or
infer mastery from having written/read a document. Neighboring concepts can
supply weak, labelled priors; observed performance is needed to strengthen them.

### Observations and decay

Store append-only learning observations and rebuildable personal projections.
A minimally useful observation names the owner, timestamp, concept/relationship,
task and cue type, response/outcome, hints/attempts, rubric/source version and
feedback exposure. Confidence and response time are secondary signals, not
substitutes for correctness. Assistant-written explanations and a reader-open
event are exposure, not a successful learner retrieval. Corrections append
superseding evidence; they do not erase the original attempt.

Anki contributes owner-scoped review events and, where available, memory-state
estimates with their algorithm/version. Existing aggregate `anki_stats`
snapshots are useful bootstrap evidence but cannot reconstruct every review.
Inspect the local mirror's supported review-history API before implementation;
ingest events idempotently by owner and source event identity. A reread of the
same snapshot or review must not count as a new practice event. Preserve the
learner's selected Anki scheduler and parameters.

Bootstrap records need explicit source-history coverage and an as-of watermark.
When raw history later covers the same practice, rebuild from an earlier prior
using that history, replacing the overlapping bootstrap projection. Do not
apply the raw events on top of the posterior they already produced. Imported
FSRS states, interval snapshots and their underlying reviews are alternative
representations of overlapping evidence, not independent practice. If coverage
cannot be established, use one representation and expose the limitation rather
than combining them. Raw source records remain available for replay/audit.

Use an interpretable time-aware retention model as the first baseline. One
candidate is `R(t) = 2^(-elapsed / half_life)`, with an uncertain half-life for
each learner/concept/task class and pooled priors when personal data is sparse.
[Half-life regression](https://aclanthology.org/P16-1174/) supplies a trainable
recall-decay baseline; its language-learning results do not validate concept
transfer or the graph model proposed here. Compare such a baseline with a
stability-based curve on later observations before choosing the production
estimator. This proposal does not select fitted coefficients or a universal
forgetting law.

The curve models accessibility conditional on a learned memory trace. It must
not assign a newly seen concept perfect recall at elapsed zero: combine it
with an uncertain initial learning estimate and an observation model that
allows guessing, slips and hints. A lapse updates competing explanations
(forgetting, missing understanding, confusing cue), rather than erasing the
concept or declaring all prerequisites unlearned. Recall can fall with time
while explanation/application evidence remains distinct.

Keep the last observation time and estimator version; compute forecasts for
"now" and the planned reading date on demand. When evidence is sparse or old,
show the uncertainty and use a short diagnostic rather than a precise-looking
personal score. A textbook's or another user's curve is only a prior. The
twin is a predictive learner model, not a claim to have measured a person's
internal memory directly.

### Refresh and challenge policy

Treat retention maintenance and learning challenge as different decisions.
For an important concept, forecast when recall will cross the chosen retention
floor, then select a useful retrieval task in the importance-ordered queue and
before the concept is needed. New learning can instead target tasks that are
challenging but answerable. A tentative pilot setting is an 80% predicted
success target for short learning probes; this is a tunable policy choice, not
a research-established optimum or the required Anki retention setting.

If repeated tasks are easy, raise the application/connection demand or move to
a new concept. If repeated tasks are hard, distinguish missing prerequisites,
uncertain grading and forgetting before adding explanation or reducing
difficulty. If predictions repeatedly disagree with outcomes, recalibrate the
model as well as changing the next task. The learner's "too easy / about right /
too hard" feedback changes preferred challenge, separately from correctness.

Rank work by learning/retention importance, quest relevance, prerequisite
leverage and uncertainty reduction. Show estimated minutes as guidance, not a
hard cap or a reason to put low-value short tasks first. Put small calibration
probes into the same queue, including occasional probes of supposedly known
concepts, so the system can discover overconfident estimates. Do not schedule
every node indefinitely; show maintenance workload and let the learner choose
priorities and time available. Retain evidence
when a topic is parked and continue to forecast its decay.

Anki keeps its card-level due dates. The higher layer chooses concepts,
connection questions and papers; it must not run an independent scheduler for
the same cards. Recognize an Anki review and its imported observation as the
same event. If concept-level work overlaps a due card, account for that overlap
in workload instead of duplicating the task. Show the reason for an assignment:
"likely fading", "uncertain connection", or "next useful challenge".

### Calibration and acceptance

Store the predicted outcome, horizon, uncertainty and model version before the
learner answers. Later compare the prediction with the actual outcome, before
updating the model. For binary probes, use Brier score (mean squared probability
error) and predicted-versus-observed success bands; for explanation rubrics,
retain ordinal outcomes rather than silently treating all partial answers as
success. Test retention, application and relationship probes separately.

Use chronological replay/held-out later events, preserving the state available
at each prediction; repeated reviews from the future cannot leak into a past
forecast. Compare against simple recency/last-interval baselines. The fourteen-
day pilot starts collecting calibration evidence; it cannot establish reliable
individual forgetting curves or broad model superiority by itself. Until
evidence supports automated adaptation, show forecasts in shadow mode and
use the curated route with conservative, visible estimates.

Shadow mode updates forecasts and displays task recommendations for the
learner to select; it never changes the accepted route automatically. The
calibration-to-automation criteria must be specified and reviewed before any
autonomous task/route policy is enabled. Manual recommendations can be checked
during the pilot without claiming that this automation gate has passed.

Model-level checks must show that time without observation lowers predicted
recall under the same assumptions, one card cannot certify an entire concept
web, unknown differs from known-but-fading, repeated source events do not
strengthen memory twice, another user's activity changes nothing, and a scoring
correction can rebuild the state. End-to-end acceptance includes both failure
directions: tasks predicted easy that are repeatedly failed, and tasks predicted
challenging that are repeatedly easy. Both must change subsequent forecasts
and recommended task choices while respecting stated priorities and any
learner-selected session limits. A suggested duration alone is not a limit. Automatic
changes to the unaccepted horizon remain subject to the automation gate.

[Knowledge tracing](https://link.springer.com/article/10.1007/BF01099821)
supports estimating changing skill knowledge from task performance.
[Anki's FSRS documentation](https://docs.ankiweb.net/manual/deck-options#fsrs)
provides the card-level distinction between recall probability and memory
stability. Combining these with personal relationship evidence is the proposed
précis design; it needs its own validation.

## Privacy and agent execution

Personal learning memory is owner-only from its first real use. Answers,
review history, concept/connection estimates, misconceptions, study choices,
summaries, embeddings and job outputs are private to that owner. Shared source
material and safe canonical concepts can remain shared; a personal association,
private source excerpt or learner answer must not be promoted into shared
concept text, tags or graph links.

Use the stable `web_users.id` for new learning records and grants. Login and
abbreviation are display/lookup handles, not durable authorization keys.
Existing `refs.owner_login` integrations resolve through the account row at
the adapter boundary. Renaming an account must not transfer or lose its memory.

### Identity and access

The server derives the user from the authenticated web/MCP credential.
An `owner` or username in a prompt/tool argument selects a requested target;
it does not authorize access. An unidentified caller, stale account, expired
grant or failed identity lookup is refused, with a next step to bind identity.
A general install token or generic `agent` actor is not permission to see
personal learning memory.

This is stricter than today's intermediate multi-user behavior. At the
verified baseline, `src/precis/profiles.py::visible_to` lets an unidentified
caller pass; `src/precis/acting_user.py` is write attribution, not a complete
network user-authentication boundary.
[mcp-session-user-identity.md](mcp-session-user-identity.md) supplies the
proposed user-binding dependency, while
[multiuser-separation.md](multiuser-separation.md) records broader remaining
gaps. Do not claim existing `memory` or `draft` refs are private simply
because an owner label is present.

Every learning read/write path enforces ownership: direct handles, lists,
semantic search, counts, graph neighbors, summaries, exports, caches and job
results. Scope retrieval before ranking and aggregation. Private derived data
inherits the source owner; unknown-owner rows are quarantined, not public.
A foreign handle must not reveal whether a hidden record exists. Context and
connection-pool reuse must not carry one user's identity into another request.

Use owner-enforcing store queries and a database role/policy boundary that
cannot be bypassed by ordinary learning jobs. Exact row-policy implementation
is a schema checkpoint; metadata ownership alone does not satisfy acceptance.
Unrestricted database credentials or general shell access must not be handed
to delegated memory agents. Database operators remain a privileged trust
boundary; this proposal does not claim end-to-end encryption from operators.

A readable memory summary is an owner-scoped derived view of the structured
learner records. Do not publish it or the pilot log through existing generic
`memory`, `draft`, graph or job readers until their entire access path
enforces the same policy. The earlier suggestion of a learner-owned draft is
superseded by a protected study log. No real personal answers are collected
until that log and all its retrieval paths pass the privacy checks.

### An agent repairing user X's records

The authenticated requester must be X or a maintenance principal with an
explicit grant for this operation. An authorized dispatcher creates a job
bound to X's stable user ID, permitted operations, resource scope and expiry.
The agent is the actor; X is the data owner. Record both, plus the request/job
identity and reason, in the audit trail.

The agent receives a restricted MCP context for that job. The server checks
its grant on every call, including after revocation; a prompt saying "act as
Y" or an `owner=Y` argument cannot widen it. Child tasks inherit an equal or
narrower scope and separate audit identities. Background jobs carry explicit
request-local ownership; a shared process-wide current user is insufficient.

For example, fixing an incorrectly graded answer retrieves only X's answer,
rubric and necessary authorized source context. The LLM can propose a revised
assessment. The server validates it, appends a correction linked to the
original event, and rebuilds X's projection. Correcting an estimate cannot
fabricate a successful learning attempt or assert that X now understands the
concept. The repair leaves another user's state untouched.

Agent prompts, intermediate artifacts and outputs inherit X's scope. Give
the configured LLM only the authorized context needed for the task. Ordinary
user isolation does not imply secrecy from the selected model provider;
respect existing model/integration choices and any configured data-routing
policy. Shared operational logs carry identifiers/status rather than private
answers or prompts.

### Deterministic operations and LLM judgment

MCP is the tool interface; an MCP operation may invoke an LLM. Define the
boundary by behavior rather than treating every MCP call as deterministic.

| Responsibility | Execution and authority |
|---|---|
| Identify caller and authorize owner/resource/operation | Server code and scoped credentials; never an LLM decision. |
| Read/search private records, append observations/corrections, enforce audit and deduplication | Deterministic validated tools and owner-enforcing store/database policies. |
| Compute decay forecasts, update projections, measure calibration and apply priority/readiness/card-generation policies | Versioned statistical/numerical model and policy code. With fixed evidence/parameters, inference is reproducible; training/model changes are versioned. |
| Extract candidate concepts and relationships, propose cards/questions/papers, explain answers | LLM proposals grounded in authorized sources; typed validation and source/quality checks before storing or acting. |
| Score an objective answer | Deterministic scorer where the task supports one. |
| Interpret a free-text explanation or ambiguous misconception | LLM-assisted assessment with rubric, model version, uncertainty and correction path; stored as an assessment, not an observed fact about the learner. |
| Commit a repair or change the study route | Authorized validated tool operation under the recorded policy and automation gate. An LLM proposal cannot expand permission or bypass the gate. |

Keep raw observations separate from interpretations. A submitted answer is an
observation; a model's judgment of it is versioned evidence with provenance.
The statistical learner model consumes accepted evidence and reports
uncertainty. Changing the grading LLM must not silently rewrite history or
claim that the user learned something new.

Privacy acceptance requires two-user concurrent tests across all exposed
paths, anonymous/expired/revoked-grant refusals, forged-owner arguments,
child-task scope, pooled connections, derived outputs and summary retrieval.
No ordinary learning worker can bypass those checks with direct DB access.
This is a gate before real multi-user pilot data, not a future hardening task.

## Morning and evening podcast integration

The existing morning reading brief and evening nidra are delivery surfaces for
the same personal memory model. They reuse the cast composition, narration and
podcast pipeline; no new audio scheduler, voice engine or parallel knowledge
store is needed. Preserve existing voices, episode lengths, schedules and
non-learning segments unless the user changes their settings.

Confirmed pacing: thirty minutes is an adjustable focused-study estimate;
the podcasts are extra listening. Show focused study and audio time separately
in Users. Existing episodes must not silently consume focused-study time. If a learner
opens a linked quiz or performs an actual retrieval task, that active task
counts toward focused study. Audio preferences and durations remain configurable.

### Content and learning evidence

The morning brief can introduce useful terms for the coming paper, explain
their relationship to familiar ideas, identify one important gap and state
today's reading goal. Use only this learner's authorized queue and memory
forecasts, with source links in the transcript. A learning segment can be
composed from the manually curated queue before the dated planner exists.
Where forecasts are uncalibrated, keep the curated policy and mark uncertainty.

The evening nidra walks familiar concepts and supported connections from that
learner's graph, optionally revisiting the day's reading. Preserve the
`precis-voice` nidra profile: calm pacing, familiar material and no recall
prompts or difficult new material. A proposed connection with weak evidence is
not made familiar simply by selecting it for narration. Put any optional
evening recall questions on the associated private study page, outside the
meditation, and count that active work against focused study.

Store an episode manifest naming the owner, source session, concept and
relationship IDs, source passages, projection/model version, learning purpose
and script/audio versions. Distinguish planned, composed, published and any
explicitly reported listening status. A feed fetch or MP3 download does not
establish that the learner listened. Listening/exposure is not successful
retrieval and must not automatically reset stability, certify a relationship
or increase a mastery score. Only submitted answers/reviews supply performance
evidence; the same answer imported through a cast page and a study session is
still one observation.

The deterministic planner selects an authorized concept/queue slice under the
recorded policy. The LLM writes a source-grounded script within the cast's voice
profile and duration allocation. Validated tools store the manifest, enforce
ownership and publish through the existing audio layer. Keep the transcript's
citations while making the spoken version understandable without visual
formulas, handles or URLs, following `precis-voice` and `precis-audio-help`.

### Privacy and existing seams

The additional podcast inspection used main
`83543e594`; the planning worktree remains based on `e249413a7`. Checked seams:

- `src/precis/reading/briefing_cast.py`: morning recall/reading lanes.
- `src/precis/reading/meditation.py`: evening concept walk.
- `src/precis/reading/cast_common.py::create_cast_draft`: existing
  idempotence is cast/date, not cast/date/owner.
- `src/precis/workers/cast_audio.py`: narration and publication.
- `src/precis_web/routes/podcast.py::feed` and
  `src/precis_web/routes/podcast.py::audio`: authenticated access currently
  lists/resolves the shared episode directory.
- `src/precis/audio_feed.py::Episode`: current episode metadata has no
  learner-owner field.

A per-user feed credential currently authenticates a listener; that alone
does not restrict which episodes they can receive. Before any personalized
learning content is published, add stable owner identity to the episode
manifest/storage, composition/render job context and owner-inclusive
idempotence keys. Resolve the authenticated listener's stable user ID and
enforce authorization both when building RSS and when serving an MP3 directly.
A different user's token, a guessed episode ID or an authenticated shared-feed
request must not retrieve personal audio. Separately label genuinely shared
episodes; unlabeled legacy output is not presumed safe for every user.

Scripts, episode descriptions, transcripts, citations, media files, caches,
render scratch and job artifacts inherit the owner's privacy. Hide personal
cast text from generic draft/Drive/graph readers unless those paths enforce
the same policy. Revocation prevents later server fetches; audio already
downloaded to the user's device cannot be recalled. Never place private
learning content on the current shared feed as a shortcut.

## Continuing queue and study activity

The primary surface is a continuing ordered queue: "what is the most important
useful thing to do next?" Thirty minutes is an adjustable starting estimate
for a typical focused session, not a quota, deadline, completion threshold or
automatic stop. A learner may do two minutes, continue for an hour, or stop
partway through an item and resume later. Podcasts remain extra listening.

### Importance before duration

Queue items include important refresh tasks, missing prerequisites,
explanation/connection probes, paper sections, synthesis and new learning.
Rank by the learner's explicit goal priorities, the consequence/urgency of
forgetting, what a task unlocks, remaining learning value and uncertainty.
Show the reason beside each item. Time estimates help choose a sensible
stopping point; they must not push trivial short tasks ahead of important work.
Use stable tie-breaking and aging to prevent useful longer work from starving.

Persist queue item IDs, prerequisites, learner pins, partial progress and
snoozes across sessions/dates. Recompute recommendations after evidence or
priority changes while leaving the current accepted task in place unless
the learner changes it. The next accepted horizon remains protected by the
automation gate: shadow mode proposes a new order for learner selection.

Start, continue, complete, pause, skip and snooze are explicit actions.
Skipped or missed days never discard or duplicate unfinished work and never
create a catch-up quota. A pinned item that cannot yet be prepared shows its
blocker; prerequisites can appear immediately ahead of it. Keep learning
maintenance, new learning and occasional synthesis in the same ordered view
instead of separate daily obligation lists.

Card preparation follows the upcoming paper items in that queue. A predicted
reading date is a revisable estimate; an explicit learner commitment can pin
one. Preserve the one/three-day preparation target from confirmed Anki
availability, expose "ready from" dates and keep blocked papers visible.
When the learner wants to read sooner, show the shorter preparation interval
and allow "read anyway". Do not wait for tomorrow merely because today's
suggested minutes have elapsed. Existing card-generation caps remain separate
controls to avoid flooding Anki; they do not cap focused study.

### GitHub style activity calendar

Interpret the requested GitHub "did you study" status as a private contribution-
calendar interface within précis, not a GitHub account write/integration.
Show a daily grid with recorded focused-study time when known and a breakdown
of meaningful activity: Anki reviews, submitted quiz/explanation attempts and
explicitly confirmed reading or synthesis. A failed but genuine attempt counts
as studying, while its learning outcome remains separate in the twin.

Offer a lightweight "Studied today?" check-in, including self-reported study
outside précis, and an optional rest-day marker. Retain whether an entry was
observed, timed or self-reported. A check-in records engagement; it does not
create a correct quiz answer or certify concept mastery. Do not reuse
`src/precis_web/routes/anki.py::known` for this check-in: that action retires
cards and marks shared concept mastery. Opening a page,
generating cards or downloading a podcast does not create study credit.
Audio listening can have a separate reported-exposure indicator.

Do not require thirty minutes or a correct answer to mark a study day. Label
an empty day "no study recorded", because the user may have studied elsewhere.
Make it easy to add/correct a past check-in. Do not reset the learning queue
after a blank day or treat rest as evidence that a concept has been forgotten.

A day tile opens its underlying events, completed/continued items and any
self-report. Show last recorded study, a recent activity summary and a direct
"continue" action beside the queue. The calendar shows engagement; separate
views show demonstrated learning, recall forecasts and uncertainty. Streaks,
if later added, are optional and never control access, difficulty or priority.

Derive the calendar deterministically from the same owner-scoped event ledger,
using the learner's timezone. Deduplicate source events and overlapping session
measurements so an Anki import plus its session timer does not double-count.
Offline/imported reviews belong to their original event date, not sync day;
unknown duration stays unknown rather than becoming invented minutes.
Calendar aggregates/check-ins are private and receive the same ownership checks
as answers and forecasts.

This activity surface can ship with the private evidence foundation before
decay forecasts are calibrated. Acceptance covers a short valid study event,
an unsuccessful attempt, a self-report without a fabricated mastery update,
a blank/rest day, offline backfill, deduplication, timezone boundaries and
two-user separation. Queue acceptance covers resume after a gap, importance
ahead of short-task convenience, preserved current work and no hard stop at
the suggested session duration.

## Study routine

Choose one quest and one concrete month-end capability, such as explaining the
competing mechanisms in its main papers and interpreting their key figures.
A five-minute diagnostic checks terminology, causal understanding, quantitative
interpretation and the ability to critique evidence. Include an "I do not know"
answer and confidence on each question. Familiar words alone do not establish
understanding. Use the result as an initial estimate, not a permanent label.

Use a continuing ordered queue, visible for steering before generation. Select
for relevance to the chosen learning objective, accessible source text, useful
prerequisites and the learner's remaining concept load. Start with a review or
tutorial where foundations are missing, then follow a connected sequence of
research papers. Add synthesis when it helps consolidate the learning. A long
paper can occupy several sessions; keep the same paper when it needs more time.

| When | Activity |
|---|---|
| Three days before reading | Explain unfamiliar, necessary concepts briefly; release the small set worth memorising to Anki. |
| Following days | Let Anki schedule reviews. Offer examples or prerequisite explanations for terms that remain unclear. |
| Focused session | Continue the most important ready item; use short or long sessions as available. A thirty-minute 5/20/5 split is an optional example, not a required allocation. |
| Synthesis item | Compare two papers, explain a figure or mechanism, identify an uncertainty, and adjust the coming queue. |

The three-day lead is a starting default; the Users tab offers one or three
days. Count it from confirmed first availability in the learner's Anki account,
not creation in précis. It is preparation time, not guaranteed mastery.
Bootstrapping uses the first three days for a diagnostic, foundations and short
readings while the first prepared research paper waits its lead time.

A reading session has an explicit target: the question, one
important figure/result and a limitation. A paper slot counts as read only
after the learner confirms it; opening the reader is exposure. Selective reading
is sufficient for a first pass. The system must show what remains unread.

Begin with at most five new **Anki review cards** per day across the whole
learning queue, counting distinct cloze indices rather than notes or concepts.
A note with c1, c2 and c3 consumes three places. Five is a ceiling, not a target;
zero is correct when no worthwhile unknown concept remains. Account for existing
review workload first. Pause new-card generation when workload is too high;
keep useful reading available and let the learner continue beyond the suggested
session duration. The card-generation cap does not cap study time.

Explain meaning, mechanism and a useful contrast before testing the exact term.
Deduplicate against the learner's own cards and reuse concepts across papers.
Card Back Extra carries paper and source passage links plus a brief explanation
when needed. Skip ordinary vocabulary, incidental acronyms and academic filler,
following `precis-cloze` and [anki-card-quality.md](anki-card-quality.md).
Unfamiliar prerequisites can be essential even if this paper does not introduce
them: use its glossary plus cited/corpus definitions and the prerequisite graph.

After reading, use two or three questions: state the main claim; explain a
mechanism or figure; name a limitation or a prediction that would distinguish
alternatives. Retain the answer, confidence, feedback and source/rubric version.
Quiz success, Anki retention and reading completion remain distinct evidence.

Before the manual pilot starts, provide a protected owner-only study log,
titled "Quest learning pilot <quest> <UTC start date>", and pin its handle in
the pilot's queue. Its complete retrieval path must pass the privacy gate
above; an owner tag on an ordinary draft is insufficient. Keep personal answers
out of the public repository. Record each
session's paper/sections, planned date, card count, owner/delivery confirmation,
actual minutes, answer text, confidence and blockers there. This study log is
separate from repo-dev memory. Later import it into personal evidence records.

Choose three explanation prompts at baseline, with the rubric fixed before
answering: 0 = missing/incorrect; 1 = partly correct; 2 = correct explanation
including the requested mechanism or limitation. Confidence is a separate
0–2 self-rating. On days seven and fourteen, use matched prompts with a new
example about material studied at least three days earlier. Retain prompts,
rubric versions and corrections; compare explanations, not confidence alone.

## Learning evidence and pilot decisions

Retrieval practice improved comprehension and inference from science texts in
[Karpicke and Blunt](https://pubmed.ncbi.nlm.nih.gov/21252317/). Spacing effects
depend on the retention horizon; experiments by
[Cepeda and colleagues](https://pubmed.ncbi.nlm.nih.gov/19439395/) support spacing
without establishing a universal one-day or three-day lead for research reading.

The optional 5/20/5 example, five-card ceiling and three-day lead are pilot
choices. Memory maintenance may change what deserves attention next. Test
whether the learner can explain the papers and resume useful work at a pace
that fits their actual availability, without enforcing a thirty-minute quota.
Compare one-day preparation later only if the three-day queue is cumbersome;
a two-week personal pilot cannot establish which interval is universally best.

At the end of fourteen days, inspect actual session/review minutes, abandoned
sessions, reported blockers and delayed explanation questions about earlier
papers. Track the number of useful concepts learned and recurring confusions.
Do not optimise for papers opened, cards generated or a single mastery score.
Continue, reduce volume or revise the route from those observations.

## Existing infrastructure and the gaps

Evidence was checked through the précis skills and Python kind against main
`e249413a72e3cc6d45b622d1657f6300672b103f`; the task worktree starts there.
The runtime's `precis` Python alias is the image checkout; `main` and
`wt-quest-learning` identify the host repository and this task's tree.
Source presence does not prove that a pass or cron is enabled in production.

| Existing seam | Reuse and required change |
|---|---|
| `src/precis/reading/__init__.py` and `src/precis/workers/paper_glossary.py` | Glossary, concepts, prerequisites and provenance already exist. Preserve the v2 quality gate in reading-prep-loop; do not enable corpus-wide extraction for this pilot. |
| `src/precis/reading/flashcards.py::mint_daily_cards` and `src/precis/reading/flashcards.py::effective_cards_per_day` | Current limit is concepts per day, with global settings; a concept can create several notes and deletions. Add an owner-scoped generated-card budget and schedule-driven eligibility. |
| `src/precis/reading/flashcards.py::_cardless_concepts` | Existing "has any live card" exclusion is not learner-scoped. One person's card must not suppress another's preparation. |
| `src/precis/reading/mastery.py::card_strength` and `src/precis/reading/mastery.py::run_mastery_pass` | Existing shared scalar averages interval-derived card strength; card_strength has no elapsed-time input. It is not the personal, time-aware concept/connection model required here. Add owner-scoped event evidence, uncertainty, decay and task-specific forecasts. |
| `src/precis/anki/__init__.py` | Per-user credentials, owner-scoped cards, AnkiWeb sync and stats already exist. Keep Anki's review scheduling and account safeguards. |
| `src/precis_web/routes/account.py::_render`, `src/precis_web/templates/account/index.html.j2`, `src/precis/users.py::WebUser` | The current self-service surface is Account, with Anki setup; there is no learning-settings panel. Add the requested Users-tab learning surface using the authenticated user's own account. Preserve CLI-only roster management. |
| `src/precis/reading/briefing_cast.py::_lane_reading` | Recent opens provide useful context, but cannot replace dated assignments, explicit completion, or time measurements. |

The missing product loop is: learner goal → dated paper session → required
concepts → advance card availability → reading/quiz evidence → revised queue.
This is separate from the quest's autonomous research execution. Selecting a
dormant quest for study must not activate it or dispatch its research jobs.

## Infrastructure proposal

Keep shared paper/concept identities and prerequisites. Add personal settings,
paper sessions and evidence beside them. Use new numbered migrations; do not
backfill a shared concept's mastery as anyone's personal knowledge.

The Users tab contains a Learning section, initially backed by the existing
`/account` self-service route and store layer. Expose an optional typical
session duration (initially 30 minutes, excluding podcasts), selected quest,
learning objective and priorities, preferred study days, timezone,
one-or-three-day preparation lead (default three), maximum new review cards
(default five), paused/enabled state and morning/evening learning-audio settings.
Show focused-study activity, extra audio minutes and the contribution calendar
separately. Suggested minutes are guidance; they never gate access or completion.
A missing authenticated identity
cannot read or edit another person's settings. Worker jobs receive an explicit
owner identity, rather than inheriting a process-wide actor.

A settings row keyed by stable `web_users.id` stores those preferences. A personal
plan records its quest, objective and version. Queue items retain stable
identity, priority/reason, dependencies, pin/snooze state and partial progress.
Session rows record the item/paper, target sections, optional planned date,
status and estimated/actual minutes. Activity aggregates derive from observed
events and labelled self-reports; never from generated content.
Personal state projections are keyed by owner, concept or relationship, and
task class; retain observation time, uncertainty and estimator version.
Append-only evidence records diagnostic/quiz outcomes and source-identified
Anki events; aggregate snapshots are labelled as bootstrap evidence. Preserve
raw answers and grading versions so corrected scoring can replay the state.
The memory model and this evidence ledger are foundation work, before adaptive
curriculum selection; only more elaborate estimators remain deferred.

Preview the queue and preparation/readiness estimates before enabling it.
An idempotent pass, runnable whenever the queue changes as well as daily,
prepares eligible sessions and creates only the missing owner/concept cards.
Use an atomic per-owner/date card-slot reservation so retries or concurrent jobs
cannot exceed the generated-card cap. Store timestamps in UTC and derive study
dates in the chosen timezone; changing timezone never grants a second daily cap.
Count already emitted cards conservatively across a timezone change.

Separate first publication to Anki from its later review due dates. Do not
create new cards early and claim they are waiting: sync currently publishes
authored cards. Keep future proposals as preparation records until release.
Only record delivery after the owner's sync confirms those cards. Compute a
paper's "ready from" date using the selected lead from confirmed availability.
Keep this item visible in the continuing queue; a forecast reading date is
revisable rather than a compulsory appointment. Delayed sync changes readiness;
missing delivery or ungrounded sources show a blocker. Allow an explicit
"read anyway" override and preserve that choice as context. A lead-time change
or moved paper updates unpublished work; delivered cards retain Anki history.

Pin the current item and any explicitly accepted upcoming items. Reorder only
the unaccepted queue after
a quest/preference/diagnostic change and explain the changed order. Skip,
reschedule, pause and resume preserve evidence and delivered cards; missed days
do not create catch-up bursts. A three-day lead uses calendar dates, while card
publication respects active study days and moves earlier if necessary.

Provide Continue Learning and Upcoming views with priority/reason, current
progress, estimated effort, preparation readiness, paper/target sections,
term explanations and quiz. Put the private activity calendar and
"Studied today?" check-in beside the queue. Show the personal
concept web's observed/inferred connections, evidence age, recall forecasts and
uncertainty, with a way to correct an estimate and report challenge. Start with the
web interface and existing Anki sync. Integrate learning segments into the
existing morning brief and evening nidra as specified above. No new email,
Discord delivery, phone client or review scheduler is
needed for the pilot.

MCP support is an explicit design checkpoint before the personal-data foundation:
choose a coherent typed read/write surface for personal study plans, settings,
sessions and quiz submissions. Inspect the existing `plan` kind before reuse:
it is an agent reasoning outline owned by a todo, not a learner curriculum.
Do not overload quest mutation to write private learner progress. Keep the seven
verbs, document ownership, retry behavior and actionable errors, then add a
`precis-learning-help` runtime skill and update `precis-anki-help` and
`precis-cloze` for actual-card limits. No new kind is selected by this proposal.

## Build sequence and acceptance

1. **Curated pilot design.** Pick quest/capability, prepare the diagnostic and a
   example continuing queue from shared papers/concepts and sample card text.
   Use synthetic accounts and responses only while identity/privacy is built;
   defer identifiable study queues, credentials, live card writes/delivery
   checks and real learning observations until slice 2 passes its privacy gate.
   Inspect every selected term and source. Keep broad extraction disabled.
   Acceptance: a study-log schema and rubric, synthetic sequence with planned
   preparation dates, source-grounded sample card text and a synthesis task.

2. **Private personal memory foundation.** Bind authenticated MCP users and
   scoped maintenance jobs; enforce the privacy/agent boundary above with
   two-user, anonymous and delegated-job tests before collecting real answers.
   Add the Users learning panel, a protected study log/activity calendar,
   labelled manual check-ins and an append-only
   owner-scoped observation ledger, concept/relationship/task projections,
   simple decay forecasts and forward-only migrations. Start with manual,
   rubric-scored explanation and connection probes as well as card reviews.
   Acceptance: the adjustable thirty-minute suggestion survives reload;
   changing/removing it changes estimates but never prevents further study.
   Activity/status checks in Continuing queue and study activity pass;
   two users have independent settings and evidence; forged cards inherit the
   intended owner; missing identity is refused. Quiz evidence must not silently
   overwrite Anki retention or shared concept state. Model checks in Personal
   memory model pass, including temporal decay, uncertain cold starts,
   evidence replay, duplicate events and owner separation.

3. **Calibration and challenge control.** Produce forecasts in shadow mode;
   store predictions before answers and run chronological evaluation against
   simple baselines. After slice 2, run the fourteen-day instrumented pilot
   with a private queue/log, manual cloze-card counting, owner-verified writes
   and confirmed delivery in that owner's Anki account. Keep the existing
   forge out of this pilot and do not use shared mastery as personal knowledge.
   Record actual minutes, usage and delayed explanations. Expose the challenge
   preference in Users and show predicted versus observed performance by task
   class. Depends on slice 2. Acceptance:
   repeated easy-but-failed and challenging-but-easy cases trigger visible
   recalibration; maintenance and new-learning targets remain distinct; probes
   count as focused study, with no forced stop at thirty minutes. Insufficient
   evidence keeps the curated policy
   active instead of claiming the twin is calibrated.

4. **Personal podcast learning segments.** Add owner-bound episode manifests,
   owner-inclusive generation/render idempotence, private transcript access and
   owner checks in both RSS and direct media delivery before personalized output.
   Reuse the morning recall/reading lanes and the evening familiar-concept walk.
   Depends on slice 2; use slice 3's forecasts only where supported and otherwise
   the curated queue. Slice 5 can later supply dated upcoming-paper context.
   Acceptance: two listeners cannot fetch each other's transcripts/audio even
   by direct ID; revoked grants stop future server access; concurrent retries
   do not mix users; downloading/listening does not certify learning; nidra has
   no recall prompts; existing voices/schedules are preserved; podcasts remain
   extra listening while linked active quizzes count toward focused study.

5. **Continuing queue and advance preparation.** Extend the agreed MCP study
   surface; add importance ordering, stable progress/pins/snoozes, preparation
   eligibility/release and Continue Learning/Upcoming views.
   This depends on slices 2–3, using the curated fallback where forecasts
   remain uncertain. Acceptance: frozen-clock one/three-day cases;
   no new Anki notes before release; two owners can learn the same concept;
   retries/concurrency and multi-cloze notes obey the cap; failure/delayed sync
   is visible; short and long sessions continue the same queue; important
   tasks precede trivial short ones; pause, missed days and timezone changes
   never reset progress or burst new work. Selecting a study quest leaves its
   execution status unchanged.

6. **Richer adaptive tasks and route changes.** Add reviewed question generation,
   learner corrections and versioned feedback; let repeated confusion select a
   prerequisite session. Depends on slices 2–3 and 5, and pilot evidence. Acceptance:
   a failed terminology question and a failed mechanism explanation produce
   different feedback; uncertainty and disputed grading are visible; the next
   accepted item stays pinned; a synthesis item compares two sources.

Implementation changes update the owning `reading`, `anki` and web package
docstrings, the linked reading-prep design and affected runtime skills/manual
pages in the same diff. Use `scripts/test` for targeted ownership, planner,
card-cap and route tests, then the repository's required lint, type and test
checks. Spec-only review verifies anchors, links and internal consistency;
it does not exercise Anki, scheduling or production writes.

## Open questions / decisions log

- Confirmed: thirty minutes is an adjustable starting session estimate, not a
  fixed budget, daily quota or stop. Importance and a continuing queue come first.
  Morning/evening podcasts remain extra listening.
- Proposed defaults: three-day preparation, five new review cards/day,
  continuing visible queue and synthesis when useful.
- Open for pilot content: which quest and what month-end capability?
- Confirmed priority: the digital memory twin is foundational. Model personal
  concepts and relationships, decay and uncertainty; refresh near predicted
  forgetting and calibrate challenge from actual outcomes.
- Open before automated implementation: the typed MCP study surface, evidence
  schema, estimator/priors and calibration-to-automation criteria. Keep this item
  at `status: idea`; split buildable slices after those choices are reviewed.
- Review resolved: the manual pilot verifies owner and delivery, counts cloze
  cards and does not use the existing forge or shared mastery as personal state.
- Review resolved: a protected owner-only study log stores the pilot;
  baseline and day-seven/day-fourteen prompts use a fixed 0–2 rubric and
  separate confidence. An ordinary draft is not assumed private.
- Review resolved: preparation lead starts at confirmed Anki availability;
  delayed delivery moves reading unless the learner explicitly reads anyway.
- Review resolved: bootstrap/state estimates carry history coverage; replay
  replaces overlapping projections rather than counting the practice twice.
- Review resolved: co-recall is sequence metadata; only tasks exercising a
  relationship contribute personal relationship evidence. Transfer stays deferred.
- Review resolved: shadow mode updates forecasts and recommends tasks for
  learner selection; automatic route changes wait for a defined automation gate.
- Confirmed privacy requirement: each user sees only their own personal
  memory. Missing identity is refused; agents need explicit owner-scoped job
  grants. Audit records distinguish acting agent from data owner.
- Design boundary: server code enforces identity, privacy, writes and policy;
  versioned numerical models make forecasts; LLMs propose/interpret with
  provenance and uncertainty. No LLM output grants access.
- Existing-identity correction: new learner records key on stable
  `web_users.id`, with adapters for legacy login-owned integrations.
- Confirmed integration: morning preparation and evening familiar-concept
  narration consume the same personal memory model. Podcast publication or
  listening does not establish recall; nidra remains free of recall prompts.
- Podcast privacy prerequisite: add owner-bound manifests, generation keys,
  transcripts and RSS/direct-MP3 authorization before personalized publication.
- Confirmed UI direction: a GitHub-style private activity calendar, study
  check-in and resume status alongside the queue. Engagement is separate from
  mastery; empty days mean no recorded study, not a negative memory observation.
- Interpretation: "GitHub" describes the contribution-grid interface here;
  no external GitHub account write/integration is requested or enabled.
- Review resolved: pre-foundation design uses synthetic accounts/responses and
  sample card text only. Identifiable queues, credentials, live card writes
  and all real pilot observations wait for the privacy gate; the actual
  fourteen-day pilot runs in calibration after private foundations.
