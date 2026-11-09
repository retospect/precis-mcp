"""Config loading + defaults for asa-slack.

Reuses asa_bot's generic ``PrecisConfig`` / ``PreambleConfig`` dataclasses
(neither has any Discord coupling) — only the Slack-specific pieces (tokens,
router/dispatch knobs) are new here. ``asa_slack`` has no ``LLMConfig``
equivalent: it calls ``precis.utils.llm.router.route()`` in-process
instead of building its own ``claude`` argv, so there's no subprocess
command to configure.

Layered like asa_bot: YAML file ($ASA_SLACK_CONFIG or ~/.asa/slack.yaml)
then env-var overrides. Frozen dataclass — read once at boot.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml

from asa_bot.config import PreambleConfig, PrecisConfig
from asa_bot.secrets import reveal_secret


@dataclasses.dataclass(frozen=True, slots=True)
class SlackConfig:
    """Slack app credentials + behavioral knobs.

    asa resolves each token independently via env → precis DB vault →
    file, mirroring ``asa_bot.config.load_discord_token`` — see
    :func:`load_slack_tokens`.
    """

    bot_token_env: str = "ASA_SLACK_BOT_TOKEN"
    bot_token_file: str = dataclasses.field(
        default_factory=lambda: str(Path.home() / ".asa" / "slack-bot-token")
    )
    app_token_env: str = "ASA_SLACK_APP_TOKEN"
    app_token_file: str = dataclasses.field(
        default_factory=lambda: str(Path.home() / ".asa" / "slack-app-token")
    )
    #: Optional: the bot user id `auth.test` is expected to resolve to.
    #: Empty (default) = no hard check — the resolved identity is always
    #: just logged prominently at boot, whatever it is (an admin-assigned
    #: app name that isn't "asa" is not an error).
    expected_bot_user_id: str = ""
    #: Only respond in these channel ids (empty = every channel the app
    #: is invited into).
    allowed_channels: tuple[str, ...] = ()
    #: Reply only when hailed: a real ``<@asa>`` mention or a DM. Every
    #: message is still captured to the transcript either way. Off = the
    #: pre-2026-09 behavior (reply to everything).
    respond_only_when_addressed: bool = True
    max_message_chars: int = 3500


@dataclasses.dataclass(frozen=True, slots=True)
class RouterConfig:
    """Per-turn ``router.route`` knobs — kept low relative to Discord's
    (``--max-turns 100`` there), since Slack turns are semi-trusted and
    shouldn't be able to run away on tool calls or spend."""

    max_turns: int = 8
    max_usd: float = 0.75
    timeout_s: float = 300.0


@dataclasses.dataclass(frozen=True, slots=True)
class Config:
    slack: SlackConfig
    router: RouterConfig
    precis: PrecisConfig
    preamble: PreambleConfig
    #: Same ``claude_mcp.json`` the Discord bridge uses (the precis MCP
    #: server config passed to the spawned agent subprocess).
    mcp_config_path: str = dataclasses.field(
        default_factory=lambda: str(Path.home() / ".claude" / "mcp.json")
    )
    #: Same persona file as Discord (``grimoire/agents/asa.md`` at deploy).
    soul_path: str = dataclasses.field(
        default_factory=lambda: str(Path.home() / ".asa" / "SOUL.md")
    )
    #: Slack-specific norms layered after SOUL: threading discipline, the
    #: no-compute-jobs framing, per-person memory instructions.
    slack_hints_path: str = dataclasses.field(
        default_factory=lambda: str(Path.home() / ".asa" / "SLACK_HINTS.md")
    )

    @classmethod
    def load(cls, path: str | None = None) -> Config:
        candidates: list[str] = []
        if path:
            candidates.append(path)
        env_path = os.environ.get("ASA_SLACK_CONFIG")
        if env_path:
            candidates.append(env_path)
        candidates.append(str(Path.home() / ".asa" / "slack.yaml"))

        data: dict[str, Any] = {}
        for c in candidates:
            p = Path(c)
            if p.exists():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                break

        # config.yaml's DSN wins over an inherited PRECIS_DATABASE_URL: a stale
        # launchd/plist value (agent_rw) must not shadow the precis_system DSN
        # the vault's infra class requires. The env var is only a fallback for
        # a config that names none.
        db_url = os.environ.get("PRECIS_DATABASE_URL")
        if db_url and not (data.get("precis") or {}).get("database_url"):
            data.setdefault("precis", {})["database_url"] = db_url
        notify_url = os.environ.get("PRECIS_NOTIFY_DATABASE_URL")
        if notify_url:
            data.setdefault("precis", {})["notify_database_url"] = notify_url

        def _merge(cls_: type, key: str) -> Any:
            d = data.get(key, {}) or {}
            field_names = {f.name for f in dataclasses.fields(cls_)}
            kept = {k: v for k, v in d.items() if k in field_names}
            return cls_(**kept)

        top_field_names = {"mcp_config_path", "soul_path", "slack_hints_path"}
        top_kwargs = {k: v for k, v in data.items() if k in top_field_names}

        return cls(
            slack=_merge(SlackConfig, "slack"),
            router=_merge(RouterConfig, "router"),
            precis=_merge(PrecisConfig, "precis"),
            preamble=_merge(PreambleConfig, "preamble"),
            **top_kwargs,
        )


def pin_process_dsn(dsn: str | None) -> None:
    """Point this process's ``PRECIS_DATABASE_URL`` at the config.yaml DSN.

    Everything asa-slack resolves *in-process* reads the env var, not
    ``cfg.precis``: ``reveal_secret``'s fallback, the store ``warm_runtime``
    binds via ``build_runtime`` → ``load_config`` on the first turn, and the
    ``precis.utils.claude_oauth.ensure_oauth_token`` → ``get_secret`` /
    ``reveal_unbound`` lookups run for the spawned ``claude -p`` env. A stale
    plist ``agent_rw`` value must not outrank the ``precis_system`` DSN —
    ``CLAUDE_CODE_OAUTH_TOKEN`` is vault class infra (``vault._class_check``,
    migration 0285), an ``agent_rw`` reveal is denied, and the turn's
    ``claude -p`` answers "Not logged in" (gr605685; the sibling of what
    ca980886c fixed on the Discord bridge). ``adopt_process_store`` scrubs
    the var from the env subprocesses inherit, so the DSN does not leak
    onward. No-op when the config names no DSN — the env var is then the
    fallback ``Config.load`` resolved.
    """
    if dsn:
        os.environ["PRECIS_DATABASE_URL"] = dsn


def load_slack_tokens(cfg: SlackConfig, *, dsn: str | None = None) -> tuple[str, str]:
    """Resolve ``(bot_token, app_token)``, each independently: env → vault → file.

    ``dsn`` is ``cfg.precis.database_url`` (precis_system); see
    :mod:`asa_bot.secrets`.
    """
    return (
        _load_token(cfg.bot_token_env, cfg.bot_token_file, dsn=dsn),
        _load_token(cfg.app_token_env, cfg.app_token_file, dsn=dsn),
    )


def _load_token(env_name: str, file_path: str, *, dsn: str | None = None) -> str:
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val.strip()
    vault_val = reveal_secret(env_name, dsn=dsn)
    if vault_val:
        return vault_val.strip()
    p = Path(file_path)
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        f"Slack token not found at ${env_name}, the precis vault, or {file_path}"
    )
