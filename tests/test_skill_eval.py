"""Per-skill eval harness (``precis.skill_eval``): loader, criterion checker,
report through the fake runner, the live runner's budget/auth behaviour with
a stubbed ``call_claude_p``, and the advisory standing (never a ship gate).
Hermetic: no DB, no model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
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
    classify_failure,
    classify_pairs,
    default_cases_dir,
    load_cases,
    load_discovery_cases,
    run_cases,
)
from precis.skill_eval.__main__ import main
from precis.skill_eval.cases import load_case_file, value_matches
from precis.skill_eval.report import CaseResult, Report
from precis.skill_eval.runners import (
    DEFAULT_DEV_DSN,
    DRY_RUN_TOKEN,
    ExecutedClaudeRunner,
    build_exec_prompt,
    build_prompt,
    fetch_record,
    serve_mcp_config,
    stream_tool_calls,
)
from precis.utils._claude_subprocess import ClaudeProcessError
from precis.utils.claude_p import ClaudePError, ClaudePResult

ROOT = Path(__file__).resolve().parent.parent
SEED_SKILLS = (
    "precis-overview",
    "precis-paper-help",
    "precis-draft-help",
    "precis-memory-help",
    "precis-finding-help",
    "precis-todo-help",
    "precis-search-help",
)

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
        # ``{run}`` in a task renders as the dry-run token in the prompt.
        assert case.prompt.replace("{run}", DRY_RUN_TOKEN) in rec.prompt
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


@pytest.mark.parametrize(
    ("skill", "case_id", "detour", "reason_bits"),
    [
        # A misleading memory skill teaches plain text recall: no
        # view='index' on the search, no fisheye get on the hit.
        (
            "precis-memory-help",
            "precis-memory-help/recall-before-task",
            Transcript(
                calls=(VerbCall("search", "memory", {"q": "skill-eval corpus"}),),
                answer="I searched memory by text.",
            ),
            (
                "expected call not made: search(kind='memory'",
                "expected call not made: get(kind='memory'",
            ),
        ),
        # A misleading finding skill accepts a bare doi: as cited_in — the
        # trap the skill names verbatim ("unknown kind 'doi'").
        (
            "precis-finding-help",
            "precis-finding-help/register-finding",
            Transcript(
                calls=(
                    VerbCall(
                        "put",
                        "finding",
                        {
                            "title": "gate-bias 2.4 kV",
                            "body": "2.4 kV for 30 s",
                            "cited_in": "doi:10.1234/xyz",
                        },
                    ),
                ),
                record={"kind": "finding"},
                answer="Registered it against the DOI.",
            ),
            (
                "expected call not made: put(kind='finding'",
                "forbidden call made: put(kind='finding'",
            ),
        ),
        # A misleading todo skill treats priority as title text.
        (
            "precis-todo-help",
            "precis-todo-help/bump-priority",
            Transcript(
                calls=(
                    VerbCall(
                        "edit",
                        "todo",
                        {
                            "id": 141,
                            "mode": "replace",
                            "text": "URGENT: finish the gate driver review",
                        },
                    ),
                ),
                answer="Rewrote the title to flag urgency.",
            ),
            (
                "expected call not made: tag(kind='todo'",
                "forbidden call made: edit(kind='todo'",
            ),
        ),
        # A misleading search skill reaches for a continuation page
        # (page=) the bounded top-N model removed.
        (
            "precis-search-help",
            "precis-search-help/page-through-hits",
            Transcript(
                calls=(
                    VerbCall(
                        "search",
                        "paper",
                        {"q": "photocatalysis", "page": 3},
                    ),
                ),
                answer="Fetched page 3.",
            ),
            (
                "expected call not made: search(kind='paper'",
                "forbidden call made: search(kind='paper'",
            ),
        ),
    ],
)
def test_misleading_transcript_turns_each_new_family_red(
    skill: str, case_id: str, detour: Transcript, reason_bits: tuple[str, ...]
) -> None:
    cases = load_cases(skills=[skill])
    (case,) = [c for c in cases if c.case_id == case_id]
    report = run_cases([case], FakeRunner({case_id: detour}))
    assert report.red and report.cases[0].status == "fail"
    for bit in reason_bits:
        assert any(r.startswith(bit) for r in report.cases[0].reasons), bit


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


# ── executed runner (stubbed claude -p + fake landed-record fetch) ───

_STREAM = "\n".join(
    json.dumps(ev)
    for ev in [
        {
            "type": "system",
            "subtype": "init",
            "mcp_servers": [{"name": "precis", "status": "connected"}],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "mcp__precis__put",
                        "input": {
                            "kind": "quest",
                            "text": "a zinc-air battery that survives 10k cycles",
                        },
                    },
                    {
                        "type": "tool_use",
                        "id": "tu2",
                        "name": "mcp__precis__get",
                        "input": {"kind": "quest", "id": 7, "view": "tree"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tu3",
                        "name": "Read",
                        "input": {"file_path": "/etc/passwd"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu1",
                        "content": [
                            {
                                "type": "text",
                                "text": "created quest qu7 (STATUS:active).",
                            }
                        ],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu2",
                        "content": [{"type": "text", "text": "<tree rollup>"}],
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu3",
                        "content": [{"type": "text", "text": "root:x:0:0"}],
                    },
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "Minted qu7 and read its tree.",
            "total_cost_usd": 0.31,
        },
    ]
)


def _agent_result(stdout: str = _STREAM, cost: float | None = 0.31) -> Any:
    return SimpleNamespace(cost_usd=cost, raw_stdout=stdout, final_text="")


def _exec_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch: Any = None,
    **runner_kw: Any,
) -> ExecutedClaudeRunner:
    _which_ok(monkeypatch)
    kw: dict[str, Any] = {
        "dsn": "postgresql://localhost/precis_test_eval",
        "call": lambda *a, **k: _agent_result(),
    }
    kw.update(runner_kw)
    if fetch is not None:
        kw["fetch_record"] = fetch
    return ExecutedClaudeRunner(**kw)


def test_stream_tool_calls_reads_precis_verbs_and_joins_results() -> None:
    pairs, results = stream_tool_calls(_STREAM)
    assert [c.render() for _, c in pairs] == [
        "put(kind='quest', text='a zinc-air battery that survives 10k cycles')",
        "get(kind='quest', id=7, view='tree')",
    ]
    # A non-precis tool (Read) is out of the transcript; results pair by id.
    assert results["tu1"][0] == "created quest qu7 (STATUS:active)."
    assert results["tu3"][0] == "root:x:0:0"


def test_stream_tool_calls_parses_command_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "precis.tools.command_parser.parse_command",
        lambda command, *, text=None: ("put", {"kind": "quest", "text": "x"}),
    )
    stream = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "mcp__precis__precis",
                        "input": {"command": "put(kind='quest', text='x')"},
                    }
                ]
            },
        }
    )
    pairs, _ = stream_tool_calls(stream)
    assert pairs == [("tu1", VerbCall("put", "quest", {"text": "x"}))]


def test_executed_runner_reconstructs_and_spends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_call(prompt: str, **kw: Any) -> Any:
        seen.update(kw)
        seen["mcp_json"] = json.loads(
            Path(kw["mcp_config"]).read_text(encoding="utf-8")
        )
        return _agent_result()

    runner = _exec_runner(monkeypatch, call=fake_call)
    case = _case(
        Criterion(
            calls=(
                CallPattern("put", "quest"),
                CallPattern("get", "quest", {"view": "tree"}),
            )
        )
    )
    t = runner.run(case, "do it")
    created_ack = "created quest qu7 (STATUS:active)."
    assert t.calls == (
        VerbCall(
            "put",
            "quest",
            {"text": "a zinc-air battery that survives 10k cycles"},
            result_bytes=len(created_ack.encode("utf-8")),
        ),
        VerbCall(
            "get",
            "quest",
            {"id": 7, "view": "tree"},
            result_bytes=len(b"<tree rollup>"),
        ),
    )
    assert t.answer == "Minted qu7 and read its tree." and t.cost_usd == 0.31
    assert runner.spent_usd == pytest.approx(0.31)
    assert seen["output_format"] == "stream-json"
    assert seen["require_mcp"] == ("precis",)
    assert seen["max_usd"] == runner.case_usd and seen["max_turns"] == runner.max_turns
    env = seen["mcp_json"]["mcpServers"]["precis"]["env"]
    assert env["PRECIS_DATABASE_URL"] == runner.dsn
    assert env["PRECIS_MCP_PROFILE"] == "typed"
    assert env["PRECIS_DEFAULT_TAGS"].startswith("eval-run-")


def test_executed_runner_prompt_tells_the_model_to_call_tools() -> None:
    case = _case(Criterion(calls=(CallPattern("get"),)))
    prompt = build_exec_prompt(case, {"demo-skill": "# demo"})
    assert "<skill id='demo-skill'>\n# demo" in prompt
    assert case.prompt in prompt and "declar" not in prompt.lower()
    assert "calling the tools" in prompt


def test_executed_runner_record_is_the_landed_row_not_the_declaration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The draft spec's pinned divergence: a ``put`` the transcript shows
    succeeding but whose row never landed fails executed where the same
    declared record passes live — and a landed row with the wrong shape
    fails too."""
    crit = Criterion(calls=(CallPattern("put", "quest"),), record={"kind": "quest"})
    case = _case(crit)

    landed = {
        "kind": "quest",
        "id": "qu7",
        "ref_id": 7,
        "title": "q",
        "body": "",
        "tags": [],
        "meta": {},
    }
    good = _exec_runner(monkeypatch, fetch=lambda *a: landed)
    t = good.run(case, "do it")
    assert t.record == landed and check(crit, t).passed

    unlanded = _exec_runner(monkeypatch, fetch=lambda *a: None)
    v = check(crit, unlanded.run(case, "do it"))
    assert not v.passed and "expected a record" in v.reasons[0]

    wrong_shape = _exec_runner(monkeypatch, fetch=lambda *a: {**landed, "kind": "todo"})
    v = check(crit, wrong_shape.run(case, "do it"))
    assert not v.passed and "record mismatch" in v.reasons[0]


