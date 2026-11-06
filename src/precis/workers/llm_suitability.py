"""Nightly suitability rollup: validated outcomes → model-card posteriors."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store


def run_llm_suitability(store: Store) -> int:
    """Recompute the suitability ledger and write it to model cards.

    Returns the number of cards whose ``meta.suitability`` changed.
    The rollup itself is pure recompute (idempotent, never accumulates)
    and spends nothing — no LLM call anywhere on this path.
    """
    from precis.utils.llm.suitability import rollup

    rows = rollup(store, write=True)
    return len({r.model for r in rows if r.written})
