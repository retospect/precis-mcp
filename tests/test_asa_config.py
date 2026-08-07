"""asa_bot.config.LLMConfig's default argv sources --model from the router
(gr193672-adjacent model-vocabulary-drift fix) instead of a literal baked
into this module, so it can't diverge from :func:`resolve_model`'s FRONTIER
default."""

from __future__ import annotations

from asa_bot.config import LLMConfig
from precis.utils.llm.router import Tier, resolve_model


def test_default_command_model_matches_router_frontier() -> None:
    cfg = LLMConfig()
    assert "--model" in cfg.command
    idx = cfg.command.index("--model")
    assert cfg.command[idx + 1] == resolve_model(Tier.FRONTIER)


def test_default_command_model_is_late_bound(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A PRECIS_MODEL_OPUS override set before construction is reflected —
    proof the default is resolved at instance-construction time, not a
    module-load-time literal."""
    monkeypatch.setenv("PRECIS_MODEL_OPUS", "claude-opus-override-test")
    cfg = LLMConfig()
    idx = cfg.command.index("--model")
    assert cfg.command[idx + 1] == "claude-opus-override-test"
