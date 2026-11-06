"""td574937 E1: the factory deny list shrinks to human-only + interim.

Reto's ruling: the factory may build everything except files only a human
may touch (secrets placement, the harness's own rules); engine changes get
a review and a checked land instead of a deny rule. While the
security-review hold (td574934 A3) is undeployed, a small interim list
stays denied too. The relaxation sits behind the DB-only setting
``factory.deny_relaxed`` (default off — off, unreadable or absent means
:data:`STRICT_DENY`: the full factory list plus the never-relaxable
human-only set, td574960).

The deploy scan tests are the guard that keeps ``HUMAN_ONLY_PATHS``
honest: every tracked file under ``deploy/`` that renders a vault variable
or a credential-shaped jinja variable must be human-only (or explicitly
allow-listed here with a reason — never narrow ``HUMAN_ONLY_PATHS`` to
pass).
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis import settings as psettings
from precis.plans import lanes
from precis.store import Store
from tests._policy_scan import (
    ScanIncomplete,
    gitignore_matcher,
    read_text,
    walk_files,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"

_VAULT_RE = re.compile(r"vault_[a-z0-9_]+")
_JINJA_CRED_RE = re.compile(r"\{\{[^}]*(?:dsn|token|password|secret|api_key)[^}]*\}\}")
_JINJA_EXPR_RE = re.compile(r"\{\{[^}]*\}\}")
_JINJA_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)")

#: Variable names the credential-jinja scan may hit in a file without the
#: file being credential-placing — the model-server roles render
#: ``{{ <role>_tiktoken_dir }}``, a path var whose name matches the keyword
#: alternation. path -> allowed variable names (whole-file exemptions do
#: not exist: any OTHER credential-shaped match still fails the scan).
_DEPLOY_SCAN_ALLOWLIST: dict[str, frozenset[str]] = {
    "deploy/roles/sglang/tasks/main.yml": frozenset({"sglang_tiktoken_dir"}),
    "deploy/roles/sglang/templates/sglang.service.j2": frozenset(
        {"sglang_tiktoken_dir"}
    ),
    "deploy/roles/vllm/tasks/main.yml": frozenset({"vllm_tiktoken_dir"}),
    "deploy/roles/vllm/templates/vllm.service.j2": frozenset({"vllm_tiktoken_dir"}),
}


def _cred_vars(text: str) -> set[str]:
    """The variable names inside ``{{…}}`` expressions that trip the
    credential-shaped name scan — the names, not the files, are what an
    allowlist entry exempts."""
    out: set[str] = set()
    for m in _JINJA_EXPR_RE.finditer(text):
        if not _JINJA_CRED_RE.search(m.group(0)):
            continue
        v = _JINJA_VAR_RE.search(m.group(0))
        out.add(v.group(1) if v else m.group(0))
    return out


def _deploy_files() -> list[str]:
    """Repo-relative tracked files under ``deploy/`` — ``git ls-files``
    when the checkout answers, else a filesystem walk honouring the root
    ``.gitignore`` (the container gate runs without ``.git``)."""
    if (_REPO_ROOT / ".git").exists():
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "-c",
                "safe.directory=*",
                "ls-files",
                "-z",
                "deploy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode != 0:
            raise ScanIncomplete(
                OSError(proc.returncode, "git ls-files exited"),
                "listing tracked files under deploy/",
            )
        return [
            rel
            for rel in proc.stdout.split("\0")
            if rel and (_REPO_ROOT / rel).is_file()
        ]
    ignored = gitignore_matcher(_REPO_ROOT)
    return [
        p.relative_to(_REPO_ROOT).as_posix()
        for p in walk_files(_DEPLOY, skip_dirs={"inventory"})
        if not ignored(p.relative_to(_REPO_ROOT).as_posix())
    ]


def _account(**kw: Any) -> Any:
    base = {
        "name": "x",
        "adapter": "claude_p",
        "plan": "subscription",
        "promo_expiry": None,
        "concurrency": 1,
        "task_classes": ("*",),
        "enabled": True,
        "models": (),
        "default_model": None,
        "vendor": "anthropic",
        "lane_rank": 0,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_deny_relaxed_setting_is_db_only() -> None:
    spec = psettings.REGISTRY["factory.deny_relaxed"]
    assert spec.env_var is None and spec.default is False


@pytest.mark.db
def test_deny_set_is_human_only_union_interim(store: Store) -> None:
    psettings.set_setting("factory.deny_relaxed", True, store=store)
    try:
        assert (
            lanes.deny_paths_for(store) == lanes.HUMAN_ONLY_PATHS + lanes.INTERIM_DENY
        )
    finally:
        psettings.clear_setting("factory.deny_relaxed", store=store)


@pytest.mark.db
def test_deny_set_is_the_full_list_when_off_or_on_db_error(store: Store) -> None:
    # Default off, no store and an unreadable store all fail closed: the
    # full list plus the never-relaxable human-only set (td574960) — the
    # slice is told and checked against the same paths in every mode.
    for s in (store, None, object()):
        deny = lanes.deny_paths_for(s)
        assert set(lanes.FACTORY_DENY_WRITE_PATHS) <= set(deny)
        assert set(lanes.HUMAN_ONLY_PATHS) <= set(deny)
        assert len(deny) == len(set(deny))
    psettings.set_setting("factory.deny_relaxed", "junk", store=store)
    try:
        # An unparsable value is not True: the strict set holds.
        deny = lanes.deny_paths_for(store)
        assert set(lanes.FACTORY_DENY_WRITE_PATHS) <= set(deny)
        assert set(lanes.HUMAN_ONLY_PATHS) <= set(deny)
    finally:
        psettings.clear_setting("factory.deny_relaxed", store=store)


@pytest.mark.db
def test_deny_relaxed_ignores_the_registry_default_and_env(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored row is the ONLY input: a builder editing settings.py —
    flipping the default to True and adding an env_var — and setting the
    env still gets the full list while no DB row exists."""
    spec = psettings.REGISTRY["factory.deny_relaxed"]
    monkeypatch.setitem(
        psettings.REGISTRY,
        "factory.deny_relaxed",
        replace(spec, default=True, env_var="PRECIS_DENY_RELAXED_TEST"),
    )
    monkeypatch.setenv("PRECIS_DENY_RELAXED_TEST", "1")
    psettings.invalidate()
    try:
        assert lanes.deny_paths_for(store) == lanes.STRICT_DENY
    finally:
        psettings.invalidate()


