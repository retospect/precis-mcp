"""Span-gold schema and QASPER task extraction (td604845).

DB-free: tasks are derived from the QASPER archive, not from the database.
Everything runs on a 3-paper synthetic fixture (no QASPER text, no network);
the committed ``bench/tasks/qasper-quick.jsonl`` is validated as a form, and
re-derived from the real archive only when it is already in the bench cache
(``PRECIS_BENCH_CACHE`` / the platformdirs user cache), else that test skips.
"""

from __future__ import annotations

import copy
import difflib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

import pytest

from precis.bench import cli, corpus, gold, planted, qasper_tasks

_TASKS = Path(__file__).parent.parent / "bench" / "tasks" / "qasper-quick.jsonl"

P_CURLY = (
    "We train the model on the “Foo corpus” – a large set of "
    "documents. It has 3 splits. The gizmo is described in detail."
)
P_FOX = (
    "The quick brown fox jumps over the lazy dog near the river bank while "
    "the sun sets slowly behind the distant hills."
)
P_GREEK = (
    "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi "
    "omicron pi rho sigma tau upsilon."
)
CAPTION = "Table 1: Results of the gizmo experiments."


def _ans(
    *,
    spans: tuple[str, ...] = (),
    yes_no: bool | None = None,
    free: str = "",
    unanswerable: bool = False,
    evidence: tuple[str, ...] = (),
    highlighted: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "answer": {
            "unanswerable": unanswerable,
            "extractive_spans": list(spans),
            "yes_no": yes_no,
            "free_form_answer": free,
            "evidence": list(evidence),
            "highlighted_evidence": list(highlighted),
        },
        "annotation_id": "a",
        "worker_id": "w",
    }


def _fake_paper(tag: int) -> dict[str, Any]:
    fox_typos = P_FOX.replace("jumps", "jumpd").replace("lazy", "lasy")
    greek_noisy = P_GREEK.replace("beta gamma", "beta  gamma").replace(
        "delta", " delta"
    )
    qas = [
        # 0: extractive, whole-paragraph evidence + a sentence highlight
        [
            _ans(
                spans=("Foo corpus",),
                evidence=(P_CURLY,),
                highlighted=("It has 3 splits.",),
            )
        ],
        # 1: yes/no, evidence differs from the text by whitespace only
        [_ans(yes_no=True, evidence=(greek_noisy,))],
        # 2: free-form, evidence with two typos
        [_ans(free="Jumping foxes.", evidence=(fox_typos,))],
        # 3: unanswerable by both annotators
        [_ans(unanswerable=True), _ans(unanswerable=True)],
        # 4: float caption evidence + a section heading that maps nowhere
        [
            _ans(
                free="See the table.",
                evidence=(f"FLOAT SELECTED: {CAPTION}", "Methods ::: Setup"),
            )
        ],
        # 5: one unanswerable vote, two answers sharing evidence
        [
            _ans(unanswerable=True),
            _ans(spans=("Foo corpus",), evidence=(P_CURLY,)),
            _ans(free="It is a corpus.", evidence=(P_CURLY, P_GREEK)),
        ],
        # 6: answerable, annotators gave no evidence at all
        [_ans(free="Because.")],
    ]
    return {
        "title": f"Gold  Fixture {tag}",
        "abstract": "A short abstract about gizmos.",
        "full_text": [
            {"section_name": "Introduction", "paragraphs": [P_CURLY, "", P_FOX]},
            {"section_name": "Methods ::: Setup", "paragraphs": [P_GREEK]},
        ],
        "qas": [
            {"question": f"q{j}  of {tag}?", "question_id": f"{tag}-{j}", "answers": a}
            for j, a in enumerate(qas)
        ],
        "figures_and_tables": [{"file": "t.png", "caption": CAPTION}],
    }


@pytest.fixture
def papers() -> dict[str, tuple[str, dict[str, Any]]]:
    return {f"9903.0000{i}": ("train", _fake_paper(i)) for i in (1, 2, 3)}


