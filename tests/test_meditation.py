"""Tests for the evening-meditation cast producer (reading-prep loop).

Pure helpers (walk ordering, script composition with a fake client) run
everywhere; `build_meditation` runs against real PG (the `store` fixture) with
seeded concepts + a fake client — no TTS, no audio deps.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.reading.meditation import (
    MEDITATION_VOICE,
    _walk_order,
    build_meditation,
    compose_script,
)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=5)


class TestWalk:
    def test_follows_edges_then_jumps(self) -> None:
        # 1-3 and 2-4 connected. From 1 → 3 (edge); 3's only nbr visited → jump to
        # 2 (min remaining) → 4 (edge). Smooth where possible, deterministic.
        adj = {1: {3}, 3: {1}, 2: {4}, 4: {2}}
        assert _walk_order([1, 2, 3, 4], adj) == [1, 3, 2, 4]

    def test_empty_and_singleton(self) -> None:
        assert _walk_order([], {}) == []
        assert _walk_order([7], {7: set()}) == [7]


class TestCompose:
    def test_script_prompts_with_concepts_in_order(self) -> None:
        client = _FakeClient("Settle in.\n\nBreathe.")
        script = compose_script(
            [("backprop", "reverse-mode autodiff"), ("chain rule", "derivative")],
            client=client,
            anchors=["the tide"],
        )
        assert script == "Settle in.\n\nBreathe."
        user = client.calls[0][1]["content"]
        assert "backprop" in user and "chain rule" in user
        assert "the tide" in user  # anchor woven into the prompt
        system = client.calls[0][0]["content"]
        assert "meditation" in system.lower()


class TestBuild:
    def test_creates_nidra_draft_with_paragraphs(self, store: Any) -> None:
        import uuid

        from precis.reading.promote import create_concept

        u = uuid.uuid4().hex[:8]
        co = f"med-{u}"
        a = create_concept(store, name=f"aa-{u}", definition="idea a", cohort=co)
        b = create_concept(store, name=f"bb-{u}", definition="idea b", cohort=co)
        store.add_link(src_ref_id=a, dst_ref_id=b, relation="analogy-of")
        client = _FakeClient(
            "Settle in and let go.\n\nHere is idea a, and idea b.\n\nDrift."
        )

        draft_id = build_meditation(
            store, client=client, name=f"evening-{u}", cohort=co
        )

        assert draft_id is not None
        with store.pool.connection() as conn:
            meta = conn.execute(
                "SELECT meta FROM refs WHERE ref_id=%s", (draft_id,)
            ).fetchone()[0]
            n_paras = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id=%s AND chunk_kind='paragraph'",
                (draft_id,),
            ).fetchone()[0]
        assert meta["cast"] == "nidra"
        assert meta["voice"] == MEDITATION_VOICE  # af_nicole
        assert n_paras >= 2  # script split into paragraphs on blank lines
        assert client.calls  # the model was consulted

    def test_too_few_concepts_returns_none(self, store: Any) -> None:
        import uuid

        from precis.reading.promote import create_concept

        u = uuid.uuid4().hex[:8]
        co = f"med-{u}"
        create_concept(store, name=f"solo-{u}", definition="only one", cohort=co)
        client = _FakeClient("unused")

        assert build_meditation(store, client=client, name=f"e-{u}", cohort=co) is None
        assert client.calls == []  # never called the model — nothing to walk