def test_every_interim_entry_is_an_engine_path() -> None:
    for glob in lanes.INTERIM_DENY:
        assert lanes.engine_hits([glob]) == [glob], glob


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: execs scripts/deploy")
def test_deploy_and_round_scripts_are_interim_denied() -> None:
    """``scripts/deploy`` and ``scripts/round`` are on no deny list today —
    a builder could edit the deploy script and the round verbs. They sit
    in INTERIM_DENY and stamp engine until the A3 hold deploys."""
    for script in ("scripts/deploy", "scripts/round"):
        assert script in lanes.INTERIM_DENY, script
        assert lanes.engine_hits([script]) == [script], script


def test_human_only_is_not_in_engine() -> None:
    assert not set(lanes.HUMAN_ONLY_PATHS) & set(lanes.ENGINE_PATHS)
    for glob in lanes.HUMAN_ONLY_PATHS:
        assert lanes.engine_hits([glob]) == [], glob
    # Real files a human-only glob covers never read as engine — including
    # the two under deploy/roles/monitoring/**, which stays an engine glob.
    for path in (
        "deploy/roles/monitoring/defaults/main.yml",
        "deploy/roles/monitoring/templates/precis-alertmanager-receiver.service.j2",
        "deploy/roles/hardening/templates/sshd_config.j2",
        "scripts/hooks/pre-write.sh",
        ".claude/settings.json",
        "deploy/inventory/hosts.yml",
        "deploy/run-migrate-refs.yml",
        "CLAUDE.md",
        "AGENTS.md",
        # the other harnesses' rule trees, MCP wiring and nested rule files
        ".codex/config.toml",
        ".devin/hooks.v1.json",
        ".kimi-code/AGENTS.md",
        ".mcp.json",
        "docs/guide/CLAUDE.md",
        "sub/deep/AGENTS.md",
        # group/host vars anywhere under deploy/, symlink hops included
        "deploy/playbooks/group_vars/all.yml",
        "deploy/host_vars/node.yml",
        "deploy/roles/postgres/templates/pg_hba.conf.j2",
        "deploy/roles/pgbouncer/templates/pgbouncer.ini.j2",
        "deploy/roles/users/tasks/main.yml",
        "deploy/roles/tcc_profile/templates/x.mobileconfig.j2",
        "deploy/roles/asa_bot/templates/claude_settings.json.j2",
    ):
        assert lanes.engine_hits([path]) == [], path