def test_executed_runner_failed_put_yields_no_record_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _STREAM.replace(
        "created quest qu7 (STATUS:active).",
        "[error:BadInput] put(kind='quest'): text is required",
    )
    seen_fetch: list[tuple] = []

    def fetch(dsn: str, kind: Any, ident: str) -> Any:
        seen_fetch.append((kind, ident))
        return {"kind": "quest", "id": "qu7", "ref_id": 7}

    runner = _exec_runner(
        monkeypatch,
        call=lambda *a, **k: _agent_result(stream),
        fetch=fetch,
    )
    case = _case(Criterion(record={"kind": "quest"}))
    t = runner.run(case, "do it")
    assert seen_fetch == [] and t.record is None
    assert t.calls[0].error is not None  # the failed put is scored, not dropped


def _exec_stream(
    calls: list[tuple[str, str, dict[str, Any]]],
    results: dict[str, dict[str, Any]],
) -> str:
    """One assistant ``tool_use`` batch + a user ``tool_result`` batch +
    the trailing result event, as stream-json lines."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": tid, "name": name, "input": inp}
                    for tid, name, inp in calls
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": tid, **block}
                    for tid, block in results.items()
                ]
            },
        },
        {"type": "result", "result": "done", "total_cost_usd": 0.01},
    ]
    return "\n".join(json.dumps(e) for e in events)


@pytest.mark.parametrize(
    "block",
    [
        {"is_error": True, "content": [{"type": "text", "text": "tool exploded"}]},
        {"content": [{"type": "text", "text": "[error:NotFound] no such ref"}]},
    ],
    ids=["is_error", "error-envelope"],
)
def test_executed_runner_errored_call_cannot_satisfy_expect(
    monkeypatch: pytest.MonkeyPatch, block: dict[str, Any]
) -> None:
    """A call whose tool_result failed never did the work: it can't fill
    an ``expect.calls`` slot and the error is named in the reasons — the
    call-only case is a fail, not a pass."""
    stream = _exec_stream(
        [("tu1", "mcp__precis__get", {"kind": "draft", "id": "eval-x-0-0"})],
        {"tu1": block},
    )
    runner = _exec_runner(monkeypatch, call=lambda *a, **k: _agent_result(stream))
    case = _case(
        Criterion(calls=(CallPattern("get", "draft", {"id": "re:eval-x-.*"}),))
    )
    report = run_cases([case], runner, skill_text=lambda s: "# s")
    assert report.cases[0].status == "fail"
    reasons = report.cases[0].reasons
    assert any(r.startswith("expected call not made: get(") for r in reasons)
    assert any(r.startswith("call errored: get(") for r in reasons)


def test_exec_prompt_expands_run_token_and_dry_prompt_uses_the_dry_token() -> None:
    """``{run}`` namespaces the records a case mints: the executed prompt
    carries the runner's per-run id, the dry-run prompt the fixed token —
    both shaped ``<digits>-<digits>`` so one ``re:`` criterion fits."""
    case = EvalCase(
        "demo-skill/one",
        "demo-skill",
        ("demo-skill",),
        "create a draft 'eval-x-{run}' and read it back",
        Criterion(calls=(CallPattern("put"),)),
        Transcript(),
    )
    texts = {"demo-skill": "# demo"}
    exec_prompt = build_exec_prompt(case, texts, run_id="4821-1730000000")
    assert "eval-x-4821-1730000000" in exec_prompt and "{run}" not in exec_prompt
    assert "eval-x-0-0" in build_prompt(case, texts)
    runner = ExecutedClaudeRunner(
        dsn="postgresql://localhost/precis_test_eval",
        call=lambda *a, **k: _agent_result(),
        run_id="9-9",
    )
    assert "eval-x-9-9" in runner.build_prompt(case, texts)


def test_fetch_record_reads_landed_row_against_the_real_schema(store) -> None:
    """Real-DB pin for the executed landed-record check: seeds a ref and
    reads it back through the migrated schema — ``retired_at`` (the
    ``deleted_at`` rename that silently sank every fetch), the cite_key
    as ``id`` (corpus criteria address slugs), handle, body, meta."""
    from tests import conftest
    from tests.workers._helpers import seed_chunk

    ref = store.insert_ref(
        kind="paper", slug="eval-fetch-1", title="Fetch target", meta={"m": 1}
    )
    seed_chunk(store, ref_id=ref.id, text="body paragraph")
    dsn = conftest._active_dsn()
    rec = fetch_record(dsn, "paper", "eval-fetch-1")
    assert rec is not None
    assert rec["kind"] == "paper"
    assert rec["id"] == "eval-fetch-1"  # the slug, not the pa<N> handle
    assert rec["handle"].startswith("pa") and rec["ref_id"] == ref.id
    assert rec["title"] == "Fetch target" and "body paragraph" in rec["body"]
    assert rec["meta"] == {"m": 1}
    by_handle = fetch_record(dsn, "paper", rec["handle"])
    by_id = fetch_record(dsn, "paper", f"id={ref.id}")
    assert by_handle is not None and by_id is not None
    assert by_handle["ref_id"] == ref.id == by_id["ref_id"]
    store.retire_ref(ref.id)
    assert fetch_record(dsn, "paper", "eval-fetch-1") is None


def test_executed_run_fetches_the_record_the_stream_landed(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end minus the model: a canned stream-json run whose put
    ack names the seeded row is judged on the real fetch (the default
    ``fetch_record``, no stub) — the record criterion sees the landed
    cite_key, and a second case whose write never landed goes red."""
    from tests import conftest

    ref = store.insert_ref(kind="paper", slug="eval-landed-1", title="Landed")
    seed = "\n".join(
        json.dumps(ev)
        for ev in [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "mcp__precis__put",
                            "input": {"kind": "paper", "id": "eval-landed-1"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu1",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "created paper eval-landed-1.",
                                }
                            ],
                        }
                    ]
                },
            },
            {"type": "result", "result": "ok", "total_cost_usd": 0.01},
        ]
    )
    runner = _exec_runner(
        monkeypatch,
        dsn=conftest._active_dsn(),
        call=lambda *a, **k: _agent_result(seed, cost=0.01),
    )
    crit = Criterion(
        calls=(CallPattern("put", "paper"),),
        record={"kind": "paper", "id": "eval-landed-1"},
    )
    t = runner.run(_case(crit), "do it")
    assert t.record is not None and t.record["id"] == "eval-landed-1"
    assert check(crit, t).passed


