"""Graph trust state through the verbs (slice 1; runtime/trust_verb.py).

The read surfaces (the trust line on ``get``, the edit receipt, the
compatibility report) and the producer each ingress stamps: an MCP agent's
own text is generated, a logged-in web user's typed note is human, a
worker door is its own producer unless a converted caller wraps it.
"""

from __future__ import annotations

import re

import pytest

from precis.errors import BadInput
from precis.runtime import PrecisRuntime
from precis.runtime.trust_verb import producer_kwargs
from precis.store import Store
from precis.store.revision_context import (
    RevisionContext,
    revision_context,
    trust_door,
)


def _put_memory(runtime: PrecisRuntime, text: str, **extra: object) -> int:
    out = runtime.dispatch("put", {"kind": "memory", "text": text, **extra})
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    return int(m.group(1))


def _sha(store: Store, kind: str, target_id: int) -> str | None:
    state = store.trust_state(kind, target_id)
    assert state is not None
    return state.sha


def _stamp(store: Store, kind: str, target_id: int) -> tuple[str, str, int]:
    state = store.trust_state(kind, target_id)
    assert state is not None
    return (state.origin, state.confidence, state.version)


def test_agent_put_is_generated_hunch_with_an_attributed_act(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "Cu(100) favours NH3 at low coverage")
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)
    (act,) = store.trust_acts_for("ref", mid)
    assert (act["action"], act["actor"], act["actor_type"]) == (
        "create",
        "agent",
        "agent",
    )


def test_get_renders_the_trust_line_with_the_sha(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "a note to read back")
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": mid})
    sha = _sha(store, "ref", mid)
    assert f"trust: generated · hunch · sha {sha}" in out


def test_get_marks_legacy_rows_unstamped(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="legacy note", meta={})
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": ref.id})
    assert "trust: unknown · unknown (unstamped)" in out


def test_considered_on_put_needs_a_reason_and_returns_a_receipt(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "checked against fi176861 twice",
            "__extras__": {
                "trust": {"confidence": "considered", "reason": "re-derived"}
            },
        },
    )
    m = re.search(r"\bme(\d+)\b", out)
    assert m, out
    mid = int(m.group(1))
    assert f"trust: ref {mid} recorded considered" in out
    assert _stamp(store, "ref", mid) == ("generated", "considered", 1)
    bare = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "no reason given",
            "__extras__": {"trust": {"confidence": "considered"}},
        },
    )
    assert "give a reason" in bare


def test_mcp_cannot_assert_human_origin(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "memory",
            "text": "I am a human",
            "__extras__": {"trust": {"origin": "human"}},
        },
    )
    assert "cannot assert human origin" in out


def test_payload_origin_overrides_the_kind_producer(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(
        runtime_with_store,
        "verbatim: 'the onset potential is -0.2 V'",
        __extras__={"trust": {"origin": "extracted"}},
    )
    assert _stamp(store, "ref", mid) == ("extracted", "hunch", 1)


def test_edit_trust_considers_demotes_and_refuses_stale(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "to be considered")
    sha = _sha(store, "ref", mid)
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "trust",
            "__extras__": {
                "trust": {
                    "confidence": "considered",
                    "reason": "read the source",
                    "expected_sha": sha,
                }
            },
        },
    )
    assert f"trust: ref {mid} hunch -> considered (generated)" in out, out
    assert "by agent (agent)" in out
    stale = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "trust",
            "__extras__": {
                "trust": {
                    "confidence": "hunch",
                    "reason": "second thoughts",
                    "expected_sha": "0" * 16,
                }
            },
        },
    )
    assert "stale act" in stale
    demoted = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": f"me{mid}",
            "mode": "trust",
            "__extras__": {
                "trust": {
                    "confidence": "hunch",
                    "reason": "source retracted",
                    "expected_sha": sha,
                }
            },
        },
    )
    assert "considered -> hunch" in demoted, demoted
    actions = [a["action"] for a in store.trust_acts_for("ref", mid)]
    assert actions == ["create", "consider", "demote"]


