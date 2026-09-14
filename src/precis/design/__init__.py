"""Shared design core — scenarios, per-number provenance, design history,
discrete states.

Four subsystems that recur **identically** at macro scale and at atomic
scale and must not be built twice (docs/backlog/design-state-core.md;
multiscale-design-system-spec.md §1.3–1.6, §5.6). One internal package,
**rented by ``se`` (including its atomic mode)** exactly as se already
rents the cad kernel and the :mod:`precis.blocktree` spine. One renter, not
two: the `nm` kind merges into se's atomic mode ahead of this work, so
"macro and nano" is one kind's two modes rather than two kinds
(multiscale-design-architecture.md §Build order, amended 2026-09-14).

**Not a kind of its own**: nothing here seeds a ``kinds`` row, no verb
reaches it, and every capability surfaces through se's ops and views.
Nothing in this package names its renter in schema — block uids,
provenance, history and states are design-level concepts, and a second
renter should cost a new import, not a migration.

- :mod:`precis.design.scenarios` — the production context that decides
  which physics runs at all. A ``scenario`` (``prototype`` /
  ``small_batch`` / ``mass_production``, seeded) carries quantity and
  objective weights and points at a ``service environment``: the lifetime
  master switch, temperature/chemical/vibration/cycle fields. A standard
  **load-case library** (shock, vibration, off-axis) applies to every
  design by default and comes off only with a reasoned exemption row.
- :mod:`precis.design.provenance` — the per-number sidecar
  ``{param: {source, fidelity, solver_id, assumptions, library_version?,
  margin_origin?}}``, with the validators that keep it well-formed. ONE
  enum pair for every number in the system: the spec's separate
  ``load_provenance`` collapses into it (:func:`~precis.design.provenance.
  from_load_provenance` is the shim). This is what makes the margin audit,
  the fidelity ladder and library-update notification *queries* rather
  than features.
- :mod:`precis.design.history` — the versioning axis, called **design
  history** and never "design state" (the glossary reserves *state* for
  the physical kind below). An ``envelope_revision`` minted on every
  tightening so a stale result is detectable; ``checkpoint``/``restore``;
  ``pin`` → a **branch** with its parent, a one-line reason, headline
  numbers, and the :class:`~precis.design.history.BranchResult`
  comparison. A pin is therefore always reversible.
- :mod:`precis.design.states` — a block's physical **discrete states**
  (compliant bistable latches and hard stops at macro scale; photoswitches
  and conformers in atomic mode) with stimulus-labelled transitions
  over a CLOSED ``driver_kind`` enum. Per-BLOCK current state, never one
  pointer per design. **Read that module's A9 hysteresis rule before
  caching anything.**
- :mod:`precis.design.uids` — the bigint mint behind stable block
  identity.

**Identity is a minted ``uid``, not a row id.** The renting persist layer
saves by retire-all/reinsert-all, so a block *row* id is rebuilt on every
save and cross-references were historically bare name text. A uid is minted
once per block from ``design_block_uid_seq``, carried forward across saves
as ordinary column data, and preserved by branch copies — which is exactly
what makes a naive full-copy branch diffable block-by-block. Core tables
here key on ``(ref_id, block_uid)`` and carry **no FK to the block**: the
block lives in a plugin migration namespace core may not reference, and the
``ref_id`` FK is what stops these rows outliving their design.

Storage lives in core migration ``0162_design_core.sql``. The
``design_situations`` table is a deliberate STUB — ids exist so things can
reference a situation, but the three-verdict rule table is build-order
step 3.

This package imports nothing from ``precis_se``: dependency flows core →
plugin, never back. It reaches the database over the store's public
connection surface (``store.tx()`` / ``store.pool.connection()``), the same
discipline the renter's persist module follows, and every write takes an
optional ``conn=`` so it can join a caller's transaction.
"""

from __future__ import annotations
