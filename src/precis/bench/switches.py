"""Server-side memory-ablation switches: remove one memory layer at a time.

The memory-ablation benchmark (memory graph, hub ``me621608``, layer arms
``me636378``) runs one ingested corpus under many arms. Each arm turns
one layer *off* in the server, in ranking and in the rendered output, so the
harness can measure what the layer buys in tokens and correctness without
re-ingesting. Nothing persisted changes: the switches gate reads only, and
with none set every code path is byte-identical to a build without them.

Arms (``PRECIS_BENCH_SWITCHES`` is a comma list of these names, e.g.
``no-kwd,semantic-only``; unset or empty means all layers on):

=================  ======================  =================================
arm                :class:`Switches` flag  what is removed
=================  ======================  =================================
``no-kwd``         ``kwd=False``           chunk keywords: the ``kwd`` rung,
                                           keyword rows / cluster labels, the
                                           keyword-containment ranking leg
``no-summary``     ``summary=False``       the ``summary`` rung and the
                                           ``llm-v1`` chunk summaries
``no-categorizer`` ``categorizer=False``   categorizer tag chips, hint and
                                           coverage lines; ``tags=`` on a
                                           categorizer axis and taxon
                                           ``under=`` are refused
``no-fisheye``     ``fisheye=False``       ``fisheye*`` rungs collapse to
                                           ``verbatim``; no neighbourhood is
                                           fetched
``no-recall``      ``recall=False``        ``+recall`` is not run, even when
                                           requested
``lexical-only``   ``semantic=False``      hybrid search runs only the
                                           lexical leg (no query embedding)
``semantic-only``  ``lexical=False``       hybrid search runs only the
                                           semantic leg
=================  ======================  =================================

``lexical-only`` together with ``semantic-only`` is an error (nothing would
be left to rank with). Any other combination composes, which is what the
factorial design needs.

How a call picks its switches: the dispatch entry point
(``precis.runtime.dispatch.DispatchMixin.dispatch_with_status``) wraps each
call in :func:`seeded`, which reads ``PRECIS_BENCH_SWITCHES`` once and binds
the result in a :class:`contextvars.ContextVar` unless a value is already
active (an in-process harness or a test that used :func:`applied`). Consumers
call :func:`current` at render / rank time; outside a dispatched call with
nothing applied it is :data:`ALL_ON`.

This module is stdlib-only and lives in ``precis.bench`` (whose package
``__init__`` imports nothing) so the memory kernel can import it without
pulling in bench code or creating a cycle.

Where each layer is cut (recorded 2026-11-09 in the retired backlog doc and
moved here 2026-11-11; verify with grep, the sites drift):

- ``no-kwd``: ``store/_mappers.py::_row_to_chunk`` (chunk rows carry no
  keywords, so every row, cluster label and TOC is built without them),
  ``store/_chunks_ops.py::_verbatim_pred`` (keyword-containment leg of
  ``mode='verbatim'``), ``ChunkStore.chunk_llm_summaries_for_ref`` (column
  not selected), ``utils/eye_render.py::_apply_bench_switches`` (the ``kwd``
  rung is refused), ``utils/toc_db.py::_pending_keyword_chunks`` and
  ``handlers/paper.py::_toc_readiness_line`` (no "pending" or "keyworded"
  text for a layer that is off, not draining).
- ``no-summary``: ``eye_render._apply_bench_switches`` (``summary`` rung
  refused), ``ChunkStore.chunk_llm_summaries_for_ref`` (the ``llm-v1``
  subquery is not run), ``_toc_readiness_line``.
- ``no-categorizer``: ``utils/tag_chips.py::chips_for`` (axis, ``topic:`` and
  ``*CASCADE`` chips), ``categorizers.py::ref_hint_line`` (hint line), and in
  ``DispatchMixin._apply_bench_switches`` / ``_resolve_under`` a ``tags=`` /
  ``exclude_tags=`` entry on a categorizer axis
  (``categorizers.is_categorizer_tag``) or a taxon ``under=`` is refused with
  a ``next=``, never silently broadened.
- ``no-fisheye``: ``eye_render._apply_bench_switches`` (via ``parse_extent``,
  the one funnel every eye render passes): any ``fisheye*`` rung becomes
  ``verbatim``, so ``_rings``, ``_first_hop`` and ``_second_hop`` never run.
- ``no-recall``: the same funnel drops ``+recall``, so ``eye_render._recall``
  (an embedding search) never runs; the empty-ring "wider" hint stops naming
  it.
- ``lexical-only`` / ``semantic-only``: ``DispatchMixin._apply_bench_switches``
  rewrites a hybrid (default) ``mode=`` on ``search`` to the existing
  ``mode='lexical'`` / ``'semantic'`` path, so lexical-only never embeds the
  query and semantic-only never runs the FTS leg;
  ``mesh_eval/retrieval.py::_search_arm`` applies the same rewrite for the
  eval's direct store call. An explicit ``mode=`` is left alone.

Measurement caveats, stated rather than hidden: ``no-fisheye`` collapses to
``verbatim``, and the document-kind ``verbatim`` chunk eye still renders its
within-cluster neighbour window, because that window is not a rung of its
own; a bare ``extent='+recall'`` still returns its implied ``fisheye+1hop``
ring under ``no-recall``; ``view='keywords'`` on ``search`` still returns its
(now empty) keyword column; the write-time ``put`` acks that name ``+recall``
and the nearest-hub hints (``memory_hubs._hubless_hints``) are not gated,
because the benchmark is read-only. A refused rung or filter is an error the
harness counts, not a degraded answer.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

#: The environment variable the server reads (comma list of arm names).
ENV_VAR = "PRECIS_BENCH_SWITCHES"

#: Arm name -> the flag it turns off.
ARMS: Mapping[str, str] = {
    "no-kwd": "kwd",
    "no-summary": "summary",
    "no-categorizer": "categorizer",
    "no-fisheye": "fisheye",
    "no-recall": "recall",
    "lexical-only": "semantic",
    "semantic-only": "lexical",
}


@dataclass(frozen=True, slots=True)
class Switches:
    """Which memory layers are on. Every flag defaults to True (all-on)."""

    kwd: bool = True
    summary: bool = True
    categorizer: bool = True
    fisheye: bool = True
    recall: bool = True
    lexical: bool = True
    semantic: bool = True

    def __post_init__(self) -> None:
        if not (self.lexical or self.semantic):
            raise ValueError(
                "memory switches: lexical and semantic are both off — "
                "'lexical-only' and 'semantic-only' cannot be combined"
            )

    @property
    def all_on(self) -> bool:
        return all(getattr(self, f.name) for f in dataclasses.fields(self))

    def search_mode(self, mode: str | None) -> str | None:
        """``mode`` as a search should run it under these switches.

        Only the hybrid default (``None`` / ``''`` / ``'hybrid'``) is
        rewritten, to the existing single-leg ``'lexical'`` / ``'semantic'``
        mode — so the embed call is skipped (lexical) or the lexical leg is
        never run (semantic) rather than computed and hidden. An explicit
        ``semantic`` / ``lexical`` / ``verbatim`` / ``literal`` stays as the
        caller wrote it. All-on returns ``mode`` untouched.
        """
        if self.lexical and self.semantic:
            return mode
        if (mode or "hybrid").strip().lower() != "hybrid":
            return mode
        return "lexical" if self.lexical else "semantic"


#: Every layer on — what runs when no switch is set.
ALL_ON = Switches()

_CURRENT: contextvars.ContextVar[Switches | None] = contextvars.ContextVar(
    "precis_bench_switches", default=None
)


def current() -> Switches:
    """The switches in force for this call (:data:`ALL_ON` when none)."""
    active = _CURRENT.get()
    return ALL_ON if active is None else active


def from_env(environ: Mapping[str, str] | None = None) -> Switches:
    """Parse ``PRECIS_BENCH_SWITCHES`` (a comma list of arm names).

    Raises ``ValueError`` on an unknown arm name, or on ``lexical-only``
    together with ``semantic-only``. Unset / blank is :data:`ALL_ON`.
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_VAR, "")
    flags: dict[str, bool] = {}
    for part in raw.split(","):
        arm = part.strip().lower()
        if not arm:
            continue
        flag = ARMS.get(arm)
        if flag is None:
            raise ValueError(
                f"{ENV_VAR}: unknown arm {arm!r}; known arms: {', '.join(ARMS)}"
            )
        flags[flag] = False
    if not flags:
        return ALL_ON
    return Switches(**flags)


@contextlib.contextmanager
def applied(**flags: bool) -> Iterator[Switches]:
    """Run a block with ``flags`` overriding the switches in force.

    ``with applied(kwd=False): ...`` — unknown flag names raise
    ``TypeError``; the previous value is restored on exit, even on error.
    An explicit :func:`applied` value wins over the environment: :func:`seeded`
    leaves it alone.
    """
    switches = dataclasses.replace(current(), **flags)
    token = _CURRENT.set(switches)
    try:
        yield switches
    finally:
        _CURRENT.reset(token)


@contextlib.contextmanager
def seeded() -> Iterator[Switches]:
    """Bind the env-derived switches for one call, unless a value is active.

    The dispatch entry point wraps every verb call in this. An already-active
    value (from :func:`applied`, or an outer ``seeded``) is kept as is, so
    nested dispatches and in-process harnesses see one consistent set.
    """
    active = _CURRENT.get()
    if active is not None:
        yield active
        return
    switches = from_env()
    token = _CURRENT.set(switches)
    try:
        yield switches
    finally:
        _CURRENT.reset(token)
