"""The canary verify loop must call `scripts/prod-psql` via `${REPO_ROOT}`, not
a bare relative path.

`scripts/deploy` does `cd "$CLUSTER_DIR"` (a rendered `<worktree>/deploy`)
before the opt-in `PRECIS_DEPLOY_CANARY` verify loop runs. `scripts/prod-psql`
does not exist under `deploy/`, so a bare relative invocation there always
fails to exec, and the loop's die message ("scripts/prod-psql failed reaching
prod") reads as a query/connectivity failure rather than the wrong-cwd bug it
actually is. Every other helper invoked after that `cd` already goes through
`${REPO_ROOT}/scripts/...` (wheel-smoke, deploy-state.sh); this pins
`scripts/prod-psql` to the same convention.

Static check: the runtime canary tests exercise `--limit`/heartbeat behavior
with fake ansible binaries, but never actually shell out to a real
`scripts/prod-psql`, so this file greps the script text directly rather than
duplicating that harness.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SRC = REPO_ROOT / "scripts" / "deploy"

_CD_CLUSTER_DIR_RE = re.compile(r'cd\s+"\$CLUSTER_DIR"')
_PROD_PSQL_RE = re.compile(r"scripts/prod-psql\b")


def test_canary_verify_calls_prod_psql_via_repo_root() -> None:
    lines = DEPLOY_SRC.read_text(encoding="utf-8").splitlines()

    cd_line_indices = [
        i for i, line in enumerate(lines) if _CD_CLUSTER_DIR_RE.search(line)
    ]
    assert cd_line_indices, 'expected a `cd "$CLUSTER_DIR"` line in scripts/deploy'
    cd_line_index = cd_line_indices[0]

    offenders = []
    for i, line in enumerate(lines[cd_line_index + 1 :], start=cd_line_index + 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = _PROD_PSQL_RE.search(line)
        if not match:
            continue
        # Only command *invocations* matter here — the die messages below also
        # mention "scripts/prod-psql" in prose (e.g. "scripts/prod-psql query
        # failed"), which is fine to leave bare. An invocation runs inside a
        # `$(...)` command substitution on the same physical line; prose does
        # not.
        if "$(" not in line[: match.start()]:
            continue
        if "${REPO_ROOT}/scripts/prod-psql" in line:
            continue
        offenders.append(f"line {i + 1}: {line!r}")

    assert not offenders, (
        'bare relative scripts/prod-psql call(s) after `cd "$CLUSTER_DIR"` — '
        "must be invoked as ${REPO_ROOT}/scripts/prod-psql:\n" + "\n".join(offenders)
    )
