"""Pending post-migration steps, by name — the shared read-only assert.

A migration applies inside one transaction, but some follow-ups cannot run
there: backfills over millions of rows, ``CREATE INDEX CONCURRENTLY``, a
``VALIDATE CONSTRAINT`` deliberately deferred (or left NOT VALID because a
widening restatement does not need to re-check live rows). Until the
follow-up runs the schema works but degraded — 0226's unvalidated
``chunk_embeddings_ref_kind_filled`` CHECK kept every kind-scoped semantic
search on the global-index post-filter for weeks (gr513663), and nothing
noticed (gr514955).

:data:`POST_MIGRATION_STEPS` is the registry the gripe asks for: each
migration that leaves work for outside its transaction maps to the step
that finishes it, so a pending item names the command to run. Add a row
when a migration needs an out-of-transaction follow-up; ``constraints``
names the NOT VALID constraint(s) the step settles (for attribution),
``ann_indexes`` marks the one step that also builds the per-kind partial
HNSW set derived from
:data:`precis.store._chunks_ops._ANN_PARTIAL_KINDS` — expected indexes are
never listed by hand, so code and prod cannot drift apart.

Consumers:

* ``precis post-migration-check`` — what ``scripts/release`` runs against
  prod after the deploy's migrate play, warning by name.
* The ``health_digest`` ``post_migration_steps`` row — the same probe
  reported continuously (:mod:`precis.workers.health_digest`).

Everything here is read-only; the finishing commands are *named*, never
run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from precis.store._chunks_ops import _ANN_PARTIAL_KINDS, ann_partial_index_name


@dataclass(frozen=True, slots=True)
class PostMigrationStep:
    """One migration's required out-of-transaction follow-up.

    ``finish`` is the command an operator runs to complete it; ``None``
    means the step is a plain ``ALTER TABLE <table> VALIDATE CONSTRAINT
    <name>`` (rendered per pending constraint). ``constraints`` attributes
    pending NOT VALID constraints to this migration — the *latest*
    restatement owns a constraint several migrations touched.
    """

    migration: str  # "<plugin>/<slug>", e.g. "precis/0226_chunk_embeddings_kind_ann"
    finish: str | None = None
    constraints: tuple[str, ...] = ()
    ann_indexes: bool = False


#: migration -> required post-step. Ordered for readability only; lookup is
#: by constraint name / the ann_indexes flag.
POST_MIGRATION_STEPS: tuple[PostMigrationStep, ...] = (
    PostMigrationStep(
        "precis/0049_doi_lowercase_guard",
        # The CHECK was left NOT VALID over the legacy non-lowercase rows:
        # reconcile folds the stub twins first, then the constraint validates.
        finish="precis reconcile-duplicates --apply, then ALTER TABLE "
        "ref_identifiers VALIDATE CONSTRAINT ref_identifiers_doi_lc",
        constraints=("ref_identifiers_doi_lc",),
    ),
    PostMigrationStep(
        "precis/0226_chunk_embeddings_kind_ann",
        finish="precis backfill-embedding-kinds --apply",
        constraints=(
            "chunk_embeddings_chunk_ref_fkey",
            "chunk_embeddings_ref_kind_fkey",
            "chunk_embeddings_ref_kind_filled",
        ),
        ann_indexes=True,
    ),
    # precis_se widening restatements stay NOT VALID on purpose (a widened
    # CHECK admits more, so no live row needs re-checking). The recorded
    # follow-up is still a plain VALIDATE — run it, or the assert keeps
    # naming the constraint.
    PostMigrationStep(
        "precis_se/0007_se_atomic",
        constraints=("se_connects_kind_check",),
    ),
    PostMigrationStep(
        "precis_se/0011_se_port_pose",
        constraints=("se_ports_pose_shape_check", "se_ports_pose_source_check"),
    ),
    PostMigrationStep(
        "precis_se/0014_se_port_frame_atoms",
        constraints=("se_ports_rot_shape_check", "se_ports_rot_source_check"),
    ),
    PostMigrationStep(
        "precis_se/0015_se_chain",
        constraints=("se_topology_kind_check",),
    ),
    PostMigrationStep(
        "precis_se/0023_se_bound_kind_pcb",
        constraints=("se_blocks_bound_kind_check",),
    ),
)


@dataclass(frozen=True, slots=True)
class PendingStep:
    """One thing a migration left for its post-step — named, with the fix."""

    name: str  # constraint or index name
    table: str  # relation it sits on
    migration: str  # owning "<plugin>/<slug>", or "" when unregistered
    fix: str  # the command / SQL that clears it


#: Every constraint Postgres knows it has never validated. NOT VALID is the
#: only producer of ``convalidated = false`` for user constraints.
_UNVALIDATED_SQL = """
SELECT conname, conrelid::regclass::text
  FROM pg_constraint
 WHERE NOT convalidated
   AND connamespace = 'public'::regnamespace
 ORDER BY conname
