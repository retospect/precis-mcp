"""Deploy-lint: the `anki_sync` cadence's runtime deps survive role churn.

`_anki_sync_eligible()` (workers/scheduler.py) gates on **two** things —
`PRECIS_ANKI_ENABLED` in the env AND the `anki` pylib being importable — and an
ineligible host short-circuits *before* `claim_scheduler_lease`. So losing
either one produces no error, no log line, and no failed job: the lease simply
stops advancing.

That is what happened, and the mechanism is subtler than a missing task. 20b DOES
import `precis_worker_agent`'s provisioning half, and its anki task DID keep
running — but it installs into `precis_worker_agent_venv` (/opt/mcps/venv), the
retired agent daemon's venv, while the collapsed `--profile all` worker runs from
`precis_worker_venv` (/opt/precis/venv). Verified on melchior 2026-08-07: anki
importable in the former, absent in the latter. The pylib was on the box the
whole time, just never on the path the live interpreter searched — and
`PRECIS_ANKI_ENABLED=1` stayed set, which is what made the host look configured.
`anki_sync` last fired 2026-08-04; the watchdog alarmed and auto-filed gripe
193428, and nothing acted on it for ~3 days.

The tests assert the *linkage* — the unit that enables anki must be provisioned
by a path that installs anki — because the regression was a venv mismatch, not a
deletion: every task involved kept running, at the wrong interpreter.
"""

from __future__ import annotations

import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
_PROVISION = _DEPLOY / "roles" / "precis_worker" / "tasks" / "provision.yml"
_COLLAPSED = _DEPLOY / "playbooks" / "20b-precis-worker-collapsed.yml"

#: Matches the actual install task, not a comment mentioning it.
_INSTALL_RE = re.compile(r"pip install[^\n]*\s--upgrade\s+anki\b")


def _uncommented(text: str) -> str:
    """Drop `#` comment lines so a *mention* of a var never satisfies a check."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_shared_worker_provisioning_installs_the_anki_pylib() -> None:
    """The provisioning half BOTH worker playbooks import must install `anki`.

    `provision.yml` is the §L-b split's shared half — playbook 20 uses the full
    role, 20b imports only this file — so putting the install here is what makes
    it reach the collapsed unit that actually renders the env.
    """
    assert _INSTALL_RE.search(_PROVISION.read_text()), (
        "precis_worker/tasks/provision.yml no longer installs the `anki` pylib "
        "— _anki_sync_eligible() goes false and the cadence stalls silently"
    )


def test_the_unit_that_enables_anki_is_provisioned_by_that_half() -> None:
    """Whoever sets `PRECIS_ANKI_ENABLED` must import the half that installs it.

    This is the assertion that would have caught the original break: the
    collapsed playbook enabled anki while importing a provisioning half with no
    anki install, and nothing anywhere objected.
    """
    collapsed = _uncommented(_COLLAPSED.read_text())
    if "PRECIS_ANKI_ENABLED" not in collapsed:
        # The collapsed unit stopped enabling anki — then it needs no pylib, and
        # whatever unit took over is covered by its own role's tasks.
        return
    assert "precis_worker/tasks/provision.yml" in collapsed or re.search(
        r"tasks_from:\s*provision", collapsed
    ), (
        "20b-precis-worker-collapsed.yml enables PRECIS_ANKI_ENABLED but no "
        "longer imports precis_worker's provision.yml — the host will render "
        "the env with no `anki` pylib, the exact 2026-08-04 stall"
    )


def test_anki_is_not_treated_as_a_precis_extra() -> None:
    """`precis-mcp[anki]` does not exist — asking for it breaks the install.

    The tempting one-line fix is to append `anki` to the worker's
    `uv pip install precis-mcp[...]` extras. `pyproject.toml` declares no `anki`
    extra, so uv rejects it and the whole worker venv install (and the deploy)
    fails. Guard the tempting fix, not just the real one.
    """
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "\nanki = [" not in pyproject, (
        "an `anki` extra now exists — if deliberate, the worker role may use it "
        "and this guard should be retired"
    )

    for path in sorted((_DEPLOY / "roles").rglob("*.yml")):
        for line in path.read_text().splitlines():
            if "precis-mcp[" in line and not line.lstrip().startswith("#"):
                extras = line.split("precis-mcp[", 1)[1].split("]", 1)[0]
                assert "anki" not in extras, (
                    f"{path.name} asks for a nonexistent `anki` extra in "
                    f"{extras!r} — uv rejects it and the venv install fails"
                )
