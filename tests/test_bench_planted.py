"""Planted-fact generator (memory-ablation slice 4b): determinism, spans, dials."""

import copy
import itertools
import json
import random
import re
from typing import Any

import pytest

from precis.bench import planted as pl

AT = "2026-11-09T12:00:00Z"
WORDS = [
    "analysis",
    "sample",
    "method",
    "result",
    "model",
    "data",
    "signal",
    "sensor",
    "network",
    "training",
    "layer",
    "protein",
    "cell",
    "tissue",
    "growth",
    "rate",
    "noise",
    "error",
    "figure",
    "table",
    "theory",
    "experiment",
    "measure",
    "control",
    "group",
    "baseline",
    "response",
    "energy",
    "surface",
    "region",
    "pattern",
    "feature",
    "input",
    "output",
    "weight",
    "kernel",
    "batch",
    "epoch",
    "cluster",
]


def make_hosts(n_docs=20, n_paras=10, seed=0):
    rng = random.Random(seed)

    def para():
        sents = [
            " ".join(rng.choices(WORDS, k=rng.randint(6, 12))).capitalize() + "."
            for _ in range(rng.randint(3, 5))
        ]
        return " ".join(sents)

    hosts: list[dict[str, Any]] = [
        {"id": f"doc{d}", "paragraphs": [para() for _ in range(n_paras)]}
        for d in range(n_docs)
    ]
    hosts[0]["paragraphs"][0] = "No sentence boundary here"
    hosts[0]["paragraphs"][1] = "Trailing space paragraph. "
    hosts[0]["paragraphs"][2] = ""
    return hosts


def run(seed=7, n=40, **kw):
    hosts = make_hosts()
    rows, side = pl.build(hosts, seed, n, at=AT, **kw)
    return hosts, rows, side


def sentence_at(side, ev):
    doc = next(d for d in side["docs"] if d["id"] == ev["doc"])
    s, e = ev["span"]
    return doc["paragraphs"][ev["paragraph"]][s:e]


def differing_middles(a, b):
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    q = 0
    while q < min(len(a), len(b)) - p and a[-1 - q] == b[-1 - q]:
        q += 1
    return a[p : len(a) - q], b[p : len(b) - q]


def test_same_seed_identical_and_inputs_untouched():
    hosts = make_hosts()
    before = copy.deepcopy(hosts)
    a = pl.build(hosts, 3, 40, at=AT, decoys=1)
    b = pl.build(hosts, 3, 40, at=AT, decoys=1)
    assert hosts == before
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    c = pl.build(hosts, 4, 40, at=AT, decoys=1)
    assert json.dumps(a[0]) != json.dumps(c[0])


def test_types_and_levels_covered():
    _, rows, _ = run()
    assert {r["type"] for r in rows} == set(pl.TYPES)
    assert {r["paraphrase_level"] for r in rows} == {0, 1, 2}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_spans_index_exactly_the_planted_sentence(seed):
    hosts, rows, side = run(seed=seed, decoys=2)
    nodes_by_task: dict[str, list[str]] = {}
    for nd in side["memory_nodes"]:
        nodes_by_task.setdefault(nd["task_id"], []).append(nd["body"])
    checked = 0
    for r in rows:
        assert r["gold_spans"] == r["evidence"]
        for ev in r["evidence"] + r["difficulty"]["decoy_evidence"]:
            got = sentence_at(side, ev)
            assert got in nodes_by_task[r["id"]]
            assert got.endswith(".") and got.startswith("The ")
            # the host text is otherwise untouched
            orig = next(h for h in hosts if h["id"] == ev["doc"])["paragraphs"][
                ev["paragraph"]
            ]
            new = next(d for d in side["docs"] if d["id"] == ev["doc"])["paragraphs"][
                ev["paragraph"]
            ]
            s, e = ev["span"]
            assert " ".join((new[:s] + new[e:]).split()) == " ".join(orig.split())
            checked += 1
    assert checked > 60
    # only memory tasks have no document evidence
    assert all((not r["evidence"]) == (r["type"] == "memory") for r in rows)


def test_one_plant_per_paragraph():
    _, rows, _ = run(decoys=2)
    seen = set()
    for r in rows:
        for ev in r["evidence"] + r["difficulty"]["decoy_evidence"]:
            key = (ev["doc"], ev["paragraph"])
            assert key not in seen
            seen.add(key)