"""

#: The router's gate CHECK (store/_chunks_ops.py::_ann_route): per-kind
#: partial indexes only serve once it validates. ``to_regclass`` keeps the
#: probe quiet on a schema that predates 0226 instead of raising.
_ANN_GATE_SQL = """
SELECT convalidated
  FROM pg_constraint
 WHERE conrelid = to_regclass('public.chunk_embeddings')
   AND conname = 'chunk_embeddings_ref_kind_filled'
"""

#: Which of the expected partial index *names* exist, and whether each is
#: valid (a failed CONCURRENTLY build leaves an INVALID index that cannot
#: serve — it counts as pending).
_ANN_INDEX_SQL = """
SELECT c.relname, i.indisvalid
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
 WHERE i.indrelid = to_regclass('public.chunk_embeddings')
   AND c.relname = ANY(%s)
"""


def _ann_step() -> PostMigrationStep | None:
    return next((s for s in POST_MIGRATION_STEPS if s.ann_indexes), None)


def _pending_ann_indexes(conn: Any) -> list[PendingStep]:
    """Per-kind partial HNSW indexes the 0226 post-step has not built yet.

    Expected names come from :data:`_ANN_PARTIAL_KINDS` via
    :func:`ann_partial_index_name` — the router's own source — so the check
    can't drift from the code that routes. While the gate CHECK is
    unvalidated the whole set is reported as one line (the indexes could
    not serve even if built); once it validates, each missing or INVALID
    index is named on its own.
    """
    step = _ann_step()
    if step is None:
        return []
    gate = conn.execute(_ANN_GATE_SQL).fetchone()
    if gate is None:
        # Gate constraint absent: 0226 has not applied here (or the schema
        # predates it). `precis migrate` reports the pending migration
        # itself; the index names would only echo it.
        return []
    expected = {ann_partial_index_name(k): k for k in _ANN_PARTIAL_KINDS}
    present = {
        name: bool(valid)
        for name, valid in conn.execute(_ANN_INDEX_SQL, (sorted(expected),))
    }
    missing = [n for n in sorted(expected) if not present.get(n, False)]
    if not missing:
        return []
    if not gate[0]:
        # The filled CHECK is still NOT VALID — reported as its own
        # constraint item above; the indexes are one summary line on the
        # same finishing command, not a name each.
        return [
            PendingStep(
                f"{len(missing)} of {len(expected)} per-kind partial HNSW indexes",
                "chunk_embeddings",
                step.migration,
                step.finish or "",
            )
        ]
    return [
        PendingStep(
            name + (" (present but INVALID)" if name in present else ""),
            "chunk_embeddings",
            step.migration,
            step.finish or "",
        )
        for name in missing
    ]


def pending_post_steps(conn: Any) -> list[PendingStep]:
    """Every pending post-migration step, read-only, names attached.

    Two probes: ``pg_constraint`` rows never validated, and the expected
    per-kind partial HNSW indexes. A constraint registered in
    :data:`POST_MIGRATION_STEPS` names its migration and the recorded
    finishing command; an unregistered one falls back to its own
    ``VALIDATE CONSTRAINT`` (and asks to be registered).
    """
    items: list[PendingStep] = []
    by_constraint = {c: s for s in POST_MIGRATION_STEPS for c in s.constraints}
    for conname, table in conn.execute(_UNVALIDATED_SQL):
        step = by_constraint.get(conname)
        if step is None:
            items.append(
                PendingStep(
                    conname,
                    table,
                    "",
                    f"ALTER TABLE {table} VALIDATE CONSTRAINT {conname} "
                    "(unregistered — also record its post-step in "
                    "migration_postchecks.POST_MIGRATION_STEPS)",
                )
            )
        else:
            items.append(
                PendingStep(
                    conname,
                    table,
                    step.migration,
                    step.finish or f"ALTER TABLE {table} VALIDATE CONSTRAINT {conname}",
                )
            )
    items.extend(_pending_ann_indexes(conn))
    return items


def format_pending(items: list[PendingStep]) -> str:
    """Render the pending list for the CLI and the release warning."""
    if not items:
        return "post-migration check: clean — no pending post-migration steps"
    lines = [f"post-migration check: {len(items)} pending post-migration step(s):"]
    for it in items:
        src = f" (from {it.migration})" if it.migration else ""
        lines.append(f"  - {it.name} on {it.table}{src} — run: {it.fix}")
    return "\n".join(lines)


__all__ = [
    "POST_MIGRATION_STEPS",
    "PendingStep",
    "PostMigrationStep",
    "format_pending",
    "pending_post_steps",
]
