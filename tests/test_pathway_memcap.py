"""Per-child memory cap on autocatpath children (gr450103)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("autocatpath")

from precis_pathway import runner


def test_mem_limit_env_parsing(monkeypatch):
    monkeypatch.delenv(runner._MEM_LIMIT_ENV, raising=False)
    assert runner._mem_limit_bytes() == 32 * 1024**3
    monkeypatch.setenv(runner._MEM_LIMIT_ENV, "1.5")
    assert runner._mem_limit_bytes() == int(1.5 * 1024**3)
    monkeypatch.setenv(runner._MEM_LIMIT_ENV, "0")
    assert runner._mem_limit_bytes() == 0
    monkeypatch.setenv(runner._MEM_LIMIT_ENV, "junk")
    assert runner._mem_limit_bytes() == 32 * 1024**3


def test_cap_disabled_is_noop(monkeypatch):
    monkeypatch.setenv(runner._MEM_LIMIT_ENV, "0")
    assert runner._apply_memory_cap() == 0


def test_aggregate_child_over_cap_fails_cleanly(monkeypatch):
    """The combine runs in a child under the cap: a real child (spawned via
    ``_child_cmd``) that over-allocates surfaces as a RuntimeError carrying
    'memory limit exceeded', not a worker crash (Linux only)."""
    if not sys.platform.startswith("linux"):
        pytest.skip("RLIMIT_DATA not enforced off Linux")
    monkeypatch.setenv("PRECIS_AUTOCATPATH_MEM_LIMIT_GB", "1")
    code = (
        "import sys;from precis_pathway import runner;"
        "runner.aggregate_seed_partials=lambda *a,**k: bytearray(3*1024**3);"
        "sys.exit(runner._subprocess_main(['x',sys.argv[1],sys.argv[2]]))"
    )
    monkeypatch.setattr(
        runner,
        "_child_cmd",
        lambda req, out: [sys.executable, "-c", code, req, out],
    )
    with pytest.raises(RuntimeError, match="memory limit exceeded"):
        runner.aggregate_seed_partials_subprocess({"name": "x"}, [{"partial": {}}])


def test_aggregate_child_roundtrip(monkeypatch):
    """Happy path through the real child entrypoint, stubbed combine."""
    code = (
        "import sys;from precis_pathway import runner;"
        "runner.aggregate_seed_partials="
        "lambda cfg, rs, force_backend=None: {'n': len(rs), 'name': cfg['name']};"
        "sys.exit(runner._subprocess_main(['x',sys.argv[1],sys.argv[2]]))"
    )
    monkeypatch.setattr(
        runner,
        "_child_cmd",
        lambda req, out: [sys.executable, "-c", code, req, out],
    )
    out = runner.aggregate_seed_partials_subprocess({"name": "x"}, [{"partial": {}}])
    assert out == {"n": 1, "name": "x"}


def test_child_over_cap_reports_classified_error(tmp_path):
    """A real child allocating past a 1 GB cap writes an ok=False envelope
    saying 'memory limit exceeded' (Linux only; elsewhere the cap is a no-op)."""
    if not sys.platform.startswith("linux"):
        pytest.skip("RLIMIT_DATA not enforced off Linux")
    code = (
        "import sys;from precis_pathway import runner;"
        "runner.run_seed_partial=lambda *a,**k: bytearray(3*1024**3);"
        "sys.exit(runner._subprocess_main(['x',sys.argv[1],sys.argv[2]]))"
    )
    req = tmp_path / "req.json"
    out = tmp_path / "out.json"
    req.write_text(
        json.dumps({"config": {}, "seed": 0, "model_index": 0}), encoding="utf-8"
    )
    env = {**os.environ, "PRECIS_AUTOCATPATH_MEM_LIMIT_GB": "1"}
    proc = subprocess.run(
        [sys.executable, "-c", code, str(req), str(out)],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert proc.returncode == 1, proc.stderr[-500:]
    assert payload["ok"] is False
    assert "memory limit exceeded" in payload["error"]
