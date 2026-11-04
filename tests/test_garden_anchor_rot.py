"""Anchor rot proposals over dev-DB memories and a local committed main tree."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from precis.graph.garden import _anchors
from precis.graph.garden import candidates as cand
from precis.graph.garden.proposals import emit_proposal, key_block, record_rejection
from precis.graph.garden.proposer import MEMORY_PROMPT_VERSION
from precis.store import Store
from precis.store.types import ChunkInsert, Tag


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    ).stdout.strip()


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    message_file = root / ".git" / "fixture-message"
    message_file.write_text(message, encoding="utf-8")
    _git(
        root,
        "-c",
        "user.name=Anchor fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-F",
        str(message_file),
    )
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def anchor_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _write(tmp_path, "src/sample/__init__.py", "")
    _write(
        tmp_path,
        "src/sample/mod.py",
        "MODULE_CONSTANT = 1\n"
        "FIRST, [SECOND] = 1, [2]\n"
        "class Settings:\n"
        "    EVENTS: tuple[str, ...] = ('event',)\n"
        "    def method(self):\n"
        "        LOCAL = 1\n"
        "def live():\n"
        "    LOCAL = 1\n"
        "    return 1\n",
    )
    _write(tmp_path, "src/asa_bot/__init__.py", "")
    _write(
        tmp_path,
        "docs/guide.md",
        "# Intro\n\n## Live section\n\nDetails.\n\n## 1. Setup\n\nSteps.\n",
    )
    _write(tmp_path, "scripts/live", "#!/bin/sh\nexit 0\n")
    _write(tmp_path, "scripts/lib/devin_hooks.py", "DEVIN_EVENTS = ('event',)\n")
    _git(tmp_path, "add", "src", "docs", "scripts")
    _commit(tmp_path, "Fixture main")
    monkeypatch.setattr(
        cand, "AnchorResolver", lambda: _anchors.AnchorResolver(tmp_path)
    )
    return tmp_path


def _memory(store: Store, body: str) -> int:
    ref = store.insert_ref(kind="memory", slug=None, title="Anchor memory")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "memory_body"})]
    )
    return int(ref.id)


def test_missing_symbol_and_section_pin_memory_and_main(
    store: Store, anchor_repo: Path
) -> None:
    body = "Use src/sample/mod.py::gone and docs/guide.md#absent."
    target = _memory(store, body)
    (candidate,) = cand.anchor_rot_candidates(store)
    assert candidate.target_id == target
    assert candidate.detector == "anchor_rot"
    assert candidate.action == "rewrite"
    expected_sha = cand._ref_shas(store, [target])[target]
    assert (
        candidate.payload["base_sha"]
        == candidate.evidence["content_sha"]
        == expected_sha
    )
    assert (
        candidate.payload["main_sha"]
        == candidate.evidence["main_sha"]
        == _git(anchor_repo, "rev-parse", "main")
    )
    assert {anchor["kind"] for anchor in candidate.evidence["anchors"]} == {
        "python",
        "section",
    }
    assert len(candidate.payload["ops"]) == 2
    for anchor in candidate.evidence["anchors"]:
        assert anchor["tried"]
        assert body[slice(*anchor["span"])] == anchor["anchor"]
        assert anchor["anchor"] in anchor["context"]
    for op in candidate.payload["ops"]:
        assert body[slice(*op["span"])] == op["find"]
        assert op["text"] is None
    assert candidate.payload["not_checked"] == []
    assert store.reviews_for("ref", target) == []
    assert cand._memory_bodies(store, [target])[0][1] == body


@pytest.mark.parametrize(
    "body",
    [
        "src/sample/mod.py::live",
        "`sample.mod.live`",
        "docs/guide.md#live-section",
        "`docs/guide.md` § Live section",
        "`docs/guide.md` § `Live section`",
        "docs/guide.md § 1. Setup",
        "See docs/guide.md § Live section for details.",
        "scripts/live",
        "docs/guide.md",
    ],
)
def test_live_anchors_produce_no_candidate(
    store: Store, anchor_repo: Path, body: str
) -> None:
    _memory(store, body)
    assert cand.anchor_rot_candidates(store) == []


@pytest.mark.parametrize(
    ("body", "target", "detail"),
    [
        (
            "scripts/lib/devin_hooks.py::DEVIN_EVENTS",
            "scripts/lib/devin_hooks.py",
            "DEVIN_EVENTS",
        ),
        ("`sample.mod.MODULE_CONSTANT`", "sample.mod.MODULE_CONSTANT", None),
        ("`sample.mod.Settings.EVENTS`", "sample.mod.Settings.EVENTS", None),
        ("src/sample/mod.py::FIRST", "src/sample/mod.py", "FIRST"),
        ("src/sample/mod.py::SECOND", "src/sample/mod.py", "SECOND"),
    ],
)
def test_live_constants_resolve(
    store: Store, anchor_repo: Path, body: str, target: str, detail: str | None
) -> None:
    resolver = _anchors.AnchorResolver(anchor_repo)
    assert resolver.check("python", target, detail)[0] is True
    _memory(store, body)
    assert cand.anchor_rot_candidates(store) == []


@pytest.mark.parametrize("symbol", ["MISSING_CONSTANT", "LOCAL", "Settings.LOCAL"])
def test_missing_constants_and_function_locals_are_absent(
    store: Store, anchor_repo: Path, symbol: str
) -> None:
    body = f"src/sample/mod.py::{symbol}"
    _memory(store, body)
    (candidate,) = cand.anchor_rot_candidates(store)
    assert [anchor["anchor"] for anchor in candidate.evidence["anchors"]] == [body]
    assert candidate.payload["not_checked"] == []


def test_meta_schedule_is_not_an_anchor(store: Store, anchor_repo: Path) -> None:
    _memory(store, "The cadence is configured with `meta.schedule`.")
    assert cand.anchor_rot_candidates(store) == []


@pytest.mark.parametrize(
    "body", ["`sample.py`", "`sample.mod.py`", "`main::sample.mod.py`"]
)
def test_backticked_filenames_are_not_python_symbols(
    store: Store, anchor_repo: Path, body: str
) -> None:
    _memory(store, body)
    assert cand.anchor_rot_candidates(store) == []


def test_meta_schedule_does_not_consume_anchor_budget(
    store: Store, anchor_repo: Path
) -> None:
    body = "`meta.schedule` " * (cand.ANCHOR_MATCH_CAP + 1) + "`sample.mod.absent`"
    _memory(store, body)
    (candidate,) = cand.anchor_rot_candidates(store)
    assert [anchor["anchor"] for anchor in candidate.evidence["anchors"]] == [
        "`sample.mod.absent`"
    ]
    assert [op["find"] for op in candidate.payload["ops"]] == ["`sample.mod.absent`"]
    assert candidate.payload["not_checked"] == []
    assert candidate.payload["matches_capped"] is False


@pytest.mark.parametrize(
    "body",
    [
        "`sample.mod.absent`",
        "sample.mod.absent.",
        "`asa_bot.absent`",
        "`docs/guide.md` § Missing section",
    ],
)
def test_dotted_symbols_and_section_names_are_checked(
    store: Store, anchor_repo: Path, body: str
) -> None:
    target = _memory(store, body)
    (candidate,) = cand.anchor_rot_candidates(store)
    assert candidate.target_id == target
    assert len(candidate.evidence["anchors"]) == 1


def test_live_section_does_not_hide_later_anchors(
    store: Store, anchor_repo: Path
) -> None:
    _memory(
        store,
        "See docs/guide.md § Live section and scripts/missing, "
        "src/sample/mod.py::gone, abc123deabc123deabc123deabc123deabc123de.",
    )
    (candidate,) = cand.anchor_rot_candidates(store)
    assert {anchor["kind"] for anchor in candidate.evidence["anchors"]} == {
        "path",
        "python",
        "commit",
    }


def test_unavailable_python_index_is_not_checked(
    store: Store, anchor_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.python_index import indexer

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ImportError("fixture: Python index unavailable")

    monkeypatch.setattr(indexer, "index_module_bytes", unavailable)
    target = _memory(store, "src/sample/mod.py::gone docs/missing.md")
    (candidate,) = cand.anchor_rot_candidates(store)
    assert candidate.target_id == target
    assert [anchor["kind"] for anchor in candidate.evidence["anchors"]] == ["path"]
    (unchecked,) = candidate.payload["not_checked"]
    assert unchecked["kind"] == "python"
    assert unchecked["tried"]
    assert "src/sample/mod.py::gone" in unchecked["anchor"]
    assert all("gone" not in op["find"] for op in candidate.payload["ops"])


def test_only_unverifiable_anchors_produce_no_rot(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cand, "AnchorResolver", lambda: _anchors.AnchorResolver(tmp_path)
    )
    _memory(store, "src/sample/mod.py::gone docs/missing.md scripts/missing abc123def")
    assert cand.anchor_rot_candidates(store) == []


def test_digits_and_letters_alone_are_not_commit_anchors(
    store: Store, anchor_repo: Path
) -> None:
    _memory(store, "1234567890123456789 abcdefabcdef deadbeef scripts/missing")
    (candidate,) = cand.anchor_rot_candidates(store)
    assert [anchor["kind"] for anchor in candidate.evidence["anchors"]] == ["path"]
    assert candidate.payload["not_checked"] == []


def test_script_and_missing_commit_are_reported(
    store: Store, anchor_repo: Path
) -> None:
    target = _memory(store, "scripts/missing abc123deabc123deabc123deabc123deabc123de")
    (candidate,) = cand.anchor_rot_candidates(store)
    assert candidate.target_id == target
    assert {anchor["kind"] for anchor in candidate.evidence["anchors"]} == {
        "path",
        "commit",
    }


def test_live_commit_resolves_as_full_and_short_sha(
    store: Store, anchor_repo: Path
) -> None:
    sha = _git(anchor_repo, "rev-parse", "main")
    _memory(store, f"The change is {sha}; abbreviated {sha[:12]}.")
    assert cand.anchor_rot_candidates(store) == []


def test_main_snapshot_ignores_other_branch_and_working_edits(
    store: Store, anchor_repo: Path
) -> None:
    main_sha = _git(anchor_repo, "rev-parse", "main")
    _git(anchor_repo, "switch", "-c", "other")
    _write(anchor_repo, "src/sample/mod.py", "def elsewhere():\n    return 2\n")
    _write(anchor_repo, "docs/guide.md", "# Elsewhere\n")
    _git(anchor_repo, "add", "src/sample/mod.py", "docs/guide.md")
    _commit(anchor_repo, "Other branch")
    _write(anchor_repo, "src/sample/mod.py", "def uncommitted():\n    return 3\n")
    _memory(store, "src/sample/mod.py::live docs/guide.md#live-section")
    target = _memory(store, "src/sample/mod.py::elsewhere")
    (candidate,) = cand.anchor_rot_candidates(store)
    assert candidate.target_id == target
    assert candidate.payload["main_sha"] == main_sha


def test_anchor_cursor_limit_and_wrap(store: Store, anchor_repo: Path) -> None:
    targets = [_memory(store, f"scripts/missing-{i}") for i in range(3)]
    cursors: dict[str, int] = {}
    seen: list[int] = []
    for _ in targets:
        found = cand.anchor_rot_candidates(
            store, limit=1, scan_cap=2, cursors=cursors, next_cursors=cursors
        )
        seen.extend(candidate.target_id for candidate in found)
    assert seen == targets
    assert (
        cand.anchor_rot_candidates(store, cursors=cursors, next_cursors=cursors) == []
    )
    assert cursors["anchor_rot:memory"] == 0


@pytest.mark.parametrize("extra", [0, 1, 5])
def test_anchor_matches_are_bounded(
    store: Store, anchor_repo: Path, extra: int
) -> None:
    body = " ".join(f"scripts/gone-{i}" for i in range(cand.ANCHOR_MATCH_CAP + extra))
    _memory(store, body)
    (candidate,) = cand.anchor_rot_candidates(store)
    assert len(candidate.evidence["anchors"]) == cand.ANCHOR_MATCH_CAP
    assert len(candidate.payload["ops"]) == cand.ANCHOR_MATCH_CAP
    assert candidate.payload["match_cap"] == cand.ANCHOR_MATCH_CAP
    assert candidate.payload["matches_capped"] is (extra > 0)


def test_retired_and_dream_memories_are_excluded(
    store: Store, anchor_repo: Path
) -> None:
    retired = _memory(store, "scripts/missing")
    store.retire_ref(retired)
    dream = _memory(store, "scripts/missing")
    store.add_tag(dream, Tag.closed("DREAM", "speculative"), set_by="system")
    assert cand.anchor_rot_candidates(store) == []


@pytest.mark.parametrize(("limit", "scan_cap"), [(0, 10), (10, 0)])
def test_empty_anchor_budget_does_not_scan(
    store: Store, anchor_repo: Path, limit: int, scan_cap: int
) -> None:
    _memory(store, "scripts/missing")
    assert cand.anchor_rot_candidates(store, limit=limit, scan_cap=scan_cap) == []


def test_anchor_rejections_do_not_hide_mentions_or_repo_changes(
    store: Store, anchor_repo: Path
) -> None:
    retired = _memory(store, "Old facts")
    store.retire_ref(retired)
    target = _memory(store, f"scripts/missing me{retired}")
    (anchor,) = cand.anchor_rot_candidates(store)
    (mention,) = cand.dangling_mention_candidates(store)
    record_rejection(
        store, anchor, rationale="History", prompt_version=MEMORY_PROMPT_VERSION
    )
    assert (
        key_block(
            store, "ref", target, anchor.key, prompt_version=MEMORY_PROMPT_VERSION
        )
        == "rejected"
    )
    assert (
        key_block(
            store, "ref", target, mention.key, prompt_version=MEMORY_PROMPT_VERSION
        )
        is None
    )
    assert emit_proposal(store, mention, rationale="Stale mention") is not None

    _write(anchor_repo, "docs/guide.md", "# Changed main\n")
    _git(anchor_repo, "add", "docs/guide.md")
    _commit(anchor_repo, "Advance main")
    (changed,) = cand.anchor_rot_candidates(store)
    assert changed.key != anchor.key
    assert (
        key_block(
            store, "ref", target, changed.key, prompt_version=MEMORY_PROMPT_VERSION
        )
        is None
    )
    assert emit_proposal(store, changed, rationale="New repo evidence") is not None
    assert replace(mention, payload={"main_sha": "other"}).key == mention.key
