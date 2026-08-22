"""Regression for gripe 171107 — the reviewer's MCP-config resolution.

On the spark review node (Phase-2 slice 2) ``PRECIS_AGENT_CONTAINER=1`` but
``PRECIS_MCP_CONFIG`` is deliberately UNSET (it would un-gate the in-proc
``claude_inproc`` passes). Before the fix, ``_mcp_config_path()`` returned
``None`` there → the containerized reviewer advertised no tools (``mcp_config
is None`` ⇒ no ``--mcp-config`` for the containerize seam to rebase) → the
"MCP tools not available" snapshot digests. It must instead fall back to the
image's baked container-internal config so the review can drill in / file
gripes.
"""

from __future__ import annotations

from pathlib import Path

from precis.workers.executors import agent_container as _ac
from precis.workers.review import _mcp_config_path


def test_host_path_when_set_and_exists(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PRECIS_MCP_CONFIG", str(cfg))
    monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)
    assert _mcp_config_path() == cfg


def test_none_when_host_path_set_but_missing(monkeypatch, tmp_path) -> None:
    """A set-but-nonexistent host path stays ``None`` — the pre-fix contract for
    the in-proc host path is unchanged (existence gate still applies)."""
    monkeypatch.setenv("PRECIS_MCP_CONFIG", str(tmp_path / "nope.json"))
    monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)
    assert _mcp_config_path() is None


def test_baked_container_path_when_unset_and_container_capable(monkeypatch) -> None:
    """The fix: unset ``PRECIS_MCP_CONFIG`` + opted-in + the container path
    verified capable ⇒ the baked container-internal path (NO host ``.exists()``
    check — it lives only inside the image), which the containerize seam
    rebases the review's ``--mcp-config`` onto."""
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)
    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setattr(_ac, "container_capability_ok", lambda *a, **k: True)
    assert _mcp_config_path() == Path(_ac.default_agent_mcp_config())


def test_none_when_opted_in_but_container_incapable(monkeypatch) -> None:
    """Load-bearing: opted in but the container can't launch (runtime down /
    image absent / health-latched) ⇒ ``call_claude_agent`` runs the review
    in-process, where the container-internal path does NOT exist. Resolve to
    ``None`` so that stays a tool-less in-proc run, not a "MCP config file not
    found" hard failure + 5h backoff."""
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)
    monkeypatch.setenv("PRECIS_AGENT_CONTAINER", "1")
    monkeypatch.setattr(_ac, "container_capability_ok", lambda *a, **k: False)
    assert _mcp_config_path() is None


def test_none_when_unset_and_not_container(monkeypatch) -> None:
    """Off a container host (the historical default) unset stays tool-less."""
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)
    monkeypatch.delenv("PRECIS_AGENT_CONTAINER", raising=False)
    assert _mcp_config_path() is None