def test_executed_runner_uses_edit_id_when_no_created_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu1",
                                "name": "mcp__precis__edit",
                                "input": {
                                    "kind": "draft",
                                    "id": "eval-brief",
                                    "title": "Nitrate brief",
                                },
                            }
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu1",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "edited draft eval-brief: title.",
                                    }
                                ],
                            }
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.01}),
        ]
    )
    seen: list[tuple] = []

    def fetch(*a: Any) -> dict[str, Any]:
        seen.append(a[1:])
        return {"kind": "draft", "id": "eval-brief"}

    runner = _exec_runner(
        monkeypatch,
        call=lambda *a, **k: _agent_result(stream),
        fetch=fetch,
    )
    case = _case(Criterion(record={"kind": "draft"}))
    t = runner.run(case, "do it")
    assert seen == [("draft", "eval-brief")]
    assert t.record == {"kind": "draft", "id": "eval-brief"}


def test_executed_runner_spawn_path_only_produces_the_eval_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing in the spawn path may resolve prod: the config's only DSN
    is the one handed in, defaulting to scripts/dev's precis_dev."""
    assert DEFAULT_DEV_DSN == "postgresql://postgres@precis-test-db:5432/precis_dev"
    cfg = serve_mcp_config(dsn=DEFAULT_DEV_DSN, run_id="t1")
    entry = cfg["mcpServers"]["precis"]
    assert entry["command"] == "precis" and entry["args"] == ["serve"]
    assert entry["env"]["PRECIS_DATABASE_URL"] == DEFAULT_DEV_DSN
    monkeypatch.setenv("PRECIS_DATABASE_URL", "postgresql://prod.example/precis")
    monkeypatch.delenv("PRECIS_SKILL_EVAL_DSN", raising=False)
    runner = ExecutedClaudeRunner(call=lambda **k: _agent_result())
    assert runner.dsn == DEFAULT_DEV_DSN  # ambient DSN can never leak in
    monkeypatch.setenv("PRECIS_SKILL_EVAL_DSN", "postgresql://x/precis_test")
    assert (
        ExecutedClaudeRunner(call=lambda **k: _agent_result()).dsn
        == "postgresql://x/precis_test"
    )


