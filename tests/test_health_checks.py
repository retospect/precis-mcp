"""``precis.health_checks.compute_backlog_counts`` — the ``/status`` panel
and ``health_digest``'s shared backlog truth.

gr483036: the module's hand-synced skip-kind copies had drifted from the
worker claim predicates (``field`` was missing from both the embed and
summarize lists), so field chunks read pending-forever in the watchdog.
Eligibility is now imported from the handlers themselves — these tests
pin that the panel view and the workers' ``skip_chunk_kinds`` can't
diverge again.
"""

from __future__ import annotations

import pytest

from precis import health_checks
from precis.store import Store
from tests.workers._helpers import seed_chunk, seed_ref

pytestmark = pytest.mark.db


def test_embed_pending_mirrors_handler_skip_kinds(store: Store) -> None:
    """Only ``field``/``references`` chunks unembedded →
    ``compute_backlog_counts()["embed"]["pending"] == 0`` — the watchdog
    sees "nothing to do", not a permanent backlog."""
    ref_id = seed_ref(store)
    seed_chunk(store, ref_id=ref_id, ord=0, chunk_kind="field", text="sdf grid")
    seed_chunk(store, ref_id=ref_id, ord=1, chunk_kind="references", text="[1] a bib")

    with store.pool.connection() as conn:
        counts = health_checks.compute_backlog_counts(conn)
    assert counts["embed"]["pending"] == 0
    assert counts["summarize"]["pending"] == 0  # 'field' had drifted here too

    # Control: an eligible paragraph chunk IS pending — the zero above
    # isn't a blind query.
    seed_chunk(store, ref_id=ref_id, ord=2, chunk_kind="paragraph", text="body")
    with store.pool.connection() as conn:
        counts = health_checks.compute_backlog_counts(conn)
    assert counts["embed"]["pending"] == 1
    assert counts["summarize"]["pending"] == 1
