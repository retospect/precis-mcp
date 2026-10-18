"""The weekly credential report behind ``precis secret check`` (gr496305).

No network: ``run_checks`` is replaced with a canned result, so only the
classification, the job lookup (real PG ``store``) and the rendering run.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from precis.cli import secret as secret_cli
from precis.store import Tag
from precis_web import secret_status
from precis_web.secret_status import CheckResult, SecretSpec, classify

_SPEC = SecretSpec("X_KEY", "p", "https://example.test/keys", "b")
_OK = CheckResult("ok", "verified")


@pytest.mark.parametrize(
    ("present", "result", "state"),
    [
        (False, None, "missing"),
        (False, _OK, "missing"),
        (True, None, "unprobed"),
        (True, _OK, "ok"),
        (True, CheckResult("bad", "HTTP 401 — key rejected"), "failed"),
        (True, CheckResult("unknown", "timeout — could not verify"), "unprobed"),
        (True, CheckResult("unknown", "HTTP 429 — rate limited"), "unprobed"),
    ],
)
def test_classify_states(present: bool, result: CheckResult | None, state: str) -> None:
    assert classify(_SPEC, present, result).state == state


def test_failed_keeps_the_http_status_and_inconclusive_is_not_failed() -> None:
    failed = classify(_SPEC, True, CheckResult("bad", "HTTP 401 — key rejected"))
    assert "401" in failed.detail
    inconclusive = classify(_SPEC, True, CheckResult("unknown", "timeout"))
    assert inconclusive.state == "unprobed"
    assert "inconclusive" in inconclusive.detail


def test_every_registry_secret_gets_a_row_and_failed_sorts_first(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    async def fake_run_checks(_store: Any) -> dict[str, CheckResult]:
        return {"ANTHROPIC_API_KEY": CheckResult("bad", "HTTP 401 — key rejected")}

    monkeypatch.setattr(secret_status, "run_checks", fake_run_checks)
    monkeypatch.setattr(
        secret_status, "is_available", lambda n, store=None: n == "ANTHROPIC_API_KEY"
    )
    rows = asyncio.run(secret_status.weekly_report(store))
    assert [r.name for r in rows] == [s.name for s in secret_status.KNOWN_SECRETS]
    text = secret_status.format_report(rows)
    body = text.splitlines()[1:]
    assert body[0].startswith("failed")
    assert "https://console.anthropic.com/settings/keys" in body[0]
    n_missing = len(secret_status.KNOWN_SECRETS) - 1
    assert text.splitlines()[0].startswith(f"secrets: 1 failed {n_missing} missing")


def test_report_never_contains_a_secret_value(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    sentinel = "sk-SENTINEL-must-not-print"
    monkeypatch.setenv("PRECIS_MP_API_KEY", sentinel)
    monkeypatch.setenv("PRECIS_FARNELL_API_KEY", sentinel)

    async def fake_run_checks(_store: Any) -> dict[str, CheckResult]:
        return {}

    monkeypatch.setattr(secret_status, "run_checks", fake_run_checks)
    rows = asyncio.run(secret_status.weekly_report(store))
    states = {r.name: r.state for r in rows}
    assert states["PRECIS_MP_API_KEY"] == "unprobed"
    assert sentinel not in secret_status.format_report(rows)
    assert sentinel not in repr(rows)


def _job(store: Any, job_type: str, status: str) -> int:
    ref = store.insert_ref(
        kind="job", slug=None, title=f"{job_type} {status}", meta={"job_type": job_type}
    )
    store.add_tag(
        ref.id,
        Tag.parse_strict(f"STATUS:{status}"),
        set_by="agent",
        replace_prefix=True,
    )
    return int(ref.id)


def test_last_succeeded_job_picks_newest_succeeded_of_the_type(store: Any) -> None:
    older = _job(store, "pourbaix_bulk", "succeeded")
    _job(store, "pourbaix_bulk", "failed")
    _job(store, "other_type", "succeeded")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '2 days' WHERE ref_id = %s",
            (older,),
        )
        conn.commit()
    newer = _job(store, "pourbaix_bulk", "succeeded")

    found = secret_status.last_succeeded_job(store, ("pourbaix_bulk",))
    assert found is not None
    assert found[0] == newer

    retired = _job(store, "pourbaix_bulk", "succeeded")
    store.retire_ref(retired)
    again = secret_status.last_succeeded_job(store, ("pourbaix_bulk",))
    assert again is not None
    assert again[0] == newer


def test_last_succeeded_job_none_without_a_match(store: Any) -> None:
    assert secret_status.last_succeeded_job(store, ("no_such_job_type",)) is None
    assert secret_status.last_succeeded_job(store, ()) is None


def test_unprobed_row_links_its_last_job_in_the_report(
    monkeypatch: pytest.MonkeyPatch, store: Any
) -> None:
    job_id = _job(store, "pourbaix_bulk", "succeeded")
    monkeypatch.setenv("PRECIS_MP_API_KEY", "x")

    async def fake_run_checks(_store: Any) -> dict[str, CheckResult]:
        return {}

    monkeypatch.setattr(secret_status, "run_checks", fake_run_checks)
    text = secret_status.format_report(asyncio.run(secret_status.weekly_report(store)))
    (line,) = [ln for ln in text.splitlines() if "PRECIS_MP_API_KEY" in ln]
    assert f"get(kind='job', id={job_id})" in line


def test_unprobed_without_jobs_says_so() -> None:
    plain = classify(_SPEC, True, None)
    assert "no job records its use" in secret_status.format_report([plain])
    tracked = classify(
        SecretSpec("T", "p", None, "b", used_by_job_types=("a_job",)), True, None
    )
    assert "no succeeded a_job job on record" in secret_status.format_report([tracked])


def test_optional_missing_does_not_ask_for_a_key() -> None:
    spec = SecretSpec("OPT", "p", "https://example.test", "b", optional=True)
    line = secret_status.format_report([classify(spec, False, None)]).splitlines()[1]
    assert "optional" in line
    assert "https://example.test" not in line


def test_check_subcommand_is_registered() -> None:
    import argparse

    root = argparse.ArgumentParser()
    secret_cli.add_parser(root.add_subparsers())
    args = root.parse_args(["secret", "check"])
    assert args.secret_cmd == "check"
