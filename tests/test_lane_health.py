"""``lane_health`` job_type (gr588221) — the hourly probe pass.

The pass semantics: a red probe on a healthy lane parks it (never
``enabled``), a green probe on a parked lane unparks it and resolves the
deduped alert, everything else only moves the health stamp. Probes
return class keys, never raw output. The seams are the same ones the
slice tests use: ``use_lanes`` stands the account rows; the writers and
alert calls are captured, so no test here needs a lane process.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from precis.plans import lanes
from precis.workers.job_types import lane_health
from precis.workers.job_types.lane_health import (
    PARKED_ALERT_AFTER,
    run_lane_health_pass,
)
from tests._lane_rows import seed_lane, use_lanes

_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
_STORE: Any = object()


def _parked(lane: lanes.Lane, *, ago: timedelta = timedelta(hours=1)) -> lanes.Lane:
    """``lane`` with a live park stamped (gr588221)."""
    return replace(lane, parked_reason="auth:not-logged-in", parked_at=_NOW - ago)


def _capture(
    monkeypatch: pytest.MonkeyPatch, outcomes: dict[str, tuple[str, str | None]]
) -> dict[str, list]:
    """Patch the pass's seams: the probe reads ``outcomes`` by adapter,
    the writers/alert calls append. Returns the call log."""
    calls: dict[str, list] = {
        "probe": [],
        "park": [],
        "unpark": [],
        "health": [],
        "raise": [],
        "resolve": [],
    }

    def probe(adapter: str) -> tuple[str, str | None]:
        calls["probe"].append(adapter)
        return outcomes[adapter]

    def park(s: Any, name: str, reason: str, now: datetime) -> bool:
        calls["park"].append((name, reason))
        return True

    def unpark(s: Any, name: str, now: datetime) -> bool:
        calls["unpark"].append(name)
        return True

    def health(s: Any, name: str, outcome: str, now: datetime) -> bool:
        calls["health"].append((name, outcome))
        return True

    def raise_(s: Any, **kw: Any) -> tuple[int, bool]:
        calls["raise"].append(kw)
        return (1, True)

    def resolve(s: Any, **kw: Any) -> bool:
        calls["resolve"].append(kw)
        return True

    monkeypatch.setattr(lane_health, "_probe_account", probe)
    monkeypatch.setattr(lanes, "park_lane", park)
    monkeypatch.setattr(lanes, "unpark_lane", unpark)
    monkeypatch.setattr(lanes, "record_lane_health", health)
    monkeypatch.setattr(lane_health, "raise_alert", raise_)
    monkeypatch.setattr(lane_health, "resolve_alert_by_fingerprint", resolve)
    return calls


def test_a_red_probe_parks_a_healthy_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 03:13Z incident class: the probe reads the credential dead —
    the lane is parked out of dispatch with the class key, the healthy
    lane only gets its stamp."""
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("claude-sub"))
    calls = _capture(
        monkeypatch,
        {"devin_tree": ("ok", None), "claude_p": ("auth", "not-logged-in")},
    )
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert out == {"claude-sub": "auth", "devin": "ok"}
    assert calls["park"] == [("claude-sub", "auth:not-logged-in")]
    assert calls["health"] == [("devin", "ok")]
    assert calls["unpark"] == calls["resolve"] == calls["raise"] == []


