"""Schema-design guard — mechanical smells the drift/baseline tests don't see.

The baseline tests prove the migration chain *converges*; the drift test
proves hand-written SQL *resolves*. Neither says anything about the shape
of what the chain builds. This module adds three checks over the migrated
schema, each mechanical (no judgment call at test time — the judgment is
recorded in the baked-in lists below, at review time):

* **FK cycles** between distinct tables. A reference cycle means neither
  table can be inserted/deleted without deferred constraints or NULL
  dances, and usually signals two concepts folded into each other.
  Self-references (``parent_id``-style trees) are fine and excluded.

* **Unindexed FK columns.** Postgres indexes the *referenced* side (the
  PK) automatically but not the referencing side, so every ``DELETE`` /
  ``UPDATE`` on the parent seq-scans the child. Existing violations are
  grandfathered by exact ``table.constraint`` name below; new FKs must
  ship with a covering index (leading-prefix) or be deliberately added
  to the list with a reason.

* **jsonb-column ratchet.** jsonb is where schema goes to hide: keys the
  planner can't see, no FK integrity, no NOT NULL. Existing columns are
  allowlisted; adding a jsonb column is a schema-design decision, so a
  new one fails here until it's added deliberately — the failure is the
  review prompt ("do these keys deserve real columns?"), not a ban.

All three ratchet in both directions: fixing a grandfathered violation
also fails until the entry is removed, so the lists never go stale.
"""

from __future__ import annotations

from precis.store import Store

# ---------------------------------------------------------------------------
# Grandfathered state. Additions need a schema-design reason in the PR;
# removals are mandatory when the underlying violation is fixed.
# ---------------------------------------------------------------------------

#: Cross-table FK cycles, each recorded as a frozenset of table names.
#: The one allowed cycle, kept deliberately (triaged 2026-08-24):
#: ``nanopub_artifacts.publish_id`` is the real ownership edge (NOT NULL);
#: ``nanopub_publish.artifact_id`` is a nullable back-pointer caching the
#: current signed artifact, written after the artifact insert (see
#: ``nanopub/mint.py``) and load-bearing across the sign/anchor/publish
#: flow. The artifacts side is trigger-enforced append-only, so the
#: cycle's failure modes (delete deadlock, orphan dance) can't occur.
#: Don't extend the pattern to tables that see deletes.
ALLOWED_FK_CYCLES: frozenset[frozenset[str]] = frozenset(
    {
        frozenset({"nanopub_artifacts", "nanopub_publish"}),
    }
)

#: FK constraints with no covering index, as ``"table.constraint_name"``.
#: Triaged 2026-08-24 against prod stats; every FK whose parent has a
#: live delete path got its index in migration 0136. What stays here is
#: exactly the provenance family: parents (``actors``, ``summarizers``,
#: ``embedders``) are tiny append-only vocab tables with zero deletes
#: ever recorded, while the children total ~6.5 GB on the ingest hot
#: path — the index would cost real write amplification and buy nothing.
#: If one of these parents ever grows a delete path, index the child in
#: that same migration and drop the entry.
UNINDEXED_FKS: frozenset[str] = frozenset(
    {
        "chunk_embeddings.chunk_embeddings_embedder_fkey",
        "chunk_summaries.chunk_summaries_summarizer_fkey",
        "chunk_tags.chunk_tags_set_by_fkey",
        "chunks.chunks_set_by_fkey",
        "links.links_set_by_fkey",
        "ref_tags.ref_tags_set_by_fkey",
        "refs.refs_set_by_fkey",
    }
)

