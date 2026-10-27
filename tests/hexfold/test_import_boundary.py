"""hexfold imports no server code — the monorepo fold-in boundary.

`src/hexfold/` is MIT-marked and lives in this monorepo only to simplify
deployment during rapid development (`docs/backlog/hexfold-integration.md`,
spec §30 last bullet: "Monorepo for now ... imports no `precis*`"). A
server `precis`/`asa_bot`/`asa_slack` import under `src/hexfold/` would
wire the package to this repo and break the later pip re-export. The
independent workspace siblings precis_geom and precis_diag are allowed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_HEXFOLD_SRC = Path(__file__).resolve().parent.parent.parent / "src" / "hexfold"

#: Prefixes hexfold must never import — the monorepo-only surface.
_FORBIDDEN_PREFIXES = ("precis", "asa_bot", "asa_slack")

#: `precis_geom` is not the `precis` package: it is the workspace wheel's
#: script-side half, shipped inside precis-geom *alongside* hexfold
#: (structure-workspaces §Packaging). sheetnet imports its tracer
#: optionally (soft-import) so standalone hexfold still works without it.
#: The explicit hexfold.diag adapter also depends on the stdlib-only
#: precis_diag sibling, never on the server's precis package.
_WHEEL_SIBLINGS = ("precis_geom", "precis_diag")


def _is_forbidden(module: str | None) -> bool:
    if module is None:
        return False
    if any(module == s or module.startswith(s + ".") for s in _WHEEL_SIBLINGS):
        return False
    return module.startswith(_FORBIDDEN_PREFIXES)


def test_hexfold_imports_nothing_from_precis() -> None:
    offenders: list[str] = []
    for path in sorted(_HEXFOLD_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_forbidden(alias.name):
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if _is_forbidden(node.module):
                    offenders.append(f"{path}: from {node.module} import ...")
    assert not offenders, (
        "src/hexfold must not import precis/asa_bot/asa_slack "
        f"(monorepo fold-in boundary, spec §30): {offenders}"
    )
