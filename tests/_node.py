"""Shared ``node`` guard for the JS-under-pytest tests (gr495017).

``needs_node`` skips locally when ``node`` is not on PATH, but FAILS under
``CI=true``: check.yml installs node, so a missing one there is a broken
runner, and a silent skip would let a JS regression land on a green CI.
"""

from __future__ import annotations

import os
import shutil

import pytest

needs_node = pytest.mark.usefixtures("require_node")


@pytest.fixture
def require_node() -> str:
    """Path of ``node``; skip when absent locally, fail when absent in CI."""
    node = shutil.which("node")
    if node is not None:
        return node
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(
            "node is not on PATH under CI=true: check.yml must run "
            "actions/setup-node before pytest (gr495017)"
        )
    pytest.skip("node not available")