def test_executed_runner_mcp_not_ready_and_auth_skip_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _which_ok(monkeypatch)
    for exc in (
        ClaudeProcessError("precis never connected", mcp_not_ready=True),
        ClaudeProcessError(
            "claude -p exited 1: Invalid authentication credentials", returncode=1
        ),
        ClaudeProcessError("claude binary not found", binary_missing=True),
    ):

        def boom(*a: Any, exc: Exception = exc, **k: Any) -> Any:
            raise exc

        runner = _exec_runner(monkeypatch, call=boom)
        case = _case(Criterion(calls=(CallPattern("get"),)))
        report = run_cases([case], runner, skill_text=lambda s: "# s")
        assert report.status == "skipped" and report.cases == []
        assert not report.red


def test_executed_runner_missing_binary_is_a_preflight_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_CLAUDE_BIN", "/definitely/not/here/claude")
    monkeypatch.setattr("precis.skill_eval.runners.shutil.which", lambda b: None)

    def never(*a: Any, **k: Any) -> Any:
        raise AssertionError("must not be called")

    runner = ExecutedClaudeRunner(call=never)
    case = _case(Criterion(calls=(CallPattern("get"),)))
    report = run_cases([case], runner, skill_text=lambda s: "# s")
    assert report.status == "skipped" and "not found" in (report.reason or "")


