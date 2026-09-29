"""Taxonomy bootstrap — generate a measurand list from corpus usage.

Implements `docs/backlog/taxonomy-bootstrap.md`: the five stages that turn a
frozen corpus snapshot into a versioned `list.vN.yaml`, plus the computed
subject axes. The list is an **output anyone can regenerate**, not a
hand-maintained table; what a human signs off is the procedure and the
thresholds in :class:`~precis.taxonomy.types.Thresholds`.

Stages, each rerunnable and each a pure function over its input:

1. :mod:`~precis.taxonomy.census` — ``scan_mentions()``. Deterministic
   number+unit scan over snapshot text. Same input, byte-identical output.
2. :mod:`~precis.taxonomy.discovery` — open-vocabulary model pass over
   mentions, split into halves A/B *before* the run so vocabulary stability
   is measurable.
3. :mod:`~precis.taxonomy.normalise` — alias merge, unit string to
   dimension, dimension mismatch as a hard gate, judge suggestions.
4. :mod:`~precis.taxonomy.select` — thresholds; also the usage test that
   earns a node ``systematic``.
5. :mod:`~precis.taxonomy.freeze` — write/diff ``list.vN.yaml`` carrying the
   snapshot identity, the thresholds and per-entry provenance.

:mod:`~precis.taxonomy.subjects` is the computed half of `term-taxonomy.md`:
a subject label in, `specialises` edge proposals out, each labelled with the
axis it widens along — ``composition`` (:mod:`~precis.taxonomy.composition`),
``periodic`` (:mod:`~precis.taxonomy.periodic`), ``termination``
(:mod:`~precis.taxonomy.termination`) and the campaign's curated
``material-class``. These are **not** `precis/data/axes/`, which is the
paper auto-tagging vocabulary and unrelated despite the shared word.

Deliberate boundaries
---------------------

**Nothing here knows chemistry.** The number+unit grammar has no unit word
list — ``pint`` is the arbiter of what counts as a unit, so an unknown token
degrades to "no unit" rather than to a wrong dimension. Reference states,
normalisation bases, required conditions and domain classes are campaign
configuration (``precis/data/taxonomy/campaigns/*.yaml``). A sociology
campaign supplies a different YAML and no code changes.

**`systematic` is not truth.** Promotion asserts only that the corpus uses a
term systematically. Validity, polysemy and consensus are `finding`
annotations linked to the node through the existing verdict/dispute path;
there is no trust column here.

**Its own unit registry.** `precis.utils.units` owns a process-wide pint
singleton shared with cad/structsolve; campaign unit definitions must not
leak into it, so :mod:`~precis.taxonomy.normalise` builds a private registry
and loads the campaign's definitions onto that.

**No writes.** Stages emit files under the campaign scratch directory. The
`taxon` node and `measures` row writers arrive as a thin adapter once
`term-taxonomy.md` and `measures-substrate.md` ship; until then nothing is
promoted in the corpus, only computed.
"""

from precis.taxonomy.types import (
    SI_BASE_ORDER,
    Anchor,
    AxisEdge,
    DimensionSpec,
    DiscoveredTerm,
    ListEntry,
    MeasurandList,
    Mention,
    Snapshot,
    TermNode,
    Thresholds,
)

__all__ = [
    "SI_BASE_ORDER",
    "Anchor",
    "AxisEdge",
    "DimensionSpec",
    "DiscoveredTerm",
    "ListEntry",
    "MeasurandList",
    "Mention",
    "Snapshot",
    "TermNode",
    "Thresholds",
]
