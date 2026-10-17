"""CLI entry points under ``PRECIS_EMBEDDER=remote`` with no URL (gr495175).

Commands that store no vectors (the worker embeds) must proceed with
``embedder=None``; ``taproot-migrate apply`` needs one and must exit 2 with a
single stderr line naming ``PRECIS_EMBEDDER_URL``. No DB: a fake store stands
in, and the first call after the embedder line is patched to record what it
received.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.cli._common import cli_embedder
from precis.config import PrecisConfig
from precis.embedder import (
    EmbedderNotConfigured,
    MockEmbedder,
    RemoteEmbedder,
    make_embedder_from_config,
)


class _Reached(Exception):
    """Private sentinel raised by the patched seam after the embedder line."""


class _FakeStore:
    def __init__(self) -> None:
        self.closed = False

    def embedding_dim(self) -> int:
        return 1024

    def close(self) -> None:
        self.closed = True


def _cfg(**kw) -> SimpleNamespace:
    base = {
        "embedder": "remote",
        "embedder_url": None,
        "embedder_timeout": 300.0,
        "embedder_max_retries": 5,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 1. make_embedder_from_config
# ---------------------------------------------------------------------------


def test_from_config_remote_without_url_raises_one_line():
    with pytest.raises(EmbedderNotConfigured) as ei:
        make_embedder_from_config(_cfg(), dim=1024)
    msg = str(ei.value)
    assert "PRECIS_EMBEDDER_URL" in msg
    assert "\n" not in msg


def test_from_config_remote_honors_cfg_timeout_and_retries():
    emb = make_embedder_from_config(
        _cfg(
            embedder_url="http://emb.invalid:9",
            embedder_timeout=7.5,
            embedder_max_retries=2,
        ),
        dim=1024,
    )
    assert isinstance(emb, RemoteEmbedder)
    assert emb._timeout == 7.5
    assert emb._max_retries == 2


def test_from_config_mock_needs_no_url():
    emb = make_embedder_from_config(_cfg(embedder="mock"), dim=16)
    assert isinstance(emb, MockEmbedder)


def test_from_config_name_override_beats_cfg():
    emb = make_embedder_from_config(_cfg(), dim=16, name="mock")
    assert isinstance(emb, MockEmbedder)


# ---------------------------------------------------------------------------
# 2. cli_embedder
# ---------------------------------------------------------------------------


def test_cli_embedder_not_needed_returns_none(capsys):
    assert (
        cli_embedder(_cfg(), cast(Any, _FakeStore()), needed=False, prog="precis x")
        is None
    )
    assert capsys.readouterr().err == ""


def test_cli_embedder_needed_exits_2_with_one_line(capsys):
    with pytest.raises(SystemExit) as ei:
        cli_embedder(_cfg(), cast(Any, _FakeStore()), needed=True, prog="precis x")
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.endswith("\n")
    assert "PRECIS_EMBEDDER_URL" in err
    assert "precis x" in err


# ---------------------------------------------------------------------------
# 3. real entry points
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no stray .env
    monkeypatch.setenv("PRECIS_EMBEDDER", "remote")
    monkeypatch.delenv("PRECIS_EMBEDDER_URL", raising=False)
    monkeypatch.setenv("PRECIS_DATABASE_URL", "postgresql://fake/none")
    assert PrecisConfig().embedder_url is None

    store = _FakeStore()
    monkeypatch.setattr(
        "precis.store.Store.connect", classmethod(lambda cls, dsn: store)
    )
    monkeypatch.setattr("precis.route_log.bind_store", lambda s: None)
    monkeypatch.setattr("precis.budget.bind_store", lambda s: None)
    monkeypatch.setattr("precis.settings.bind_store", lambda s: None)
    return SimpleNamespace(store=store, tmp=tmp_path)


def _recorder(monkeypatch, target, *, kw):
    """Patch ``target`` to record its ``kw`` argument and raise ``_Reached``."""
    seen: list = []

    def fake(*a, **k):
        seen.append(k[kw])
        raise _Reached

    monkeypatch.setattr(target, fake)
    return seen


def test_ingest_run_ingest(env, monkeypatch):
    from precis.cli import ingest

    seen = _recorder(monkeypatch, "precis.dispatch.Hub", kw="embedder")
    args = argparse.Namespace(
        root=str(env.tmp), kinds="md", database_url=None, force=False
    )
    with pytest.raises(_Reached):
        ingest.run_ingest(args)
    assert seen == [None]
    assert env.store.closed


def test_ingest_run_oracles(env, monkeypatch):
    from precis.cli import ingest

    seen = _recorder(
        monkeypatch, "precis.jobs.oracle_sync.maybe_reingest", kw="embedder"
    )
    args = argparse.Namespace(
        src=str(env.tmp),
        dry_run=False,
        database_url=None,
        force=True,
        overwrite=False,
    )
    with pytest.raises(_Reached):
        ingest.run_oracles(args)
    assert seen == [None]


def _patch_sim_registry(monkeypatch, env):
    entry = SimpleNamespace(
        path=env.tmp, resolved_manifest_path=lambda: env.tmp / "m.yaml"
    )
    monkeypatch.setattr(
        "precis.sim.registry.registry_path", lambda override=None: env.tmp
    )
    monkeypatch.setattr("precis.sim.registry.load_registry", lambda p: {"s": entry})
    monkeypatch.setattr("precis.sim.manifest.load_manifest", lambda p: object())
    monkeypatch.setenv("PRECIS_ROOT", str(env.tmp))


def _patch_hub_recording(monkeypatch) -> list:
    seen: list = []

    def fake_hub(*, store, embedder):
        seen.append(embedder)
        return object()

    monkeypatch.setattr("precis.dispatch.Hub", fake_hub)
    return seen


def test_sim_ingest(env, monkeypatch):
    from precis.cli import sim

    _patch_sim_registry(monkeypatch, env)
    seen = _patch_hub_recording(monkeypatch)

    def fake_ingest(**k):
        raise _Reached

    monkeypatch.setattr("precis.sim.ingest.ingest_sim", fake_ingest)
    args = argparse.Namespace(slug="s", registry=None, database_url=None, force=False)
    with pytest.raises(_Reached):
        sim._run_ingest(args)
    assert seen == [None]


def test_sim_verify(env, monkeypatch):
    from precis.cli import sim

    _patch_sim_registry(monkeypatch, env)
    seen = _patch_hub_recording(monkeypatch)
    monkeypatch.setattr("precis.sim.verify.make_corpus_search_fn", lambda s: None)
    monkeypatch.setattr("precis.sim.verify.make_llm_judge_fn", lambda: None)

    def fake_verify(**k):
        raise _Reached

    monkeypatch.setattr("precis.sim.verify.verify_sim", fake_verify)
    args = argparse.Namespace(slug="s", registry=None, database_url=None, dry_run=False)
    with pytest.raises(_Reached):
        sim._run_verify(args)
    assert seen == [None]


def test_memory_index_q(env, monkeypatch):
    from precis.cli import memory

    seen = _recorder(
        monkeypatch, "precis.cli.memory.render_memory_index", kw="embedder"
    )
    args = argparse.Namespace(
        memory_cmd="index", q="anything", recall_set=None, k=5, database_url=None
    )
    with pytest.raises(_Reached):
        memory.run(args)
    assert seen == [None]
    assert env.store.closed


def test_memory_index_recall_set_exits_2_one_line(env, capsys):
    from precis.cli import memory

    rs = env.tmp / "recall.json"
    rs.write_text("[]", encoding="utf-8")
    args = argparse.Namespace(
        memory_cmd="index", q=None, recall_set=str(rs), k=5, database_url=None
    )
    with pytest.raises(SystemExit) as ei:
        memory.run(args)
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "PRECIS_EMBEDDER_URL" in err


@pytest.fixture
def patent_env(env, monkeypatch):
    monkeypatch.setattr("precis.secrets.get_secret", lambda name, store=None: "x")
    monkeypatch.setattr("precis.patent.ops.OpsClient", lambda **k: object())
    return env


def _patent_args(**kw):
    base = {
        "database_url": None,
        "fair_use_limit_gb": 1.0,
        "dry_run": True,
        "name": None,
        "limit": None,
        "slugs": None,
        "include_stubs": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_patent_run_runner(patent_env, monkeypatch):
    from precis.cli import patent

    seen = _recorder(
        monkeypatch, "precis.jobs.patent_watch.run_one_pass", kw="embedder"
    )
    with pytest.raises(_Reached):
        patent.run_runner(_patent_args())
    assert seen == [None]


def test_patent_run_fulltext_sweep(patent_env, monkeypatch):
    from precis.cli import patent

    seen = _recorder(
        monkeypatch,
        "precis.jobs.patent_fulltext_sweep.run_fulltext_sweep",
        kw="embedder",
    )
    with pytest.raises(_Reached):
        patent.run_fulltext_sweep_cli(_patent_args())
    assert seen == [None]


def test_patent_run_reingest(patent_env, monkeypatch):
    from precis.cli import patent

    seen = _recorder(
        monkeypatch, "precis.jobs.patent_reingest.run_reingest_pass", kw="embedder"
    )
    with pytest.raises(_Reached):
        patent.run_reingest_cli(_patent_args())
    assert seen == [None]


def test_perplexity_import(env, monkeypatch):
    from precis.cli import perplexity

    (env.tmp / "a.md").write_text("hello", encoding="utf-8")
    seen: list = []

    def fake_hub(*, store, embedder):
        seen.append(embedder)
        raise _Reached

    monkeypatch.setattr("precis.dispatch.Hub", fake_hub)
    args = argparse.Namespace(
        dir=str(env.tmp),
        glob="*.md",
        limit=None,
        kind="websearch",
        dry_run=False,
        database_url=None,
        query_from="filename",
    )
    with pytest.raises(_Reached):
        perplexity.run(args)
    assert seen == [None]
    assert env.store.closed


def test_taproot_apply_exits_2_one_line(env, monkeypatch, capsys):
    from precis.cli import taproot_migrate

    monkeypatch.setattr(taproot_migrate, "_bind_llm_telemetry", lambda s: None)

    def boom(*a, **k):
        raise AssertionError("apply_dry_run must not be reached")

    monkeypatch.setattr("precis.taproot.apply_migrate.apply_dry_run", boom)
    rows = env.tmp / "rows.jsonl"
    rows.write_text("", encoding="utf-8")
    args = argparse.Namespace(
        json_path=str(rows),
        only_verdict=None,
        limit=None,
        database_url=None,
        embedder=None,
    )
    with pytest.raises(SystemExit) as ei:
        taproot_migrate._run_apply(args)
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "PRECIS_EMBEDDER_URL" in err
    assert "precis taproot-migrate" in err


def test_slice_refine_eval_exits_2_one_line(env, monkeypatch, capsys):
    from precis.taproot import slice_refine_eval

    def boom(*a, **k):
        raise AssertionError("eval_hub_slice must not be reached")

    monkeypatch.setattr(slice_refine_eval, "eval_hub_slice", boom)
    with pytest.raises(SystemExit) as ei:
        slice_refine_eval.main(["1", "--embedder", "remote", "--dsn", "postgresql://x"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "PRECIS_EMBEDDER_URL" in err
