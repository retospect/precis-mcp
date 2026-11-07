"""Acceptance is a local file edit; the operator owns review and landing."""

from __future__ import annotations

import subprocess

import pytest

from precis.cli import _build_parser
from precis.cli import memory_recall_set as recall_set
from precis.cli.memory import load_recall_set, run
from precis.store import Tag


def _pair(query="observed question", handle="me123"):
    return {
        "query": query,
        "want": handle,
        "title": "Observed answer",
        "hook": "The answer hook",
        "source": "trace",
        "why": f"ag456: used {handle} (mode=hybrid)",
    }


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    path = tmp_path / recall_set.SET_PATH
    path.parent.mkdir(parents=True)
    path.write_text("[\n]\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", recall_set.SET_PATH],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(tmp_path)
    return path


def test_accept_subset_preserves_schema_prints_diff_and_is_idempotent(checkout):
    first, second = _pair(), _pair("second query", "me124")
    count, diff = recall_set.accept_proposals(checkout, [first, second], only=[2])
    assert count == 1
    assert "--- a/src/precis/data/memory_recall_set.json" in diff
    entries = load_recall_set(checkout)
    assert entries == [second]
    assert entries[0].keys() == recall_set.FIELDS
    assert entries[0]["source"] == "trace" and "ag456" in entries[0]["why"]
    before = checkout.read_text(encoding="utf-8")
    assert recall_set.accept_proposals(checkout, [first, second], only=[2]) == (0, "")
    assert checkout.read_text(encoding="utf-8") == before
    assert recall_set.accept_proposals(checkout, [first, second])[0] == 1
    assert load_recall_set(checkout) == [second, first]
    assert before.splitlines()[1] in checkout.read_text(encoding="utf-8")


@pytest.mark.parametrize("only", [[0], [2], []])
def test_invalid_selection_never_edits(checkout, only):
    with pytest.raises(ValueError, match="pair numbers"):
        recall_set.accept_proposals(checkout, [_pair()], only=only)
    assert load_recall_set(checkout) == []


@pytest.mark.parametrize(
    "change",
    [{"source": "manual"}, {"want": "td123"}, {"why": "no provenance"}, {"extra": "x"}],
)
def test_bad_selected_pair_refuses_entire_edit(checkout, change):
    with pytest.raises(ValueError, match="six-field trace-backed"):
        recall_set.accept_proposals(checkout, [_pair(), {**_pair(), **change}])
    assert load_recall_set(checkout) == []


def test_refuses_outside_git_before_any_connection(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("outside a checkout must refuse before connecting")

    monkeypatch.setattr(recall_set.psycopg, "connect", unexpected)
    with pytest.raises(SystemExit, match="Git work tree"):
        run(_build_parser().parse_args(["memory", "recall-set", "accept", "td123"]))


def test_requires_tracked_recall_set(checkout):
    assert recall_set.checkout_set_path() == checkout
    subprocess.run(
        ["git", "rm", "--cached", recall_set.SET_PATH], check=True, capture_output=True
    )
    with pytest.raises(ValueError, match="tracked"):
        recall_set.checkout_set_path()


def test_operator_cli_reads_todo_but_never_commits_or_changes_graph(
    checkout, store, monkeypatch, capsys
):
    pairs = [_pair(), _pair("second query", "me124")]
    todo = store.insert_ref(
        kind="todo",
        slug=None,
        title="Review recall pairs",
        meta={recall_set.PROPOSALS_KEY: pairs},
    )
    store.add_tag(todo.id, Tag.closed("STATUS", "open"), set_by="system")
    with store.pool.connection() as conn, monkeypatch.context() as scoped:
        scoped.setattr(recall_set.psycopg, "connect", lambda dsn, **_kw: conn)
        scoped.setattr(recall_set, "resolve_dsn", lambda *a, **k: "unused")
        run(
            _build_parser().parse_args(
                ["memory", "recall-set", "accept", f"td{todo.id}", "--only", "1"]
            )
        )
    assert load_recall_set(checkout) == [pairs[0]]
    output = capsys.readouterr().out
    assert "+++ b/src/precis/data/memory_recall_set.json" in output
    assert "No graph writes, commits or pushes" in output and "Operator:" in output
    meta = dict(store.get_ref(kind="todo", id=todo.id).meta)
    meta.pop("filed_by", None)  # the server-side origin stamp is not under test
    assert meta == {recall_set.PROPOSALS_KEY: pairs}
    history = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], capture_output=True, check=False
    )
    assert history.returncode != 0
