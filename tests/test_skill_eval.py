"""Per-skill eval harness (``precis.skill_eval``): loader, criterion checker,
report through the fake runner, the live runner's budget/auth behaviour with
a stubbed ``call_claude_p``, and the advisory standing (never a ship gate).
Hermetic: no DB, no model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.errors import BadInput
from precis.skill_eval import (
    CallPattern,
    Criterion,
    EvalCase,
    FakeRunner,
    LiveClaudeRunner,
    Transcript,
    VerbCall,
    check,
    default_cases_dir,
    load_cases,
    run_cases,
)
from precis.skill_eval.__main__ import main
from precis.skill_eval.cases import load_case_file, value_matches
from precis.skill_eval.runners import build_prompt
from precis.utils.claude_p import ClaudePError, ClaudePResult

ROOT = Path(__file__).resolve().parent.parent
SEED_SKILLS = ("precis-overview", "precis-paper-help", "precis-draft-help")

_MINIMAL = """\
skill: demo-skill
cases:
  - id: one
    prompt: do the thing
    expect:
      calls: [{verb: get, kind: paper, args: {id: pa1}}]
    fake:
      calls: [{verb: get, kind: paper, args: {id: pa1}}]
      answer: done
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _case(criterion: Criterion, fake: Transcript | None = None) -> EvalCase:
    return EvalCase(
        case_id="demo-skill/one",
        skill="demo-skill",
        skills=("demo-skill",),
        prompt="do the thing",
        criterion=criterion,
        fake=fake or Transcript(),
    )


# ── loader ────────────────────────────────────────────────────────


def test_seed_corpus_loads_one_file_per_high_traffic_skill() -> None:
    cases = load_cases()
    assert {c.skill for c in cases} >= set(SEED_SKILLS)
    assert all(c.fake.calls for c in cases), "every seed has a fake transcript"
    assert len({c.case_id for c in cases}) == len(cases)
    assert default_cases_dir().is_dir()


def test_loader_filters_by_owning_skill() -> None:
    cases = load_cases(skills=["precis-paper-help"])
    assert cases and all(c.skill == "precis-paper-help" for c in cases)
    assert load_cases(skills=["precis-no-such-skill"]) == []


def test_loader_accepts_minimal_case(tmp_path: Path) -> None:
    _write(tmp_path, "demo-skill.yaml", _MINIMAL)
    (case,) = load_cases(tmp_path, known_skills={"demo-skill"})
    assert case.case_id == "demo-skill/one"
    assert case.skills == ("demo-skill",)
    assert case.criterion.calls == (CallPattern("get", "paper", {"id": "pa1"}),)
    assert case.fake.answer == "done"


@pytest.mark.parametrize(
    ("mutation", "fragment"),
    [
        (("skill: demo-skill", "skill: other"), "file stem"),
        (
            (
                "verb: get, kind: paper, args: {id: pa1}}]\n    fake",
                "verb: fetch}]\n    fake",
            ),
            "unknown verb",
        ),
        (
            (
                "    expect:\n      calls: [{verb: get, kind: paper, args: {id: pa1}}]\n",
                "    expect: {}\n",
            ),
            "at least one",
        ),
        (
            (
                "    fake:\n      calls: [{verb: get, kind: paper, args: {id: pa1}}]\n      answer: done\n",
                "",
            ),
            "'fake'",
        ),
        (("prompt: do the thing", "prompt: ''"), "prompt"),
        (("  - id: one", "  - id: One Two"), "slug"),
    ],
)
def test_loader_rejects_malformed_cases(
    tmp_path: Path, mutation: tuple[str, str], fragment: str
) -> None:
    old, new = mutation
    assert old in _MINIMAL
    _write(tmp_path, "demo-skill.yaml", _MINIMAL.replace(old, new))
    with pytest.raises(BadInput, match=fragment):
        load_cases(tmp_path, known_skills={"demo-skill"})


def test_loader_rejects_unknown_skill_and_bad_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path, "demo-skill.yaml", _MINIMAL)
    with pytest.raises(BadInput, match="unknown skill"):
        load_case_file(p, known_skills={"something-else"})
    _write(tmp_path, "demo-skill.yaml", "skill: [unclosed")
    with pytest.raises(BadInput, match="cannot read"):
        load_cases(tmp_path, known_skills={"demo-skill"})
    with pytest.raises(BadInput, match="does not exist"):
        load_cases(tmp_path / "nope", known_skills=set())


