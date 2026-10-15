"""Regression for gr486952: the precis-aizynth image failed to build on
castor (aarch64) — ``pip install linchemin`` pulls hdbscan, which publishes
no aarch64 wheel, so pip compiles it from source and ``python:3.11-slim``
ships no C/C++ toolchain (``gcc: No such file or directory``).

The fix builds the LinChemIn venv in a ``linchemin-builder`` stage that
installs ``build-essential``, and the runtime image only
``COPY --from=``s the finished venv — the toolchain never ships. These
tests pin that shape plus two invariants the role comments name: the
pre-pulled ``aizynth_base_image`` must equal the Dockerfile ``FROM`` (the
build resolves it from the local store), and the build task keeps
``DOCKER_BUILDKIT=0`` (deliberate — BuildKit's registry metadata HEAD
reintroduces the flaky-Docker-Hub failure mode; see the role README).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "docker" / "aizynth" / "Dockerfile"
_DEFAULTS = _REPO_ROOT / "deploy" / "roles" / "aizynth" / "defaults" / "main.yml"
_TASKS = _REPO_ROOT / "deploy" / "roles" / "aizynth" / "tasks" / "main.yml"


def _stages() -> list[str]:
    """Dockerfile split into stages on ``FROM`` lines (first element holds
    any preamble)."""
    return re.split(r"(?im)^from\s+", _DOCKERFILE.read_text(encoding="utf-8"))


def _builder_stage() -> str:
    matches = [
        s
        for s in _stages()
        if re.match(r"[\w.:-]+\s+as\s+linchemin-builder\b", s, re.IGNORECASE)
    ]
    assert len(matches) == 1, (
        "Dockerfile must carry a `FROM <base> AS linchemin-builder` stage — "
        "hdbscan (a linchemin dep) has no aarch64 wheel and compiles from "
        "source (gr486952)"
    )
    return matches[0]


def _final_stage() -> str:
    return _stages()[-1]


def test_builder_stage_has_toolchain() -> None:
    stage = _builder_stage()
    assert "build-essential" in stage, (
        "linchemin-builder stage lost its C/C++ toolchain — hdbscan fails "
        "to compile on aarch64 (gr486952)"
    )
    assert "linchemin" in stage, (
        "the `pip install linchemin` moved out of the toolchain stage — "
        "the runtime image would need gcc again"
    )


def test_runtime_image_carries_no_toolchain() -> None:
    final = _final_stage()
    assert re.search(r"(?im)^copy\s+--from=linchemin-builder\b", final), (
        "the final stage must COPY --from=linchemin-builder the built venv"
    )
    assert "build-essential" not in final, (
        "build-essential reached the runtime stage — install it only in "
        "linchemin-builder so the shipped image stays lean"
    )


def test_prepulled_base_image_matches_dockerfile_from() -> None:
    """`aizynth_base_image` is pre-pulled so the legacy-builder build
    resolves FROM from the local store without a registry HEAD — it must
    stay in sync with the Dockerfile (the defaults comment says so)."""
    defaults = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))
    from_images = {
        m.group(1)
        for m in re.finditer(
            r"(?im)^from\s+([\w./:-]+)", _DOCKERFILE.read_text(encoding="utf-8")
        )
    }
    assert from_images == {defaults["aizynth_base_image"]}, (
        "Dockerfile FROM(s) and aizynth_base_image diverged — the pre-pull "
        "no longer covers the build's base"
    )


def _all_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tasks:
        out.append(t)
        out.extend(_all_tasks(t.get("block") or []))
    return out


def test_node_build_keeps_legacy_builder() -> None:
    tasks: list[dict[str, Any]] = yaml.safe_load(_TASKS.read_text(encoding="utf-8"))
    build = [
        t
        for t in _all_tasks(tasks)
        if t.get("name", "").startswith("Build the precis-aizynth image")
    ]
    assert len(build) == 1
    env = build[0].get("environment", {})
    assert env.get("DOCKER_BUILDKIT") == "0", (
        "the on-node build must stay on the legacy builder: BuildKit HEADs "
        "the registry for FROM even when the base is cached, and the node's "
        "Docker Hub link is flaky (see roles/aizynth/README.md)"
    )
