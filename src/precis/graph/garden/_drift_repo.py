"""Read-only content drift checks against committed repo history.

The baseline is the first-parent main snapshot preceding the memory's touch,
not a working-tree file or a file mtime. Missing history and ambiguous anchors
are unknowns. Comparing only the selected subtree avoids treating an unrelated
edit in the same file as evidence that the cited claim needs review.
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.graph.garden._anchors import AnchorResolver


def _section_text(source: str, heading: str) -> str:
    from precis.utils.md_parse import parse_markdown

    headings = [b for b in parse_markdown(source) if b.kind == "heading"]
    wanted = " ".join(heading.strip().casefold().split())
    exact: list[int] = []
    prefixes: list[int] = []
    for index, block in enumerate(headings):
        title = re.sub(r"^#{1,6}\s+|\s+#+\s*$", "", block.text.strip())
        title = " ".join(title.casefold().split())
        if wanted.startswith(("#", "~")):
            if block.slug == wanted[1:]:
                exact.append(index)
        else:
            if wanted in {title, block.slug}:
                exact.append(index)
            elif (
                wanted.startswith(title)
                and len(wanted) > len(title)
                and not wanted[len(title)].isalnum()
                and wanted[len(title)] != "_"
            ):
                prefixes.append(index)
    matches = exact or prefixes
    if len(matches) != 1:
        raise ValueError("section is missing or ambiguous")
    index = matches[0]
    selected = headings[index]
    lines = source.splitlines(keepends=True)
    stop = len(lines)
    for following in headings[index + 1 :]:
        if (following.heading_level or 0) <= (selected.heading_level or 0):
            stop = following.line_start - 1
            break
    return "".join(lines[selected.line_start - 1 : stop]).rstrip()


class RepoDriftResolver(AnchorResolver):
    """Compare selected section/symbol bytes at two immutable Git snapshots."""

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root)
        self._baselines: dict[datetime, str | None] = {}
        self._drifts: dict[tuple[str, str, str | None, datetime], dict[str, Any]] = {}
        self._full_history: bool | None = None

    def _baseline(self, touched: datetime) -> str:
        if self._unavailable or self.main_sha is None:
            raise ValueError(self._unavailable or "main snapshot unavailable")
        if touched.tzinfo is None:
            raise ValueError("memory touch timestamp has no timezone")
        if self._full_history is None:
            self._full_history = (
                self._git("rev-parse", "--is-shallow-repository").strip() == b"false"
            )
        if not self._full_history:
            raise ValueError("prior snapshot unavailable in shallow history")
        if touched not in self._baselines:
            raw = self._git(
                "rev-list",
                "--first-parent",
                "--max-count=1",
                f"--before={touched.astimezone(UTC).isoformat()}",
                self.main_sha,
            )
            self._baselines[touched] = raw.decode("ascii").strip() or None
        baseline = self._baselines[touched]
        if baseline is None:
            raise ValueError("no main snapshot before memory touch")
        return baseline

    def _symbol_text(self, sha: str, target: str, detail: str | None) -> str:
        from precis.python_index.indexer import index_module_bytes

        paths: list[str] = []
        if detail is not None:
            if target in self._regular:
                paths = [target]
        else:
            parts = target.split(".")
            for size in range(len(parts), 0, -1):
                paths = self._modules.get(".".join(parts[:size]), [])
                if paths:
                    break
        if len(paths) != 1:
            raise ValueError("Python module is missing or ambiguous")
        path = paths[0]
        qualname = self._qualname(path)
        wanted = detail if detail is not None else target
        if detail is not None and not (
            wanted == qualname or wanted.startswith(qualname + ".")
        ):
            wanted = f"{qualname}.{wanted}"
        raw = self._git("show", f"{sha}:{path}")
        module = index_module_bytes(
            raw, qualname=qualname, file_relative=path, filename=path
        )
        if module.parse_error or module.source is None:
            raise ValueError("Python index unavailable")
        symbols = [symbol for symbol in module.symbols if symbol.qualname == wanted]
        if len(symbols) != 1:
            raise ValueError("Python symbol is missing or ambiguous")
        symbol = symbols[0]
        start = symbol.start_line
        # The index starts at def/class; include decorators in the same claim.
        for node in ast.walk(ast.parse(module.source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.lineno == start and node.decorator_list:
                    start = min(d.lineno for d in node.decorator_list)
                    break
        return "".join(
            module.source.splitlines(keepends=True)[start - 1 : symbol.end_line]
        ).rstrip()

    def _compare(
        self, kind: str, target: str, detail: str | None, touched: datetime
    ) -> dict[str, Any]:
        key = (kind, target, detail, touched)
        if key in self._drifts:
            return dict(self._drifts[key])
        result: dict[str, Any] = {"status": "not_checked", "main_sha": self.main_sha}
        try:
            prior = self._baseline(touched)
            assert self.main_sha is not None
            texts: list[str] = []
            for sha in (prior, self.main_sha):
                if kind in {"section", "document"}:
                    if target not in self._regular:
                        raise ValueError("document is not a regular file in main")
                    source = self._git("show", f"{sha}:{target}").decode("utf-8")
                    texts.append(
                        _section_text(source, detail or "")
                        if kind == "section"
                        else source
                    )
                else:
                    texts.append(self._symbol_text(sha, target, detail))
            old_sha, new_sha = (
                hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts
            )
            first_difference = next(
                (
                    i
                    for i, (old, new) in enumerate(zip(*texts, strict=False))
                    if old != new
                ),
                min(map(len, texts)),
            )
            start = max(0, first_difference - 300) if old_sha != new_sha else 0
            result.update(
                status="unchanged" if old_sha == new_sha else "changed",
                old_sha=old_sha,
                new_sha=new_sha,
                excerpt=texts[1][start : start + 1200],
                prior_main_sha=prior,
            )
        except (
            ImportError,
            OSError,
            subprocess.SubprocessError,
            SyntaxError,
            UnicodeError,
            ValueError,
        ) as exc:
            result["reason"] = (
                str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            )
        self._drifts[key] = result
        return dict(result)

    def section(self, path: str, heading: str, touched: datetime) -> dict[str, Any]:
        """Hash a heading and its full subtree, excluding sibling sections."""
        return self._compare("section", path, heading, touched)

    def python(
        self, target: str, detail: str | None, touched: datetime
    ) -> dict[str, Any]:
        """Hash one indexed symbol, including its decorators and body."""
        return self._compare("python", target, detail, touched)

    def document(self, path: str, touched: datetime) -> dict[str, Any]:
        """Hash an entire committed document, for file-backed skill contracts."""
        return self._compare("document", path, None, touched)
