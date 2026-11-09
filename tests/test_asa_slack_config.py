"""load_slack_tokens's env -> vault -> file chain, mirroring
asa_bot.config.load_discord_token."""

from __future__ import annotations

import os

import pytest

from asa_slack.config import SlackConfig, load_slack_tokens, pin_process_dsn


def test_env_wins(monkeypatch):
    monkeypatch.setenv("ASA_SLACK_BOT_TOKEN", "xoxb-from-env")
    monkeypatch.setenv("ASA_SLACK_APP_TOKEN", "xapp-from-env")
    bot_token, app_token = load_slack_tokens(SlackConfig())
    assert bot_token == "xoxb-from-env"
    assert app_token == "xapp-from-env"


def test_falls_back_to_vault_when_no_env(monkeypatch):
    monkeypatch.delenv("ASA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ASA_SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr(
        "asa_slack.config.reveal_secret",
        lambda name, **kw: {
            "ASA_SLACK_BOT_TOKEN": ("xo" + "xb-from-vault"),
            "ASA_SLACK_APP_TOKEN": "xapp-from-vault",
        }.get(name),
    )
    bot_token, app_token = load_slack_tokens(SlackConfig())
    assert bot_token == ("xo" + "xb-from-vault")
    assert app_token == "xapp-from-vault"


def test_falls_back_to_file_when_no_env_or_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("ASA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ASA_SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr("asa_slack.config.reveal_secret", lambda name, **kw: None)
    bot_file = tmp_path / "bot-token"
    app_file = tmp_path / "app-token"
    bot_file.write_text("xoxb-from-file\n", encoding="utf-8")
    app_file.write_text("xapp-from-file\n", encoding="utf-8")
    cfg = SlackConfig(bot_token_file=str(bot_file), app_token_file=str(app_file))
    bot_token, app_token = load_slack_tokens(cfg)
    assert bot_token == "xoxb-from-file"
    assert app_token == "xapp-from-file"


def test_raises_when_token_unresolvable(tmp_path, monkeypatch):
    monkeypatch.delenv("ASA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ASA_SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr("asa_slack.config.reveal_secret", lambda name, **kw: None)
    cfg = SlackConfig(
        bot_token_file=str(tmp_path / "missing-bot"),
        app_token_file=str(tmp_path / "missing-app"),
    )
    with pytest.raises(RuntimeError):
        load_slack_tokens(cfg)


# ── DSN precedence: config.yaml's precis_system beats a stale env agent_rw ───
# Regression for gr605685: the sibling of ca980886c's Discord fix — the vault
# infra class (migration 0285) denies agent_rw, so every reveal must run over
# the config DSN, never an inherited PRECIS_DATABASE_URL.

_CONFIG_DSN = "postgresql://precis_system@db:6432/precis_prod"
_STALE_ENV_DSN = "postgresql://agent_rw@db:6432/precis_prod"


def test_load_slack_tokens_threads_dsn_into_reveal_secret(monkeypatch):
    monkeypatch.delenv("ASA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ASA_SLACK_APP_TOKEN", raising=False)
    seen: list[object] = []

    def _reveal(name: str, **kw: object) -> str:
        seen.append(kw.get("dsn"))
        return "tok"

    monkeypatch.setattr("asa_slack.config.reveal_secret", _reveal)
    load_slack_tokens(SlackConfig(), dsn=_CONFIG_DSN)
    assert seen == [_CONFIG_DSN, _CONFIG_DSN]


def test_config_load_yaml_dsn_beats_stale_env(tmp_path, monkeypatch):
    from asa_slack.config import Config

    cfg_file = tmp_path / "slack.yaml"
    cfg_file.write_text(f'precis:\n  database_url: "{_CONFIG_DSN}"\n', encoding="utf-8")
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    assert Config.load(str(cfg_file)).precis.database_url == _CONFIG_DSN


def test_config_load_env_dsn_is_the_fallback(tmp_path, monkeypatch):
    from asa_slack.config import Config

    cfg_file = tmp_path / "slack.yaml"
    cfg_file.write_text("precis: {}\n", encoding="utf-8")
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    assert Config.load(str(cfg_file)).precis.database_url == _STALE_ENV_DSN


def test_pin_process_dsn_overwrites_stale_env(monkeypatch):
    """The in-process runtime + OAuth reveal resolve over the config DSN."""
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    pin_process_dsn(_CONFIG_DSN)
    assert os.environ["PRECIS_DATABASE_URL"] == _CONFIG_DSN


def test_pin_process_dsn_noops_when_config_names_none(monkeypatch):
    """An empty config DSN leaves the env fallback untouched."""
    monkeypatch.setenv("PRECIS_DATABASE_URL", _STALE_ENV_DSN)
    pin_process_dsn("")
    pin_process_dsn(None)
    assert os.environ["PRECIS_DATABASE_URL"] == _STALE_ENV_DSN
