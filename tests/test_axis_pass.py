"""Tests for the generic axis classifier runner (ADR 0047 §3, ``workers/axis_pass.py``).

DB-backed (real ``refs``/``chunks``/``ref_tags``/``chunk_tags`` via the
``store`` fixture) with a fake LLM client — no network. Exercises real axis
YAMLs already in ``data/axes/``: ``domain`` (ref-level, no prereq),
``material`` (ref-level, ``prereq: [domain]``) and ``role3`` (chunk-level,
no prereq) — so the prereq-gate test pins the real, shipped
``material``-waits-for-``domain`` relationship rather than a synthetic one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.store.types import Tag
from precis.workers.axis_pass import run_axis_pass
from tests.workers._helpers import seed_chunk, seed_ref


class _FakeClient:
    """Records every prompt it's given; always answers with a fixed value."""

    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=f'{{"value": "{self.value}"}}', total_tokens=5)


_LONG_PARA = (
    "We synthesized a Pd(111) catalyst and measured its NO to NH3 selectivity "
    "across several electrolysis runs against the literature benchmark carefully."
)


def _ref_tag(store: Any, ref_id: int, ns: str) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = %s AND t.namespace = %s",
            (ref_id, ns),
        ).fetchone()
    return row[0] if row else None


def _chunk_tag(store: Any, ref_id: int, ord_: int, ns: str) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id "
            "JOIN chunks c ON c.chunk_id = ct.chunk_id "
            "WHERE c.ref_id = %s AND c.ord = %s AND t.namespace = %s",
            (ref_id, ord_, ns),
        ).fetchone()
    return row[0] if row else None


# ── ref-level axis, no prereq (domain) ──────────────────────────────────


def test_ref_level_axis_writes_ref_tags_not_chunk_tags(store: Any) -> None:
    ref_id = seed_ref(store, title="A study of Pd catalysts")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("chemistry")

    result = run_axis_pass(
        store, dispatch=client, axis_id="domain", batch_size=10, ref_ids=[ref_id]
    )

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"chemistry": 1}}
    assert _ref_tag(store, ref_id, "DOMAIN") == "chemistry"
    assert _ref_tag(store, ref_id, "DOMAINCASCADE") == "1"
    assert _chunk_tag(store, ref_id, 0, "DOMAIN") is None  # not written at chunk level


def test_ref_level_idempotent_current_version_skipped(store: Any) -> None:
    ref_id = seed_ref(store, title="A study of Pd catalysts")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("chemistry")

    first = run_axis_pass(
        store, dispatch=client, axis_id="domain", batch_size=10, ref_ids=[ref_id]
    )
    assert first["claimed"] == 1

    second = run_axis_pass(
        store, dispatch=client, axis_id="domain", batch_size=10, ref_ids=[ref_id]
    )
    assert second == {"claimed": 0, "ok": 0, "failed": 0}
    assert len(client.calls) == 1  # only the first pass called the model


def test_ref_level_version_bump_reclaims(store: Any) -> None:
    ref_id = seed_ref(store, title="A study of Pd catalysts")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("chemistry")

    first = run_axis_pass(
        store, dispatch=client, axis_id="domain", batch_size=10, ref_ids=[ref_id]
    )
    assert first["claimed"] == 1
    assert _ref_tag(store, ref_id, "DOMAINCASCADE") == "1"

    # Same axis version -> still skipped.
    still_skipped = run_axis_pass(
        store, dispatch=client, axis_id="domain", batch_size=10, ref_ids=[ref_id]
    )
    assert still_skipped["claimed"] == 0

    # Explicit version bump -> reclaimed even though the marker (v1) exists.
    client2 = _FakeClient("physics")
    bumped = run_axis_pass(
        store,
        dispatch=client2,
        axis_id="domain",
        batch_size=10,
        version="2",
        ref_ids=[ref_id],
    )
    assert bumped == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"physics": 1}}
    assert _ref_tag(store, ref_id, "DOMAIN") == "physics"  # replaced
    assert _ref_tag(store, ref_id, "DOMAINCASCADE") == "2"


