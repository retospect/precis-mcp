---
status: draft
title: temporal index over documents — schedule and narrative events into a queryable fact table, so an agent can ask "what is on at 11:30, and what clashes"
prio: normal
---

# Temporal index over documents — a calendar-like read surface over any ingested text

## Motivation / why

**Driving use case: conference planning.** The case that produced this item
is a multi-track conference programme — an abstract book PDF carrying, for
every talk, a start and end time, a room, a speaker, an affiliation and a
title. It is a document précis can already ingest and search, and cannot
answer a single useful question about: what is on now, what is next in this
room, which two talks I want clash, what did I miss in the other track. An
attendee taking live notes ends up re-deriving speaker names and titles by
hand, from the podium, with transcription errors that then have to be
chased through Crossref — which is precisely what happened in the session
that prompted this. The programme holds the answer in structured form; the
corpus just has no surface that exposes it.

That use case also sharpens the requirement: a *schedule* document is not a
narrative one. Its events are scheduled and future, they carry structured
metadata (room, session, speaker) that matters as much as the time, and
they can **overlap** — the overlap is the question being asked, not noise to
be smoothed. A lab notebook or a filing history is the other profile: past,
narrative, prose-embedded, non-overlapping. Both are in scope, but the
schedule profile is the one to build first, because it is far easier to
extract and it is the one with a caller waiting.

An agent that wants the *chronology* of a document today has exactly one
option: read the document. Search returns semantically-similar chunks with
no time ordering; summaries compress content but drop dates; the corpus has
no way to answer "what happened in 2023", "what came before the ruling", or
"show me every experiment in this notebook in the order it was run".

Every structure in the codebase called a timeline is a *process log*, not a
document index. A gripe's comment timeline and a job's `job_event` chunks
(`src/precis/fixer/intake.py`, `_TimelineEntry`) are append-only records of
what précis itself did, ordered by insertion. `edgar`'s `view='diff'`
compares two filings quarter to quarter, which is the closest existing thing
to a temporal read, but it operates *across* documents and only on the
section level — it cannot tell you the order of events described *inside*
one filing.

The asymmetry is that the corpus is already excellent at "what does this
document say about X" and has nothing for "when". Documents where the
chronology *is* the content — lab notebooks, filing histories, clinical
courses, incident reports, project logs, meeting series, a patent's
prosecution history — are therefore readable but not queryable, and an agent
must burn the whole document into context to answer a question a two-row
lookup should settle.

Temporal tagging is a mature, well-specified problem rather than an open
research one. ISO-TimeML fixes the annotation model — TIMEX3 temporal
expressions, EVENT instances, and TLINK relations between them
[pa450283] — and rule-based taggers such as HeidelTime demonstrate that
extraction generalises across domains and languages with document-creation
time as the anchor for relative expressions [pa450282]. The design below
deliberately borrows the TIMEX3 value/precision split and the
document-anchored resolution of relative dates rather than inventing a
private representation.

## In scope

**A derived temporal index, not a new kind.** The obvious reading of the
request is "a `timeline` kind alongside `paper` and `patent`". That is the
wrong shape here, because it fragments the corpus: a document would have to
be re-ingested *as* the new kind to gain a chronology, and a `paper` that
happens to contain a reaction sequence would be permanently excluded. The
temporal index is instead a derived layer over refs that already exist, on
the same footing as embeddings and chunk summaries.

**Star schema, mirroring `component`.** The `component`/`material` pattern —
entity plus typed registry plus sourced value fact table, canonical units,
`conditions`, `as_of`, `method`, and a `chunk=` provenance pointer per row
(`src/precis/handlers/component.py`) — is the right precedent and should be
followed closely rather than re-derived:

- `document_events` — one row per extracted event: `ref_id`, `chunk_id`
  (provenance: which chunk asserted it), the resolved time as a range with
  an explicit `precision` (`day`/`month`/`quarter`/`year`/`unknown`) so a
  bare "in 2019" is not silently widened into a false January-1 point, the
  verbatim source span, a short label, `event_type`, `method`
  (`metadata`/`regex`/`llm`), and `confidence`.
- `document_event_types` — the typed registry, with the same
  proposed-on-unknown behaviour `component` uses for categories: an
  unrecognised type mints a `proposed` row rather than being coerced into an
  existing one or rejected.

**Extraction as a registered synthesis pass.** Events derive from chunk
text, so extraction belongs on the existing cascade that already re-runs
when chunks change, next to embeddings and summaries — not in ingest.
Ingest writes chunks; the worker fills the index. This also gets
re-extraction on edit for free.

**Event metadata beyond the time.** A `document_events` row carries a
`meta` jsonb for the structured fields a schedule supplies — room, session
or track, speaker, affiliation. These are not decoration: "what is on in HS
15.13" and "which of these two clashes" are the queries the driving use
case actually issues, and neither is answerable from a time range alone.

**Overlap as a first-class query.** For the schedule profile, two events
overlapping in time but differing in room is the *signal*. `view='timeline'`
must render concurrency rather than flattening it into a single ordered
list, and a conflict query — given a set of events of interest, which pairs
overlap — is part of v1, not a later refinement. This is the one place the
schedule profile demands something the narrative profile does not.

**The calendar read surface**, which is the actual deliverable:

- `get(kind=<any>, id=<slug>, view='timeline')` — that document's events in
  date order, with unresolved and relative expressions listed separately
  rather than silently dropped.