#: Every json/jsonb column on a base table, as ``"table.column"``.
#: Allowlisted wholesale 2026-08-24 (60 columns) — the per-column
#: keep/promote/index judgment is docs/backlog/jsonb-column-review.md.
JSONB_COLUMNS: frozenset[str] = frozenset(
    {
        "cache_state.meta",
        "cad_nodes.pattern",
        "chunk_events.source",
        "chunks.keywords_meta",
        "chunks.meta",
        "claude_quota_snapshot.data",
        "cluster_cells.words",
        "cluster_runs.params",
        "component_spec_values.conditions",
        "component_specs.allowed_values",
        "dream_log.seed_clusters",
        "dream_log.summary",
        "dream_transcripts.transcript",
        "email_account.config",
        "email_scan.evidence",
        "host_heartbeat.meta",
        "links.meta",
        "llm_call_log.features",
        "material_properties.allowed_values",
        "material_values.conditions",
        "nanopub_artifacts.dois",
        "nanopub_mirror.assertion_predicates",
        "nanopub_mirror.dois",
        "nanopub_publish.dependency_codes",
        "nanopub_publish.grounding",
        "part_footprints.centroid",
        "part_footprints.courtyard",
        "part_footprints.pads",
        "part_footprints.pin_map",
        "parts.params",
        "parts.price",
        "pcb_boards.fold_lines",
        "pcb_boards.meta",
        "pcb_boards.stackup",
        "pcb_components.centroid",
        "pcb_components.courtyard",
        "pcb_components.meta",
        "pcb_copper.geom",
        "pcb_drc_findings.objects",
        "pcb_features.geom",
        "pcb_features.meta",
        "pcb_instances.meta",
        "pcb_measures.meta",
        "pcb_measures.operands",
        "pcb_net_classes.meta",
        "pcb_net_classes.rules",
        "pcb_netconns.meta",
        "pcb_nets.meta",
        "pcb_pins.meta",
        "pcb_planes.meta",
        "pcb_planes.region_hint",
        "pcb_routes.fail",
        "pcb_routes.layer_assign",
        "pcb_routes.meta",
        "pcb_routes.topology",
        "pcb_routes.tree",
        "provenance_rw_cache.raw",
        "ref_artifacts.payload",
        "ref_events.payload",
        "refs.authors",
        "refs.meta",
        "struct_frames.positions",
        "struct_measures.embodiment",
        "struct_measures.goal",
        "struct_measures.operands",
        "struct_measures.value_derived",
        "struct_runs.charges",
        "struct_runs.final_geometry",
        "struct_runs.forces",
        "struct_runs.method",
        "struct_runs.params",
        "summarizers.config",
        "tool_calls.input_keys",
        "worker_logs.payload",
    }
)


def _rows(store: Store, sql: str) -> list[tuple]:
    with store.pool.connection() as conn:
        return conn.execute(sql).fetchall()


def _find_cycles(edges: set[tuple[str, str]]) -> set[frozenset[str]]:
    """Tables lying on at least one directed cycle, grouped per cycle.

    Tarjan-free: a strongly-connected component of size >= 2 (or any
    mutual pair) contains a cycle; report each non-trivial SCC as one
    "cycle" set. Iterative Kosaraju keeps it stdlib and recursion-safe.
    """
    nodes = {n for e in edges for n in e}
    fwd: dict[str, list[str]] = {n: [] for n in nodes}
    rev: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        fwd[a].append(b)
        rev[b].append(a)

    order: list[str] = []
    seen: set[str] = set()
    for start in nodes:
        if start in seen:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        seen.add(start)
        while stack:
            node, i = stack[-1]
            if i < len(fwd[node]):
                stack[-1] = (node, i + 1)
                nxt = fwd[node][i]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, 0))
            else:
                order.append(node)
                stack.pop()

    comp_of: dict[str, str] = {}
    for start in reversed(order):
        if start in comp_of:
            continue
        members = [start]
        comp_of[start] = start
        while members:
            node = members.pop()
            for nxt in rev[node]:
                if nxt not in comp_of:
                    comp_of[nxt] = start
                    members.append(nxt)

    comps: dict[str, set[str]] = {}
    for node, root in comp_of.items():
        comps.setdefault(root, set()).add(node)
    return {frozenset(c) for c in comps.values() if len(c) > 1}


