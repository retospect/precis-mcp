"""The FDA-grant audit's interpreter list covers the asa chat daemons (gr530710).

asa-bot and asa-slack share /opt/asa/venv, which the asa roles build against
/opt/homebrew/bin/python3 --symlinks — it resolves to an ad-hoc-signed
Homebrew Cellar python, so a `brew upgrade` re-mints the cdhash and silently
voids the daemon's Full Disk Access grant. That is exactly the drift the
monthly audit exists to catch, yet tcc_profile_binaries (and so the rendered
per-host checklist and the runbook loop) never listed it. The entry is the
venv path — never a pinned Cellar path, which every brew bump silently drops.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
_DEFAULTS = _REPO / "deploy" / "roles" / "tcc_profile" / "defaults" / "main.yml"
_RUNBOOK = _REPO / "docs" / "runbooks" / "fda-grant-review.md"

_ASA_VENV = "/opt/asa/venv/bin/python3"


def _binaries() -> list[str]:
    defaults: dict[str, Any] = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    return defaults["tcc_profile_binaries"]


def test_asa_bridge_venv_is_in_the_canonical_list() -> None:
    assert _ASA_VENV in _binaries()


def test_no_entry_pins_a_homebrew_cellar_path() -> None:
    """A Cellar path names one exact brew build: the role's stat loop drops
    it as missing after any `brew upgrade python`, silently un-auditing the
    daemon. Entries stay venv/stable paths resolved at audit time."""
    cellar = [p for p in _binaries() if "Cellar" in p]
    assert not cellar, cellar


def test_runbook_audit_loop_covers_every_listed_opt_python() -> None:
    """The canonical list cannot outrun the audit procedure: every /opt
    interpreter the checklist can render must appear in the runbook's
    `for v in ...` loop."""
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    loop = re.search(r"for v in (.+?); do", runbook, re.S)
    assert loop, "runbook lost the interpreter-resolution loop"
    loop_paths = set(re.findall(r"/\S+", loop.group(1)))
    for path in _binaries():
        if path.startswith("/opt/"):
            assert path in loop_paths, f"{path} listed but not audited"
    assert _ASA_VENV in loop_paths


def test_runbook_states_the_current_fleet_scope() -> None:
    """The host list reflects the fleet: caspar has no canonical venvs or
    heartbeat row left, and the castor/pollux heartbeat rows are marked
    Linux noise — not macOS hosts to audit."""
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert re.search(r"caspar has no canonical venvs", runbook)
    assert re.search(r"castor and pollux[^\n]*Linux", runbook, re.I)
