"""Shared block-tree IR spine for kind plugins that model a recursive,
instanced, port-connected spatial tree over the cad DSL.

Extracted from ``precis_se.ops`` (phase 1) — ``precis_nm`` migrated onto
the same spine in phase 2, then merged back into ``precis_se`` as its
atomic mode (the nm→se merge; ``nm`` now answers with a retired-kind
pointer). This package owns exactly the part that agreed between the two
kinds: the recursive tree (``parent``/``template``),
instancing with cycle guards, ports with ``roles``/``direction``/an open
``annotations`` dict, port-to-port connects, and envelope validation over
the ``precis.cad`` SDF kernel.

**Settled, 2026-09-07** (both phases landed that day, ``28877919`` se,
``96690d37`` nm): this was never a kind merge — a domain keeps its own
table and its own dark-gate setting; the superset is of the
implementation, never the vocabulary. Doctrine behind it: deduplication
is justified by duplication you can measure, parameterisation by users
you can name — this spine stayed a plain, unparameterised core rather
than pre-building a unit/binding-provider abstraction for a second user
that did not yet exist. ``pcb`` stays off the spine (Reto, 2026-09-07):
it carries a circuit vocabulary (nets, copper, footprints) and imports
nothing from ``precis.cad``; an instancing rhyme alone is too thin a
thread — do not re-litigate.

**Known wart, left deliberately:** :class:`~precis.blocktree.types.
BlockNode`'s ``ports`` is plain ``dict[str, Port]``, not generic over its
port type — ``precis_se``'s own narrowing (``PortSpec``) carries a scoped
``# type: ignore[assignment]``. Widen to ``BlockNode[TPort: Port]`` only
if a second domain needs its own port fields; ``se`` is currently the
only one.

**Open gap, unowned:** containment is expressed two incompatible ways in
the kind family — ``cad``/``component`` use ``links`` rows
(``contains``/``part-of``), ``se`` uses an intra-ref FK
(``parent_block_id``) — so there is no single "what contains what" query
across kinds, and block trees are invisible to the links graph. Possibly
the right trade (a links row per block is heavy), but it is
undocumented — ``docs/codebase.md`` says nothing about this kind family.

**Unit-agnostic by design** — this package never states Å or metres (or
any other unit constant); it only ever passes numbers through to
``precis.cad.dsl``, itself a unit-agnostic ``float64`` kernel. A domain
plugin declares its own unit and states it once, in its own docs.

Two submodules:

- :mod:`precis.blocktree.types` — the generic dataclasses (``OpError``,
  ``Port``, ``Connect``, ``Block``, ``Tree``) a domain subclasses to add
  its own fields.
- :mod:`precis.blocktree.ops` — the pure helpers (vector/name/envelope
  validation, tree-walk helpers, ``effective_ports``/``effective_envelope``)
  and the 9 shared op implementations (``add_block``, ``instance_block``,
  ``set_pose``, ``remove_block``, ``add_port``, ``remove_port``,
  ``set_port_pose``, ``connect``, ``disconnect``), dispatched through :func:`~precis.blocktree.
  ops.apply_ops` over a domain-supplied (and domain-extendable) ops table.

This package imports nothing from ``precis_se`` — dependency flows one
way, core to plugin, never back.
"""

from __future__ import annotations
