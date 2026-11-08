"""deny_list boundary probe (td574934 A4, gr596928).

The deny list is the fence a slice may not write past. Two halves:

* every glob on the deny lists refuses a path —
  :func:`precis.workers.job_types.run_slice.deny_violations` must
  return a witness path built from each glob of
  :data:`lanes.FACTORY_DENY_WRITE_PATHS`, :data:`lanes.HUMAN_ONLY_PATHS`
  and :data:`lanes.INTERIM_DENY` under every resolved set that carries
  it, and a human-only glob must refuse even under an empty lane set
  (never relaxable);
* the lane deny set agrees with the registry —
  :func:`lanes.deny_paths_for` resolves :data:`STRICT_DENY` with no
  setting and the relaxed ``HUMAN_ONLY_PATHS + INTERIM_DENY`` with the
  flag on; :data:`STRICT_DENY` / :data:`ENGINE_PATHS` equal their
  documented derivations; a fresh :class:`lanes.Lane` defaults to
  ``STRICT_DENY``;
* every glob in :data:`_REGISTRY_FLOOR` is still a verbatim member of
  its named tuple — the floor is pinned HERE, not re-derived from the
  branch (td598968): a glob removed consistently from ``lanes.py``
  (source tuple and every derived set in lockstep) leaves all the
  derivations above self-consistent yet the protection is gone.

When A2's ``precis.plans.boundary`` lands it becomes the hit-list
registry; this probe already prefers its sets when importable
(TODO A2: drop the lanes fallback then).

Contract: exit 0 pass, 1 boundary breach (stdout = the finding), else
cannot-run (exit 2 here). ``argv[1]`` names the tree root (default: cwd).
"""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from types import SimpleNamespace

