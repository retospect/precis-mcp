"""The one embeddable summary per design — the ``card_text`` a save
stores so ``search(kind='structure')`` lands on intent, not just
composition.

Lives in the IR core (not ``handlers/structure``) so every write-back
path with no live handler instance — ``struct_search``'s candidate
write-back, ``struct_neb``'s TS-design mint — builds the same card text
the handler's own ``put``/``edit`` paths do, under the import-linter
contract "workers import store and utils, never handlers".
"""

from __future__ import annotations

from . import probe
from .fragment_names import fragment_names
from .scene import Scene


def card_text(title: str, scene: Scene, description: str = "") -> str:
    """Title + composition + the LLM's own description, one line.

    :meth:`StructureHandler._card_text` delegates here, so handler and
    worker write paths can never grow two driftable definitions."""
    t = probe.toc(scene)
    pbc = "".join("T" if p else "F" for p in scene.cell.pbc)
    elements = ", ".join(sorted(scene.composition()))
    intent = f" {description}" if description else ""
    names = fragment_names(scene)
    groups = f" Groups: {'; '.join(names)}." if names else ""
    return (
        f"{title} (atomistic structure).{intent} Composition: {t['formula']} "
        f"({elements}); {t['natoms']} atoms, {t['nbonds']} bonds; pbc[{pbc}]."
        f"{groups}"
    )
