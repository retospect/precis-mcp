"""Workspace layout convention — (kind, name) → relative path.

The LLM expresses INTENT (``put(kind='tex', name='intro', text=...)``);
this module turns it into the right physical path inside the workspace.
Caller code (file-kind handlers) calls :func:`resolve` with the
workspace + kind + name; it returns the path relative to the workspace
root.

The dict lives in one place so adding a new file kind or convention
is a one-line change.
"""

from __future__ import annotations

import re

#: Slug-safety pattern for ``name`` values the LLM passes. Lowercase
#: a-z, digits, hyphens, underscores, and a single optional ``.<ext>``
#: tail. Forbids slashes (you can't escape the workspace via name=).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9]+)?$")


def is_valid_name(name: str) -> bool:
    """Return True when ``name`` is safe to route via the convention."""
    return bool(_NAME_RE.match(name))


def resolve(
    *,
    format: str,
    kind: str,
    name: str,
) -> str:
    """Return the workspace-relative path for ``(kind, name)``.

    Raises :class:`ValueError` if the kind isn't routable in this
    format or the name is invalid. Callers (file-kind handlers) catch
    and re-raise as ``BadInput`` so the LLM sees a friendly error.

    Examples (format='tex')::

        resolve(format='tex', kind='tex', name='intro')  → 'tex/intro.tex'
        resolve(format='tex', kind='tex', name='main')   → 'main.tex'    # entrypoint
        resolve(format='tex', kind='pic', name='timeline.svg') → 'pics/timeline.svg'

    Examples (format='md')::

        resolve(format='md', kind='markdown', name='intro') → 'sections/intro.md'
        resolve(format='md', kind='markdown', name='main')  → 'main.md'
    """
    if not is_valid_name(name):
        raise ValueError(
            f"invalid name {name!r}: lowercase a-z 0-9 hyphens underscores "
            "with optional .ext"
        )

    if format == "tex":
        return _resolve_tex(kind, name)
    if format == "md":
        return _resolve_md(kind, name)
    raise ValueError(f"unsupported workspace format {format!r}")


def _resolve_tex(kind: str, name: str) -> str:
    # Entrypoint special-case.
    if kind == "tex" and name == "main":
        return "main.tex"
    if kind == "tex":
        ext = ".tex" if "." not in name else ""
        return f"tex/{name}{ext}"
    if kind == "pic":
        return f"pics/{name}"
    if kind == "data":
        return f"data/{name}"
    if kind == "markdown":
        # Even in a tex workspace, allow notes/README.md style.
        if name == "README":
            return "README.md"
        return f"notes/{name}.md" if "." not in name else f"notes/{name}"
    raise ValueError(f"kind {kind!r} not routable in tex workspace")


def _resolve_md(kind: str, name: str) -> str:
    if kind == "markdown" and name == "main":
        return "main.md"
    if kind == "markdown":
        ext = ".md" if "." not in name else ""
        return f"sections/{name}{ext}"
    if kind == "pic":
        return f"pics/{name}"
    if kind == "data":
        return f"data/{name}"
    raise ValueError(f"kind {kind!r} not routable in md workspace")


#: File names that are workspace-generated (not LLM-writable). The
#: handler should refuse a put against these to prevent the LLM from
#: stomping on auto-generated artifacts.
GENERATED_FILES: frozenset[str] = frozenset({"refs.bib"})


def is_generated(workspace_relpath: str) -> bool:
    """True when ``workspace_relpath`` is a generated artifact off-limits to put.

    Compared against the basename of the relative path.
    """
    return workspace_relpath.split("/")[-1] in GENERATED_FILES


__all__ = [
    "GENERATED_FILES",
    "is_generated",
    "is_valid_name",
    "resolve",
]