def test_engine_paths_is_factory_and_interim_minus_human_only() -> None:
    """INTERIM entries outside FACTORY (``scripts/impacted-tests`` …) must
    still stamp a land engine, so the stamp set is the deduped union."""
    assert (
        tuple(
            dict.fromkeys(
                g
                for g in lanes.FACTORY_DENY_WRITE_PATHS + lanes.INTERIM_DENY
                if g not in lanes.HUMAN_ONLY_PATHS
            )
        )
        == lanes.ENGINE_PATHS
    )
    # monitoring/** stays engine apart from its two credential-placing files.
    assert lanes.engine_hits(
        ["deploy/roles/monitoring/templates/alert_rules.yml.j2"]
    ) == ["deploy/roles/monitoring/templates/alert_rules.yml.j2"]


def test_engine_hits_folds_case_too() -> None:
    """A case-mangled write is the same file on the slice hosts'
    filesystems: the engine stamp folds case like the human-only match
    does (td574960) — and human-only still wins, folded as well."""
    assert lanes.engine_hits(["Scripts/Ship", "SRC/Precis/Plans/X.py"]) == [
        "SRC/Precis/Plans/X.py",
        "Scripts/Ship",
    ]
    assert lanes.engine_hits(
        ["Deploy/Roles/Monitoring/Templates/Alert_Rules.yml.j2"]
    ) == ["Deploy/Roles/Monitoring/Templates/Alert_Rules.yml.j2"]
    assert lanes.engine_hits(["DEPLOY/Roles/Monitoring/Defaults/Main.yml"]) == []


def test_derive_lanes_carries_the_resolved_deny_paths() -> None:
    (lane,) = lanes.derive_lanes([_account()], deny_paths=("only/this/**",))
    assert lane.deny_write_paths == ("only/this/**",)


def test_every_deploy_file_rendering_a_credential_variable_is_human_only() -> None:
    missing: dict[str, list[str]] = {}
    hits = 0
    for rel in _deploy_files():
        try:
            text = read_text(_REPO_ROOT / rel)
        except UnicodeDecodeError:
            continue
        matched = [m for m in (_VAULT_RE, _JINJA_CRED_RE) if m.search(text)]
        if not matched:
            continue
        hits += 1
        if rel in _DEPLOY_SCAN_ALLOWLIST and (
            _VAULT_RE.search(text) is None
            and _cred_vars(text) <= _DEPLOY_SCAN_ALLOWLIST[rel]
        ):
            continue
        if not any(fnmatch.fnmatchcase(rel, g) for g in lanes.HUMAN_ONLY_PATHS):
            missing[rel] = [m.pattern for m in matched]
    assert hits > 20, f"the deploy scan found only {hits} files — not a verdict"
    assert not missing, (
        "deploy files that place a credential but are not human-only — "
        "add them to HUMAN_ONLY_PATHS, or allow-list one in this test "
        f"with a one-line reason: {sorted(missing)}"
    )
    stale = [
        rel
        for rel in _DEPLOY_SCAN_ALLOWLIST
        if not (_REPO_ROOT / rel).is_file()
        or not _JINJA_CRED_RE.search(read_text(_REPO_ROOT / rel))
    ]
    assert not stale, f"allow-listed deploy files no longer needed: {stale}"


def test_nested_claude_dirs_and_claude_local_are_human_only() -> None:
    """A ``.claude`` tree nested at any depth and ``CLAUDE.local.md``
    anywhere are human-only (td574960): a slice may not drop harness
    rules under a subdirectory."""
    for path in (
        "vendor/pkg/.claude/settings.json",
        "a/b/c/.claude/hooks/pre-write.sh",
        "CLAUDE.local.md",
        "docs/guide/CLAUDE.local.md",
    ):
        assert lanes.human_only_hits([path]) == [path], path
        assert lanes.engine_hits([path]) == [], path


def test_human_only_matching_is_case_insensitive() -> None:
    """The slice hosts' filesystems fold case (macOS): a case-mangled
    write lands on the human-only file, so the match folds case too."""
    for path in (
        "Deploy/Inventory/Hosts.yml",
        "CLAUDE.MD",
        "claude.local.MD",
        ".CLAUDE/settings.json",
        "Sub/.Claude/X.json",
        "Docs/Guide/CLAUDE.MD",
    ):
        assert lanes.human_only_hits([path]) == [path], path
