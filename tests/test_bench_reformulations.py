"""Committed query reformulations for the multi-query arm (td664457):
the file contract, the generator's no-leak and retry rules, and the files
on disk."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from precis.bench import gold, qasper_tasks, runner
from precis.bench import reformulations as rf

QUESTION = "What is CamemBERT trained on?"
GOOD = {
    "queries": [
        "What training data was used for CamemBERT?",
        "Which corpus is CamemBERT pretrained on?",
        "CamemBERT training dataset",
    ],
    "answers": ["CamemBERT is trained on a large French web corpus."],
}
PLANTED_SEEDS = (7, 1, 2, 3)  # the reported seed and the dev seeds


def _row(task: str = "t1", question: str = QUESTION, **over: Any) -> dict[str, Any]:
    row = {
        "task": task,
        "queries": list(GOOD["queries"]),
        "answers": list(GOOD["answers"]),
        "model": rf.DEFAULT_MODEL,
        "prompt_sha256": "a" * 64,
        "question_sha256": rf.sha256_text(question),
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# prompt: the generator sees the question and nothing else
# ---------------------------------------------------------------------------


def test_prompt_template_has_one_question_slot():
    template, sha = rf.load_prompt()
    assert template.count(rf.PLACEHOLDER) == 1
    assert sha == rf.sha256_text(template) and len(sha) == 64


def test_prompt_carries_the_question_and_no_gold():
    template, _ = rf.load_prompt()
    rows = gold.load_tasks(qasper_tasks.default_tasks_path("quick"))
    assert rows
    for row in rows:
        prompt = rf.render_prompt(template, row["question"])
        assert prompt == template.replace(rf.PLACEHOLDER, row["question"].strip())
        for evidence in row["source"].get("evidence_raw", []):
            assert evidence not in prompt


# ---------------------------------------------------------------------------
# reply and row validation
# ---------------------------------------------------------------------------


def test_check_texts_accepts_a_full_reply_and_single_spaces_it():
    reply = {
        "queries": [*GOOD["queries"][:2], "CamemBERT  training\ndataset"],
        "answers": GOOD["answers"],
    }
    queries, answers = rf.check_texts(reply, QUESTION, exact=True)
    assert queries == GOOD["queries"] and answers == GOOD["answers"]


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        ({"queries": ["a", "b"], "answers": ["c"]}, "2 entries"),
        ({"queries": ["a", "b", "c"], "answers": []}, "0 entries"),
        ({"queries": ["a", "b", "c", "d"], "answers": ["e"]}, "4 entries"),
        ({"queries": ["a", "b", "A?"], "answers": ["c"]}, "the same"),
        (
            {"queries": ["a", "b", "what is camembert trained on"], "answers": ["c"]},
            "equals",
        ),
        ({"queries": ["a", "b", "x" * 301], "answers": ["c"]}, "cap"),
        ({"queries": ["a", "b", " "], "answers": ["c"]}, "empty"),
        ({"queries": "a", "answers": ["c"]}, "list of strings"),
        ({"queries": ["a", "b", 3], "answers": ["c"]}, "list of strings"),
    ],
)
def test_check_texts_refuses(reply: dict[str, Any], match: str):
    with pytest.raises(rf.ReformulationError, match=match):
        rf.check_texts(reply, QUESTION, exact=True)


def test_a_stored_row_may_carry_fewer_strings():
    rf.validate_row(_row(queries=["one rephrasing"], answers=[]))
    with pytest.raises(rf.ReformulationError, match="0 entries"):
        rf.validate_row(_row(queries=[]))
    with pytest.raises(rf.ReformulationError, match="keys"):
        rf.validate_row({**_row(), "evidence": "leak"})
    with pytest.raises(rf.ReformulationError, match="sha256"):
        rf.validate_row(_row(prompt_sha256="abc"))


def test_load_names_the_bad_line_and_refuses_duplicates(tmp_path: Path):
    path = tmp_path / "x.queries.jsonl"
    path.write_text(rf.dump([_row("t1"), _row("t1")]), encoding="utf-8")
    with pytest.raises(rf.ReformulationError, match=r":2: duplicate task 't1'"):
        rf.load(path)
    path.write_text(rf.dump([_row("t1")]) + "{not json}\n", encoding="utf-8")
    with pytest.raises(rf.ReformulationError, match=":2:"):
        rf.load(path)


def test_lookup_needs_the_same_question():
    rows = {"t1": _row("t1")}
    assert rf.lookup(rows, "t1", QUESTION) is rows["t1"]
    assert rf.lookup(rows, "t1", f"  {QUESTION} ") is rows["t1"]
    assert rf.lookup(rows, "t1", "Another question?") is None
    assert rf.lookup(rows, "t2", QUESTION) is None


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------


class _Stub:
    """A transport that replays scripted replies and records its prompts."""

    def __init__(self, replies: list[Any], model: str = rf.DEFAULT_MODEL) -> None:
        self.replies = list(replies)
        self.model = model
        self.prompts: list[str] = []

    def __call__(self, prompt: str, model: str) -> tuple[Mapping[str, Any], str]:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else GOOD
        if isinstance(reply, Exception):
            raise reply
        return reply, self.model


def _gen(questions: list[tuple[str, str]], stub: _Stub, **kw: Any):
    return rf.generate(
        questions,
        template="Q: {question}",
        model=rf.DEFAULT_MODEL,
        transport=stub,
        workers=1,
        **kw,
    )


def test_generate_writes_one_row_per_question_in_input_order():
    stub = _Stub([])
    rows, failures = _gen([("t2", "Second question?"), ("t1", QUESTION)], stub)
    assert failures == [] and [r["task"] for r in rows] == ["t2", "t1"]
    assert stub.prompts == ["Q: Second question?", f"Q: {QUESTION}"]
    assert rows[1] == _row("t1", prompt_sha256=rf.sha256_text("Q: {question}"))
    for row in rows:
        rf.validate_row(row)


def test_generate_retries_a_bad_reply_then_reports_the_failure():
    bad = {"queries": [QUESTION, "b", "c"], "answers": ["d"]}
    stub = _Stub([bad, RuntimeError("claude -p timed out"), GOOD])
    rows, failures = _gen([("t1", QUESTION)], stub)
    assert failures == [] and len(rows) == 1 and len(stub.prompts) == 3

    stub = _Stub([bad] * rf.ATTEMPTS)
    rows, failures = _gen([("t1", QUESTION)], stub)
    assert rows == [] and len(stub.prompts) == rf.ATTEMPTS
    assert len(failures) == 1 and failures[0].startswith("t1: ")


def test_generate_refuses_a_reply_from_another_model():
    rows, failures = _gen([("t1", QUESTION)], _Stub([], model="claude-haiku-5-5"))
    assert rows == [] and "reported 'claude-haiku-5-5'" in failures[0]
    dated = _Stub([], model=f"{rf.DEFAULT_MODEL}-20261101")
    rows, failures = _gen([("t1", QUESTION)], dated)
    assert failures == [] and rows[0]["model"] == f"{rf.DEFAULT_MODEL}-20261101"


def test_generate_resumes_from_finished_rows():
    sha = rf.sha256_text("Q: {question}")
    done = {
        "t1": _row("t1", prompt_sha256=sha, queries=["kept"]),
        "t2": _row("t2", prompt_sha256=sha),  # written for another question
        "t3": _row("t3", "Third?", prompt_sha256="b" * 64),  # another prompt
    }
    new: list[str] = []
    stub = _Stub([])
    rows, failures = _gen(
        [("t1", QUESTION), ("t2", "Second question?"), ("t3", "Third?")],
        stub,
        done=done,
        on_row=lambda r: new.append(r["task"]),
    )
    assert failures == [] and rows[0]["queries"] == ["kept"]
    assert new == ["t2", "t3"] and len(stub.prompts) == 2


def test_reported_model_reads_the_envelope():
    env = {
        "modelUsage": {
            "claude-haiku-5-5": {"outputTokens": 3},
            "claude-opus-5-5": {"outputTokens": 90},
        }
    }
    assert rf.reported_model(json.dumps(env)) == "claude-opus-5-5"
    assert rf.reported_model("plain text") is None
    assert rf.reported_model(json.dumps({"result": "x"})) is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _questions_file(tmp_path: Path) -> Path:
    path = tmp_path / "q.jsonl"
    path.write_text(rf.dump([{"task": "t1", "question": QUESTION}]), encoding="utf-8")
    return path


def test_reformulate_refuses_to_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.chdir(tmp_path)  # the command changes directory; restore after
    calls: list[str] = []

    def transport(prompt: str, model: str) -> tuple[Mapping[str, Any], str]:
        calls.append(prompt)
        return GOOD, model

    monkeypatch.setattr(rf, "claude_cli", transport)
    out = tmp_path / "t.queries.jsonl"
    out.write_text("committed\n", encoding="utf-8")
    argv = ["reformulate", "--questions", str(_questions_file(tmp_path))]
    argv += ["--name", "t", "--out", str(out)]
    assert rf.main(argv) == 1
    assert "pass --force" in capsys.readouterr().err
    assert calls == [] and out.read_text(encoding="utf-8") == "committed\n"

    assert rf.main([*argv, "--force"]) == 0
    rows = rf.load(out)
    assert list(rows) == ["t1"] and len(calls) == 1
    assert rf.lookup(rows, "t1", QUESTION) is not None
    assert not out.with_name(out.name + ".partial").exists()


def test_reformulate_keeps_finished_rows_when_a_question_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.chdir(tmp_path)
    questions = tmp_path / "q.jsonl"
    pairs = [{"task": "t1", "question": QUESTION}, {"task": "t2", "question": "Bad?"}]
    questions.write_text(rf.dump(pairs), encoding="utf-8")

    def transport(prompt: str, model: str) -> tuple[Mapping[str, Any], str]:
        if "Bad?" in prompt:
            raise RuntimeError("claude -p exited 1")
        return GOOD, model

    monkeypatch.setattr(rf, "claude_cli", transport)
    out = tmp_path / "t.queries.jsonl"
    argv = ["reformulate", "--questions", str(questions), "--name", "t"]
    argv += ["--out", str(out), "--workers", "1"]
    assert rf.main(argv) == 1
    assert "1 of 2 questions failed" in capsys.readouterr().err
    assert not out.exists()
    assert list(rf.load(out.with_name(out.name + ".partial"))) == ["t1"]


# ---------------------------------------------------------------------------
# the committed files
# ---------------------------------------------------------------------------


def _committed(name: str) -> dict[str, dict[str, Any]]:
    rows = rf.load(rf.default_path(name))
    _, prompt_sha = rf.load_prompt()
    for row in rows.values():
        assert row["prompt_sha256"] == prompt_sha
        assert row["model"] == rf.DEFAULT_MODEL
        assert len(row["queries"]) == rf.MAX_QUERIES
        assert len(row["answers"]) == rf.MAX_ANSWERS
    return rows


def test_committed_qasper_quick_covers_its_task_set():
    rows = _committed(rf.default_name("quick"))
    tasks = gold.load_tasks(qasper_tasks.default_tasks_path("quick"))
    assert set(rows) == {t["id"] for t in tasks}
    for task in tasks:
        row = rf.lookup(rows, task["id"], task["question"])
        assert row is not None, task["id"]
        rf.check_texts(row, task["question"], exact=True)


@pytest.mark.parametrize("seed", PLANTED_SEEDS)
def test_committed_planted_files_cover_their_seed(seed: int):
    rows = _committed(rf.default_name("quick", seed))
    ids = re.compile(rf"planted-{seed}-\d{{4}}")
    assert len(rows) == runner.PLANTED_N
    assert all(ids.fullmatch(task) for task in rows)
    assert runner.PLANTED_SEED in PLANTED_SEEDS
