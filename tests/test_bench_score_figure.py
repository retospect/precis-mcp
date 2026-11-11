"""QASPER answer scorer and the report's recall-vs-tokens figure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.bench import cli, figure, score


def test_normalize_follows_qasper() -> None:
    assert score.normalize("The  Quick, brown Fox!") == "quick brown fox"
    assert score.normalize("An apple a day") == "apple day"


def test_token_f1_hand_cases() -> None:
    assert score.token_f1("the cat sat", "cat sat") == 1.0
    # pred 3 tokens, gold 2, common 2: p=2/3 r=1 -> 0.8
    assert score.token_f1("cat sat down", "cat sat") == pytest.approx(0.8)
    assert score.token_f1("dog", "cat") == 0.0
    assert score.token_f1("", "") == 1.0
    assert score.token_f1("", "cat") == 0.0
    # multiset overlap: common = 1, p = 1/3, r = 1/2 (a/an/the are dropped)
    assert score.token_f1("x b b", "b c") == pytest.approx(0.4)


def test_max_over_annotators() -> None:
    answers = ["bert base", "a transformer encoder"]
    assert score.score_answer("Transformer encoder.", answers, "abstractive") == 1.0
    assert score.score_answer("bert", answers, "extractive") == pytest.approx(2 / 3)


def test_yes_no_is_exact() -> None:
    assert score.score_answer("Yes", ["yes"], "yes_no") == 1.0
    assert score.score_answer("yes, it does", ["yes"], "yes_no") == 0.0
    assert score.score_answer("no", ["yes"], "yes_no") == 0.0


def test_unanswerable_means_abstain() -> None:
    row = {"answer": [], "answer_kind": "unanswerable"}
    assert score.score_row("Unanswerable.", row) == 1.0
    assert score.score_row("", row) == 1.0
    assert score.score_row("some guess", row) == 0.0
    # abstaining on an answerable row is wrong
    assert score.score_answer("unanswerable", ["x"], "extractive") == 0.0


def _row(
    arm: str, task: str, tok: int | None, censored: bool, r: float
) -> dict[str, Any]:
    return {
        "task": task,
        "arm": arm,
        "source": "qasper",
        "abstention": False,
        "applicable": True,
        "censored": censored,
        "found": not censored,
        "tokens_to_first_gold": tok,
        "tokens_total": 100,
        "calls": 3,
        "verbatim_opens": 1,
        "refusals": 0,
        "errors": 0,
        "recall_at_1": r / 2,
        "recall_at_3": r,
        "recall_at_5": r,
        "recall_at_10": min(1.0, r + 0.2),
    }


def _results(caller: str = "triage") -> dict[str, Any]:
    rows = [
        _row("all-on", "t1", 400, False, 1.0),
        _row("all-on", "t2", 600, False, 0.5),
        _row("no-kwd", "t1", 900, False, 0.5),
        _row("no-kwd", "t2", 12000, True, 0.0),
        _row("no-recall", "t1", 12000, True, 0.0),  # nothing reached: omitted
    ]
    return {"header": {"caller": {"name": caller}, "arms": {}}, "rows": rows}


def test_figure_points() -> None:
    pts = {p["label"]: p for p in figure.figure_points([_results()])}
    assert set(pts) == {"all-on", "no-kwd"}
    assert pts["all-on"]["x"] == 500.0
    assert pts["all-on"]["recall"][5] == pytest.approx(0.75)
    assert pts["no-kwd"]["censored"] == 1


def test_figure_labels_carry_caller_for_several_files() -> None:
    labels = {p["label"] for p in figure.figure_points([_results(), _results("rowkw")])}
    assert "all-on (triage)" in labels and "all-on (rowkw)" in labels


def test_report_figure_writes_svg_and_png(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "res.json"
    src.write_text(json.dumps(_results()), encoding="utf-8")
    svg, png = tmp_path / "f.svg", tmp_path / "f.png"
    assert cli.main(["report", str(src), "--figure", str(svg)]) == 0
    assert cli.main(["report", str(src), "--figure", str(png)]) == 0
    assert b"<svg" in svg.read_bytes()
    assert png.read_bytes().startswith(b"\x89PNG")
    assert "censored" in svg.read_text(encoding="utf-8")
    assert "figure:" in capsys.readouterr().out


def test_figure_rejects_bad_suffix_and_empty(tmp_path: Path) -> None:
    src = tmp_path / "res.json"
    src.write_text(json.dumps(_results()), encoding="utf-8")
    assert cli.main(["report", str(src), "--figure", str(tmp_path / "f.pdf")]) != 0
    with pytest.raises(ValueError, match="nothing to plot"):
        figure.render_figure([{"header": {}, "rows": []}])