def test_too_few_paragraphs_raises():
    with pytest.raises(ValueError, match="not enough host paragraphs"):
        pl.build([{"id": "d", "paragraphs": ["One."]}], 1, 10, at=AT)


def test_entity_names_do_not_collide():
    hosts, rows, side = run()
    host_tokens = {
        t
        for h in hosts
        for p in h["paragraphs"]
        for t in re.findall(r"[a-z0-9]+", p.lower())
    }
    names = set()
    for nd in side["memory_nodes"]:
        for w in re.findall(r"[A-Z][a-z]{5,}", nd["body"]):
            if w != "The":
                names.add(w.lower())
    assert names and not names & host_tokens


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
def test_level2_shares_no_content_words_beyond_entity(seed):
    hosts = make_hosts()
    rows, side = pl.build(hosts, seed, 60, at=AT, levels=(2,))
    bodies: dict[str, list[str]] = {}
    for nd in side["memory_nodes"]:
        bodies.setdefault(nd["task_id"], []).append(nd["body"])
    for r in rows:
        plant = set().union(*(pl.content_words(b) for b in bodies[r["id"]]))
        shared = pl.content_words(r["question"]) & plant
        # shared words are all made-up names (capitalised nonsense tokens in the question)
        names = {w.lower() for w in re.findall(r"\b[A-Z][a-z]{5,}\b", r["question"])}
        assert shared <= names, (r["question"], shared - names)
        assert len(shared) <= 1
        assert r["overlap"] <= 0.25


def test_overlap_falls_with_paraphrase_level():
    mean = {}
    for lvl in (0, 1, 2):
        rows, _ = pl.build(make_hosts(), 5, 60, at=AT, levels=(lvl,))
        mean[lvl] = sum(r["overlap"] for r in rows) / len(rows)
        assert all(0.0 <= r["overlap"] <= 1.0 for r in rows)
    assert mean[0] > mean[1] > mean[2], mean


def test_level1_substitutes_synonyms_but_keeps_entity():
    assert not set(pl.SYNONYMS.values()) & set(pl.SYNONYMS)
    assert (
        pl.synonymize("What is the boiling point of Zorvanel?")
        == "What is the vaporising threshold of Zorvanel?"
    )
    rows, _ = pl.build(make_hosts(), 5, 30, at=AT, levels=(1,))
    assert any(r["type"] == "lookup" for r in rows)


def test_khop_answers_need_every_component():
    _, rows, side = run(n=80)
    bodies: dict[str, list[str]] = {}
    for nd in side["memory_nodes"]:
        bodies.setdefault(nd["task_id"], []).append(nd["body"])
    hop_rows = [r for r in rows if r["type"] == "k-hop"]
    assert hop_rows and {r["difficulty"]["hops"] for r in hop_rows} == {2, 3}
    for r in hop_rows:
        k = r["difficulty"]["hops"]
        sents = [sentence_at(side, ev) for ev in r["evidence"]]
        assert len(sents) == k == len(r["difficulty"]["fact_ids"])
        assert sents == bodies[r["id"]]
        m_first = re.search(r"of (\w+) (?:is|was)", sents[0])
        assert m_first is not None
        first = m_first.group(1)
        assert first in r["question"]
        for prev, cur in itertools.pairwise(sents):
            m_hop = re.search(r"is (\w+)\.$", prev)
            assert m_hop is not None
            hop_name = m_hop.group(1)
            assert hop_name in cur  # fact i+1 is about fact i's answer
            assert hop_name not in r["question"]  # intermediate hop is not given away
        assert r["answer"] in sents[-1]
        assert all(r["answer"] not in s for s in sents[:-1])
        assert r["answer"] not in r["question"]
        assert [e["doc"] for e in r["evidence"]]  # spans for every component


def test_decoys_differ_from_plant_in_exactly_one_value():
    _, rows, side = run(decoys=2, n=60)
    n_checked = 0
    for r in rows:
        if r["type"] not in ("lookup", "k-hop"):
            assert r["difficulty"]["decoys"] == 0
            continue
        assert r["difficulty"]["decoys"] == len(r["difficulty"]["decoy_evidence"]) == 2
        plant = sentence_at(side, r["evidence"][-1])
        values = set()
        for d in r["difficulty"]["decoy_evidence"]:
            ds = sentence_at(side, d)
            assert ds != plant
            mid_p, mid_d = differing_middles(plant, ds)
            assert mid_d and mid_d in d["value"]
            assert mid_p in r["answer"]
            assert d["answer"] != r["answer"]
            # everything outside the value slot is identical
            assert ds.replace(mid_d, "#", 1) == plant.replace(mid_p, "#", 1)
            values.add(d["value"])
            n_checked += 1
        assert len(values) == 2
    assert n_checked >= 20