def test_a_green_probe_unparks_a_parked_lane_and_posts_one_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The self-heal half: green on a parked lane clears the park, resolves
    the ``lane-parked:<name>`` alert and earns exactly one job_event
    line — no human toggle."""
    use_lanes(monkeypatch, _parked(seed_lane("claude-sub")))
    calls = _capture(monkeypatch, {"claude_p": ("ok", None)})
    notes: list[str] = []
    out = run_lane_health_pass(_STORE, now=_NOW, note=notes.append)
    assert out == {"claude-sub": "ok"}
    assert calls["unpark"] == ["claude-sub"]
    assert calls["resolve"] == [
        {
            "source": lane_health.ALERT_SOURCE,
            "fingerprint": "lane-parked:claude-sub",
            "resolved_by": "lane_health",
        }
    ]
    assert len(notes) == 1 and "unparked" in notes[0]
    assert calls["park"] == calls["health"] == calls["raise"] == []


def test_a_red_probe_on_a_parked_lane_only_refreshes_the_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Red on parked: ``record_lane_health`` moves the stamp, the first
    ``parked_at`` keeps the >6h alert clock — no re-park, no alert while
    the park is fresh."""
    use_lanes(monkeypatch, _parked(seed_lane("claude-sub"), ago=timedelta(hours=1)))
    calls = _capture(monkeypatch, {"claude_p": ("auth", "key-rejected")})
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert out == {"claude-sub": "auth"}
    assert calls["health"] == [("claude-sub", "auth")]
    assert calls["park"] == calls["unpark"] == calls["raise"] == []


def test_a_disabled_lane_is_not_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled`` is the human switch: a lane flipped off — the kimi
    lane's standing state — is skipped whole, never probed."""
    use_lanes(
        monkeypatch,
        seed_lane("devin"),
        seed_lane("kimi"),  # kimi: enabled=False
    )
    calls = _capture(
        monkeypatch,
        {"devin_tree": ("ok", None), "kimi_tree": ("ok", None)},
    )
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert calls["probe"] == ["devin_tree"]
    assert out == {"devin": "ok"}


def test_an_unprobed_lane_reports_unprobed_without_parking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that cannot judge must not park: the stamp moves to
    ``unprobed``, the lane stays in dispatch."""
    use_lanes(monkeypatch, seed_lane("claude-sub"))
    calls = _capture(monkeypatch, {"claude_p": ("unprobed", "unverifiable")})
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert out == {"claude-sub": "unprobed"}
    assert calls["health"] == [("claude-sub", "unprobed")]
    assert calls["park"] == []


def test_a_lane_parked_past_six_hours_files_one_deduped_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Still parked past :data:`PARKED_ALERT_AFTER` raises the
    ``lane-parked:<name>`` alert — one fingerprint per lane, so the
    dedup is the alert layer's."""
    use_lanes(
        monkeypatch,
        _parked(seed_lane("claude-sub"), ago=PARKED_ALERT_AFTER + timedelta(1)),
    )
    calls = _capture(monkeypatch, {"claude_p": ("outage", "timeout")})
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert out == {"claude-sub": "outage"}
    (raised,) = calls["raise"]
    assert raised["source"] == lane_health.ALERT_SOURCE
    assert raised["fingerprint"] == "lane-parked:claude-sub"
    assert "claude-sub" in raised["title"]
    assert "auth:not-logged-in" in raised["detail"]


def test_probe_account_reports_an_unprobed_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An adapter with no probe reads ``unprobed`` — and a probe that
    raises reads the same: neither may park a lane on a guess."""
    assert lane_health._probe_account("api") == ("unprobed", "no-probe")
    monkeypatch.setitem(
        lane_health._PROBES,
        "claude_p",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert lane_health._probe_account("claude_p") == ("unprobed", "probe-error")


def test_probe_claude_p_classifies_a_logged_out_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident's own log shape: a ``claude -p`` ping answering
    "Not logged in · Please run /login" classifies
    ``("auth", "not-logged-in")`` — the mark a park stamps."""
    monkeypatch.setattr(lane_health, "_claude_ping_env", lambda: {})
    monkeypatch.setattr(
        lane_health.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0],
            1,
            stdout='{"type":"result","is_error":true,'
            '"result":"Not logged in · Please run /login"}\n',
            stderr="",
        ),
    )
    assert lane_health._probe_claude_p() == ("auth", "not-logged-in")


