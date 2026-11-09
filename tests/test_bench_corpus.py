"""``precis.bench.corpus`` — QASPER fixture manifest, loader, ingest-local.

Everything runs on a synthetic QASPER-shaped fixture (fake papers, no real
text, no network). The DB tests use the ``store`` fixture and skip when no
Postgres is reachable. The committed ``bench/corpus/manifest.json`` is only
checked for internal consistency here; ``scripts/bench manifest --check``
re-derives it from the pinned archive.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from precis.bench import corpus


def _fake_paper(
    i: int,
    *,
    n_qas: int = 6,
    abstractive: bool = True,
    extractive: bool = True,
    abstract: str | None = None,
) -> dict[str, Any]:
    long_para = " ".join(
        f"Sentence number {k} of fake paper {i} mentions gizmo{k} and 3.5 eV."
        for k in range(40)
    )
    answers = []
    if abstractive:
        answers.append(
            {
                "answer": {
                    "unanswerable": False,
                    "extractive_spans": [],
                    "yes_no": None,
                    "free_form_answer": "a fake abstractive answer",
                    "evidence": ["Short fake paragraph."],
                    "highlighted_evidence": ["Short fake paragraph."],
                },
                "annotation_id": "a",
                "worker_id": "w",
            }
        )
    if extractive:
        answers.append(
            {
                "answer": {
                    "unanswerable": False,
                    "extractive_spans": ["gizmo1"],
                    "yes_no": None,
                    "free_form_answer": "",
                    "evidence": [long_para],
                    "highlighted_evidence": [],
                },
                "annotation_id": "b",
                "worker_id": "w",
            }
        )
    return {
        "title": f"Synthetic  Paper {i}",
        "abstract": abstract if abstract is not None else f"Synthetic abstract {i}.",
        "full_text": [
            {
                "section_name": "Introduction",
                "paragraphs": ["Short fake paragraph.", "", long_para],
            },
            {
                "section_name": "Methods ::: Setup",
                "paragraphs": [
                    f"Setup paragraph for paper {i}.",
                    "Short fake paragraph.",
                ],
            },
            {"section_name": None, "paragraphs": ["Orphan paragraph."]},
        ],
        "qas": [
            {"question": f"q{j}?", "question_id": f"{i}-{j}", "answers": answers}
            for j in range(n_qas)
        ],
        "figures_and_tables": [
            {"file": "f.png", "caption": f"Figure 1: fake caption {i}."}
        ],
    }


@pytest.fixture
def fake_papers() -> dict[str, tuple[str, dict[str, Any]]]:
    """A: tier 0.  B: tier 1 (extractive only).  C: unusable (3 questions)."""
    return {
        "9901.00001": ("train", _fake_paper(1)),
        "9901.00002": ("dev", _fake_paper(2, abstractive=False)),
        "9901.00003": ("train", _fake_paper(3, n_qas=3)),
    }


def _fake_archive(path: Path, papers: dict[str, tuple[str, dict[str, Any]]]) -> None:
    by_member: dict[str, dict[str, Any]] = {name: {} for name in corpus.MEMBERS}
    for qid, (split, paper) in papers.items():
        name = next(n for n, s in corpus.MEMBERS.items() if s == split)
        by_member[name][qid] = paper
    with tarfile.open(path, "w:gz") as tar:
        for name, data in by_member.items():
            blob = json.dumps(data).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))


# ---------------------------------------------------------------------------
# selection and manifest
# ---------------------------------------------------------------------------


def test_tiers(fake_papers):
    tiers = {q: corpus.paper_tier(p) for q, (_, p) in fake_papers.items()}
    assert tiers == {"9901.00001": 0, "9901.00002": 1, "9901.00003": None}


def test_selection_is_deterministic_and_order_independent():
    pool = {f"9902.{n:05d}": ("train", _fake_paper(n)) for n in range(20)}
    first = corpus.select_papers(pool, 8)
    assert first == corpus.select_papers(dict(reversed(list(pool.items()))), 8)
    assert len(set(first)) == 8
    # ranked by sha256(seed:id), so the shorter selection is a prefix of the longer
    assert corpus.select_papers(pool, 5) == first[:5]
    keys = [corpus.rank_key(q) for q in first]
    assert keys == sorted(keys)
    # pinned: a change of seed or rule must be a deliberate edit of this value
    assert corpus.rank_key("9902.00000") == (
        "531032a8c0652ad90233941810a01ba52462e3b4a3477fefa7e893173db42336"
    )


def test_selection_prefers_tier0_then_fills_with_tier1(fake_papers):
    assert corpus.select_papers(fake_papers, 2) == ["9901.00001", "9901.00002"]
    assert corpus.select_papers(fake_papers, 1) == ["9901.00001"]
    with pytest.raises(corpus.BenchCorpusError, match="only 2 usable"):
        corpus.select_papers(fake_papers, 3)


def test_build_manifest_shape_and_determinism(fake_papers):
    built = corpus.build_manifest(fake_papers, quick_n=1, full_n=2)
    again = corpus.build_manifest(
        dict(reversed(list(fake_papers.items()))), quick_n=1, full_n=2
    )
    assert corpus.manifest_text(built) == corpus.manifest_text(again)
    assert [r["qasper_id"] for r in built["quick"]] == ["9901.00001"]
    assert [r["qasper_id"] for r in built["full"]] == ["9901.00001", "9901.00002"]
    assert built["quick"] == built["full"][:1]
    rec = built["full"][0]
    assert rec["title"] == "Synthetic Paper 1"  # whitespace collapsed
    assert rec["sha256"] == corpus.paper_sha256(fake_papers["9901.00001"][1])
    assert rec["licence"] == "CC-BY-4.0" and rec["hf_revision"] == corpus.HF_REVISION
    assert built["corpus_sha256"]["full"] == corpus.corpus_sha256(built["full"])
    assert (
        built["selection"]["tier0_size"] == 1 and built["selection"]["tier1_size"] == 1
    )


def test_paper_sha256_ignores_key_order_but_not_content(fake_papers):
    p = fake_papers["9901.00001"][1]
    shuffled = dict(reversed(list(p.items())))
    assert corpus.paper_sha256(p) == corpus.paper_sha256(shuffled)
    changed = json.loads(json.dumps(p))
    changed["abstract"] += "!"
    assert corpus.paper_sha256(changed) != corpus.paper_sha256(p)


def test_committed_manifest_is_self_consistent():
    man = corpus.load_manifest()
    assert len(man["quick"]) == corpus.QUICK_N
    assert len(man["full"]) == corpus.FULL_N
    assert man["quick"] == man["full"][: corpus.QUICK_N]
    ids = [r["qasper_id"] for r in man["full"]]
    assert len(set(ids)) == len(ids)
    for name in ("quick", "full"):
        assert corpus.corpus_sha256(man[name]) == man["corpus_sha256"][name]
    assert {r["licence"] for r in man["full"]} == {"CC-BY-4.0"}
    assert {r["hf_revision"] for r in man["full"]} == {corpus.HF_REVISION}
    assert man["dataset"]["hf_revision"] == corpus.HF_REVISION
    assert man["dataset"]["archive"]["sha256"] == corpus.ARCHIVE_SHA256
    assert all(r["n_questions"] >= corpus.MIN_QUESTIONS for r in man["full"])
    assert all(len(r["sha256"]) == 64 for r in man["full"])
    # every quick paper is tier 0; tiers never decrease along the ranking
    tiers = [r["tier"] for r in man["full"]]
    assert tiers == sorted(tiers) and set(tiers[: corpus.QUICK_N]) == {0}


# ---------------------------------------------------------------------------
# cache, archive
# ---------------------------------------------------------------------------


def test_cache_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PRECIS_BENCH_CACHE", str(tmp_path / "c"))
    assert corpus.cache_dir() == tmp_path / "c"
    monkeypatch.delenv("PRECIS_BENCH_CACHE")
    assert corpus.cache_dir().name == "precis-bench"


def test_archive_roundtrip_and_cache_hit_needs_no_network(
    fake_papers, tmp_path, monkeypatch
):
    archive = tmp_path / corpus.ARCHIVE_NAME
    _fake_archive(archive, fake_papers)
    loaded = corpus.load_archive(archive)
    assert set(loaded) == set(fake_papers)
    assert loaded["9901.00002"][0] == "dev"
    assert loaded["9901.00001"][1] == fake_papers["9901.00001"][1]

    def boom(*a, **k):  # a cached, hash-correct file must not touch the network
        raise AssertionError("network used")

    monkeypatch.setattr("precis.utils.http.http_client", boom)
    got = corpus.fetch_archive(tmp_path, sha256=corpus._file_sha256(archive))
    assert got == archive


def test_load_archive_reads_only_known_members(tmp_path):
    path = tmp_path / "x.tgz"
    with tarfile.open(path, "w:gz") as tar:
        blob = b"{}"
        info = tarfile.TarInfo("../evil.json")
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))
    with pytest.raises(corpus.BenchCorpusError, match="found"):
        corpus.load_archive(path)


def test_throwaway_dsn_guard():
    corpus.assert_throwaway_dsn("postgresql://postgres@precis-test-db:5432/precis_dev")
    corpus.assert_throwaway_dsn("postgresql://u@h/precis_bench_2")
    for bad in ("postgresql://u@h/precis", "postgresql://u@h/postgres"):
        with pytest.raises(corpus.BenchCorpusError, match="refusing"):
            corpus.assert_throwaway_dsn(bad)


# ---------------------------------------------------------------------------
# units, chunks, offsets
# ---------------------------------------------------------------------------


def test_canonical_units_offsets_are_exact(fake_papers):
    paper = fake_papers["9901.00001"][1]
    units = corpus.canonical_units(paper)
    text = corpus.canonical_text(paper)
    for u in units:
        assert text[u.doc_start : u.doc_end] == u.text
    ids = [u.para_id for u in units]
    assert ids[:2] == ["title", "abstract"]
    assert "s0p1" not in ids  # the empty paragraph is skipped ...
    assert "s0p2" in ids  # ... but its neighbour keeps its raw index
    assert ids[-1] == "f0"
    by_id = {u.para_id: u for u in units}
    assert by_id["s1p0"].section_path == ("Methods", "Setup")
    assert by_id["s2p0"].section_path == ()  # section_name None
    assert by_id["f0"].src == "float"


def test_chunks_carry_source_ids_and_exact_offsets(fake_papers):
    paper = fake_papers["9901.00001"][1]
    units = corpus.canonical_units(paper)
    by_id = {u.para_id: u for u in units}
    text = corpus.canonical_text(paper)
    chunks = corpus.chunk_units(units)
    assert [c.ord for c in chunks] == list(range(len(chunks)))
    long_chunks = [c for c in chunks if c.meta["qasper"]["para_id"] == "s0p2"]
    assert len(long_chunks) > 1  # the 2.4k-char paragraph was split
    starts = [c.meta["qasper"]["para_char_start"] for c in long_chunks]
    assert starts == sorted(starts) and starts[0] == 0
    for c in chunks:
        q = c.meta["qasper"]
        unit = by_id[q["para_id"]]
        assert unit.text[q["para_char_start"] : q["para_char_end"]] == c.text
        assert text[q["doc_char_start"] : q["doc_char_end"]] == c.text
        assert q["doc_char_start"] == unit.doc_start + q["para_char_start"]
        assert c.meta["chunker_version"] == corpus.CHUNKER_VERSION
        assert c.section_path == list(unit.section_path)
    # overlapping chunks: consecutive pieces of one paragraph share text
    a, b = long_chunks[0].meta["qasper"], long_chunks[1].meta["qasper"]
    assert b["para_char_start"] < a["para_char_end"]


def test_duplicate_texts_are_dropped_before_ord_is_assigned(fake_papers):
    # "Short fake paragraph." appears twice in the body; first wins, no ord hole
    units = corpus.canonical_units(fake_papers["9901.00001"][1])
    chunks = corpus.chunk_units(units)
    shorts = [c for c in chunks if c.text == "Short fake paragraph."]
    assert len(shorts) == 1 and shorts[0].meta["qasper"]["para_id"] == "s0p0"
    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_paper_to_write_cards_body_and_meta(fake_papers):
    qid, (split, paper) = "9901.00001", fake_papers["9901.00001"]
    ptw = corpus.paper_to_write(qid, split, paper)
    assert ptw.kind == "paper" and ptw.paper_id == "qasper:9901.00001"
    assert ptw.doi is None and ptw.arxiv_id is None and ptw.pdf_sha256 is None
    kinds = [c.chunk_kind for c in ptw.chunks if c.ord < 0]
    assert kinds == ["card_combined", "card_title", "card_abstract"]
    body = [c for c in ptw.chunks if c.ord >= 0]
    # title and the short abstract are the cards, not repeated as body chunks
    assert "title" not in {c.meta["qasper"]["para_id"] for c in body}
    assert "abstract" not in {c.meta["qasper"]["para_id"] for c in body}
    assert {c.chunk_kind for c in body} == {"paragraph", "caption"}
    m = ptw.meta["qasper"]
    assert m["id"] == qid and m["split"] == "train" and m["n_questions"] == 6
    assert m["sha256"] == corpus.paper_sha256(paper)
    assert ptw.meta["source_format"] == "qasper"


def test_long_abstract_becomes_body_chunks():
    paper = _fake_paper(
        7, abstract=" ".join(f"Abstract sentence {k} here." for k in range(120))
    )
    ptw = corpus.paper_to_write("9901.00007", "train", paper)
    abs_chunks = [
        c for c in ptw.chunks if c.ord >= 0 and c.meta["qasper"]["src"] == "abstract"
    ]
    assert len(abs_chunks) > 1
    assert abs_chunks[0].chunk_kind == "paragraph"
    assert abs_chunks[0].section_path == ["Abstract"]


def test_paper_to_write_does_not_embed(fake_papers):
    ptw = corpus.paper_to_write("9901.00001", "train", fake_papers["9901.00001"][1])
    assert all(not hasattr(c, "embedding") for c in ptw.chunks)


# ---------------------------------------------------------------------------
# ingest-local (DB)
# ---------------------------------------------------------------------------


def _counts(conn) -> tuple[int, int]:
    refs = conn.execute("SELECT count(*) FROM refs WHERE meta ? 'qasper'").fetchone()[0]
    chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    return refs, chunks


def test_ingest_papers_inserts_then_is_idempotent(store, fake_papers):
    man = corpus.build_manifest(fake_papers, quick_n=1, full_n=2)
    with store.pool.connection() as conn:
        first = corpus.ingest_papers(conn, man["full"], fake_papers, set_name="full")
        assert (first.inserted, first.skipped, first.stale) == (2, 0, 0)
        refs, chunks = _counts(conn)
        assert refs == 2 and chunks == first.chunks > 0

        again = corpus.ingest_papers(conn, man["full"], fake_papers, set_name="full")
        assert (again.inserted, again.skipped, again.stale, again.chunks) == (
            0,
            2,
            0,
            0,
        )
        assert _counts(conn) == (refs, chunks)

        # a superset run only adds the new paper
        wider = corpus.build_manifest(
            {**fake_papers, "9901.00004": ("dev", _fake_paper(4))}, quick_n=1, full_n=3
        )
        papers4 = {**fake_papers, "9901.00004": ("dev", _fake_paper(4))}
        third = corpus.ingest_papers(conn, wider["full"], papers4, set_name="full")
        assert (third.inserted, third.skipped) == (1, 2)
        assert _counts(conn)[0] == 3


def test_ingested_rows_match_the_loader_contract(store, fake_papers):
    man = corpus.build_manifest(fake_papers, quick_n=1, full_n=1)
    with store.pool.connection() as conn:
        corpus.ingest_papers(conn, man["full"], fake_papers)
        ref_id, kind, title, meta = conn.execute(
            "SELECT ref_id, kind, title, meta FROM refs WHERE meta->'qasper'->>'id' = %s",
            ("9901.00001",),
        ).fetchone()
        assert kind == "paper" and title == "Synthetic Paper 1"
        assert meta["qasper"]["sha256"] == man["full"][0]["sha256"]
        rows = conn.execute(
            "SELECT ord, chunk_kind, text, meta FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "ORDER BY ord",
            (ref_id,),
        ).fetchall()
        assert [r[0] for r in rows] == list(range(len(rows)))  # gapless
        canon = corpus.canonical_text(fake_papers["9901.00001"][1])
        for _ord, _kind, text, cmeta in rows:
            q = cmeta["qasper"]
            assert canon[q["doc_char_start"] : q["doc_char_end"]] == text
        assert (
            conn.execute(
                "SELECT count(*) FROM chunks WHERE ref_id = %s AND ord < 0", (ref_id,)
            ).fetchone()[0]
            == 3
        )
        # no embedding rows: the worker owns those
        assert conn.execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0] == 0


def test_hash_mismatch_refuses_before_writing(store, fake_papers):
    man = corpus.build_manifest(fake_papers, quick_n=1, full_n=1)
    tampered = {k: (s, json.loads(json.dumps(p))) for k, (s, p) in fake_papers.items()}
    tampered["9901.00001"][1]["abstract"] += " tampered"
    with store.pool.connection() as conn:
        with pytest.raises(corpus.BenchCorpusError, match="sha256"):
            corpus.ingest_papers(conn, man["full"], tampered)
        assert _counts(conn) == (0, 0)


def test_existing_ref_with_other_hash_is_reported_stale(store, fake_papers):
    man = corpus.build_manifest(fake_papers, quick_n=1, full_n=1)
    with store.pool.connection() as conn:
        corpus.ingest_papers(conn, man["full"], fake_papers)
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{qasper,sha256}', '\"old\"') "
            "WHERE meta->'qasper'->>'id' = '9901.00001'"
        )
        report = corpus.ingest_papers(conn, man["full"], fake_papers)
        assert (report.inserted, report.skipped, report.stale) == (0, 1, 1)


def test_ingest_set_end_to_end_from_cached_archive(store, fake_papers, tmp_path):
    archive = tmp_path / corpus.ARCHIVE_NAME
    _fake_archive(archive, fake_papers)
    man = corpus.build_manifest(
        fake_papers,
        archive_sha256=corpus._file_sha256(archive),
        quick_n=1,
        full_n=2,
    )
    with store.pool.connection() as conn:
        report = corpus.ingest_set(conn, "full", manifest=man, cache=tmp_path)
        assert report.inserted == 2
        assert report.corpus_sha256 == man["corpus_sha256"]["full"]
        assert (
            corpus.ingest_set(conn, "full", manifest=man, cache=tmp_path).skipped == 2
        )
        with pytest.raises(corpus.BenchCorpusError, match="unknown set"):
            corpus.ingest_set(conn, "huge", manifest=man, cache=tmp_path)
