"""Tiny shared helper used by ``finding.py`` and its split-out helper
modules (``_finding_acquire``, ``_finding_edit``, ``_finding_evidence``).

Kept separate (rather than importing from ``finding.py`` itself) so those
helper modules don't have to import back into the module that imports
them — ``finding.py``'s ``FindingHandler`` pulls all of these in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import BadInput
from precis.store.types import Ref

if TYPE_CHECKING:
    from precis.store import Store


def fetch_ref_any_kind(store: Store, ref_id: int) -> Ref:
    """Look up a ref by id without knowing its kind.

    The store's ``get_ref`` API requires ``kind``; ``parse_link_target``
    returns the resolved kind on the ``LinkTarget`` so callers can
    round-trip. Re-fetching here reads the slug (cite_key) for the
    deterministic pub_id input, and is reused everywhere a finding-family
    caller needs a handle for an arbitrary link endpoint (put's cited_in
    resolution, the candidate-pick / evidence-render paths, the begat
    detail render).
    """
    from precis.store._mappers import _REFS_COLS, _row_to_ref

    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT {_REFS_COLS} FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (ref_id,),
        ).fetchone()
    if row is None:
        raise BadInput(
            f"cited_in target ref_id={ref_id} not found",
            next=(
                "the target was deleted or never existed — find a live one "
                "with search(kind='paper', q='<topic>') or look up by DOI "
                "with get(kind='paper', id='<doi>')"
            ),
        )
    return _row_to_ref(row)
