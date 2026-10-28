"""Direct pathway kill guards, with fake process operations only."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from precis_pathway import scratch
from precis_pathway.types import DetachedHandle


@pytest.mark.parametrize("pgid", [0, 1, -1, 8888])
def test_kill_refuses_reserved_and_own_process_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    pgid: int,
) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgrp", lambda: 8888)
    monkeypatch.setattr(os, "killpg", lambda pg, sig: calls.append((pg, sig)))
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: calls.append((pid, flags)))
    directory = tmp_path / "seed"
    directory.mkdir()
    handle: DetachedHandle = {
        "pid": 4242,
        "pgid": pgid,
        "dir": str(directory),
        "started_at": 0.0,
    }

    assert scratch.kill_seed_partial_detached(handle) is False

    assert calls == []
    assert directory.is_dir()
    assert "refusing unsafe process group" in caplog.text


@pytest.mark.parametrize("entrypoint", ["finalize", "kill"])
@pytest.mark.parametrize(
    "location", ["direct", "outside", "traversal", "symlink", "sibling-symlink"]
)
def test_cleanup_confined_to_submit_temp_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    entrypoint: str,
    location: str,
) -> None:
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.delenv("PRECIS_PATHWAY_KEEP_FAILED_SCRATCH", raising=False)
    monkeypatch.setattr(os, "getpgrp", lambda: 8888)
    monkeypatch.setattr(os, "killpg", lambda pg, sig: None)
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (pid, 0))
    parent = tmp_path if location in {"outside", "symlink"} else temp_root
    directory = parent / "autocatpath-seed-safe"
    directory.mkdir()
    supplied = directory
    if location == "traversal":
        staging = temp_root / "autocatpath-seed-staging"
        staging.mkdir()
        supplied = staging / ".." / directory.name
    elif location in {"symlink", "sibling-symlink"}:
        supplied = temp_root / "autocatpath-seed-link"
        supplied.symlink_to(directory, target_is_directory=True)
    handle: DetachedHandle = {
        "pid": 4242,
        "pgid": 4242,
        "dir": str(supplied),
        "started_at": 0.0,
    }

    if entrypoint == "finalize":
        scratch.finalize_seed_partial_detached(handle)
    else:
        assert scratch.kill_seed_partial_detached(handle) is True

    if location == "direct":
        assert not directory.exists()
    else:
        assert directory.is_dir()
        if location in {"symlink", "sibling-symlink"}:
            assert supplied.is_symlink()
        assert "refusing unsafe scratch directory" in caplog.text


def test_seed_kill_propagates_unsafe_group_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis_pathway import seed_job

    monkeypatch.setattr(
        scratch, "kill_seed_partial_detached", lambda handle, **kwargs: False
    )
    with pytest.raises(RuntimeError, match="^unsafe pgid$"):
        seed_job._kill(None, {"pgid": 0})
