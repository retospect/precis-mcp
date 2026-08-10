"""asa_bot.config.LLMConfig's default argv now pins ``--model local`` — the
sentinel ``claude_invoke`` maps per turn onto the router's BIG placement
chain (operator ``llm.chain.big``: local/OSS rung first, cloud fallback) —
instead of a claude id. The claude lane stays reachable via a concrete id
(`/model opus`, a config override)."""

from __future__ import annotations

from asa_bot.config import LLMConfig


def test_default_command_model_is_the_local_chain_sentinel() -> None:
    cfg = LLMConfig()
    assert "--model" in cfg.command
    idx = cfg.command.index("--model")
    assert cfg.command[idx + 1] == "local"
