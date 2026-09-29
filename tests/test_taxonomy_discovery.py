"""Tests for :mod:`precis.taxonomy.discovery` — stage 2, open-vocabulary
model discovery over stage-1 mentions.

DB-free and network-free throughout: :func:`~precis.taxonomy.discovery.discover`
is exercised with a fake :class:`~precis.taxonomy.discovery.DiscoveryClient`
that never leaves the process, and :func:`~precis.taxonomy.discovery.router_client`
itself (the only thing that touches the router) is not called here.
"""

from __future__ import annotations

import json
from typing import Any

from precis.taxonomy.config import CampaignConfig
from precis.taxonomy.discovery import (
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
