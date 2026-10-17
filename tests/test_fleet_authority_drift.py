"""Generated fleet authority files must match .claude/fleet/authority.md."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "fleet_authority", REPO / "scripts" / "lib" / "fleet_authority.py"
)
assert _spec is not None and _spec.loader is not None
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)


def test_generated_files_equal_fresh_render() -> None:
    fresh = fa.render(fa.load())
    assert len(fresh) == 8
    for name, text in fresh.items():
        have = (fa.GENERATED / name).read_text(encoding="utf-8")
        assert have == text, f"{name} is stale: run scripts/fleet-authority render"


def test_every_key_the_generator_reads_exists() -> None:
    data = fa.load()
    for k in fa.TOP_KEYS:
        assert k in data, k
    for tier in fa.TIERS:
        resolved = fa.resolve(data, tier)
        for k in ("mcp_endpoint", "db_role", "deny_exec"):
            assert k in resolved, (tier, k)
        assert set(data["tiers"][tier]) <= set(fa.TIER_KEYS), tier
    for k in ("allow_mcp", "allow_exec", "spend_cap_usd"):
        assert k in data["tiers"]["W"], k


def test_sample_deny_in_every_tier() -> None:
    for tier in fa.TIERS:
        claude = json.loads(
            (fa.GENERATED / f"claude-settings-{tier}.json").read_text(encoding="utf-8")
        )
        devin = json.loads(
            (fa.GENERATED / f"devin-config-{tier}.json").read_text(encoding="utf-8")
        )
        assert "Bash(scripts/release:*)" in claude["permissions"]["deny"], tier
        assert "Exec(scripts/release)" in devin["permissions"]["deny"], tier
