"""Regression for the 2026-08-02 incident (gr197478): commit 23ff8cf8 dropped
the inline DB password from ``deploy/roles/asa_bot/templates/claude_mcp.json.j2``
without pinning ``PGPASSFILE`` in the same env block, so the rendered config
carried a passwordless ``PRECIS_DATABASE_URL`` with no compensating auth
channel — ``precis serve`` couldn't authenticate, no ``mcp__precis__*`` tool
ever registered, and 21 consecutive review passes over ~4.6 days went
undetected (the tool-starvation guard in ``src/precis/workers/review.py``
now catches the *symptom*; this test catches the *template regression*
directly, before it ships).
"""

from __future__ import annotations

from pathlib import Path

import jinja2

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = (
    _REPO_ROOT / "deploy" / "roles" / "asa_bot" / "templates" / "claude_mcp.json.j2"
)

#: Minimal vars the template needs to render. ``hostvars``/``postgres_host``
#: mirror ansible's own name-then-index-by-name idiom; everything else the
#: template guards with a ``| default(...)`` filter except these two + the
#: pgbouncer port + the pgpass path, which are unconditionally interpolated.
_RENDER_VARS: dict[str, object] = {
    "postgres_host": "pg_primary",
    "hostvars": {"pg_primary": {"ansible_host": "203.0.113.10"}},
    "pgbouncer_port": 6432,
    "claude_mcp_pgpass_file": "/home/deploy/.pgpass",
}


def _render() -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    template = env.from_string(_TEMPLATE.read_text(encoding="utf-8"))
    return template.render(**_RENDER_VARS)


def test_template_renders_to_valid_json() -> None:
    """Sanity: the jinja source (minus its ``{# #}`` comments) is valid JSON —
    a template rendering to broken JSON would break ``precis serve`` startup
    on every host, independent of the credential question below."""
    import json

    parsed = json.loads(_render())
    assert "precis" in parsed["mcpServers"]


def test_rendered_precis_env_has_a_compensating_auth_channel() -> None:
    """The regression this test exists for: a rendered ``PRECIS_DATABASE_URL``
    with no inline password MUST be paired with a ``PGPASSFILE`` entry (or
    carry the password inline itself) — never neither, which is exactly the
    passwordless-DSN-with-no-auth-channel shape 23ff8cf8 shipped."""
    import json

    parsed = json.loads(_render())
    env = parsed["mcpServers"]["precis"]["env"]

    dsn = env.get("PRECIS_DATABASE_URL", "")
    assert dsn, "PRECIS_DATABASE_URL must render to a non-empty DSN"
    # ``scheme://user[:password]@host[:port]/db`` — the password lives in the
    # userinfo segment (before the first ``@``), so a colon there means an
    # inline password; no ``@`` at all means no userinfo, hence no password.
    userinfo = dsn.split("://", 1)[-1].split("@", 1)[0] if "@" in dsn else ""
    dsn_has_inline_password = ":" in userinfo

    pgpassfile = env.get("PGPASSFILE", "")

    assert dsn_has_inline_password or pgpassfile, (
        "rendered precis MCP env carries a passwordless PRECIS_DATABASE_URL "
        f"({dsn!r}) with no PGPASSFILE to compensate — this is the exact "
        "shape of the 2026-08-02 incident (23ff8cf8, gr197478): precis "
        "serve cannot authenticate and its tools silently never register."
    )
    # The current template's actual choice (PGPASSFILE, not an inline
    # password) — pin it so a future edit that swaps the auth channel is a
    # deliberate, reviewed decision rather than a silent drop.
    assert not dsn_has_inline_password
    assert pgpassfile == "/home/deploy/.pgpass"


def test_rendered_pgpassfile_is_not_blank() -> None:
    """A rendered-but-empty ``PGPASSFILE`` (e.g. ``claude_mcp_pgpass_file``
    silently defaulting to ``''``) would satisfy the "key exists" half of the
    guard above while still leaving the server unauthenticated — the key
    must carry a real path."""
    import json

    parsed = json.loads(_render())
    env = parsed["mcpServers"]["precis"]["env"]
    assert env.get("PGPASSFILE", "").strip() != ""


# ── gr208726: every worker launch unit must pin PGPASSFILE on BOTH OSes ──
#
# The collapsed worker's darwin env used to rely on libpq resolving
# ``~/.pgpass`` from the home directory — but the short-lived ``precis`` CLI
# subprocesses a plan_tick session's Bash tool spawns hit transient
# home-directory-lookup failures under deploy/memory load (getpwuid via the
# directory service), surfacing as ``fe_sendauth: no password supplied``
# bursts. The env pin removes that resolution path entirely; these tests keep
# a future edit from quietly re-narrowing the pin to one OS.

