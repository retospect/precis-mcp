"""``edit(kind='finding', testable_by=…/motivation=…)`` — sharpen a live
hypothesis's falsification terms (gr263258).

DB-backed (real `refs`/`links`/`ref_tags` via the `store` fixture); no LLM.
Companion to `test_finding_hypothesis_put.py` (the mint side); this file
covers the edit door: the ordinary-finding rejection, the happy path
(payload patch + history), and the already-reviewed/signed refusal.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._finding_hypothesis import META_PROPOSED_PAYLOAD
from precis.handlers.finding import FindingHandler
from precis.store import Store
from tests.workers._helpers import seed_chunk, seed_ref

_SENTENCE = "Nanoindentation measures a modulus above 9 GPa in nanobud films."


def _handler(store: Store) -> FindingHandler:
    return FindingHandler(hub=Hub(store=store))


def _paper_with_chunk(store: Store, title: str) -> tuple[int, int]:
    ref_id = seed_ref(store, title=title, kind="paper")
    chunk_id = seed_chunk(store, ref_id=ref_id, text=f"A passage from {title}.")
    return ref_id, chunk_id


def _hub_id(body: str) -> int:
    return int(body.split("fi", 1)[1].split()[0])


def _mint_hypothesis(store: Store, **overrides: Any) -> int:
    _, ch1 = _paper_with_chunk(store, "First source")
    _, ch2 = _paper_with_chunk(store, "Second source")
    kw: dict[str, Any] = {
        "title": _SENTENCE,
        "hypothesis": True,
        "motivation": "Both papers attribute stiffness to the covalent junction; "
        "the transfer to films is untested.",
        "testable_by": "nanoindentation of a pressed nanobud film versus "
        "pristine graphene under the same tip and load",
        "motivated_by": [f"pc{ch1}", f"pc{ch2}"],
        "llm_models": ["test-model"],
    }
    kw.update(overrides)
    resp = _handler(store).put(**kw)
    return _hub_id(resp.body)


def test_sharpen_testable_by_patches_payload_and_records_history(
    store: Store,
) -> None:
    hub_id = _mint_hypothesis(store)
    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    original = ref.meta[META_PROPOSED_PAYLOAD]["testable_by"]

    resp = _handler(store).edit(
        id=hub_id,
        testable_by="site-resolved AFM force spectroscopy on individually "
        "addressed cages, versus ensemble SAXS",
    )

    assert f"fi{hub_id}" in resp.body
    assert "testable_by" in resp.body

    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    payload = ref.meta[META_PROPOSED_PAYLOAD]
    assert payload["testable_by"].startswith("site-resolved AFM")
    # motivation untouched.
    assert payload["motivation"] == ref.meta[META_PROPOSED_PAYLOAD]["motivation"]

    history = ref.meta["testable_by_history"]
    assert len(history) == 1
    assert history[0]["value"] == original
    assert "replaced_at" in history[0]
    assert "motivation_history" not in ref.meta


def test_sharpen_motivation_and_testable_by_together(store: Store) -> None:
    hub_id = _mint_hypothesis(store)

    resp = _handler(store).edit(
        id=hub_id,
        testable_by="a sharper experiment",
        motivation="a sharper leap",
    )
    assert "testable_by" in resp.body
    assert "motivation" in resp.body

    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    payload = ref.meta[META_PROPOSED_PAYLOAD]
    assert payload["testable_by"] == "a sharper experiment"
    assert payload["motivation"] == "a sharper leap"
    assert len(ref.meta["testable_by_history"]) == 1
    assert len(ref.meta["motivation_history"]) == 1


def test_sharpen_is_a_noop_when_value_is_unchanged(store: Store) -> None:
    hub_id = _mint_hypothesis(store)
    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    current = ref.meta[META_PROPOSED_PAYLOAD]["testable_by"]

    resp = _handler(store).edit(id=hub_id, testable_by=current)

    assert "no change" in resp.body
    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    assert "testable_by_history" not in ref.meta


def test_sharpen_refuses_an_empty_testable_by(store: Store) -> None:
    hub_id = _mint_hypothesis(store)
    with pytest.raises(BadInput, match="testable_by"):
        _handler(store).edit(id=hub_id, testable_by="   ")


def test_sharpen_refuses_an_empty_motivation(store: Store) -> None:
    hub_id = _mint_hypothesis(store)
    with pytest.raises(BadInput, match="motivation"):
        _handler(store).edit(id=hub_id, motivation="")


def test_sharpen_refused_on_an_ordinary_finding(store: Store) -> None:
    """A plain (non-hypothesis) finding has no testable_by/motivation
    fields — the door rejects, mirroring the ``title=`` non-hub check."""
    ref_id = seed_ref(store, title="an ordinary finding", kind="finding")
    with pytest.raises(BadInput, match="hypothesis"):
        _handler(store).edit(id=ref_id, testable_by="anything")


def test_sharpen_refused_once_the_publish_row_left_candidate(store: Store) -> None:
    """`nanopub/mint.py::approve` freezes ``testable_by``/``motivation``
    into the review row's ``grounding`` the moment a human reviews it
    (``state='reviewed'``) — well before ``sign``. A live edit past that
    point would silently diverge from what was reviewed, so the door
    refuses as soon as the row leaves ``candidate``."""
    hub_id = _mint_hypothesis(store)
    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    payload = ref.meta[META_PROPOSED_PAYLOAD]

    row = store.nanopub_create_publish_row(hub_id, artifact_type="hypothesis")
    assert store.nanopub_approve(
        row.id,
        approved_title=_SENTENCE,
        claim_sha="ab" * 8,
        aida_uri="http://purl.org/aida/test.",
        grounding=payload,
    )

    with pytest.raises(BadInput, match="candidate"):
        _handler(store).edit(id=hub_id, testable_by="too late now")

    # Nothing was written — the parked payload is untouched.
    ref = store.fetch_refs_by_ids([hub_id])[hub_id]
    assert ref.meta[META_PROPOSED_PAYLOAD]["testable_by"] == payload["testable_by"]
    assert "testable_by_history" not in ref.meta


def test_sharpen_and_other_ops_are_mutually_exclusive(store: Store) -> None:
    hub_id = _mint_hypothesis(store)
    with pytest.raises(BadInput, match="exactly one"):
        _handler(store).edit(id=hub_id, testable_by="x", pick_candidate="miller23a")


def test_sharpen_does_not_support_dry_run(store: Store) -> None:
    hub_id = _mint_hypothesis(store)
    with pytest.raises(BadInput, match="dry_run"):
        _handler(store).edit(id=hub_id, testable_by="x", dry_run=True)
