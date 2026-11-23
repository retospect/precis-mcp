"""Keep job troubleshooting discoverable in the shipped skill corpus."""

from precis.store._todo_sql import _DOABLE_EXCLUSION_TAGS
from tests._skill_roots import skill_path


def test_ops_names_exclusion_tag_park() -> None:
    text = skill_path("precis-job-ops-help").read_text(encoding="utf-8")
    section = text.split("## Why is my hand-minted job queued forever?", 1)[1]
    section = section.split("## Re-submit a failed job", 1)[0]
    assert "hand-minted job is never claimed" in section
    assert "view='raw'" in section
    for tag in _DOABLE_EXCLUSION_TAGS:
        display = f"{tag}*" if tag.endswith(":") else tag
        assert f"`{display}`" in section
    assert "unexpired `claimed-by:*` lease" in section
    assert "`STATUS:paused` is not in this exclusion set" in section
    assert "mode='retry'" in section
    assert "remove=['child-failed:<job>']" in section


def test_observe_names_liveness_rules() -> None:
    text = skill_path("precis-job-observe-help").read_text(encoding="utf-8")
    section = text.split("## Read raw worker logs", 1)[1]
    section = section.split("## Read garden review counts", 1)[0]
    for phrase in (
        "completion line is not liveness",
        "unfiltered recent logs",
        "worker's age or restart",
        "jobs leased to that host",
        "known cadence",
        "condition alert",
        "check the deployed sha and enumerate every worker",
        "does not reclaim stale running jobs itself",
        "Do not restart workers mid-coordinator run",
        "re-mint afterwards once the orphan is terminal",
        # gr766930: the four gr663858 diagnoses the 2026-11 retire pass
        # dropped — busy-vs-wedged from the completion payload, the
        # overrun log line, the /leases view, and the stuck-hours story.
        "duration_ms / claimed",
        "llm_call_log",
        "ref-pass exceeded its <N>s deadline",
        "still running detached",
        "id='/leases",
        "PRECIS_STUCK_JOB_HOURS",
        "requeue-from-checkpoint is future work",
    ):
        assert phrase in section


def test_factory_names_shared_token() -> None:
    text = skill_path("precis-job-factory-help").read_text(encoding="utf-8")
    section = text.split("## Dark-factory job types — the lanes that run slices", 1)[1]
    section = section.split("## Dark-factory job types — second-repo", 1)[0]
    for phrase in (
        "mcp token: slice",
        "mcp token: shared",
        "every MCP call fails",
        "self-contained",
        "`brief:`",
        "before the job is minted or retried",
        "docs/runbooks/dark-factory-arming.md",
    ):
        assert phrase in section
