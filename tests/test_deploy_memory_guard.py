"""hardening role: per-unit memory-guard drop-in + memsnap templates.

No vars -> no Memory* directive; vars set -> rendered; the guard/memsnap
tasks stay Linux-gated and the guard drop-in only reloads systemd.
"""

from __future__ import annotations

from pathlib import Path

import jinja2
import yaml

_ROLE = Path(__file__).resolve().parent.parent / "deploy" / "roles" / "hardening"


def _render(name: str, **ctx: object) -> str:
    env = jinja2.Environment(
        undefined=jinja2.ChainableUndefined, trim_blocks=True, lstrip_blocks=True
    )
    src = (_ROLE / "templates" / name).read_text(encoding="utf-8")
    return env.from_string(src).render(ansible_managed="managed", **ctx)


def _directives(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("Memory")]


def test_no_vars_renders_no_memory_lines() -> None:
    out = _render("memory-guard.conf.j2", unit_cfg={"name": "x.service"})
    assert _directives(out) == []
    out = _render(
        "memory-guard.conf.j2",
        unit_cfg={"memory_high": "", "memory_max": "", "memory_swap_max": ""},
    )
    assert _directives(out) == []


def test_vars_render_lines() -> None:
    out = _render(
        "memory-guard.conf.j2",
        unit_cfg={"memory_high": "20G", "memory_max": "24G", "memory_swap_max": "0"},
    )
    assert _directives(out) == ["MemoryHigh=20G", "MemoryMax=24G", "MemorySwapMax=0"]
    out = _render("memory-guard.conf.j2", unit_cfg={"memory_max": "24G"})
    assert _directives(out) == ["MemoryMax=24G"]


def test_defaults_are_off() -> None:
    d = yaml.safe_load((_ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
    for k in (
        "precis_worker_memory_high",
        "precis_worker_memory_max",
        "precis_worker_memory_swap_max",
    ):
        assert d[k] == ""
    assert d["host_memory_guard_units"] == []
    assert d["memsnap_enabled"] is False
    assert "precis-worker-compute" in d["precis_worker_memory_units"]


def test_memsnap_templates_render() -> None:
    sh = _render("memsnap.sh.j2", memsnap_guard_unit_names=["a.service"])
    assert "a.service" in sh and "precis-*" in sh
    assert "SyslogIdentifier=memsnap" in _render("memsnap.service.j2")
    timer = _render("memsnap.timer.j2", memsnap_interval_min=10)
    assert "OnUnitActiveSec=10min" in timer


def test_tasks_linux_gated_and_reload_only() -> None:
    tasks = yaml.safe_load((_ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    guard = [
        t
        for t in tasks
        if "memory-guard" in t.get("name", "") or "memsnap" in t.get("name", "")
    ]
    assert guard
    for t in guard:
        when = t["when"] if isinstance(t["when"], list) else [t["when"]]
        assert 'os_family == "linux"' in when
        assert (
            t.get("notify", "reload systemd (hardening)")
            == "reload systemd (hardening)"
        )
