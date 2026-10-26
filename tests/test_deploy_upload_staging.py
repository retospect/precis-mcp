"""PRECIS_UPLOAD_STAGING delivery route (gr517423 MF1 re-review).

The staged-upload dir was wired only into the precis_web plist's
explicit key — but ``paper_pull`` (the staged-attach ``os.replace``)
and the MCP mint/attach doors (``handlers/paper.py`` —
``_mint_pdf_upload_link`` / ``_queue_pdf_upload``) read the var live in
worker and agent/MCP units too, and would have refused every upload
after deploy. The fix routes it through ``precis_identity_env_raw`` in
``deploy/group_vars/all.yml`` — the same dict
``PRECIS_INBOUND_CHASE_ENABLED`` rides — so every consumer that merges
``precis_shared_env | combine(precis_identity_env)`` picks it up
automatically.

Pinned here: the entry exists and derives a SIBLING of the inbox even
when ``papers_inbox_path`` carries a trailing slash (``dirname`` of
``"/x/inbox/"`` is ``"/x/inbox"`` — without the strip the staging dir
nests INSIDE the watched tree, the exact hazard the var exists to
remove), and every deploy source that merges ``precis_shared_env``
into a unit env also merges ``precis_identity_env`` — the only
delivery route the var has.
"""

from __future__ import annotations

import plistlib
import posixpath
import re
from pathlib import Path
from typing import Any

import jinja2
import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"
_ALL_YML = _DEPLOY / "group_vars" / "all.yml"
_WEB_DEFAULTS = _DEPLOY / "roles" / "precis_web" / "defaults" / "main.yml"
_WEB_PLIST = _DEPLOY / "roles" / "precis_web" / "templates" / "precis-web.plist.j2"


def _staging_exprs() -> dict[str, str]:
    """The staging derivation lives ONLY in the shared identity-env
    entry — the web plist used to carry its own ``precis_web_upload_staging``
    key, which the loop's copy then shadowed (last wins); it is gone so
    no role-level knob can silently diverge web from workers."""
    all_vars = yaml.safe_load(_ALL_YML.read_text(encoding="utf-8"))
    web = yaml.safe_load(_WEB_DEFAULTS.read_text(encoding="utf-8"))
    assert "precis_web_upload_staging" not in web
    return {
        "precis_identity_env_raw": str(
            all_vars["precis_identity_env_raw"]["PRECIS_UPLOAD_STAGING"]
        ),
    }


def _render_expr(expr: str, inbox: str) -> str:
    """Render one ansible-style derivation with the two filters it uses
    stubbed by their semantics (ansible's ``dirname`` is
    ``posixpath.dirname``; ``regex_replace`` is ``re.sub``)."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["regex_replace"] = lambda s, pattern, repl: re.sub(pattern, repl, s)
    env.filters["dirname"] = posixpath.dirname
    return env.from_string(expr).render(papers_inbox_path=inbox)


def test_upload_staging_rides_identity_env() -> None:
    """The var must be in ``precis_identity_env_raw`` — that dict is
    what every unit's ``combine`` loop merges; anywhere else (plist key,
    playbook literal) reaches only that one consumer."""
    exprs = _staging_exprs()
    assert "regex_replace('/+$'" in exprs["precis_identity_env_raw"]


@pytest.mark.parametrize("inbox", ["/srv/papers/inbox", "/srv/papers/inbox/"])
def test_staging_default_is_a_sibling_of_the_inbox(inbox: str) -> None:
    """Both declarations must derive ``<inbox-parent>/precis-upload-staging``
    — the trailing-slash case is the regression: ``dirname("/x/inbox/")``
    is ``"/x/inbox"``, nesting staging INSIDE the watched tree."""
    for name, expr in _staging_exprs().items():
        assert _render_expr(expr, inbox) == "/srv/papers/precis-upload-staging", (
            f"{name} derived {_render_expr(expr, inbox)!r} from {inbox!r}"
        )


def test_every_shared_env_consumer_merges_identity_env() -> None:
    """Every deploy source that merges ``precis_shared_env`` into a
    unit env must merge ``precis_identity_env`` too — otherwise
    PRECIS_UPLOAD_STAGING (and INBOUND_CHASE, and any future entry)
    silently never reaches that consumer. Comment-only mentions
    (examples, prose) are excluded — only live lines count."""
    missing: list[str] = []
    for path in sorted(_DEPLOY.rglob("*")):
        if not path.is_file() or path.suffix not in {".yml", ".j2"}:
            continue
        live = [
            line.split("{#", 1)[0]
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        joined = "\n".join(live)
        if (
            "precis_shared_env" in joined
            and "combine(" in joined
            and "precis_identity_env" not in joined
        ):
            missing.append(str(path.relative_to(_REPO)))
    assert missing == [], (
        "deploy source(s) merge precis_shared_env but not "
        f"precis_identity_env — env vars routed through "
        f"precis_identity_env_raw never reach them: {missing}"
    )


def _render_web_plist(**extra: Any) -> str:
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.filters["combine"] = lambda a, b: {**(a or {}), **(b or {})}
    render_vars: dict[str, Any] = {
        "precis_shared_env": {},
        "precis_identity_env": {},
        "precis_web_venv": "/opt/mcps/venv",
        "precis_web_dsn": "postgresql://agent_rw@db:6432/precis_prod",
        "precis_web_corpus": "/srv/corpus",
        "precis_web_watch_inbox": "/srv/papers/inbox",
        "precis_web_podcast_dir": "/srv/podcast",
        "precis_web_podcast_base_url": "https://gw.example",
        "precis_capabilities": {},
        "inventory_hostname": "gw",
        **extra,
    }
    return env.from_string(_WEB_PLIST.read_text(encoding="utf-8")).render(**render_vars)


def test_web_plist_env_carries_upload_staging_once() -> None:
    """The web unit gets the var ONLY from the identity-env merge —
    exactly one ``<key>PRECIS_UPLOAD_STAGING</key>`` in the rendered
    plist (a second explicit key would both duplicate the entry and be
    shadowed by the loop's copy, which is emitted last)."""
    out = _render_web_plist(
        precis_identity_env={
            "PRECIS_UPLOAD_STAGING": "/srv/papers/precis-upload-staging"
        }
    )
    assert out.count("<key>PRECIS_UPLOAD_STAGING</key>") == 1
    env = plistlib.loads(out.encode())["EnvironmentVariables"]
    assert env["PRECIS_UPLOAD_STAGING"] == "/srv/papers/precis-upload-staging"
