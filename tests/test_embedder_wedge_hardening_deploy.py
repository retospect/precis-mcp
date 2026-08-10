"""Deploy-role pins for ``docs/backlog/embedder-wedge-hardening.md``.

Two things live here that don't fit a unit test module:

* §1 offline-first load — the plist/service templates must set
  ``HF_HUB_OFFLINE=1`` / ``TRANSFORMERS_OFFLINE=1``, and the role's
  cache-warm task must be idempotent (gated on cache absence).
* §3 watchdog escalation — the actual POSIX-sh script, executed for
  real (with a stub ``curl`` on ``$PATH`` so no network/embedder is
  needed) across several simulated ticks, pinning that the restart-cycle
  counter survives across ticks and the 3rd+ cycle logs the escalation
  line while earlier cycles don't.

No jinja2 rendering — the templates' only Jinja token relevant to these
assertions is plain vars the surrounding tests don't need substituted
(mirrors this repo's existing plain-text-assertion convention for role
files, e.g. ``test_precis_worker_role_split.py``); the watchdog script in
particular has exactly one Jinja token (``{{ ansible_managed }}``, a
comment), so it's run directly after substituting that.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_EMBEDDER_ROLE = _REPO / "deploy" / "roles" / "precis_embedder"
_WATCHDOG_SCRIPT_TEMPLATE = (
    _REPO
    / "deploy"
    / "roles"
    / "precis_embedder_watchdog"
    / "templates"
    / "precis-embedder-watchdog.sh.j2"
)


# ---------------------------------------------------------------------------
# §1 — offline-first load
# ---------------------------------------------------------------------------


def test_plist_sets_offline_env() -> None:
    text = (_EMBEDDER_ROLE / "templates" / "precis-embedder.plist.j2").read_text(
        encoding="utf-8"
    )
    assert "<key>HF_HUB_OFFLINE</key>" in text
    assert "<key>TRANSFORMERS_OFFLINE</key>" in text
    # Both keys need a truthy string value right after them.
    assert "<string>1</string>" in text.split("HF_HUB_OFFLINE</key>", 1)[1][:60]


def test_systemd_unit_sets_offline_env() -> None:
    text = (_EMBEDDER_ROLE / "templates" / "precis-embedder.service.j2").read_text(
        encoding="utf-8"
    )
    assert "Environment=HF_HUB_OFFLINE=1" in text
    assert "Environment=TRANSFORMERS_OFFLINE=1" in text


def test_warm_task_is_gated_on_cache_absence() -> None:
    text = (_EMBEDDER_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "models--BAAI--bge-m3" in text
    assert "huggingface-cli download BAAI/bge-m3" in text
    # The download task must be `when:`-gated on the stat result, not
    # unconditional — that's what makes a warm host's redeploy a no-op
    # stat check instead of a re-download every run.
    assert "not precis_embedder_hf_cache_stat.stat.exists" in text


def test_warm_task_runs_before_daemon_env_is_set(monkeypatch=None) -> None:
    # The cache MUST be warmed before the offline-only daemon ever starts
    # — order the warm task ahead of the LaunchDaemon/systemd render+load
    # steps in the same file.
    text = (_EMBEDDER_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    warm_idx = text.index("Warm the bge-m3 HF cache")
    macos_load_idx = text.index("Load precis-embedder LaunchDaemon")
    linux_start_idx = text.index("Enable + start precis-embedder (Linux)")
    assert warm_idx < macos_load_idx
    assert warm_idx < linux_start_idx


def test_hf_revision_default_exists_and_is_pinnable() -> None:
    text = (_EMBEDDER_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")
    assert "precis_embedder_hf_revision" in text


# ---------------------------------------------------------------------------
# §3 — watchdog escalation (real script execution, no network)
# ---------------------------------------------------------------------------


def _render_watchdog_script(dest: Path) -> None:
    text = _WATCHDOG_SCRIPT_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("{{ ansible_managed }}", "test-rendered, do not edit")
    dest.write_text(text, encoding="utf-8")
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)


def _write_stub_curl(bin_dir: Path, *, fail: bool) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text("#!/bin/sh\nexit " + ("1\n" if fail else "0\n"), encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)


def _run_tick(
    script: Path,
    tmp_path: Path,
    *,
    fail: bool,
    statefile: Path,
    restart_marker: Path,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "stubbin"
    _write_stub_curl(bin_dir, fail=fail)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PRECIS_EMBEDDER_WATCHDOG_URL": "http://127.0.0.1:1/readyz",
        "PRECIS_EMBEDDER_WATCHDOG_FAIL_THRESHOLD": "1",
        "PRECIS_EMBEDDER_WATCHDOG_STATEFILE": str(statefile),
        "PRECIS_EMBEDDER_WATCHDOG_RESTART_CMD": f"touch {restart_marker}",
    }
    return subprocess.run(
        ["sh", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_watchdog_script_never_exits_nonzero(tmp_path: Path) -> None:
    script = tmp_path / "watchdog.sh"
    _render_watchdog_script(script)
    result = _run_tick(
        script,
        tmp_path,
        fail=True,
        statefile=tmp_path / "fails",
        restart_marker=tmp_path / "restarted",
    )
    assert result.returncode == 0, result.stderr


def test_watchdog_escalates_from_third_consecutive_restart_cycle(
    tmp_path: Path,
) -> None:
    script = tmp_path / "watchdog.sh"
    _render_watchdog_script(script)
    statefile = tmp_path / "fails"
    restart_marker = tmp_path / "restarted"

    outputs = []
    for _tick in range(4):
        result = _run_tick(
            script,
            tmp_path,
            fail=True,
            statefile=statefile,
            restart_marker=restart_marker,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
        # Threshold=1 means every failing tick restarts immediately.
        assert restart_marker.exists()
        restart_marker.unlink()

    # Cycles 1 and 2: no escalation line.
    assert "ESCALATION" not in outputs[0]
    assert "ESCALATION" not in outputs[1]
    # Cycle 3 onward: the distinctive escalation line, every time.
    assert "ESCALATION" in outputs[2]
    assert "restart cycle 3" in outputs[2]
    assert "ESCALATION" in outputs[3]
    assert "restart cycle 4" in outputs[3]


def test_watchdog_cycle_streak_resets_on_recovery(tmp_path: Path) -> None:
    script = tmp_path / "watchdog.sh"
    _render_watchdog_script(script)
    statefile = tmp_path / "fails"
    restart_marker = tmp_path / "restarted"
    cycle_statefile = tmp_path / "fails.cycles"

    for _tick in range(3):
        _run_tick(
            script,
            tmp_path,
            fail=True,
            statefile=statefile,
            restart_marker=restart_marker,
        )
        restart_marker.unlink()
    assert cycle_statefile.exists()

    # A healthy probe clears the whole streak.
    healthy = _run_tick(
        script, tmp_path, fail=False, statefile=statefile, restart_marker=restart_marker
    )
    assert healthy.returncode == 0
    assert not cycle_statefile.exists()
    assert not statefile.exists()

    # The next failure starts a fresh cycle 1 — no escalation.
    result = _run_tick(
        script, tmp_path, fail=True, statefile=statefile, restart_marker=restart_marker
    )
    assert "restart cycle 1" in result.stdout
    assert "ESCALATION" not in result.stdout


def test_watchdog_first_failure_timestamp_persists_across_cycles(
    tmp_path: Path,
) -> None:
    script = tmp_path / "watchdog.sh"
    _render_watchdog_script(script)
    statefile = tmp_path / "fails"
    restart_marker = tmp_path / "restarted"
    cycle_statefile = tmp_path / "fails.cycles"

    _run_tick(
        script, tmp_path, fail=True, statefile=statefile, restart_marker=restart_marker
    )
    restart_marker.unlink()
    first_line = cycle_statefile.read_text(encoding="utf-8").strip()
    first_ts = first_line.split(" ", 1)[1]

    _run_tick(
        script, tmp_path, fail=True, statefile=statefile, restart_marker=restart_marker
    )
    second_line = cycle_statefile.read_text(encoding="utf-8").strip()
    second_ts = second_line.split(" ", 1)[1]

    assert first_ts == second_ts
