"""Tests for ``precis classify progress`` — the auth-free categorizer
coverage readout (gr515633 ask 4).

The readout is a thin render over
:func:`precis.categorizers.progress_rows` — the same aggregate counts the
``/categorizers/progress`` web fragment serves — so these tests pin the
line shape against the shared test DB rather than re-testing the SQL
(that lives in ``tests/precis_web/test_categorizers.py``).
"""

from __future__ import annotations

import argparse
import io
from argparse import Namespace
from contextlib import redirect_stdout

from precis.cli import classify as classify_cli
from precis.store import Store
from precis.store.types import Tag


def test_progress_subcommand_registered() -> None:
    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="cmd")
    classify_cli.add_parser(sub)

    args = top.parse_args(["classify", "progress"])
    assert args.cmd == "classify"
    assert args.classify_cmd == "progress"


def test_progress_prints_one_line_per_categorizer(store: Store) -> None:
    """Every axis and every topic gets a done/total/last_minted line,
    including categorizers nothing has ever tagged (0/N)."""
    out = io.StringIO()
    with redirect_stdout(out):
        classify_cli._cmd_progress(store, Namespace(database_url=None))
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    assert lines, "progress printed nothing"
    by_name = dict(ln.split(":", 1) for ln in lines)
    assert "domain" in by_name  # a ref-level axis
    assert "role3" in by_name  # a chunk-level axis
    assert "mof" in by_name  # a topic (src/precis/data/topics/mof.yaml)
    assert "last_minted=" in by_name["domain"]


def test_progress_reports_tagged_counts(store: Store) -> None:
    """A ref tagged DOMAIN:chemistry bumps domain's done count; its
    last_minted is a real timestamp, not 'never'."""
    ref = store.insert_ref(
        kind="paper", slug="clz-progress-paper", title="a paper", meta={}
    )
    store.add_tag(ref.id, Tag.closed("DOMAIN", "chemistry"), set_by="agent")

    out = io.StringIO()
    with redirect_stdout(out):
        classify_cli._cmd_progress(store, Namespace(database_url=None))
    by_name = dict(ln.split(":", 1) for ln in out.getvalue().splitlines() if ln.strip())
    domain = by_name["domain"]
    done_s = domain.split("/", 1)[0].strip()
    assert done_s.isdigit() and int(done_s) >= 1
    assert "last_minted=never" not in domain
