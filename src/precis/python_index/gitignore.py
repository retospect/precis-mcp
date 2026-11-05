"""Hierarchical ``.gitignore`` matching for the python index walk.

The AST walk prunes directories the way ``git status`` would never
list them: a ``.gitignore`` applies to its own subtree, deeper files
override shallower ones, ``!`` negates, a trailing ``/`` marks a
directory-only rule, and a pattern containing ``/`` anchors to the
``.gitignore``'s directory while a slash-free pattern matches any path
component below it.

Deliberate simplifications vs gitignore(5): ``.git/info/exclude`` and
``core.excludesFile`` are not read, backslash escapes are not
honoured, and a ``.gitignore`` inside an ignored directory is never
seen — the same outcome git produces, since it cannot re-include a
path under an excluded directory either. An unreadable ``.gitignore``
contributes no rules, so a permission problem indexes MORE, never
less.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Rule:
    """One compiled ``.gitignore`` pattern.

    ``anchored`` (the pattern body contains ``/``) matches the
    candidate's path relative to the ``.gitignore``'s directory or an
    ancestor prefix of it; unanchored patterns match any single path
    component. ``dir_only`` (trailing ``/``) matches directories — for
    a file candidate it can still hit an ancestor directory, never the
    file itself.
    """

    negated: bool
    dir_only: bool
    anchored: bool
    regex: re.Pattern[str]

    def hits(self, parts: tuple[str, ...], *, is_dir: bool) -> bool:
        """Whether this pattern excludes the candidate ``parts``."""
        # A dir-only rule never names the candidate file itself, only
        # directories — the candidate dir, or an ancestor of a file.
        last = len(parts) if is_dir or not self.dir_only else len(parts) - 1
        if self.anchored:
            return any(
                self.regex.fullmatch("/".join(parts[:i])) for i in range(1, last + 1)
            )
        return any(self.regex.fullmatch(part) for part in parts[:last])


#: ``(dir carrying a .gitignore, its compiled rules)`` pairs, shallowest
#: first — the shape :func:`is_ignored` consumes.
Rulesets = tuple[tuple[Path, tuple[Rule, ...]], ...]


def read_rules(path: Path) -> tuple[Rule, ...]:
    """Compile the patterns of one ``.gitignore`` file.

    ``()`` when the file is absent or unreadable — an unreadable
    ignore file must not hide source, so it simply contributes no
    rules.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ()
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line[:-1]
        anchored = "/" in line
        line = line.lstrip("/")
        if not line:
            continue
        rules.append(Rule(negated, dir_only, anchored, re.compile(_ignore_glob(line))))
    return tuple(rules)


def is_ignored(
    rulesets: Rulesets,
    candidate: Path,
    *,
    is_dir: bool,
) -> bool:
    """Whether ``candidate`` is ignored under the accumulated rulesets.

    ``rulesets`` pairs each ancestor directory that carried a
    ``.gitignore`` (base dir → its rules), shallowest first: git gives
    deeper files precedence, so the last matching rule across all sets
    decides. A set applies only to candidates under its base.
    """
    ignored = False
    for base, rules in rulesets:
        if not rules:
            continue
        try:
            parts = candidate.relative_to(base).parts
        except ValueError:
            continue
        for rule in rules:
            if rule.hits(parts, is_dir=is_dir):
                ignored = not rule.negated
    return ignored


def _ignore_glob(pat: str) -> str:
    """gitignore glob → regex body: ``*``/``?``/``[...]`` never cross ``/``;
    ``**/`` matches any run of directories (including none) and a trailing
    ``**`` everything below."""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**/", i):
                out.append("(?:[^/]+/)*")
                i += 3
            elif pat.startswith("**", i) and i + 2 == n:
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pat[j] == "!":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j < n:
                inner = pat[i + 1 : j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]")
                i = j + 1
            else:
                out.append(re.escape(c))
                i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)
