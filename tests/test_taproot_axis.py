"""Taproot Phase-2 slice 2a — the TAPROOT claim/review classifier (open #11).

`data/axes/taproot.yaml` is a ref-level axis over `finding` refs, driven by the
generic `workers/axis_pass.py` runner (no bespoke worker code). These tests
pin the wiring with a fake dispatch — the LLM *quality* of the prompt is a
host-native spot-check, not the offline gate (live-model tests can't run in
the container, whose `claude` CLI is unauthenticated).

DB-backed (real `refs`/`chunks`/`ref_tags` via the `store` fixture); no
network. Mirrors `tests/test_axis_pass.py`'s shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.errors import BadInput
from precis.store.types import Tag
from precis.workers.axis_pass import discover_axis_ids, run_axis_pass
from tests.workers._helpers import seed_chunk, seed_ref


class _FakeClient:
    """Records prompts; always answers with a fixed axis value."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=f'{{"value": "{self.value}"}}', total_tokens=5)


# A finding's body is flowing prose (the claim + setup); taproot reads
# title + this first ord>=0 chunk via axis_pass._build_ref_prompt.
_CLAIM_BODY = (
    "Pd/C catalyzes Suzuki coupling at room temperature with a mild base, "
    "reaching high yields across the aryl halides tested."
)
_REVIEW_BODY = (
    "The acronym MOF is used in the introduction before it is expanded, and "
    "the 500 S/cm figure in section 3 is asserted without a supporting cite."
)


def _ref_tag(store: Any, ref_id: int, ns: str) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s",
            (ref_id, ns),
        ).fetchone()
    return row[0] if row else None


def _seed_finding(store: Any, *, title: str, body: str) -> int:
    ref_id = seed_ref(store, title=title, kind="finding")
    seed_chunk(store, ref_id=ref_id, text=body, ord=0)
    return ref_id


# ── the classifier writes the discriminator + marker ────────────────────


def test_taproot_tags_a_grounded_claim(store: Any) -> None:
    ref_id = _seed_finding(
        store, title="Pd/C catalyzes Suzuki coupling at RT", body=_CLAIM_BODY
    )
    client = _FakeClient("claim")

    result = run_axis_pass(
        store, dispatch=client, axis_id="taproot", batch_size=10, ref_ids=[ref_id]
    )

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"claim": 1}}
    assert _ref_tag(store, ref_id, "TAPROOT") == "claim"
    assert _ref_tag(store, ref_id, "TAPROOTCASCADE") == "1"


def test_taproot_tags_an_editorial_note(store: Any) -> None:
    ref_id = _seed_finding(
        store, title="acronym unexpanded; missing citation", body=_REVIEW_BODY
    )
    client = _FakeClient("review")

    result = run_axis_pass(
        store, dispatch=client, axis_id="taproot", batch_size=10, ref_ids=[ref_id]
    )

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"review": 1}}
    assert _ref_tag(store, ref_id, "TAPROOT") == "review"


def test_taproot_only_applies_to_findings(store: Any) -> None:
    """`applies_to_kinds: [finding]` — a `paper` ref must not be claimed."""
    paper_id = seed_ref(store, title="A paper about catalysis", kind="paper")
    seed_chunk(store, ref_id=paper_id, text=_CLAIM_BODY, ord=0)
    client = _FakeClient("claim")

    result = run_axis_pass(
        store, dispatch=client, axis_id="taproot", batch_size=10, ref_ids=[paper_id]
    )

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert _ref_tag(store, paper_id, "TAPROOT") is None
    assert client.calls == []


def test_taproot_idempotent_same_version_skipped(store: Any) -> None:
    ref_id = _seed_finding(store, title="A grounded claim", body=_CLAIM_BODY)
    client = _FakeClient("claim")

    first = run_axis_pass(
        store, dispatch=client, axis_id="taproot", batch_size=10, ref_ids=[ref_id]
    )
    assert first["claimed"] == 1

    second = run_axis_pass(
        store, dispatch=client, axis_id="taproot", batch_size=10, ref_ids=[ref_id]
    )
    assert second == {"claimed": 0, "ok": 0, "failed": 0}
    assert len(client.calls) == 1  # only the first pass hit the model


def test_taproot_unparseable_output_stays_claimable(store: Any) -> None:
    """Fail-open: `taproot.yaml` omits `default_unknown`, so an unparseable /
    out-of-vocab read is `failed` (no tag, re-claimable once the attempt-lease
    cooldown lapses), never a mis-tag."""
    ref_id = _seed_finding(store, title="An ambiguous finding", body=_CLAIM_BODY)

    class _JunkClient:
        def complete(self, messages: list[dict[str, str]]) -> Any:
            return SimpleNamespace(text="sorry, I cannot decide", total_tokens=5)

    result = run_axis_pass(
        store,
        dispatch=_JunkClient(),
        axis_id="taproot",
        batch_size=10,
        ref_ids=[ref_id],
    )
    assert result == {"claimed": 1, "ok": 0, "failed": 1, "dist": {}}
    assert _ref_tag(store, ref_id, "TAPROOT") is None
    assert _ref_tag(store, ref_id, "TAPROOTCASCADE") is None

    # The failed attempt leaves a claim-time attempt lease braking an
    # immediate re-claim; expire it (rather than waiting out the cooldown)
    # to assert the row is claimable again — not permanently lost.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 minute' "
            "WHERE ref_id = %s",
            (ref_id,),
        )
        conn.commit()

    retried = run_axis_pass(
        store,
        dispatch=_FakeClient("claim"),
        axis_id="taproot",
        batch_size=10,
        ref_ids=[ref_id],
    )
    assert retried == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"claim": 1}}


# ── registration + vocab ────────────────────────────────────────────────


def test_taproot_is_a_discovered_axis() -> None:
    assert "taproot" in discover_axis_ids()


def test_taproot_closed_vocab_parses_and_rejects_typos() -> None:
    # Registered closed axis: valid values parse to a closed Tag on `finding`
    # (unrestricted kind), a bad value fails loud rather than silently.
    for value in ("claim", "review"):
        tag = Tag.parse_strict(f"TAPROOT:{value}", kind="finding")
        assert (tag.prefix, tag.value) == ("TAPROOT", value)

    with pytest.raises(BadInput):
        Tag.parse_strict("TAPROOT:bogus", kind="finding")