def test_probe_claude_p_timeout_is_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review S3: a ping that hits its budget is ``unprobed`` — the
    probe could not judge the credential, so it must neither park nor
    unpark the lane."""
    monkeypatch.setattr(lane_health, "_claude_ping_env", lambda: {})

    def _hang(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=20)

    monkeypatch.setattr(lane_health.subprocess, "run", _hang)
    assert lane_health._probe_claude_p() == ("unprobed", "timeout")


def test_probe_claude_p_a_missing_binary_is_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review B1: the CLI absent on the probing host is a probe that
    cannot run — ``unprobed``, never a park."""
    monkeypatch.setattr(lane_health, "_claude_ping_env", lambda: {})

    def _missing(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(lane_health.subprocess, "run", _missing)
    assert lane_health._probe_claude_p() == ("unprobed", "cli-missing")


def test_probe_devin_tree_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """B1+S3: a missing binary or a timed-out ``auth status`` is
    ``unprobed``; only the definitive not-logged-in wording is ``auth``;
    anything else the probe cannot judge is ``unprobed`` too."""
    from precis.workers.job_types import run_slice

    monkeypatch.setattr(run_slice, "_devin_bin_ok", lambda b: False)
    assert lane_health._probe_devin_tree() == ("unprobed", "cli-missing")

    monkeypatch.setattr(run_slice, "_devin_bin_ok", lambda b: True)
    monkeypatch.setattr(
        run_slice,
        "_devin_auth_status",
        lambda b: subprocess.CompletedProcess([b], 124, stdout="", stderr="timed out"),
    )
    assert lane_health._probe_devin_tree() == ("unprobed", "timeout")
    monkeypatch.setattr(
        run_slice,
        "_devin_auth_status",
        lambda b: subprocess.CompletedProcess([b], 1, stdout="Not logged in\n"),
    )
    assert lane_health._probe_devin_tree() == ("auth", "not-logged-in")
    monkeypatch.setattr(
        run_slice,
        "_devin_auth_status",
        lambda b: subprocess.CompletedProcess([b], 1, stdout="", stderr="boom"),
    )
    assert lane_health._probe_devin_tree() == ("unprobed", "unverifiable")


def test_probe_codex_exec_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """B1+S3: a missing binary or a ``login status`` that times out or
    errors is ``unprobed`` — ``codex_login_present`` flattens those to
    False, so the probe runs the subprocess itself for the tri-state."""
    from precis.utils import codex_exec

    monkeypatch.setattr(codex_exec, "codex_bin_present", lambda: False)
    assert lane_health._probe_codex_exec() == ("unprobed", "cli-missing")

    monkeypatch.setattr(codex_exec, "codex_bin_present", lambda: True)

    def _hang(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="codex", timeout=15)

    monkeypatch.setattr(lane_health.subprocess, "run", _hang)
    assert lane_health._probe_codex_exec() == ("unprobed", "unverifiable")

    monkeypatch.setattr(
        lane_health.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="Logged in using ChatGPT\n"
        ),
    )
    assert lane_health._probe_codex_exec() == ("ok", None)
    monkeypatch.setattr(
        lane_health.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="Not logged in\n"),
    )
    assert lane_health._probe_codex_exec() == ("auth", "not-logged-in")
    monkeypatch.setattr(
        lane_health.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="x"),
    )
    assert lane_health._probe_codex_exec() == ("unprobed", "unverifiable")


def test_lane_health_job_type_registered() -> None:
    """The registry half: ``get_job_type`` resolves the spec — the
    ssh_node detached pair, not plugin dispatch."""
    from precis.workers.job_types import get_job_type, known_job_types

    spec = get_job_type("lane_health")
    assert spec is not None
    assert spec.submit is not None and spec.poll is not None
    assert spec.dispatch is None
    assert "ssh_node" in spec.compatible_executors
    assert "lane_health" in known_job_types()


def test_lane_health_cadence_registered() -> None:
    """Hourly, spends=False — CLI pings and fixed endpoints, no LLM.
    Host affinity stays None: the ServiceSpec placement pins the job to
    a slice host (``claude_bin`` IS the affinity)."""
    from precis.workers.registry import SERVICES
    from precis.workers.scheduler import CADENCES

    cad = next(c for c in CADENCES if c.name == "lane_health")
    assert cad.interval_s == 3600 + 13 * 60
    assert cad.host_affinity is None
    assert not cad.spends
    spec = next(s for s in SERVICES if s.name == "lane_health")
    assert spec.placement == frozenset({"repo_checkout", "claude_bin", "git"})


