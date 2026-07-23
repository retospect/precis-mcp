"""load_slack_tokens's env -> vault -> file chain, mirroring
asa_bot.config.load_discord_token."""

from __future__ import annotations

import pytest

from asa_slack.config import SlackConfig, load_slack_tokens


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
            "ASA_SLACK_BOT_TOKEN": "xoxb-from-vault",
            "ASA_SLACK_APP_TOKEN": "xapp-from-vault",
        }.get(name),
    )
    bot_token, app_token = load_slack_tokens(SlackConfig())
    assert bot_token == "xoxb-from-vault"
    assert app_token == "xapp-from-vault"


def test_falls_back_to_file_when_no_env_or_vault(tmp_path, monkeypatch):
    monkeypatch.delenv("ASA_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ASA_SLACK_APP_TOKEN", raising=False)
    monkeypatch.setattr("asa_slack.config.reveal_secret", lambda name, **kw: None)
    bot_file = tmp_path / "bot-token"
    app_file = tmp_path / "app-token"
    bot_file.write_text("xoxb-from-file\n")
    app_file.write_text("xapp-from-file\n")
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
