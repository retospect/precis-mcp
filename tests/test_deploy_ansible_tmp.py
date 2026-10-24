"""Per-user ansible tmp/cache paths on the controller (gr513889).

r95's final verify play died at "Fetch once on the controller" — a
delegate_to: localhost task running as the operator's user could not enter
remote_tmp (/tmp/.ansible/tmp) after a deploy-user ssh run minted it mode
700. remote_tmp is back on ansible's per-user default (~ expands per
connecting user) and fact_caching_connection moved with it — a fixed /tmp
path there just flips the failure to whichever user did not create it
first. These tests pin that neither setting is a shared absolute path, and
that no play re-pins remote_tmp to one.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parent.parent / "deploy"


def _defaults() -> configparser.SectionProxy:
    parser = configparser.ConfigParser()
    parser.read(_DEPLOY / "ansible.cfg", encoding="utf-8")
    return parser["defaults"]


def _assert_per_user(setting: str, value: str) -> None:
    assert not Path(value).is_absolute(), (
        f"{setting}={value!r}: a shared absolute path is minted mode-700 by "
        "whichever user reaches it first (gr513889)"
    )
    assert value.startswith("~/") or "$" in value, (
        f"{setting}={value!r} is not per-user; want ~ or $USER expansion"
    )


def test_remote_tmp_is_per_user() -> None:
    _assert_per_user("remote_tmp", _defaults()["remote_tmp"])


def test_fact_cache_connection_is_per_user() -> None:
    _assert_per_user("fact_caching_connection", _defaults()["fact_caching_connection"])


def test_no_play_repins_remote_tmp_to_a_shared_path() -> None:
    """localhost-delegated tasks used to carry ansible_remote_tmp vars; the
    global is per-user now, so only a shared-path re-pin can regress."""
    offenders = []
    for path in _DEPLOY.rglob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#"):
                continue
            m = re.search(r"(?:ansible_)?remote_tmp:\s*['\"]?(\S+)", line)
            if m and Path(m.group(1)).is_absolute():
                offenders.append(f"{path}:{m.group(1)}")
    assert offenders == [], f"shared remote_tmp re-pinned: {offenders}"