def test_no_new_fk_cycles(store: Store) -> None:
    """Cross-table FK reference cycles are design regressions."""
    edges = {
        (str(a), str(b))
        for a, b in _rows(
            store,
            """
            SELECT conrelid::regclass::text, confrelid::regclass::text
            FROM pg_constraint
            WHERE contype = 'f'
              AND connamespace = 'public'::regnamespace
              AND conrelid <> confrelid
            """,
        )
    }
    cycles = _find_cycles(edges)
    new = cycles - ALLOWED_FK_CYCLES
    fixed = ALLOWED_FK_CYCLES - cycles
    assert not new, (
        "new FK reference cycle(s) between tables:\n  "
        + "\n  ".join(sorted(" <-> ".join(sorted(c)) for c in new))
        + "\nBreak the cycle (move one FK to a join table, or fold the "
        "concepts) rather than grandfathering it."
    )
    assert not fixed, (
        f"FK cycle(s) no longer present — remove from ALLOWED_FK_CYCLES: {fixed}"
    )


def test_fk_columns_have_covering_index(store: Store) -> None:
    """Every FK's referencing columns are a leading prefix of some index."""
    found = {
        f"{tbl}.{con}"
        for tbl, con in _rows(
            store,
            """
            SELECT c.conrelid::regclass::text, c.conname
            FROM pg_constraint c
            WHERE c.contype = 'f'
              AND c.connamespace = 'public'::regnamespace
              AND NOT EXISTS (
                SELECT 1 FROM pg_index i
                WHERE i.indrelid = c.conrelid
                  -- A partial index only serves the unqualified FK-cascade
                  -- lookup (`col = $1`) when the planner can prove the
                  -- predicate from it. The one mechanically checkable case:
                  -- the predicate is exactly `<fk-col> IS NOT NULL` — which
                  -- `col = $1` entails. Anything else (retired_at IS NULL,
                  -- state filters) leaves cascade scans over the excluded
                  -- rows unindexed and doesn't count as coverage.
                  AND (
                    i.indpred IS NULL
                    OR (
                      array_length(c.conkey, 1) = 1
                      AND pg_get_expr(i.indpred, i.indrelid) =
                        '(' || (
                          SELECT a.attname FROM pg_attribute a
                          WHERE a.attrelid = c.conrelid
                            AND a.attnum = c.conkey[1]
                        ) || ' IS NOT NULL)'
                    )
                  )
                  AND (string_to_array(i.indkey::text, ' ')::smallint[])
                        [1:array_length(c.conkey, 1)] @> c.conkey
              )
            """,
        )
    }
    new = found - UNINDEXED_FKS
    fixed = UNINDEXED_FKS - found
    assert not new, (
        "FK constraint(s) with no covering index (parent DELETE/UPDATE "
        "seq-scans the child):\n  "
        + "\n  ".join(sorted(new))
        + "\nAdd an index on the FK columns in the same migration, or "
        "grandfather here with a reason."
    )
    assert not fixed, f"now-indexed FK(s) — remove from UNINDEXED_FKS: {sorted(fixed)}"


def test_jsonb_columns_are_deliberate(store: Store) -> None:
    """A new json/jsonb column must be added to the allowlist on purpose."""
    found = {
        f"{tbl}.{col}"
        for tbl, col in _rows(
            store,
            """
            SELECT c.table_name, c.column_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema
             AND t.table_name = c.table_name
            WHERE c.table_schema = 'public'
              AND t.table_type = 'BASE TABLE'
              AND c.data_type IN ('json', 'jsonb')
            """,
        )
    }
    new = found - JSONB_COLUMNS
    dropped = JSONB_COLUMNS - found
    assert not new, (
        "new json/jsonb column(s):\n  "
        + "\n  ".join(sorted(new))
        + "\nIf the keys are stable and queried, they deserve real columns; "
        "if genuinely open-ended, add the column to JSONB_COLUMNS."
    )
    assert not dropped, (
        f"dropped jsonb column(s) — remove from JSONB_COLUMNS: {sorted(dropped)}"
    )
