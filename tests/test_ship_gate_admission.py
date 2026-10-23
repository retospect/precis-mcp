"""scripts/ship --remote caps concurrent ci/* gate runs client-side (gr495187).

A ship wave ran 20 check.yml gates at once (~240 jobs against the 40-runner
account cap, 2026-10-14..21) and main's + release's shard jobs queued behind
them (main run→first job p90 19 min, max 80). A shared concurrency group
cannot express the cap — a group keeps ONE pending run and cancels the rest,
which a ship reads as a red gate — so ship holds its ci/<branch> push until
fewer than PRECIS_REMOTE_GATE_SLOTS (default 2) ci/* runs are queued or in
flight.

The functions are cut out of the real script and run under bash with a
stubbed `gh` on PATH — no network, no GitHub.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the shipped bash script is POSIX-only"
)

SHIP = Path(__file__).resolve().parent.parent / "scripts" / "ship"


def _functions() -> str:
    text = SHIP.read_text(encoding="utf-8")
    out = []
    for name in ("_ci_gate_runs_in_flight", "_remote_gate_admit"):
        m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
        assert m, f"{name} not found in scripts/ship"
        out.append(m.group(0))
    return "".join(out)


GH_STUB = """\
#!/usr/bin/env python3
import json, os, re, sys
d = os.environ["GH_STUB_DIR"]
if os.environ.get("GH_STUB_FAIL"):
    sys.exit(1)
url = next((a for a in sys.argv[1:] if "/runs?" in a), "")
m = re.search(r"status=(\\w+)", url)
status = m.group(1) if m else "queued"
seq = json.loads(open(os.path.join(d, f"seq-{status}.json")).read())
ctr = os.path.join(d, f"n-{status}")
i = int(open(ctr).read()) if os.path.exists(ctr) else 0
open(ctr, "w").write(str(i + 1))
branches = seq[min(i, len(seq) - 1)]
prefix = re.search(r'startswith\\("([^"]+)"\\)', " ".join(sys.argv[1:]))
assert prefix, "ship must filter runs by a head_branch startswith() prefix"
print(sum(1 for b in branches if b.startswith(prefix.group(1))))
"""


@pytest.fixture()
def stub(tmp_path: Path):
    d = tmp_path / "ghstub"
    d.mkdir()
    gh = d / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    for status in ("queued", "in_progress"):
        (d / f"seq-{status}.json").write_text("[[]]", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GH_STUB")}
    env["GH_STUB_DIR"] = str(d)
    env["PATH"] = f"{d}:{os.environ['PATH']}"
    return d, env


def _run(stub, body: str) -> subprocess.CompletedProcess:
    d, env = stub
    script = (
        "set -euo pipefail\n"
        'say() { echo "SAY $*"; }\n'
        "sleep() { :; }\n"
        "BRANCH=t-branch\n" + _functions() + body
    )
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _set_runs(stub, queued: list[list[str]], in_progress: list[list[str]]) -> None:
    d, _ = stub
    (d / "seq-queued.json").write_text(json.dumps(queued), encoding="utf-8")
    (d / "seq-in_progress.json").write_text(json.dumps(in_progress), encoding="utf-8")


def test_counts_only_ci_heads_across_both_statuses(stub) -> None:
    _set_runs(
        stub,
        queued=[["ci/a", "main", "release/r9"]],
        in_progress=[["ci/b", "ci/c", "dependabot/x"]],
    )
    p = _run(stub, "_ci_gate_runs_in_flight")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "3"


def test_admits_immediately_below_the_cap(stub) -> None:
    _set_runs(stub, queued=[["ci/a"]], in_progress=[[]])
    p = _run(stub, "_remote_gate_admit 2 60 && echo ADMITTED")
    assert p.returncode == 0, p.stderr
    assert "ADMITTED" in p.stdout
    assert "SAY" not in p.stdout


def test_waits_for_a_slot_then_admits(stub) -> None:
    # Two ci runs in flight on the first poll, one on the second.
    _set_runs(stub, queued=[[]], in_progress=[["ci/a", "ci/b"], ["ci/a"]])
    p = _run(stub, "_remote_gate_admit 2 60 && echo ADMITTED")
    assert p.returncode == 0, p.stderr
    assert "ADMITTED" in p.stdout
    assert "cap 2" in p.stdout


def test_budget_spent_is_a_refusal_not_a_hang(stub) -> None:
    _set_runs(stub, queued=[[]], in_progress=[["ci/a", "ci/b", "ci/c"]])
    p = _run(
        stub,
        'if _remote_gate_admit 2 0; then echo RC=0; else echo "RC=$?"; fi',
    )
    assert p.returncode == 0, p.stderr  # caller dies on rc; the function returns 1
    assert "RC=1" in p.stdout
    assert "cap 2" in p.stderr


def test_unreadable_count_proceeds(stub) -> None:
    d, env = stub
    env["GH_STUB_FAIL"] = "1"
    p = _run(stub, "_remote_gate_admit 2 60 && echo ADMITTED")
    assert p.returncode == 0, p.stderr
    assert "ADMITTED" in p.stdout


def test_zero_slots_disables_the_cap(stub) -> None:
    _set_runs(stub, queued=[[]], in_progress=[["ci/a"] * 9])
    p = _run(stub, "_remote_gate_admit 0 60 && echo ADMITTED")
    assert p.returncode == 0, p.stderr
    assert "ADMITTED" in p.stdout


def test_remote_gate_waits_for_admission_before_pushing(stub) -> None:
    # Remote mode must consult the cap before the ci/<branch> push exists.
    text = SHIP.read_text(encoding="utf-8")
    m = re.search(r"^remote_gate_wait\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert m, "remote_gate_wait not found in scripts/ship"
    body = m.group(0)
    admit = body.index("_remote_gate_admit")
    push = body.index("git push -q -f origin")
    assert admit < push
    assert "PRECIS_REMOTE_GATE_SLOTS" in body
