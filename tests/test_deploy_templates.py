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
