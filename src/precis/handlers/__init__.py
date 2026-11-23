"""Handlers — kind adapters.

Handlers register :class:`precis.protocol.KindSpec` with
:class:`precis.dispatch.Hub` at boot. Shared adapters use underscore-prefixed
modules (:mod:`precis.handlers._numeric_ref`).
Plaintext and Markdown take the links view as an explicit argument so paths
ending in ``/links`` keep addressing files across verbs.
LLM diagnostics bound call-log scans to recent windows and report the
effective decide modes through the same memo used by live routing.
Schema introspection reads the live public catalog in a bounded read-only
transaction. Column statistics project only numeric fields. JSONB key
counts scan one ordinary table and use the database role's row visibility.
Staged findings retain the URL and verbatim quote without fetching.
Gripe themes span unrelated defects. Automatic merges require matching
project and cluster scope, and filing receipts suggest out-of-scope keepers.
Write-capable friction gripe creation counts and inserts under one database
lock across workers. Its filing marker survives later triage tag changes.
Gripe batch and cohort exports fit whole UTF-8 blocks against the runtime
budget so re-fitting preserves the cursor. An oversized block has its own
character cursor; the cohort cursor still advances to the next gripe.
Paper import promotes source-and-quote matches (:mod:`precis.finding_drafts`).
Hub-mint concurrency awaits production timing measurements.
Cached record selectors preserve ref identity despite numeric slug collisions.
Cache finds resolve stored rows without fetching. Withheld bodies also hide
section handles, and tag changes follow find validation.
Cache eyes apply injection verdicts while retaining link rings.
Retirement parity: :mod:`precis.handlers._audited_retirement`.
Superseded handles, restoration and DOI status: :mod:`precis.handlers.paper`.
Crossref misses leave DOI health unverified because other agencies register DOIs.
Paper tag batches commit edits with audit events. Numeric-ref tag batches
accept 300 IDs to support deck moves, and Anki opts in for tag alone.
Job simulation queues follow the quest's candidate links and nested jobs
so a freeze or parent hold is visible without inferring it from measurements.
Structure edit previews share relax validation with apply so invalid requests
never appear eligible for dispatch. Preview TOCs omit saved relax fields
because those fields describe the stored coordinates.
Abstract checks never queue paper ingest.
Queryless CFP search derives status from saved dates at read time so a call
closes on schedule without a metadata write.
An unheld DOI lookup offers acquisition without minting. A viewless get
can opt into the capped queue with ``queue='bounded'`` or bypass the daily
cap with ``queue='explicit'``. Batch passage searches keep their mint limit.
arXiv DataCite DOIs can use stored arXiv aliases. On an alias miss,
paper reads check the DOI itself before considering acquisition.
An acquire that lands on a tracked paper registers the identifiers it
resolved there: a stub known only by its S2 id needs the arXiv id on its
row before ``fetch_oa`` can try arXiv. One preprint, two spellings —
``arxiv:<id>`` and its DataCite DOI ``10.48550/arXiv.<id>`` collapse
onto the same row at mint, put and chase probe
(:func:`precis.identity.arxiv_doi_aliases`), and ``fetch_oa`` claims a
stub whose arXiv id only lives in ``refs.meta`` / a ``paper_id`` alias,
heals the strict row on the pass, and runs the arXiv leg first for any
stub that carried the id in.
Python-only navigation provenance precedes content so pagination retains
checkout identity. Call-local collection covers empty searches and cross-root
views; Git observations do not attest indexed bytes. Entry metadata and runtrace
execution remain separate. Root inventory does not index; authorization stays
in ``PRECIS_PYTHON_ROOTS``.
Explicit Python source anchors share the gripe ledger's alias namespace so
legacy comment handles stay addressable. A worktree observation is recorded
under its repository alias so the handle survives the worktree.
Python symbol link targets use static roots so persistent edges survive
session-scoped worktrees. Roots with the same qualname share its stored anchor.
Python reads warn when an explicitly requested ``wt-*`` alias is absent,
or when no worktrees are registered. Static aliases and the worktree registry
snapshot determine availability without discovery or index warming: a cold
registered worktree stays quiet, preserving its useful cold-build guidance.
``datasheet`` inbox-drop paths and the browser two-pane reader/citation
detail: ``docs/runbooks/datasheet-ops.md``.

Paper-family search (``paper``, ``cfp``, ``datasheet``) keeps title
matches in record callouts. Promoting records would consume chunk
slots, discard same-paper hits, or displace ranked papers after
store-side body preference and diversity caps — so no kind on the
``FusedBlockSearch.run`` path promotes. Only ``PaperHandler.search_hits``
retains title promotion, for the cross-kind merge.
Paper keyword navigation: stateless matched-chunk facets; ephemeral
extraction respects no-kwd.

Paper same-search hints preserve scope, mode (including omission) and broad
arguments to maintain corpus and ranking. Next-page hints preserve page size.
The single-hit larger-page action restarts at page 1.

``search(kind='paper')`` defaults to ``unique_per='paper'``: one row per
ref — the best surviving hit's ``pc`` handle, a ``+N`` count of its other
fused-pool hits, that chunk's keywords — over ``search_chunks_multi``'s
``per_paper=1`` + ``prefer_body`` collapse, which pages at paper
granularity. ``unique_per='chunk'`` keeps the per-hit drill shape and is
implied by the doors that already ask for it (``scope=``,
``mode='verbatim'`` — verbatim has no per-paper pool to collapse, so an
explicit ``'paper'`` there is a BadInput, not a silent fallback). Page-size
defaults follow the row shape (25 papers / 20 chunks): the MCP surface
forwards ``page_size=None`` when unset so the handler resolves it *after*
``unique_per``. Headline counters stay honest: "N papers of M matched
(K chunk hits)" counts the lexical universe (a lower bound under fusion);
``semantic``/multi-leg calls show the plain count, same rule as before.
Explicit ``unique_per=`` echoes into same-search continuations so a
``page=N+1`` hint can't flip shapes mid-walk.

Routine reads cap child collections and link to full views
(:mod:`precis.handlers._capped_section`). Comment timelines show the newest
comment in full and summarize older rows (:mod:`precis.handlers._comment_timeline`).

Paper ``view='measures'``: :mod:`precis.handlers._paper_measures`.
Range summaries reuse ordinal filtering.

Paper ``view='sentences'`` reuses the anchor splitter. It fits pages after
runtime reserves suffix bytes so the continuation names the first unseen
span. Marked excerpts keep oversized sentences from stalling the walk
(:mod:`precis.handlers._paper_sentences`).

Paper ``view="lookup"`` bypasses acquisition and processing marks. Held
records can be stubs.

Paper identity warnings exclude misleading issue-page prefixes.

Memory body writes lock the ref and anchored writes recheck the snapshot.
Ruling dates come from explicit metadata because a moved decision can
predate its memory node. Undated creates still get a review deadline.
Anchors retry folded on a literal miss to handle retyped prose.
Transaction and resolver details live in :mod:`precis.handlers.memory`.
Attribution reads rendered decimals in cited text.

Memory fusion deduplication retains the closest semantic distance across
each ref's candidate chunks. Otherwise a lexical chunk can represent the
ref and erase a sibling's rank-one semantic evidence before recall rescue.

Stored-route/ledger-count rationale: :mod:`precis.handlers.llm`,
:mod:`precis.handlers.job`.

**Bare ``**_kw`` catch-alls are strict by default (gr334695).** A verb
method's own ``**kwargs`` is the cooperative-inheritance idiom this
package leans on heavily — a subclass declares its kind-specific params
and forwards the rest via ``return super().<verb>(..., **_kw)`` to a
shared base (``NumericRefHandler``/``CacheBackedHandler``). The dispatch
boundary (:func:`precis.runtime.dispatch._handler_accepted_kwargs`)
statically walks that forwarding chain to compute the real accepted-kwarg
set and rejects anything outside it with a ``BadInput`` — a handler-
specific kwarg the schema declared but the handler's own signature never
wired in used to vanish into a dead catch-all with no error (gr333433,
gr334153). A verb method that must legitimately ignore extras (a
read-only-kind stub, or a kind that stays lenient by design) opts out
explicitly with ``@precis.protocol.tolerates_extra_kwargs`` — don't rely
on an undecorated ``**_kw`` to mean "tolerant"; the default is strict.

**Container-aware ``exclude=`` (cite closure).** ``search(kind='paper')``
and ``get(kind='semanticscholar')`` accept ``exclude=`` entries that are
paper slugs, draft refs or draft-chunk subtrees; containers resolve
server-side to every paper cited within. Container shapes, resolution
order and the stale-vs-bogus contract: :mod:`precis.handlers._exclude_closure`;
the held/stub/NEW corpus diff: :mod:`precis.handlers.semanticscholar`.

Create receipts read committed lifecycle tags because creation may override
open/active atomically; request defaults misreported parked todos/quests.
Todo receipts offer start-work only for open, unhalted refs.

**Forgiving inputs (gr494831).** Rule 9 of
``docs/conventions/mcp-authoring.md``; the announcement is a body prefix,
not a HintBus tip, because hints dedupe and cap
(:mod:`precis.handlers._interpret`).

Queryless workflow sorts, live defaults and exact terminal counts share
:mod:`precis.handlers._numeric_ref`. Empty ``PRIO:`` results suggest a fresh
priority browse because only resolved filters reach the renderer.

Finding edit previews validate against current data without taking the
apply path's review locks or reopening reviews. Taproot shares the signed
artifact and chunk-user guards across preview and apply.
See :mod:`precis.handlers._finding_edit`.

The todo tree
=============

``kind='todo'`` is a hierarchical todo graph unifying intent, scheduling,
execution, and review. The subsystem architecture — facet model, job
lanes, planner coroutines, cost guardrails, orphan subtrees, projects —
owns its facts in :mod:`precis.handlers.todo`'s module docstring; skills
``precis-todo-tree-help``, ``precis-minter-help``.

"""
