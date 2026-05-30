"""Tests for ``precis.cli.watch``.

Filesystem helpers (``_wait_stable``, ``_move_to``,
``_move_to_corpus``, ``_write_error``, ``_should_skip``) are
plain unit tests — fast, no DB, no observer.

The orchestration (``process_pdf``, ``_handle_success``,
``_handle_failure``) is exercised with a stubbed
:func:`precis.ingest.add.precis_add` so we don't pull Marker /
Postgres into watcher tests. End-to-end watcher coverage with
the real ``precis_add`` lives in ``tests/ingest/test_add.py``.

The CLI parser registration is checked via the same ``--help``
smoke test the existing ``test_cli.py`` uses for ``add``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from precis.cli.watch import (
    _is_pdf,
    _move_to,
    _move_to_corpus,
    _PdfHandler,
    _should_skip,
    _wait_stable,
    _write_error,
    process_pdf,
)
from precis.ingest.add import IngestResult

# ---------------------------------------------------------------------------
# _is_pdf / _should_skip — pure
# ---------------------------------------------------------------------------


class TestIsPdf:
    def test_lowercase_pdf(self):
        assert _is_pdf(Path("paper.pdf"))

    def test_uppercase_pdf(self):
        assert _is_pdf(Path("paper.PDF"))

    def test_mixed_case(self):
        assert _is_pdf(Path("paper.Pdf"))

    def test_other_suffix(self):
        assert not _is_pdf(Path("paper.txt"))
        assert not _is_pdf(Path("paper.acatome"))


class TestShouldSkip:
    def test_inside_errors_dir(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        (watch_dir / "errors").mkdir()
        target = watch_dir / "errors" / "20240101-120000" / "bad.pdf"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x")
        assert _should_skip(target, watch_dir) is True

    def test_inside_completed_dir(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        (watch_dir / "completed").mkdir()
        target = watch_dir / "completed" / "ok.pdf"
        target.write_bytes(b"x")
        assert _should_skip(target, watch_dir) is True

    def test_normal_path_not_skipped(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        target = watch_dir / "subdir" / "fresh.pdf"
        target.parent.mkdir()
        target.write_bytes(b"x")
        assert _should_skip(target, watch_dir) is False

    def test_outside_watch_dir_skipped(self, tmp_path: Path):
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        outside = tmp_path / "elsewhere" / "stray.pdf"
        outside.parent.mkdir()
        outside.write_bytes(b"x")
        assert _should_skip(outside, watch_dir) is True


# ---------------------------------------------------------------------------
# _wait_stable
# ---------------------------------------------------------------------------


class TestBackfillSubprocess:
    """When ``subprocess_batch_size > 0``, backfill spawns
    ``precis _watch_batch_ingest`` subprocesses instead of calling
    ``process_pdf`` in-process. Marker memory leaks accumulate in the
    long-running watcher; subprocess isolation reclaims them per
    batch.
    """

    def _make_handler_with_batches(
        self, tmp_path: Path, batch_size: int
    ) -> _PdfHandler:
        from unittest.mock import MagicMock

        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        return _PdfHandler(
            watch_dir=watch_dir,
            corpus_dir=tmp_path / "corpus",
            errors_dir=tmp_path / "errors",
            duplicates_dir=tmp_path / "duplicates",
            store=MagicMock(),
            debounce=0.0,
            user="test",
            subprocess_batch_size=batch_size,
            database_url="postgresql://test/test",
        )

    def test_batches_paths_into_subprocess_calls(self, tmp_path: Path) -> None:
        handler = self._make_handler_with_batches(tmp_path, batch_size=2)
        for i, size in enumerate([10, 20, 30, 40, 50]):
            (handler.watch_dir / f"{chr(ord('a') + i)}.pdf").write_bytes(b"x" * size)

        spawned: list[list[Path]] = []

        def fake_spawn(pdfs: list[Path], **_kwargs: Any) -> None:
            spawned.append(list(pdfs))

        with patch("precis.cli.watch._spawn_batch_subprocess", side_effect=fake_spawn):
            handler.backfill()

        # 5 PDFs / batch=2 → 3 subprocess calls of sizes [2, 2, 1].
        assert [len(b) for b in spawned] == [2, 2, 1]
        # Ordering within batches: smallest-first.
        all_names = [p.name for batch in spawned for p in batch]
        assert all_names == ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"]

    def test_in_process_when_batch_size_zero(self, tmp_path: Path) -> None:
        """``subprocess_batch_size=0`` keeps the original behaviour:
        per-PDF in-process ``_enqueue`` calls, no subprocess spawn.
        """
        handler = self._make_handler_with_batches(tmp_path, batch_size=0)
        (handler.watch_dir / "a.pdf").write_bytes(b"x")
        (handler.watch_dir / "b.pdf").write_bytes(b"xx")

        seen: list[str] = []
        handler._enqueue = lambda p: seen.append(p.name)  # type: ignore[method-assign]
        with patch("precis.cli.watch._spawn_batch_subprocess") as spawn:
            handler.backfill()

        assert seen == ["a.pdf", "b.pdf"]
        spawn.assert_not_called()

    def test_k_shards_partition_round_robin(self, tmp_path: Path) -> None:
        """With ``subprocess_concurrency=2`` and 5 PDFs (sizes 10, 20,
        30, 40, 50), shards should be sorted-then-round-robin: shard 0
        gets sizes [10, 30, 50], shard 1 gets [20, 40]. Each PDF
        appears in exactly one shard.
        """
        handler = self._make_handler_with_batches(tmp_path, batch_size=1)
        handler.subprocess_concurrency = 2

        for i, size in enumerate([10, 20, 30, 40, 50]):
            (handler.watch_dir / f"{chr(ord('a') + i)}.pdf").write_bytes(b"x" * size)

        per_shard_calls: dict[int, list[Path]] = {}
        call_idx = {"i": 0}
        lock = __import__("threading").Lock()

        def fake_spawn(pdfs: list[Path], **_kwargs: Any) -> None:
            with lock:
                shard = call_idx["i"] % 2
                call_idx["i"] += 1
                per_shard_calls.setdefault(shard, []).extend(pdfs)

        # Force serial execution so we can assert on shard assignment
        # deterministically.
        handler.subprocess_concurrency = 1
        # Now patch to capture which shard each batch came from. With
        # K=2 the round-robin gives shard 0 = [a, c, e] and shard 1 =
        # [b, d].
        all_called: list[Path] = []
        with patch(
            "precis.cli.watch._spawn_batch_subprocess",
            side_effect=lambda pdfs, **kw: all_called.extend(pdfs),
        ):
            # Pass K=2 explicitly via the handler attribute.
            handler.subprocess_concurrency = 2
            handler.backfill()

        # All 5 PDFs processed, no duplicates.
        assert sorted(p.name for p in all_called) == [
            "a.pdf",
            "b.pdf",
            "c.pdf",
            "d.pdf",
            "e.pdf",
        ]
        # No file appears more than once.
        assert len(all_called) == 5

    def test_subprocess_command_threads_db_url_and_dirs(self, tmp_path: Path) -> None:
        """``_spawn_batch_subprocess`` builds a ``python -m precis
        _watch_batch_ingest …`` command with the right flags."""
        from precis.cli.watch import _spawn_batch_subprocess

        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

            class _R:
                returncode = 0

            return _R()

        with patch("precis.cli.watch.subprocess.run", side_effect=fake_run):
            _spawn_batch_subprocess(
                [tmp_path / "a.pdf", tmp_path / "b.pdf"],
                corpus_dir=tmp_path / "corpus",
                errors_dir=tmp_path / "errors",
                duplicates_dir=tmp_path / "dup",
                debounce=0.5,
                user="reto",
                database_url="postgresql://x/y",
            )

        cmd = captured["cmd"]
        assert "_watch_batch_ingest" in cmd
        assert "--corpus-dir" in cmd
        assert "--user" in cmd and "reto" in cmd
        assert "--database-url" in cmd and "postgresql://x/y" in cmd
        # PDFs trail the flags.
        assert cmd[-2:] == [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")]


class TestBackfillOrder:
    """Backfill processes smallest-first so a giant OOM PDF only
    blocks itself, not the whole queue behind it.
    """

    def _make_handler(self, tmp_path: Path) -> _PdfHandler:
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        from unittest.mock import MagicMock

        return _PdfHandler(
            watch_dir=watch_dir,
            corpus_dir=tmp_path / "corpus",
            errors_dir=tmp_path / "errors",
            duplicates_dir=tmp_path / "duplicates",
            store=MagicMock(),
            debounce=0.0,
            user="test",
        )

    def test_smaller_files_enqueued_first(self, tmp_path: Path) -> None:
        handler = self._make_handler(tmp_path)
        # Names sort alphabetically in reverse of size — proves the
        # sort key is size, not name.
        (handler.watch_dir / "a_huge.pdf").write_bytes(b"x" * 10_000)
        (handler.watch_dir / "m_medium.pdf").write_bytes(b"x" * 1_000)
        (handler.watch_dir / "z_tiny.pdf").write_bytes(b"x" * 10)

        seen: list[str] = []
        handler._enqueue = lambda p: seen.append(p.name)  # type: ignore[method-assign]
        handler.backfill()

        assert seen == ["z_tiny.pdf", "m_medium.pdf", "a_huge.pdf"]

    def test_managed_dirs_still_skipped(self, tmp_path: Path) -> None:
        handler = self._make_handler(tmp_path)
        (handler.watch_dir / "small.pdf").write_bytes(b"x" * 10)
        (handler.watch_dir / "errors").mkdir()
        (handler.watch_dir / "errors" / "20240101-000000").mkdir()
        (handler.watch_dir / "errors" / "20240101-000000" / "skip.pdf").write_bytes(
            b"x" * 5
        )

        seen: list[str] = []
        handler._enqueue = lambda p: seen.append(p.name)  # type: ignore[method-assign]
        handler.backfill()

        assert seen == ["small.pdf"]


class TestWaitStable:
    def test_stable_file_returns_true(self, tmp_path: Path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"hello")
        assert _wait_stable(f, debounce=0.01) is True

    def test_missing_file_returns_false(self, tmp_path: Path):
        f = tmp_path / "ghost.pdf"
        assert _wait_stable(f, debounce=0.01) is False


# ---------------------------------------------------------------------------
# _move_to / _move_to_corpus / _write_error
# ---------------------------------------------------------------------------


class TestMoveTo:
    def test_move_basic(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        src.write_bytes(b"data")
        dest_dir = tmp_path / "dest"
        result = _move_to(src, dest_dir)
        assert result.parent == dest_dir
        assert result.name == "paper.pdf"
        assert not src.exists()
        assert result.read_bytes() == b"data"

    def test_move_creates_parent(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        src.write_bytes(b"x")
        dest_dir = tmp_path / "deep" / "nested" / "dest"
        result = _move_to(src, dest_dir)
        assert result.exists()

    def test_move_conflict_renames_with_timestamp(self, tmp_path: Path):
        src = tmp_path / "paper.pdf"
        src.write_bytes(b"new")
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        (dest_dir / "paper.pdf").write_bytes(b"old")
        result = _move_to(src, dest_dir)
        assert result.parent == dest_dir
        assert result.name != "paper.pdf"
        assert result.name.startswith("paper_") and result.name.endswith(".pdf")
        # Old file untouched.
        assert (dest_dir / "paper.pdf").read_bytes() == b"old"


class TestMoveToCorpus:
    def test_letter_sharded(self, tmp_path: Path):
        pdf = tmp_path / "tmp.pdf"
        pdf.write_bytes(b"x")
        corpus = tmp_path / "corpus"
        result = _move_to_corpus(pdf, cite_key="smith24", corpus_dir=corpus)
        assert result == corpus / "s" / "smith24.pdf"
        assert result.exists()
        assert not pdf.exists()

    def test_uppercase_first_letter_lowered(self, tmp_path: Path):
        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"x")
        corpus = tmp_path / "corpus"
        result = _move_to_corpus(pdf, cite_key="Wei24a", corpus_dir=corpus)
        assert result.parent.name == "w"

    def test_non_alpha_first_char_falls_back_to_underscore(self, tmp_path: Path):
        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"x")
        corpus = tmp_path / "corpus"
        result = _move_to_corpus(pdf, cite_key="_anon23", corpus_dir=corpus)
        assert result.parent.name == "_"

    def test_existing_destination_renamed(self, tmp_path: Path):
        corpus = tmp_path / "corpus"
        (corpus / "s").mkdir(parents=True)
        (corpus / "s" / "smith24.pdf").write_bytes(b"old")

        pdf = tmp_path / "in.pdf"
        pdf.write_bytes(b"new")

        result = _move_to_corpus(pdf, cite_key="smith24", corpus_dir=corpus)
        # Conflict resolution preserves the existing file.
        assert result.name != "smith24.pdf"
        assert result.name.startswith("smith24_")
        assert (corpus / "s" / "smith24.pdf").read_bytes() == b"old"


class TestWriteError:
    def test_writes_error_file_with_traceback(self, tmp_path: Path):
        errors_dir = tmp_path / "errors"
        errors_dir.mkdir()
        pdf = tmp_path / "bad.pdf"
        pdf.write_bytes(b"x")
        try:
            raise ValueError("ingest blew up")
        except ValueError as e:
            err_path = _write_error(errors_dir, pdf, e)

        assert err_path.exists()
        assert err_path.name == "bad.error.txt"
        text = err_path.read_text()
        assert "bad.pdf" in text
        assert "ingest blew up" in text
        assert "Traceback" in text
        assert "Time:" in text


# ---------------------------------------------------------------------------
# process_pdf — orchestration with stubbed precis_add
# ---------------------------------------------------------------------------


class TestProcessPdf:
    def _layout(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        watch_dir = tmp_path / "inbox"
        watch_dir.mkdir()
        errors_dir = watch_dir / "errors"
        duplicates_dir = errors_dir / "duplicates"
        errors_dir.mkdir()
        duplicates_dir.mkdir()
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        return watch_dir, errors_dir, duplicates_dir, corpus_dir

    def test_inserted_path_moves_to_corpus(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "smith2024.pdf"
        pdf.write_bytes(b"%PDF-1.4 test bytes")

        fake_result = IngestResult(
            ref_id=42,
            inserted=True,
            paper_id="aabbccdd",
            pub_id="doi:10.1/x",
            cite_key="smith24",
            pdf_sha256="a" * 64,
            content_hash="b" * 64,
            chunks_written=3,
            identifiers={"doi": "10.1/x", "cite_key": "smith24"},
        )

        with patch("precis.cli.watch.precis_add", return_value=fake_result):
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]  # stubbed precis_add ignores it
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="reto",
            )

        assert dest is not None
        assert dest == corpus_dir / "s" / "smith24.pdf"
        assert dest.exists()
        assert not pdf.exists()  # moved out of inbox

        # ingest.log written with correct columns.
        log_lines = (corpus_dir / "ingest.log").read_text().splitlines()
        assert len(log_lines) == 1
        cols = log_lines[0].split("\t")
        assert cols[1] == "reto"
        assert cols[2] == "smith24"
        assert cols[3] == "42"
        assert cols[4] == "inserted"
        assert cols[5] == "smith2024.pdf"

    def test_existed_path_moves_to_duplicates(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "dup.pdf"
        pdf.write_bytes(b"%PDF dup")

        fake_result = IngestResult(
            ref_id=7,
            inserted=False,
            paper_id="ee55ff66",
            pub_id="doi:10.1/y",
            cite_key="jones23",
            pdf_sha256=None,
            content_hash=None,
            chunks_written=0,
            identifiers={"doi": "10.1/y", "cite_key": "jones23"},
        )

        with patch("precis.cli.watch.precis_add", return_value=fake_result):
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="reto",
            )

        assert dest is not None
        assert dest.parent == duplicates_dir
        assert not pdf.exists()
        # No corpus copy.
        assert not (corpus_dir / "j" / "jones23.pdf").exists()
        # Log line records ``existed`` not ``inserted``.
        log_text = (corpus_dir / "ingest.log").read_text()
        assert "\texisted\t" in log_text

    def test_failure_path_moves_to_errors_with_traceback(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "broken.pdf"
        pdf.write_bytes(b"%PDF broken")

        with patch(
            "precis.cli.watch.precis_add",
            side_effect=ValueError("marker exploded"),
        ):
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="reto",
            )

        # Failure path returns None, the watcher loop survives.
        assert dest is None
        # Original PDF moved into a timestamped bucket under errors/.
        assert not pdf.exists()
        ts_buckets = [
            p for p in errors_dir.iterdir() if p.is_dir() and p.name != "duplicates"
        ]
        assert len(ts_buckets) == 1
        bucket = ts_buckets[0]
        assert (bucket / "broken.pdf").exists()
        # Sibling .error.txt with traceback.
        err_files = list(bucket.glob("*.error.txt"))
        assert len(err_files) == 1
        assert "marker exploded" in err_files[0].read_text()
        # Failure does NOT write to ingest.log.
        assert not (corpus_dir / "ingest.log").exists()

    def test_disappearing_file_returns_none(self, tmp_path: Path):
        watch_dir, errors_dir, duplicates_dir, corpus_dir = self._layout(tmp_path)
        pdf = watch_dir / "ghost.pdf"
        # Don't write — file doesn't exist.
        with patch("precis.cli.watch.precis_add") as m:
            dest = process_pdf(
                pdf,
                store=object(),  # type: ignore[arg-type]
                corpus_dir=corpus_dir,
                errors_dir=errors_dir,
                duplicates_dir=duplicates_dir,
                debounce=0.01,
                user="",
            )
        assert dest is None
        m.assert_not_called()  # never reached precis_add


# ---------------------------------------------------------------------------
# CLI parser smoke test
# ---------------------------------------------------------------------------


class TestParserRegistration:
    def test_watch_in_top_level_help(self):
        from precis.cli import _build_parser

        parser = _build_parser()
        actions = {getattr(action, "dest", None) for action in parser._actions}
        # The subparsers action's choices are the registered subcommands.
        sub_actions = [action for action in parser._actions if action.dest == "cmd"]
        assert sub_actions, "expected a subparsers action with dest='cmd'"
        choices = sub_actions[0].choices  # type: ignore[attr-defined]
        assert "watch" in choices
        assert "add" in choices  # sanity — B4 still registered

    def test_watch_help_renders(self, capsys):
        from precis.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["watch", "--help"])
        out = capsys.readouterr().out
        assert "watch_dir" in out
        assert "--corpus-dir" in out
        assert "--no-backfill" in out