@pytest.fixture
def records(papers) -> list[dict[str, Any]]:
    return [
        {"qasper_id": q, "sha256": corpus.paper_sha256(p)}
        for q, (_, p) in papers.items()
    ]


@pytest.fixture
def built(papers, records):
    return qasper_tasks.build_tasks(records, papers)


def check_rows_against_papers(
    rows: list[dict[str, Any]], papers: dict[str, tuple[str, dict[str, Any]]]
) -> int:
    """The form checks that need paper text; returns the number of items seen."""
    seen = 0
    for row in rows:
        qid = row["source"]["qasper_id"]
        paper = papers[qid][1]
        units = {u.para_id: u for u in corpus.canonical_units(paper)}
        canon = corpus.canonical_text(paper)
        for item in row["evidence"]:
            seen += 1
            unit = units[item["unit"]]
            s, e = item["span"]
            sliced = unit.text[s:e]
            raw = row["source"]["evidence_raw"][item["raw_idx"]]
            want = qasper_tasks.evidence_text(raw)[0]
            if item["match"] in ("exact", "normalised"):
                assert qasper_tasks.normalise(sliced) == qasper_tasks.normalise(want), (
                    row["id"],
                    item,
                )
            else:
                ratio = difflib.SequenceMatcher(
                    None,
                    qasper_tasks.normalise(want),
                    qasper_tasks.normalise(sliced),
                    autojunk=False,
                ).ratio()
                assert ratio >= qasper_tasks.FUZZY_MIN, (row["id"], item, ratio)
                assert abs(ratio - float(item["match"][6:])) < 0.02
            ds, de = item["doc_span"]
            assert (ds, de) == (unit.doc_start + s, unit.doc_start + e)
            assert canon[ds:de] == sliced
            for hs, he in item.get("highlights", []):
                assert s <= hs < he <= e
                assert (hs, he) != (s, e)  # a sub-span, not the whole paragraph
    return seen


# ---------------------------------------------------------------------------
# extraction on the fixture
# ---------------------------------------------------------------------------


def test_rows_validate_and_every_span_slices_to_its_raw_evidence(papers, built):
    rows, _ = built
    assert len(rows) == 3 * 7
    for row in rows:
        gold.validate_row(row)
    assert check_rows_against_papers(rows, papers) > 0


def test_yield_table_counts_every_class(built):
    _, report = built
    assert dict(report.counts) == {
        "exact": 12,
        "normalised": 3,
        "fuzzy": 3,
        "unmapped": 3,
    }
    assert report.total == 21 and report.questions == 21
    assert dict(report.by_kind) == {
        "extractive": 6,
        "yes_no": 3,
        "abstractive": 9,
        "unanswerable": 3,
    }
    assert report.answerable_no_evidence == 3  # q6 (no evidence strings)
    assert report.answerable_partial == 3  # q4 (float mapped, heading not)
    assert min(report.ratios) >= qasper_tasks.FUZZY_MIN
    table = report.table()
    for word in ("exact", "normalised", "fuzzy", "unmapped"):
        assert word in table


