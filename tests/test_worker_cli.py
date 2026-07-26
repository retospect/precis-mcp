"""Smoke tests for ``precis worker`` CLI parser + dispatch.

End-to-end behaviour (claim/process/write) lives under
``tests/workers/``; this file just pins the argparse surface and
the ``--status`` output shape so the CLI contract is locked.
"""

from __future__ import annotations

import argparse
import json

import pytest

from precis.cli.main import _build_parser
from precis.cli.worker import (
    _axis_id_default_on,
    _build_handlers,
    _classify_topics_enabled_slugs,
    _print_status,
    _resolve_embedder,
    _should_register_categorizer,
)
from precis.embedder import MockEmbedder, RemoteEmbedder
from precis.format import toon

# ---------------------------------------------------------------------------
# Parser registration
# ---------------------------------------------------------------------------


class TestParser:
    def test_worker_subcommand_registered(self, monkeypatch):
        # --embedder now defaults to PRECIS_EMBEDDER; clear it so the
        # documented fallback ('bge-m3') is what the test pins.
        monkeypatch.delenv("PRECIS_EMBEDDER", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["worker"])
        assert args.cmd == "worker"
        # Default flag values.
        assert args.status is False
        assert args.once is False
        assert args.batch_size == 32
        assert args.idle_seconds == 2.0
        assert args.only is None
        assert args.embedder == "bge-m3"
        assert args.summarizer_model == "rake-lemma"

    def test_only_accepts_watch_poll(self, monkeypatch):
        """``watch_poll`` must be a valid ``--only`` choice. It has a
        registration block in worker.py but is deliberately NOT in the
        default profile sets (it runs from a dedicated cron), so the only
        way to invoke it is ``--only watch_poll`` — which argparse must
        accept."""
        monkeypatch.delenv("PRECIS_EMBEDDER", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["worker", "--only", "watch_poll", "--once"])
        assert args.only == "watch_poll"
        assert args.once is True

    def test_worker_embedder_reads_env(self, monkeypatch):
        monkeypatch.setenv("PRECIS_EMBEDDER", "remote")
        parser = _build_parser()
        args = parser.parse_args(["worker"])
        assert args.embedder == "remote"

    def test_worker_status_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["worker", "--status"])
        assert args.status is True

    def test_worker_only_choices(self):
        parser = _build_parser()
        args = parser.parse_args(["worker", "--only", "embed"])
        assert args.only == "embed"
        args2 = parser.parse_args(["worker", "--only", "summarize"])
        assert args2.only == "summarize"

    def test_worker_embedder_mock(self):
        parser = _build_parser()
        args = parser.parse_args(["worker", "--embedder", "mock"])
        assert args.embedder == "mock"

    def test_worker_remote_embedder_flags(self, monkeypatch):
        # Env should not leak into the parser defaults under test.
        monkeypatch.delenv("PRECIS_EMBEDDER_URL", raising=False)
        monkeypatch.delenv("PRECIS_EMBEDDER_TIMEOUT", raising=False)
        monkeypatch.delenv("PRECIS_EMBEDDER_MAX_RETRIES", raising=False)
        parser = _build_parser()
        args = parser.parse_args(
            [
                "worker",
                "--embedder",
                "remote",
                "--embedder-url",
                "http://127.0.0.1:8181",
                "--embedder-timeout",
                "5",
                "--embedder-max-retries",
                "1",
            ]
        )
        assert args.embedder == "remote"
        assert args.embedder_url == "http://127.0.0.1:8181"
        assert args.embedder_timeout == 5.0
        assert args.embedder_max_retries == 1

    def test_worker_remote_embedder_defaults(self, monkeypatch):
        monkeypatch.delenv("PRECIS_EMBEDDER_URL", raising=False)
        monkeypatch.delenv("PRECIS_EMBEDDER_TIMEOUT", raising=False)
        monkeypatch.delenv("PRECIS_EMBEDDER_MAX_RETRIES", raising=False)
        parser = _build_parser()
        args = parser.parse_args(["worker"])
        assert args.embedder_url is None
        assert args.embedder_timeout == 30.0
        assert args.embedder_max_retries == 3

    def test_worker_format_flag_defaults_to_none(self):
        parser = _build_parser()
        args = parser.parse_args(["worker"])
        # ``None`` is the explicit "no override" sentinel so
        # ``resolve_format`` can pick the contextual default.
        assert args.format is None

    def test_worker_format_flag_accepts_choices(self):
        parser = _build_parser()
        for fmt in ("toon", "json", "table"):
            args = parser.parse_args(["worker", "--format", fmt])
            assert args.format == fmt


