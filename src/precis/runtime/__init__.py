"""Server runtime — public surface.

``runtime.py`` used to be one 2400-line module; it's now a package split
by concern (``dispatch`` / ``search`` / ``angle`` / ``hints`` / ``error``,
plus ``core`` for the ``PrecisRuntime`` class itself and ``factory`` for
``build_runtime``). This module re-exports the same names every existing
``from precis.runtime import X`` call site (in this repo and any caller
outside it) already relies on — the split is invisible from here down.

Oversized responses refuse with targeted-read recovery rather than partial
content or continuation cursors. Exact-handle write substitution uses current
stored bytes and one-hour read receipts persisted by authenticated credential.
Without a bound principal, receipts remain session-local in process memory. The
tool ledger retains the caller's reference envelope, never expanded content.

Writer labels go through the credential gate at payload validation because
attribution is persisted even for handlers that accept opaque text.

A queried verbatim skill read uses find-in-ref so it searches the served
body even when the default read shows a section index. Fisheye queries
continue to select neighbor groups.

Table refusals select table help from explicit arguments or a best-effort chunk
lookup for ambiguous edits, so prose and table edits retain specific recovery.

An id-less taxon metadata get with a subtree selector goes to the taxon
handler, where the subtree selects individual rows. An unscoped get still
aliases the kind-wide metadata key inventory.

Licensed live responses opt out of pagination caching while retaining the
frame limit. The tool ledger already stores only call metadata, not content.

Labelled memory search batches expand before shared subtree filters resolve.
Each child uses ordinary dispatch and keeps its own ranking and error envelope;
shared tags are ANDed with item tags so a batch cannot drop its SPACE scope.
The eight-item/ten-hit bounds limit context while sequential dispatch retains
normal request/user scope without sharing a connection across worker threads.
Each child gets one additional backed-off attempt for lock contention so a
short-lived lock can clear without rerunning successful sibling searches.
The aggregate JSON is indivisible: an oversized envelope refuses with a
smaller-batch recovery instead of passing through the prose truncation fitter.

Structured ``Response.budget_fit`` callbacks preserve indivisible units
such as slides: the final fitter reserves runtime hint/cost bytes before
asking the handler to fit content. Runtime alone applies the optional
context cap, respecting ``fold=False`` and explicit part reads.

Read-scope handle redirects recheck the resolved kind against the sandbox
allowlist before rewriting dispatch arguments: the server's earlier gate
only saw the caller's kind, which a conversion can change.
Capability kind gates also span SDK resource and prompt handlers because
these modalities call runtime dispatch without passing through the tools.
The binding resets on success and failure so it cannot leak to another call.
Retired memory redirects drop view, extent and q when the successor is
another kind, with a receipt: memory read options can break that reader.

Mixed-kind handle lists on get use scalar routing for each address in caller
order, checking each resolved kind before reading. Same-kind lists retain
their handler's native batch behavior; writes still require one shared kind.

Cross-kind search reserves workers for finding, memory, todo, gripe, quest
and paper so the slow cache-backed kinds cannot occupy those workers.
Priority jobs have five seconds, and best-effort jobs have two. Both tiers
run concurrently and report omissions in the result header. Fusion
normalizes each kind's scores and applies kind priors on wildcard queries.
Default searches can salvage a canceled kind with one lexical retry inside
its deadline. Explicit kind lists and modes preserve the selected search
semantics.

Search traces reuse agentlog refs without tags or chunks: one best-effort
INSERT after dispatch, never an embedding/summarize/keywords work item.
Call-local stage evidence retains pre-cut candidates (50 plus the total),
so floor/fold/page misses remain attributable without extra retrieval queries.
Default agentlog listings omit traces; ``/traces`` is the explicit read door.
Followed/used feedback only consults a bounded process-local session cache:
32 searches per session, 128 sessions per store; Postgres alone checks the
30-minute window. Retrieval supplies chunk-owner aliases without extra queries.
No trace lookup is added to get; each matching row update has a 50 ms lock /
75 ms statement budget and only a confirmed committed update is cached.
Failures skip at debug level; losing feedback on process restart is intentional.
``precis traces export --jsonl`` reads a consistent read-only snapshot,
retaining unrendered candidates as unlabeled. Weak negatives exclude both
followed and used candidates; graph misses must postdate the trace.
"""

from __future__ import annotations

from precis.config import PrecisConfig
from precis.runtime.core import PrecisRuntime
from precis.runtime.factory import _connect_store_or_raise, build_runtime

__all__ = [
    "PrecisConfig",
    "PrecisRuntime",
    "_connect_store_or_raise",
    "build_runtime",
]
