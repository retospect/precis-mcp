"""Tests for :mod:`precis.taxonomy.discovery` — stage 2, open-vocabulary
model discovery over stage-1 mentions.

DB-free and network-free throughout: :func:`~precis.taxonomy.discovery.discover`
is exercised with a fake :class:`~precis.taxonomy.discovery.DiscoveryClient`
that never leaves the process, and :func:`~precis.taxonomy.discovery.router_client`
itself (the only thing that touches the router) is not called here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.discovery import (
    _MEASURAND_MAX_WORDS,
    CallRecord,
    Reply,
    _RouterDiscoveryClient,
    build_prompt,
    discover,
    parse_response,
    split_halves,
)
from precis.taxonomy.types import (
    Anchor,
    DiscoveredTerm,
    Half,
    Mention,
    Snapshot,
    Thresholds,
)

# ── fixtures ──


def _snapshot() -> Snapshot:
    return Snapshot(
        source="test",
        row_count=2,
        sha256="deadbeef",
        pulled_at="2026-09-28T00:00:00Z",
        text_field="text",
        ref_field="ref_id",
    )


def _config(**overrides: Any) -> CampaignConfig:
    kwargs: dict[str, Any] = {
        "campaign": "test-campaign",
        "config_version": 1,
        "snapshot": _snapshot(),
        "snapshot_path": None,
        "thresholds": Thresholds(),
        "categorical_qualifiers": {"feed": ("no-gas", "nitrite")},
        "site_classes": ("edge", "terrace"),
    }
    kwargs.update(overrides)
    from pathlib import Path

    kwargs["snapshot_path"] = Path()
    return CampaignConfig(**kwargs)


def _mention(
    *, ref_id: int = 1, literal: str = "1.2 V", raw_unit: str | None = "V"
) -> Mention:
    return Mention(
        anchor=Anchor(source_ref_id=ref_id, start=0, end=len(literal)),
        kind="value",
        literal=literal,
        raw_unit=raw_unit,
        context=f"...{literal} vs RHE...",
    )


class FakeClient:
    """Records every prompt it is handed; never touches a network."""

    def __init__(self, payloads: list[str] | None = None, payload: str | None = None):
        self.calls: list[str] = []
        self._payloads = payloads or []
        self._payload = payload
        self._n = 0

    def complete_json(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self._payload is not None:
            return self._payload
        result = self._payloads[self._n]
        self._n += 1
        return result


class RaisingClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, prompt: str) -> str:
        self.calls += 1
        raise RuntimeError("model unavailable")


# ── split_halves ──


def test_split_halves_deterministic_for_a_salt() -> None:
    ids = [1, 2, 3, 42, 999]
    first = split_halves(ids, salt="s1")
    second = split_halves(ids, salt="s1")
    assert first == second


def test_split_halves_changes_with_salt() -> None:
    ids = list(range(200))
    a = split_halves(ids, salt="salt-a")
    b = split_halves(ids, salt="salt-b")
    assert a != b


def test_split_halves_stable_under_reordering() -> None:
    ids = [5, 1, 9, 3, 7]
    forward = split_halves(ids, salt="s1")
    backward = split_halves(list(reversed(ids)), salt="s1")
    assert forward == backward


def test_split_halves_roughly_balanced() -> None:
    ids = list(range(1000))
    halves = split_halves(ids, salt="balance-check")
    a_count = sum(1 for half in halves.values() if half == "A")
    fraction = a_count / len(ids)
    assert 0.45 <= fraction <= 0.55


# ── build_prompt ──


def test_build_prompt_contains_mention_literals_and_indices() -> None:
    mentions = [
        _mention(literal="1.2 V"),
        _mention(literal="34 mA cm-2", raw_unit=None),
    ]
    prompt = build_prompt(
        "The catalyst reached 1.2 V at 34 mA cm-2.", mentions, _config()
    )
    assert "[0]" in prompt
    assert "1.2 V" in prompt
    assert "[1]" in prompt
    assert "34 mA cm-2" in prompt


def test_build_prompt_has_no_candidate_measurand_menu() -> None:
    mentions = [_mention()]
    prompt = build_prompt("sentence", mentions, _config())
    # names from norr-her-meta.md step 2's hand-written baseline list — none
    # of them may appear, since a menu would make discovery circular.
    for banned in ("Faradaic efficiency", "overpotential", "Tafel slope", "yield rate"):
        assert banned.lower() not in prompt.lower()


def test_build_prompt_qualifier_hints_kept_separate_from_measurand_field() -> None:
    mentions = [_mention()]
    prompt = build_prompt("sentence", mentions, _config())
    assert "QUALIFIER" in prompt
    assert "no-gas" in prompt
    assert "edge" in prompt


def test_build_prompt_unchanged_for_unchanged_input() -> None:
    mentions = [_mention()]
    config = _config()
    assert build_prompt("sentence", mentions, config) == build_prompt(
        "sentence", mentions, config
    )


# ── parse_response: well-formed ──


def test_parse_response_well_formed_yields_one_term_per_mention() -> None:
    mentions = [_mention(literal="1.2 V"), _mention(literal="0.85 V", raw_unit="V")]
    payload = json.dumps(
        [
            {"index": 0, "measurand": "applied potential"},
            {"index": 1, "measurand": "limiting potential", "reference_state": "RHE"},
        ]
    )
    terms, warnings = parse_response(payload, mentions, "A")
    assert warnings == ()
    assert len(terms) == 2
    assert all(isinstance(t, DiscoveredTerm) for t in terms)
    assert all(t.half == "A" for t in terms)
    assert terms[0].measurand == "applied potential"
    assert terms[0].mention is mentions[0]
    assert terms[1].reference_state == "RHE"


# ── parse_response: defensive paths ──


def test_parse_response_non_json_produces_warning_and_no_terms() -> None:
    terms, warnings = parse_response("not json at all", [_mention()], "A")
    assert terms == ()
    assert len(warnings) == 1
    assert "json" in warnings[0].lower()


def test_parse_response_strips_a_markdown_fence() -> None:
    payload = "```json\n" + json.dumps([{"index": 0, "measurand": "x"}]) + "\n```"
    terms, warnings = parse_response(payload, [_mention()], "B")
    assert len(terms) == 1
    assert terms[0].measurand == "x"
    assert warnings == ()


def test_parse_response_missing_index_warns_and_skips() -> None:
    payload = json.dumps([{"measurand": "x"}])
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert terms == ()
    assert any("index" in w.lower() for w in warnings)


def test_parse_response_out_of_range_index_warns_and_skips() -> None:
    payload = json.dumps([{"index": 7, "measurand": "x"}])
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert terms == ()
    assert any("out of range" in w.lower() for w in warnings)


def test_parse_response_empty_measurand_warns_and_skips() -> None:
    payload = json.dumps([{"index": 0, "measurand": ""}])
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert terms == ()
    assert any("measurand" in w.lower() for w in warnings)


def test_parse_response_never_fabricates_a_row() -> None:
    # a row is present for index 0 but it is malformed in every case above;
    # no DiscoveredTerm may appear that the model didn't actually supply.
    payload = json.dumps([{"index": 0}])  # no measurand key at all
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert terms == ()
    assert warnings


# ── discover ──


def test_discover_never_touches_network_and_calls_once_per_row() -> None:
    mentions_by_ref = {1: [_mention(ref_id=1)], 2: [_mention(ref_id=2, literal="5 mA")]}
    rows = [
        {"ref_id": 1, "text": "sentence one"},
        {"ref_id": 2, "text": "sentence two"},
    ]
    client = FakeClient(payload=json.dumps([{"index": 0, "measurand": "current"}]))
    halves: dict[int, Half] = {1: "A", 2: "B"}
    terms, warnings = discover(rows, mentions_by_ref, _config(), client, halves=halves)
    assert len(client.calls) == 2
    assert len(terms) == 2
    assert {t.half for t in terms} == {"A", "B"}
    assert warnings == ()


def test_discover_skips_rows_with_no_mentions_without_a_call() -> None:
    rows = [{"ref_id": 1, "text": "sentence"}]
    client = FakeClient(payload=json.dumps([]))
    terms, warnings = discover(rows, {}, _config(), client, halves={1: "A"})
    assert terms == ()
    assert client.calls == []


# ── AC2: halves are carried through, and a failing client surfaces warnings ──


def test_discover_tags_terms_by_half_for_downstream_stability() -> None:
    """The stability computation itself lives in select.py; this only proves
    discover's output carries what that computation needs — mention-weighted
    A/B vocabulary, taggable by :attr:`DiscoveredTerm.half`."""
    mentions_by_ref = {
        1: [_mention(ref_id=1, literal="1.2 V")],
        2: [_mention(ref_id=2, literal="0.9 V")],
    }
    rows = [{"ref_id": 1, "text": "s1"}, {"ref_id": 2, "text": "s2"}]
    client = FakeClient(
        payloads=[
            json.dumps([{"index": 0, "measurand": "widget-alpha"}]),
            json.dumps([{"index": 0, "measurand": "widget-beta"}]),
        ]
    )
    terms, _warnings = discover(
        rows, mentions_by_ref, _config(), client, halves={1: "A", 2: "B"}
    )
    by_half = {t.half: t.measurand for t in terms}
    assert by_half == {"A": "widget-alpha", "B": "widget-beta"}
    # little shared vocabulary between the two halves is exactly the input a
    # stability threshold is computed over downstream.
    assert by_half["A"] != by_half["B"]


def test_discover_surfaces_warnings_instead_of_crashing_on_every_call_failing() -> None:
    mentions_by_ref = {1: [_mention(ref_id=1)], 2: [_mention(ref_id=2)]}
    rows = [{"ref_id": 1, "text": "s1"}, {"ref_id": 2, "text": "s2"}]
    client = RaisingClient()
    terms, warnings = discover(
        rows, mentions_by_ref, _config(), client, halves={1: "A", 2: "B"}
    )
    assert terms == ()
    assert client.calls == 2
    assert len(warnings) == 2
    assert all("failed" in w.lower() for w in warnings)


# ── measurand SHAPE: the first probe's 0.046 A/B overlap ──
#
# The 2026-09-29 probe asked only for "what is being measured, in your own
# words" and got 166 distinct strings over 204 rows. Each string below is a
# real one from that run's `discovered.jsonl`. The vocabulary stays open —
# these tests pin the *shape* constraints, and the menu test above still
# forbids naming a candidate measurand anywhere in the prompt.


def test_build_prompt_caps_the_measurand_length() -> None:
    prompt = build_prompt("sentence", [_mention()], _config())
    assert str(_MEASURAND_MAX_WORDS) in prompt
    assert "at most" in prompt


def test_build_prompt_names_the_field_each_folded_in_part_belongs_in() -> None:
    """The probe's worst measurands each had an empty field of their own to
    go in: `... at which the yield rate was measured` had
    required_conditions, `... normalized to electrode area` had
    normalisation_basis. The prompt has to say so, per field."""
    prompt = build_prompt("sentence", [_mention()], _config())
    for field in (
        "required_conditions",
        "normalisation_basis",
        "reference_state",
        "convention",
        "subject_label",
    ):
        assert prompt.count(field) >= 2, (
            f"{field} should appear both as a redirect and as a JSON key"
        )


def test_build_prompt_worked_example_is_from_another_field() -> None:
    """A worked example shows the shape; it must not seed the vocabulary.
    This one is thermal transport — a field norr-her-meta does not cover —
    which is also why the no-menu test above still passes."""
    prompt = build_prompt("sentence", [_mention()], _config())
    assert "thermal conductivity" in prompt
    assert "do not reuse these words" in prompt


def test_build_prompt_offers_a_decline_for_a_label() -> None:
    """Blocker 3: the probe returned `compositional index X` and Miller
    indices as measurands because the prompt gave it no way to say no."""
    prompt = build_prompt("sentence", [_mention()], _config())
    assert "skip_reason" in prompt
    assert "facet index" in prompt


def test_parse_response_declined_row_is_a_decline_not_a_failure() -> None:
    payload = json.dumps(
        [
            {
                "index": 0,
                "measurand": None,
                "skip_reason": "Miller index identifying a facet, not a measurement",
            }
        ]
    )
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert terms == (), "a declined mention produces no term"
    assert len(warnings) == 1
    assert "declined" in warnings[0]
    assert "Miller index" in warnings[0], "the reason is carried, for audit"
    assert "empty or missing" not in warnings[0], (
        "a working prompt must not read as a broken one in the warning list"
    )


def test_parse_response_null_measurand_without_a_reason_is_still_a_failure() -> None:
    """The decline is earned by supplying a reason. A bare null is the same
    non-answer it always was."""
    payload = json.dumps([{"index": 0, "measurand": None}])
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert terms == ()
    assert len(warnings) == 1
    assert "empty or missing measurand" in warnings[0]
    assert "declined" not in warnings[0]


def test_parse_response_keeps_an_over_cap_measurand_and_warns() -> None:
    """Verbatim from the probe. The row is KEPT: this warning's whole job is
    to count prompt non-compliance across a run, and dropping the row would
    destroy the string needed to fix the prompt."""
    long = (
        "applied electrode potential at which the yield rate and Faradaic "
        "efficiency were measured"
    )
    payload = json.dumps([{"index": 0, "measurand": long}])
    terms, warnings = parse_response(payload, [_mention()], "A")
    assert len(terms) == 1, "kept, not dropped"
    assert terms[0].measurand == long, "kept verbatim, not truncated"
    assert len(warnings) == 1
    assert "over the" in warnings[0]
    assert long in warnings[0], "the offending string is in the warning"


def test_parse_response_is_silent_at_the_cap() -> None:
    at_cap = " ".join(f"w{i}" for i in range(_MEASURAND_MAX_WORDS))
    terms, warnings = parse_response(
        json.dumps([{"index": 0, "measurand": at_cap}]), [_mention()], "A"
    )
    assert len(terms) == 1
    assert warnings == (), f"{_MEASURAND_MAX_WORDS} words is at the cap, not over it"


def test_parse_response_still_strips_and_counts_words_around_whitespace() -> None:
    terms, warnings = parse_response(
        json.dumps([{"index": 0, "measurand": "  cell   voltage \n"}]),
        [_mention()],
        "A",
    )
    assert terms[0].measurand == "cell   voltage", "outer whitespace only"
    assert warnings == (), "two words, however they are spaced"


# ── metering: one CallRecord per call, failed calls included ──


class MeteredClient:
    """Returns a metered :class:`Reply` for the first call and a bare string
    for the second — both shapes the protocol admits."""

    def __init__(self, payload: str) -> None:
        self.calls: list[str] = []
        self._payload = payload

    def complete_json(self, prompt: str) -> Reply | str:
        self.calls.append(prompt)
        if len(self.calls) == 1:
            return Reply(
                text=self._payload,
                model="claude-sonnet-5",
                cost_usd=0.0125,
                input_tokens=900,
                output_tokens=40,
                cache_read_tokens=850,
                cache_creation_tokens=0,
                duration_s=3.5,
            )
        return self._payload


def test_discover_records_every_call_with_prompt_hash_and_metering() -> None:
    mentions_by_ref = {1: [_mention(ref_id=1)], 2: [_mention(ref_id=2, literal="5 mA")]}
    rows = [
        {"ref_id": 1, "text": "sentence one"},
        {"ref_id": 2, "text": "sentence two"},
    ]
    payload = json.dumps([{"index": 0, "measurand": "current"}])
    client = MeteredClient(payload)
    halves: dict[int, Half] = {1: "A", 2: "B"}
    records: list[CallRecord] = []
    terms, warnings = discover(
        rows, mentions_by_ref, _config(), client, halves=halves, on_call=records.append
    )
    assert len(terms) == 2 and warnings == ()
    assert [r.ref_id for r in records] == [1, 2]
    assert [r.half for r in records] == ["A", "B"]
    for record, prompt in zip(records, client.calls, strict=True):
        assert (
            record.prompt_sha256 == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        )
        assert record.prompt_chars == len(prompt)
        assert record.payload == payload
        assert record.error is None
        assert record.duration_s >= 0
        assert record.terms == 1 and record.warnings == 0
    metered, bare = records
    assert (metered.cost_usd, metered.input_tokens, metered.output_tokens) == (
        0.0125,
        900,
        40,
    )
    assert (metered.cache_read_tokens, metered.cache_creation_tokens) == (850, 0)
    assert metered.model == "claude-sonnet-5"
    # A bare-string reply is an unmetered call: every metering field stays
    # None (unreported), never a fabricated zero.
    assert bare.cost_usd is None and bare.input_tokens is None
    assert bare.cache_read_tokens is None and bare.model is None


def test_discover_records_a_failed_call_with_its_error() -> None:
    mentions_by_ref = {1: [_mention(ref_id=1)]}
    rows = [{"ref_id": 1, "text": "sentence one"}]
    client = RaisingClient()
    records: list[CallRecord] = []
    terms, warnings = discover(
        rows,
        mentions_by_ref,
        _config(),
        client,
        halves={1: "A"},
        on_call=records.append,
    )
    assert terms == ()
    assert len(warnings) == 1 and "model unavailable" in warnings[0]
    (record,) = records
    assert record.payload is None
    assert record.error is not None and "model unavailable" in record.error
    assert record.terms == 0 and record.duration_s >= 0


def test_discover_records_parse_warnings_on_the_row() -> None:
    mentions_by_ref = {1: [_mention(ref_id=1)]}
    rows = [{"ref_id": 1, "text": "sentence one"}]
    client = FakeClient(payload="not json at all")
    records: list[CallRecord] = []
    discover(
        rows,
        mentions_by_ref,
        _config(),
        client,
        halves={1: "A"},
        on_call=records.append,
    )
    (record,) = records
    assert record.payload == "not json at all"
    assert record.error is None, "a reply that parses badly is not a failed call"
    assert record.terms == 0 and record.warnings >= 1


def test_call_record_to_json_carries_every_field() -> None:
    record = CallRecord(
        ref_id=7,
        half="B",
        prompt_sha256="ab" * 32,
        prompt_chars=1200,
        duration_s=2.25,
        payload="[]",
        model="m",
        cost_usd=0.01,
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_creation_tokens=4,
        terms=0,
        warnings=1,
    )
    data = record.to_json()
    assert set(data) == {
        "ref_id",
        "half",
        "prompt_sha256",
        "prompt_chars",
        "duration_s",
        "payload",
        "error",
        "model",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "terms",
        "warnings",
    }
    assert json.loads(json.dumps(data)) == data


class _FakeDispatch:
    """A ``DispatchClient`` stand-in: the router's ``LlmResult`` fields as
    attributes, no network."""

    def __init__(self, **fields: Any) -> None:
        self.fields = fields
        self.messages: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.messages.append(messages)
        return type("LlmResult", (), self.fields)()


def test_router_adapter_maps_the_llm_result_metering_onto_the_reply() -> None:
    dispatch = _FakeDispatch(
        text="[]",
        model="claude-sonnet-5",
        cost_usd=0.02,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=8,
        cache_creation_tokens=2,
        duration_s=1.5,
    )
    reply = _RouterDiscoveryClient(dispatch).complete_json("the prompt")
    assert dispatch.messages == [[{"role": "user", "content": "the prompt"}]]
    assert reply == Reply(
        text="[]",
        model="claude-sonnet-5",
        cost_usd=0.02,
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=8,
        cache_creation_tokens=2,
        duration_s=1.5,
    )


def test_router_adapter_leaves_unreported_metering_none() -> None:
    reply = _RouterDiscoveryClient(_FakeDispatch(text="[]")).complete_json("p")
    assert reply.text == "[]"
    assert reply.cost_usd is None and reply.cache_read_tokens is None


class _FlakyDispatch:
    """Raises on the first ``failures`` calls, then answers — the third
    probe's ``claude -p timed out after 120s`` tail (6 of 61 calls)."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("claude -p timed out after 120s")
        return type("LlmResult", (), {"text": "[]"})()


def test_router_adapter_retries_a_transport_failure_once() -> None:
    dispatch = _FlakyDispatch(failures=1)
    reply = _RouterDiscoveryClient(dispatch).complete_json("p")
    assert reply.text == "[]"
    assert dispatch.calls == 2


def test_router_adapter_raises_after_the_retry_budget() -> None:
    dispatch = _FlakyDispatch(failures=2)
    with pytest.raises(RuntimeError, match="timed out"):
        _RouterDiscoveryClient(dispatch).complete_json("p")
    assert dispatch.calls == 2

    strict = _FlakyDispatch(failures=1)
    with pytest.raises(RuntimeError, match="timed out"):
        _RouterDiscoveryClient(strict, retries=0).complete_json("p")
    assert strict.calls == 1