def test_row_semantics(built):
    rows, _ = built
    r = {x["id"]: x for x in rows if x["source"]["qasper_id"] == "9903.00001"}
    q = lambda j: r[f"qasper-9903.00001-q{j:02d}"]
    assert q(0)["type"] == "lookup" and q(0)["answer"] == ["Foo corpus"]
    assert q(0)["question"] == "q0 of 1?"  # whitespace collapsed
    (ev,) = q(0)["evidence"]
    assert ev["unit"] == "s0p0" and ev["match"] == "exact"
    assert ev["span"] == [0, len(P_CURLY)]
    sentence = P_CURLY.index("It has 3 splits.")
    assert ev["highlights"] == [[sentence, sentence + len("It has 3 splits.")]]

    assert q(1)["type"] == "grounding" and q(1)["answer"] == ["yes"]
    assert q(1)["answer_kind"] == "yes_no"
    assert q(1)["evidence"][0]["match"] == "normalised"
    assert q(1)["evidence"][0]["unit"] == "s1p0"
    assert q(1)["evidence"][0]["span"] == [0, len(P_GREEK)]  # mapped back

    assert q(2)["evidence"][0]["match"].startswith("fuzzy:0.9")
    assert q(2)["evidence"][0]["unit"] == "s0p2"

    # unanswerable: lookup, no answer, no evidence, still tied to its paper
    assert q(3)["type"] == "lookup" and q(3)["answer_kind"] == "unanswerable"
    assert q(3)["answer"] == [] and q(3)["evidence"] == []
    assert q(3)["docs"] == ["qasper:9903.00001"]

    assert (
        q(4)["evidence"][0]["unit"] == "f0" and q(4)["evidence"][0]["match"] == "exact"
    )
    assert q(4)["evidence"][0]["span"] == [0, len(CAPTION)]
    assert q(4)["unmapped_evidence"] == ["Methods ::: Setup"]

    # mixed annotators: unanswerable vote dropped, evidence deduplicated
    assert q(5)["answer"] == ["Foo corpus", "It is a corpus."]
    assert q(5)["answer_kind"] == "extractive" and q(5)["type"] == "lookup"
    assert q(5)["source"]["answer_kinds"] == ["extractive", "abstractive"]
    assert q(5)["source"]["n_unanswerable"] == 1
    assert [e["unit"] for e in q(5)["evidence"]] == ["s0p0", "s1p0"]
    assert q(5)["source"]["evidence_raw"] == [P_CURLY, P_GREEK]

    assert q(6)["evidence"] == [] and "unmapped_evidence" not in q(6)
    assert q(6)["answer_kind"] == "abstractive"

    assert q(0)["source"]["question_id"] == "1-0"
    assert q(0)["source"]["paper_sha256"] == corpus.paper_sha256(_fake_paper(1))
    assert q(0)["labelled_by"] == "qasper"


def test_no_chunk_ids_or_gold_spans_anywhere(built):
    rows, _ = built
    blob = json.dumps(rows)
    for forbidden in ("gold_spans", "gold_chunks", "chunk_id", "ref_id"):
        assert forbidden not in blob


def test_build_is_deterministic(papers, records):
    a, _ = qasper_tasks.build_tasks(records, papers)
    b, _ = qasper_tasks.build_tasks(list(records), copy.deepcopy(papers))
    assert qasper_tasks.tasks_text(a) == qasper_tasks.tasks_text(b)


def test_build_refuses_a_paper_that_drifted_from_its_manifest_row(papers, records):
    papers["9903.00001"][1]["abstract"] += " drift"
    with pytest.raises(corpus.BenchCorpusError, match="sha256"):
        qasper_tasks.build_tasks(records, papers)
    with pytest.raises(corpus.BenchCorpusError, match="not in the archive"):
        qasper_tasks.build_tasks([{"qasper_id": "0000.00000", "sha256": "x"}], papers)


# ---------------------------------------------------------------------------
# matching and normalisation
# ---------------------------------------------------------------------------


def test_normalise_maps_offsets_back_to_the_original():
    text = "  A  b’s  ﬁnal​ word – end  "
    norm, origin = qasper_tasks.normalise_with_map(text)
    assert norm == "A b's final word - end"
    assert len(origin) == len(norm)
    a = norm.index("final")
    s, e = origin[a], origin[a + len("final") - 1] + 1
    assert text[s:e] == "ﬁnal"  # the ligature is one original character
    assert qasper_tasks.normalise("") == "" and qasper_tasks.normalise(" \n ") == ""


