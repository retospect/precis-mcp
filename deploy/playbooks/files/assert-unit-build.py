#!/usr/bin/env python3
# Managed by ansible (deploy/playbooks/files/assert-unit-build.py; rendered
# by playbooks 43 and 35).
# assert-unit-build.py UNIT [EXPECTED_SHA] — exit 0 iff UNIT is
# active, the venv its ExecStart runs from holds precis-mcp at
# EXPECTED_SHA (skipped when not a 40-char sha, e.g. a branch
# deploy), and its main process started after that install.
import glob
import json
import os
import re
import subprocess
import sys
import time
from typing import NoReturn


def fail(msg: str) -> NoReturn:
    print(f"assert-unit-build: {msg}", file=sys.stderr)
    sys.exit(1)


def main(argv):
    unit = argv[1]
    expected = argv[2] if len(argv) > 2 else ""
    out = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "ActiveState",
            "-p",
            "MainPID",
            "-p",
            "ExecStart",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    props = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)
    state, pid = props.get("ActiveState", ""), props.get("MainPID", "0").strip()
    if state != "active" or pid in ("", "0"):
        fail(f"{unit} is {state or 'unknown'} (MainPID={pid or '?'}), not running")
    m = re.search(r"path=(\S+)", props.get("ExecStart", ""))
    if not m:
        fail(f"cannot read the ExecStart binary of {unit}: {props.get('ExecStart')!r}")
    venv = os.path.dirname(os.path.dirname(m.group(1)))
    hits = sorted(
        glob.glob(
            f"{venv}/lib/python*/site-packages/precis_mcp-*.dist-info/direct_url.json"
        )
    )
    if not hits:
        fail(f"{unit} runs {m.group(1)} but {venv} has no precis_mcp install")
    with open(hits[0], encoding="utf-8") as fh:
        installed = (json.load(fh).get("vcs_info") or {}).get("commit_id", "")
    if re.fullmatch(r"[0-9a-f]{40}", expected) and installed != expected:
        fail(
            f"{unit} runs {venv}, which holds {installed or 'no vcs commit'}, not the deployed {expected}"
        )
    etimes = subprocess.run(
        ["ps", "-o", "etimes=", "-p", pid],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    started = time.time() - int(etimes)
    built = os.stat(hits[0]).st_mtime
    # ps reports whole seconds; 2s of slack keeps a restart in the
    # same second as the install from reading as stale.
    if started + 2 < built:
        fail(
            f"{unit} pid {pid} started {int(built - started)}s before {venv} was installed; it runs the old code"
        )
    print(f"ok {unit} pid={pid} venv={venv} sha={installed or '?'}")


if __name__ == "__main__":
    main(sys.argv)
