"""Server runtime — public surface.

``runtime.py`` used to be one 2400-line module; it's now a package split
by concern (``dispatch`` / ``search`` / ``angle`` / ``hints`` / ``error``,
plus ``core`` for the ``PrecisRuntime`` class itself and ``factory`` for
``build_runtime``). This module re-exports the same names every existing
``from precis.runtime import X`` call site (in this repo and any caller
outside it) already relies on — the split is invisible from here down.

Oversized responses refuse with targeted-read recovery rather than partial
content or continuation cursors. Exact-handle write substitution uses current
stored bytes and same-session read receipts held only in process memory. The
tool ledger retains the caller's reference envelope, never expanded content.

Licensed live responses opt out of pagination caching while retaining the
frame limit. The tool ledger already stores only call metadata, not content.

Read-scope handle redirects recheck the resolved kind against the sandbox
allowlist before rewriting dispatch arguments: the server's earlier gate
only saw the caller's kind, which a conversion can change.

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
