"""Shared block-tree IR spine for kind plugins that model a recursive,
instanced, port-connected spatial tree over the cad DSL.

Extracted from ``precis_se.ops`` (docs/backlog/nm-se-shared-blocktree-core.md,
phase 1) — ``precis_nm`` is a second, still-unmigrated fork of the same
abstraction (phase 2, not touched here). This package owns exactly the part
that agreed between the two: the recursive tree (``parent``/``template``),
instancing with cycle guards, ports with ``roles``/``direction``/an open
``annotations`` dict, port-to-port connects, and envelope validation over
the ``precis.cad`` SDF kernel.

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
  and the 8 shared op implementations (``add_block``, ``instance_block``,
  ``set_pose``, ``remove_block``, ``add_port``, ``remove_port``,
  ``connect``, ``disconnect``), dispatched through :func:`~precis.blocktree.
  ops.apply_ops` over a domain-supplied (and domain-extendable) ops table.

This package imports nothing from ``precis_se``/``precis_nm`` — dependency
flows one way, core to plugin, never back.
"""

from __future__ import annotations
