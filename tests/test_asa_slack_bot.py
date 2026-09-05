"""asa_slack.bot — the dispatch seam.

Only `_dispatch_warm` for now: the runtime warm-up MUST precede `route`,
else `live_config.chain_override` reads dark in the storeless asa-slack
process and every turn falls back to the default (claude) chain.
"""

from __future__ import annotations

import pytest

bot = pytest.importorskip("asa_slack.bot")


def test_dispatch_warm_binds_runtime_before_dispatch(monkeypatch):
    calls: list[str] = []
    sentinel_req = object()
    sentinel_result = object()

    def fake_warm() -> None:
        calls.append("warm")

    def fake_dispatch(req):
        assert req is sentinel_req
        calls.append("dispatch")
        return sentinel_result

    monkeypatch.setattr(bot, "warm_runtime", fake_warm)
    monkeypatch.setattr(bot, "route", fake_dispatch)

    assert bot._dispatch_warm(sentinel_req) is sentinel_result
    assert calls == ["warm", "dispatch"]


def test_addressed_default_on():
    from asa_slack.config import SlackConfig

    cfg = SlackConfig()
    assert cfg.respond_only_when_addressed is True
    assert cfg.address_names == ("asa",)


def _addressed(text: str, **overrides) -> bool:
    kwargs = dict(
        text=text,
        own_user_id="U0ASA",
        address_names=("asa",),
        is_dm=False,
        parent_user_id=None,
        thread_engaged=False,
    )
    kwargs.update(overrides)
    return bot._is_addressed(**kwargs)


def test_addressed_by_mention():
    assert _addressed("hey <@U0ASA> what do you think?")
    assert not _addressed("hey <@U0ROCKY> what do you think?")


def test_addressed_by_name_word_boundary():
    assert _addressed("Asa, log that for me")
    assert _addressed("thanks asa!")
    assert not _addressed("the casa is nice")
    assert not _addressed("NASA launched today")


def test_addressed_in_dm_and_engaged_thread():
    assert _addressed("anything at all", is_dm=True)
    assert _addressed("follow-up without a mention", thread_engaged=True)


def test_addressed_reply_on_asas_own_thread_root():
    assert _addressed("re: your note", parent_user_id="U0ASA")
    assert not _addressed("re: rocky's note", parent_user_id="U0ROCKY")


def test_not_addressed_plain_channel_chatter():
    assert not _addressed("morning everyone")


class _FakeApp:
    """Just enough AsyncApp: accepts the message handler registration."""

    def event(self, _name):
        def register(fn):
            return fn

        return register


class _FakeClient:
    def __init__(self):
        self.posted: list[dict] = []
        self.updated: list[dict] = []

    async def users_info(self, user):
        return {"user": {"real_name": "Rocky", "is_bot": False}}

    async def conversations_info(self, channel):
        return {"channel": {"name": "rockyandfriends"}}

    async def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ts": f"ph-{len(self.posted)}"}

    async def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}


def _make_bot(monkeypatch):
    from asa_slack.config import Config

    cfg = Config.load(path="/nonexistent-asa-slack-config.yaml")
    instance = bot.AsaSlack(cfg=cfg, precis=object(), app=_FakeApp())
    instance._own_user_id = "U0ASA"
    captures: list[dict] = []

    async def fake_capture(**kw):
        captures.append(kw)

    monkeypatch.setattr(instance, "_capture", fake_capture)

    async def fake_prompt(**kw):
        return "system"

    monkeypatch.setattr(instance, "_build_system_prompt", fake_prompt)
    result = bot.LlmResult(
        text="hi!", cost_usd=None, turns_used=None, model="test", tier=bot.Tier.BIG
    )
    monkeypatch.setattr(bot, "_dispatch_warm", lambda req: result)
    return instance, captures


def _event(text, **extra):
    ev = {"channel": "C123", "ts": "1.0", "text": text, "user": "U1ROCKY"}
    ev.update(extra)
    return ev


def test_gate_unaddressed_captures_but_stays_quiet(monkeypatch):
    import asyncio

    instance, captures = _make_bot(monkeypatch)
    client = _FakeClient()
    asyncio.run(instance._handle_message(_event("morning everyone"), client))
    assert len(captures) == 1  # observed turn still lands in the transcript
    assert client.posted == []  # no placeholder, no reply


def test_gate_addressed_replies_and_engages_thread(monkeypatch):
    import asyncio

    instance, captures = _make_bot(monkeypatch)
    client = _FakeClient()
    asyncio.run(instance._handle_message(_event("asa, what do you think?"), client))
    assert len(client.posted) == 1  # placeholder
    assert client.updated and client.updated[0]["text"] == "hi!"
    assert len(captures) == 2  # observed turn + asa's reply
    # Follow-up in the same thread needs no re-mention.
    asyncio.run(
        instance._handle_message(
            _event("and a follow-up", ts="2.0", thread_ts="1.0"), client
        )
    )
    assert len(client.posted) == 2


def test_gate_off_restores_reply_to_everything(monkeypatch):
    import asyncio
    import dataclasses

    instance, _captures = _make_bot(monkeypatch)
    slack_cfg = dataclasses.replace(
        instance._cfg.slack, respond_only_when_addressed=False
    )
    instance._cfg = dataclasses.replace(instance._cfg, slack=slack_cfg)
    client = _FakeClient()
    asyncio.run(instance._handle_message(_event("morning everyone"), client))
    assert len(client.posted) == 1