# ── subtasks depth + accuracy-by-depth ───────────────────────────────


def test_loader_reads_subtasks_and_rejects_bad(tmp_path: Path) -> None:
    text = _MINIMAL.replace("    fake:", "    subtasks: 4\n    fake:")
    _write(tmp_path, "demo-skill.yaml", text)
    (case,) = load_cases(tmp_path, known_skills={"demo-skill"})
    assert case.subtasks == 4
    bad = _MINIMAL.replace("    fake:", "    subtasks: 0\n    fake:")
    _write(tmp_path, "demo-skill.yaml", bad)
    with pytest.raises(BadInput, match="subtasks"):
        load_cases(tmp_path, known_skills={"demo-skill"})


def test_accuracy_by_depth_groups_pass_fail_per_model() -> None:
    def case(cid: str, subtasks: int) -> EvalCase:
        return EvalCase(
            cid,
            "demo-skill",
            ("demo-skill",),
            "p",
            Criterion(calls=(CallPattern("get"),)),
            Transcript(),
            subtasks=subtasks,
        )

    class DepthFake(FakeRunner):
        name = "executed"

        def run(self, c: EvalCase, prompt: str) -> Transcript:
            ok = c.subtasks <= 2
            return Transcript(
                calls=(VerbCall("get", "paper"),) if ok else (),
                cost_usd=0.01,
            )

    runner = DepthFake()
    runner.model = "claude-x"
    report = run_cases(
        [case(f"demo-skill/c{i}", d) for i, d in enumerate([1, 2, 2, 3, 4])],
        runner,
        skill_text=lambda s: "# s",
    )
    depth = report.accuracy_by_depth()
    assert depth == {
        "claude-x": {
            1: {"pass": 1, "total": 1},
            2: {"pass": 2, "total": 2},
            3: {"pass": 0, "total": 1},
            4: {"pass": 0, "total": 1},
        }
    }
    table = report.render_table()
    assert "accuracy by subtask count:" in table
    assert "4 subtasks  0/1  (0%)" in table
    assert report.to_dict()["accuracy_by_depth"] == depth


