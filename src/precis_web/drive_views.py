"""Drive's "Saved views": the fixed preset list in the sidebar.

These replaced the top-nav Browse dropdown. Each preset is a Drive facet
URL (``k=`` kind, optional ``tag=``); :func:`active_view` names the one the
current request is showing so the sidebar can highlight it. Nanopubs is a
plain link to its workbench, so it is never "active" here.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode


@dataclass(frozen=True)
class DriveView:
    key: str
    label: str
    kind: str
    tag: str = ""
    submitted: bool = False

    @property
    def href(self) -> str:
        params: list[tuple[str, str]] = []
        if self.submitted:
            params.append(("submitted", "1"))
        params.append(("k", self.kind))
        if self.tag:
            params.append(("tag", self.tag))
        if not self.submitted:
            params.append(("folder", "*"))
        params.append(("sort", "recency"))
        return "/drive?" + urlencode(params, safe=":*")


DRIVE_VIEWS: tuple[DriveView, ...] = (
    DriveView("drafts", "Drafts", "draft"),
    DriveView("papers", "Papers", "paper"),
    DriveView("schedules", "Schedules", "todo", tag="level:recurring", submitted=True),
    DriveView("structure", "Structures", "structure"),
    DriveView("cad", "CAD", "cad"),
    DriveView("pcb", "PCB", "pcb"),
    DriveView("figure", "Figures", "figure"),
    DriveView("mermaid", "Mermaid", "mermaid"),
)

#: Link-only entries after the presets: (label, href).
DRIVE_VIEW_LINKS: tuple[tuple[str, str], ...] = (("Nanopubs", "/nanopub"),)


def active_view(kinds: list[str], tags: list[str]) -> str:
    """Key of the preset whose kind and tags equal the request's, else ``""``."""
    ks = {k.strip().lower() for k in kinds if k.strip()}
    ts = {t.strip() for t in tags if t.strip()}
    for v in DRIVE_VIEWS:
        if ks == {v.kind} and ts == ({v.tag} if v.tag else set()):
            return v.key
    return ""
