"""Tests for :func:`precis.quest.weave_tick.weave_tick` — rung 6e-1 of the
paper-writing pipeline (docs/design/paper-writing-pipeline.md §"Integrate —
the tick body" + §"Make/Maintain, one loop").

DB-backed (real ``chunks``/``links``/``tags`` via the ``store`` fixture),
controlled one-hot vectors standing in for real bge-m3 embeddings (mirrors
``tests/test_placement.py`` / ``tests/test_residual_cluster.py``), and a
fake LLM client that routes ``.complete`` calls to a canned response by
matching a marker substring against the prompt (weave_tick drives two
distinct prompt shapes — weave-section compose and section-title judgment
— through the same client, unlike ``tests/test_weave.py``'s single fixed
text).
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import precis.quest.weave_tick as weave_tick_module
from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest.dossier import ensure_dossier
from precis.quest.weave_tick import weave_tick
from precis.store.types import Tag
from tests.workers._helpers import seed_chunk

_DEFAULT_EMBEDDER = "bge-m3"  # migration-seeded default (embedders.dim=1024)
_DIM = 1024


def _onehot(i: int) -> list[float]:
    v = [0.0] * _DIM
    v[i] = 1.0
    return v


def _embed(store: Any, chunk_id: int, vec: list[float]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, vector, status, attempts) "
            "VALUES (%s, %s, %s, 'ok', 1)",
            (chunk_id, _DEFAULT_EMBEDDER, vec),
        )


def _mk_quest(store: Any, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _dossier_with_topic(store: Any, qid: int, *, topic: str = "mof") -> int:
    """A quest's dossier, tagged with one ``topic:<t>``."""
    did = ensure_dossier(store, qid)
    store.add_tag(did, Tag.open(f"topic:{topic}"), set_by="agent")
    return did


def _scaffold_section(store: Any, did: int, title: str, vec: list[float]) -> str:
    """One top-level heading, embedded at ``vec`` so its (body-less)
    centroid is exactly ``vec`` — mirrors ``tests/test_placement.py``'s
    ``_dossier_with_sections``. Returns the section's legacy ``handle``
    (what ``place_papers`` rows key on)."""
    dc_handle = store.scaffold_sections(did, [(title, None)])[0]
    chunk = store.get_draft_chunk(dc_handle)
    assert chunk is not None
    _embed(store, chunk.chunk_id, vec)
    return str(chunk.handle)


def _paper(
    store: Any,
    slug: str,
    title: str,
    *,
    topic: str = "mof",
    claim_text: str = "A claim.",
    vec: list[float] | None = None,
) -> int:
    """A paper tagged ``topic:<t>``, with one ``ROLE3:own`` chunk (ord 0,
    ``claim_text``) and, if ``vec`` is given, a ``card_combined`` gist
    embedded at ``vec`` (the geometry :func:`place_papers` /
    :func:`cluster_residual` read)."""
    ref = store.insert_ref(kind="paper", slug=slug, title=title, meta={})
    store.add_tag(ref.id, Tag.open(f"topic:{topic}"), set_by="agent")
    seed_chunk(store, ref_id=ref.id, text=claim_text, ord=0)
    store.add_tag(ref.id, Tag.closed("ROLE3", "own"), pos=0, set_by="agent")
    if vec is not None:
        cid = store.upsert_card_combined(ref.id, "gist")
        _embed(store, cid, vec)
    return ref.id


_CLAIMS_TEXT = json.dumps([{"claim": "A generic own-contribution claim.", "source": 0}])


class _FixedClaimsClient:
    """Every paper here has exactly one ``ROLE3:own`` chunk at ord 0, so a
    single fixed ``source: 0`` response is correct for any paper — mirrors
    ``tests/test_weave.py``'s ``_claims_client``."""

    def complete(self, messages: list[dict[str, str]]) -> Any:
        return SimpleNamespace(text=_CLAIMS_TEXT, total_tokens=7)