_WORKER_UNIT_SOURCES = [
    _REPO_ROOT / "deploy" / "playbooks" / "20b-precis-worker-collapsed.yml",
    _REPO_ROOT / "deploy" / "playbooks" / "20c-precis-heartbeat-serving.yml",
    _REPO_ROOT / "deploy" / "playbooks" / "20d-precis-worker-drain.yml",
    _REPO_ROOT
    / "deploy"
    / "roles"
    / "precis_worker"
    / "templates"
    / "precis-worker.plist.j2",
    _REPO_ROOT
    / "deploy"
    / "roles"
    / "precis_worker"
    / "templates"
    / "precis-worker.service.j2",
    _REPO_ROOT
    / "deploy"
    / "roles"
    / "precis_worker_agent"
    / "templates"
    / "precis-worker-agent.plist.j2",
    _REPO_ROOT
    / "deploy"
    / "roles"
    / "precis_worker_agent"
    / "templates"
    / "precis-worker-agent.service.j2",
]


def test_every_worker_unit_pins_pgpassfile() -> None:
    """Each worker launch-unit source (live collapsed/heartbeat/drain
    playbooks + the split-unit rollback templates, both OSes) must mention a
    ``PGPASSFILE`` pin somewhere in its env definition."""
    for src in _WORKER_UNIT_SOURCES:
        assert "PGPASSFILE" in src.read_text(encoding="utf-8"), (
            f"{src.relative_to(_REPO_ROOT)} defines a worker launch unit with "
            "no PGPASSFILE pin — its passwordless agent_rw DSN then depends on "
            "libpq's home-directory resolution, the gr208726 failure mode."
        )


def _render_collapsed_worker_base_env(os_family: str) -> dict[str, str]:
    """Render 20b's ``_l_b_base_env`` jinja expression for one OS family,
    with ansible's ``combine`` filter stubbed and every interpolated var
    given a placeholder."""
    import yaml
    from jinja2.nativetypes import NativeEnvironment

    play_src = (
        _REPO_ROOT / "deploy" / "playbooks" / "20b-precis-worker-collapsed.yml"
    ).read_text(encoding="utf-8")
    plays = yaml.safe_load(play_src)
    expr = plays[0]["vars"]["_l_b_base_env"]

    env = NativeEnvironment(undefined=jinja2.ChainableUndefined)

    def _combine(base: dict, extra: dict) -> dict:
        return {**base, **extra}

    env.filters["combine"] = _combine
    rendered = env.from_string(expr).render(
        os_family=os_family,
        precis_shared_env={},
        precis_identity_env={},
        precis_worker_venv="/opt/precis/venv",
        precis_worker_dsn="postgresql://agent_rw@203.0.113.10:6432/precis_prod",
        precis_worker_hf_home="/var/lib/precis/hf-cache",
        precis_worker_embedder="remote",
        precis_worker_embedder_url="http://127.0.0.1:8181",
    )
    assert isinstance(rendered, dict), f"_l_b_base_env rendered to {type(rendered)}"
    return rendered


def test_collapsed_worker_env_pins_pgpassfile_on_both_oses() -> None:
    """The rendered collapsed-worker env must carry the per-OS ``PGPASSFILE``
    path — the darwin branch is the gr208726 regression (it used to be
    linux-only)."""
    darwin = _render_collapsed_worker_base_env("darwin")
    assert darwin.get("PGPASSFILE") == "/Users/deploy/.pgpass"
    linux = _render_collapsed_worker_base_env("linux")
    assert linux.get("PGPASSFILE") == "/home/deploy/.pgpass"


def _render_collapsed_worker_fix_env(*, gateway: bool, enabled: bool) -> dict[str, str]:
    """Render 20b's ``_l_b_fix_env`` (dark-factory fix-lane env) for one
    host/gate combination — same stub idiom as
    :func:`_render_collapsed_worker_base_env`."""
    import yaml
    from jinja2.nativetypes import NativeEnvironment

    play_src = (
        _REPO_ROOT / "deploy" / "playbooks" / "20b-precis-worker-collapsed.yml"
    ).read_text(encoding="utf-8")
    plays = yaml.safe_load(play_src)
    expr = plays[0]["vars"]["_l_b_fix_env"]

    env = NativeEnvironment(undefined=jinja2.ChainableUndefined)
    rendered = env.from_string(expr).render(
        inventory_hostname="host-a",
        groups={"gateway": ["host-a"] if gateway else ["host-b"]},
        precis_fix_lane_enabled=enabled,
    )
    assert isinstance(rendered, dict), f"_l_b_fix_env rendered to {type(rendered)}"
    return rendered


def test_collapsed_worker_fix_lane_env_is_gated() -> None:
    """The fix-lane env (PRECIS_FIX_WORK_DIR / PRECIS_FIX_REPO_DIR) renders
    ONLY on a gateway host with ``precis_fix_lane_enabled`` set — everywhere
    else the block must be empty, so an unarmed host neither advertises the
    ``clones_dir`` capability nor lets a soft-fallback-routed diagnose job
    half-run (docs/backlog/dark-factory-arming.md, gripe 210007)."""
    armed = _render_collapsed_worker_fix_env(gateway=True, enabled=True)
    assert armed == {
        "PRECIS_FIX_WORK_DIR": "/Users/deploy/precis-fix-work",
        "PRECIS_FIX_REPO_DIR": "/Users/deploy/precis-fix-repo",
    }
    assert _render_collapsed_worker_fix_env(gateway=True, enabled=False) == {}
    assert _render_collapsed_worker_fix_env(gateway=False, enabled=True) == {}
