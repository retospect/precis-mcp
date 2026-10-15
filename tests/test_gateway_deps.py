"""The shared MCP gateway venv carries every client a served path imports.

gr489310: the gateway installs ``precis-mcp[web,estimate]``
(``deploy/roles/precis_web/tasks/install.yml``; it also named ``pcb``,
an extra deleted from pyproject, which uv ignores). ``semanticscholar``
and ``habanero`` lived in ``[paper]``, so every paper acquire served
there minted its stub unverified ("No module named 'semanticscholar'")
and the provenance kind stayed dark. This test pins the gateway's extra
set and checks that core deps plus those extras cover the
gateway-served imports, so a re-gating or an install edit fails here
instead of on prod.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_YML = ROOT / "deploy" / "roles" / "precis_web" / "tasks" / "install.yml"

#: The extras the gateway's main install line requests. Changing the
#: install line means updating this pin (and re-checking the list below).
GATEWAY_EXTRAS = frozenset({"web", "estimate"})

#: Distributions imported on gateway-served paths: paper acquire and
#: stub enrich (semanticscholar), the provenance kind and citation
#: resolution (habanero).
GATEWAY_REQUIRED = ("semanticscholar", "habanero")


def _dist_name(req: str) -> str:
    return re.split(r"[\s<>=!~;\[]", req.strip(), maxsplit=1)[0].lower()


def _gateway_extras() -> set[str]:
    text = INSTALL_YML.read_text(encoding="utf-8")
    m = re.search(r"Install precis-mcp\[([^\]]+)\] into shared MCP venv", text)
    assert m, "gateway install task not found in precis_web/tasks/install.yml"
    named = {e.strip() for e in m.group(1).split(",")}
    installed = re.search(r"'precis-mcp\[([^\]]+)\] @ git\+", text)
    assert installed, "gateway install command not found"
    assert {e.strip() for e in installed.group(1).split(",")} == named
    return named


def test_gateway_extra_set_is_pinned() -> None:
    assert _gateway_extras() == GATEWAY_EXTRAS


def test_gateway_extras_exist_in_pyproject() -> None:
    """uv ignores an unknown extra with a warning, so a deleted extra left
    on the install line (``pcb`` until gr489310) installs nothing."""
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["optional-dependencies"]
    assert _gateway_extras() <= set(extras)


def test_gateway_venv_carries_served_clients() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    dists = {_dist_name(r) for r in project["dependencies"]}
    for extra in _gateway_extras():
        dists |= {_dist_name(r) for r in project["optional-dependencies"][extra]}
    missing = [d for d in GATEWAY_REQUIRED if d not in dists]
    assert not missing, (
        f"gateway venv (core + [{','.join(sorted(_gateway_extras()))}]) lacks "
        f"{missing}; keep them core or add their extra to the gateway install"
    )
