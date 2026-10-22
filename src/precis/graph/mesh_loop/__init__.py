"""kg-mesh loop — a recurring pass that meshes a quest's claim hubs.

Spec: ``docs/backlog/kg-mesh-loop.md`` (approved 2026-10-22, Pd-hydride
pilot me504790). The pipeline is

    select cluster ──▶ cheap tier: candidates ──▶ strong tier: writes
         (SQL)          (proposals only)              (pins + meta)
                                   │
        acceptance test + stamp + coverage metrics ◀┘

**Slice 1 (this package today)** is the SQL-only front and back ends:
:mod:`precis.graph.mesh_loop.clusters` ranks candidate clusters —
``precis mesh loop clusters`` — and
:mod:`precis.graph.mesh_loop.stats` reports the per-quest coverage
metrics — ``precis mesh loop stats``. Both are read-only: plain SELECTs
over ``refs``/``links``/``reviews``/``llm_call_log``, safe on
``agent_ro``. The cheap tier (detectors → ledger proposals), strong tier
(writes + acceptance test + revert) and the cadence land in later slices.

**Cluster.** A quest's hub set is its claim-hub neighbourhood. The
canonical membership edge — ``finding serves quest`` — does not exist
yet for the pilot hubs (the quest→finding direction fix is in flight),
so membership reads through three doors:

1. **direct** — any ``links`` row between the quest and a live finding,
   either direction, any relation (today that is mostly the pre-fix
   ``quest --supports--> finding`` harvester edges plus a handful of
   ``finding serves quest``);
2. **via serving paper** — a live finding carrying a claim-graph edge
   (:data:`~clusters.MEMBERSHIP_RELS`, either direction) to a live
   ``paper --serves--> quest`` — this is how the pilot hubs join: their
   papers serve the quest and carry ``corroborates``/``disputes``
   evidence edges to them;
3. **hub extension** — a live finding claim-linked to a hub already in
   the set (one hop: fi504780 reaches qu164903 only through
   ``fi504778 qualifies fi504780``; its own paper serves no quest).

A cluster qualifies when it holds ≥ ``min_hubs`` (8) hubs and carries
<1 stance edge per hub (:data:`~clusters.STANCE_RELS` among the set,
``meta.reverted`` edges excluded). Order: ``COALESCE(prio, 5) ASC``
(most urgent first, the repo's job-claim convention), then lowest
stance density, then the most recently added serving paper.

**Ledger conventions the later slices write** (slice 1 reads them):
proposal rows are ``reviews`` rows ``actor='kg-mesh-propose'``,
``verdict='proposed'`` with a ``{"kg_mesh": {...,"quest": <ref_id>,
"key", "run_id", ...}}`` note; resolutions append a terminal
``approved``/``rejected`` row echoing the ``key`` — the garden's
two-rows-per-action shape. A hub is **stamped** when a terminal
kg-mesh reviews row sits on it at its current ``content_sha``.
Strong-tier spend reads ``llm_call_log`` rows ``source='kg_mesh:write'``
keyed by ``features->>'run_id'``.
"""