#: The trusted registry floor (td598968): the globs each lanes tuple
#: must still carry, pinned verbatim in the probe — the only copy a
#: branch cannot edit without hitting this boundary (main's probe blob
#: runs even when the branch touches lanes.py AND the probe). A branch
#: may ADD a glob; removing one trips here even when every derived set
#: is updated consistently, which is exactly the removal the derivation
#: checks above cannot see. A legitimate retirement lands on main's
#: copy of this floor first.
_REGISTRY_FLOOR: dict[str, tuple[str, ...]] = {
    "FACTORY_DENY_WRITE_PATHS": (
        "scripts/ship",
        "scripts/test",
        "scripts/lib/**",
        ".github/**",
        "src/precis/plans/**",
        "src/precis/workers/job_types/run_slice.py",
        "src/precis/workers/job_types/plan_dispatch.py",
        "src/precis/workers/job_types/gripe_validate.py",
        "src/precis/plans/release.py",
        "src/precis/plans/lanes.py",
        "src/precis/workers/job_types/release_cut.py",
        "src/precis/workers/job_types/ci_verdict_poll.py",
        "src/precis/workers/job_types/ci_red_digest.py",
        "src/precis/workers/job_types/ci_rerun.py",
        "src/precis/workers/job_types/release_poll.py",
        "src/precis/workers/job_types/release_deploy.py",
        "src/precis/workers/job_types/release_confirm.py",
        "src/precis/workers/job_types/land_slice.py",
        "scripts/release",
        "scripts/ci-rerun",
        "src/precis/alert_ids.py",
        "src/precis/alerts.py",
        "src/precis/workers/nursery.py",
        "deploy/roles/monitoring/**",
    ),
    "HUMAN_ONLY_PATHS": (
        "deploy/inventory*/**",
        "deploy/*vault*",
        "deploy/*group_vars*",
        "deploy/*host_vars*",
        "deploy/ansible.cfg",
        "deploy/group_vars/all.yml",
        "deploy/roles/hardening/**",
        "deploy/playbooks/10-hardening.yml",
        "deploy/mcp-http/**",
        "deploy/playbooks/20b-precis-worker-collapsed.yml",
        "deploy/playbooks/20c-precis-heartbeat-serving.yml",
        "deploy/playbooks/20f-precis-worker-oasnap.yml",
        "deploy/playbooks/20g-precis-worker-slices.yml",
        "deploy/playbooks/22-papers-sync.yml",
        "deploy/run-*.yml",
        "deploy/roles/asa_bot/templates/claude_mcp.json.j2",
        "deploy/roles/asa_bot/templates/claude_settings.json.j2",
        "deploy/roles/asa_bot/templates/com.asa.bot.plist.j2",
        "deploy/roles/asa_bot/templates/config.yaml.j2",
        "deploy/roles/asa_slack/templates/com.asa.slack.plist.j2",
        "deploy/roles/asa_slack/templates/config.yaml.j2",
        "deploy/roles/backups/templates/b2_sync.sh.j2",
        "deploy/roles/db_exporters/defaults/main.yml",
        "deploy/roles/db_exporters/tasks/main.yml",
        "deploy/roles/db_exporters/templates/db-exporters.env.j2",
        "deploy/roles/db_exporters/templates/launcher.sh.j2",
        "deploy/roles/extract_watch/templates/extract-watch.plist.j2",
        "deploy/roles/monitoring/defaults/main.yml",
        "deploy/roles/monitoring/templates/precis-alertmanager-receiver.service.j2",
        "deploy/roles/nginx/tasks/main.yml",
        "deploy/roles/pgbouncer/templates/pgbouncer.ini.j2",
        "deploy/roles/pgbouncer/templates/userlist.txt.j2",
        "deploy/roles/pgpass/templates/pgpass.j2",
        "deploy/roles/postgres/tasks/main.yml",
        "deploy/roles/postgres/templates/channel_prompts.sql.j2",
        "deploy/roles/postgres/templates/pg_hba.conf.j2",
        "deploy/roles/precis_embedder/templates/precis-embedder.plist.j2",
        "deploy/roles/precis_embedder/templates/precis-embedder.service.j2",
        "deploy/roles/precis_watch/defaults/main.yml",
        "deploy/roles/precis_watch/templates/precis-watch.plist.j2",
        "deploy/roles/precis_watch/templates/precis-watch.service.j2",
        "deploy/roles/precis_web/defaults/main.yml",
        "deploy/roles/precis_web/tasks/migrate.yml",
        "deploy/roles/precis_web/templates/precis-web.plist.j2",
        "deploy/roles/precis_worker/tasks/provision.yml",
        "deploy/roles/precis_worker/templates/precis-worker.plist.j2",
        "deploy/roles/precis_worker/templates/precis-worker.service.j2",
        "deploy/roles/precis_worker_agent/tasks/provision.yml",
        "deploy/roles/precis_worker_agent/templates/com.precis.colima.plist.j2",
        "deploy/roles/precis_worker_agent/templates/precis-worker-agent.plist.j2",
        "deploy/roles/precis_worker_agent/templates/precis-worker-agent.service.j2",
        "deploy/roles/service_unit/templates/launchd.plist.j2",
        "deploy/roles/service_unit/templates/systemd.service.j2",
        "deploy/roles/tailscale/tasks/main.yml",
        "deploy/roles/tcc_profile/**",
        "deploy/roles/users/**",
        ".claude/**",
        "*/.claude/**",
        ".codex/**",
        ".devin/**",
        ".kimi-code/**",
        ".mcp.json",
        "*/CLAUDE.md",
        "*/AGENTS.md",
        "CLAUDE.local.md",
        "*/CLAUDE.local.md",
        "scripts/hooks/**",
        "CLAUDE.md",
        "AGENTS.md",
    ),
    "INTERIM_DENY": (
        "src/precis/plans/lanes.py",
        "scripts/ship",
        "scripts/test",
        "scripts/impacted-tests",
        "scripts/migration-check",
        "scripts/mutate-diff",
        "scripts/gen-devin-hooks",
        "scripts/deploy",
        "scripts/round",
        "scripts/lib/**",
        ".github/**",
    ),
}


