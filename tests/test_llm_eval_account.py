"""``llm eval --account`` — pin one hosted account for one model (td574812).

Real PG (llm_account / external_rate_limits / llm.profiles / llm cards);
the provider seam is faked through monkeypatch so no real model runs. The
account/pool ledger persists across tests (the tables are seeded data), so
the autouse fixture snapshots and restores them — same shape as
tests/test_llm_accounts_handler.py. The fake key is assembled at runtime:
the repo is public, so no credential-shaped literal lands in the tree, and
no real hostname appears (``https://llm.example.invalid/v1``).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from precis import llm_catalog
from precis.errors import BadInput
from precis.llm_eval import run_eval
from precis.llm_eval.tasks import GoldTask

_MODEL = "openai/gpt-oss-120b"
_ACCOUNT = "acct-eval"
_BASE_URL = "https://llm.example.invalid/v1"
_KEY_REF = "PRECIS_EVAL_TEST_KEY"
# Assembled so no credential-shaped literal sits in the public tree.
_FAKE_KEY = "gsk" + "-fakeEvalKey0" + "12345"


@pytest.fixture(autouse=True)
def _restore_ledger_rows(store: Any) -> Iterator[None]:
    """Both ledger tables persist, including account/pool mutations."""
    snapshots: list[tuple[str, str, list[str], list[Any]]] = []
    for table, key, predicate in (
        ("llm_account", "name", "TRUE"),
        ("external_rate_limits", "provider", "provider LIKE 'llm:%%'"),
    ):
        with store.pool.connection() as conn:
            cursor = conn.execute(
                sql.SQL("SELECT * FROM {} WHERE " + predicate).format(
                    sql.Identifier(table)
                )
            )
            columns = [column.name for column in cursor.description]
            snapshots.append((table, key, columns, cursor.fetchall()))
    yield
    with store.pool.connection() as conn:
        for table, key, columns, rows in snapshots:
            assignments = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(column)) for column in columns
            )
            key_index = columns.index(key)
            keys = [row[key_index] for row in rows]
            predicate = "" if table == "llm_account" else "provider LIKE 'llm:%%' AND "
            conn.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE " + predicate + "NOT ({} = ANY(%s))"
                ).format(sql.Identifier(table), sql.Identifier(key)),
                (keys,),
            )
            for row in rows:
                values = tuple(
                    Jsonb(value) if isinstance(value, dict) else value for value in row
                )
                conn.execute(
                    sql.SQL("UPDATE {} SET {} WHERE {} = %s").format(
                        sql.Identifier(table), assignments, sql.Identifier(key)
                    ),
                    (*values, row[key_index]),
                )


def _pool_row(
    store: Any,
    key: str,
    *,
    limit: float | None = 30,
    rule: str | None = "rolling:60",
    unit: str | None = "calls",
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO external_rate_limits (provider, capacity, refill_per_sec, "
            'tokens, "class", unit, limit_reported, reset_rule, source, reported_at) '
            "VALUES (%s, 1, 1.0, 1, 'llm-api', %s, %s, %s, 'user', now())",
            (key, unit, limit, rule),
        )


def _seed_account(
    store: Any,
    name: str = _ACCOUNT,
    *,
    adapter: str = "openai_compat",
    profile: str | None = None,
    api_key_ref: str | None = _KEY_REF,
    models: list[str] | None = None,
    enabled: bool = False,
    entitlement: dict[str, Any] | None = None,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO llm_account (name, vendor, plan, adapter, profile, "
            "api_key_ref, models, entitlement, enabled) VALUES "
            "(%s, 'testvendor', 'free', %s, %s, %s, %s, %s::jsonb, %s)",
            (
                name,
                adapter,
                profile if profile is not None else name,
                api_key_ref,
                models if models is not None else [_MODEL],
                json.dumps(entitlement or {}),
                enabled,
            ),
        )


def _profiles_env(
    monkeypatch: pytest.MonkeyPatch,
    name: str = _ACCOUNT,
    entry: dict[str, Any] | None = None,
) -> None:
    if entry is None:
        entry = {"base_url": _BASE_URL, "api_key_ref": _KEY_REF, "kind": "metered"}
    monkeypatch.setenv("PRECIS_LLM_PROFILES", json.dumps({name: entry}))


def _seed_all(
    store: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pools: list[tuple[str, float | None, str | None]] | None = None,
    enabled: bool = False,
    models: list[str] | None = None,
    entitlement: dict[str, Any] | None = None,
    card: bool = True,
) -> None:
    """One resolvable pin end to end: account row, calls pools, profile,
    secret and card."""
    if pools is None:
        pools = [(f"llm:{_ACCOUNT}:gptoss120brpm", 30, "rolling:60")]
    _seed_account(store, enabled=enabled, models=models, entitlement=entitlement)
    for key, limit, rule in pools:
        _pool_row(store, key, limit=limit, rule=rule)
    _profiles_env(monkeypatch)
    monkeypatch.setenv(_KEY_REF, _FAKE_KEY)
    if card:
        llm_catalog.upsert_card(store, model_id=_MODEL, text="A model.")


def _needle_task(tid: str = "t1", axis: str = "long-context-recall") -> GoldTask:
    return GoldTask(tid, axis, "needle", "find it", {"needle": "AB-9"})


def _stub_provider(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[Any]:
    """Patch ``account_pin.provider_for``; return the (req, model) call list."""
    from precis.llm_eval import account_pin

    calls: list[Any] = []

    def _run(req: Any, *, model: str) -> Any:
        calls.append((req, model))
        return handler(req, model=model)

    monkeypatch.setattr(
        account_pin, "provider_for", lambda t, **kw: SimpleNamespace(run=_run)
    )
    return calls


def _ok_result(req: Any, model: str, *, text: str = "the answer is AB-9") -> Any:
    from precis.utils.llm.router import LlmResult

    return LlmResult(
        text=text, cost_usd=None, turns_used=None, model=model, tier=req.tier
    )


def _fail_if_called(*_a: Any, **_kw: Any) -> Any:
    raise AssertionError("a provider request was sent")


# ── resolve + dispatch ───────────────────────────────────────────────


def test_pinned_request_hits_account_endpoint_with_account_key_and_never_routes(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request goes to the account's own base_url with its own key —
    ``router.route`` is never consulted."""
    import precis.utils.llm.router as router
    import precis.workers.llm_summarize as ls
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Transport

    _seed_all(store, monkeypatch)
    pin = account_pin.resolve_account_pin(store, _ACCOUNT, _MODEL)

    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, cfg: Any) -> None:
            captured["url"] = cfg.url
            captured["api_key"] = cfg.api_key
            captured["model"] = cfg.model

        def complete(self, messages: Any, *, extra_body: Any = None) -> Any:
            return ls.LlmResult(text="the answer is AB-9", total_tokens=5)

    monkeypatch.setattr(ls, "LlmClient", _Client)
    monkeypatch.setattr(
        account_pin,
        "provider_for",
        lambda t, **kw: SimpleNamespace(
            run=lambda req, *, model: router._dispatch_openai_compat(req, model)
        ),
    )
    monkeypatch.setattr(router, "route", _fail_if_called)
    recs: list[Any] = []
    monkeypatch.setattr(
        account_pin,
        "record_dispatch",
        lambda req, res, **kw: recs.append((req, res, kw)),
    )

    from precis.utils.llm.router import Tier

    report = run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=[_needle_task()],
        account=_ACCOUNT,
        record=False,
    )
    assert report.ordinals["long-context-recall"] == 5
    assert captured["url"] == _BASE_URL
    assert captured["api_key"] == _FAKE_KEY
    assert captured["model"] == _MODEL
    assert recs and recs[0][2]["transport"] is Transport.OPENAI_COMPAT
    assert recs[0][2]["routed"] == "cloud"
    assert recs[0][0].profile == _ACCOUNT


