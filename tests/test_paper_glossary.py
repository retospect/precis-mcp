"""Tests for the per-paper inferred glossary pass (reading-prep loop, slice 1).

Pure helpers (prompt / parse / clean / render) run everywhere. The end-to-end
pass runs against real PG (the ``store`` fixture) with a fake LLM client — no
network — so it exercises the claim SQL, the ``card_glossary`` negative-ord
write, and idempotency.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from precis.workers.paper_glossary import (
    CHUNK_KIND,
    GLOSSARY_VERSION,
    _build_prompt,
    _clean_clusters,
    _extract_json,
    _render_glossary,
    run_paper_glossary_pass,
)

_GLOSSARY_JSON = (
    '{"clusters":[{"name":"Methods","terms":[{"term":"DFT",'
    '"definition":"Density Functional Theory, an electronic-structure method.",'
    '"note":"used to model the device"}]},'
    '{"name":"Devices","terms":[{"term":"GNR-FET",'
    '"definition":"Graphene nanoribbon field-effect transistor.",'
    '"note":"the studied device"}]}]}'
)


class _FakeClient:
    """Records calls; returns a fixed completion text (like ``LlmClient``)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=7)


# ── pure helpers ───────────────────────────────────────────────────────


class TestPure:
    def test_extract_json_plain_and_embedded(self) -> None:
        assert _extract_json('{"a": 1}') == {"a": 1}
        assert _extract_json('junk before {"a": 1} junk after') == {"a": 1}
        assert _extract_json("not json at all") is None
        assert _extract_json("") is None
        assert _extract_json("[1, 2, 3]") is None  # a list is not a glossary object

    def test_clean_clusters_drops_empties_and_strips(self) -> None:
        raw = {
            "clusters": [
                {"name": " Methods ", "terms": [{"term": " DFT ", "definition": "x"}]},
                {"name": "Empty", "terms": [{"term": ""}]},  # no usable term
                {"name": "AlsoEmpty", "terms": []},
                "garbage",  # not a dict
            ]
        }
        out = _clean_clusters(raw)
        assert len(out) == 1
        assert out[0]["name"] == "Methods"
        assert out[0]["terms"][0] == {"term": "DFT", "definition": "x", "note": ""}

    def test_clean_clusters_handles_missing(self) -> None:
        assert _clean_clusters(None) == []
        assert _clean_clusters({}) == []

    def test_render_glossary(self) -> None:
        clusters = _clean_clusters(_extract_json(_GLOSSARY_JSON))
        text = _render_glossary(clusters)
        assert "## Methods" in text
        assert "**DFT** — Density Functional Theory" in text
        assert "_used to model the device_" in text
        assert "**GNR-FET**" in text

    def test_render_glossary_term_without_note_or_def(self) -> None:
        text = _render_glossary(
            [{"name": "", "terms": [{"term": "X", "definition": "", "note": ""}]}]
        )
        assert text == "**X**"

    def test_build_prompt_includes_all_signals(self) -> None:
        prompt = _build_prompt(
            title="A study of GNR-FET devices",
            abstract="We model the transistor.",
            defined={"DFT": "Density Functional Theory"},
            undefined=["GNR-FET", "FET"],
            keywords=["graphene nanoribbon", "band gap"],
        )
        assert "A study of GNR-FET devices" in prompt
        assert "DFT: Density Functional Theory" in prompt
        assert "GNR-FET, FET" in prompt
        assert "graphene nanoribbon" in prompt
        assert '"clusters"' in prompt  # the JSON schema instruction


# ── end-to-end pass (real PG, fake client) ─────────────────────────────


def _seed_paper(store: Any, title: str, body: str) -> int:
    from tests.workers._helpers import seed_chunk, seed_ref

    ref_id = seed_ref(store, title=title)
    seed_chunk(store, ref_id=ref_id, text=body, ord=0)
    return ref_id


def _glossary_chunk(store: Any, ref_id: int) -> tuple[int, str, dict] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord, text, meta FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (ref_id, CHUNK_KIND),
        ).fetchone()
    return (int(row[0]), row[1], row[2]) if row else None


class TestPass:
    def test_writes_glossary_chunk(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "A study of GNR-FET devices",
            "We used Density Functional Theory (DFT) to model the GNR-FET.",
        )
        client = _FakeClient(_GLOSSARY_JSON)

        result = run_paper_glossary_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert len(client.calls) == 1
        got = _glossary_chunk(store, ref_id)
        assert got is not None
        ord_, text, meta = got
        assert ord_ == -1000
        assert meta["glossary_version"] == GLOSSARY_VERSION
        assert meta["term_count"] == 2
        assert "**DFT**" in text and "Density Functional Theory" in text

    def test_idempotent_not_reclaimed(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "Study of DFT on a GNR-FET",
            "We used Density Functional Theory (DFT) here.",
        )
        client = _FakeClient(_GLOSSARY_JSON)
        first = run_paper_glossary_pass(
            store, client=client, batch_size=100, ref_ids=[ref_id]
        )
        assert first == {"claimed": 1, "ok": 1, "failed": 0}

        # Second pass: the fresh-version glossary exists → this ref is NOT
        # re-claimed (nothing to do, no LLM call).
        second = run_paper_glossary_pass(
            store, client=client, batch_size=100, ref_ids=[ref_id]
        )
        assert second == {"claimed": 0, "ok": 0, "failed": 0}
        assert len(client.calls) == 1  # only the first pass called the model
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
                (ref_id, CHUNK_KIND),
            ).fetchone()[0]
        assert n == 1  # exactly one glossary chunk, not duplicated

    def test_empty_candidates_writes_marker(self, store: Any) -> None:
        # Plain lowercase prose, generic title → no abbrevs, no acronyms, no
        # keywords → a version marker is written so it is not re-claimed forever.
        ref_id = _seed_paper(
            store, "a quiet note", "there is nothing here worth abbreviating at all."
        )
        client = _FakeClient(_GLOSSARY_JSON)

        result = run_paper_glossary_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 1, "failed": 0}
        assert client.calls == []  # no candidate terms → no LLM call
        got = _glossary_chunk(store, ref_id)
        assert got is not None
        _ord, _text, meta = got
        assert meta["glossary_version"] == GLOSSARY_VERSION
        assert meta["term_count"] == 0

    def test_unparseable_output_is_failed_no_write(self, store: Any) -> None:
        ref_id = _seed_paper(
            store,
            "Study of DFT devices",
            "We used Density Functional Theory (DFT) here.",
        )
        client = _FakeClient("sorry, I cannot help with that")

        result = run_paper_glossary_pass(
            store, client=client, batch_size=10, ref_ids=[ref_id]
        )

        assert result == {"claimed": 1, "ok": 0, "failed": 1}
        # No glossary chunk written → the ref stays claimable for a retry.
        assert _glossary_chunk(store, ref_id) is None