def test_seed_skills_must_exist_in_the_shipped_corpus(tmp_path: Path) -> None:
    _write(tmp_path, "demo-skill.yaml", _MINIMAL)
    with pytest.raises(BadInput, match="unknown skill"):
        load_cases(tmp_path)  # default vocabulary = shipped skills


# ── criterion checker ─────────────────────────────────────────────


def test_value_matches_subset_regex_list_and_coercion() -> None:
    assert value_matches({"id": "pa1"}, {"id": "pa1", "view": "toc"})
    assert not value_matches({"id": "pa1", "view": "toc"}, {"id": "pa1"})
    assert value_matches("re:(?i)pero.*", "Perovskite review")
    assert not value_matches("re:pero", "perovskite")  # fullmatch, not search
    assert value_matches(["a"], ["b", "a"]) and not value_matches(["c"], ["a"])
    assert value_matches("4821", 4821) and value_matches(True, True)
    assert value_matches({"meta": {"k": "re:v.*"}}, {"meta": {"k": "v1", "x": 2}})


def test_check_expected_call_subset_and_order_insensitive() -> None:
    crit = Criterion(
        calls=(
            CallPattern("get", "paper", {"id": "10.1038/x", "view": "abstract"}),
            CallPattern("search", "paper"),
        )
    )
    ok = Transcript(
        calls=(
            VerbCall("search", "paper", {"q": "x"}),
            VerbCall("get", "paper", {"id": "10.1038/x", "view": "abstract", "n": 1}),
        )
    )
    assert check(crit, ok) == check(crit, ok) and check(crit, ok).passed
    missing = Transcript(calls=(VerbCall("get", "paper", {"id": "10.1038/x"}),))
    v = check(crit, missing)
    assert not v.passed
    assert v.reasons == (
        "expected call not made: get(kind='paper', id='10.1038/x', view='abstract')",
        "expected call not made: search(kind='paper')",
    )


def test_check_forbidden_call_and_text_and_record() -> None:
    crit = Criterion(
        calls=(CallPattern("put", "draft"),),
        record={"kind": "draft", "id": "re:pero.*"},
        forbid_calls=(CallPattern("tag", "draft"),),
        forbid_text=("SELECT ",),
    )
    good = Transcript(
        calls=(VerbCall("put", "draft", {"id": "perovskite"}),),
        record={"kind": "draft", "id": "perovskite", "title": "t"},
        answer="put(kind='draft', ...)",
    )
    assert check(crit, good).passed
    bad = Transcript(
        calls=(VerbCall("put", "draft"), VerbCall("tag", "draft", {"add": ["r"]})),
        record={"kind": "draft", "id": "other"},
        answer="or run select * from refs",
    )
    v = check(crit, bad)
    assert v.reasons == (
        "forbidden call made: tag(kind='draft', add=['r'])",
        "record mismatch: expected {'kind': 'draft', 'id': 're:pero.*'}, "
        "got {'kind': 'draft', 'id': 'other'}",
        "forbidden text in answer: 'SELECT '",
    )
    none = Transcript(calls=(VerbCall("put", "draft"),))
    assert check(crit, none).reasons == ("expected a record, none produced",)


def test_check_kind_none_pattern_matches_any_kind() -> None:
    crit = Criterion(forbid_calls=(CallPattern("delete"),))
    assert not check(crit, Transcript(calls=(VerbCall("delete", "paper"),))).passed
    assert check(crit, Transcript(calls=(VerbCall("get", "paper"),))).passed


# ── report through the fake runner ────────────────────────────────


def test_seed_corpus_is_green_under_the_fake_runner_and_prompts_carry_skills() -> None:
    cases = load_cases()
    runner = FakeRunner()
    report = run_cases(cases, runner)
    assert report.status == "ok" and not report.red
    assert report.count("pass") == len(cases)
    assert [r.case_id for r in runner.recorded] == [c.case_id for c in cases]
    for case, rec in zip(cases, runner.recorded, strict=True):
        assert case.prompt in rec.prompt
        for slug in case.skills:
            assert f"<skill id='{slug}'>" in rec.prompt
            assert f"id: {slug}" in rec.prompt  # the skill's own front-matter