def test_fuzzy_needs_the_bar_and_a_float_prefix_never_matches_prose(papers):
    idx = qasper_tasks._Index(corpus.canonical_units(papers["9903.00001"][1]))
    far = P_FOX.replace("quick brown fox", "slow green cat").replace(
        "lazy dog", "tired"
    )
    assert qasper_tasks.match_evidence(idx, far) is None  # well under 0.9
    assert qasper_tasks.match_evidence(idx, "") is None
    # prose never matches a caption and a FLOAT string never matches prose
    assert qasper_tasks.match_evidence(idx, f"FLOAT SELECTED: {P_GREEK}") is None
    m = qasper_tasks.match_evidence(idx, f"FLOAT SELECTED: {CAPTION}")
    assert m is not None and m.unit.para_id == "f0" and m.how == "exact"
    m = qasper_tasks.match_evidence(idx, "Foo corpus")  # a sub-paragraph string
    assert m is not None and m.how == "exact" and m.unit.para_id == "s0p0"
    assert m.span == (P_CURLY.index("Foo corpus"), P_CURLY.index("Foo corpus") + 10)


def test_annotator_answer_kinds():
    f = qasper_tasks.annotator_answer
    assert f({"unanswerable": True, "yes_no": True}) == ("unanswerable", None)
    assert f({"extractive_spans": ["a ", " b"], "free_form_answer": "x"}) == (
        "extractive",
        "a, b",
    )
    assert f({"free_form_answer": " text "}) == ("abstractive", "text")
    assert f({"yes_no": False}) == ("yes_no", "no")
    assert f({"free_form_answer": ""}) == ("empty", None)


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------


def _good() -> dict[str, Any]:
    return {
        "id": "x-1",
        "type": "lookup",
        "question": "Q?",
        "answer": ["a"],
        "answer_kind": "extractive",
        "docs": ["qasper:1"],
        "evidence": [
            {
                "doc": "qasper:1",
                "unit": "s0p0",
                "span": [0, 5],
                "doc_span": [10, 15],
                "match": "fuzzy:0.953",
                "raw_idx": 0,
                "highlights": [[1, 3]],
            }
        ],
        "labelled_by": "qasper",
        "source": {"evidence_raw": ["hello"]},
    }


def _bad(**change: Any) -> dict[str, Any]:
    row = _good()
    row.update(change)
    return row


def test_validate_row_accepts_the_reference_row():
    gold.validate_row(_good())


@pytest.mark.parametrize(
    "change, needle",
    [
        ({"type": "cross-paper"}, "type"),
        ({"answer_kind": "span"}, "answer_kind"),
        ({"answer": []}, "exactly when"),
        ({"answer": "a"}, "list of non-empty"),
        ({"labelled_by": "reto"}, "labelled_by"),
        ({"docs": []}, "docs is empty"),
        ({"docs": ["qasper:1", "qasper:1"]}, "distinct"),
        ({"docs": ["planted-1"]}, "qasper:<id>"),
        ({"gold_spans": []}, "unknown keys"),
        ({"gold_chunks": ["x"]}, "unknown keys"),
        ({"id": ""}, "id"),
        ({"question": " "}, "question"),
        ({"source": []}, "source"),
        ({"unmapped_evidence": []}, "unmapped_evidence"),
        ({"evidence": "x"}, "list"),
    ],
)
def test_validate_row_rejects(change, needle):
    with pytest.raises(gold.GoldError, match=needle):
        gold.validate_row(_bad(**change))


@pytest.mark.parametrize(
    "edit, needle",
    [
        ({"span": [5, 5]}, "non-empty half-open"),
        ({"span": [6, 2]}, "non-empty half-open"),
        ({"span": [0, 4]}, "differ in length"),
        ({"span": ["a", 4]}, "two ints"),
        ({"doc": "qasper:2"}, "not in docs"),
        ({"unit": ""}, "unit"),
        ({"unit": None}, "unit"),
        ({"match": "approx"}, "match"),
        ({"match": "fuzzy:2.0"}, "match"),
        ({"raw_idx": 3}, "raw_idx"),
        ({"highlights": [[4, 9]]}, "outside span"),
        ({"highlights": "x"}, "highlights"),
        ({"chunk_id": 7}, "unknown keys"),
        ({"gold_chunk": 7}, "unknown keys"),
    ],
)
def test_validate_row_rejects_bad_evidence_items(edit, needle):
    row = _good()
    row["evidence"][0].update(edit)
    with pytest.raises(gold.GoldError, match=needle):
        gold.validate_row(row)


