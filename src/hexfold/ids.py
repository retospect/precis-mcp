"""Atom path IDs and ordinal assignment (SPEC section 5).

    <instance>/(u,v,s)        native lattice site
    <instance>/d<i>/(u,v,s)   atom created by defect i's inserted wedge
    <fragment>.<label>        atom inside a foreign fragment (phase 2)

Ordinals are flat integers assigned by sorting (instance, path).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .lattice import Site

_PATH_RE = re.compile(
    r"^(?P<inst>[A-Za-z_][\w.]*)/(?P<h>H/)?(?:d(?P<d>\d+)/)?"
    r"\((?P<u>-?\d+),(?P<v>-?\d+),(?P<s>[AB])\)$"
)
_LABEL_RE = re.compile(r"^(?P<inst>[A-Za-z_][\w.]*)/(?P<label>s\d+)$")


@dataclass(frozen=True)
class AtomPath:
    instance: str
    site: Site
    defect: int | None = None  # inserted-wedge index d<i>
    h: bool = False  # termination atom hanging off this site
    # a non-lattice atom's own label, e.g. a seam atom "s<i>" (SPEC 11.3):
    # overrides `site` for display; `site` still holds a placeholder so
    # the field stays required everywhere else that reads a path.
    label: str | None = None

    def __str__(self) -> str:
        if self.label is not None:
            return f"{self.instance}/{self.label}"
        d = f"d{self.defect}/" if self.defect is not None else ""
        hh = "H/" if self.h else ""
        return f"{self.instance}/{hh}{d}{self.site}"

    @staticmethod
    def parse(text: str) -> AtomPath:
        lm = _LABEL_RE.match(text.strip())
        if lm:
            # a seam atom "<seam>/s<i>" (SPEC 9, 11.3): same placeholder
            # site the builder mints it with, so parse(str(p)) == p
            return AtomPath(lm["inst"], Site(0, 0, 0), label=lm["label"])
        m = _PATH_RE.match(text.strip())
        if not m:
            raise ValueError(f"bad atom path: {text!r}")
        return AtomPath(
            m["inst"],
            Site(int(m["u"]), int(m["v"]), 0 if m["s"] == "A" else 1),
            int(m["d"]) if m["d"] is not None else None,
            m["h"] is not None,
        )


def path_key(p: AtomPath) -> tuple:
    return (p.instance, str(p))


def assign_ordinals(paths: list[AtomPath]) -> dict[AtomPath, int]:
    """path -> ordinal, atoms sorted by (instance, path), 0-based."""
    return {p: i for i, p in enumerate(sorted(paths, key=path_key))}