def test_disabled_account_evaluates_and_stays_disabled(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``enabled`` is never a gate and never written: a disabled account
    resolves, evaluates and stays disabled (``updated_at`` untouched)."""
    from precis.utils.llm.accounts import get_account
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch, enabled=False)
    before = get_account(store, _ACCOUNT)
    assert before is not None and before.enabled is False
    _stub_provider(monkeypatch, _ok_result)

    report = run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=[_needle_task()],
        account=_ACCOUNT,
        record=False,
    )
    assert report.ordinals["long-context-recall"] == 5
    after = get_account(store, _ACCOUNT)
    assert after is not None
    assert after.enabled is False
    assert after.updated_at == before.updated_at


# ── refusals, all before any request ─────────────────────────────────


def test_unknown_account_refuses_with_next(store: Any) -> None:
    from precis.llm_eval.account_pin import resolve_account_pin

    with pytest.raises(BadInput) as ei:
        resolve_account_pin(store, "no-such-account", _MODEL)
    assert ei.value.next and "/accounts" in str(ei.value.next)


def test_model_not_on_account_refuses_without_request(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch, models=["other/model"])
    monkeypatch.setattr(account_pin, "provider_for", _fail_if_called)
    with pytest.raises(BadInput) as ei:
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=[_needle_task()],
            account=_ACCOUNT,
            record=False,
        )
    assert ei.value.options == ["other/model"]

    # entitlement.models.<model>.allowed = false refuses the same way.
    _seed_account(
        store,
        name="acct-eval-2",
        entitlement={
            "models": {_MODEL: {"allowed": False, "price_in": 0, "price_out": 0}}
        },
    )
    _pool_row(store, "llm:acct-eval-2:gptoss120brpm")
    _profiles_env(monkeypatch, "acct-eval-2")
    with pytest.raises(BadInput):
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=[_needle_task()],
            account="acct-eval-2",
            record=False,
        )


def test_no_api_key_ref_refuses(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile with no ``api_key_ref`` refuses — never the host-keyed or
    ``PRECIS_LLM_API_KEY`` fallback."""
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch)
    _profiles_env(
        monkeypatch,
        entry={"base_url": _BASE_URL, "kind": "metered"},
    )
    monkeypatch.setattr(account_pin, "provider_for", _fail_if_called)
    with pytest.raises(BadInput, match="api_key_ref"):
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=[_needle_task()],
            account=_ACCOUNT,
            record=False,
        )


