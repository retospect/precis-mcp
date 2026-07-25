"""``precis classify {role3,topics}`` — CLI argument parsing.

Pure argparse-tree checks (mutually-exclusive scope group, ``--all`` only on
``topics``) plus the shared ``_resolve_scope`` helper's ``--all`` short-circuit
— no DB touch needed for that path since it returns before querying the store.
"""

from __future__ import annotations

import argparse

import pytest

from precis.cli import classify as classify_cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    classify_cli.add_parser(sub)
    return parser


def test_topics_all_parses_and_resolves_to_ref_ids_none() -> None:
    ns = _parser().parse_args(["classify", "topics", "--all"])
    assert ns.all is True
    # `--all` short-circuits before touching the store — passing None is safe.
    assert classify_cli._resolve_scope(None, ns) is None  # type: ignore[arg-type]


def test_topics_cites_of_parses() -> None:
    ns = _parser().parse_args(["classify", "topics", "--cites-of", "43020"])
    assert ns.all is False
    assert ns.cites_of == 43020


def test_topics_topic_parses() -> None:
    ns = _parser().parse_args(["classify", "topics", "--topic", "nanobuds"])
    assert ns.topic == "nanobuds"


def test_topics_ref_ids_parses() -> None:
    ns = _parser().parse_args(["classify", "topics", "--ref-ids", "1,2,3"])
    assert ns.ref_ids == "1,2,3"


def test_topics_requires_exactly_one_scope_selector() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["classify", "topics"])


def test_topics_rejects_multiple_scope_selectors() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["classify", "topics", "--all", "--topic", "nanobuds"])


def test_role3_has_no_all_selector() -> None:
    """``role3`` never sweeps the whole corpus from the CLI — that's the
    worker pass; the driver only takes the three targeted selectors."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["classify", "role3", "--all"])


def test_role3_requires_exactly_one_scope_selector() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(["classify", "role3"])


def test_role3_cites_of_parses() -> None:
    ns = _parser().parse_args(["classify", "role3", "--cites-of", "43020"])
    assert ns.cites_of == 43020
    assert getattr(ns, "all", False) is False
