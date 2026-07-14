"""Config loading + defaults.

Layered:
  1. Defaults below
  2. YAML file at $ASA_CONFIG (or ~/.asa/config.yaml)
  3. Env-var overrides (ASA_* prefix; selected fields only)

Frozen dataclass — read once at boot, propagated explicitly.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True, slots=True)
class LLMConfig:
    """How to launch the LLM subprocess per turn."""

    command: list[str] = dataclasses.field(
        default_factory=lambda: [
            "/opt/homebrew/bin/claude",
            "-p",
            "--max-turns",
            "100",
            "--model",
            "claude-opus-4-7",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
    )
    system_prompt_flag: str = "--append-system-prompt"
    mcp_config_flag: str = "--mcp-config"
    mcp_config_path: str = "/Users/hermes/.claude/mcp.json"
    cwd: str = "/Users/hermes/claudebot"
    turn_timeout_seconds: int = 300
    env: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class DiscordConfig:
    token_file: str = "/Users/hermes/.secrets/asa-discord-token"
    token_env: str = "ASA_DISCORD_TOKEN"
    # Only respond to messages in these channel ids (empty = all).
    allowed_channels: tuple[str, ...] = ()
    # Discord 2000-char hard limit. Split outputs cleanly under this.
    max_message_chars: int = 1900
    # If the model returns >= this many chars, upload as .md attachment
    # rather than spamming N message posts.
    attachment_threshold_chars: int = 6000


@dataclasses.dataclass(frozen=True, slots=True)
class PrecisConfig:
    """How asa_bot talks to precis."""

    command: list[str] = dataclasses.field(
        default_factory=lambda: ["/opt/mcps/venv/bin/precis", "serve"]
    )
    database_url: str = ""
    #: Separate DSN for the LISTEN/NOTIFY connection. LISTEN does not survive
    #: a transaction-pooled pgbouncer (the listener can't hold a server
    #: backend, so NOTIFYs are dropped), so this must point at a *direct*
    #: postgres session connection. Falls back to ``database_url`` when unset.
    notify_database_url: str = ""
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    health_check_interval_seconds: int = 30


@dataclasses.dataclass(frozen=True, slots=True)
class PreambleConfig:
    """Per-turn preamble construction budget + sources."""

    soul_path: str = "/Users/hermes/.asa/SOUL.md"
    tool_hints_path: str = "/Users/hermes/.asa/TOOL_HINTS.md"
    recent_turns: int = 5
    digest_turns: int = 20
    sticky_max_thread: int = 5
    sticky_max_global: int = 5
    expiry_warn_within_days: int = 3


@dataclasses.dataclass(frozen=True, slots=True)
class CaptureConfig:
    """HTTP shim that hooks call to write the assistant turn."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 9876
    # Fallback JSONL when the precis MCP is down at capture time.
    fallback_jsonl: str = "/Users/hermes/claudebot/capture-fallback.jsonl"


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    llm: LLMConfig
    discord: DiscordConfig
    precis: PrecisConfig
    preamble: PreambleConfig
    capture: CaptureConfig

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        candidates: list[str] = []
        if path:
            candidates.append(path)
        env_path = os.environ.get("ASA_CONFIG")
        if env_path:
            candidates.append(env_path)
        candidates.append(str(Path.home() / ".asa" / "config.yaml"))
        candidates.append("/Users/hermes/.asa/config.yaml")

        data: dict[str, Any] = {}
        for c in candidates:
            p = Path(c)
            if p.exists():
                data = yaml.safe_load(p.read_text()) or {}
                break

        # Env-var overrides for fields that change between deploys.
        discord_token_env = os.environ.get("ASA_DISCORD_TOKEN")
        if discord_token_env:
            data.setdefault("discord", {})["token_env_value"] = discord_token_env
        db_url = os.environ.get("PRECIS_DATABASE_URL")
        if db_url:
            data.setdefault("precis", {})["database_url"] = db_url
        notify_url = os.environ.get("PRECIS_NOTIFY_DATABASE_URL")
        if notify_url:
            data.setdefault("precis", {})["notify_database_url"] = notify_url

        def _merge(cls_: type, key: str) -> Any:
            d = data.get(key, {}) or {}
            field_names = {f.name for f in dataclasses.fields(cls_)}
            kept = {k: v for k, v in d.items() if k in field_names}
            return cls_(**kept)

        return cls(
            llm=_merge(LLMConfig, "llm"),
            discord=_merge(DiscordConfig, "discord"),
            precis=_merge(PrecisConfig, "precis"),
            preamble=_merge(PreambleConfig, "preamble"),
            capture=_merge(CaptureConfig, "capture"),
        )


def load_discord_token(cfg: DiscordConfig) -> str:
    """Resolve the Discord bot token: env → precis DB vault → file.

    Vault reveal (precis ADR 0055) is best-effort — it returns None when the
    vault is unavailable, so the file path stays the fallback.
    """
    env_val = os.environ.get(cfg.token_env)
    if env_val:
        return env_val.strip()
    from asa_bot.secrets import reveal_secret

    vault_val = reveal_secret(cfg.token_env)
    if vault_val:
        return vault_val.strip()
    p = Path(cfg.token_file)
    if p.exists():
        return p.read_text().strip()
    raise RuntimeError(
        f"Discord token not found at ${cfg.token_env}, the precis vault, "
        f"or {cfg.token_file}"
    )
