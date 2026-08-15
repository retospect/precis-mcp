"""Offline tests for :func:`precis.taproot.canon.extract_claim_strict_medium`
— strict MEDIUM-tier extraction with a format-flake guard (round 4 of
``docs/backlog/taproot-migration-extraction-quality-gates.md``: the
2026-08-15 ``llm.chain.medium`` cutover made MEDIUM haiku-via-``claude_p``,
so this now dispatches through the router instead of bypassing it). Every
LLM call is mocked (``canon.dispatch`` monkeypatched) — no live model, no DB.

Each failure mode mirrors what the 4-hub raw-response probe observed on the
BIG chain (2026-08-14): prose instead of JSON, a self-invented schema, a
literally empty reply, and (new here) a dispatch-level timeout/transport
error. A genuine NO-CLAIM (``not_claims`` populated, or a repeat empty-empty
on pointer prose) must NOT retry forever or raise.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from precis.taproot import canon
from precis.taproot.canon import ExtractionUnavailable, extract_claim_strict_medium

_SENTENCE = (
    "Gecko adhesion arises from van der Waals forces at individual spatulae "
    "(~200 nm) that support the animal's full body weight on glass."
)

_GOOD_PAYLOAD = {
    "assertions": [
        "Gecko adhesion arises from van der Waals forces at individual "
        "spatulae (~200 nm)",
        "Van der Waals forces support the gecko's full body weight on glass",
    ],
    "claims": [
        {
            "claim": (
                "Gecko adhesion arises from van der Waals forces at "
                "individual spatulae (~200 nm)."
            ),
            "quantity": "~200 nm",
        },
        {
            "claim": (
                "Van der Waals forces at gecko spatulae support the "
                "gecko's full body weight on glass."
            ),
            "regime": "on glass",
        },
    ],
    "compound": _SENTENCE,
    "not_claims": [],
}

_EMPTY_PAYLOAD: dict[str, list[object]] = {
    "assertions": [],
    "claims": [],
    "not_claims": [],
}

_NOT_CLAIM_PAYLOAD = {
    "assertions": ["a demonstrated sweet spot"],
    "claims": [],
    "not_claims": [
        {"text": "a demonstrated sweet spot", "reason": "vague, no quantity"}
    ],
}

#: The self-invented-schema probe shape (fi176399 rep0): valid JSON, no
#: "claims" key, and no key `_parse_claim_item` recognizes — the ONLY
#: contract violation that reaches the extractor as a *returned* result.
_INVENTED_SCHEMA_PAYLOAD = {
    "adhesion_mechanism": "van der Waals forces",
    "setae_count": "~1 billion setae per foot",
}


def _result(
    *,
    data: dict[str, Any] | None = None,
    text: str = "",
    error: str | None = None,
    timed_out: bool = False,
) -> Any:
    """A stand-in for ``LlmResult`` — the function under test only reads
    ``.error``, ``.timed_out``, ``.data``, and ``.text``."""
    return SimpleNamespace(text=text, data=data, error=error, timed_out=timed_out)


def _good_result() -> Any:
    return _result(data=_GOOD_PAYLOAD, text=json.dumps(_GOOD_PAYLOAD))


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[Any]:
    """Stub ``canon.dispatch`` to pop ``outcomes`` in order. Returns the
    call log (the ``LlmRequest`` each call was made with)."""
    calls: list[Any] = []

    def fake_dispatch(req: Any) -> Any:
        calls.append(req)
        return outcomes[len(calls) - 1]

    monkeypatch.setattr(canon, "dispatch", fake_dispatch)
    return calls


def test_good_payload_parses_on_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_dispatch(monkeypatch, [_good_result()])
    extraction = extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 1
    assert calls[0].tier is canon.Tier.MEDIUM
    assert calls[0].source == "taproot:extract-medium"
    assert len(extraction.atoms) == 2
    assert extraction.compound is not None


def test_timeout_raises_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatch timeout (``res.timed_out``) raises ExtractionUnavailable on
    the FIRST call, never retried — retrying a 240 s stall would double it
    for nothing."""
    calls = _patch_dispatch(
        monkeypatch,
        [_result(error="claude -p timed out", timed_out=True), _good_result()],
    )
    with pytest.raises(ExtractionUnavailable):
        extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 1


def test_non_timeout_dispatch_error_retries_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "_FLAKE_RETRY_BACKOFF_S", 0.0)
    calls = _patch_dispatch(
        monkeypatch, [_result(error="ECONNREFUSED"), _good_result()]
    )
    extraction = extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 2
    assert len(extraction.atoms) == 2


def test_persistent_dispatch_error_raises_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canon, "_FLAKE_RETRY_BACKOFF_S", 0.0)
    calls = _patch_dispatch(
        monkeypatch,
        [_result(error="ECONNREFUSED"), _result(error="ECONNREFUSED")],
    )
    with pytest.raises(ExtractionUnavailable):
        extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 2


def test_unparseable_reply_retries_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prose (or otherwise unparseable) reply — no ``data`` dict and no
    parseable JSON in ``text`` — is retried once, second call good."""
    calls = _patch_dispatch(
        monkeypatch, [_result(data=None, text="not json at all"), _good_result()]
    )
    extraction = extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 2
    assert len(extraction.atoms) == 2


def test_persistently_unparseable_raises_extraction_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twice-unparseable = a model stuck in prose mode — infra-grade, loud,
    NEVER a silent NO-CLAIM (the exact defect this guard exists to kill)."""
    calls = _patch_dispatch(
        monkeypatch,
        [
            _result(data=None, text="not json at all"),
            _result(data=None, text="still not json"),
        ],
    )
    with pytest.raises(ExtractionUnavailable):
        extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 2


def test_invented_schema_retries_as_empty_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fi176399-rep0 shape: valid JSON in a self-invented schema parses
    to empty-empty — retried once, recovered by a good second reply."""
    calls = _patch_dispatch(
        monkeypatch,
        [
            _result(
                data=_INVENTED_SCHEMA_PAYLOAD, text=json.dumps(_INVENTED_SCHEMA_PAYLOAD)
            ),
            _good_result(),
        ],
    )
    extraction = extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 2
    assert len(extraction.atoms) == 2


def test_repeat_empty_empty_is_accepted_as_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two empty-empty replies = a genuine NO-CLAIM (pointer prose), not an
    infinite retry: exactly two calls, empty extraction returned."""
    payload = _result(data=_EMPTY_PAYLOAD, text=json.dumps(_EMPTY_PAYLOAD))
    calls = _patch_dispatch(monkeypatch, [payload, payload])
    extraction = extract_claim_strict_medium("See refs. [12-14].")
    assert len(calls) == 2
    assert extraction.is_empty
    assert extraction.not_claims == ()


def test_reasoned_rejection_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty ``claims`` with populated ``not_claims`` is a considered
    verdict (the fi176468 shape) — accepted on the first call."""
    calls = _patch_dispatch(
        monkeypatch,
        [_result(data=_NOT_CLAIM_PAYLOAD, text=json.dumps(_NOT_CLAIM_PAYLOAD))],
    )
    extraction = extract_claim_strict_medium(_SENTENCE)
    assert len(calls) == 1
    assert extraction.is_empty
    assert len(extraction.not_claims) == 1


def test_empty_input_short_circuits_without_dispatching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_dispatch(monkeypatch, [])
    extraction = extract_claim_strict_medium("   ")
    assert calls == []
    assert extraction.is_empty
