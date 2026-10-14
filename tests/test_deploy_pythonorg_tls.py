"""pythonorg TLS assert runs only when TLS truth could have moved.

r34's ~25-30 min deploy spent a chunk of it in the python.org live-handshake
assert's serialized 30s-timeout retries — on converged hosts where nothing
had fetched a cert or relinked cert.pem. The assert now runs when the series
version moved (an install) or the CA store was just populated; the
Developer-ID signature assert still runs every deploy, and install-time
verification is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="deploy roles are POSIX; nothing here runs on Windows hosts",
)

ROLE = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "roles"
    / "pythonorg"
    / "tasks"
    / "install_one.yml"
)


def _tasks() -> list[dict]:
    data = yaml.safe_load(ROLE.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return data


def _named(tasks: list[dict], fragment: str) -> dict:
    for t in tasks:
        if fragment in str(t.get("name", "")):
            return t
    raise AssertionError(f"no task matching {fragment!r} in {ROLE}")


def test_tls_assert_runs_on_install_or_fresh_ca_store_only() -> None:
    task = _named(_tasks(), "can verify TLS")
    when = str(task.get("when", ""))
    assert "not pythonorg_up_to_date" in when
    assert "pythonorg_ca_populate" in when


def test_ca_store_populate_registers_for_the_tls_gate() -> None:
    task = _named(_tasks(), "Populate python.org")
    assert task.get("register") == "pythonorg_ca_populate"


def test_signature_assert_still_runs_every_deploy() -> None:
    task = _named(_tasks(), "carries a Developer-ID TeamIdentifier")
    assert "when" not in task


def test_download_and_install_stay_gated_on_up_to_date() -> None:
    tasks = _tasks()
    for fragment in ("Download python.org", "Install python.org"):
        assert (
            str(_named(tasks, fragment).get("when", "")) == "not pythonorg_up_to_date"
        )