def test_executed_cli_merges_models_into_one_depth_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--execute --model A --model B: one report, per-model depth rows —
    the gr608716 acceptance (accuracy by subtask count for two models)."""
    _write(
        tmp_path,
        "precis-overview.yaml",
        """\
skill: precis-overview
cases:
  - id: one
    prompt: do the thing
    subtasks: 1
    expect:
      calls: [{verb: get, kind: paper}]
    fake:
      calls: [{verb: get, kind: paper, args: {id: pa1}}]
      answer: done
""",
    )

    class StubRunner:
        name = "executed"

        def __init__(self, *, model: Any = None, **kw: Any) -> None:
            self.model = model

        def run(self, case: EvalCase, prompt: str) -> Transcript:
            return Transcript(calls=case.fake.calls or (VerbCall("get", "paper"),))

    monkeypatch.setattr("precis.skill_eval.__main__.ExecutedClaudeRunner", StubRunner)
    out = tmp_path / "r.json"
    rc = main(
        [
            "--execute",
            "--cases",
            str(tmp_path),
            "--out",
            str(out),
            "--model",
            "model-a",
            "--model",
            "model-b",
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["accuracy_by_depth"] == {
        "model-a": {"1": {"pass": 1, "total": 1}},
        "model-b": {"1": {"pass": 1, "total": 1}},
    }
    assert {c["model"] for c in data["cases"]} == {"model-a", "model-b"}
    assert "accuracy by subtask count:" in capsys.readouterr().out
    assert LiveClaudeRunner(call=lambda *a, **k: None, run_usd=3).run_usd == 3


# ── discovery condition + paired runs (gr634773) ──────────────────


def test_discovery_prompt_has_no_skill_text_or_slug() -> None:
    case = _case(Criterion(calls=(CallPattern("get"),)))
    texts = {"demo-skill": "# demo skill body"}
    supplied = build_exec_prompt(case, texts)
    discovery = build_exec_prompt(case, texts, condition="discovery")
    assert "<skill id='demo-skill'>" in supplied
    assert "# demo skill body" in supplied
    # The discovery prompt renders neither the slug nor the skill text…
    assert "demo-skill" not in discovery
    assert "# demo" not in discovery
    assert "skill" not in discovery.lower()
    # …but the task itself is still there.
    assert case.prompt in discovery
    assert "actually call the tools" in discovery
    runner = ExecutedClaudeRunner(
        dsn="postgresql://localhost/precis_test_eval",
        call=lambda *a, **k: _agent_result(),
        condition="discovery",
    )
    assert runner.build_prompt(case, texts) == build_exec_prompt(
        case, texts, run_id=runner.run_id, condition="discovery"
    )
    # The dry-run builder honours the condition too.
    dry = build_prompt(case, texts, condition="discovery")
    assert "demo-skill" not in dry and "# demo" not in dry
    assert case.prompt.replace("{run}", DRY_RUN_TOKEN) in dry
    with pytest.raises(ValueError, match="unknown condition"):
        build_exec_prompt(case, texts, condition="bogus")


def test_paired_runs_both_conditions_with_same_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--condition paired: one runner per condition, identical caps, both
    conditions' rows in one report; --condition paired on a dry runner is
    refused."""
    _write(
        tmp_path,
        "precis-overview.yaml",
        """\
skill: precis-overview
cases:
  - id: one
    prompt: do the thing
    expect:
      calls: [{verb: get, kind: paper}]
    fake:
      calls: [{verb: get, kind: paper, args: {id: pa1}}]
      answer: done
""",
    )
    seen: list[dict[str, Any]] = []

    class StubRunner:
        name = "executed"

        def __init__(
            self, *, model: Any = None, condition: str = "supplied", **kw: Any
        ):
            self.model = model
            self.condition = condition
            seen.append({"model": model, "condition": condition, **kw})

        def run(self, case: EvalCase, prompt: str) -> Transcript:
            assert ("<skill id=" in prompt) == (self.condition == "supplied")
            return Transcript(calls=case.fake.calls or (VerbCall("get", "paper"),))

    monkeypatch.setattr("precis.skill_eval.__main__.ExecutedClaudeRunner", StubRunner)
    out = tmp_path / "r.json"
    rc = main(
        [
            "--execute",
            "--condition",
            "paired",
            "--corpus",
            "supplied",
            "--cases",
            str(tmp_path),
            "--out",
            str(out),
            "--case-usd",
            "0.5",
            "--run-usd",
            "2.0",
        ]
    )
    assert rc == 0
    # Both conditions ran under the SAME budget, supplied first.
    assert [(s["condition"], s["case_usd"], s["run_usd"]) for s in seen] == [
        ("supplied", 0.5, 2.0),
        ("discovery", 0.5, 2.0),
    ]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["condition"] == "paired"
    rows = data["cases"]
    assert {r["condition"] for r in rows} == {"supplied", "discovery"}
    # The fake's first call matches, so discovery metrics are zero — and
    # both conditions pass, so no failure class is stamped.
    discovery_rows = [r for r in rows if r["condition"] == "discovery"]
    assert all(r["discovery_calls"] == 0 for r in discovery_rows)
    assert all(r["failure"] is None for r in rows)
    assert "paired failures:" not in capsys.readouterr().out
    # paired needs the executed runner.
    assert main(["--runner", "live", "--condition", "paired"]) == 2
    assert main(["--runner", "fake", "--condition", "paired"]) == 2