def test_validate_row_qasper_items_need_the_mapping_fields():
    for key in ("doc_span", "match", "raw_idx"):
        row = _good()
        del row["evidence"][0][key]
        with pytest.raises(gold.GoldError, match=f"lacks {key}"):
            gold.validate_row(row)


def test_validate_row_unanswerable_and_empty_evidence_rules():
    un = _good()
    un.update(answer=[], answer_kind="unanswerable")
    with pytest.raises(gold.GoldError, match="empty evidence"):
        gold.validate_row(un)
    un["evidence"] = []
    gold.validate_row(un)
    # an answerable QASPER row may carry no evidence (reported by the yield)
    ok = _good()
    ok["evidence"] = []
    gold.validate_row(ok)
    # a planted lookup may not; a planted memory task has none by design
    pl = _good()
    pl.update(labelled_by="planted", evidence=[], docs=["d"])
    with pytest.raises(gold.GoldError, match="planted"):
        gold.validate_row(pl)
    pl.update(type="memory", docs=[])
    gold.validate_row(pl)
    model = _good()
    model["labelled_by"] = "model:claude-opus-x"
    gold.validate_row(model)


def test_load_tasks_checks_json_rows_and_duplicate_ids(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(gold.dump_tasks([_good()]) + "\n", encoding="utf-8")
    assert gold.load_tasks(p) == [_good()]
    p.write_text(gold.dump_tasks([_good(), _good()]), encoding="utf-8")
    with pytest.raises(gold.GoldError, match="duplicate id"):
        gold.load_tasks(p)
    p.write_text("{not json\n", encoding="utf-8")
    with pytest.raises(gold.GoldError, match=":1: not JSON"):
        gold.load_tasks(p)
    p.write_text(json.dumps({"id": "x"}) + "\n", encoding="utf-8")
    with pytest.raises(gold.GoldError, match=":1: .*missing keys"):
        gold.load_tasks(p)


# ---------------------------------------------------------------------------
# span overlap with a rendered chunk
# ---------------------------------------------------------------------------


def _row_with(*doc_spans: tuple[str, str, list[int], list[int] | None]) -> dict:
    return {
        "evidence": [
            {"doc": d, "unit": u, "span": s, **({"doc_span": ds} if ds else {})}
            for d, u, s, ds in doc_spans
        ]
    }


def test_spans_overlap_chunk_is_half_open_on_both_sides():
    row = _row_with(("qasper:1", "s0p0", [0, 10], [100, 110]))
    f = gold.spans_overlap_chunk

    def meta(a: int | None, b: int | None) -> dict:
        return {"doc_char_start": a, "doc_char_end": b}

    assert f(row, meta(105, 200))  # clips the tail
    assert f(row, meta(0, 101))  # one char of overlap
    assert f(row, meta(109, 300))  # last gold char
    assert not f(row, meta(110, 300))  # chunk starts where the span ends
    assert not f(row, meta(0, 100))  # chunk ends where the span starts
    assert f(row, meta(0, 1000))  # chunk swallows the span
    assert f(row, meta(102, 104))  # chunk inside the span
    assert not f(row, meta(None, None))  # unlocated chunk
    # the corpus block may be passed whole, as it sits in chunk meta
    assert f(row, {"qasper": meta(105, 200), "chunker_version": "x"})
    assert not f(row, {"chunker_version": "x"})
    assert not f({"evidence": []}, meta(0, 1000))  # abstention task


def test_spans_overlap_chunk_doc_filter_and_any_item():
    row = _row_with(
        ("qasper:1", "s0p0", [0, 10], [100, 110]),
        ("qasper:2", "s0p0", [0, 10], [500, 510]),
    )
    f = gold.spans_overlap_chunk
    meta = {"doc_char_start": 505, "doc_char_end": 520}
    assert f(row, meta)  # second item, document unknown
    assert f(row, meta, doc="qasper:2")
    assert not f(row, meta, doc="qasper:1")


def test_spans_overlap_chunk_falls_back_to_the_unit_relative_span():
    row = _row_with(("d", "s0p2", [20, 40], None))
    f = gold.spans_overlap_chunk
    hit = {"para_id": "s0p2", "para_char_start": 30, "para_char_end": 90}
    assert f(row, hit)
    assert not f(row, {**hit, "para_char_start": 40})
    assert not f(row, {**hit, "para_id": "s0p3"})
    # planted units are ints; chunk meta may hold the string form
    prow = _row_with(("d", 3, [0, 5], None))
    assert f(prow, {"para_id": "3", "para_char_start": 0, "para_char_end": 9})


def test_one_gold_span_can_touch_two_overlapping_chunks(papers):
    paper = copy.deepcopy(papers["9903.00001"][1])
    long_para = " ".join(
        f"Sentence number {k} mentions gizmo{k} and 3.5 eV." for k in range(80)
    )
    paper["full_text"][0]["paragraphs"][2] = long_para
    units = corpus.canonical_units(paper)
    chunks = [
        c for c in corpus.chunk_units(units) if c.meta["qasper"]["para_id"] == "s0p2"
    ]
    assert len(chunks) > 1
    a, b = chunks[0].meta["qasper"], chunks[1].meta["qasper"]
    assert b["para_char_start"] < a["para_char_end"]  # they overlap
    s, e = b["para_char_start"], a["para_char_end"]
    unit = next(u for u in units if u.para_id == "s0p2")
    row = _row_with(
        ("qasper:x", "s0p2", [s, e], [unit.doc_start + s, unit.doc_start + e])
    )
    hits = [gold.spans_overlap_chunk(row, c.meta) for c in chunks]
    assert hits[0] and hits[1] and not any(hits[2:])
    # and the other paragraphs' chunks never match
    others = [
        c for c in corpus.chunk_units(units) if c.meta["qasper"]["para_id"] != "s0p2"
    ]
    assert not any(gold.spans_overlap_chunk(row, c.meta) for c in others)


# ---------------------------------------------------------------------------
# planted adapter
# ---------------------------------------------------------------------------

AT = "2026-11-09T12:00:00Z"


def _hosts() -> list[dict]:
    return [
        {
            "id": f"host-{d}",
            "paragraphs": [
                f"Host document {d} paragraph {p} discusses weather, rivers and "
                f"old bridges in some detail. A second sentence follows for {p}."
                for p in range(10)
            ],
        }
        for d in range(6)
    ]


def test_unit_sep_matches_the_corpus_canonical_text():
    assert gold.UNIT_SEP == corpus._UNIT_SEP


def test_paragraph_offsets_follow_canonical_text(papers):
    paras = ["alpha", "", "  ", "beta gamma", "delta"]
    off = gold.paragraph_offsets(paras)
    assert off == {0: 0, 3: 7, 4: 19}  # blanks skipped, indexes kept
    assert gold.joined_text(paras) == "alpha\n\nbeta gamma\n\ndelta"
    # identical to corpus.canonical_units for a title/abstract-free paper
    paper = {"title": "", "abstract": "", "full_text": [{"paragraphs": paras}]}
    for u in corpus.canonical_units(paper):
        assert off[u.para_idx] == u.doc_start
    assert gold.joined_text(paras) == corpus.canonical_text(paper)


def test_from_planted_round_trips_a_small_build():
    rows, side = planted.build(_hosts(), seed=7, n=20, decoys=1, at=AT)
    docs = {d["id"]: d["paragraphs"] for d in side["docs"]}
    assert {r["type"] for r in rows} == {"lookup", "k-hop", "stance", "memory"}
    n_items = 0
    for row in rows:
        out = gold.from_planted(row, side["docs"])
        gold.validate_row(out)
        assert out["labelled_by"] == "planted" and out["id"] == row["id"]
        assert "gold_spans" not in out
        assert out["answer"] == [str(row["answer"])]
        assert out["type"] == row["type"]
        assert out["source"]["at"] == AT
        assert out["source"]["paraphrase_level"] == row["paraphrase_level"]
        assert out["source"]["difficulty"] == row["difficulty"]
        assert len(out["evidence"]) == len(row["evidence"])
        if row["type"] == "memory":
            assert out["evidence"] == [] and out["docs"] == []
        if row["type"] == "stance":
            assert out["answer_kind"] == "yes_no"
        else:
            assert out["answer_kind"] == "exact"
        for ev, src in zip(out["evidence"], row["evidence"], strict=True):
            n_items += 1
            assert ev["unit"] == src["paragraph"] and ev["doc"] == src["doc"]
            assert ev["span"] == src["span"]
            paras = docs[ev["doc"]]
            s, e = ev["span"]
            sliced = paras[ev["unit"]][s:e]
            assert sliced.startswith("The ") and sliced.endswith((".",))
            ds, de = ev["doc_span"]
            assert gold.joined_text(paras)[ds:de] == sliced
            assert ev["doc"] in out["docs"]
    assert n_items > 20


def test_from_planted_accepts_a_doc_mapping_and_works_without_docs():
    rows, side = planted.build(_hosts(), seed=3, n=8, at=AT)
    row = next(r for r in rows if r["evidence"])
    by_id = {d["id"]: d["paragraphs"] for d in side["docs"]}
    assert gold.from_planted(row, by_id) == gold.from_planted(row, side["docs"])
    bare = gold.from_planted(row)
    gold.validate_row(bare)
    assert all("doc_span" not in e for e in bare["evidence"])
    # no doc_span: scoring falls back to the unit-relative span
    ev = bare["evidence"][0]
    chunk = {"para_id": str(ev["unit"]), "para_char_start": 0, "para_char_end": 10**6}
    assert gold.spans_overlap_chunk(bare, chunk)


def test_from_planted_doc_span_uses_blank_paragraph_rule():
    row = {
        "id": "planted-1-0000",
        "type": "lookup",
        "question": "q",
        "answer": "7 kg",
        "evidence": [{"doc": "d", "paragraph": 3, "span": [4, 9]}],
        "gold_spans": [{"doc": "d", "paragraph": 3, "span": [4, 9]}],
        "paraphrase_level": 0,
        "overlap": 0.1,
        "difficulty": {},
        "labelled_by": "planted",
        "at": AT,
    }
    out = gold.from_planted(
        row, [{"id": "d", "paragraphs": ["aa", "", "bb", "0123456789"]}]
    )
    # "aa" (0..2) + sep, "bb" (4..6) + sep, paragraph 3 starts at 8
    assert out["evidence"][0]["doc_span"] == [8 + 4, 8 + 9]
    assert out["evidence"][0]["unit"] == 3


# ---------------------------------------------------------------------------
# committed rows and the real archive
# ---------------------------------------------------------------------------


def test_committed_quick_tasks_are_well_formed():
    rows = gold.load_tasks(_TASKS)  # validates every row, ids unique
    man = corpus.load_manifest()
    quick = {r["qasper_id"]: r for r in man["quick"]}
    assert len(rows) == sum(r["n_questions"] for r in quick.values())
    assert {r["source"]["qasper_id"] for r in rows} == set(quick)
    for r in rows:
        rec = quick[r["source"]["qasper_id"]]
        assert r["docs"] == [f"qasper:{rec['qasper_id']}"]
        assert r["source"]["paper_sha256"] == rec["sha256"]
        assert r["labelled_by"] == "qasper"
        assert r["type"] in ("lookup", "grounding")
        assert (r["type"] == "lookup") == (
            r["answer_kind"] in ("extractive", "unanswerable")
        )
        for item in r["evidence"]:
            assert item["doc"] in r["docs"]
            assert (
                item["doc_span"][1] - item["doc_span"][0]
                == item["span"][1] - item["span"][0]
            )
    kinds = {r["answer_kind"] for r in rows}
    assert kinds == {"extractive", "abstractive", "yes_no", "unanswerable"}
    assert sum(1 for r in rows if r["evidence"]) > len(rows) * 0.8
    # every unmapped string is reported on its row, not dropped
    for r in rows:
        for u in r.get("unmapped_evidence", []):
            assert u in r["source"]["evidence_raw"]


def _cached_archive() -> Path | None:
    p = corpus.cache_dir() / corpus.ARCHIVE_NAME
    return p if p.is_file() else None


@pytest.mark.skipif(
    _cached_archive() is None, reason="QASPER archive not in the bench cache"
)
def test_committed_quick_tasks_match_the_real_archive():
    archive = _cached_archive()
    assert archive is not None
    if corpus._file_sha256(archive) != corpus.ARCHIVE_SHA256:
        pytest.skip("cached archive fails its pin")
    papers = corpus.load_archive(archive)
    man = corpus.load_manifest()
    rows, report = qasper_tasks.build_tasks(man["quick"], papers)
    assert qasper_tasks.tasks_text(rows) == _TASKS.read_text(encoding="utf-8")
    assert check_rows_against_papers(rows, papers) == sum(
        len(r["evidence"]) for r in rows
    )
    # yield: at least 95 % of distinct evidence strings land on a unit
    assert report.counts["unmapped"] / report.total < 0.05


# ---------------------------------------------------------------------------
# scripts/bench tasks
# ---------------------------------------------------------------------------


def _write_archive(path: Path, papers) -> None:
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


def test_cli_tasks_writes_checks_and_prints_the_yield(
    papers, records, tmp_path, capsys
):
    _write_archive(tmp_path / corpus.ARCHIVE_NAME, papers)
    manifest = {
        "schema": corpus.MANIFEST_SCHEMA,
        "dataset": {
            "archive": {"sha256": corpus._file_sha256(tmp_path / corpus.ARCHIVE_NAME)}
        },
        "corpus_sha256": {
            "quick": corpus.corpus_sha256(records),
            "full": corpus.corpus_sha256(records),
        },
        "quick": records,
        "full": records,
    }
    man_path = tmp_path / "manifest.json"
    man_path.write_text(corpus.manifest_text(manifest), encoding="utf-8")
    out = tmp_path / "out" / "tasks.jsonl"
    argv = [
        "tasks",
        "--set",
        "quick",
        "--manifest",
        str(man_path),
        "--cache",
        str(tmp_path),
        "--out",
        str(out),
    ]
    assert cli.main(argv) == 0
    shown = capsys.readouterr().out
    assert "unmapped" in shown and "normalised" in shown and "fuzzy" in shown
    assert f"wrote {out} (21 rows" in shown
    assert len(gold.load_tasks(out)) == 21
    assert cli.main([*argv, "--check"]) == 0
    out.write_text(out.read_text(encoding="utf-8") + "\n{}\n", encoding="utf-8")
    assert cli.main([*argv, "--check"]) == 1
    assert "differs" in capsys.readouterr().err


def test_default_tasks_path_is_under_bench_tasks():
    p = qasper_tasks.default_tasks_path("quick")
    assert p.name == "qasper-quick.jsonl" and p.parent.name == "tasks"
    assert p.parent.parent.name == "bench"