class _RoutedClient:
    """Routes ``.complete`` to a canned response by matching a marker
    substring against the joined prompt content. ``weave_tick`` drives two
    distinct prompt shapes (weave-section compose, section-title judgment)
    through the same ``client`` — a single fixed text (as ``test_weave.py``
    uses) can't tell them apart."""

    def __init__(self, routes: list[tuple[str, str]]) -> None:
        self._routes = routes
        self.calls: list[Any] = []

    def complete(self, messages: list[dict[str, str]]) -> Any:
        self.calls.append(messages)
        content = "\n".join(str(m.get("content", "")) for m in messages)
        for marker, text in self._routes:
            if marker in content:
                return SimpleNamespace(text=text, total_tokens=7)
        raise AssertionError(
            f"_RoutedClient: no route matched prompt:\n{content[:300]}"
        )


def _weave_payload_one(disposition: str, claim: str = "A claim.") -> str:
    return json.dumps(
        {
            "section_text": f"Woven prose for a single paper ({disposition}).",
            "papers": [
                {
                    "index": 0,
                    "disposition": disposition,
                    "claims_used": [
                        {"text": claim, "source_handle": None, "source_ord": 0}
                    ],
                }
            ],
        }
    )


def _weave_payload_two(disposition: str = "cited-in") -> str:
    return json.dumps(
        {
            "section_text": "Woven prose merging two residual papers.",
            "papers": [
                {
                    "index": 0,
                    "disposition": disposition,
                    "claims_used": [
                        {"text": "Claim zero.", "source_handle": None, "source_ord": 0}
                    ],
                },
                {
                    "index": 1,
                    "disposition": disposition,
                    "claims_used": [
                        {"text": "Claim one.", "source_handle": None, "source_ord": 0}
                    ],
                },
            ],
        }
    )


_NEW_TITLE = "Residual Cluster Topic"
_TITLE_JSON = json.dumps({"title": _NEW_TITLE})


def _full_client() -> _RoutedClient:
    """A client that can drive a full Maintain+Make tick: one existing
    section ("Existing Section") and one freshly-judged new section
    (``_NEW_TITLE``)."""
    return _RoutedClient(
        [
            ("Cluster keywords:", _TITLE_JSON),
            ("Section: Existing Section", _weave_payload_one("cited-in")),
            (f"Section: {_NEW_TITLE}", _weave_payload_two("cited-in")),
        ]
    )


def _setup_maintain_and_residual(store: Any) -> tuple[int, int, int, int, int]:
    """Quest + dossier (topic:mof) + one scaffolded/embedded existing
    section + one aligned paper (Maintain) + two orthogonal papers
    (Make/residual). Returns ``(qid, did, aligned_pid, orth1_pid,
    orth2_pid)``."""
    qid = _mk_quest(store, "A dossier-owning quest")
    did = _dossier_with_topic(store, qid)
    _scaffold_section(store, did, "Existing Section", _onehot(0))

    aligned = _paper(
        store,
        "wt-aligned",
        "Aligned Paper",
        claim_text="Aligned finding.",
        vec=_onehot(0),
    )
    orth1 = _paper(
        store,
        "wt-orth1",
        "Orthogonal Paper One",
        claim_text="Orth one.",
        vec=_onehot(500),
    )
    orth2 = _paper(
        store,
        "wt-orth2",
        "Orthogonal Paper Two",
        claim_text="Orth two.",
        vec=_onehot(500),
    )
    return qid, did, aligned, orth1, orth2


