"""gr568270: Devin configuration pasted on /secrets reaches the executor."""

from unittest.mock import MagicMock

import pytest

from precis import secrets
from precis.secret_status import KNOWN_SECRETS
from precis.store import Store
from precis.workers.executors import devin_api


@pytest.mark.parametrize(
    ("env", "setting", "vault", "expected"),
    [
        (" org-env ", "org-setting", "org-vault", "org-env"),
        (" ", " org-setting ", "org-vault", "org-setting"),
        (None, None, " org-vault ", "org-vault"),
        (None, " ", "org-vault", "org-vault"),
        (None, None, None, None),
        (None, None, " ", None),
    ],
)
def test_org_resolution_precedence(
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    setting: str | None,
    vault: str | None,
    expected: str | None,
) -> None:
    monkeypatch.delenv("PRECIS_DEVIN_ORG_ID", raising=False)
    if env is not None:
        monkeypatch.setenv("PRECIS_DEVIN_ORG_ID", env)
    store = MagicMock(spec=Store)
    store.get_setting.return_value = setting
    get_secret = MagicMock(return_value=vault)
    monkeypatch.setattr(secrets, "get_secret", get_secret)

    assert devin_api._resolve_org(store) == expected
    if env and env.strip():
        store.get_setting.assert_not_called()
    if (env and env.strip()) or (setting and setting.strip()):
        get_secret.assert_not_called()
    else:
        get_secret.assert_called_once_with("PRECIS_DEVIN_ORG_ID")


def test_org_vault_fallback_when_setting_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECIS_DEVIN_ORG_ID", raising=False)
    store = MagicMock(spec=Store)
    store.get_setting.side_effect = RuntimeError("setting unavailable")
    monkeypatch.setattr(secrets, "get_secret", MagicMock(return_value="org-vault"))
    assert devin_api._resolve_org(store) == "org-vault"


def test_org_resolves_through_vault_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DEVIN_ORG_ID", raising=False)
    store = MagicMock(spec=Store)
    store.get_setting.return_value = None
    monkeypatch.setattr(secrets, "_STORE", store)
    reveal = MagicMock(return_value="org-vault")
    monkeypatch.setattr(secrets, "_cached_reveal", reveal)

    assert devin_api._resolve_org(store) == "org-vault"
    reveal.assert_called_once_with(store, "PRECIS_DEVIN_ORG_ID")


def test_missing_config_explains_both_secrets(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("PRECIS_DEVIN_ORG_ID", raising=False)
    store = MagicMock(spec=Store)
    store.get_setting.return_value = None
    monkeypatch.setattr(secrets, "get_secret", MagicMock(return_value=None))

    result = devin_api.run_devin_api_pass(store, enabled=True)

    assert result["claimed"] == 0
    assert "DEVIN_API_TOKEN" in caplog.text
    assert "PRECIS_DEVIN_ORG_ID" in caplog.text
    assert "/secrets" in caplog.text
    assert "Settings > Devin API" in caplog.text
    store.pool.connection.assert_not_called()


def test_org_id_has_optional_presence_only_spec() -> None:
    spec = next(s for s in KNOWN_SECRETS if s.name == "PRECIS_DEVIN_ORG_ID")
    assert spec.optional
    assert spec.cost == "free"
    assert spec.probe_group is None
    assert spec.purpose == "Devin organization id, non-secret config"
    assert spec.get_url == "https://app.devin.ai/settings/api"
    assert "Settings > Devin API" in spec.get_blurb
