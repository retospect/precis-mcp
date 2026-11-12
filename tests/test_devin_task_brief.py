"""gr635349: cloud Devin's branch handoff awaits a fetch/landing consumer."""

from precis.workers.job_types import devin_task


def test_cloud_brief_hands_off_branch_without_a_pr() -> None:
    prompt = devin_task.render_prompt(
        "Fix the parser; supplied tests: tests/test_parser.py", job_id=42
    )
    assert "Fix the parser; supplied tests: tests/test_parser.py" in prompt
    assert "devin/job-42" in prompt
    assert "Never open a PR" in prompt
    assert "pull request" not in prompt.lower()
    assert "push" in prompt.lower()
    assert "fetch" in prompt.lower()
    assert "workspace" in prompt.lower()
    assert "Do not commit" in prompt
    assert "local" in prompt.lower()


def test_cloud_brief_requires_supplied_tests_before_build() -> None:
    prompt = devin_task.render_prompt("Fix the parser", job_id=42)
    text = prompt.lower()
    assert "tests" in text and "before" in text and "build" in text
    assert "supplied" in text
    assert "stop" in text and "missing" in text
    assert "verify" in text


def test_cloud_brief_requires_first_line_no_change_verdict() -> None:
    prompt = devin_task.render_prompt("Check the parser", job_id=42)
    assert "first line" in prompt.lower()
    assert "moot: gr<N> already fixed by <sha>" in prompt
    assert "validated: <reason>" in prompt
    assert "no-op: <reason>" in prompt
    assert "bare moot" in prompt.lower()
    assert "refused for code builds" in prompt.lower()


def test_cloud_branch_handoff_is_pending_a_consumer() -> None:
    prompt = devin_task.render_prompt("Fix the parser", job_id=42)
    assert "pending" in prompt.lower()
    assert "no fetch/landing consumer" in prompt.lower()
    assert "Our side will fetch" not in prompt


def test_cloud_finishing_contract_overrides_old_task_instructions() -> None:
    task = "Fix the parser. Open one pull request against main."
    prompt = devin_task.render_prompt(task, job_id=77)
    assert task in prompt
    assert "override" in prompt.lower()
    assert prompt.rindex("Never open a PR") > prompt.index(task)
    assert "devin/job-77" in prompt
