"""``get(kind='finding', view='mint-preflight')`` — the read-only gate door.

The point of this view is that it runs the REAL
`precis.nanopub.gates.run_mint_gates`, not a copy: the nanobud campaign's
hand-rolled local mirror is exactly what it exists to retire
(`docs/backlog/nanopub-mcp-surface-gaps.md` §1). So these tests assert on
real gate slugs, and one of them pins the mirror-rot property directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.finding import FindingHandler
from precis.store import Store
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import attach_evidence, mint_hub
from tests.workers._helpers import seed_chunk, seed_ref

#: Passes the blocking lint: evidence verb ("shows") + epistemic mode
#: ("DFT") + terminal period + single assertion + self-contained.
_CLEAN = "DFT shows the elastic modulus increases by 12% under uniaxial strain."

#: Fails it on `no-epistemic-mode` — no technique named, so nothing says how
#: the assertion would ever be observed.
_NO_MODE = "The elastic modulus predicts a 12% increase under uniaxial strain."


def _handler(store: Store) -> FindingHandler:
    """The handler alone. Ids are passed bare: the ``fi`` prefix is stripped
    upstream by `runtime/dispatch.py::_maybe_split_prefixed_id`, so a handler
    called directly reads a prefixed string as a `pub_id` slug."""
    return FindingHandler(hub=Hub(store=store))


def _hypothesis_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "hypothesis": True,
        "passages": [],
        "fields": {},
        "motivation": "the transfer between the two systems is unproven",
        "testable_by": "nanoindentation under matched tip and load",
        "motivated_by_refs": [],
    }
    base.update(over)
    return base


def _bare_hub(store: Store, sentence: str) -> int:
    """A hub with one evidence edge, so `load_bundle` has something to load."""
    paper = seed_ref(store, title="A source", kind="paper")
    chunk = seed_chunk(store, ref_id=paper, text="A passage.")
    hub = mint_hub(store, CanonicalClaim(sentence=sentence, scope={}))
    attach_evidence(
        store,
        hub_ref_id=hub,
        paper_ref_id=paper,
        role="corroborates",
        meta={"source_handle": f"pc{chunk}"},
        check_retraction=False,
    )
    return hub


def test_clean_hypothesis_payload_passes(store: Store) -> None:
    hub = _bare_hub(store, _CLEAN)
    resp = _handler(store).get(
        id=hub, view="mint-preflight", payload=_hypothesis_payload()
    )
    assert "PASS" in resp.body
    assert "hypothesis" in resp.body
    # Admissibility is not truth, and the door says so rather than letting a
    # green result read as verification.
    assert "not truth" in resp.body


def test_sentence_without_an_epistemic_mode_is_blocked(store: Store) -> None:
    """The publishable-standard bar, reached through the view. Asserted with
    a CLAIM payload: the epistemic pair asks how a finding was established,
    which only a claim can answer — see the hypothesis case below."""
    hub = _bare_hub(store, _NO_MODE)
    resp = _handler(store).get(
        id=hub,
        view="mint-preflight",
        # `hanging` so the ungrounded-claim arm stays quiet and the sentence
        # gate is the only thing this asserts on.
        payload={"passages": [], "fields": {}, "hanging": True},
    )
    assert "BLOCKED" in resp.body
    assert "no-epistemic-mode" in resp.body


def test_hypothesis_is_not_asked_to_name_an_epistemic_mode(store: Store) -> None:
    """td244962: the pair is a category error for a conjecture — nothing has
    established it yet, and `testable_by` (separately mandatory above) is
    where its discriminating experiment lives. Same sentence, same view,
    different artifact type, opposite verdict."""
    hub = _bare_hub(store, _NO_MODE)
    resp = _handler(store).get(
        id=hub, view="mint-preflight", payload=_hypothesis_payload()
    )
    assert "PASS" in resp.body
    assert "no-epistemic-mode" not in resp.body


def test_hypothesis_carrying_a_passage_is_blocked(store: Store) -> None:
    """A hypothesis has no supporting passage by definition — this is the
    real gate, reached through the view rather than reimplemented."""
    hub = _bare_hub(store, _CLEAN)
    resp = _handler(store).get(
        id=hub,
        view="mint-preflight",
        payload=_hypothesis_payload(passages=[{"doi": "10.1/x", "quote": "q"}]),
    )
    assert "BLOCKED" in resp.body
    assert "schema-lint" in resp.body


def test_missing_testable_by_is_blocked(store: Store) -> None:
    hub = _bare_hub(store, _CLEAN)
    resp = _handler(store).get(
        id=hub,
        view="mint-preflight",
        payload=_hypothesis_payload(testable_by=""),
    )
    assert "BLOCKED" in resp.body
    assert "testableBy" in resp.body


def test_falls_back_to_the_parked_proposal(store: Store) -> None:
    """No payload supplied ⇒ gate whatever the proposal door parked, which is
    what makes `view='mint-preflight'` a one-liner self-check."""
    paper_a = seed_ref(store, title="First source", kind="paper")
    chunk_a = seed_chunk(store, ref_id=paper_a, text="A passage.")
    paper_b = seed_ref(store, title="Second source", kind="paper")
    chunk_b = seed_chunk(store, ref_id=paper_b, text="Another passage.")

    resp = _handler(store).put(
        title=_CLEAN,
        hypothesis=True,
        motivation="leap",
        testable_by="experiment",
        motivated_by=[f"pc{chunk_a}", f"pc{chunk_b}"],
        llm_models=["test-model"],
    )
    hub = int(resp.body.split("fi", 1)[1].split()[0])

    out = _handler(store).get(id=hub, view="mint-preflight")
    assert "parked/frozen payload" in out.body
    assert "PASS" in out.body

    # The view passes hub_meta, so the llm-attribution gate sees the parked
    # marker: a candidate payload that DROPS the attribution is blocked here
    # exactly as approve would block it.
    stripped = _handler(store).get(
        id=hub, view="mint-preflight", payload=_hypothesis_payload()
    )
    assert "BLOCKED" in stripped.body
    assert "llm-attribution" in stripped.body


def test_non_dict_payload_is_refused(store: Store) -> None:
    hub = _bare_hub(store, _CLEAN)
    with pytest.raises(BadInput, match="payload must be a dict"):
        _handler(store).get(
            id=hub,
            view="mint-preflight",
            payload="nope",  # type: ignore[arg-type]
        )
