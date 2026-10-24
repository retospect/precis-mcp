"""``precis_geom`` — the script-side geometry core of a structure workspace.

The ``precis-geom`` wheel (``packaging/geom``) is what a workspace script
imports inside the ``--network none`` sandbox: ``hexfold`` (the atomic
builder, ``sheetnet`` after multicell slice 1), ``precis.cad`` /
``precis.structsolve`` / ``precis.structure`` (the macro side), and this
package — the pieces that are *the script's own surface*:

- :mod:`precis_geom.trace` — the construction trace: ``@traced`` on every
  public primitive pushes a frame; ``precis_ws.out`` dumps it to
  ``out/trace.json`` (``precis_geom.trace_schema`` is the wire format both
  the writer and the importer share).
- :mod:`precis_geom.out` — the dev-loop ``out/`` writer: a scratch script
  on the host records blocks/instances/connects and gets the same
  ``manifest.json`` + ``trace.json`` + ``blocks/…`` tree the importer
  reads (dev-loop-scratch spec D0). Its extxyz / ``.bonds.json`` bytes
  come from :mod:`precis_geom.extxyz` — the canonical write side of the
  importer's geometry format, duck-typed on the scene protocol so this
  package never imports ``precis``.
- ``precis_geom.assembly`` (``pga``) — the one assembly implementation
  scripts and MCP ops share (slice 2a).
- :mod:`precis_geom.flow` — the DB-free cad solid → thin-gap ``Domain``
  adapter, and :mod:`precis_geom.physics` — ``evaluate(shape, physics,
  spec, *, seed, cells)``, the toolkit's one physics call (flow first;
  se-flow-kernel slice 4a). Both are numpy/``precis_flow`` code.

The runnable dev-loop recipe — the PCB path and the ``hexfold
prism_tube`` → ``pga.geom`` → ``out.block`` atomic path — is
``packaging/geom/README.md`` (§"Build an atomic structure (hexfold)"
for the atomic side).

Stdlib only in this ``__init__``: anything heavier lives in submodules
or the wheel's other packages.
Import alias convention (spec §Core design): ``import precis_geom as pg``.
"""

__version__ = "0.1.0"
