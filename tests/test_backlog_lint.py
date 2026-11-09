"""scripts/backlog-lint: done-marked sub-parts of open specs must not be loud (gr458459)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: execs the bash script scripts/backlog-lint",
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backlog-lint"


def _lint_result(
    d: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(d)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        env=env,
    )


def _lint(d: Path, env: dict[str, str] | None = None) -> str:
    cp = _lint_result(d, env)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout


def _fm(status: str) -> str:
    return f"---\nstatus: {status}\npillar: platform\n---\n"


def test_checked_subitem_in_open_status_spec_is_quiet(tmp_path: Path) -> None:
    for st in ("idea", "draft", "ready", "in-progress"):
        (tmp_path / f"{st}.md").write_text(
            _fm(st) + "# Spec\n\n- [x] a sub-part shipped\n", encoding="utf-8"
        )
    out = _lint(tmp_path)
    assert "marked done but still in" not in out
    assert "4 done-marked sub-part(s)" in out


def test_marker_without_open_signal_is_loud(tmp_path: Path) -> None:
    (tmp_path / "thread.md").write_text(
        "# thread\n\n- **x** — SHIPPED 2026-09-30 as foo\n", encoding="utf-8"
    )
    out = _lint(tmp_path)
    assert "1 item(s) marked done" in out
    assert "thread.md:3" in out


def test_h1_done_marker_is_loud_even_in_open_status(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(_fm("draft") + "# Spec — DONE\n", encoding="utf-8")
    assert "1 item(s) marked done" in _lint(tmp_path)


def _crowded(d: Path, prefix: str, n: int) -> None:
    for i in range(n):
        (d / f"{prefix}-{i}.md").write_text(
            _fm("idea") + f"# {prefix} {i}\n", encoding="utf-8"
        )


def test_parked_is_a_valid_status(tmp_path: Path) -> None:
    """`parked` (a deliberately shelved item) is in the closed status set —
    Reto parked hexfold-multicell-prism 2026-10-24 and the lint went red on
    a legitimate state."""
    (tmp_path / "x.md").write_text(_fm("parked") + "# Spec\n", encoding="utf-8")
    assert "status must be one of" not in _lint(tmp_path)


def test_crowded_prefix_within_baseline_is_quiet(tmp_path: Path) -> None:
    _crowded(tmp_path, "foo", 10)
    (tmp_path / "lint-baseline.txt").write_text("foo 10\n", encoding="utf-8")
    assert "crowded prefixes" not in _lint(tmp_path)


def test_crowded_prefix_over_baseline_warns(tmp_path: Path) -> None:
    _crowded(tmp_path, "foo", 10)
    (tmp_path / "lint-baseline.txt").write_text(
        "# reviewed families\nfoo 9\n", encoding="utf-8"
    )
    out = _lint(tmp_path)
    assert "crowded prefixes" in out
    assert "foo-*  10 files" in out


def test_crowded_prefix_without_baseline_still_warns(tmp_path: Path) -> None:
    _crowded(tmp_path, "foo", 9)
    assert "foo-*  9 files" in _lint(tmp_path)


def test_relative_arg_resolves_against_caller_cwd(tmp_path: Path) -> None:
    """A relative arg names a dir under the caller's cwd, not the script's
    own root — the script can be invoked through another checkout's
    scripts/ on PATH (gr514451)."""
    d = tmp_path / "mybacklog"
    d.mkdir()
    (d / "x.md").write_text("# t\n\n- **x** — SHIPPED 2026-01-01\n", encoding="utf-8")
    out = subprocess.run(
        ["bash", str(SCRIPT), "mybacklog"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert "mybacklog/x.md:3" in out


# --- prior-art (gr610793) ----------------------------------------------------
# Existence resolution is prod-backed and best-effort: PRECIS_BACKLOG_RESOLVER
# swaps the resolver command so tests inject a stub — the same contract
# scripts/backlog-handles honours (argv = handles, one
# "<handle>\t<verdict>[\t<detail>]" line each, verdict resolved|missing;
# nonzero exit = unavailable).


def _resolver(
    tmp_path: Path, body: str, *, name: str = "resolver.sh"
) -> dict[str, str]:
    """A fake resolver + the env pointing the lint at it."""
    stub = tmp_path / name
    stub.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)
    return {**os.environ, "PRECIS_BACKLOG_RESOLVER": str(stub)}


ALL_RESOLVE = 'for h in "$@"; do printf "%s\\tresolved\\n" "$h"; done'


def _steal_file(fm: str = "", body: str = "") -> str:
    front = (
        _fm("draft") if not fm else f"---\nstatus: draft\npillar: platform\n{fm}---\n"
    )
    return front + "# Spec\n\n" + body


def test_prior_art_resolving_handles_are_quiet(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: pa57193, web:608520\n"), encoding="utf-8"
    )
    out = _lint(tmp_path, env=_resolver(tmp_path, ALL_RESOLVE))
    assert "prior-art" not in out


def test_prior_art_dead_handle_fails(tmp_path: Path) -> None:
    """A resolver-verified dead handle is a broken cite — the lint must go
    red, not just print (td611601 finding: it used to exit 0)."""
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: pa99999\n"), encoding="utf-8"
    )
    env = _resolver(
        tmp_path, 'for h in "$@"; do printf "%s\\tmissing\\tno live row\\n" "$h"; done'
    )
    cp = _lint_result(tmp_path, env=env)
    assert cp.returncode != 0
    assert "1 prior-art handle(s) do not resolve" in cp.stdout
    assert "pa99999" in cp.stdout


def test_prior_art_malformed_entry_fails(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: not-a-handle\n"), encoding="utf-8"
    )
    cp = _lint_result(tmp_path, env=_resolver(tmp_path, ALL_RESOLVE))
    assert cp.returncode != 0
    assert "malformed prior-art" in cp.stdout
    assert "not-a-handle" in cp.stdout


def test_steal_sentence_without_prior_art_warns(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(body="Steal identified by the landscape comparison.\n"),
        encoding="utf-8",
    )
    out = _lint(tmp_path, env=_resolver(tmp_path, ALL_RESOLVE))
    assert "name a source in prose only" in out


def test_steal_section_needs_its_own_prior_art_line(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(
            body=(
                "## One\n\nSteal identified by the landscape comparison.\n\n"
                "## Two\n\nprior-art: pa2708\n\nSteal identified by it too.\n"
            )
        ),
        encoding="utf-8",
    )
    out = _lint(tmp_path, env=_resolver(tmp_path, ALL_RESOLVE))
    assert "1 place(s) name a source in prose only" in out
    assert "## One" in out
    assert "## Two" not in out


def test_front_matter_prior_art_covers_section_steals(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(
            fm="prior-art: pa2708\n",
            body="## One\n\nAdapted from the cited method.\n",
        ),
        encoding="utf-8",
    )
    out = _lint(tmp_path, env=_resolver(tmp_path, ALL_RESOLVE))
    assert "prose only" not in out
    assert "prior-art" not in out


def test_prior_art_resolver_unavailable_is_a_note_not_findings(
    tmp_path: Path,
) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: pa2708\n"), encoding="utf-8"
    )
    env = _resolver(tmp_path, 'echo "no prod route" >&2; exit 3')
    out = _lint(tmp_path, env=env)
    assert "do not resolve" not in out
    assert "resolution unavailable" in out


def test_prior_art_resolution_off_via_env(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: pa2708\n"), encoding="utf-8"
    )
    env = {**os.environ, "PRECIS_BACKLOG_RESOLVER": "off"}
    out = _lint(tmp_path, env=env)
    assert "resolution off" in out
    assert "do not resolve" not in out


def test_prior_art_resolution_on_under_ci(tmp_path: Path) -> None:
    """CI is the acceptance path — resolution must not default off there
    (td611601 finding). No PRECIS_BACKLOG_RESOLVER under CI=1 runs the
    default resolver; the old 'off under CI' skip must be gone."""
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: pa2708\n"), encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if k != "PRECIS_BACKLOG_RESOLVER"}
    env["CI"] = "1"
    out = _lint_result(tmp_path, env=env).stdout
    assert "resolution off" not in out


def test_prior_art_handle_with_no_verdict_fails(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text(
        _steal_file(fm="prior-art: pa2708, pa4715\n"), encoding="utf-8"
    )
    # Stub answers only pa2708; pa4715 must not silently pass. The trailing
    # `exit 0` matters — an unanswered-handle stub is not an outage.
    env = _resolver(
        tmp_path,
        'for h in "$@"; do [ "$h" = pa2708 ] && printf "%s\\tresolved\\n" "$h"; '
        "done; exit 0",
    )
    cp = _lint_result(tmp_path, env=env)
    assert cp.returncode != 0
    assert "1 prior-art handle(s) do not resolve" in cp.stdout
    assert "pa4715" in cp.stdout