def test_decoy_variant_changes_only_the_value_slot():
    m = pl._Minter(random.Random(1), 1, set())
    for kind in pl.KINDS:
        for _ in range(25):
            f = m.fact(kind)
            v = m.variant(f)
            assert v.value != f.value
            assert (v.entity, v.key, v.unit, v.kind) == (
                f.entity,
                f.key,
                f.unit,
                f.kind,
            )
            assert v.sentence().replace(v.value, "#") == f.sentence().replace(
                f.value, "#"
            )


def test_stance_tasks_agree_and_conflict():
    _, rows, side = run(n=80)
    seen = set()
    for r in [r for r in rows if r["type"] == "stance"]:
        a, b = (sentence_at(side, ev) for ev in r["evidence"])
        st = r["difficulty"]["stance"]
        seen.add(st)
        assert r["question"].startswith("Do the documents agree?")
        if st == "agree":
            assert a == b and r["answer"] == "yes"
        else:
            assert a != b and r["answer"] == "no"
            mid_a, mid_b = differing_middles(a, b)
            assert mid_a and mid_b
            assert a.split()[:3] == b.split()[:3]
        assert r["evidence"][0]["doc"] != r["evidence"][1]["doc"]
    assert seen == {"agree", "conflict"}


def test_memory_nodes_title_leads_with_claim_and_is_short():
    _, rows, side = run(decoys=1)
    roles = {nd["role"] for nd in side["memory_nodes"]}
    assert roles == {"gold", "decoy", "contradiction"}
    for nd in side["memory_nodes"]:
        assert len(nd["title"].split()) <= 12
        assert nd["body"].endswith(".")
        value_tokens = re.findall(r"[A-Za-z0-9]+", nd["title"])
        assert len(value_tokens) >= 3  # entity, property and value all in the title
    # memory tasks: fact exists only as a node
    mem = [r for r in rows if r["type"] == "memory"]
    assert mem and all(r["evidence"] == [] and r["gold_spans"] == [] for r in mem)


def test_far_dial_picks_lower_overlap_hosts():
    def mean_host_overlap(frac):
        rows, _ = pl.build(make_hosts(), 9, 40, at=AT, far_fraction=frac)
        vals = [v for r in rows for v in r["difficulty"]["host_overlap"]]
        assert {
            r["difficulty"]["topical_distance"] for r in rows if r["type"] != "memory"
        } == {"far" if frac else "any"}
        return sum(vals) / len(vals)

    assert mean_host_overlap(1.0) <= mean_host_overlap(0.0)


def test_row_shape_and_cli(tmp_path):
    hosts = make_hosts()
    hp, out, docs = (
        tmp_path / "h.json",
        tmp_path / "o" / "t.jsonl",
        tmp_path / "o" / "d.json",
    )
    hp.write_text(json.dumps(hosts), encoding="utf-8")
    argv = [
        "--seed",
        "5",
        "--n",
        "20",
        "--hosts",
        str(hp),
        "--out",
        str(out),
        "--docs-out",
        str(docs),
        "--decoys",
        "1",
    ]
    assert pl.main(argv) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 20
    for r in rows:
        assert set(r) == {
            "id", "type", "question", "answer", "evidence", "gold_spans", "paraphrase_level",
            "overlap", "difficulty", "labelled_by", "at",
        }  # fmt: skip
        assert r["labelled_by"] == "planted" and r["at"].endswith("Z")
        assert r["question"].endswith("?")
        for ev in r["evidence"]:
            assert set(ev) == {"doc", "paragraph", "span"}
    side = json.loads(docs.read_text(encoding="utf-8"))
    assert set(side) == {"seed", "at", "docs", "memory_nodes"}
    assert {d["id"] for d in side["docs"]} == {h["id"] for h in hosts}
    # CLI output equals the library call under the same stamp
    again = tmp_path / "again.jsonl"
    pl.main(
        argv[:6]
        + [
            "--out",
            str(again),
            "--docs-out",
            str(tmp_path / "d2.json"),
            "--decoys",
            "1",
            "--at",
            rows[0]["at"],
        ]
    )
    assert again.read_text(encoding="utf-8") == out.read_text(encoding="utf-8")