def test_mint_health_job_dedupes_while_one_is_pending(
    store: Any,
) -> None:
    """The scheduler's own dedup: a fresh pending ``lane_health`` job
    refuses a second mint."""
    first = lane_health.mint_health_job(
        store, origin="background", title="lane_health (test)"
    )
    assert first is not None
    assert lane_health.mint_health_job(store, origin="background", title="x") is None


# ── the ssh_node submit/poll pair (review B1) ──────────────────────


class _Ctx:
    """The ssh_node ctx seam — the same fake shape the run_slice suite's
    ``_Ctx`` carries."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.ref_id = 900
        self.meta: dict[str, Any] = {"params": {}}
        self.status: str | None = None
        self.failure: str | None = None
        self.chunks: list[tuple[str, str]] = []
        self.meta_set: dict[str, Any] = {}

    def set_status(self, value: str) -> None:
        self.status = value

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def record_failure(self, reason: str, **_kw: Any) -> None:
        self.failure = reason
        self.status = "failed"

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)


def test_submit_launches_the_probe_detached_on_this_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review B1: the probe runs WHERE the sessions run — a detached
    child of the claiming ssh_node worker (the ci_red_digest shape),
    argv the module's own ``probe`` entry over the enabled launchable
    adapters, the worker's own env (HOME/PATH/PRECIS_*_BIN)."""
    from precis import secrets
    from precis.utils import claude_oauth
    from precis.workers.job_types import run_slice

    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path))
    # Credential resolution is the worker's own vault path — off under
    # test (the child-env handoff has its own test).
    monkeypatch.setattr(claude_oauth, "ensure_oauth_token", lambda env: None)
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **kw: None)
    use_lanes(
        monkeypatch,
        seed_lane("devin"),
        seed_lane("claude-sub"),
        seed_lane("kimi"),  # disabled — not probed
    )
    launches: list[tuple[list[str], dict[str, Any]]] = []

    def fake_launch(argv: list[str], **kw: Any) -> int:
        launches.append((argv, kw))
        return 4242

    monkeypatch.setattr(run_slice, "_launch", fake_launch)
    ctx = _Ctx(_STORE)
    handle = lane_health.submit(ctx, None)
    assert handle is not None and handle["pid"] == 4242
    [(argv, kw)] = launches
    assert argv[1:4] == [
        "-m",
        "precis.workers.job_types.lane_health",
        "probe",
    ]
    assert sorted(argv[4:]) == ["claude_p", "devin_tree"]
    assert kw["stdin"] is None
    assert Path(handle["log"]).parent.name == "lane-health"


def test_submit_with_no_enabled_lane_marks_no_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every lane off between mint and claim: no child, the poll closes
    the job succeeded with nothing applied."""
    use_lanes(monkeypatch, seed_lane("kimi"))  # the only lane is disabled
    ctx = _Ctx(_STORE)
    handle = lane_health.submit(ctx, None)
    assert handle == {"no_probe": True}
    assert lane_health.poll(ctx, handle) is True
    assert ctx.status == "succeeded"


def test_probe_entrypoint_prints_one_classified_line_per_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The detached child's contract: ``probe: adapter=<a> outcome=<o>
    key=<k>`` per adapter — the class key, never raw output."""
    monkeypatch.setattr(
        lane_health,
        "_probe_account",
        lambda a: {
            "claude_p": ("auth", "not-logged-in"),
            "devin_tree": ("ok", None),
        }[a],
    )
    assert lane_health._main(["probe", "claude_p", "devin_tree"]) == 0
    out = capsys.readouterr().out
    assert "probe: adapter=claude_p outcome=auth key=not-logged-in" in out
    assert "probe: adapter=devin_tree outcome=ok key=-" in out