# ---------------------------------------------------------------------------
# _resolve_embedder — remote URL threading (regression for ADR 0020 deploy)
# ---------------------------------------------------------------------------


class TestResolveEmbedder:
    def _ns(self, **overrides) -> argparse.Namespace:
        defaults = dict(
            embedder="mock",
            embedder_url=None,
            embedder_timeout=30.0,
            embedder_max_retries=3,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_mock(self):
        assert isinstance(_resolve_embedder(self._ns(embedder="mock")), MockEmbedder)

    def test_remote_threads_url(self):
        emb = _resolve_embedder(
            self._ns(embedder="remote", embedder_url="http://127.0.0.1:8181")
        )
        assert isinstance(emb, RemoteEmbedder)

    def test_remote_without_url_raises(self):
        # The deploy regression: `precis worker --embedder remote` with no
        # URL must fail loudly, not silently build a broken embedder.
        with pytest.raises(ValueError, match="PRECIS_EMBEDDER_URL"):
            _resolve_embedder(self._ns(embedder="remote", embedder_url=None))


# ---------------------------------------------------------------------------
# _build_handlers — per-flag handler selection
# ---------------------------------------------------------------------------


class TestBuildHandlers:
    def _ns(self, **overrides) -> argparse.Namespace:
        defaults = dict(
            only=None,
            embedder="mock",
            summarizer_model="rake-lemma",
            max_keywords=50,
            min_phrase_words=1,
            max_phrase_words=4,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_default_includes_both(self):
        handlers = _build_handlers(self._ns())
        names = [h.name for h in handlers]
        assert names == ["embed:mock", "summarize:rake-lemma"]

    def test_only_embed_excludes_summarizer(self):
        handlers = _build_handlers(self._ns(only="embed"))
        names = [h.name for h in handlers]
        assert names == ["embed:mock"]

    def test_only_summarize_excludes_embedder(self):
        handlers = _build_handlers(self._ns(only="summarize"))
        names = [h.name for h in handlers]
        assert names == ["summarize:rake-lemma"]

    def test_summarizer_model_propagates(self):
        handlers = _build_handlers(
            self._ns(only="summarize", summarizer_model="rake-v2")
        )
        assert handlers[0].name == "summarize:rake-v2"


# ---------------------------------------------------------------------------
# --status output formatting (DB-backed)
# ---------------------------------------------------------------------------


class TestPrintStatus:
    """Pin the rendered shape of ``precis worker --status``.

    The default format is ``"toon"`` — matching the pipe default
    that :func:`precis.cli._common.resolve_format` picks when
    stdout is not a TTY. Tests cover all three formats so the
    registry wiring is exercised end-to-end.
    """

    def _handlers(self):
        from precis.workers.embed import EmbedHandler
        from precis.workers.summarize import RakeLemmaHandler
        from tests.workers._helpers import make_mock_bge_m3

        return [
            EmbedHandler(make_mock_bge_m3()),
            RakeLemmaHandler(),
        ]

    def test_emits_toon_header_and_one_row_per_handler(self, store, capsys):
        handlers = self._handlers()
        _print_status(handlers, store)
        out = capsys.readouterr().out
        # ``print`` adds the trailing newline; TOON itself does not.
        rows = toon.load(out)
        assert len(rows) == len(handlers)

        # Column shape is the pinned status schema.
        assert list(rows[0]) == ["handler", "total", "ok", "failed", "pending"]

        # Names must match the handlers in order.
        names = [row["handler"] for row in rows]
        assert names == ["embed:bge-m3", "summarize:rake-lemma"]

        # All numeric columns parse as digits — load returns strings,
        # so the test asserts on the string form.
        for row in rows:
            assert row["total"].isdigit()
            assert row["ok"].isdigit()
            assert row["failed"].isdigit()
            assert row["pending"].isdigit()

    def test_format_table_renders_box_drawing(self, store, capsys):
        _print_status(self._handlers(), store, format="table")
        out = capsys.readouterr().out
        # The ASCII renderer uses U+2500-family glyphs; pinning a
        # corner is enough to confirm dispatch landed on the table
        # serializer.
        assert "┌" in out
        assert "└" in out
        assert "handler" in out

    def test_format_json_round_trips(self, store, capsys):
        _print_status(self._handlers(), store, format="json")
        out = capsys.readouterr().out
        decoded = json.loads(out)
        assert isinstance(decoded, list)
        assert len(decoded) == 2
        # JSON preserves native types — `total` is an int, not a
        # string. Differs from the TOON / table paths intentionally;
        # nested-record consumers want real ints.
        assert isinstance(decoded[0]["total"], int)
        assert decoded[0]["handler"] == "embed:bge-m3"


# ---------------------------------------------------------------------------
# Ref-pass scheduling priority (real work before background I/O)
# ---------------------------------------------------------------------------


class TestRefPassPriority:
    """``ref_passes`` must run job execution + planner lifecycle ahead
    of slow fetch/enrichment/reviewer passes, or a fetch backlog
    starves ``dispatch`` and the planner stalls (the incident this
    ordering was introduced for). The run loop is sequential per cycle,
    so priority == list order.
    """

    @staticmethod
    def _named(name):
        # Stand-in for a registered ref-pass closure: only ``__name__``
        # matters to the scheduler.
        def _pass(_batch_size):  # pragma: no cover - never invoked
            raise AssertionError("scheduling test never calls the pass")

        _pass.__name__ = name
        return _pass

    def test_real_work_outranks_background_fetch(self):
        from precis.cli.worker import _ref_pass_priority

        dispatch = _ref_pass_priority(self._named("_dispatch_pass"))
        inproc = _ref_pass_priority(self._named("_job_claude_inproc_pass"))
        for slow in ("_fetch_pass", "_chase_pass", "_gp_fetch_pass"):
            assert dispatch < _ref_pass_priority(self._named(slow))
            assert inproc < _ref_pass_priority(self._named(slow))

    def test_plan_tick_executor_outranks_reviewers(self):
        # On the agent profile the plan_tick executor must not sit
        # behind the multi-minute opus reviewers.
        from precis.cli.worker import _ref_pass_priority

        inproc = _ref_pass_priority(self._named("_job_claude_inproc_pass"))
        for reviewer in (
            "_structural_pass",
            "_deep_review_pass",
            "_llm_summarize_pass",
        ):
            assert inproc < _ref_pass_priority(self._named(reviewer))

    def test_unknown_pass_lands_between_real_work_and_tail(self):
        from precis.cli.worker import _ref_pass_priority

        unknown = _ref_pass_priority(self._named("_some_plugin_pass"))
        assert _ref_pass_priority(self._named("_dispatch_pass")) < unknown
        assert unknown < _ref_pass_priority(self._named("_fetch_pass"))

    def test_stable_sort_pulls_dispatch_ahead_of_fetch(self):
        # Registration order has fetch before dispatch (fetch_oa at 746,
        # dispatch at 924); the sort must invert that while keeping
        # intra-band registration order stable.
        from precis.cli.worker import _ref_pass_priority

        registered = [
            self._named(n)
            for n in (
                "_chase_pass",
                "_fetch_pass",
                "_llm_summarize_pass",
                "_auto_check_pass",
                "_dispatch_pass",
                "_sweeper_pass",
                "_job_claude_inproc_pass",
            )
        ]
        registered.sort(key=_ref_pass_priority)
        order = [p.__name__ for p in registered]
        # Job execution first, then lifecycle, then the fetch tail.
        assert order.index("_job_claude_inproc_pass") < order.index("_auto_check_pass")
        assert order.index("_dispatch_pass") < order.index("_fetch_pass")
        assert order.index("_dispatch_pass") < order.index("_chase_pass")
        assert order.index("_sweeper_pass") < order.index("_llm_summarize_pass")
        # Stable within the lifecycle band: auto_check kept ahead of
        # dispatch kept ahead of sweeper (their registration order).
        assert (
            order.index("_auto_check_pass")
            < order.index("_dispatch_pass")
            < order.index("_sweeper_pass")
        )

    def test_ref_pass_priority_keys_match_registered_passes(self):
        """Every band-assigned key must name a live ``ref_passes.append``
        closure. Guards the ``__name__``-keyed table against a silent
        rename: renaming ``_chase_pass`` without updating the table would
        drop it from BACKGROUND into DEFAULT and mis-schedule it. Parsing
        the module AST rather than importing keeps this a pure static
        check with no worker wiring.
        """
        import ast
        from pathlib import Path

        from precis.cli import worker as worker_mod
        from precis.cli.worker import _REF_PASS_PRIORITY

        source = Path(worker_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        appended: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "append"
                and isinstance(func.value, ast.Name)
                and func.value.id == "ref_passes"
                and node.args
                and isinstance(node.args[0], ast.Name)
            ):
                appended.add(node.args[0].id)

        missing = set(_REF_PASS_PRIORITY) - appended
        assert not missing, (
            "priority table keys with no matching ref_passes.append() site "
            f"(renamed or removed closure?): {sorted(missing)}"
        )


# ---------------------------------------------------------------------------
# Live-toggle registration (bug: a live On-flip couldn't start a default-off
# categorizer, because the boot gate only ever *disabled* — the pass was never
# registered, so the per-cycle gate had nothing to enable).
# ---------------------------------------------------------------------------


class TestCategorizerRegistration:
    def test_registers_all_categorizers_with_no_only(self):
        # No --only: every categorizer registers regardless of enabled-state,
        # so a live prio-flip is picked up by the per-cycle gate without a
        # worker restart.
        for name in ("classify", "classify_topics", "axis:domain", "axis:material"):
            assert _should_register_categorizer(None, name) is True

    def test_only_restricts_to_the_named_pass(self):
        assert _should_register_categorizer("classify", "classify") is True
        assert _should_register_categorizer("classify", "classify_topics") is False
        assert _should_register_categorizer("classify", "axis:domain") is False
        assert _should_register_categorizer("axis:domain", "axis:domain") is True
        assert _should_register_categorizer("axis:domain", "axis:material") is False


class TestAxisGateDefault:
    def test_axis_default_reads_env_set(self):
        env = frozenset({"domain", "material"})
        assert _axis_id_default_on("axis:domain", env) is True
        assert _axis_id_default_on("axis:material", env) is True
        assert _axis_id_default_on("axis:scale", env) is False  # not seeded → off

    def test_empty_env_means_all_axes_default_off(self):
        assert _axis_id_default_on("axis:domain", frozenset()) is False

    def test_non_axis_service_returns_none(self):
        # None → caller falls back to the registry/profile default (these have
        # their own ServiceSpec; an axis:<id> does not).
        assert _axis_id_default_on("classify", frozenset()) is None
        assert _axis_id_default_on("classify_topics", frozenset({"domain"})) is None
        assert _axis_id_default_on("chunk_keywords", frozenset({"domain"})) is None


# ---------------------------------------------------------------------------
# classify_topics enabled-slugs (bug: --only classify_topics / the
# PRECIS_CLASSIFY_TOPICS_ENABLED admin backfill hatches silently classified
# zero topics once per-topic gating (ADR 0068) landed).
# ---------------------------------------------------------------------------


class _FakeResolver:
    """Stubs :meth:`ServiceConfigResolver.enabled` — ``overrides`` mimics an
    explicit ``service_config`` row (prio 0 ⇒ False, prio >= 1 ⇒ True); a
    service with no entry falls back to the caller-supplied ``default_on``,
    same as the real resolver with no DB row."""

    def __init__(self, overrides: dict[str, bool] | None = None) -> None:
        self.overrides = overrides or {}

    def enabled(self, service: str, *, default_on: bool) -> bool:
        return self.overrides.get(service, default_on)


class TestClassifyTopicsEnabledSlugs:
    def test_only_classify_topics_means_full_taxonomy(self):
        resolver = _FakeResolver()
        assert (
            _classify_topics_enabled_slugs(
                resolver,
                only="classify_topics",
                global_on=False,
                topics_env=frozenset(),
                slugs=["safety", "batteries"],
            )
            is None
        )

    def test_global_on_enables_every_slug(self):
        resolver = _FakeResolver()
        assert _classify_topics_enabled_slugs(
            resolver,
            only=None,
            global_on=True,
            topics_env=frozenset(),
            slugs=["safety", "batteries"],
        ) == ["safety", "batteries"]

    def test_neither_falls_back_to_topics_env_subset(self):
        resolver = _FakeResolver()
        assert _classify_topics_enabled_slugs(
            resolver,
            only=None,
            global_on=False,
            topics_env=frozenset({"safety"}),
            slugs=["safety", "batteries"],
        ) == ["safety"]

    def test_per_topic_override_wins_over_global_on(self):
        # An explicit prio-0 `topic:batteries` row force-disables it even
        # though PRECIS_CLASSIFY_TOPICS_ENABLED=1 defaults every topic on.
        resolver = _FakeResolver(overrides={"topic:batteries": False})
        assert _classify_topics_enabled_slugs(
            resolver,
            only=None,
            global_on=True,
            topics_env=frozenset(),
            slugs=["safety", "batteries"],
        ) == ["safety"]