def test_edit_trust_refuses_origin_correction_over_mcp(
    runtime_with_store: PrecisRuntime,
) -> None:
    mid = _put_memory(runtime_with_store, "origin stays put")
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "trust",
            "__extras__": {
                "trust": {"origin": "extracted", "reason": "x", "expected_sha": "y"}
            },
        },
    )
    assert "operator acts" in out


def test_content_edit_through_the_verb_revises_to_hunch(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    mid = _put_memory(runtime_with_store, "first wording")
    sha = _sha(store, "ref", mid)
    runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": mid,
            "mode": "trust",
            "__extras__": {
                "trust": {
                    "confidence": "considered",
                    "reason": "ok",
                    "expected_sha": sha,
                }
            },
        },
    )
    runtime_with_store.dispatch(
        "edit",
        {"kind": "memory", "id": mid, "mode": "replace", "text": "second wording"},
    )
    assert _stamp(store, "ref", mid) == ("generated", "hunch", 1)


def test_web_human_ingress_stamps_human_for_typed_text(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    with revision_context(
        actor="user:reto", trust_origin="human", trust_actor_type="human"
    ):
        mid = _put_memory(runtime_with_store, "quick note from the browser")
    assert _stamp(store, "ref", mid) == ("human", "hunch", 1)
    (act,) = store.trust_acts_for("ref", mid)
    assert (act["actor"], act["actor_type"]) == ("user:reto", "human")


def test_human_ingress_does_not_relabel_fetched_or_unconverted_kinds() -> None:
    human = RevisionContext(trust_origin="human", trust_actor_type="human")
    # an acquisition keeps its producer, an unconverted kind is cleared
    assert producer_kwargs("put", "paper", "extracted", None, human) == {
        "trust_origin": "extracted"
    }
    assert producer_kwargs("put", "web", None, None, human) == {"trust_origin": ""}
    # the person's own text kind keeps human
    assert producer_kwargs("put", "memory", "generated", None, human) == {}


def test_unconverted_kind_sends_no_trust_context() -> None:
    assert producer_kwargs("put", "paper", None, None, None) == {}
    with pytest.raises(BadInput, match="no trust producer"):
        producer_kwargs(
            "put", "paper", None, {"confidence": "considered", "reason": "x"}, None
        )


def test_link_verb_stamps_the_link_and_edit_considers_it(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    a = _put_memory(runtime_with_store, "node a")
    b = _put_memory(runtime_with_store, "node b")
    runtime_with_store.dispatch(
        "link",
        {"kind": "memory", "id": a, "target": f"memory:{b}", "rel": "related-to"},
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT link_id, trust_origin, trust_confidence, trust_write_version "
            "  FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "   AND relation = 'related-to'",
            (a, b),
        ).fetchone()
    assert row is not None and row[1:] == ("generated", "hunch", 1)
    lid = int(row[0])
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": a,
            "mode": "trust",
            "__extras__": {
                "trust": {
                    "confidence": "considered",
                    "reason": "checked both ends",
                    "expected_sha": _sha(store, "link", lid),
                    "link": lid,
                }
            },
        },
    )
    assert f"trust: link {lid} hunch -> considered" in out, out


def test_worker_door_converts_unless_the_caller_is_converted(store: Store) -> None:
    @trust_door("computed")
    def _door() -> int:
        return store.insert_ref(
            kind="memory", slug=None, title="from a door", meta={}
        ).id

    assert _stamp(store, "ref", _door()) == ("computed", "hunch", 1)
    with revision_context(trust_origin="generated", trust_actor_type="agent"):
        inside = _door()
    assert _stamp(store, "ref", inside) == ("generated", "hunch", 1)


def test_unconverted_edit_of_a_stamped_ref_is_a_bad_input(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """A writer outside every context hitting version-1 content gets the
    trigger's named rule, not an internal error."""
    from precis.store._review_ops import trust_refusal

    mid = _put_memory(runtime_with_store, "stamped by the verb")
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        with store.pool.connection() as conn:
            conn.execute("UPDATE refs SET title = 'raw' WHERE ref_id = %s", (mid,))
    refusal = trust_refusal(exc.value)
    assert refusal is not None and "version 1" in refusal.cause
    assert refusal.next and "revision_context(trust_origin" in str(refusal.next)