def test_discovery_metrics_count_calls_and_bytes_before_first_correct_call() -> None:
    crit = Criterion(calls=(CallPattern("put", "quest"),))
    case = _case(crit)
    transcript = Transcript(
        calls=(
            VerbCall("search", "quest", {"q": "x"}, result_bytes=100),
            VerbCall("get", "quest", {"id": 7}, result_bytes=250),
            VerbCall("put", "quest", {"text": "y"}, result_bytes=80),
        )
    )
    runner = FakeRunner({case.case_id: transcript}, condition="discovery")
    report = run_cases([case], runner, skill_text=lambda s: "# s")
    row = report.cases[0]
    assert report.condition == "discovery" and row.condition == "discovery"
    assert row.discovery_calls == 2 and row.discovery_bytes == 350
    # First call already correct → no exploration spent.
    first_ok = FakeRunner(
        {case.case_id: Transcript(calls=(VerbCall("put", "quest"),))},
        condition="discovery",
    )
    row = run_cases([case], first_ok, skill_text=lambda s: "# s").cases[0]
    assert row.discovery_calls == 0 and row.discovery_bytes == 0
    # Supplied rows carry no discovery metrics.
    supplied = FakeRunner({case.case_id: Transcript(calls=(VerbCall("put", "quest"),))})
    row = run_cases([case], supplied, skill_text=lambda s: "# s").cases[0]
    assert row.condition == "supplied"
    assert row.discovery_calls is None and row.discovery_bytes is None


def test_retrieval_vs_guidance_vs_unattributed() -> None:
    def row(
        status: str,
        reasons: tuple[str, ...] = (),
        condition: str = "supplied",
    ) -> CaseResult:
        return CaseResult(
            "demo/one", "demo-skill", status, reasons, condition=condition
        )

    ok = row("pass")
    fail_clean = row("fail", ("expected call not made: get(kind='paper')",))
    fail_errored = row("fail", ("call errored: get(kind='paper') — boom",))
    crashed = row("error", ("claude -p timed out after 600.0s",))
    skipped = row("skip", ("run budget $2.00 would be exceeded",))
    # Given the skill it PASSES → the discovery failure is retrieval.
    assert classify_failure(ok, fail_clean) == "retrieval"
    # Fails even supplied, clean calls on both sides → guidance.
    assert classify_failure(fail_clean, fail_clean) == "guidance"
    # The calls themselves errored (or the runner crashed) → tool surface.
    assert classify_failure(fail_clean, fail_errored) == "tool"
    assert classify_failure(crashed, fail_clean) == "tool"
    # Contradictions, clean pairs and skips attribute nothing.
    assert classify_failure(fail_clean, ok) == "unattributed"
    assert classify_failure(ok, ok) == "unattributed"
    assert classify_failure(skipped, fail_clean) == "unattributed"


