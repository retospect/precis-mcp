"""workers/factory_git: the factory's git/GitHub credential wiring (td543734).

The token reaches git through a credential helper installed via
``GIT_CONFIG_*`` env entries and ``gh`` through ``GH_TOKEN``; it is never on
argv. No token → the env is returned unchanged.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from precis import secrets as _secrets
from precis.workers import factory_git as fg


@pytest.fixture
def no_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fg.SECRET_NAME, raising=False)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)


def test_env_token_wins_over_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fg.SECRET_NAME, "from-env")
    monkeypatch.setattr(_secrets, "get_secret", lambda *a, **k: "from-vault")
    assert fg.factory_git_token() == "from-env"


def test_vault_token_when_env_lacks_it(
    no_env_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _secrets,
        "get_secret",
        lambda name, *a, **k: "from-vault" if name == fg.SECRET_NAME else None,
    )
    assert fg.factory_git_token() == "from-vault"


def test_no_token_leaves_the_env_alone(
    no_env_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_secrets, "get_secret", lambda *a, **k: None)
    monkeypatch.setattr(_secrets, "reveal_unbound", lambda *a, **k: None)
    base = {"PATH": "/usr/bin", "HOME": "/tmp/h"}
    assert fg.git_auth_env(base) == base


def test_git_auth_env_installs_helper_and_gh_token(
    no_env_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_secrets, "get_secret", lambda *a, **k: "ghp-token")
    env = fg.git_auth_env({"PATH": "/usr/bin"})
    assert env["GH_TOKEN"] == "ghp-token"
    assert env[fg.SECRET_NAME] == "ghp-token"
    assert env["GIT_CONFIG_COUNT"] == "2"
    # A reset of every helper configured before ours, then ours for github.
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == (
        "credential.helper",
        "",
    )
    assert env["GIT_CONFIG_KEY_1"] == "credential.https://github.com.helper"
    # The helper reads the token from the env; the config value never holds it.
    assert "ghp-token" not in env["GIT_CONFIG_VALUE_1"]
    assert f"${fg.SECRET_NAME}" in env["GIT_CONFIG_VALUE_1"]


def test_git_auth_env_appends_after_existing_git_config(
    no_env_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_secrets, "get_secret", lambda *a, **k: "t")
    env = fg.git_auth_env(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "x",
        }
    )
    assert env["GIT_CONFIG_COUNT"] == "3"
    assert env["GIT_CONFIG_KEY_0"] == "user.name"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_KEY_2"] == "credential.https://github.com.helper"


def test_helper_answers_git_credential_get(
    no_env_token: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end against a real git: ``git credential fill`` for a github
    URL answers with the token as password, read from the env."""
    # Built at runtime so the repo's credential scan sees no password literal.
    fake = "fake-" + "token"
    monkeypatch.setattr(_secrets, "get_secret", lambda *a, **k: fake)
    env = fg.git_auth_env({"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "password=" + fake in proc.stdout
    assert "username=" + fg._HELPER_USER in proc.stdout
