"""Pin: the public GHCR image must build without the private catpath dep.

publish.yml's ``publish-image`` job (docker/build-push-action →
``docker/Dockerfile`` ``runtime`` target) runs on a credential-less GitHub
runner. Until v8.36.0 (run 38545276118) the ``deps`` stage synced
``--all-extras``, which includes ``catalyst``/``catalyst-gpu`` — both pull
``autocatpath`` from the PRIVATE ``retospect/catpath`` git repo
(``[tool.uv.sources]``), so the build died on "fatal: could not read
Username for 'https://github.com'". The ruling (Reto 2026-10-01): catpath
stays private until the paper, so the public image must not contain it —
the fix is to subtract the private extras in the Dockerfile's default,
not to feed credentials into the public pipeline.

These tests pin that shape: the ``deps`` sync is driven by
``PRECIS_DEPS_EXTRAS`` whose *default* subtracts both private extras (the
same ``--all-extras --no-extra ...`` selection check.yml already uses),
no ``uv sync`` in the file hard-codes a bare ``--all-extras``, and the
private dev/agent image build (docker/dev compose, driven by
scripts/build-image) opts back in to ``--all-extras`` so it keeps
autocatpath via its gh_token secret.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "docker" / "Dockerfile"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_COMPOSE_FILES = (
    _REPO_ROOT / "docker" / "dev" / "compose.yaml",
    _REPO_ROOT / "docker" / "dev" / "compose.mirror.local.yaml",
)

#: The extras whose deps resolve from the private catpath repo.
_PRIVATE_EXTRAS = ("catalyst", "catalyst-gpu")


def _dockerfile() -> str:
    return _DOCKERFILE.read_text(encoding="utf-8")


def _extras_arg_default() -> str:
    m = re.search(
        r'^ARG\s+PRECIS_DEPS_EXTRAS="([^"]*)"\s*$', _dockerfile(), re.MULTILINE
    )
    assert m, "Dockerfile must declare `ARG PRECIS_DEPS_EXTRAS` for the deps stage"
    return m.group(1)


def test_deps_extras_default_subtracts_private_extras() -> None:
    """The bare ``docker build`` / publish.yml path (no build args) must not
    request the private extras — a credential-less runner can satisfy every
    other extra but never the git-sourced autocatpath."""
    default = _extras_arg_default()
    assert "--all-extras" in default
    for extra in _PRIVATE_EXTRAS:
        assert f"--no-extra {extra}" in default, (
            f"public-image default must subtract the private `{extra}` extra"
        )


def test_deps_stage_syncs_via_the_arg() -> None:
    """The deps-stage sync must consume the arg — a hard-coded extras list
    would silently bypass the private-build override."""
    assert re.search(
        r"uv sync --frozen --no-install-project \$\{?PRECIS_DEPS_EXTRAS\}?",
        _dockerfile(),
    ), "deps stage must `uv sync ... ${PRECIS_DEPS_EXTRAS}`"


def test_no_uv_sync_hardcodes_all_extras() -> None:
    """No ``uv sync`` invocation anywhere in the Dockerfile may pass a
    literal ``--all-extras`` — the flag set must live in the arg default so
    it stays subtracted for credential-less builds."""
    for lineno, line in enumerate(_dockerfile().splitlines(), start=1):
        if "uv sync" not in line:
            continue
        assert "--all-extras" not in line, (
            f"docker/Dockerfile:{lineno} hard-codes --all-extras; "
            "route extras through PRECIS_DEPS_EXTRAS"
        )


def test_private_extras_still_resolve_from_private_repo() -> None:
    """Pin the premise: the catalyst extras pull ``autocatpath`` and uv
    sources it from the private git repo — the reason the public image
    must not request them. If this test fails because autocatpath moved to
    PyPI, revisit whether the subtraction is still needed."""
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    optional = pyproject["project"]["optional-dependencies"]
    for extra in _PRIVATE_EXTRAS:
        assert extra in optional, f"expected a `{extra}` extra"
        assert any(dep.startswith("autocatpath") for dep in optional[extra]), (
            f"`{extra}` no longer depends on autocatpath"
        )
    source = pyproject["tool"]["uv"]["sources"]["autocatpath"]
    assert source.get("git") == "https://github.com/retospect/catpath", (
        "autocatpath no longer git-sourced from the private catpath repo"
    )


def test_private_dev_build_restores_all_extras() -> None:
    """The private dev/agent image keeps catalyst: each compose file's
    ``precis-dev`` build must pass ``PRECIS_DEPS_EXTRAS: --all-extras`` so
    the deps stage still syncs autocatpath (fetched via its gh_token
    secret — scripts/build-image exports GH_TOKEN)."""
    for compose_path in _COMPOSE_FILES:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        args = compose["services"]["precis-dev"]["build"]["args"]
        assert args.get("PRECIS_DEPS_EXTRAS") == "--all-extras", (
            f"{compose_path.name} precis-dev must opt back into "
            "--all-extras (the private image keeps catalyst)"
        )