# ── ref-level axis WITH prereq (material waits on domain) ───────────────


def test_prereq_gate_blocks_claim_until_prereq_tag_present(store: Any) -> None:
    ref_id = seed_ref(store, title="A study of CNTs")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("cnt")

    # No DOMAIN tag yet -> material is not eligible.
    blocked = run_axis_pass(
        store, dispatch=client, axis_id="material", batch_size=10, ref_ids=[ref_id]
    )
    assert blocked == {"claimed": 0, "ok": 0, "failed": 0}
    assert client.calls == []

    # Satisfy the prereq (as the `domain` axis itself would have written).
    with store.pool.connection() as conn:
        store.add_tag(
            ref_id, Tag.closed("DOMAIN", "chemistry"), set_by="agent", conn=conn
        )
        conn.commit()

    unblocked = run_axis_pass(
        store, dispatch=client, axis_id="material", batch_size=10, ref_ids=[ref_id]
    )
    assert unblocked == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"cnt": 1}}
    assert _ref_tag(store, ref_id, "MATERIAL") == "cnt"
    assert len(client.calls) == 1


def test_prereq_gate_two_axes_both_required(store: Any) -> None:
    """``transport`` needs BOTH ``domain`` and ``property`` — one alone
    must not unblock it."""
    ref_id = seed_ref(store, title="A study of point contacts")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("point-contact")

    with store.pool.connection() as conn:
        store.add_tag(
            ref_id, Tag.closed("DOMAIN", "physics"), set_by="agent", conn=conn
        )
        conn.commit()

    still_blocked = run_axis_pass(
        store, dispatch=client, axis_id="transport", batch_size=10, ref_ids=[ref_id]
    )
    assert still_blocked == {"claimed": 0, "ok": 0, "failed": 0}

    with store.pool.connection() as conn:
        store.add_tag(
            ref_id, Tag.closed("PROPERTY", "conductivity"), set_by="agent", conn=conn
        )
        conn.commit()

    unblocked = run_axis_pass(
        store, dispatch=client, axis_id="transport", batch_size=10, ref_ids=[ref_id]
    )
    assert unblocked == {
        "claimed": 1,
        "ok": 1,
        "failed": 0,
        "dist": {"point-contact": 1},
    }


# ── chunk-level axis (role3) ────────────────────────────────────────────


def test_chunk_level_axis_writes_chunk_tags_at_the_right_ord(store: Any) -> None:
    ref_id = seed_ref(store, title="A paper about catalysis")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("own")

    result = run_axis_pass(
        store, dispatch=client, axis_id="role3", batch_size=10, ref_ids=[ref_id]
    )

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"own": 1}}
    assert _chunk_tag(store, ref_id, 0, "ROLE3") == "own"
    assert _chunk_tag(store, ref_id, 0, "ROLE3CASCADE") == "1"
    assert _ref_tag(store, ref_id, "ROLE3") is None  # not written at ref level


def test_chunk_level_idempotent_and_version_bump(store: Any) -> None:
    ref_id = seed_ref(store, title="A paper about catalysis")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)
    client = _FakeClient("own")

    first = run_axis_pass(
        store, dispatch=client, axis_id="role3", batch_size=10, ref_ids=[ref_id]
    )
    assert first["claimed"] == 1

    second = run_axis_pass(
        store, dispatch=client, axis_id="role3", batch_size=10, ref_ids=[ref_id]
    )
    assert second == {"claimed": 0, "ok": 0, "failed": 0}

    client2 = _FakeClient("background")
    bumped = run_axis_pass(
        store,
        dispatch=client2,
        axis_id="role3",
        batch_size=10,
        version="2",
        ref_ids=[ref_id],
    )
    assert bumped == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"background": 1}}
    assert _chunk_tag(store, ref_id, 0, "ROLE3") == "background"
    assert _chunk_tag(store, ref_id, 0, "ROLE3CASCADE") == "2"