- `search(since=..., until=..., view='timeline')` — the cross-corpus
  query. `since=`/`until=` are already on the `search` signature, so this
  extends an existing parameter rather than adding vocabulary.
- A "now" query — events live at a given instant, defaulting to the current
  time — since for a schedule that is the single most common read.
- Both render bounded output: the point is that an agent asks "what
  happened in Q3" and gets rows, instead of paging the document.

## Explicitly NOT in scope

- **A new `kind`.** See above — this is a derived index over existing kinds.
- **Resolving relative expressions against a narrative anchor.** "Three
  weeks after the reaction", "the following quarter". Document-creation-time
  anchoring (TIMEX3 `DCT`) handles "last March" and is in scope; chaining a
  relative expression to a *previously extracted event* needs TLINK-style
  relation extraction and is a second project. v1 stores the verbatim span,
  marks it unresolved, and surfaces it in a separate section of
  `view='timeline'` so the omission is visible rather than silent.
- **Event coreference and deduplication across documents.** Two filings
  describing the same merger produce two rows. Merging them is a later
  slice.
- **Ordering relations without dates** (TLINK `BEFORE`/`AFTER`). The index
  is date-anchored only.
- **Backfilling the whole corpus.** The pass runs on new and edited chunks;
  a bulk backfill is a separate opt-in CLI, deliberately not automatic given
  corpus size.

## Acceptance criteria

1. A document containing dated statements, ingested through the normal
   path, yields `document_events` rows without any explicit extraction call.
2. `get(kind='paper', id=<slug>, view='timeline')` returns those events in
   chronological order; each row names the chunk it came from, and that
   chunk handle resolves.
3. A bare year in the source produces `precision='year'` and a range
   spanning the year — not a January-1 point. A test asserts this
   specifically, since it is the failure mode that makes a timeline quietly
   wrong.
4. An unresolved relative expression is stored and rendered in a distinct
   "unresolved" section; it is never omitted and never guessed.
5. `search(since=..., until=..., view='timeline')` returns matching events
   across at least two different kinds in one result set.
5a. A multi-track conference programme ingested as a document yields one
   event per talk, each carrying room and speaker in `meta`; a query
   restricted to one room returns that track in order; and a conflict query
   over two selected talks reports the overlap rather than silently
   ordering them.
5b. A "now" query at an instant covered by concurrent events returns all of
   them, not the first.
6. An unknown `event_type` mints a `proposed` registry row; it is neither
   rejected nor mapped onto an existing type.
7. Editing a chunk re-runs extraction for that chunk and leaves no stranded
   rows for the previous text.
8. Extraction failure on one chunk degrades to no rows for that chunk and
   does not fail the ingest or the surrounding pass.

## Target + blast radius

- New migration: `document_events` + `document_event_types` (forward-only;
  never edit a sealed migration).
- New synthesis pass registered alongside the embedding/summary cascade —
  follow `src/precis/workers/job_types/derived_drain.py` for the drain
  shape.
- `view='timeline'` on the shared get path, so it lands for every kind at
  once rather than per-handler; `src/precis/handlers/_cache_base.py` is the
  common surface.
- `search` gains a `view='timeline'` branch over `since=`/`until=`.
- Kind-totality risk: adding a view that claims to work on every kind is
  exactly the pincer `new_kind_totality_pincer` warns about — run
  `test_kind_totality` and `test_item_view` together, not `--impacted`.

## Open questions / decisions log

- **Range type.** `tstzrange` gives PostgreSQL range operators and indexing
  for free, but forces a timezone on a document that says "March 1987".
  `daterange` plus a separate precision column is cruder and probably
  righter. Leaning `daterange`; decide before the migration seals.
- **Who extracts.** A rule-based tagger is deterministic, cheap, auditable,
  and handles the bulk of well-formed dates; an LLM pass catches
  "the summer after the merger" but costs per chunk and needs a confidence
  story. Likely both, distinguished by `method`, with the rule pass
  mandatory and the LLM pass opt-in per ref.
- **Two profiles, one table or two.** Schedule events (future, structured,
  overlapping, metadata-heavy) and narrative events (past, prose-extracted,
  ordered, metadata-light) differ enough that one schema may serve both
  badly. Leaning one table with an `event_type` discriminator, since the
  query surface is shared; revisit if the schedule profile's `meta`
  requirements turn out to be load-bearing enough to want columns.
- **Schedule extraction is a different problem from temporal tagging.** A
  programme PDF is tabular and regular; HeidelTime-style prose tagging is
  the wrong tool for it, and a layout-aware parse will do better. The
  existing PDF pipeline (`src/precis/ingest/pres.py`, Marker plus Surya
  OCR) already recovers structure and is the more likely starting point for
  this profile. Related: `docs/backlog/slide-photo-capture-to-pres.md`.
- **Does an event need to be an event.** The cheap version indexes temporal
  *expressions* (every date mentioned). The useful version indexes
  *events* (something that happened, with a label). The gap between them is
  most of the difficulty, and the acceptance criteria above assume the
  latter. Worth confirming that the cheap version is genuinely insufficient
  before paying for the expensive one.
- **Interaction with `as_of`.** `put` already carries `as_of` on sourced
  value rows. Whether a `document_events` row for a measurement should also
  write a `component_spec_values`/`material` fact, or stay separate, is
  unresolved — they are the same information viewed two ways.