def test_missing_key_refuses_without_request(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable secret refuses naming the REFERENCE, never a value."""
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch)
    monkeypatch.delenv(_KEY_REF, raising=False)
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **kw: None)
    monkeypatch.setattr(account_pin, "provider_for", _fail_if_called)
    with pytest.raises(BadInput) as ei:
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=[_needle_task()],
            account=_ACCOUNT,
            record=False,
        )
    assert _KEY_REF in str(ei.value)


def test_missing_card_refuses_without_request(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch, card=False)
    monkeypatch.setattr(account_pin, "provider_for", _fail_if_called)
    with pytest.raises(BadInput) as ei:
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=[_needle_task()],
            account=_ACCOUNT,
            record=False,
        )
    assert ei.value.next


def test_no_applicable_limit_refuses(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No calls pool with a known limit applying to (account, model) →
    refuse; a pool naming a DIFFERENT listed model does not apply."""
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_account(store, models=[_MODEL, "meta/llama-70b"])
    # a pool for the other model only — segment starts with another slug
    _pool_row(store, "llm:acct-eval:llama70brpm", limit=30, rule="rolling:60")
    _profiles_env(monkeypatch)
    monkeypatch.setenv(_KEY_REF, _FAKE_KEY)
    llm_catalog.upsert_card(store, model_id=_MODEL, text="A model.")
    monkeypatch.setattr(account_pin, "provider_for", _fail_if_called)
    with pytest.raises(BadInput, match="limit"):
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=[_needle_task()],
            account=_ACCOUNT,
            record=False,
        )


