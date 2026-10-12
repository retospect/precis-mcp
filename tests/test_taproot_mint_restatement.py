"""Mint-time identity catches draft-scoped restatements (gr477211).

The fi449493 → fi192855 fork: a review draft's subject (``Carbon
nanobuds``) leaked into the scope of a sentence about "carbon
nanomaterials", the scope-hashed pub_id missed the existing hub, and a
duplicate was minted. Pins ``hub.mint_hub``'s sentence-first lookup for
untyped / non-landscape hubs, and that every identity hit on a merged
loser resolves to the live winner. DB-backed via the ``store`` fixture;
no LLM.
"""

from __future__ import annotations

from typing import Any

from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import merge_hubs, mint_hub

# The regression pair, verbatim from prod (fi449493 merged into fi192855
# by hand 2026-10-09).
FI449493 = (
    "The electrical and optical properties of carbon nanomaterials are "
    "conventionally tuned by chemical or electrochemical doping and by "
    "charge transfer from adsorbed molecules."
)
FI449493_SCOPE = {"material": "Carbon nanobuds"}
FI192855 = (
    "Carbon nanomaterials can be adjusted by doping and dedoping and by "
    "charge transfer through inter- and intramolecular interactions."
)
FI192855_SCOPE = {"material": "carbon nanomaterials"}


def _retired(store: Any, ref_id: int) -> bool:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT retired_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return row is not None and row[0] is not None


def test_untyped_restatement_under_a_leaked_scope_converges(store: Any) -> None:
    """Same sentence; the sentence names the hub's material, the new scope
    names one the sentence never mentions — a leaked label, not a claim."""
    hub = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI192855_SCOPE))
    for claim_type in (None, "mechanism"):
        again = mint_hub(
            store,
            CanonicalClaim(
                sentence=FI449493, scope=FI449493_SCOPE, claim_type=claim_type
            ),
        )
        assert again == hub


def test_leaked_scope_first_then_the_named_scope_converges(store: Any) -> None:
    """Order-independent: the leaked-scope hub came first."""
    hub = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI449493_SCOPE))
    again = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI192855_SCOPE))
    assert again == hub


def test_whitespace_and_case_do_not_hide_the_restatement(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI192855_SCOPE))
    noisy = "  " + FI449493.upper().replace(" ", "  ") + " "
    again = mint_hub(store, CanonicalClaim(sentence=noisy, scope=FI449493_SCOPE))
    assert again == hub


def test_scope_the_sentence_does_not_name_still_forks(store: Any) -> None:
    """Real scope: neither value is in the sentence, so the scope is what
    distinguishes the claims; an added unnamed axis is real scope too."""
    sentence = "Films show a work function of 4.9 eV by UPS."
    a = mint_hub(store, CanonicalClaim(sentence=sentence, scope={"material": "A"}))
    b = mint_hub(store, CanonicalClaim(sentence=sentence, scope={"material": "B"}))
    c = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    assert len({a, b, c}) == 3


def test_remint_of_a_merged_loser_converges_onto_the_winner(store: Any) -> None:
    """The prod pair: after the merge, re-minting fi449493's exact
    (sentence, scope) used to return the soft-deleted loser via its pub_id."""
    winner = mint_hub(store, CanonicalClaim(sentence=FI192855, scope=FI192855_SCOPE))
    loser = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI449493_SCOPE))
    assert loser != winner
    merge_hubs(store, loser_ref_id=loser, winner_ref_id=winner, set_by="agent")
    assert _retired(store, loser)

    exact = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI449493_SCOPE))
    assert exact == winner
    # The loser's sentence under the scope its words name: a pub_id miss,
    # found through the loser's title and resolved to the winner.
    restated = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI192855_SCOPE))
    assert restated == winner


def test_scope_value_inside_a_longer_word_is_not_named(store: Any) -> None:
    sentence = "Stability is fairly high under ambient storage."
    a = mint_hub(store, CanonicalClaim(sentence=sentence, scope={"atmosphere": "air"}))
    b = mint_hub(store, CanonicalClaim(sentence=sentence, scope={"atmosphere": "N2"}))
    assert a != b


def test_merge_chain_resolves_to_the_last_survivor(store: Any) -> None:
    first = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI449493_SCOPE))
    middle = mint_hub(store, CanonicalClaim(sentence=FI192855, scope=FI192855_SCOPE))
    last = mint_hub(
        store, CanonicalClaim(sentence="Doping tunes carbon nanomaterials.", scope={})
    )
    merge_hubs(store, loser_ref_id=first, winner_ref_id=middle, set_by="agent")
    merge_hubs(store, loser_ref_id=middle, winner_ref_id=last, set_by="agent")
    again = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI449493_SCOPE))
    assert again == last


def test_retired_hub_without_a_merge_record_is_not_a_target(store: Any) -> None:
    hub = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI192855_SCOPE))
    store.retire_ref(hub)
    again = mint_hub(store, CanonicalClaim(sentence=FI449493, scope=FI449493_SCOPE))
    assert again != hub and not _retired(store, again)
