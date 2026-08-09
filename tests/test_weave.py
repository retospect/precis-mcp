"""Tests for :func:`precis.quest.weave.weave_section` — rung 6d-2 of the
paper-writing pipeline (docs/backlog/paper-writing-pipeline.md §"Integrate —
the tick body" step 2, Weave).

DB-backed (real ``chunks``/``links``/``tags`` via the ``store`` fixture)
with two fake LLM clients — one for the claims extractor (rung 6c), one
for the weave prompt itself — no network. Mirrors ``tests/test_claims.py``
/ ``tests/test_citation_mint.py``'s fixture shape.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import precis.quest.weave as weave_module
from precis.quest.weave import weave_section
from precis.store._draft_ops import content_sha
from precis.store.types import Tag
from precis.utils import handle_registry
from tests.workers._helpers import seed_chunk


class _FakeClient:
    """Records calls; returns a fixed completion text (like ``LlmClient``)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        return SimpleNamespace(text=self._text, total_tokens=7)


class _SequentialClient:
    """Returns one fixed completion text per call, in call order.

    ``_FakeClient``'s single fixed text can't feed ``extract_claims`` a
    *distinct* completion per paper in a multi-paper batch (needed for the
    cross-paper-attribution test) — this does.
    """

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        idx = len(self.calls)
        self.calls.append(messages)
        text = self._texts[idx] if idx < len(self._texts) else self._texts[-1]
        return SimpleNamespace(text=text, total_tokens=7)


def _dossier_with_section(store: Any, slug: str) -> tuple[int, str]:
    """A ``draft`` with one scaffolded top-level heading; returns
    ``(dossier_ref_id, section_dc_handle)``."""
    ref = store.insert_ref(kind="draft", slug=slug, title="Dossier", meta={})
    handles = store.scaffold_sections(ref.id, [("Section One", None)])
    return ref.id, handles[0]


def _paper(store: Any, slug: str, title: str) -> int:
    return store.insert_ref(kind="paper", slug=slug, title=title, meta={}).id


def _tag_own(store: Any, ref_id: int, ord_: int) -> None:
    store.add_tag(ref_id, Tag.closed("ROLE3", "own"), pos=ord_, set_by="agent")