def test_account_wide_pool_applies_to_any_listed_model(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment starting with no listed model's slug is account-wide."""
    from precis.llm_eval.account_pin import resolve_account_pin

    _seed_account(store, models=[_MODEL])
    _pool_row(store, "llm:acct-eval:freerpm", limit=20, rule="rolling:60")
    _profiles_env(monkeypatch)
    monkeypatch.setenv(_KEY_REF, _FAKE_KEY)
    llm_catalog.upsert_card(store, model_id=_MODEL, text="A model.")
    pin = resolve_account_pin(store, _ACCOUNT, _MODEL)
    assert pin.min_gap_s == pytest.approx(60 / 20)


# ── pacing, daily budget, 429 halt ───────────────────────────────────


def test_rpm_gap_respected(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling pools pace request STARTS by s/limit; the stricter wins."""
    from precis.llm_eval import account_pin

    _seed_account(store)
    _pool_row(store, "llm:acct-eval:gptoss120brpm", limit=30, rule="rolling:60")
    _pool_row(store, "llm:acct-eval:freerpm", limit=6, rule="rolling:60")
    _profiles_env(monkeypatch)
    monkeypatch.setenv(_KEY_REF, _FAKE_KEY)
    llm_catalog.upsert_card(store, model_id=_MODEL, text="A model.")
    pin = account_pin.resolve_account_pin(store, _ACCOUNT, _MODEL)
    assert pin.min_gap_s == pytest.approx(60 / 6)

    now = [0.0]
    starts: list[float] = []

    def _run(req: Any, *, model: str) -> Any:
        starts.append(now[0])
        return _ok_result(req, model)

    monkeypatch.setattr(
        account_pin, "provider_for", lambda t, **kw: SimpleNamespace(run=_run)
    )
    monkeypatch.setattr(account_pin, "record_dispatch", lambda *a, **kw: None)
    dispatch = account_pin.account_pinned_dispatch(
        pin,
        clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    from precis.utils.llm.router import LlmRequest, Tier

    req = LlmRequest(tier=Tier.MEDIUM, prompt="p")
    for _ in range(3):
        dispatch(req)
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 10.0
    assert starts[2] - starts[1] >= 10.0


def test_daily_budget_refuses_up_front(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long-window pool bounds TOTAL requests: n > limit refuses before
    the first request."""
    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_all(
        store,
        monkeypatch,
        pools=[("llm:acct-eval:gptoss120brpd", 2, "rolling:86400")],
    )
    monkeypatch.setattr(account_pin, "provider_for", _fail_if_called)
    tasks = [_needle_task(f"t{i}") for i in range(3)]
    with pytest.raises(BadInput, match="limit"):
        run_eval(
            store,
            model=_MODEL,
            tier=Tier.MEDIUM,
            tasks=tasks,
            account=_ACCOUNT,
            record=False,
        )


def test_429_halts_run_and_records_nothing(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider 429 halts the run: later calls error without sending and
    the void axis records nothing."""
    from precis.utils.llm.router import LlmResult, Tier

    _seed_all(store, monkeypatch)
    calls = _stub_provider(
        monkeypatch,
        lambda req, *, model: LlmResult(
            text="",
            cost_usd=None,
            turns_used=None,
            model=model,
            tier=req.tier,
            error="HTTP Error 429: too many requests",
        ),
    )
    tasks = [_needle_task("t1"), _needle_task("t2")]
    report = run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=tasks,
        account=_ACCOUNT,
        record=True,
    )
    res = report.results[0]
    assert res.void and res.errors == 2
    assert len(calls) == 1  # the second task never reached the wire
    assert "halted after provider rate limit" in (res.per_task[1].error or "")
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value=_MODEL)
    assert ref is not None
    assert not (ref.meta or {}).get("account_evals")
    assert llm_catalog.list_reviews(store, ref.id) == []


# ── recording per (account, model) ───────────────────────────────────


def test_result_recorded_against_account_and_model(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record lands in meta.account_evals[<account>][<axis>] with a
    ``account:<name>`` review; card-level ``capability`` stays untouched
    and a second account does not overwrite the first."""
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch)
    _stub_provider(monkeypatch, _ok_result)
    report = run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=[_needle_task()],
        account=_ACCOUNT,
        record=True,
    )
    assert report.results[0].recorded is True
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value=_MODEL)
    assert ref is not None
    meta = ref.meta or {}
    entry = meta["account_evals"][_ACCOUNT]["long-context-recall"]
    assert entry["score"] == 5
    assert entry["model"] == _MODEL
    assert entry["n"] == 1
    assert entry["provenance"] == "measured-eval"
    assert entry["at"].endswith("Z")
    # card-level capability untouched
    assert "long-context-recall" not in (meta.get("capability") or {})
    reviews = llm_catalog.list_reviews(store, ref.id)
    assert len(reviews) == 1
    rmeta = reviews[0].meta or {}
    assert rmeta.get("entry_type") == "measured-eval"
    assert rmeta.get("provenance") == f"account:{_ACCOUNT}"
    assert rmeta.get("axis") == "long-context-recall"
    assert f"account {_ACCOUNT}" in reviews[0].text

    # A second account records beside the first.
    _seed_account(store, name="acct-eval-2")
    _pool_row(store, "llm:acct-eval-2:freerpm", limit=20, rule="rolling:60")
    _profiles_env(
        monkeypatch,
        "acct-eval-2",
        {"base_url": _BASE_URL, "api_key_ref": _KEY_REF, "kind": "metered"},
    )
    run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=[_needle_task()],
        account="acct-eval-2",
        record=True,
    )
    ref2 = store.find_ref_by_meta(kind="llm", key="model_id", value=_MODEL)
    assert ref2 is not None
    evals = (ref2.meta or {})["account_evals"]
    assert _ACCOUNT in evals and "acct-eval-2" in evals
    assert evals[_ACCOUNT]["long-context-recall"]["score"] == 5


