"""The startup identity self-check — informational by default, only a hard
gate when the operator explicitly pins an expected bot user id.

No pytest-asyncio in this repo's test deps — drive the coroutines with a
plain ``asyncio.run`` rather than adding one.
"""

from __future__ import annotations

import asyncio

import pytest

from asa_slack.identity import IdentityMismatch, check_identity


class _FakeClient:
    def __init__(self, resp: dict) -> None:
        self._resp = resp

    async def auth_test(self) -> dict:
        return self._resp


def test_logs_and_returns_bot_user_id_with_no_expectation():
    client = _FakeClient({"user_id": "U123", "user": "asa", "team": "workshop"})
    bot_user_id = asyncio.run(check_identity(client))
    assert bot_user_id == "U123"


def test_admin_assigned_name_is_not_an_error():
    # An admin may register the app under a name that isn't "asa" — that's
    # not a mismatch on its own; with no expected_bot_user_id configured,
    # any resolved identity is accepted.
    client = _FakeClient({"user_id": "U999", "user": "ada", "team": "workshop"})
    bot_user_id = asyncio.run(check_identity(client))
    assert bot_user_id == "U999"


def test_matching_expectation_passes():
    client = _FakeClient({"user_id": "U123", "user": "asa", "team": "workshop"})
    bot_user_id = asyncio.run(check_identity(client, expected_bot_user_id="U123"))
    assert bot_user_id == "U123"


def test_mismatched_expectation_raises():
    client = _FakeClient({"user_id": "U999", "user": "ada", "team": "workshop"})
    with pytest.raises(IdentityMismatch):
        asyncio.run(check_identity(client, expected_bot_user_id="U123"))