def _witness(glob: str) -> str:
    """One concrete path a ``fnmatch`` glob must match: ``**`` → a
    nested pair, ``*``/``?``/``[…]`` → literal fill."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob[i : i + 2] == "**":
            out.append("zz/zz")
            i += 2
        elif glob[i] == "*":
            out.append("zz")
            i += 1
        elif glob[i] == "?":
            out.append("z")
            i += 1
        elif glob[i] == "[":
            j = glob.find("]", i)
            if j < 0:
                raise ValueError(f"unclosed class in glob {glob!r}")
            cls = glob[i + 1 : j]
            out.append(cls[1] if cls.startswith("!") and len(cls) > 1 else cls[0])
            i = j + 1
        else:
            out.append(glob[i])
            i += 1
    return "".join(out)


def _deny_sets() -> dict[str, tuple[str, ...]]:
    """The resolved sets to verify — A2's registry when it exists, else
    the lanes.py tuples (TODO A2)."""
    try:
        from precis.plans import boundary as _b  # A2
    except ImportError:
        _b = None
    if _b is not None and hasattr(_b, "LANE_DENY_SETS"):
        return dict(_b.LANE_DENY_SETS)
    from precis.plans import lanes

    return {
        "strict": lanes.STRICT_DENY,
        "relaxed": lanes.HUMAN_ONLY_PATHS + lanes.INTERIM_DENY,
    }


class _Conn:
    """A store double whose app_settings row reads "1" (relaxed mode)."""

    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return ("1",)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Pool:
    def connection(self):
        return _Conn()


def _agreement(lanes) -> list[str]:
    """The lane deny set agrees with the registry."""
    findings: list[str] = []
    strict = lanes.deny_paths_for(None)
    if strict != lanes.STRICT_DENY:
        findings.append(
            "deny_paths_for(None) is not STRICT_DENY — the default deny set "
            "drifted from the registry"
        )
    relaxed = set(lanes.deny_paths_for(SimpleNamespace(pool=_Pool())))
    want_relaxed = set(lanes.HUMAN_ONLY_PATHS) | set(lanes.INTERIM_DENY)
    if relaxed != want_relaxed:
        findings.append("the relaxed deny set is not HUMAN_ONLY_PATHS + INTERIM_DENY")
    want_strict = tuple(
        dict.fromkeys(lanes.FACTORY_DENY_WRITE_PATHS + lanes.HUMAN_ONLY_PATHS)
    )
    if want_strict != lanes.STRICT_DENY:
        findings.append(
            "STRICT_DENY ≠ dedup(FACTORY_DENY_WRITE_PATHS + HUMAN_ONLY_PATHS)"
        )
    want_engine = tuple(
        dict.fromkeys(
            g
            for g in lanes.FACTORY_DENY_WRITE_PATHS + lanes.INTERIM_DENY
            if g not in lanes.HUMAN_ONLY_PATHS
        )
    )
    if want_engine != lanes.ENGINE_PATHS:
        findings.append("ENGINE_PATHS ≠ dedup(FACTORY + INTERIM) − HUMAN_ONLY")
    lane = lanes.Lane(
        name="probe",
        adapter="claude_p",
        credential_pool="probe",
        cap=1,
        task_classes=frozenset(),
    )
    if lane.deny_write_paths != lanes.STRICT_DENY:
        findings.append("a lane's default deny_write_paths is not STRICT_DENY")
    return findings


def _floor(lanes) -> list[str]:
    """Every :data:`_REGISTRY_FLOOR` glob the branch's tuple dropped —
    the trusted-expectations check: expectations pinned in the probe,
    never derived from the tuples under test."""
    findings: list[str] = []
    for attr, want in _REGISTRY_FLOOR.items():
        have = set(getattr(lanes, attr, None) or ())
        missing = [g for g in want if g not in have]
        if missing:
            findings.append(
                f"{attr} dropped protected glob(s): " + ", ".join(missing[:8])
            )
    return findings


def check(root: Path) -> list[str]:
    """Every finding; empty = pass."""
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from precis.plans import lanes
    from precis.workers.job_types import run_slice

    findings: list[str] = _floor(lanes)
    for label, globs in _deny_sets().items():
        for glob in dict.fromkeys(globs):
            witness = _witness(glob)
            if not fnmatch.fnmatchcase(witness, glob):
                raise ValueError(
                    f"probe bug: witness {witness!r} does not match {glob!r}"
                )
            hits = run_slice.deny_violations([witness], tuple(globs))
            if witness not in hits:
                findings.append(
                    f"deny glob {glob!r} ({label} set) does not refuse {witness!r}"
                )
    for glob in lanes.HUMAN_ONLY_PATHS:
        witness = _witness(glob)
        if witness not in run_slice.deny_violations([witness], ()):
            findings.append(
                f"human-only glob {glob!r} escapes an empty lane deny set "
                f"({witness!r} passed)"
            )
    findings += _agreement(lanes)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Probe entry: ``[tree_root]``; 0 pass, 1 breach, 2 cannot-run."""
    argv = list(sys.argv if argv is None else argv)
    root = Path(argv[1]).resolve() if len(argv) > 1 else Path.cwd()
    try:
        findings = check(root)
    except Exception as exc:
        print(f"cannot-run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
