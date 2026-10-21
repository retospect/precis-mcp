"""Tests for ``precis llm op set/list`` (:mod:`precis.cli.llm`'s op subcommands).

gr502328: ``op set`` grew ``--chain``/``--local`` (validated through
``parse_chain_rungs``) so routing an op to a local model no longer needs a
raw SQL insert, and ``op list`` renders a chain override instead of "—/—".
``op set`` merges into the existing ``llm.op.<source>`` row rather than
rewriting it wholesale — a ``--tier`` set must not silently drop a chain.

No real DB: ``budget_settings.get_setting``/``set_setting`` are faked against
a dict (mirroring ``tests/test_llm_operations.py``'s ``_bind`` idiom) and the
``live_config`` TTL cache is busted around every test.
"""

from __future__ import annotations

import json
from argparse import Namespace
from collections.abc import Generator
from typing import Any, cast

import pytest

from precis.budget import meter
from precis.budget import settings as budget_settings
from precis.cli import llm as llm_cli
from precis.utils.llm import live_config


@pytest.fixture(autouse=True)
def _clear_cache() -> Generator[None, None, None]:
    live_config.bust_cache()
    yield
    live_config.bust_cache()


class _Store:
    """Opaque sentinel — all app_settings access is stubbed, never queried."""


def _app_settings(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, str] | None = None,
) -> dict[str, str]:
    """Fake the ``app_settings`` KV for ``budget_settings``; returns the dict
    (also bound as live_config's store) so tests see the written rows."""
    store_rows: dict[str, str] = dict(rows or {})
    store = _Store()
    monkeypatch.setattr(meter, "active_store", lambda: store)
    monkeypatch.setattr(
        budget_settings, "get_setting", lambda _s, key: store_rows.get(key)
    )
    monkeypatch.setattr(
        budget_settings,
        "set_setting",
        lambda _s, key, value: store_rows.__setitem__(key, value),
    )
    monkeypatch.setattr(
        budget_settings,
        "clear_setting",
        lambda _s, key: store_rows.pop(key, None),
    )
    return store_rows


def _set_args(**kw: Any) -> Namespace:
    defaults: dict[str, Any] = {
        "source": "reading_brief",
        "tier": None,
        "model": None,
        "chain": None,
        "local": None,
    }
    defaults.update(kw)
    return Namespace(**defaults)


# ── op set: requirement + validation ────────────────────────────────────


def test_op_set_requires_a_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _app_settings(monkeypatch)
    with pytest.raises(SystemExit, match="at least one of"):
        llm_cli._cmd_op_set(cast("Any", _Store()), _set_args())


def test_op_set_refuses_bad_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _app_settings(monkeypatch)
    with pytest.raises(SystemExit, match="invalid --tier"):
        llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(tier="nonsense"))
    assert rows == {}


def test_op_set_chain_refuses_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _app_settings(monkeypatch)
    with pytest.raises(SystemExit, match="not valid JSON"):
        llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(chain="{nope"))
    assert rows == {}


def test_op_set_chain_refuses_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _app_settings(monkeypatch)
    with pytest.raises(SystemExit, match="non-empty JSON list"):
        llm_cli._cmd_op_set(
            cast("Any", _Store()), _set_args(chain='{"transport": "local"}')
        )
    assert rows == {}


def test_op_set_chain_refuses_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _app_settings(monkeypatch)
    with pytest.raises(SystemExit, match="non-empty JSON list"):
        llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(chain="[]"))
    assert rows == {}


@pytest.mark.parametrize(
    ("chain", "reason"),
    [
        # Each reason is parse_chain_rungs' (reason, index, detail) triple's
        # first two fields — the CLI must refuse with the parser's own words.
        ([{"transport": "local"}], "rung 0 is missing a model"),
        ([{"transport": "nope", "model": "m"}], "rung 0 has an unknown transport"),
        (
            [{"transport": "local", "model": "m", "bare": "yes"}],
            "rung 0 has a non-boolean bare",
        ),
        (
            [{"transport": "local", "model": "m", "max_tokens": 0}],
            "rung 0 has a non-positive-integer max_tokens",
        ),
        (
            [{"transport": "local", "model": "ok"}, "junk"],
            "rung 1 is not an object",
        ),
    ],
)
def test_op_set_chain_refuses_bad_rungs_with_reason(
    monkeypatch: pytest.MonkeyPatch, chain: list, reason: str
) -> None:
    rows = _app_settings(monkeypatch)
    with pytest.raises(SystemExit, match=reason):
        llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(chain=json.dumps(chain)))
    assert rows == {}