def test_poll_applies_the_probe_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """exit 0 + ``probe:`` lines: the pass applies the parsed outcomes —
    a missing adapter reads ``unprobed/no-result``, never a park."""
    use_lanes(monkeypatch, _parked(seed_lane("claude-sub")), seed_lane("devin"))
    calls = _capture(monkeypatch, {})
    log_path = tmp_path / "j.log"
    exit_file = tmp_path / "j.exit"
    log_path.write_text("probe: adapter=claude_p outcome=ok key=-\n", encoding="utf-8")
    exit_file.write_text("0\n", encoding="utf-8")
    ctx = _Ctx(_STORE)
    handle = {
        "pid": 4242,
        "log": str(log_path),
        "exit_file": str(exit_file),
        "adapters": ["claude_p", "devin_tree"],
    }
    assert lane_health.poll(ctx, handle) is True
    assert ctx.status == "succeeded"
    # claude-sub's phrase park clears on the one green.
    assert calls["unpark"] == ["claude-sub"]
    # devin printed no line — stamped unprobed, never parked/unparked.
    assert ("devin", "unprobed") in calls["health"]
    assert calls["park"] == []


def test_poll_failed_probe_process_fails_the_job(tmp_path: Path) -> None:
    """A non-zero probe exit is the probe PROCESS's failure — the job
    fails and nothing is applied to the lanes."""
    log_path = tmp_path / "j.log"
    exit_file = tmp_path / "j.exit"
    log_path.write_text("boom\n", encoding="utf-8")
    exit_file.write_text("1\n", encoding="utf-8")
    ctx = _Ctx(_STORE)
    handle = {"pid": 4242, "log": str(log_path), "exit_file": str(exit_file)}
    assert lane_health.poll(ctx, handle) is True
    assert ctx.status == "failed"
    assert ctx.failure is not None and "lane_health" in ctx.failure


def test_poll_a_running_probe_returns_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No exit file yet + a live pid → still running."""
    from precis.workers.job_types import run_slice

    monkeypatch.setattr(run_slice, "_process_alive", lambda pid: True)
    ctx = _Ctx(_STORE)
    handle = {
        "pid": 4242,
        "log": str(tmp_path / "j.log"),
        "exit_file": str(tmp_path / "j.exit"),
    }
    assert lane_health.poll(ctx, handle) is False


# ── S2: a probe unparks only the class it can verify ────────────────


def test_a_phrase_or_outage_park_needs_one_green(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-stamped ``auth:<phrase>`` or a probe-stamped
    ``outage:*`` park is the class the probe itself answers — one green
    clears it."""
    for reason in ("auth:not-logged-in", "outage:timeout"):
        use_lanes(
            monkeypatch,
            replace(
                seed_lane("claude-sub"),
                parked_reason=reason,
                parked_at=_NOW - timedelta(hours=1),
            ),
        )
        calls = _capture(monkeypatch, {"claude_p": ("ok", None)})
        run_lane_health_pass(_STORE, now=_NOW)
        assert calls["unpark"] == ["claude-sub"], reason
        monkeypatch.undo()


def test_a_structural_park_needs_two_greens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review S2: ``auth:cli-missing``/``auth:http-*`` parks are the
    process/API's report, not wording the ping re-asks — the FIRST
    green only stamps ``ok``; the second consecutive green unparks."""
    for reason in ("auth:cli-missing", "auth:http-401"):
        use_lanes(
            monkeypatch,
            replace(
                seed_lane("claude-sub"),
                parked_reason=reason,
                parked_at=_NOW - timedelta(hours=1),
            ),
        )
        calls = _capture(monkeypatch, {"claude_p": ("ok", None)})
        out = run_lane_health_pass(_STORE, now=_NOW)
        assert out == {"claude-sub": "ok"}
        assert calls["unpark"] == []
        assert calls["health"] == [("claude-sub", "ok")]
        monkeypatch.undo()


def test_a_second_green_unparks_a_structural_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The parked account row already stamped ``ok`` by the previous
    pass: this green is the second — the park clears."""
    from precis.utils.llm import accounts

    use_lanes(
        monkeypatch,
        replace(
            seed_lane("claude-sub"),
            parked_reason="auth:cli-missing",
            parked_at=_NOW - timedelta(hours=1),
        ),
    )
    row = accounts.list_accounts(_STORE)[0]
    monkeypatch.setattr(
        accounts,
        "list_accounts",
        lambda store: [replace(row, health_outcome="ok")],
    )
    calls = _capture(monkeypatch, {"claude_p": ("ok", None)})
    run_lane_health_pass(_STORE, now=_NOW)
    assert calls["unpark"] == ["claude-sub"]