# ── scrubbing ────────────────────────────────────────────────────────


def test_key_never_in_logs_results_or_exceptions(
    store: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A provider error or reply echoing the key is scrubbed before it can
    reach a log, a result row, a review or a card."""
    import logging

    from precis.llm_eval import account_pin
    from precis.utils.llm.router import Tier

    _seed_all(store, monkeypatch)
    calls = _stub_provider(monkeypatch, _ok_result)
    monkeypatch.setattr(account_pin, "record_dispatch", lambda *a, **kw: None)

    pin = account_pin.resolve_account_pin(store, _ACCOUNT, _MODEL)
    dispatch = account_pin.account_pinned_dispatch(
        pin, clock=lambda: 0.0, sleep=lambda s: None
    )
    from precis.utils.llm.router import LlmRequest

    req = LlmRequest(tier=Tier.MEDIUM, prompt="p")
    # 1) a raised provider error echoing key + bearer shape
    calls.clear()

    def _raise(r: Any, *, model: str) -> Any:
        raise RuntimeError(f"HTTP Error 401: bad key {_FAKE_KEY} Bearer {_FAKE_KEY}")

    monkeypatch.setattr(
        account_pin,
        "provider_for",
        lambda t, **kw: SimpleNamespace(run=_raise),
    )
    with caplog.at_level(logging.WARNING):
        res = dispatch(req)
    assert res.error is not None
    assert _FAKE_KEY not in res.error
    assert "Bearer" not in res.error
    assert len(res.error) <= 300
    assert _FAKE_KEY not in caplog.text

    # 2) a reply text echoing the key is scrubbed before record_dispatch.
    monkeypatch.setattr(
        account_pin,
        "provider_for",
        lambda t, **kw: SimpleNamespace(
            run=lambda r, *, model: _ok_result(
                r, model, text=f"echo {_FAKE_KEY} Bearer abcdef123"
            )
        ),
    )
    recs: list[Any] = []
    monkeypatch.setattr(
        account_pin,
        "record_dispatch",
        lambda req, res, **kw: recs.append(res),
    )
    res2 = dispatch(req)
    assert _FAKE_KEY not in (res2.text or "")
    assert _FAKE_KEY not in (recs[0].text or "")

    # 3) record path: review text + card meta carry no key material.
    _stub_provider(
        monkeypatch,
        lambda r, *, model: _ok_result(r, model),
    )
    run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=[_needle_task()],
        account=_ACCOUNT,
        record=True,
    )
    ref = store.find_ref_by_meta(kind="llm", key="model_id", value=_MODEL)
    assert ref is not None
    blob = json.dumps(ref.meta or {})
    reviews = llm_catalog.list_reviews(store, ref.id)
    for r in reviews:
        blob += r.text + json.dumps(r.meta or {})
    assert _FAKE_KEY not in blob
    # no 6+ character run of the key survives anywhere we recorded
    for i in range(len(_FAKE_KEY) - 5):
        assert _FAKE_KEY[i : i + 6] not in res.error
        assert _FAKE_KEY[i : i + 6] not in res2.text
        assert _FAKE_KEY[i : i + 6] not in caplog.text


# ── untouched paths + CLI ────────────────────────────────────────────


def test_compare_and_unpinned_paths_unchanged(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --account the single-model path still routes through the
    live ``router.route`` (dispatch_fn default), unchanged."""
    import precis.utils.llm.router as router
    from precis.utils.llm.router import Tier

    seen: list[Any] = []

    def _route_stub(req: Any) -> Any:
        seen.append(req)
        return _ok_result(req, req.model or "m")

    monkeypatch.setattr(router, "route", _route_stub)
    report = run_eval(
        store,
        model=_MODEL,
        tier=Tier.MEDIUM,
        tasks=[_needle_task()],
        record=False,
    )
    assert seen and report.ordinals["long-context-recall"] == 5


def test_cli_account_refuses_compare_and_pins(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--account`` refuses --compare/--placement/--endpoint; the CLI
    threads it into run_eval and prints it in the header."""
    import precis.llm_eval as llm_eval_pkg
    from precis.cli.llm import _cmd_eval
    from precis.llm_eval.harness import AxisResult, EvalReport

    base = dict(
        model="m",
        compare=None,
        account="acct-eval",
        tier="small",
        gold=None,
        no_record=True,
        placement_a=None,
        placement_b=None,
        endpoint_a=None,
        endpoint_b=None,
    )
    for clash in ("compare", "placement_a", "endpoint_b"):
        args = argparse.Namespace(**{**base, clash: "x"})
        with pytest.raises(BadInput, match="--account"):
            _cmd_eval(store, args)

    captured: dict[str, Any] = {}

    def _rep(store: Any, **kw: Any) -> EvalReport:
        captured.update(kw)
        return EvalReport(
            model="m",
            results=[
                AxisResult(axis="long-context-recall", n=1, mean_score=1.0, ordinal=5)
            ],
            skipped=[],
        )

    monkeypatch.setattr(llm_eval_pkg, "run_eval", _rep)
    _cmd_eval(store, argparse.Namespace(**base))
    assert captured["account"] == "acct-eval"


def test_cli_account_header_names_account(
    store: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import precis.llm_eval as llm_eval_pkg
    from precis.cli.llm import _cmd_eval
    from precis.llm_eval.harness import EvalReport

    monkeypatch.setattr(
        llm_eval_pkg,
        "run_eval",
        lambda store, **kw: EvalReport(model="m", results=[], skipped=[]),
    )
    _cmd_eval(
        store,
        argparse.Namespace(
            model="m",
            compare=None,
            account="acct-eval",
            tier="small",
            gold=None,
            no_record=True,
            placement_a=None,
            placement_b=None,
            endpoint_a=None,
            endpoint_b=None,
        ),
    )
    assert "acct-eval" in capsys.readouterr().out.splitlines()[0]


def test_resolve_ignores_enabled_and_entitlement_absent(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account with enabled=False and no entitlement entry resolves —
    only ``allowed is False`` refuses."""
    from precis.llm_eval.account_pin import resolve_account_pin

    _seed_all(store, monkeypatch, enabled=False)
    pin = resolve_account_pin(store, _ACCOUNT, _MODEL)
    assert pin.account == _ACCOUNT and pin.model == _MODEL
    assert pin.profile_name == _ACCOUNT
    assert pin.api_key_ref == _KEY_REF
