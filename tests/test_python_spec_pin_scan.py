"""Spec-pin discovery preserves per-seed results without rescanning sources."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from precis.handlers import _python_tests as discovery
from precis.python_index import RepoIndex, Symbol, index_repo


@pytest.fixture
def pin_index(tmp_path: Path) -> tuple[RepoIndex, list[Symbol]]:
    (tmp_path / "core.py").write_text(
        "class First:\n    edit_modes = ()\n    shared_names = ()\n"
        "class Second:\n    requires_setting = ()\n    edit_modes = ()\n"
        "class Third:\n    missing_names = ()\n    generic_names = ()\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    sources = {
        "test_a.py": "# edit_modes edit_modes shared_names\n",
        "test_b.py": "# requires_setting; shared_names\n",
        "test_boundaries.py": "# xedit_modes edit_modes_extra éedit_modes edit_modesé\n",
        "test_stronger.py": "from core import First\n# edit_modes\n",
        **{f"test_generic_{i}.py": "# generic_names\n" for i in range(13)},
    }
    for name, source in sources.items():
        (tests / name).write_text(source, encoding="utf-8")
    idx = index_repo(tmp_path)
    seeds = [idx.symbol(f"core.{name}") for name in ("First", "Second", "Third")]
    assert all(seed is not None for seed in seeds)
    return idx, [seed for seed in seeds if seed is not None]


def test_spec_pin_results_equal_per_seed_scan(
    pin_index: tuple[RepoIndex, list[Symbol]], monkeypatch: pytest.MonkeyPatch
) -> None:
    idx, seeds = pin_index
    actual = [discovery.discover_tests(seed, idx) for seed in seeds]
    for seed, hits in zip(seeds, actual, strict=True):
        mod = idx.file(seed.file)
        assert mod is not None
        names = discovery._spec_pin_names(seed, mod)
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b")
        files: dict[str, list[str]] = {name: [] for name in names}
        for tmod in idx.modules.values():
            if discovery.is_test_path(tmod.file) and tmod.source is not None:
                for name in {m.group(0) for m in pattern.finditer(tmod.source)}:
                    files[name].append(tmod.file)

        def per_seed_scan(
            _idx: RepoIndex, result: dict[str, list[str]] = files
        ) -> dict[str, list[str]]:
            return result

        with monkeypatch.context() as patch:
            patch.setattr(discovery, "_spec_pin_files", per_seed_scan)
            assert discovery.discover_tests(seed, idx) == hits
    assert {hit.file for hit in actual[0]} == {
        "tests/test_a.py",
        "tests/test_b.py",
        "tests/test_stronger.py",
    }
    # Absent and overly common names add no pins; module imports still apply.
    assert all(hit.tier != discovery.TIER_SPEC_PIN for hit in actual[2])


def test_spec_pin_sources_scanned_once_per_index(
    pin_index: tuple[RepoIndex, list[Symbol]], monkeypatch: pytest.MonkeyPatch
) -> None:
    idx, seeds = pin_index
    scan = Mock(wraps=discovery._SPEC_PIN_TOKEN_RE)
    monkeypatch.setattr(discovery, "_SPEC_PIN_TOKEN_RE", scan)
    monkeypatch.setattr(discovery, "_SPEC_PIN_FILES_MEMO", None)
    for seed in seeds * 2:
        discovery.discover_tests(seed, idx)
    sources = [
        mod.source
        for mod in idx.modules.values()
        if discovery.is_test_path(mod.file) and mod.source is not None
    ]
    assert [call.args[0] for call in scan.finditer.call_args_list] == sources

    # A replacement snapshot must not reuse results from the old index.
    replacement = RepoIndex.build(idx.root, list(idx.modules.values()))
    discovery.discover_tests(seeds[0], replacement)
    assert scan.finditer.call_count == 2 * len(sources)