class TestEarlyReturns:
    def test_no_dossier(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest with no dossier yet")
        result = weave_tick(store, _FixedClaimsClient(), qid)
        assert result == {"ok": False, "error": "no_dossier"}

    def test_no_topics(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest with a topicless dossier")
        did = ensure_dossier(store, qid)
        result = weave_tick(store, _FixedClaimsClient(), qid)
        assert result == {"ok": False, "error": "no_topics", "did": did}

    def test_empty_batch(self, store: Any) -> None:
        qid = _mk_quest(store, "A quest with nothing to integrate")
        _dossier_with_topic(store, qid)
        result = weave_tick(store, _FixedClaimsClient(), qid)
        assert result["ok"] is True
        assert result["woven"] == []
        assert result["note"] == "nothing unintegrated"


class TestMaintainAndResidual:
    def test_full_tick_maintains_and_makes(self, store: Any) -> None:
        qid, did, aligned, orth1, orth2 = _setup_maintain_and_residual(store)
        client = _full_client()
        claims_client = _FixedClaimsClient()

        result = weave_tick(store, client, qid, claims_client=claims_client)

        assert result["ok"] is True
        assert result["applied"] is True
        assert result["did"] == did
        assert result["topics"] == ["mof"]
        assert result["batch_size"] == 3
        assert len(result["woven"]) == 2
        assert all(w["ok"] for w in result["woven"])

        # Make: the two orthogonal papers cluster into one judged section.
        assert len(result["new_sections"]) == 1
        new_section = result["new_sections"][0]
        assert new_section["title"] == _NEW_TITLE
        assert new_section["handle"] is not None
        assert set(new_section["paper_ref_ids"]) == {orth1, orth2}
        assert result["residual_unplaced"] == []
        assert isinstance(result["log_entry"], int)

        # A new section heading now exists in the dossier's TOC.
        toc = store.draft_toc(did)
        assert any(h.title == _NEW_TITLE for h in toc)

        # cited-in links exist for all three papers.
        links = store.links_for(did, direction="in", relation="cited-in")
        cited_pids = {link.src_ref_id for link in links}
        assert cited_pids == {aligned, orth1, orth2}

        # Citations were minted.
        with store.pool.connection() as conn:
            n_citations = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'citation'"
            ).fetchone()[0]
        assert n_citations == 3

        # The batch is no longer unintegrated.
        assert store.unintegrated_papers(did, ["mof"]) == []

    def test_dry_run_writes_nothing(self, store: Any) -> None:
        qid, did, aligned, orth1, orth2 = _setup_maintain_and_residual(store)
        client = _full_client()
        claims_client = _FixedClaimsClient()

        toc_before = {h.handle for h in store.draft_toc(did)}

        result = weave_tick(
            store, client, qid, claims_client=claims_client, dry_run=True
        )

        assert result["ok"] is True
        assert result["applied"] is False
        # Only the Maintain-leg weave_section call happens in dry_run — the
        # Make leg reports proposed titles without scaffolding/weaving.
        assert len(result["woven"]) == 1
        assert result["woven"][0]["applied"] is False
        assert result["new_sections"] == [
            {"title": _NEW_TITLE, "handle": None, "paper_ref_ids": [orth1, orth2]}
        ]
        assert result["log_entry"] is None

        # Nothing physically written.
        toc_after = {h.handle for h in store.draft_toc(did)}
        assert toc_after == toc_before  # no new heading scaffolded

        with store.pool.connection() as conn:
            n_citations = conn.execute(
                "SELECT count(*) FROM refs WHERE kind = 'citation'"
            ).fetchone()[0]
        assert n_citations == 0

        assert store.links_for(did, direction="in") == []

        # No paper was dispositioned — still all pending.
        pending = {p["paper_ref_id"] for p in store.unintegrated_papers(did, ["mof"])}
        assert pending == {aligned, orth1, orth2}

    def test_one_section_failure_does_not_abort_tick(
        self, store: Any, monkeypatch: Any
    ) -> None:
        qid, did, aligned, orth1, orth2 = _setup_maintain_and_residual(store)
        client = _full_client()
        claims_client = _FixedClaimsClient()

        real_weave_section = weave_tick_module.weave_section

        def _flaky_weave_section(
            store_: Any,
            client_: Any,
            did_: int,
            handle_: str,
            pids_: list[int],
            **kw: Any,
        ) -> Any:
            if aligned in pids_:
                return {
                    "ok": False,
                    "error": "conflict",
                    "applied": False,
                    "section_handle": handle_,
                }
            return real_weave_section(store_, client_, did_, handle_, pids_, **kw)

        monkeypatch.setattr(weave_tick_module, "weave_section", _flaky_weave_section)

        result = weave_tick(store, client, qid, claims_client=claims_client)

        assert result["ok"] is True
        assert len(result["woven"]) == 2
        failing = [w for w in result["woven"] if not w["ok"]]
        succeeding = [w for w in result["woven"] if w["ok"]]
        assert len(failing) == 1
        assert failing[0]["error"] == "conflict"
        assert len(succeeding) == 1

        # The failing (Maintain) section's paper is still pending; the
        # succeeding (Make) section's papers are dispositioned.
        pending = {p["paper_ref_id"] for p in store.unintegrated_papers(did, ["mof"])}
        assert pending == {aligned}