def test_classify_pairs_stamps_both_rows_and_the_table_lists_them() -> None:
    supplied = CaseResult("demo/one", "demo-skill", "pass", condition="supplied")
    discovery = CaseResult(
        "demo/one",
        "demo-skill",
        "fail",
        ("expected call not made: get(kind='paper')",),
        condition="discovery",
        discovery_calls=3,
        discovery_bytes=4210,
    )
    rows = [supplied, discovery]
    out = classify_pairs(rows)
    assert out == {(None, "demo/one"): "retrieval"}
    assert rows[0].failure == "retrieval" and rows[1].failure == "retrieval"
    report = Report(runner="executed", condition="paired", cases=rows)
    table = report.render_table()
    assert "paired failures:" in table
    assert "demo/one  supplied=PASS discovery=FAIL  retrieval" in table
    assert "discovery 3 calls/4210B to first correct call" in table
    data = report.to_dict()
    assert data["condition"] == "paired"
    assert data["cases"][1]["failure"] == "retrieval"


# ── discovery corpus (gr634773) ───────────────────────────────────


def test_discovery_tasks_name_no_skill() -> None:
    cases = load_discovery_cases()
    # Ten families, each mapped to its own help skill.
    assert len({c.case_id.split("/")[0] for c in cases}) == 10
    assert len({c.skill for c in cases}) == 10
    for case in cases:
        prompt = case.prompt
        assert case.skill not in prompt  # no slug…
        assert not re.search(r"(?i)\bskills?\b", prompt)  # …no "skill" mention…
        assert not re.search(r"precis-[a-z0-9-]+", prompt)  # …no slug-shaped token
        assert "-help" not in prompt


def test_each_task_has_two_variants_and_a_fake_that_passes() -> None:
    cases = load_discovery_cases()
    tasks: dict[str, list[EvalCase]] = {}
    for case in cases:
        task_id = re.sub(r"-v\d+$", "", case.case_id)
        tasks.setdefault(task_id, []).append(case)
    assert len(tasks) == 10
    for task_id, group in tasks.items():
        assert len(group) == 2, task_id
        assert len({c.prompt for c in group}) == 2, task_id  # distinct wordings
        assert all(c.criterion == group[0].criterion for c in group)
        assert all(c.fake == group[0].fake for c in group)
        # The corpus self-check: the fake satisfies its own criterion, so
        # the task is provably completable (alternative valid paths would
        # also pass — the patterns stay broad on purpose).
        verdict = check(group[0].criterion, group[0].fake)
        assert verdict.passed, f"{task_id}: {verdict.reasons}"


def test_discovery_corpus_is_green_under_the_fake_runner() -> None:
    cases = load_discovery_cases()
    runner = FakeRunner()
    report = run_cases(cases, runner)
    assert report.status == "ok" and not report.red
    assert report.count("pass") == len(cases)
    for case, rec in zip(cases, runner.recorded, strict=True):
        assert case.prompt.replace("{run}", DRY_RUN_TOKEN) in rec.prompt
        for slug in case.skills:
            assert f"<skill id='{slug}'>" in rec.prompt


# ── advisory standing ─────────────────────────────────────────────


def test_skill_eval_is_not_a_ship_gate_stage() -> None:
    ship = (ROOT / "scripts" / "ship").read_text(encoding="utf-8")
    assert "skill-eval" not in ship and "skill_eval" not in ship
    wrapper = ROOT / "scripts" / "skill-eval"
    assert wrapper.exists() and "precis.skill_eval" in wrapper.read_text(
        encoding="utf-8"
    )


def test_cli_loads_discovery_corpus(tmp_path: Path) -> None:
    from precis.skill_eval.__main__ import main

    for explicit in (False, True):
        out = tmp_path / f"discovery-{explicit}.json"
        args = ["--condition", "discovery", "--out", str(out)]
        if explicit:
            args.extend(["--cases", "src/precis/data/skill-evals/discovery"])
        assert main(args) == 0
        rows = json.loads(out.read_text(encoding="utf-8"))["cases"]
        assert {r["case_id"] for r in rows} == {
            c.case_id for c in load_discovery_cases()
        }
        assert len(rows) == 20
