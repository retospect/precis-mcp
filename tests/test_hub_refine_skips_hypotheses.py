"""Widening must never touch a conjecture.

`hub_refine` and `chase_trigger` select over `TAPROOT:claim` +
`STATUS:canonical`, and `mint_hub` writes both tags on every hub it makes —
including a hypothesis. Left alone, the widening pass would go searching for
evidence that supports a guess, which is the failure
`docs/backlog/claim-review-mechanism.md` names in as many words: *"it will
find support for whatever the claim already says, including claims that are
wrong."* A hypothesis is the worst possible input, since the type exists
precisely because nothing supports it yet.

Both passes are dark today, so this is a latent trap rather than a live one
— which is exactly why it needs a test rather than a memory.
"""

from __future__ import annotations

from typing import Any

from precis.handlers._finding_hypothesis import (
    ARTIFACT_HYPOTHESIS,
    META_ARTIFACT_TYPE,
)
from precis.store import Store
from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    CanonicalClaim,
    claim_hub_predicate_sql,
    not_hypothesis_predicate_sql,
)
from precis.taproot.hub import mint_hub

_CLAIM = "DFT shows the elastic modulus increases by 12% under uniaxial strain."
_CONJECTURE = "Nanoindentation measures a modulus above 9 GPa in nanobud films."


def _selected(store: Store) -> set[int]:
    """Hub ids the widening passes' shared predicate pair admits."""
    sql = (
        "SELECT r.ref_id FROM refs r WHERE r.kind = 'finding' "
        "AND r.deleted_at IS NULL "
        f"AND {claim_hub_predicate_sql()} AND {not_hypothesis_predicate_sql()}"
    )
    params: dict[str, Any] = {
        **CLAIM_HUB_PREDICATE_PARAMS,
        **NOT_HYPOTHESIS_PREDICATE_PARAMS,
    }
    with store.pool.connection() as conn:
        return {int(r[0]) for r in conn.execute(sql, params).fetchall()}


def test_hypothesis_hub_is_excluded_but_a_claim_hub_is_not(store: Store) -> None:
    claim_hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    hypothesis_hub = mint_hub(
        store,
        CanonicalClaim(sentence=_CONJECTURE, scope={}),
        extra_meta={META_ARTIFACT_TYPE: ARTIFACT_HYPOTHESIS},
    )

    selected = _selected(store)
    assert claim_hub in selected
    assert hypothesis_hub not in selected


def test_the_claim_hub_predicate_alone_cannot_tell_them_apart(store: Store) -> None:
    """Pins *why* the extra clause is needed: both hubs carry the same tags,
    so a future refactor that drops it silently re-arms the trap."""
    claim_hub = mint_hub(store, CanonicalClaim(sentence=_CLAIM, scope={}))
    hypothesis_hub = mint_hub(
        store,
        CanonicalClaim(sentence=_CONJECTURE, scope={}),
        extra_meta={META_ARTIFACT_TYPE: ARTIFACT_HYPOTHESIS},
    )

    sql = (
        "SELECT r.ref_id FROM refs r WHERE r.kind = 'finding' "
        "AND r.deleted_at IS NULL "
        f"AND {claim_hub_predicate_sql()}"
    )
    with store.pool.connection() as conn:
        rows = {
            int(r[0]) for r in conn.execute(sql, CLAIM_HUB_PREDICATE_PARAMS).fetchall()
        }
    assert {claim_hub, hypothesis_hub} <= rows