# ── failure path stays retryable ────────────────────────────────────────


# ── applies_when gate (orthogonal to, coexists with, prereq) ───────────


def test_applies_when_domain_in_blocks_out_of_list_value(store: Any) -> None:
    """``material``'s ``applies_when: domain_in: [chemistry, materials,
    eng]`` must reject a DOMAIN:bio ref even though DOMAIN's mere presence
    already satisfies material's bare ``prereq: [domain]`` check — the two
    gates are independent layers, both must pass."""
    ref_bio = seed_ref(store, title="A study of biomarkers")
    seed_chunk(store, ref_id=ref_bio, text=_LONG_PARA, ord=0)
    with store.pool.connection() as conn:
        store.add_tag(ref_bio, Tag.closed("DOMAIN", "bio"), set_by="agent", conn=conn)
        conn.commit()

    ref_chem = seed_ref(store, title="A study of catalysts")
    seed_chunk(store, ref_id=ref_chem, text=_LONG_PARA, ord=0)
    with store.pool.connection() as conn:
        store.add_tag(
            ref_chem, Tag.closed("DOMAIN", "chemistry"), set_by="agent", conn=conn
        )
        conn.commit()

    client = _FakeClient("cnt")
    result = run_axis_pass(
        store,
        dispatch=client,
        axis_id="material",
        batch_size=10,
        ref_ids=[ref_bio, ref_chem],
    )

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"cnt": 1}}
    assert _ref_tag(store, ref_chem, "MATERIAL") == "cnt"
    assert _ref_tag(store, ref_bio, "MATERIAL") is None  # blocked by domain_in


def test_applies_when_tags_any_gates_on_listed_tag(store: Any) -> None:
    """``move`` (the dream/memory axis) gates on
    ``applies_when: tags_any: [DREAM:speculative, DREAM:grounded]``."""
    dream_ref = seed_ref(store, title="dream 1", kind="memory")
    seed_chunk(store, ref_id=dream_ref, text=_LONG_PARA, ord=0)
    with store.pool.connection() as conn:
        store.add_tag(
            dream_ref,
            Tag.closed("DREAM", "speculative"),
            set_by="agent",
            conn=conn,
        )
        conn.commit()

    plain_ref = seed_ref(store, title="plain memory", kind="memory")
    seed_chunk(store, ref_id=plain_ref, text=_LONG_PARA, ord=0)

    client = _FakeClient("analogy")
    result = run_axis_pass(
        store,
        dispatch=client,
        axis_id="move",
        batch_size=10,
        ref_ids=[dream_ref, plain_ref],
    )

    assert result == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"analogy": 1}}
    assert _ref_tag(store, dream_ref, "MOVE") == "analogy"
    assert _ref_tag(store, plain_ref, "MOVE") is None  # not tags_any-eligible


def test_unparseable_output_is_failed_and_stays_claimable(store: Any) -> None:
    ref_id = seed_ref(store, title="A study of Pd catalysts")
    seed_chunk(store, ref_id=ref_id, text=_LONG_PARA, ord=0)

    class _JunkClient:
        def complete(self, messages: list[dict[str, str]]) -> Any:
            return SimpleNamespace(text="sorry, I cannot help", total_tokens=5)

    result = run_axis_pass(
        store, dispatch=_JunkClient(), axis_id="domain", batch_size=10, ref_ids=[ref_id]
    )
    assert result == {"claimed": 1, "ok": 0, "failed": 1, "dist": {}}
    assert _ref_tag(store, ref_id, "DOMAIN") is None
    assert _ref_tag(store, ref_id, "DOMAINCASCADE") is None

    # Untagged -> still claimable on retry.
    retry_client = _FakeClient("chemistry")
    retried = run_axis_pass(
        store,
        dispatch=retry_client,
        axis_id="domain",
        batch_size=10,
        ref_ids=[ref_id],
    )
    assert retried == {"claimed": 1, "ok": 1, "failed": 0, "dist": {"chemistry": 1}}
