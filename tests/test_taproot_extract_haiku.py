"""Offline tests for :func:`precis.taproot.canon.extract_claim_strict_haiku`
— the router-bypassing haiku extractor (round 3 of
``docs/backlog/taproot-migration-extraction-quality-gates.md``). The
``claude -p`` wrapper is a stub — no subprocess, no LLM.

The stubs honor ``call_claude_p``'s real contract: a prose or empty reply
never *returns* — the wrapper raises
:class:`~precis.utils.claude_p.ClaudePUnparseableError` (no parseable JSON
block), so that shape must be retried at the exception level; only a
schema-shaped-but-empty JSON payload comes back as a returned result. Each
failure mode mirrors what the 4-hub raw-response probe observed on the BIG
chain (2026-08-14): prose instead of JSON, a self-invented schema, a
literally empty reply. A genuine NO-CLAIM (``not_claims`` populated, or a
repeat empty-empty on pointer prose) must NOT retry forever or raise.
"""

from __future__ import annotations

import json

import pytest

from precis.taproot.canon import (
    ExtractionUnavailable,
    extract_claim_strict_haiku,
)
from precis.utils._claude_subprocess import ClaudeProcessError
from precis.utils.claude_p import ClaudePResult, ClaudePUnparseableError

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


def _result(payload: dict) -> ClaudePResult:
    """A ClaudePResult as the real wrapper builds it: ``data`` = the parsed
    payload dict, ``text`` = the envelope-stripped reply string."""
    text = json.dumps(payload)
    return ClaudePResult(data=payload, raw_stdout=text, cost_usd=0.001, text=text)


def _patch_calls(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[dict | Exception],
) -> list[str]:
    """Stub call_claude_p to pop ``outcomes`` in order — a dict becomes a
    returned result, an Exception instance is raised. Returns the call log."""
    calls: list[str] = []

    def fake_call(prompt: str, **kwargs: object) -> ClaudePResult:
        calls.append(prompt)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return _result(outcome)

    monkeypatch.setattr("precis.utils.claude_p.call_claude_p", fake_call)
    return calls


def _unparseable() -> ClaudePUnparseableError:
    return ClaudePUnparseableError("claude -p returned no parseable JSON block")


def test_good_payload_parses_from_data(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_calls(monkeypatch, [_GOOD_PAYLOAD])
    extraction = extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 1
    assert len(extraction.atoms) == 2
    assert extraction.compound is not None


def test_unparseable_reply_retries_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fi176465-probe shape: prose (or an empty reply) makes the wrapper
    raise ClaudePUnparseableError — retried once, second call is good."""
    calls = _patch_calls(monkeypatch, [_unparseable(), _GOOD_PAYLOAD])
    extraction = extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 2
    assert len(extraction.atoms) == 2


def test_persistently_unparseable_raises_extraction_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twice-unparseable = a model stuck in prose mode — infra-grade, loud,
    NEVER a silent NO-CLAIM (the exact defect this round exists to kill)."""
    calls = _patch_calls(monkeypatch, [_unparseable(), _unparseable()])
    with pytest.raises(ExtractionUnavailable):
        extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 2


def test_invented_schema_retries_as_empty_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fi176399-rep0 shape: valid JSON in a self-invented schema parses
    to empty-empty — retried once, recovered by a good second reply."""
    calls = _patch_calls(monkeypatch, [_INVENTED_SCHEMA_PAYLOAD, _GOOD_PAYLOAD])
    extraction = extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 2
    assert len(extraction.atoms) == 2


def test_repeat_empty_empty_is_accepted_as_no_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two empty-empty replies = a genuine NO-CLAIM (pointer prose), not an
    infinite retry: exactly two calls, empty extraction returned."""
    calls = _patch_calls(monkeypatch, [_EMPTY_PAYLOAD, _EMPTY_PAYLOAD])
    extraction = extract_claim_strict_haiku("See refs. [12-14].")
    assert len(calls) == 2
    assert extraction.is_empty
    assert extraction.not_claims == ()


def test_reasoned_rejection_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty ``claims`` with populated ``not_claims`` is a considered
    verdict (the fi176468 shape) — accepted on the first call."""
    calls = _patch_calls(monkeypatch, [_NOT_CLAIM_PAYLOAD])
    extraction = extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 1
    assert extraction.is_empty
    assert len(extraction.not_claims) == 1


def test_timeout_raises_immediately_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout / spawn failure carries ``returncode is None`` — raised as
    ExtractionUnavailable on the FIRST call, never retried (retrying a
    240 s timeout would double the stall for nothing)."""
    calls = _patch_calls(
        monkeypatch, [ClaudeProcessError("claude -p timed out"), _GOOD_PAYLOAD]
    )
    with pytest.raises(ExtractionUnavailable):
        extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 1


def test_fast_nonzero_exit_retries_once_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first-local-canary shape (2026-08-14): the CLI intermittently
    exits 1 with empty stderr. A real ``returncode`` means the process ran
    and failed fast — retried once (after a load backoff, zeroed here),
    second call good."""
    monkeypatch.setattr("precis.taproot.canon._FLAKE_RETRY_BACKOFF_S", 0.0)
    flake = ClaudeProcessError("claude -p exited 1: ", returncode=1)
    calls = _patch_calls(monkeypatch, [flake, _GOOD_PAYLOAD])
    extraction = extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 2
    assert len(extraction.atoms) == 2


def test_persistent_nonzero_exit_raises_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("precis.taproot.canon._FLAKE_RETRY_BACKOFF_S", 0.0)
    calls = _patch_calls(
        monkeypatch,
        [
            ClaudeProcessError("claude -p exited 1: ", returncode=1),
            ClaudeProcessError("claude -p exited 1: ", returncode=1),
        ],
    )
    with pytest.raises(ExtractionUnavailable):
        extract_claim_strict_haiku(_SENTENCE)
    assert len(calls) == 2


def test_empty_input_short_circuits_without_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_calls(monkeypatch, [])
    extraction = extract_claim_strict_haiku("   ")
    assert calls == []
    assert extraction.is_empty
