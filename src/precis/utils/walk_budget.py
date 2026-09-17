"""Shared wall-clock budget for an unbounded ``os.walk`` over PRECIS_ROOT.

PRECIS_ROOT can be bind-mounted over a huge host tree (the dev-stdio
launcher mounts all of ``~/work`` — hundreds of thousands of files),
and two call sites walk it end to end:

- the boot-time file-kind count in :mod:`precis.server`
  (:func:`precis.server._file_kind_counts`), which blocks the MCP
  handshake — see dc785a20;
- the plaintext/markdown/tex listing walk in
  :meth:`precis.handlers.plaintext.PlaintextHandler._walk_files`,
  which blocks a ``get(kind='markdown')`` call (gr345271).

Both bound the same walk against the same tree, so they share one
budget constant here rather than each carrying its own magic number
that could quietly drift out of sync — a boot preamble that reports
"large tree, count skipped" but a listing call that still hangs (or
vice versa) would be a worse bug than either alone.
"""

from __future__ import annotations

#: Wall-clock budget, in seconds, for one full ``os.walk`` over
#: PRECIS_ROOT. Checked once per directory by each call site.
FILE_WALK_BUDGET_S: float = 1.0