def _heading_ord(store: Any, chunk_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ord FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    return int(row[0])


def _seed_own_paper(
    store: Any, slug: str, title: str, claim_text: str
) -> tuple[int, str]:
    """A paper with one ``ROLE3:own`` chunk; returns ``(paper_ref_id,
    source_handle)``."""
    paper_id = _paper(store, slug, title)
    chunk_id = seed_chunk(store, ref_id=paper_id, text=claim_text, ord=0)
    _tag_own(store, paper_id, 0)
    source_handle = handle_registry.format_handle("paper", chunk_id, chunk=True)
    return paper_id, source_handle


def _claims_client(claim_text: str) -> _FakeClient:
    return _FakeClient(json.dumps([{"claim": claim_text, "source": 0}]))


def _weave_payload(
    section_text: str,
    index: int,
    disposition: str,
    claims_used: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps(
        {
            "section_text": section_text,
            "papers": [
                {
                    "index": index,
                    "disposition": disposition,
                    "claims_used": claims_used or [],
                }
            ],
        }
    )


class TestWeaveApplies:
    def test_body_chunk_citation_and_link(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv1")
        claim_text = "12% improvement over baseline."
        paper_id, source_handle = _seed_own_paper(
            store, "wv1-p1", "Paper One", claim_text
        )

        claims_client = _claims_client(claim_text)
        section_text = "Paper One reports a 12% improvement over baseline."
        weave_client = _FakeClient(
            _weave_payload(
                section_text,
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result["ok"] is True
        assert result["applied"] is True
        assert result["section_text_len"] == len(section_text)

        body_chunk = store.get_draft_chunk(result["body_handle"], kind="draft")
        assert body_chunk is not None
        assert body_chunk.text == section_text
        assert body_chunk.meta.get("weave_body") is True

        assert len(result["citation_ids"]) == 1
        cid = result["citation_ids"][0]
        citation_ref = store.get_ref(kind="citation", id=cid)
        assert citation_ref is not None
        assert citation_ref.meta["claim"] == claim_text

        heading = store.get_draft_chunk(section_handle, kind="draft")
        heading_ord = _heading_ord(store, heading.chunk_id)
        links = store.links_for(dossier_id, direction="in", relation="cited-in")
        assert any(
            link.src_ref_id == paper_id and link.dst_pos == heading_ord
            for link in links
        )

        assert result["papers"] == [
            {"ref_id": paper_id, "disposition": "cited-in", "citation_ids": [cid]}
        ]

    def test_reweave_edits_same_chunk_no_duplicate(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv2")
        claim_text = "A novel catalyst design."
        paper_id, source_handle = _seed_own_paper(
            store, "wv2-p1", "Paper Two", claim_text
        )
        claims_client = _claims_client(claim_text)

        first_text = "First composition of the section."
        weave_client_1 = _FakeClient(
            _weave_payload(
                first_text,
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )
        result_1 = weave_section(
            store,
            weave_client_1,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )
        assert result_1["ok"] is True

        second_text = "Second, re-woven composition of the section — longer text."
        weave_client_2 = _FakeClient(
            _weave_payload(
                second_text,
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )
        result_2 = weave_section(
            store,
            weave_client_2,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )
        assert result_2["ok"] is True

        # Same chunk identity (handle stable) — no duplicate woven body.
        assert result_2["body_handle"] == result_1["body_handle"]
        body_chunk = store.get_draft_chunk(result_2["body_handle"], kind="draft")
        assert body_chunk is not None
        assert body_chunk.text == second_text
        assert content_sha(first_text) != content_sha(second_text)

        heading = store.get_draft_chunk(section_handle, kind="draft")
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE parent_chunk_id = %s "
                "AND retired_at IS NULL AND meta->>'weave_body' = 'true'",
                (heading.chunk_id,),
            ).fetchone()[0]
        assert n == 1

    def test_human_authored_chunk_left_untouched(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv3")
        human_chunks = store.add_chunks(
            ref_id=dossier_id,
            chunk_kind="paragraph",
            text="A human wrote this paragraph by hand.",
            at={"into": section_handle},
        )
        human_handle = human_chunks[0].dc

        claim_text = "We further demonstrate a novel result."
        paper_id, source_handle = _seed_own_paper(
            store, "wv3-p1", "Paper Three", claim_text
        )
        claims_client = _claims_client(claim_text)
        weave_client = _FakeClient(
            _weave_payload(
                "Woven prose distinct from the human paragraph.",
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result["ok"] is True
        assert result["body_handle"] != human_handle

        human_chunk = store.get_draft_chunk(human_handle, kind="draft")
        assert human_chunk is not None
        assert human_chunk.text == "A human wrote this paragraph by hand."


class TestDispositions:
    def test_off_topic_for_adds_link_and_drops_matching_topic_tag(
        self, store: Any
    ) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv4")
        store.add_tag(dossier_id, Tag.open("topic:mof"), set_by="agent")

        paper_id = _paper(store, "wv4-p1", "Off Topic Paper")
        store.add_tag(paper_id, Tag.open("topic:mof"), set_by="agent")
        store.add_tag(paper_id, Tag.open("topic:unrelated"), set_by="agent")
        claims_client = _FakeClient("[]")  # no ROLE3:own chunks -> no claims anyway

        weave_client = _FakeClient(
            _weave_payload(
                "Section prose unaffected by the rejection.", 0, "off-topic-for"
            )
        )

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result["ok"] is True
        assert result["papers"] == [
            {"ref_id": paper_id, "disposition": "off-topic-for", "citation_ids": []}
        ]

        heading = store.get_draft_chunk(section_handle, kind="draft")
        heading_ord = _heading_ord(store, heading.chunk_id)
        links = store.links_for(dossier_id, direction="in", relation="off-topic-for")
        assert any(
            link.src_ref_id == paper_id and link.dst_pos == heading_ord
            for link in links
        )

        remaining = {str(t) for t in store.tags_for(paper_id)}
        assert "topic:mof" not in remaining
        assert "topic:unrelated" in remaining  # non-matching topic tag survives

    def test_superseded_in_adds_link_with_no_citation(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv5")
        claim_text = "An earlier, now-subsumed finding."
        paper_id, source_handle = _seed_own_paper(
            store, "wv5-p1", "Superseded Paper", claim_text
        )
        claims_client = _claims_client(claim_text)
        weave_client = _FakeClient(
            _weave_payload("Section prose unaffected.", 0, "superseded-in")
        )

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result["ok"] is True
        assert result["citation_ids"] == []
        assert result["papers"] == [
            {"ref_id": paper_id, "disposition": "superseded-in", "citation_ids": []}
        ]

        heading = store.get_draft_chunk(section_handle, kind="draft")
        heading_ord = _heading_ord(store, heading.chunk_id)
        links = store.links_for(dossier_id, direction="in", relation="superseded-in")
        assert any(
            link.src_ref_id == paper_id and link.dst_pos == heading_ord
            for link in links
        )


class TestDryRunAndParseFailure:
    def test_dry_run_writes_nothing(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv6")
        claim_text = "A dry-run claim."
        paper_id, source_handle = _seed_own_paper(
            store, "wv6-p1", "Dry Run Paper", claim_text
        )
        claims_client = _claims_client(claim_text)
        weave_client = _FakeClient(
            _weave_payload(
                "Proposed but not applied section text.",
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
            dry_run=True,
        )

        assert result["ok"] is True
        assert result["applied"] is False
        assert result["section_text"] == "Proposed but not applied section text."
        assert result["papers"] == [
            {
                "ref_id": paper_id,
                "disposition": "cited-in",
                "claims_used": [
                    {
                        "text": claim_text,
                        "source_handle": source_handle,
                        "source_ord": 0,
                    }
                ],
            }
        ]
        assert "body_handle" not in result
        assert "citation_ids" not in result

        heading = store.get_draft_chunk(section_handle, kind="draft")
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE parent_chunk_id = %s "
                "AND retired_at IS NULL",
                (heading.chunk_id,),
            ).fetchone()[0]
        assert n == 0  # no body chunk written

        with store.pool.connection() as conn:
            citations = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'citation'"
            ).fetchone()[0]
        assert citations == 0

        links = store.links_for(dossier_id, direction="in")
        assert links == []

    def test_unparseable_model_output_no_writes(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv7")
        claim_text = "A claim that should never get woven."
        paper_id, _source_handle = _seed_own_paper(
            store, "wv7-p1", "Unparseable Paper", claim_text
        )
        claims_client = _claims_client(claim_text)
        weave_client = _FakeClient("sorry, I cannot help with that")

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result == {"ok": False, "error": "unparseable", "applied": False}

        heading = store.get_draft_chunk(section_handle, kind="draft")
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE parent_chunk_id = %s "
                "AND retired_at IS NULL",
                (heading.chunk_id,),
            ).fetchone()[0]
        assert n == 0

        links = store.links_for(dossier_id, direction="in")
        assert links == []

    def test_empty_section_text_no_writes(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv13")
        claim_text = "A claim that should never get woven."
        paper_id, _source_handle = _seed_own_paper(
            store, "wv13-p1", "Empty Text Paper", claim_text
        )
        claims_client = _claims_client(claim_text)
        weave_client = _FakeClient(_weave_payload("   \n  ", 0, "cited-in"))

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result == {
            "ok": False,
            "error": "empty_section_text",
            "applied": False,
        }

        heading = store.get_draft_chunk(section_handle, kind="draft")
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM chunks WHERE parent_chunk_id = %s "
                "AND retired_at IS NULL",
                (heading.chunk_id,),
            ).fetchone()[0]
        assert n == 0  # no body chunk written — the section is not blanked

        with store.pool.connection() as conn:
            citations = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'citation'"
            ).fetchone()[0]
        assert citations == 0

        links = store.links_for(dossier_id, direction="in")
        assert links == []


class TestCrossPaperAttribution:
    """Rung 6d-2 review fix: in a multi-paper batch the model can echo one
    paper's ``source_handle``/``source_ord`` under a *different* paper's
    entry — never mint a citation for a claim whose ``source_ord`` isn't
    among that paper's own excerpts."""

    def test_cross_paper_source_ord_is_not_minted(self, store: Any) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv14")

        # Paper zero: a single own chunk at ord 0.
        claim0 = "Paper zero's own claim."
        paper0, _source_handle0 = _seed_own_paper(
            store, "wv14-p0", "Paper Zero", claim0
        )

        # Paper one: five filler chunks (ords 0-4), its own chunk at ord 5 —
        # so ord 5 is a real, valid source_ord for paper one but NOT for
        # paper zero (whose only chunk is at ord 0).
        paper1 = _paper(store, "wv14-p1", "Paper One")
        for i in range(5):
            seed_chunk(store, ref_id=paper1, text=f"Filler background {i}.", ord=i)
        claim1 = "Paper one's own claim."
        chunk1_id = seed_chunk(store, ref_id=paper1, text=claim1, ord=5)
        _tag_own(store, paper1, 5)
        source_handle1 = handle_registry.format_handle("paper", chunk1_id, chunk=True)

        claims_client = _SequentialClient(
            [
                json.dumps([{"claim": claim0, "source": 0}]),
                json.dumps([{"claim": claim1, "source": 0}]),
            ]
        )

        # The model misattributes: claims_used under paper index 0 (paper
        # zero) echoes paper one's real source_handle/source_ord (5).
        weave_client = _FakeClient(
            json.dumps(
                {
                    "section_text": "Composed section text.",
                    "papers": [
                        {
                            "index": 0,
                            "disposition": "cited-in",
                            "claims_used": [
                                {
                                    "text": claim1,
                                    "source_handle": source_handle1,
                                    "source_ord": 5,
                                }
                            ],
                        },
                        {
                            "index": 1,
                            "disposition": "off-topic-for",
                            "claims_used": [],
                        },
                    ],
                }
            )
        )

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper0, paper1],
            claims_client=claims_client,
        )

        assert result["ok"] is True
        by_pid = {p["ref_id"]: p for p in result["papers"]}
        assert by_pid[paper0]["citation_ids"] == []  # cross-paper claim dropped
        assert result["citation_ids"] == []

        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'citation'"
            ).fetchone()[0]
        assert n == 0

        # The cited-in link for paper zero is still recorded (the
        # disposition itself is trusted even though its one claim was
        # dropped) — but with no citation behind it.
        links = store.links_for(dossier_id, direction="in", relation="cited-in")
        assert any(link.src_ref_id == paper0 for link in links)


class TestReweaveConflict:
    """Rung 6d-2 review fix: re-weave passes ``base_sha`` on the woven-body
    edit; a stale read (a concurrent edit landed first) must abort BEFORE
    any per-paper write, not clobber it."""

    def test_stale_base_sha_returns_conflict_and_writes_nothing_further(
        self, store: Any, monkeypatch: Any
    ) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv15")
        claim_text = "Claim for the conflict test."
        paper_id, source_handle = _seed_own_paper(
            store, "wv15-p1", "Conflict Paper", claim_text
        )
        claims_client = _claims_client(claim_text)

        first_text = "Initial composition."
        weave_client_1 = _FakeClient(
            _weave_payload(
                first_text,
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )
        result_1 = weave_section(
            store,
            weave_client_1,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )
        assert result_1["ok"] is True
        citation_count_before = len(result_1["citation_ids"])

        # Simulate a stale read: the internal lookup hands back the right
        # handle but a content_sha that doesn't match what's actually
        # stored — as if a concurrent edit landed between our read and our
        # edit_text call.
        real_find_woven_body = weave_module._find_woven_body

        def _stale_find_woven_body(store_: Any, heading_chunk_id: int) -> Any:
            found = real_find_woven_body(store_, heading_chunk_id)
            assert found is not None
            handle, _real_sha = found
            return handle, "0" * 64

        monkeypatch.setattr(weave_module, "_find_woven_body", _stale_find_woven_body)

        second_text = "Second composition attempt that should be rejected."
        weave_client_2 = _FakeClient(
            _weave_payload(
                second_text,
                0,
                "cited-in",
                [{"text": claim_text, "source_handle": source_handle, "source_ord": 0}],
            )
        )
        result_2 = weave_section(
            store,
            weave_client_2,
            dossier_id,
            section_handle,
            [paper_id],
            claims_client=claims_client,
        )

        assert result_2 == {
            "ok": False,
            "error": "conflict",
            "applied": False,
            "section_handle": section_handle,
        }

        # Body text unchanged (still the first weave's text) — no clobber.
        body_chunk = store.get_draft_chunk(result_1["body_handle"], kind="draft")
        assert body_chunk is not None
        assert body_chunk.text == first_text

        # No new citation minted, no new link added by the rejected pass.
        with store.pool.connection() as conn:
            citations = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'citation'"
            ).fetchone()[0]
        assert citations == citation_count_before

        links = store.links_for(dossier_id, direction="in", relation="cited-in")
        assert len(links) == 1  # only from the first, successful weave


class TestPerPaperResilience:
    """Rung 6d-2 review fix: one paper's mint/link failure must not sink
    the rest of the batch — the failure is reported, not raised."""

    def test_one_paper_mint_failure_does_not_abort_the_batch(
        self, store: Any, monkeypatch: Any
    ) -> None:
        dossier_id, section_handle = _dossier_with_section(store, "wv16")
        claim_a = "Claim A text."
        claim_b = "Claim B text."
        paper_a, source_handle_a = _seed_own_paper(store, "wv16-pa", "Paper A", claim_a)
        paper_b, source_handle_b = _seed_own_paper(store, "wv16-pb", "Paper B", claim_b)

        claims_client = _SequentialClient(
            [
                json.dumps([{"claim": claim_a, "source": 0}]),
                json.dumps([{"claim": claim_b, "source": 0}]),
            ]
        )
        weave_client = _FakeClient(
            json.dumps(
                {
                    "section_text": "Composed prose citing both papers.",
                    "papers": [
                        {
                            "index": 0,
                            "disposition": "cited-in",
                            "claims_used": [
                                {
                                    "text": claim_a,
                                    "source_handle": source_handle_a,
                                    "source_ord": 0,
                                }
                            ],
                        },
                        {
                            "index": 1,
                            "disposition": "cited-in",
                            "claims_used": [
                                {
                                    "text": claim_b,
                                    "source_handle": source_handle_b,
                                    "source_ord": 0,
                                }
                            ],
                        },
                    ],
                }
            )
        )

        real_mint_citation = weave_module.mint_citation

        def _flaky_mint_citation(store_: Any, **kwargs: Any) -> int:
            if kwargs["paper_ref_id"] == paper_a:
                raise RuntimeError("simulated mint failure")
            return real_mint_citation(store_, **kwargs)

        monkeypatch.setattr(weave_module, "mint_citation", _flaky_mint_citation)

        result = weave_section(
            store,
            weave_client,
            dossier_id,
            section_handle,
            [paper_a, paper_b],
            claims_client=claims_client,
        )

        assert result["ok"] is True
        assert result["applied"] is True

        by_pid = {p["ref_id"]: p for p in result["papers"]}
        assert by_pid[paper_a]["error"] == "simulated mint failure"
        assert by_pid[paper_a]["citation_ids"] == []
        assert "error" not in by_pid[paper_b]
        assert len(by_pid[paper_b]["citation_ids"]) == 1

        links = store.links_for(dossier_id, direction="in", relation="cited-in")
        assert any(link.src_ref_id == paper_b for link in links)
        assert not any(link.src_ref_id == paper_a for link in links)

        # The body chunk still got written despite the per-paper failure.
        body_chunk = store.get_draft_chunk(result["body_handle"], kind="draft")
        assert body_chunk is not None
        assert body_chunk.text == "Composed prose citing both papers."