def test_op_set_chain_openai_compat_needs_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``parse_chain_rungs``' openai_compat guard reaches the CLI: without
    ``PRECIS_LLM_BASE_URL`` the rung is refused, not silently stored."""
    monkeypatch.delenv("PRECIS_LLM_BASE_URL", raising=False)
    rows = _app_settings(monkeypatch)
    chain = [{"transport": "openai_compat", "model": "m", "placement": "cloud"}]
    with pytest.raises(SystemExit, match="PRECIS_LLM_BASE_URL"):
        llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(chain=json.dumps(chain)))
    assert rows == {}


# ── op set: writes ──────────────────────────────────────────────────────


def test_op_set_chain_writes_validated_rungs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    rows = _app_settings(monkeypatch)
    chain = [
        {
            "transport": "local",
            "model": "openai/gpt-oss-120b",
            "placement": "local",
            "max_tokens": 4000,
            "timeout_s": 300,
            "reasoning_effort": "medium",
        },
        {
            "transport": "openai_compat",
            "model": "z-ai/glm-4.7-flash",
            "placement": "cloud",
        },
    ]
    monkeypatch.setenv("PRECIS_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(chain=json.dumps(chain)))
    key = live_config.op_key("reading_brief")
    assert json.loads(rows[key]) == {"chain": chain}
    out = capsys.readouterr().out
    assert f"set {key}" in out
    assert "openai/gpt-oss-120b" in out


def test_op_set_local_shorthand_writes_one_local_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _app_settings(monkeypatch)
    llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(local="openai/gpt-oss-120b"))
    key = live_config.op_key("reading_brief")
    assert json.loads(rows[key]) == {
        "chain": [
            {
                "placement": "local",
                "model": "openai/gpt-oss-120b",
                "transport": "local",
            }
        ]
    }


def test_op_set_merges_into_existing_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--tier`` on an op that already carries a chain keeps the chain — the
    gripe's exact footgun (a SQL-written chain clobbered by the next CLI set)."""
    key = live_config.op_key("reading_brief")
    chain = [{"transport": "local", "model": "m", "placement": "local"}]
    rows = _app_settings(monkeypatch, {key: json.dumps({"chain": chain})})
    llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(tier="big"))
    assert json.loads(rows[key]) == {"chain": chain, "tier": "big"}


def test_op_set_malformed_existing_row_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = live_config.op_key("reading_brief")
    rows = _app_settings(monkeypatch, {key: "not-json{"})
    llm_cli._cmd_op_set(cast("Any", _Store()), _set_args(tier="big"))
    assert json.loads(rows[key]) == {"tier": "big"}


def test_op_set_chain_and_local_are_mutually_exclusive() -> None:
    import argparse

    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="cmd")
    llm_cli.add_parser(sub)
    with pytest.raises(SystemExit):
        top.parse_args(
            [
                "llm",
                "op",
                "set",
                "reading_brief",
                "--chain",
                "[]",
                "--local",
                "m",
            ]
        )


# ── op list: readable overrides ─────────────────────────────────────────


def _op_list_out(
    monkeypatch: pytest.MonkeyPatch,
    rows: dict[str, str],
    capsys: pytest.CaptureFixture,
) -> str:
    _app_settings(monkeypatch, rows)
    llm_cli._cmd_op_list(cast("Any", _Store()))
    return capsys.readouterr().out


def _row_for(out: str, source: str) -> str:
    return next(line for line in out.splitlines() if line.strip().startswith(source))


def test_op_list_shows_tier_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    out = _op_list_out(
        monkeypatch,
        {live_config.op_key("reading_brief"): '{"tier": "big"}'},
        capsys,
    )
    assert "override=tier=big" in _row_for(out, "reading_brief")


def test_op_list_shows_no_override_as_dash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    out = _op_list_out(monkeypatch, {}, capsys)
    assert _row_for(out, "reading_brief").endswith("override=—")


def test_op_list_renders_a_chain_readably(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The gr502328 symptom: a chain-only override used to print as '—/—'.
    Now each rung shows placement, model, transport and its tuning keys."""
    chain = [
        {
            "transport": "local",
            "model": "openai/gpt-oss-120b",
            "placement": "local",
            "max_tokens": 2400,
            "timeout_s": 300,
            "reasoning_effort": "low",
        },
        {
            "transport": "openai_compat",
            "model": "z-ai/glm-4.7-flash",
            "placement": "cloud",
        },
    ]
    out = _op_list_out(
        monkeypatch,
        {live_config.op_key("llm_summarize"): json.dumps({"chain": chain})},
        capsys,
    )
    row = _row_for(out, "llm_summarize")
    assert "—/—" not in row
    assert (
        "[local] openai/gpt-oss-120b (local, max_tokens=2400, timeout_s=300, "
        "reasoning_effort=low) → [cloud] z-ai/glm-4.7-flash (openai_compat)"
    ) in row


def test_op_list_shows_tier_model_and_chain_together(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    out = _op_list_out(
        monkeypatch,
        {
            live_config.op_key("llm_summarize"): json.dumps(
                {
                    "tier": "small",
                    "model": "m1",
                    "chain": [
                        {"transport": "local", "model": "m2", "placement": "local"}
                    ],
                }
            )
        },
        capsys,
    )
    row = _row_for(out, "llm_summarize")
    assert "tier=small" in row
    assert "model=m1" in row
    assert "[local] m2 (local)" in row


def test_op_list_surfaces_unknown_keys_verbatim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A key this CLI predates still prints — the row can't hide a field."""
    out = _op_list_out(
        monkeypatch,
        {live_config.op_key("reading_brief"): '{"tier": "big", "future": 1}'},
        capsys,
    )
    assert "future=1" in _row_for(out, "reading_brief")