def test_an_unprobed_lane_stays_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``unprobed`` moves the stamp only — the park stands either way."""
    use_lanes(monkeypatch, _parked(seed_lane("claude-sub")))
    calls = _capture(monkeypatch, {"claude_p": ("unprobed", "cli-missing")})
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert out == {"claude-sub": "unprobed"}
    assert calls["health"] == [("claude-sub", "unprobed")]
    assert calls["unpark"] == calls["park"] == []


# ── review round 2: credentials ride the child env; only auth parks ──


def test_submit_hands_resolved_credentials_to_the_child_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review R2-1: the detached child binds no store (the worker's
    ``adopt_process_store`` popped the DSN), so ``submit`` resolves both
    probe credentials on the WORKER — read-only — and hands them down
    through ``env`` only. Presence is asserted by key NAME; the values
    never reach the handle, a chunk or meta."""
    from precis import secrets
    from precis.utils import claude_oauth
    from precis.utils.llm import kimi
    from precis.workers.job_types import run_slice

    monkeypatch.setenv("PRECIS_SLICE_OPS_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv(kimi.API_KEY_REF, raising=False)

    def fake_ensure(env: dict[str, str]) -> None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-test-value"

    monkeypatch.setattr(claude_oauth, "ensure_oauth_token", fake_ensure)
    monkeypatch.setattr(
        secrets,
        "get_secret",
        lambda name, **kw: "kimi-test-value" if name == kimi.API_KEY_REF else None,
    )
    use_lanes(monkeypatch, seed_lane("devin"), seed_lane("claude-sub"))
    launches: list[tuple[list[str], dict[str, Any]]] = []

    def fake_launch(argv: list[str], **kw: Any) -> int:
        launches.append((argv, kw))
        return 4242

    monkeypatch.setattr(run_slice, "_launch", fake_launch)
    ctx = _Ctx(_STORE)
    handle = lane_health.submit(ctx, None)
    assert handle is not None
    [(_argv, kw)] = launches
    env = kw["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env
    assert kimi.API_KEY_REF in env
    surfaces = repr(handle) + repr(ctx.chunks) + repr(ctx.meta_set)
    assert "oauth-test-value" not in surfaces
    assert "kimi-test-value" not in surfaces


def test_probe_kimi_tree_a_missing_key_is_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review R2-1: a key the worker could not hand down is the host's
    secret state, not a positive auth failure the lane's binary
    reported — ``unprobed`` parks nothing and unparks nothing."""
    from precis import secrets

    monkeypatch.setattr(secrets, "get_secret", lambda *a, **kw: None)
    assert lane_health._probe_kimi_tree() == ("unprobed", "key-missing")
    use_lanes(monkeypatch, replace(seed_lane("kimi"), enabled=True))
    calls = _capture(monkeypatch, {"kimi_tree": ("unprobed", "key-missing")})
    out = run_lane_health_pass(_STORE, now=_NOW)
    assert out == {"kimi": "unprobed"}
    assert calls["health"] == [("kimi", "unprobed")]
    assert calls["park"] == calls["unpark"] == []
    use_lanes(
        monkeypatch,
        replace(
            seed_lane("kimi"),
            enabled=True,
            parked_reason="auth:key-rejected",
            parked_at=_NOW - timedelta(hours=1),
        ),
    )
    calls = _capture(monkeypatch, {"kimi_tree": ("unprobed", "key-missing")})
    run_lane_health_pass(_STORE, now=_NOW)
    assert calls["park"] == calls["unpark"] == []


def test_probe_claude_p_only_a_positive_auth_classification_parks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review R2-2: the claude ping parks the lane ONLY on a positive
    ``auth`` classification — a crash with no JSON, an ``is_error``
    answer the classifier cannot name, and a probe-seen quota/rate
    window are all ``unprobed``, never ``outage:error``."""
    monkeypatch.setattr(lane_health, "_claude_ping_env", lambda: {})

    def _proc(stdout: str, rc: int = 1) -> Any:
        return lambda *a, **k: subprocess.CompletedProcess(
            a[0], rc, stdout=stdout, stderr=""
        )

    # rc 1 with empty stdout (a crash, a network blip, a 5xx).
    monkeypatch.setattr(lane_health.subprocess, "run", _proc(""))
    assert lane_health._probe_claude_p() == ("unprobed", "error")
    # is_error with a result the auth allowlist does not name.
    monkeypatch.setattr(
        lane_health.subprocess,
        "run",
        _proc('{"type":"result","is_error":true,"result":"tool crashed"}\n'),
    )
    assert lane_health._probe_claude_p() == ("unprobed", "error")
    # A quota window the probe saw: unprobed — the window's own wording
    # is not a positive auth failure from the lane's binary.
    monkeypatch.setattr(
        lane_health.subprocess,
        "run",
        _proc(
            '{"type":"result","is_error":true,'
            '"result":"You\'ve hit your weekly limit · resets 11am (UTC)",'
            '"api_error_status":429}\n'
        ),
    )
    assert lane_health._probe_claude_p() == ("unprobed", "quota")


def test_probes_are_read_only_no_login_refresh_or_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review R2-5: every lane probe is a read-only status check — the
    argv carries no login/refresh/update verb, the claude ping's env
    drops the shared MCP bearer and disables the CLI's nonessential
    (self-update) traffic, and kimi launches NO binary at all: the key
    check is one HTTPS GET."""
    from precis import secret_status, secrets
    from precis.utils import codex_exec
    from precis.workers.job_types import run_slice

    spawned: list[list[str]] = []
    envs: list[dict[str, str]] = []

    def spy(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        spawned.append(list(argv))
        envs.append(dict(kw.get("env") or {}))
        return subprocess.CompletedProcess(argv, 0, stdout="Logged in\n", stderr="")

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("PRECIS_MCP_TOKEN", "shared-mcp-token")

    # claude: the ping argv is a plain -p read; its env keeps the OAuth
    # token, drops the MCP bearer and arms the nonessential-traffic
    # switch so the probe can never trigger a CLI self-update.
    assert lane_health._probe_claude_p() == ("unprobed", "error")
    cl_argv, cl_env = spawned[-1], envs[-1]
    assert cl_argv[1:] == [
        "-p",
        "ping",
        "--output-format",
        "json",
        "--max-turns",
        "1",
    ]
    assert "PRECIS_MCP_TOKEN" not in cl_env
    assert "CLAUDE_CODE_OAUTH_TOKEN" in cl_env
    assert cl_env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1"
    assert not {"login", "refresh", "update", "upgrade"} & set(cl_argv)

    # devin / codex: the read-only status calls only.
    monkeypatch.setattr(run_slice, "devin_bin", lambda: "/vendor/devin")
    monkeypatch.setattr(run_slice, "_devin_bin_ok", lambda b: True)
    assert lane_health._probe_devin_tree() == ("ok", None)
    assert spawned[-1] == ["/vendor/devin", "auth", "status"]
    monkeypatch.setattr(codex_exec, "codex_bin_present", lambda: True)
    monkeypatch.setattr(codex_exec, "resolve_codex_binary", lambda: "/vendor/codex")
    assert lane_health._probe_codex_exec() == ("unprobed", "unverifiable")
    assert spawned[-1] == ["/vendor/codex", "login", "status"]

    # kimi: no binary launch — the membership key check is one HTTPS
    # GET through the secrets probe (the CLI has no read-only mode a
    # self-update guard could pin).
    async def fake_probe(client: Any, values: dict[str, str]) -> Any:
        return secret_status.CheckResult("ok", "verified")

    monkeypatch.setattr(secret_status, "_probe_kimi", fake_probe)
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **kw: "k")
    calls_before = len(spawned)
    assert lane_health._probe_kimi_tree() == ("ok", None)
    assert len(spawned) == calls_before
