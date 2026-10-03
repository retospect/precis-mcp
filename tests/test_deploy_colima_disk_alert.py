"""colima Docker-VM disk alert + its textfile-collector script and plist."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import time
from pathlib import Path

import jinja2
import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: runs the rendered bash collector script",
)

_ROLES = Path(__file__).resolve().parent.parent / "deploy" / "roles"
_NE = _ROLES / "node_exporter" / "templates"
_MARKER = "colima-fake-wedge-marker-7f3a"
_UP0 = 'precis_colima_docker_fs_up{profile="default"} 0'


def _render(path: Path, **ctx: object) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return env.from_string(path.read_text(encoding="utf-8")).render(**ctx)


def _rules() -> dict[str, dict]:
    text = _render(
        _ROLES / "monitoring" / "templates" / "alert_rules.yml.j2",
        groups={},
        hostvars={},
        ansible_managed="x",
    )
    return {r["alert"]: r for g in yaml.safe_load(text)["groups"] for r in g["rules"]}


def test_alert_rules_cover_usage_probe_down_and_stale() -> None:
    rules = _rules()
    hi = rules["ColimaDockerDiskHigh"]
    assert "> 0.85" in hi["expr"] and hi["for"] == "10m"
    assert hi["labels"]["severity"] == "warning"
    down = rules["ColimaDockerDiskMetricDown"]
    assert "precis_colima_docker_fs_up == 0" in down["expr"]
    assert down["for"] == "15m"
    stale = rules["ColimaDockerDiskMetricStale"]
    assert "node_textfile_mtime_seconds" in stale["expr"]
    assert "> 900" in stale["expr"] and stale["for"] == "5m"
    assert stale["labels"]["severity"] == "warning"


def test_plist_parses_with_owner_and_interval() -> None:
    text = _render(
        _NE / "com.precis.colima-docker-fs.plist.j2",
        ansible_managed="x",
        node_exporter_colima_user="someone",
    )
    pl = plistlib.loads(text.encode("utf-8"))
    assert pl["UserName"] == "someone"
    assert pl["StartInterval"] == 300
    assert pl["ProgramArguments"] == ["/usr/local/bin/colima-docker-fs-metrics"]
    assert pl["EnvironmentVariables"]["HOME"] == "/Users/someone"


_FAKE = """#!/bin/bash
[ "$*" = "ssh -p default -- df -B1 --output=size,used,avail /var/lib/docker" ] || exit 2
case "$FAKE_MODE" in
  down) exit 1 ;;
  garbage) echo 'not a table'; exit 0 ;;
  wedge) bash -c "exec -a %s sleep 60" & wait ;;
esac
echo '1B-blocks Used Avail'
echo '1000 900 100'
"""


def _setup(tmp_path: Path, timeout_s: int = 5) -> tuple[Path, Path]:
    fake = tmp_path / "colima"
    body = _FAKE % _MARKER
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    script = tmp_path / "run.sh"
    script.write_text(
        _render(
            _NE / "colima-docker-fs-metrics.sh.j2",
            ansible_managed="x",
            node_exporter_colima_profile="default",
            node_exporter_colima_bin=str(fake),
            node_exporter_colima_timeout_s=timeout_s,
            node_exporter_textfile_dir=str(tmp_path),
        ),
        encoding="utf-8",
    )
    return script, tmp_path / "colima_docker_fs.prom"


def _run(script: Path, mode: str = "") -> None:
    env = {"FAKE_MODE": mode, "PATH": "/usr/bin:/bin"}
    subprocess.run(["bash", str(script)], check=True, env=env)


def test_script_writes_metrics(tmp_path: Path) -> None:
    script, out = _setup(tmp_path)
    _run(script)
    ok = out.read_text(encoding="utf-8")
    assert 'precis_colima_docker_fs_size_bytes{profile="default"} 1000' in ok
    assert 'precis_colima_docker_fs_used_bytes{profile="default"} 900' in ok
    assert 'precis_colima_docker_fs_avail_bytes{profile="default"} 100' in ok
    assert 'precis_colima_docker_fs_up{profile="default"} 1' in ok
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_script_down_and_garbage_write_up_zero(tmp_path: Path) -> None:
    script, out = _setup(tmp_path)
    _run(script)  # seed a good file so a stale one would be detectable
    for mode in ("down", "garbage"):
        _run(script, mode)
        text = out.read_text(encoding="utf-8")
        assert _UP0 in text, mode
        assert "size_bytes" not in text, mode
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_script_timeout_writes_up_zero_and_leaves_no_child(tmp_path: Path) -> None:
    script, out = _setup(tmp_path, timeout_s=1)
    t0 = time.monotonic()
    _run(script, "wedge")
    assert time.monotonic() - t0 < 20
    assert _UP0 in out.read_text(encoding="utf-8")
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", _MARKER], capture_output=True, check=False)
    assert left.returncode == 1, left.stdout