def test_misleading_transcript_turns_a_seed_red(tmp_path: Path) -> None:
    cases = load_cases(skills=["precis-paper-help"])
    (case,) = [c for c in cases if c.case_id == "precis-paper-help/doi-to-abstract"]
    detour = Transcript(
        calls=(VerbCall("search", "web", {"q": "nature10352 abstract"}),),
        answer="I'd search the web.",
    )
    report = run_cases([case], FakeRunner({case.case_id: detour}))
    assert report.red and report.cases[0].status == "fail"
    assert report.cases[0].reasons[0].startswith("expected call not made: get(")
    assert "forbidden call made: search(kind='web'" in report.cases[0].reasons[1]
    path = report.write_json(tmp_path / "r.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["summary"] == {"pass": 0, "fail": 1, "error": 0, "skip": 0}
    assert data["cases"][0]["calls"] == ["search(kind='web', q='nature10352 abstract')"]
    table = report.render_table()
    assert "precis-paper-help/doi-to-abstract  FAIL" in table
    assert table.splitlines()[-1].startswith("0 pass, 1 fail")


def test_run_cases_isolates_a_crashing_case_and_handles_empty() -> None:
    class Boom(FakeRunner):
        def run(self, case: EvalCase, prompt: str) -> Transcript:
            if case.case_id.endswith("one"):
                raise ValueError("kaboom")
            return super().run(case, prompt)

    crit = Criterion(calls=(CallPattern("get"),))
    a = _case(crit, Transcript(calls=(VerbCall("get", "paper"),)))
    b = EvalCase("demo-skill/two", "demo-skill", ("demo-skill",), "p", crit, a.fake)
    report = run_cases([a, b], Boom(), skill_text=lambda s: f"# {s}")
    assert [c.status for c in report.cases] == ["error", "pass"]
    assert report.cases[0].reasons == ("kaboom",)
    assert report.red
    empty = run_cases([], FakeRunner())
    assert empty.status == "skipped" and "no cases" in (empty.reason or "")
    assert empty.render_table().startswith("skill-eval [fake]: skipped")


def test_cli_fake_runner_writes_report_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    assert main(["--out", str(out), "--skill", "precis-overview"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["runner"] == "fake" and data["summary"]["pass"] >= 1
    assert all(c["skill"] == "precis-overview" for c in data["cases"])
    assert "report:" in capsys.readouterr().out
    _write(
        tmp_path, "demo-skill.yaml", _MINIMAL.replace("skill: demo-skill", "skill: x")
    )
    assert main(["--cases", str(tmp_path), "--out", str(out)]) == 2
    assert "skill-eval:" in capsys.readouterr().err


# ── live runner (stubbed claude -p) ───────────────────────────────


def _result(data: dict[str, Any], cost: float | None) -> ClaudePResult:
    text = json.dumps(data)
    return ClaudePResult(data=data, raw_stdout=text, cost_usd=cost, text=text)


def _which_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "precis.skill_eval.runners.shutil.which", lambda b: "/bin/claude"
    )


def test_live_runner_parses_reply_and_books_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which_ok(monkeypatch)
    seen: list[dict[str, Any]] = []

    def fake_call(prompt: str, **kw: Any) -> ClaudePResult:
        seen.append({"prompt": prompt, **kw})
        return _result(
            {
                "calls": [
                    {"verb": "get", "kind": "paper", "args": {"id": "pa1"}},
                    {"verb": "nope"},  # dropped, not fatal
                    {"verb": "put", "args": "not-a-dict"},
                ],
                "record": {"kind": "draft"},
                "answer": "ok",
            },
            0.0421,
        )

    runner = LiveClaudeRunner(call=fake_call, case_usd=0.2, run_usd=1.0, model="m")
    case = _case(Criterion(calls=(CallPattern("get", "paper"),)))
    t = runner.run(case, build_prompt(case, {"demo-skill": "# demo"}))
    assert t.calls == (
        VerbCall("get", "paper", {"id": "pa1"}),
        VerbCall("put", None, {}),
    )
    assert t.record == {"kind": "draft"} and t.answer == "ok" and t.cost_usd == 0.0421
    assert runner.spent_usd == pytest.approx(0.0421)
    assert seen[0]["max_usd"] == 0.2 and seen[0]["model"] == "m"
    assert "<skill id='demo-skill'>\n# demo" in seen[0]["prompt"]
    assert check(case.criterion, t).passed


def test_live_runner_run_budget_skips_remaining_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which_ok(monkeypatch)
    calls = 0

    def fake_call(prompt: str, **kw: Any) -> ClaudePResult:
        nonlocal calls
        calls += 1
        # No cost in the envelope → booked at the per-case cap (conservative).
        return _result({"calls": [{"verb": "get", "kind": "paper"}]}, None)

    runner = LiveClaudeRunner(call=fake_call, case_usd=0.4, run_usd=1.0)
    crit = Criterion(calls=(CallPattern("get"),))
    cases = [
        EvalCase(
            f"demo-skill/c{i}", "demo-skill", ("demo-skill",), "p", crit, Transcript()
        )
        for i in range(4)
    ]
    report = run_cases(cases, runner, skill_text=lambda s: "# s")
    assert calls == 2  # 0.4 + 0.4 fits; a third would exceed 1.0
    assert [c.status for c in report.cases] == ["pass", "pass", "skip", "skip"]
    assert "run budget $1.00 would be exceeded" in report.cases[2].reasons[0]
    assert report.status == "ok" and not report.red


@pytest.mark.parametrize(
    "exc",
    [
        ClaudePError("claude binary not found", binary_missing=True),
        ClaudePError(
            "claude -p returned no parseable JSON block",
            stdout="Not logged in · Please run /login",
        ),
        ClaudePError(
            "claude -p exited 1: Invalid authentication credentials", returncode=1
        ),
    ],
)
def test_live_runner_unauthenticated_skips_the_run(
    monkeypatch: pytest.MonkeyPatch, exc: ClaudePError
) -> None:
    _which_ok(monkeypatch)

    def fake_call(prompt: str, **kw: Any) -> ClaudePResult:
        raise exc

    runner = LiveClaudeRunner(call=fake_call, case_usd=0.1, run_usd=1.0)
    case = _case(Criterion(calls=(CallPattern("get"),)))
    report = run_cases([case], runner, skill_text=lambda s: "# s")
    assert report.status == "skipped" and report.cases == []
    assert not report.red
    assert "skipped" in report.render_table()


def test_live_runner_other_claude_errors_are_case_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which_ok(monkeypatch)

    def fake_call(prompt: str, **kw: Any) -> ClaudePResult:
        raise ClaudePError("claude -p timed out after 1.0s", timed_out=True)

    runner = LiveClaudeRunner(call=fake_call, case_usd=0.1, run_usd=1.0)
    case = _case(Criterion(calls=(CallPattern("get"),)))
    report = run_cases([case], runner, skill_text=lambda s: "# s")
    assert report.status == "ok" and report.cases[0].status == "error"
    assert "timed out" in report.cases[0].reasons[0]


def test_live_runner_missing_binary_is_a_preflight_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "/definitely/not/here/claude")
    monkeypatch.setattr("precis.skill_eval.runners.shutil.which", lambda b: None)

    def never(prompt: str, **kw: Any) -> ClaudePResult:
        raise AssertionError("must not be called")

    runner = LiveClaudeRunner(call=never)
    case = _case(Criterion(calls=(CallPattern("get"),)))
    report = run_cases([case], runner, skill_text=lambda s: "# s")
    assert report.status == "skipped" and "not found" in (report.reason or "")


def test_live_runner_env_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_SKILL_EVAL_CASE_USD", "0.05")
    monkeypatch.setenv("PRECIS_SKILL_EVAL_RUN_USD", "0.5")
    monkeypatch.setenv("PRECIS_SKILL_EVAL_TIMEOUT_S", "7")
    monkeypatch.setenv("PRECIS_SKILL_EVAL_MODEL", "claude-x")
    r = LiveClaudeRunner(call=lambda *a, **k: None)
    assert (r.case_usd, r.run_usd, r.timeout_s, r.model) == (0.05, 0.5, 7.0, "claude-x")
    assert LiveClaudeRunner(call=lambda *a, **k: None, run_usd=3).run_usd == 3


# ── advisory standing ─────────────────────────────────────────────


def test_skill_eval_is_not_a_ship_gate_stage() -> None:
    ship = (ROOT / "scripts" / "ship").read_text(encoding="utf-8")
    assert "skill-eval" not in ship and "skill_eval" not in ship
    wrapper = ROOT / "scripts" / "skill-eval"
    assert wrapper.exists() and "precis.skill_eval" in wrapper.read_text(
        encoding="utf-8"
    )
