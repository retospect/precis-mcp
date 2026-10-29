"""``precis_geom`` — the script-side geometry core of a structure workspace.

The ``precis-geom`` wheel (``packaging/geom``) is what a workspace script
imports inside the ``--network none`` sandbox: ``hexfold`` (the atomic
builder, ``sheetnet`` after multicell slice 1), ``precis.cad`` /
``precis.structsolve`` / ``precis.structure`` (the macro side), and this
package — the pieces that are *the script's own surface*:

- :mod:`precis_geom.trace` — the construction trace: ``@traced`` on every
  public primitive pushes a frame; ``precis_ws.out`` dumps it to
  ``out/trace.json`` (``precis_geom.trace_schema`` is the wire format both
  the writer and the importer share). Schema 3 retains schema 1/2 readers
  and adds inclusive frame timing, bounded return digests joinable to input
  digests, and folded argument/return summaries. Timing stays out of semantic
  diffs; sampled user frames measure only their observed interval because
  tracing every Python return would impose a profiling hook on all calls.
  Arguments and returns share a 256-value, depth-16, 4096-byte digest budget;
  stored content hashes bypass traversal. Capped samples are marked explicitly
  because they cannot prove equality of unseen content. This bounds repeated
  capture of nested geometry without breaking producer/consumer joins.
  PCB parts/nets carry run-local maker stamps and primitive creation counts;
  stamps stay outside the authored batch so tracing cannot change its bytes.
- :mod:`precis_geom.out` — the dev-loop ``out/`` writer: a scratch script
  on the host records blocks/instances/connects and gets the same
  ``manifest.json`` + ``trace.json`` + ``blocks/…`` tree the importer
  reads (dev-loop-scratch spec D0). Its extxyz / ``.bonds.json`` bytes
  come from :mod:`precis_geom.extxyz` — the canonical write side of the
  importer's geometry format, duck-typed on the scene protocol so this
  package never imports ``precis``.
- ``precis_geom.assembly`` (``pga``) — the one assembly implementation
  scripts and MCP ops share (slice 2a). Transforms record touches while
  retaining creator stamps, so frame selection still reaches the source.
  Fusion retains source/placement makers; coincident atoms and duplicate
  bonds use the earliest copy's representative rather than a tile stamp.
- :mod:`precis_geom.pcb_motifs` — the stdlib netlist motif registry called
  by ``Board.check()``. Each ``Motif`` declares eligibility and a check
  returning the existing ``CheckFinding`` shape: LED current limiting,
  buck SW/inductor/output and FB divider, and LDO input/output capacitors.
  Pin functions override semantic pin labels; unclassified parts are
  ineligible. The buck divider identifies its output independently of the
  inductor, so moving the inductor to another power rail is detectable.
  Fixed-output bucks may sense the inductor's output net directly at FB.
  Passive roles or conventional R/C/L designators identify bridges; every
  distinct-net terminal pair is a bridge for multi-terminal passives because
  the builder has no internal winding/network map. No catalog, server,
  placement or copper is needed.
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
