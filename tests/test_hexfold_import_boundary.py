"""hexfold imports nothing from precis — the monorepo fold-in boundary.

`src/hexfold/` is MIT-marked and lives in this monorepo only to simplify
deployment during rapid development (`docs/backlog/hexfold-integration.md`,
spec §30 last bullet: "Monorepo for now ... imports no `precis*`"). A
`precis*`/`asa_bot`/`asa_slack` import anywhere under `src/hexfold/` would
wire the package to this repo and break the later pip re-export.
"""

from __future__ import annotations

import ast
from pathlib import Path

_HEXFOLD_SRC = Path(__file__).resolve().parent.parent / "src" / "hexfold"

#: Prefixes hexfold must never import — the monorepo-only surface.
_FORBIDDEN_PREFIXES = ("precis", "asa_bot", "asa_slack")


def _is_forbidden(module: str | None) -> bool:
    if module is None:
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
