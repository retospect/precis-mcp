"""Read-only anchor checks against one pinned main commit.

Working-tree files and process-local handler caches are not a main snapshot.
Instead, lazily feed committed blobs through the same Python indexer and
markdown chunk parser. Missing tools, unreadable blobs and parse failures are
unknowns, never proof of rot. No checkout, fetch or on-disk cache is needed.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.python_index.types import ModuleIndex

_GIT_TIMEOUT_S = 5.0


def _assignment_qualnames(source: str, qualname: str) -> set[str]:
    """Supplement the symbol index with module/class assignments, never locals."""
    names: set[str] = set()

    def walk(node: ast.AST, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return
        if isinstance(node, ast.ClassDef):
            prefix = f"{prefix}.{node.name}"
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(
                    f"{prefix}.{child.id}"
                    for child in ast.walk(target)
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store)
                )
        for child in ast.iter_child_nodes(node):
            walk(child, prefix)

    walk(ast.parse(source), qualname)
    return names


def _main_root() -> Path | None:
    # The general alias parser lives in handlers; garden must not import it.
    from precis.config import load_config

    for entry in (load_config().python_roots or "").split(","):
        alias, sep, raw = entry.partition(":")
        if sep and alias.strip() == "main" and raw.strip():
            root = Path(raw.strip()).expanduser().resolve()
            if root.is_dir():
                return root
    return None


class AnchorResolver:
    """Resolve anchors with three outcomes: exists, absent, or not checked.

    ``check`` returns ``(bool | None, tried)``; ``None`` must never become a
    rot claim. ``main_sha`` is immutable for this resolver's lifetime, even
    if the local main branch advances during the scan.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else _main_root()
        self.main_sha: str | None = None
        self._unavailable = "main root is not configured"
        self._paths: set[str] = set()
        self._regular: set[str] = set()
        self._modules: dict[str, list[str]] = {}
        self.python_packages: set[str] = set()
        self._python: dict[str, ModuleIndex | None] = {}
        self._constants: dict[str, set[str]] = {}
        self._results: dict[
            tuple[str, str, str | None], tuple[bool | None, list[str]]
        ] = {}
        if self.root is None:
            return
        try:
            sha = self._git("rev-parse", "--verify", "refs/heads/main^{commit}")
            self.main_sha = sha.decode("ascii").strip()
            tree = self._git("ls-tree", "-rz", "--full-tree", self.main_sha)
            for row in tree.split(b"\0"):
                if not row:
                    continue
                meta, raw_path = row.split(b"\t", 1)
                mode, kind, _oid = meta.decode("ascii").split()
                path = raw_path.decode("utf-8")
                self._paths.add(path)
                self._paths.update(
                    str(p) for p in PurePosixPath(path).parents if str(p) != "."
                )
                if kind == "blob" and mode in {"100644", "100755"}:
                    self._regular.add(path)
            for path in sorted(self._regular):
                if path.endswith(".py"):
                    self._modules.setdefault(self._qualname(path), []).append(path)
                if PurePosixPath(path).name == "__init__.py":
                    self.python_packages.add(self._qualname(path).split(".", 1)[0])
            self._unavailable = ""
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
            self._unavailable = f"git main snapshot unavailable: {type(exc).__name__}"

    def _git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        ).stdout

    def _qualname(self, path: str) -> str:
        file = PurePosixPath(path)
        parts: list[str] = [] if file.name == "__init__.py" else [file.stem]
        parent = file.parent
        while str(parent / "__init__.py") in self._regular:
            parts.insert(0, parent.name)
            if parent == parent.parent:
                break
            parent = parent.parent
        return ".".join(parts) if parts else file.parent.name

    def check(
        self, kind: str, target: str, detail: str | None = None
    ) -> tuple[bool | None, list[str]]:
        key = (kind, target, detail)
        cached = self._results.get(key)
        if cached is not None:
            return cached
        tried = [f"{kind}: {target}" + (f" :: {detail}" if detail else "")]
        if self._unavailable:
            return None, [*tried, self._unavailable]
        tried.append(f"main:{self.main_sha}")
        try:
            if kind == "python":
                result = self._check_python(target, detail, tried)
            elif kind == "path":
                result = target in self._paths
            elif kind == "section":
                result = self._check_section(target, detail or "", tried)
            elif kind == "commit":
                result = self._check_commit(target, tried)
            else:
                tried.append("unsupported anchor class")
                result = None
        except (
            ImportError,
            OSError,
            subprocess.SubprocessError,
            SyntaxError,
            UnicodeError,
            ValueError,
        ) as exc:
            tried.append(f"lookup unavailable: {type(exc).__name__}")
            result = None
        self._results[key] = (result, tried)
        return result, tried

    def _check_python(
        self, target: str, detail: str | None, tried: list[str]
    ) -> bool | None:
        try:
            from precis.python_index.indexer import index_module_bytes
        except ImportError:
            tried.append("python index unavailable")
            return None
        paths: list[str]
        if detail is not None:
            paths = [target] if target in self._regular else []
            if target in self._paths and not paths:
                tried.append("python anchor is not a regular file")
                return None
        else:
            # A dotted symbol belongs to the longest indexed module prefix.
            parts = target.split(".")
            paths = []
            for size in range(len(parts), 0, -1):
                paths = self._modules.get(".".join(parts[:size]), [])
                if paths:
                    break
        if not paths:
            tried.append("no Python module in pinned tree")
            return False
        unknown = False
        for path in paths:
            qualname = self._qualname(path)
            wanted = detail if detail is not None else target
            if detail is not None and not (
                wanted == qualname or wanted.startswith(qualname + ".")
            ):
                wanted = f"{qualname}.{wanted}"
            tried.append(f"python index {path}::{wanted}")
            if path not in self._python:
                raw = self._git("show", f"{self.main_sha}:{path}")
                self._python[path] = index_module_bytes(
                    raw, qualname=qualname, file_relative=path, filename=path
                )
            module = self._python[path]
            if module is None or module.parse_error:
                tried.append("python index parse failed")
                unknown = True
            elif module.by_qualname(wanted) is not None:
                return True
            elif module.source is None:
                tried.append("python source unavailable for assignment lookup")
                unknown = True
            else:
                # ModuleIndex deliberately omits assignments; durable anchors
                # also allow constants, resolved from the same committed source.
                if path not in self._constants:
                    self._constants[path] = _assignment_qualnames(
                        module.source, qualname
                    )
                if wanted in self._constants[path]:
                    return True
        return None if unknown else False

    def _check_section(self, path: str, section: str, tried: list[str]) -> bool | None:
        from precis.utils.md_parse import parse_markdown

        if path not in self._paths:
            tried.append("named document absent from pinned tree")
            return False
        if path not in self._regular:
            tried.append("named document is not a regular file")
            return None
        source = self._git("show", f"{self.main_sha}:{path}").decode("utf-8")
        headings = [
            block for block in parse_markdown(source) if block.kind == "heading"
        ]
        tried.append("md heading chunks in named document")
        if section.startswith("#"):
            return any(block.slug == section[1:] for block in headings)
        wanted = " ".join(section.casefold().split())
        for block in headings:
            title = re.sub(r"^#{1,6}\s+|\s+#+\s*$", "", block.text.strip())
            title = " ".join(title.casefold().split())
            # A prose § citation may continue after the heading name. Resolve
            # that prefix conservatively rather than declaring the whole
            # sentence a missing heading; numbered titles keep their dots.
            if wanted == title or (
                wanted.startswith(title)
                and len(wanted) > len(title)
                and not wanted[len(title)].isalnum()
                and wanted[len(title)] != "_"
            ):
                return True
        return False

    def _check_commit(self, target: str, tried: list[str]) -> bool | None:
        if not (
            re.fullmatch(r"[a-fA-F0-9]{7,40}", target)
            and re.search(r"[a-fA-F]", target)
            and re.search(r"[0-9]", target)
        ):
            tried.append("not a mixed letter-digit commit abbreviation")
            return None
        matches = self._git(
            "rev-parse", f"--disambiguate={target.lower()}"
        ).splitlines()
        tried.append("git object abbreviation lookup")
        if len(matches) > 1:
            tried.append("ambiguous object abbreviation")
            return None
        if not matches:
            shallow = self._git("rev-parse", "--is-shallow-repository").strip()
            if shallow != b"false":
                tried.append("missing object in shallow repository")
                return None
            return False
        object_type = self._git("cat-file", "-t", matches[0].decode("ascii")).strip()
        tried.append(f"git object type: {object_type.decode('ascii')}")
        return object_type == b"commit"
