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
from precis.cli.worker import _build_handlers, _print_status, _resolve_embedder
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
